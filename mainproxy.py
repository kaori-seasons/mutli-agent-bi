"""Optional model proxy. Private deployment plus explicit service bearer authentication."""
import hmac
import os
import time
from urllib.parse import urlparse

from flask import Flask, request
from werkzeug.exceptions import HTTPException
from server import llm, aws
from server.errors import ServiceError, error_response
from server.execution import request_deadline

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024


@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.errorhandler(ServiceError)
def fail(error):
    return error_response(error), error.status


@app.errorhandler(Exception)
def unexpected(error):
    app.logger.error("proxy_failed type=%s", type(error).__name__)
    if isinstance(error, HTTPException):
        return error_response(ServiceError("INVALID_REQUEST", "代理请求无效", error.code)), error.code
    return error_response(ServiceError("MODEL_UNAVAILABLE", "模型代理请求失败", 502)), 502


@app.post("/query")
def post_endpoint():
    secret = os.getenv("LLM_PROXY_TOKEN", "")
    if len(secret) < 32:
        raise ServiceError("PROXY_CONFIGURATION", "代理认证未配置", 503)
    provided = request.headers.get("Authorization", "")
    if not hmac.compare_digest(provided, "Bearer " + secret):
        raise ServiceError("AUTH_REQUIRED", "代理身份认证失败", 401)
    questions = request.get_json()
    if (not isinstance(questions, list) or not 1 <= len(questions) <= 40
            or any(not isinstance(x, dict) or x.get("role") not in {"user", "assistant"}
                   or not isinstance(x.get("content"), str) for x in questions)):
        raise ServiceError("INVALID_MESSAGES", "消息格式无效")
    token = request_deadline.set(time.monotonic() + 60)
    try:
        return llm.query(questions, bedrock_client=aws.get("bedrock-runtime", force_llm=True))
    finally:
        request_deadline.reset(token)


if __name__ == "__main__":
    if os.getenv("APP_ENV_FILE"):
        from server import conf
        conf.load_env(os.environ["APP_ENV_FILE"])
    address = urlparse(os.getenv("LLM_PROXY_SERVER", "http://127.0.0.1:5020"))
    app.run(host="0.0.0.0", port=address.port or 5020)
