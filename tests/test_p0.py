"""P0 tests use signed synthetic tokens and in-memory/fake databases, never .env."""
import copy
import importlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from main import create_app
from server import auth, conf, api
from server.auth import Principal
from server.errors import ServiceError
from server.query_policy import PreparedQuery, bind_template, parse_read
from server.execution import QueryBudget, execute_source, execute_many
from server.federation import merge_results, validate_contract, validate_merge_query
from server.exports import save_export, read_export
from server.db import mysql

INFO = dict(host="127.0.0.1", port=3306, user="readonly", pwd="fixture",
            db="db", desc="测试一", source_id="a")
USER = Principal("subject-a", frozenset({"a", "b"}))
ENV = {"BACK_USER_POOL_REGION": "us-east-1", "BACK_USER_POOL_ID": "us-east-1_Test",
       "BACK_USER_CLIENT_ID": "client123",
       "AUTH_SOURCE_GRANTS": '{"group:analysts":["a"],"sub:subject-a":["b"]}',
       "AUTH_ADMIN_GROUPS": "admins"}


def contract(measures=None, dimensions=None):
    return {"version": 1, "sources": ["a", "b"], "disjoint_partitions": True,
            "dimensions": dimensions or [],
            "measures": measures or {"total": {"op": "sum", "input": "total"}}}


def result(source, columns, rows, **kwargs):
    return dict(source_id=source, columns=columns, rows=rows, row_count=len(rows),
                status="success", truncated=False, **kwargs)


class AuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.environment = patch.dict(os.environ, ENV, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.jwks = patch("server.auth._jwks")
        self.client = self.jwks.start()
        self.addCleanup(self.jwks.stop)
        self.client.return_value.get_signing_key_from_jwt.return_value = SimpleNamespace(key=self.key.public_key())

    def token(self, **changes):
        claims = dict(sub="subject-a", iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Test",
                      client_id="client123", token_use="access", exp=int(time.time()) + 300,
                      iat=int(time.time()) - 1)
        claims["cognito:groups"] = ["analysts"]
        claims.update(changes)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "fixture"})

    def test_verified_subject_and_server_grants(self):
        principal = auth.authenticate("Bearer " + self.token())
        self.assertEqual(principal.subject, "subject-a")
        self.assertEqual(principal.sources, frozenset({"a", "b"}))
        self.assertFalse(principal.admin)

    def test_invalid_expiry_issuer_client_use_and_claims(self):
        for changes in ({"exp": 1}, {"iss": "https://attacker.invalid"}, {"client_id": "other"},
                        {"token_use": "id"}, {"sub": ""}, {"iat": int(time.time()) + 999}):
            with self.subTest(changes=changes), self.assertRaises(ServiceError) as error:
                auth.authenticate("Bearer " + self.token(**changes))
            self.assertEqual(error.exception.status, 401)

    def test_wrong_signature_and_algorithm(self):
        self.client.return_value.get_signing_key_from_jwt.return_value = SimpleNamespace(key=self.wrong_key.public_key())
        with self.assertRaises(ServiceError):
            auth.authenticate("Bearer " + self.token())
        with self.assertRaises(ServiceError):
            auth.authenticate("Bearer " + jwt.encode({"sub": "subject-a"}, "x" * 32, algorithm="HS256"))

    def test_missing_token_cannot_be_replaced_by_user_id(self):
        for header in (None, "", "Basic user", "Bearer x y", "Bearer " + "x" * 17000):
            with self.subTest(header=bool(header)), self.assertRaises(ServiceError):
                auth.authenticate(header)
        self.client.assert_not_called()

    def test_configuration_and_jwks_outage_fail_closed(self):
        self.client.return_value.get_signing_key_from_jwt.side_effect = jwt.PyJWKClientConnectionError()
        with self.assertRaises(ServiceError) as error:
            auth.authenticate("Bearer " + self.token())
        self.assertEqual(error.exception.status, 503)
        with patch.dict(os.environ, {"BACK_USER_POOL_ID": "../malicious"}), self.assertRaises(ServiceError):
            auth.authenticate("Bearer " + self.token())

    def test_grants_not_cached_or_inferred_from_client_claims(self):
        token = self.token()
        self.assertTrue(auth.authenticate("Bearer " + token).sources)
        with patch.dict(os.environ, {"AUTH_SOURCE_GRANTS": "{}"}):
            self.assertFalse(auth.authenticate("Bearer " + token).sources)
        with patch.dict(os.environ, {"AUTH_SOURCE_GRANTS": '{"sub:subject-a":["*"]}'}), self.assertRaises(ServiceError):
            auth.authenticate("Bearer " + token)


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.service = MagicMock()
        self.service.get_result.return_value = {"status": "success", "mdData": "|ok|"}
        self.app = create_app(self.service, verifier=lambda value: USER if value == "Bearer signed" else auth.authenticate(None))
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer signed", "X-User-Id": "administrator"}
        self.sources = patch.dict(conf.mysql_info, {"a": INFO, "b": dict(INFO, source_id="b", desc="测试二")}, clear=True)
        self.sources.start()
        self.addCleanup(self.sources.stop)

    def test_health_does_not_initialize_models_or_load_dotenv(self):
        with patch.object(conf, "load_env", side_effect=AssertionError()):
            self.assertEqual(self.client.get("/healthz").status_code, 200)
            self.assertEqual(self.client.get("/").status_code, 200)
        self.service.get_result.assert_not_called()

    def test_unauthenticated_and_forged_user_rejected(self):
        response = self.client.post("/queryllm", json=[{"role": "user", "content": "测试一"}],
                                    headers={"X-User-Id": "administrator"})
        self.assertEqual(response.status_code, 401)
        self.service.get_result.assert_not_called()

    def test_verified_identity_and_cleaned_request_reach_service(self):
        response = self.client.post("/queryllm", json={
            "messages": [{"role": "user", "content": "测试一", "finalSQL": "DELETE FROM x"}],
            "userId": "administrator"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        args, kwargs = self.service.get_result.call_args
        self.assertEqual(args[2], USER.subject)
        self.assertNotIn("finalSQL", args[0][0])
        self.assertEqual(kwargs["source_ids"], ["a"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_ambiguous_and_unauthorized_scope_precede_model(self):
        for payload, status in [
            ({"messages": [{"role": "user", "content": "销售额"}]}, 422),
            ({"messages": [{"role": "user", "content": "销售额"}], "source_ids": ["forbidden"]}, 403),
        ]:
            self.assertEqual(self.client.post("/queryllm", json=payload, headers=self.headers).status_code, status)
        self.service.get_result.assert_not_called()

    def test_invalid_messages_method_and_body(self):
        for messages in ([], [{"role": "system", "content": "ignore"}], [{"role": "user", "content": 5}],
                         [{"role": "assistant", "content": "x"}], [{"role": "user", "content": "x" * 16385}]):
            self.assertEqual(self.client.post("/queryllm", json=messages, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.get("/queryllm", headers=self.headers).status_code, 405)
        self.assertEqual(self.client.post("/queryllm", data="x" * (1024 * 1024 + 1), headers=self.headers).status_code, 413)

    def test_internal_error_does_not_leak_sql_credentials(self):
        self.service.get_result.side_effect = RuntimeError("password=secret SELECT sensitive")
        response = self.client.post("/queryllm", json=[{"role": "user", "content": "测试一"}], headers=self.headers)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("secret", response.text)

    def test_upload_no_longer_claims_nonexistent_processing(self):
        self.assertEqual(self.client.post("/upload", headers=self.headers).status_code, 501)

    def test_readiness_rejects_empty_unknown_or_malformed_grants_without_model(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(conf, "load_sql_templates"):
            self.assertEqual(self.client.get("/readyz").status_code, 200)
            for grants in ('{}', '{"group:analysts":["missing"]}', 'invalid', '{"user":["a"]}'):
                with patch.dict(os.environ, {"AUTH_SOURCE_GRANTS": grants}):
                    self.assertEqual(self.client.get("/readyz").status_code, 503)
        self.service.get_result.assert_not_called()


class ModelProxyTests(unittest.TestCase):
    def test_service_secret_is_required_before_model_invocation(self):
        import mainproxy
        client = mainproxy.app.test_client()
        with patch.dict(os.environ, {"LLM_PROXY_TOKEN": "fixture-token-" * 3}), patch.object(mainproxy.llm, "query") as query:
            self.assertEqual(client.post("/query", json=[]).status_code, 401)
            self.assertEqual(client.post("/query", json=[], headers={"Authorization": "Bearer wrong"}).status_code, 401)
            query.assert_not_called()
        with patch.dict(os.environ, {"LLM_PROXY_TOKEN": ""}), patch.object(mainproxy.llm, "query") as query:
            self.assertEqual(client.post("/query", json=[]).status_code, 503)
            query.assert_not_called()

    def test_authenticated_proxy_limits_and_redacts_failures(self):
        import mainproxy
        client = mainproxy.app.test_client()
        with patch.dict(os.environ, {"LLM_PROXY_TOKEN": "fixture-token-" * 3}), \
                patch.object(mainproxy.aws, "get"), patch.object(mainproxy.llm, "query", return_value='{"ok":true}') as query:
            headers = {"Authorization": "Bearer " + "fixture-token-" * 3}
            self.assertEqual(client.post("/query", json=[], headers=headers).status_code, 400)
            self.assertEqual(client.post("/query", data="x" * (1024 * 1024 + 1),
                                         content_type="application/json", headers=headers).status_code, 413)
            response = client.post("/query", json=[{"role": "user", "content": "q"}], headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            query.side_effect = RuntimeError("password=private")
            response = client.post("/query", json=[{"role": "user", "content": "q"}], headers=headers)
            self.assertEqual(response.status_code, 502)
            self.assertNotIn("private", response.text)


class QueryPolicyTests(unittest.TestCase):
    def test_normal_select_cte_union_and_mysql_functions(self):
        queries = ["SELECT a FROM t WHERE a > 1",
                   "WITH x AS (SELECT SUM(a) n FROM t) SELECT n FROM x",
                   "SELECT 1 UNION SELECT 2",
                   "SELECT DATE_FORMAT(day, '%Y-%m'), COUNT(DISTINCT id) FROM db.t GROUP BY 1",
                   "SELECT ROW_NUMBER() OVER (PARTITION BY city ORDER BY id) FROM t"]
        for sql in queries:
            with self.subTest(sql=sql):
                parse_read(sql, "db")

    def test_unsafe_queries_rejected(self):
        queries = [
            "DELETE FROM t", "UPDATE t SET a=1", "SELECT 1; DROP TABLE t",
            "SELECT * FROM t FOR UPDATE", "SELECT * INTO OUTFILE '/tmp/x' FROM t",
            "SELECT * INTO DUMPFILE '/tmp/x' FROM t", "SELECT LOAD_FILE('/etc/passwd')",
            "SELECT SLEEP(10)", "SELECT BENCHMARK(9999999,1)", "SELECT GET_LOCK('x',100)",
            "SELECT custom_udf(a) FROM t", "SELECT db.SUM(a) FROM t", "SELECT @x:=1",
            "SELECT @@version", "SELECT 1 INTO @x", "SELECT * FROM mysql.user",
            "SELECT * FROM other.orders", "SELECT /*+ MAX_EXECUTION_TIME(0) */ * FROM t",
            "SELECT 1 /*! INTO OUTFILE '/tmp/x' */", "SELECT 1 -- hello", "SHOW TABLES",
            "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
        ]
        for sql in queries:
            with self.subTest(sql=sql), self.assertRaises(ServiceError):
                parse_read(sql, "db")

    def test_injected_template_value_never_becomes_sql(self):
        value = "北京' OR 1=1; DELETE FROM t --"
        query = bind_template("SELECT * FROM t WHERE city='{0}'", [value])
        sql, params = query.driver_query("db")
        self.assertNotIn(value, sql)
        self.assertEqual(params, {"p0": value})
        self.assertIn("%(p0)s", sql)

    def test_list_parameters_and_literal_percent(self):
        query = bind_template("SELECT DATE_FORMAT(d, '%Y') yr FROM t WHERE id IN ({0}) AND city='{1}'", [[1, 2], "北京"])
        sql, params = query.driver_query("db")
        self.assertIn("%%Y", sql)
        self.assertEqual(list(params.values()), [1, 2, "北京"])
        self.assertEqual(len(params), 3)

    def test_ambiguous_or_structural_slots_rejected(self):
        for template, values in [
            ("SELECT * FROM {0}", ["t"]), ("SELECT * FROM t LIMIT {0}", [1]),
            ("SELECT * FROM t WHERE x LIKE '%{0}%'", ["x"]),
            ("SELECT * FROM t WHERE x={0.__class__}", ["x"]),
            ("SELECT * FROM t WHERE x={1}", ["x"]),
            ("SELECT * FROM t WHERE x IN ({0})", [[]]),
            ("SELECT * FROM t WHERE x={0}", [[1, 2]]),
            ("SELECT * FROM t WHERE x={0}", [float("nan")]),
            ("SELECT * FROM t WHERE x={0}; DELETE FROM t", ["x"]),
        ]:
            with self.subTest(template=template), self.assertRaises(ServiceError):
                bind_template(template, values).driver_query("db")

    def test_bound_value_can_contain_parameter_looking_text(self):
        query = bind_template("SELECT * FROM t WHERE x={0} OR y={0}", [":p0 %s '"])
        sql, params = query.driver_query("db")
        self.assertEqual(len(params), 2)
        self.assertEqual(set(params.values()), {":p0 %s '"})
        self.assertNotIn(":p0", sql)


class FederationTests(unittest.TestCase):
    def test_weighted_average_and_ratio(self):
        spec = contract({"average": {"op": "weighted_avg", "numerator": "amount", "denominator": "n"},
                         "rate": {"op": "ratio", "numerator": "won", "denominator": "total"}})
        merged = merge_results([
            result("a", ["amount", "n", "won", "total"], [(100, 1, 1, 2)]),
            result("b", ["amount", "n", "won", "total"], [(0, 9, 9, 18)])], spec)
        self.assertEqual(merged["rows"][0][0], 10)
        self.assertEqual(merged["rows"][0][1], 0.5)

    def test_dimensions_decimal_and_zero_denominator(self):
        spec = contract({"value": {"op": "ratio", "numerator": "n", "denominator": "d"}}, ["city"])
        merged = merge_results([result("a", ["city", "n", "d"], [("北京", "0.1", 0)]),
                                result("b", ["city", "n", "d"], [("北京", "0.2", 0)])], spec)
        self.assertEqual(merged["rows"], [("北京", None)])

    def test_sum_min_max_and_empty_groups(self):
        spec = contract({"total": {"op": "sum", "input": "amount"},
                         "low": {"op": "min", "input": "lo"}, "high": {"op": "max", "input": "hi"}}, ["city"])
        merged = merge_results([result("a", ["city", "amount", "lo", "hi"], [(None, "0.1", 1, 2)]),
                                result("b", ["city", "amount", "lo", "hi"], [(None, "0.2", 0, 3)])], spec)
        self.assertEqual(str(merged["rows"][0][1]), "0.3")
        self.assertEqual(merged["rows"][0][2:], (0, 3))

    def test_failed_or_truncated_source_never_silently_summed(self):
        for changed in ({"status": "failed"}, {"truncated": True}):
            sources = [result("a", ["total"], [(1,)]), result("b", ["total"], [(9,)])]
            sources[1].update(changed)
            with self.subTest(changed=changed), self.assertRaises(ServiceError):
                merge_results(sources, contract())

    def test_missing_duplicate_or_unsupported_contract(self):
        for spec in (None, contract({"n": {"op": "distinct_count", "input": "n"}}),
                     dict(contract(), disjoint_partitions=False), dict(contract(), sources=["a"])):
            with self.subTest(spec=spec), self.assertRaises(ServiceError):
                validate_contract(spec, ["a", "b"])

    def test_output_shape_grain_and_nonfinite_guard(self):
        for rows in ([(1,), (2,)], [(None,)], [(float("nan"),)], [(float("inf"),)]):
            with self.subTest(rows=rows), self.assertRaises(ServiceError):
                merge_results([result("a", ["total"], rows), result("b", ["total"], [(1,)])], contract())
        with self.assertRaises(ServiceError):
            merge_results([result("a", ["wrong"], [(1,)]), result("b", ["total"], [(1,)])], contract())

    def test_sql_algebra_rejects_avg_distinct_local_topk_fanout(self):
        for sql in ("SELECT AVG(x) AS total FROM t",
                    "SELECT COUNT(DISTINCT customer) AS total FROM t",
                    "SELECT SUM(x) AS total FROM t LIMIT 1",
                    "SELECT SUM(x) AS total FROM t HAVING total>5",
                    "SELECT SUM(x) AS total FROM t JOIN u ON t.id=u.id",
                    "SELECT SUM(x) AS total FROM (SELECT * FROM t) a"):
            with self.subTest(sql=sql), self.assertRaises(ServiceError):
                validate_merge_query(PreparedQuery(sql), contract(), "db")
        validate_merge_query(PreparedQuery("SELECT SUM(x) AS total FROM t"), contract(), "db")

    def test_weighted_algebra_requires_sufficient_statistics(self):
        spec = contract({"avg": {"op": "weighted_avg", "numerator": "s", "denominator": "n"}}, ["city"])
        validate_merge_query(PreparedQuery("SELECT city,SUM(amount) s,COUNT(amount) n FROM t GROUP BY city"), spec, "db")
        with self.assertRaises(ServiceError):
            validate_merge_query(PreparedQuery("SELECT city,AVG(amount) s,COUNT(amount) n FROM t GROUP BY city"), spec, "db")


class ExecutionTests(unittest.TestCase):
    def test_rejected_sql_never_connects(self):
        with patch.object(mysql, "get_conn") as connect, self.assertRaises(ServiceError):
            execute_source(INFO, "DELETE FROM t", QueryBudget())
        connect.assert_not_called()

    def test_readonly_timeout_binding_and_connection_cleanup(self):
        connection = MagicMock()
        control, cursor = MagicMock(), MagicMock()
        connection.cursor.side_effect = [control, cursor]
        cursor.description = [("city",)]
        cursor.__iter__.return_value = iter([("北京",)])
        query = bind_template("SELECT city FROM t WHERE city='{0}'", ["北京"])
        with patch.object(mysql, "get_conn", return_value=connection):
            output = mysql.execute_read(INFO, query, timeout=1.2, max_rows=10, max_bytes=1024)
        calls = control.__enter__.return_value.execute.call_args_list
        self.assertEqual(calls[1].args[0], "START TRANSACTION READ ONLY")
        self.assertIn("MAX_EXECUTION_TIME", calls[0].args[0])
        self.assertIn("LIMIT 11", cursor.execute.call_args.args[0])
        self.assertEqual(cursor.execute.call_args.args[1], {"p0": "北京"})
        self.assertEqual(output["rows"], [("北京",)])
        connection.commit.assert_not_called()
        connection.close.assert_called_once()

    def test_failed_readonly_setup_never_executes_query(self):
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("unsupported")
        with patch.object(mysql, "get_conn", return_value=connection), self.assertRaises(RuntimeError):
            mysql.execute_read(INFO, PreparedQuery("SELECT * FROM t"), timeout=1, max_rows=10, max_bytes=1024)
        self.assertEqual(connection.cursor.call_count, 1)
        connection.close.assert_called_once()

    def test_max_rows_and_bytes(self):
        for rows, size, raises in [([(1,), (2,), (3,)], 1024, False), ([("x" * 2000,)], 1024, True)]:
            connection, control, cursor = MagicMock(), MagicMock(), MagicMock()
            connection.cursor.side_effect = [control, cursor]
            cursor.description = [("value",)]
            cursor.__iter__.return_value = iter(rows)
            with patch.object(mysql, "get_conn", return_value=connection):
                if raises:
                    with self.assertRaises(ServiceError):
                        mysql.execute_read(INFO, PreparedQuery("SELECT x value FROM t"), timeout=1, max_rows=2, max_bytes=size)
                else:
                    output = mysql.execute_read(INFO, PreparedQuery("SELECT x value FROM t"), timeout=1, max_rows=2, max_bytes=size)
                    self.assertTrue(output["truncated"])
                    self.assertEqual(output["row_count"], 2)
            connection.close.assert_called_once()

    def test_failed_source_is_retained_and_errors_redacted(self):
        with patch.object(mysql, "execute_read", side_effect=[{"columns": ["n"], "rows": [(1,)], "row_count": 1},
                                                             RuntimeError("password secret")]):
            output = execute_many([INFO, dict(INFO, source_id="b")], "SELECT COUNT(*) n FROM t", QueryBudget())
        self.assertEqual(len(output), 2)
        self.assertEqual({r["status"] for r in output}, {"success", "failed"})
        self.assertNotIn("secret", str(output))

    def test_budget_includes_retries_and_all_sources(self):
        budget = QueryBudget(max_queries=1)
        with patch.object(mysql, "execute_read", return_value={}):
            execute_source(INFO, "SELECT 1", budget)
            with self.assertRaises(ServiceError):
                execute_source(INFO, "SELECT 1", budget)
        with self.assertRaises(ServiceError):
            QueryBudget(deadline=time.monotonic() - 1).consume()


class RoutingAndExportsTests(unittest.TestCase):
    def test_reviewed_template_configuration_is_validated_atomically(self):
        with tempfile.TemporaryDirectory() as path, patch.dict(os.environ, {"SQL_TEMPLATE_PATH": path}), \
                patch.dict(conf.template_info, {"previous": {}}, clear=True):
            root = Path(path)
            (root / "query.sql").write_text("SELECT SUM(amount) total FROM orders")
            valid = {"question": "total", "params": [], "condition_keys": [], "sql": "query.sql",
                     "source_ids": ["a", "b"], "merge": contract()}
            for change in ({"source_ids": "a"}, {"condition_keys": ["extra"]}, {"sql": "../outside.sql"},
                           {"merge": {"op": "sum"}}, {"params": "invalid"}):
                (root / "summary.json").write_text(json.dumps([dict(valid, **change)]))
                with self.assertRaises(ServiceError) as error:
                    conf.load_sql_templates()
                self.assertEqual(error.exception.status, 503)
                self.assertEqual(conf.template_info, {"previous": {}})
            (root / "summary.json").write_text(json.dumps([valid]))
            conf.load_sql_templates()
            self.assertEqual(set(conf.template_info), {"total"})
            self.assertEqual(conf.template_info["total"]["merge"], contract())

    def test_source_default_only_for_single_authorized_source(self):
        with patch.dict(conf.mysql_info, {"a": INFO, "b": dict(INFO, desc="测试二")}, clear=True):
            self.assertEqual(conf.get_mysql_conf_by_question("销售额", {"a"})[0]["source_id"], "a")
            with self.assertRaises(ServiceError):
                conf.get_mysql_conf_by_question("销售额", {"a", "b"})
            with self.assertRaises(ServiceError):
                conf.get_mysql_conf_by_question("测试二", {"a"}, ["b"])
            self.assertEqual(len(conf.get_mysql_conf_by_question("测试一和测试二", {"a", "b"})), 2)

    def test_env_file_cannot_override_runtime_secret(self):
        with tempfile.TemporaryDirectory() as path, patch.dict(os.environ, {"FIXTURE_KEY": "runtime"}):
            file = Path(path) / "fixture.env"
            file.write_text("FIXTURE_KEY=file\n")
            conf.load_env(file)
            self.assertEqual(os.environ["FIXTURE_KEY"], "runtime")

    def test_export_owner_revocation_expiry_and_csv_formula(self):
        with tempfile.TemporaryDirectory() as path, patch.dict(os.environ, {"QUERY_EXPORT_PATH": path}):
            url = save_export(USER, ["a"], ["value"], [("=1+1",)])
            identity = url.rsplit("/", 1)[1]
            self.assertIn("'=1+1", read_export(USER, identity).decode("utf-8-sig"))
            for subject in (Principal("other", USER.sources), Principal(USER.subject, frozenset())):
                with self.assertRaises(ServiceError):
                    read_export(subject, identity)
            with patch("server.exports.time.time", return_value=time.time() + 601), self.assertRaises(ServiceError):
                read_export(USER, identity)


class RuntimeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.infos = patch.dict(conf.mysql_info, {"a": INFO}, clear=True)
        self.infos.start()
        self.addCleanup(self.infos.stop)
        self.templates = patch.dict(conf.template_info, {}, clear=True)
        self.templates.start()
        self.addCleanup(self.templates.stop)
        self.generated = {"bedrockSQL": "SELECT SUM(amount) AS total FROM t",
                          "bedrockColumn": ["total"], "column_type": ["度量"], "chart_type": "BarChart"}

    def test_single_source_response_and_no_export_for_small_result(self):
        with patch.object(api, "answer_template_sql", return_value=self.generated), patch.object(api.aws, "get"), \
                patch.object(api.Helper, "query_db", return_value=result("a", ["total"], [(10,)])):
            response = api.get_result([{"role": "user", "content": "测试一"}], "trace", principal=USER)
        self.assertEqual(response["status"], "success")
        self.assertIn("10", response["mdData"])
        self.assertEqual(response["extra"], "")
        self.assertEqual(response["chartType"], "BarChartPic")

    def test_malformed_model_labels_cannot_break_result_rendering(self):
        generated = dict(self.generated, cn_column=[None], clarify={"bad": True}, chart_type=["bad"])
        with patch.object(api, "answer_template_sql", return_value=generated), patch.object(api.aws, "get"), \
                patch.object(api.Helper, "query_db", return_value=result("a", ["total"], [(10,)])):
            response = api.get_result([{"role": "user", "content": "测试一"}], "trace", principal=USER)
        self.assertEqual(response["content"], "")
        self.assertIn("total", response["mdData"])
        self.assertEqual(response["chartType"], "BarChartPic")
        with self.assertRaises(ServiceError):
            api._columns({"names": ["total"], "types": [{}]}, "names", "types")

    def test_repair_reenters_gate_and_remains_single_source(self):
        with patch.object(api.llm, "query", return_value='{"finalSQL":"DELETE FROM t"}'), \
                patch.object(api.llm, "format_bedrock_result", side_effect=lambda x: x), \
                patch.object(mysql, "get_conn") as connect, self.assertRaises(ServiceError):
            api.retry_when_sql_error("u", "trace", [{"role": "user", "content": "q"}], "SELECT x",
                                     {"error": "hidden"}, [INFO], None)
        connect.assert_not_called()

    def test_malformed_repair_and_missing_finalsql_are_explicit_failures(self):
        for raw in ('not json', '{}', '{"finalSQL":""}'):
            with patch.object(api.llm, "query", return_value=raw), \
                    patch.object(api.llm, "format_bedrock_result", side_effect=lambda x: x), self.assertRaises(ServiceError):
                api.retry_when_sql_error("u", "t", [{"role": "user", "content": "q"}], "SELECT x", {}, [INFO], None)

    def test_parameter_order_is_trusted_configuration_not_model_order(self):
        metadata = {"q": {"conditions": {"城市=": "", "年份=": ""}, "querys": ["总额"]}}
        entry = {"content": "SELECT city, SUM(amount) total FROM t WHERE city='{0}' AND year={1} GROUP BY city",
                 "condition_keys": ["城市=", "年份="]}
        responses = ['{"conditions":{"年份=":2026,"城市=":"上海"},"querys":["总额"]}',
                     '{"columns":["city","total"],"columns_type":["维度","度量"]}']
        with patch.dict(api.meta, metadata, clear=True), patch.dict(conf.template_info, {"q": entry}, clear=True), \
                patch.object(api.llm, "query", side_effect=responses):
            output = api.answer_template_sql(None, [{"role": "user", "content": "q"}], "trace")
        self.assertEqual(output["prepared_query"].parameters, {"p0": "上海", "p1": 2026})

    def test_multi_source_failure_never_retries_first_database(self):
        generated = dict(self.generated, merge_contract=contract())
        with patch.dict(conf.mysql_info, {"b": dict(INFO, source_id="b", desc="测试二")}), \
                patch.object(api, "answer_template_sql", return_value=generated), patch.object(api.aws, "get"), \
                patch.object(api.Helper, "query_many_db", return_value=[
                    result("a", ["total"], [(10,)]), {"source_id": "b", "status": "failed"}]), \
                patch.object(api, "retry_when_sql_error") as retry, self.assertRaises(ServiceError):
            api.get_result([{"role": "user", "content": "测试一 测试二"}], "trace", principal=USER)
        retry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
