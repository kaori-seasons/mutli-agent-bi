"""Import-safe Flask application; authentication precedes model/data initialization."""
import os
import threading
import time
from uuid import uuid4
from urllib.parse import urlparse

from flask import Flask, g, jsonify, request, Response
from werkzeug.exceptions import HTTPException

from server.auth import authenticate
from server.errors import ServiceError, error_response


def create_app(query_service=None, verifier=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
    from server.ontology_api import blueprint
    app.register_blueprint(blueprint)
    initialized = False
    lock = threading.Lock()

    def service():
        nonlocal query_service, initialized
        with lock:
            if not initialized:
                if query_service is None:
                    from server import conf, api
                    conf.load_sql_templates()
                    api.init(load_templates=False)
                    query_service = api
                initialized = True
        return query_service

    @app.before_request
    def identity():
        g.trace_id = uuid4().hex
        if request.path not in {"/", "/healthz", "/readyz"}:
            g.principal = (verifier or authenticate)(request.headers.get("Authorization"))

    @app.after_request
    def response_headers(response):
        response.headers["X-Trace-Id"] = g.trace_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.errorhandler(ServiceError)
    def expected(error):
        app.logger.info("request_failed trace=%s code=%s", g.trace_id, error.code)
        return jsonify(error_response(error)), error.status

    @app.errorhandler(Exception)
    def unexpected(error):
        status = error.code if isinstance(error, HTTPException) else 500
        code = "INVALID_REQUEST" if isinstance(error, HTTPException) else "INTERNAL_ERROR"
        app.logger.error("request_failed trace=%s type=%s", g.trace_id, type(error).__name__)
        return jsonify(error_response(ServiceError(code, "请求未完成", status))), status

    @app.get("/")
    @app.get("/healthz")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/readyz")
    def readiness():
        from server import conf
        from server.auth import identity_configuration, source_grants
        identity_configuration()
        conf._load_db_conf()
        grants = source_grants()
        granted = {source for sources in grants.values() for source in sources}
        if not granted or not granted <= set(conf.mysql_info):
            raise ServiceError("NOT_READY", "数据源授权为空或引用了不存在的数据源", 503)
        conf.load_sql_templates(publish=False)
        from ontology.runtime import query_sources, namespace
        enabled = query_sources()
        if enabled:
            if not enabled <= granted:
                raise ServiceError("NOT_READY", "本体灰度源必须具有有效数据源授权", 503)
            from ontology.registry import Registry
            from server.auth import Principal
            Registry().pin(namespace(), Principal("readiness", frozenset(enabled)))
        return jsonify({"status": "ready", "model_probe": "not_run"})

    @app.get("/auth/session")
    def session():
        return jsonify({"subject": g.principal.subject, "admin": g.principal.admin,
                        "source_ids": sorted(g.principal.sources)})

    @app.post("/queryllm")
    def query():
        if request.content_length and request.content_length > 1024 * 1024:
            raise ServiceError("REQUEST_TOO_LARGE", "请求体超出上限", 413)
        payload = request.get_json()
        messages = payload.get("messages") if isinstance(payload, dict) else payload
        sources = payload.get("source_ids") if isinstance(payload, dict) else None
        ontology_options = payload.get("ontology") if isinstance(payload, dict) else None
        if not isinstance(messages, list) or not 1 <= len(messages) <= 40:
            raise ServiceError("INVALID_MESSAGES", "消息数量应为 1 到 40")
        clean = []
        for message in messages:
            if (not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}
                    or not isinstance(message.get("content"), str) or not 1 <= len(message["content"]) <= 16384):
                raise ServiceError("INVALID_MESSAGES", "消息格式无效")
            clean.append({"role": message["role"], "content": message["content"]})
        if clean[-1]["role"] != "user":
            raise ServiceError("INVALID_MESSAGES", "最后一条消息必须来自用户")
        from server import conf
        selected = conf.get_mysql_conf_by_question(clean[-1]["content"], g.principal.sources, sources)
        from server.execution import request_deadline, bounded_int
        token = request_deadline.set(time.monotonic() + bounded_int("QUERY_DEADLINE_SECONDS", 60, 1, 600))
        try:
            kwargs = {"principal": g.principal, "source_ids": [x["source_id"] for x in selected]}
            if ontology_options is not None:
                kwargs["ontology_options"] = ontology_options
            result = service().get_result(clean, g.trace_id, g.principal.subject,
                                          request.args.get("mtype", "normal"),
                                          **kwargs)
        finally:
            request_deadline.reset(token)
        return jsonify(result)

    @app.post("/upload")
    def upload():
        raise ServiceError("UPLOAD_NOT_IMPLEMENTED", "文件导入尚未实现，请使用离线审核流程", 501)

    @app.get("/downloads/<identity>")
    def download(identity):
        from server.exports import read_export
        data = read_export(g.principal, identity)
        return Response(data, mimetype="text/csv", headers={
            "Content-Disposition": 'attachment; filename="query-result.csv"'})

    return app


app = create_app()

if __name__ == "__main__":
    if os.getenv("APP_ENV_FILE"):
        from server import conf
        conf.load_env(os.environ["APP_ENV_FILE"])
    address = urlparse(os.getenv("SERVER_HOST", "http://127.0.0.1:5020"))
    app.run(host="0.0.0.0", port=address.port or 5020)
