"""旁路有界性、隐私、异常隔离与配置测试。"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ontology import shadow
from ontology.service import OntologyService
from ontology.shadow import ShadowConfig, ShadowRecorder
from ontology.store import FileOntologyStore
from tests.fixtures import snapshot


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("后台任务超时")


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name)
        self.store = FileOntologyStore(path / "store")
        self.data = snapshot(path / "orders.xlsx")
        self.store.replace_sources(self.data, {("default", "orders.xlsx"), ("default", "sql_templates")})
        self.service = OntologyService(self.store, refresh_seconds=0)

    def recorder(self, service=None, sink=None, **kwargs):
        config = ShadowConfig(enabled=True, sample_rate=1.0, **kwargs)
        recorder = ShadowRecorder(config, service or self.service, sink)
        self.addCleanup(recorder.close)
        return recorder

    def test_json_comparison_and_privacy(self):
        records = []
        recorder = self.recorder(sink=records.append)
        question = "订单销售额，客户姓名是SECRET_USER"
        recorder.submit("scenario", question, "trace-123", selected="SECRET_LLM_TEXT",
                        final_selection="订单", status="default_fallback", llm_elapsed_ms=1.2)
        wait_for(lambda: len(records) == 1)
        record = records[0]
        payload = json.dumps(record, ensure_ascii=False)
        self.assertNotIn(question, payload)
        self.assertNotIn("SECRET_USER", payload)
        self.assertNotIn("SECRET_LLM_TEXT", payload)
        self.assertEqual(record["trace_id"], "trace-123")
        self.assertEqual(record["revision"], self.store.read().revision)
        self.assertIn("topk_agreement", record)
        self.assertTrue(record["relation_ids"])
        self.assertEqual(record["status"], "default_fallback")

    def test_agreement_and_no_match(self):
        records = []
        recorder = self.recorder(sink=records.append)
        recorder.submit("template", "北京订单数量", "t1", selected="上海订单数量",
                        final_selection="上海订单数量")
        recorder.submit("template", "天气", "t2", status="no_match")
        wait_for(lambda: len(records) == 2)
        self.assertTrue(records[0]["top1_agreement"])
        self.assertFalse(records[1]["topk_agreement"])
        self.assertIsNone(records[1]["legacy_selection_id"])

    def test_saturated_queue_is_nonblocking_and_close_bounded(self):
        entered, release = threading.Event(), threading.Event()
        service = Mock()
        def blocked(question):
            entered.set()
            release.wait(3)
            return []
        service.scenario_candidates.side_effect = blocked
        service.revision, service.error_type = "", ""
        recorder = self.recorder(service=service, sink=lambda record: None, queue_size=1)
        recorder.submit("scenario", "订单", "t")
        self.assertTrue(entered.wait(2))
        self.assertTrue(recorder.submit("scenario", "订单", "t"))
        start = time.monotonic()
        for _ in range(100):
            self.assertFalse(recorder.submit("scenario", "订单", "t"))
        self.assertLess(time.monotonic() - start, 0.25)
        self.assertEqual(recorder.dropped, 100)
        start = time.monotonic()
        recorder.close(timeout=0.02)
        self.assertLess(time.monotonic() - start, 0.25)
        self.assertTrue(recorder.queue.empty())
        release.set()

    def test_background_service_and_log_exceptions_are_isolated(self):
        failing = Mock()
        failing.scenario_candidates.side_effect = RuntimeError("SECRET_ERROR")
        records = []
        recorder = self.recorder(service=failing, sink=records.append)
        self.assertTrue(recorder.submit("scenario", "订单", "t"))
        wait_for(lambda: recorder.errors == 1)
        self.assertEqual(records[0]["error_type"], "RuntimeError")
        self.assertNotIn("SECRET_ERROR", json.dumps(records))
        bad_sink = self.recorder(sink=Mock(side_effect=OSError("日志失败")))
        self.assertTrue(bad_sink.submit("scenario", "订单", "t"))
        wait_for(lambda: bad_sink.errors == 1)
        self.assertTrue(bad_sink.submit("scenario", "订单", "t"))

    def test_disabled_and_zero_sampling_do_not_touch_store(self):
        service = Mock()
        for config in [ShadowConfig(), ShadowConfig(enabled=True, sample_rate=0)]:
            recorder = ShadowRecorder(config, service)
            self.addCleanup(recorder.close)
            self.assertFalse(recorder.submit("scenario", "订单", "t"))
            service.scenario_candidates.assert_not_called()

    def test_invalid_configuration_and_disabled_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(ShadowConfig.from_env().enabled)
        for key, value in [("ONTOLOGY_SHADOW_ENABLED", "yes"), ("ONTOLOGY_SHADOW_SAMPLE_RATE", "nan"),
                           ("ONTOLOGY_SHADOW_SAMPLE_RATE", "2"), ("ONTOLOGY_TOP_K", "0"),
                           ("ONTOLOGY_SHADOW_QUEUE_SIZE", "0"), ("ONTOLOGY_REFRESH_SECONDS", "inf"),
                           ("ONTOLOGY_NAMESPACE", "")]:
            with self.subTest(key=key), patch.dict(os.environ, {key: value}, clear=True):
                with self.assertRaises(ValueError):
                    ShadowConfig.from_env()
                with patch.object(shadow, "_config", None), patch.object(shadow, "_recorder", None):
                    shadow.configure()
                    self.assertFalse(shadow.record_comparison("scenario", "订单", "t"))

    def test_fork_reset_does_not_reuse_parent_queue_or_lock(self):
        with patch.object(shadow, "_recorder", Mock()), patch.object(shadow, "_lock", threading.Lock()):
            old_lock = shadow._lock
            shadow._after_fork()
            self.assertIsNone(shadow._recorder)
            self.assertIsNot(old_lock, shadow._lock)

    def test_inherited_recorder_does_not_acquire_parent_locks(self):
        recorder = ShadowRecorder(ShadowConfig(), Mock())
        self.addCleanup(recorder.close)
        with patch("ontology.shadow.os.getpid", return_value=recorder._pid + 1), \
                patch.object(recorder._stop, "set") as stop:
            self.assertFalse(recorder.submit("scenario", "订单", "trace"))
            recorder.close()
            stop.assert_not_called()

    def test_adjacent_slots_match_is_bounded(self):
        from ontology.extractor import extract_templates
        definition = dict(params=["a", "b", "c", "d"], content="SELECT {0},{1},{2},{3}",
                          question_pattern="{0}{1}{2}{3}end")
        template = extract_templates({"模板": definition}).templates[0]
        start = time.monotonic()
        self.assertFalse(self.service._slot_match("x" * 400 + "no", template))
        self.assertLess(time.monotonic() - start, 1)


if __name__ == "__main__":
    unittest.main()
