"""既有查询与生成命令回归：所有外部依赖均替身，无真实凭据。"""
import copy
import importlib.util
import io
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from ontology import shadow
from ontology.service import OntologyService
from ontology.shadow import ShadowConfig, ShadowRecorder
from ontology.store import FileOntologyStore
from tests.fixtures import snapshot, workbook
from tests.test_shadow import wait_for

ROOT = Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def sandbox():
    server = types.ModuleType("server")
    server.__path__ = [str(ROOT / "server")]
    db = types.ModuleType("server.db")
    db.__path__ = [str(ROOT / "server" / "db")]
    handlers = types.ModuleType("logging.handlers")
    handlers.RotatingFileHandler = lambda *args, **kwargs: logging.NullHandler()
    modules = {"server": server, "server.db": db, "boto3": MagicMock(), "logging.handlers": handlers}
    for name in ("llm", "conf", "aws", "prompt", "sql"):
        child = types.ModuleType("server." + name)
        setattr(server, name, child)
        modules["server." + name] = child
    db.mysql = MagicMock()
    modules["server.db.mysql"] = db.mysql
    server.db = db
    server.llm.query = MagicMock()
    server.llm.format_bedrock_result = lambda value: value
    server.conf.load_env = MagicMock()
    server.conf.get_sql_templates = MagicMock(return_value={})
    server.conf.get_mysql_conf_by_question = MagicMock(return_value=[dict(host="fake", port=3306,
                                                    user="readonly", pwd="fixture", db="fake")])
    server.aws.get = MagicMock(return_value="fake-bedrock")
    server.aws.upload_file_to_s3 = MagicMock(return_value=True)
    server.prompt.build_template_question_meta_prompt = lambda question: "模板匹配:" + question
    server.prompt.template_sql = lambda question: "SELECT COUNT(*) AS count FROM orders WHERE city='{0}'"
    server.prompt.template_sql_columns = lambda sql, question: "描述列:" + sql
    names = ["server.api", "server.api_helpler"]
    old_handlers = {name: logging.getLogger(name).handlers[:] for name in names}
    try:
        with patch.dict(sys.modules, modules), patch("logging.StreamHandler", lambda *a, **k: logging.NullHandler()):
            helper = load("server.api_helpler", ROOT / "server" / "api_helpler.py")
            sys.modules["server.api_helpler"] = helper
            server.api_helpler = helper
            yield server, load("server.api", ROOT / "server" / "api.py")
    finally:
        for name in names:
            logging.getLogger(name).handlers = old_handlers[name]


class QueryRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name)
        self.store = FileOntologyStore(path / "store")
        self.data = snapshot(path / "orders.xlsx")
        self.store.replace_sources(self.data, {("default", "orders.xlsx"), ("default", "sql_templates")})
        self.messages = [{"role": "user", "content": "订单销售额"}]
        self.config = dict(Overall=dict(AllScenariosPrompt="订单", ScenarioSelectionPrompt="选择场景"),
                           订单=dict(RolePrompt="role", TablePrompt="table", IndicatorsListPrompt="columns", OtherPrompt="other"),
                           Examples={}, ChartPrompt="chart", HardPrompt="hard")
        self.generated = json.dumps(dict(finalSQL="SELECT SUM(amount) AS amount FROM orders", columnList=["SUM(amount) AS amount"],
                             columnCNList=["销售额"], columnType=["度量"], chartType="BarChartPic", clarify="查询完毕"))

    def compare_modes(self, server, execute, responses):
        outputs = []
        calls = []
        records = []
        for mode in ("disabled", "enabled", "failed"):
            server.llm.query.reset_mock()
            server.llm.query.side_effect = list(responses)
            recorder = ShadowRecorder(ShadowConfig(enabled=True, sample_rate=1),
                                      OntologyService(self.store, refresh_seconds=0), records.append)
            try:
                with patch.object(shadow, "_config", ShadowConfig(enabled=mode == "enabled", sample_rate=1)), \
                        patch.object(shadow, "_recorder", recorder):
                    if mode == "failed":
                        with patch.object(shadow, "record_comparison", side_effect=OSError("本体服务失败")):
                            outputs.append(execute())
                    else:
                        outputs.append(execute())
                    calls.append([(copy.deepcopy(call.args), copy.deepcopy(call.kwargs))
                                  for call in server.llm.query.call_args_list])
                    if mode == "enabled":
                        wait_for(lambda: recorder.queue.unfinished_tasks == 0)
            finally:
                recorder.close()
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0], calls[2])
        return outputs[0], records

    def test_scenario_success_keeps_prompts_calls_and_response(self):
        with sandbox() as (server, api):
            result, records = self.compare_modes(server, lambda: api.answer("fake", copy.deepcopy(self.messages), self.config, "trace", True),
                                                  ["订单", self.generated])
            self.assertEqual(result["queryTableName"], "订单")
            self.assertEqual(result["bedrockColumn"], ["amount"])
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "matched")

    def test_default_fallback_and_no_match(self):
        with sandbox() as (server, api):
            config = copy.deepcopy(self.config)
            config["Overall"]["DefaulteScenario"] = "订单"
            result, records = self.compare_modes(server, lambda: api.answer("fake", self.messages, config, "trace", False),
                                                  ["未找到", self.generated])
            self.assertEqual(result["queryTableName"], "订单")
            self.assertEqual(records[0]["status"], "default_fallback")
            result, records = self.compare_modes(server, lambda: api.answer("fake", self.messages, self.config, "trace", False), ["未找到"])
            self.assertIn("error", result)
            self.assertEqual(records[0]["status"], "no_match")

    def test_template_success_no_match_and_invalid_json(self):
        with sandbox() as (server, api):
            api.meta = {"上海订单数量": dict(conditions={"城市=": "上海"}, querys=["订单数量"])}
            selection = json.dumps(dict(conditions={"城市=": "北京"}, querys=["订单数量"]))
            columns = json.dumps(dict(columns=["count"], columns_type=["度量"]))
            result, records = self.compare_modes(server, lambda: api.answer_template_sql("fake", self.messages, "trace"), [selection, columns])
            self.assertIn(":p0", result["bedrockSQL"])
            self.assertEqual(result["prepared_query"].parameters, {"p0": "北京"})
            self.assertEqual(records[0]["status"], "matched")
            no_match = json.dumps(dict(conditions={}, querys=["天气"]))
            result, records = self.compare_modes(server, lambda: api.answer_template_sql("fake", self.messages, "trace"), [no_match])
            self.assertIn("error", result)
            self.assertEqual(records[0]["status"], "no_match")
            result, records = self.compare_modes(server, lambda: api.answer_template_sql("fake", self.messages, "trace"), ["not json"])
            self.assertIn("error", result)
            self.assertEqual(records[0]["status"], "invalid_legacy_json")

    def test_init_loads_templates_before_configuring_shadow(self):
        with sandbox() as (_, api), patch.object(api, "_load_template_questions") as templates, \
                patch.object(shadow, "configure") as configure:
            api.init()
            templates.assert_called_once()
            configure.assert_called_once()


class CommandRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def command_module(self):
        module = load("_test_prompt_gen", ROOT / "prompt_gen.py")
        module.data_files = str(self.path)
        module.prompt_path, module.example_path, module.rag_path = "prompt.json", "example.json", "rag.json"
        module.prompt_file, module.example_file, module.rag_file = "demo/prompt.json", "demo/example.json", "demo/rag.json"
        module.bucket_name = "fixture-bucket"
        return module

    def test_import_does_not_read_env_or_run_cli(self):
        with sandbox() as (server, _):
            self.command_module()
            server.conf.load_env.assert_not_called()
            server.llm.query.assert_not_called()

    def test_template_batches_and_both_tables_in_pending_ontology(self):
        from prompt import gen
        with sandbox() as (server, _), redirect_stdout(io.StringIO()), \
                patch.dict(os.environ, {"ONTOLOGY_STORE_PATH": str(self.path / "store"), "ONTOLOGY_NAMESPACE": "fixture"}):
            module = self.command_module()
            schema = [dict(Name=f"column_{i}", Type="decimal", Key="", Comment="") for i in range(43)]
            responses = []
            for table in ("one", "two"):
                for begin, end in ((0, 40), (40, 43)):
                    responses.append(json.dumps(dict(table=table, desc=table + "说明", columns=[dict(
                        name=f"column_{i}", tips="度量", option="", desc="金额", option_desc="") for i in range(begin, end)])))
            server.llm.query.side_effect = responses
            with patch.object(gen, "get_table_schema", return_value=schema), patch.object(gen, "get_sample_data", return_value=[]):
                module.template_command(types.SimpleNamespace(scenario="业务", tables=["one", "two"]))
            self.assertTrue((self.path / "业务.xlsx").exists())
            self.assertEqual(json.loads((self.path / "业务.json").read_text())["table"], "two")
            output = FileOntologyStore(self.path / "store").read("pending")
            self.assertEqual(len(output.tables), 2)
            self.assertEqual(len(output.columns), 86)
            self.assertEqual(server.llm.query.call_count, 4)
            self.assertTrue(all(e.review_status == "pending" for e in output.entities()))
            server.db.mysql.get_conn.return_value.close.assert_called_once()

    def test_prompt_confirmation_publication_order_and_approval(self):
        workbook(self.path / "orders.xlsx")
        with sandbox() as (server, _), redirect_stdout(io.StringIO()), patch.dict(os.environ,
                {"ONTOLOGY_STORE_PATH": str(self.path / "store"), "ONTOLOGY_NAMESPACE": "default", "SQL_TEMPLATE_PATH": ""}):
            module = self.command_module()
            events = []
            server.aws.upload_file_to_s3.side_effect = lambda *args: events.append(args[0]) or True
            with patch.object(module, "confirm_action", return_value=True), patch.object(module.tool, "run", side_effect=lambda *a: events.append("prompt-run")):
                module.prompt_command()
            self.assertEqual(events, ["prompt-run", "prompt.json", "example.json", "rag.json"])
            output = FileOntologyStore(self.path / "store").read()
            self.assertEqual(output.scenarios[0].review_status, "approved")
            with patch.object(module, "confirm_action", return_value=False), patch.object(module.tool, "run") as run:
                module.prompt_command()
                run.assert_not_called()

    def test_partial_upload_failure_and_ontology_failure_keep_original_calls(self):
        with sandbox() as (server, _), redirect_stdout(io.StringIO()):
            module = self.command_module()
            server.aws.upload_file_to_s3.side_effect = [True, False, True]
            with patch.object(module, "confirm_action", return_value=True), patch.object(module.tool, "run"), patch.object(module, "_ontology_write") as write:
                module.prompt_command()
                self.assertEqual(server.aws.upload_file_to_s3.call_count, 3)
                write.assert_not_called()
            server.aws.upload_file_to_s3.reset_mock()
            server.aws.upload_file_to_s3.side_effect = None
            with patch.object(module, "confirm_action", return_value=True), patch.object(module.tool, "run"), \
                    patch("ontology.extractor.safe_publish_workbooks", side_effect=RuntimeError("本体失败")):
                self.assertIsNone(module.prompt_command())
                self.assertEqual(server.aws.upload_file_to_s3.call_count, 3)

    def test_uploader_result_does_not_change_upload_arguments(self):
        with sandbox(), redirect_stdout(io.StringIO()):
            aws = load("server._test_aws", ROOT / "server" / "aws.py")
            client = MagicMock()
            with patch.object(aws, "get", return_value=client):
                self.assertTrue(aws.upload_file_to_s3("file", "bucket", "key"))
                client.upload_file.assert_called_once_with("file", "bucket", "key")
                client.upload_file.side_effect = FileNotFoundError()
                self.assertFalse(aws.upload_file_to_s3("file", "bucket", "key"))


if __name__ == "__main__":
    unittest.main()
