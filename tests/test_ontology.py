"""元模型、仓库、抽取和只读服务验收。"""
import copy
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ontology.__main__ import main
from ontology.extractor import (combine, extract_analysis, extract_template_directory,
                                extract_templates, extract_workbook, publish_workbooks,
                                safe_write_analysis)
from ontology.schema import Column, Scenario, Snapshot, ValidationError, stable_id
from ontology.service import OntologyService
from ontology.store import FileOntologyStore
from tests.fixtures import snapshot, workbook

ROOT = Path(__file__).resolve().parent.parent


class OntologyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.data = snapshot(self.path / "orders.xlsx")
        self.store = FileOntologyStore(self.path / "store")
        self.sources = {("default", "orders.xlsx"), ("default", "sql_templates")}

    def publish(self, data=None):
        return self.store.replace_sources(data or self.data, self.sources)

    def test_six_entities_roundtrip_and_revision(self):
        output = self.data.seal()
        self.assertTrue(all(getattr(output, name) for name in
                            ("scenarios", "tables", "columns", "metrics", "relations", "templates")))
        self.assertEqual(output.to_dict(), Snapshot.from_json(output.to_json()).to_dict())
        output.verify_revision()
        output.scenarios[0].description += "修改"
        with self.assertRaises(ValidationError):
            output.verify_revision()

    def test_strict_types_unknown_fields_and_required_fields(self):
        source = self.data.scenarios[0].to_dict()
        for key, value in [("version", True), ("version", 0), ("review_status", "yes"),
                           ("synonyms", "交易"), ("name", ""), ("unknown", "x")]:
            with self.subTest(key=key), self.assertRaises(ValidationError):
                Scenario.from_dict(dict(source, **{key: value}))
        source.pop("source")
        with self.assertRaises(ValidationError):
            Scenario.from_dict(source)
        with self.assertRaises(ValidationError):
            Snapshot.from_json('{"schema_version":1,"schema_version":1}')
        column = self.data.columns[0].to_dict()
        column.update(is_measure=True, is_dimension=True)
        with self.assertRaises(ValidationError):
            Column.from_dict(column)

    def test_reference_integrity_and_namespace(self):
        for mutation in (lambda d: setattr(d.columns[0], "table_id", "missing"),
                         lambda d: setattr(d.tables[0].source, "namespace", "other"),
                         lambda d: setattr(d.relations[0], "left_key", d.columns[-1].id),
                         lambda d: d.scenarios.append(copy.deepcopy(d.scenarios[0]))):
            data = copy.deepcopy(self.data)
            mutation(data)
            with self.assertRaises(ValidationError):
                data.validate()

    def test_atomic_roundtrip_idempotency_and_versions(self):
        first = self.publish()
        second = self.publish()
        self.assertEqual(first.revision, second.revision)
        self.data.scenarios[0].description += "新增说明"
        third = self.publish()
        self.assertNotEqual(first.revision, third.revision)
        self.assertEqual(third.scenarios[0].version, 2)
        self.assertEqual(third.tables[0].version, 1)
        self.assertEqual(third.revision, self.store.read().revision)

    def test_source_deletion_and_recreation_keep_high_water(self):
        self.publish()
        empty = self.store.replace_sources(Snapshot(), {("default", "orders.xlsx")})
        self.assertFalse(empty.scenarios)
        self.assertTrue(empty.templates)
        self.assertEqual(self.publish().scenarios[0].version, 2)

    def test_namespace_and_source_collision(self):
        self.publish()
        other = snapshot(self.path / "orders.xlsx", "eu")
        result = self.store.replace_sources(other, {("eu", "orders.xlsx"), ("eu", "sql_templates")})
        self.assertEqual(len(result.scenarios), 2)
        self.assertNotEqual(result.scenarios[0].id, result.scenarios[1].id)
        collision = copy.deepcopy(self.data)
        for entity in collision.entities():
            entity.source.asset = "conflict"
        with self.assertRaises(ValidationError):
            self.store.replace_sources(collision, {("default", "conflict")})

    def test_concurrent_writers_no_lost_update(self):
        def write(index):
            data = copy.deepcopy(self.data)
            for entity in data.entities():
                entity.source.namespace = f"ns{index}"
            mapping = {e.id: stable_id("entity", f"ns{index}", e.id) for e in data.entities()}
            for entity in data.entities():
                entity.id = mapping[entity.id]
                for attr in ("scenario_id", "table_id", "left_table", "left_key", "right_table", "right_key"):
                    if hasattr(entity, attr):
                        setattr(entity, attr, mapping[getattr(entity, attr)])
                if hasattr(entity, "column_ids"):
                    entity.column_ids = [mapping[x] for x in entity.column_ids]
            return self.store.replace_sources(data, {(f"ns{index}", "orders.xlsx"),
                                                     (f"ns{index}", "sql_templates")})
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, range(6)))
        self.assertEqual(len(self.store.read().scenarios), 6)

    def test_failed_replace_leaves_old_snapshot(self):
        original = self.publish()
        self.data.scenarios[0].description = "更改"
        with patch("ontology.store.os.replace", side_effect=OSError("模拟磁盘错误")):
            with self.assertRaises(OSError):
                self.publish()
        self.assertEqual(self.store.read().revision, original.revision)
        self.assertFalse(list(self.store.path.rglob(".snapshot-*")))

    def test_rollback_versions_and_history_bounds(self):
        first = self.publish()
        self.data.scenarios[0].description = "第二版"
        self.publish()
        restored = self.store.rollback(first.revision)
        self.assertEqual(restored.scenarios[0].description, first.scenarios[0].description)
        self.assertEqual(restored.scenarios[0].version, 3)
        with self.assertRaises(ValidationError):
            self.store.rollback("../bad")
        limited = FileOntologyStore(self.path / "limited", history_limit=2)
        for index in range(4):
            self.data.scenarios[0].description = str(index)
            limited.replace_sources(self.data, self.sources)
        self.assertEqual(len(list((limited.path / "history" / "approved").glob("*.json"))), 2)

    def test_corruption_is_rejected_and_cache_is_not_poisoned(self):
        original = self.publish()
        service = OntologyService(self.store, refresh_seconds=0)
        self.assertTrue(service.find_scenario_candidates("订单销售额"))
        with (self.store.path / "approved.json").open("w") as stream:
            stream.write("{broken")
        with self.assertRaises(ValidationError):
            self.store.read()
        self.assertTrue(service.find_scenario_candidates("订单销售额"))
        self.assertEqual(service.revision, original.revision)
        self.assertEqual(service.error_type, "ValidationError")

    def test_corrupted_current_can_be_restored_from_history(self):
        first = self.publish()
        self.data.scenarios[0].description = "第二版"
        self.publish()
        with (self.store.path / "approved.json").open("w") as stream:
            stream.write("损坏")
        restored = self.store.rollback(first.revision)
        self.assertEqual(restored.scenarios[0].version, 3)
        self.assertEqual(restored.scenarios[0].description, first.scenarios[0].description)
        self.store.read().verify_revision()

    def test_lock_timeout_and_snapshot_limit(self):
        other = FileOntologyStore(self.store.path, lock_timeout=0.02)
        with self.store._lock(), self.assertRaises(TimeoutError):
            other.replace_sources(self.data, self.sources)
        self.publish()
        with patch("ontology.store.MAX_SNAPSHOT_BYTES", 10), self.assertRaises(ValidationError):
            self.store.read()

    def test_concurrent_readers_always_see_complete_snapshots(self):
        self.publish()
        def write():
            for index in range(5):
                data = copy.deepcopy(self.data)
                data.scenarios[0].description = str(index)
                self.publish(data)
        def read():
            for _ in range(20):
                self.store.read().verify_revision()
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(write), pool.submit(read), pool.submit(read)]
            for future in futures:
                future.result()

    def test_relations_do_not_reveal_unapproved_endpoints(self):
        self.publish()
        service = OntologyService(self.store, refresh_seconds=0)
        self.assertTrue(service.get_relations("订单"))
        key = self.data.relations[0].left_key
        next(c for c in self.data.columns if c.id == key).review_status = "rejected"
        self.publish()
        self.assertFalse(service.get_relations("订单"))

    def test_structured_override_preserves_original_join_rule(self):
        from openpyxl import load_workbook
        path = workbook(self.path / "override.xlsx", join_rule="人工解释关联关系")
        book = load_workbook(path)
        rule = json.dumps([dict(left_table="orders", left_key="customer_id", right_table="customers",
                               right_key="id", join_type="inner")])
        book.active.append(["结构化关联", rule])
        book.save(path)
        book.close()
        data = extract_workbook(path, review_status="approved")
        self.assertEqual(data.scenarios[0].join_rule, "人工解释关联关系")
        self.assertEqual(data.relations[0].join_type, "inner")

    def test_workbook_headers_disabled_columns_empty_cells_and_metrics(self):
        self.assertEqual(len(self.data.columns), 5)
        self.assertNotIn("字段名称", [c.name for c in self.data.columns])
        self.assertNotIn("private_note", [c.name for c in self.data.columns])
        self.assertNotIn("ignored", [c.name for c in self.data.columns])
        status = next(c for c in self.data.columns if c.name == "status")
        self.assertTrue(status.enumerable)
        self.assertEqual(status.option_desc, "paid=已支付")
        self.assertEqual(self.data.metrics[0].review_status, "pending")
        self.assertEqual(self.data.metrics[0].aggregation, "")
        self.assertEqual(len(self.data.relations), 1)
        self.assertEqual(self.data.relations[0].join_type, "left")

    def test_free_text_relations_are_conservative(self):
        for rule, count in [("根据业务适当关联两个表", 0),
                            ("orders.customer_id=customers.id", 1),
                            ("orders.missing=customers.id", 0)]:
            workbook(self.path / "joins.xlsx", join_rule=rule)
            output = extract_workbook(self.path / "joins.xlsx", review_status="approved")
            self.assertEqual(len(output.relations), count)
            self.assertTrue(output.warnings)
            if count:
                self.assertEqual(output.relations[0].review_status, "pending")

    def test_repo_excel_and_sql_assets(self):
        output = extract_workbook(ROOT / "assets" / "orders_default.xlsx")
        self.assertTrue(output.scenarios)
        self.assertTrue(output.tables)
        self.assertTrue(output.columns)
        output.validate()
        templates = extract_template_directory(ROOT / "server" / "prompt_sqls")
        self.assertEqual(templates.templates[0].params, ["上海市"])
        self.assertEqual(templates.templates[0].slots[0].name, "param_0")

    def test_analysis_preserves_batches_and_schema_without_samples(self):
        analysis = dict(table="t1", desc="表说明", columns=[dict(name="cost", tips="度量", option="", desc="成本", option_desc="")])
        schema = [dict(Name="cost", Type="decimal", Key="", Comment="")]
        data = extract_analysis("场景", [(analysis, schema), (dict(analysis, table="t2"), schema)])
        self.assertEqual(len(data.tables), 2)
        self.assertEqual(len(data.columns), 2)
        self.assertTrue(all(e.review_status == "pending" for e in data.entities()))
        self.assertNotIn("sample", data.to_json())

    def test_template_slots_types_and_invalid_placeholders(self):
        data = extract_templates({"超过10的订单": dict(params=[10], content="SELECT * FROM t WHERE n>{0}")})
        self.assertEqual(data.templates[0].slots[0].dtype, "integer")
        self.assertEqual(data.templates[0].question_pattern, "超过{slot}的订单")
        for content in ("SELECT {1}", "SELECT {0.name}", "SELECT {}", "SELECT {0!r}"):
            with self.subTest(content=content), self.assertRaises(ValidationError):
                extract_templates({"模板": dict(params=["x"], content=content)})

    def test_recall_synonyms_slots_irrelevant_and_relation(self):
        self.publish()
        service = OntologyService(self.store, namespace="default", refresh_seconds=0)
        self.assertEqual(service.find_scenario_candidates("GMV")[0].name, "订单")
        self.assertEqual(service.find_scenario_candidates("交易")[0].name, "订单")
        self.assertFalse(service.find_scenario_candidates("天气预报"))
        self.assertFalse(service.find_scenario_candidates(""))
        self.assertEqual(service.template_candidates("北京订单数量")[0].reason, "slot_pattern")
        self.assertEqual(service.template_candidates("上海订单总数")[0].reason, "synonym")
        self.assertFalse(service.find_template_candidates("天气预报"))
        self.assertEqual(len(service.get_relations("订单")), 1)
        candidate = service.find_scenario_candidates("交易")[0]
        candidate.name = "外部修改"
        self.assertEqual(service.find_scenario_candidates("交易")[0].name, "订单")

    def test_pending_not_recalled_and_refresh_finds_new_data(self):
        pending = copy.deepcopy(self.data)
        for entity in pending.entities():
            entity.review_status = "pending"
        self.store.replace_sources(pending, self.sources, "pending")
        service = OntologyService(self.store, refresh_seconds=0)
        self.assertFalse(service.find_scenario_candidates("订单"))
        self.publish()
        self.assertTrue(service.find_scenario_candidates("订单"))
        self.data.scenarios[0].review_status = "rejected"
        self.publish()
        self.assertFalse(service.find_scenario_candidates("订单"))

    def test_single_character_scenario_alias_is_recalled(self):
        path = workbook(self.path / "cars.xlsx", scenario="车")
        data = extract_workbook(path, review_status="approved")
        self.store.replace_sources(data, {("default", "cars.xlsx")})
        service = OntologyService(self.store)
        self.assertEqual(service.find_scenario_candidates("车")[0].name, "车")

    def test_top_k_stable_order_and_namespace_filter(self):
        self.publish()
        other = snapshot(self.path / "orders.xlsx", "other")
        self.store.replace_sources(other, {("other", "orders.xlsx"), ("other", "sql_templates")})
        service = OntologyService(self.store, top_k=1, refresh_seconds=0)
        first = service.find_scenario_candidates("交易")
        self.assertEqual(len(first), 1)
        self.assertEqual(first, service.find_scenario_candidates("交易"))
        service = OntologyService(self.store, namespace="other")
        self.assertEqual(service.find_scenario_candidates("交易")[0].source.namespace, "other")

    def test_directory_publish_clears_deleted_workbooks(self):
        folder = self.path / "input"
        folder.mkdir()
        workbook(folder / "one.xlsx", "订单")
        workbook(folder / "two.xlsx", "售后")
        publish_workbooks(folder, store=self.store)
        # 通过 fixture 的文件重命名模拟来源移除，不修改仓库业务文件。
        (folder / "two.xlsx").rename(folder / "two.backup")
        output = publish_workbooks(folder, store=self.store)
        self.assertEqual([s.name for s in output.scenarios], ["订单"])

    def test_safe_write_failure_and_cli_exit_codes(self):
        with patch("ontology.extractor.extract_analysis", side_effect=RuntimeError("模拟故障")):
            self.assertIsNone(safe_write_analysis("场景", []))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--store", str(self.store.path), "validate"]), 1)
            self.assertEqual(main(["--store", str(self.store.path), "extract", "--workbook",
                                   str(self.path / "orders.xlsx"), "--reviewed"]), 0)
            self.assertEqual(main(["--store", str(self.store.path), "validate"]), 0)
            self.assertEqual(main(["--store", str(self.store.path), "extract"]), 1)


if __name__ == "__main__":
    unittest.main()
