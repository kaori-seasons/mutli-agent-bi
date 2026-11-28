"""Disposable MySQL gold results for approved metric plans; never reads application .env."""
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import pymysql

from ontology.registry import Registry
from ontology.planner import runtime
from ontology.discovery import workflow
from ontology.v2 import entity
from tests.test_registry_v2 import AUTHOR, REVIEWER, READER
from tests import planner_fixtures as fixture


@unittest.skipUnless(os.getenv('P0_MYSQL_TEST_PORT') and os.getenv('P0_MYSQL_TEST_ALLOW_CREATE') == '1', 'disposable MySQL fixture not enabled')
class PlannerMySQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        suffix = uuid4().hex[:10]; cls.database = 'p3_' + suffix; cls.user = 'p3_reader_' + suffix
        cls.admin = pymysql.connect(host='127.0.0.1', port=int(os.environ['P0_MYSQL_TEST_PORT']), user='root', password=os.environ['P0_MYSQL_TEST_PASSWORD'], autocommit=True)
        cls.addClassCleanup(cls.cleanup)
        with cls.admin.cursor() as c:
            c.execute(f'CREATE DATABASE {cls.database}')
            c.execute(f"CREATE USER '{cls.user}'@'%' IDENTIFIED BY 'p3-synthetic-reader'")
            c.execute(f"GRANT SELECT ON {cls.database}.* TO '{cls.user}'@'%'")
            c.execute(f'USE {cls.database}')
            c.execute('CREATE TABLE customers (tenant_id INT, id INT, region VARCHAR(80), PRIMARY KEY(tenant_id,id))')
            c.execute("INSERT INTO customers VALUES (1,1,'华东'),(1,2,'华南'),(2,1,'另一个租户')")
            c.execute('CREATE TABLE orders (tenant_id INT, id INT, customer_id INT, amount DECIMAL(12,2), units INT, created_at DATETIME, owner_id VARCHAR(80), PRIMARY KEY(tenant_id,id))')
            c.executemany('INSERT INTO orders VALUES (%s,%s,%s,%s,%s,%s,%s)', [
                (1,1,1,'100.00',2,'2026-11-01 00:00:00','reader'),
                (1,2,1,'20.00',1,'2026-11-15 00:00:00','reader'),
                (1,3,2,'30.00',0,'2026-11-30 23:59:59','reader'),
                (1,4,2,None,None,'2026-11-20 00:00:00','reader'),
                (1,5,99,'10.00',1,'2026-11-10 00:00:00','reader'),
                (1,6,1,'9999.00',1,'2026-11-10 00:00:00','other-user'),
                (1,7,1,'9999.00',1,'2026-12-01 00:00:00','reader'),
                (2,1,1,'9999.00',1,'2026-11-10 00:00:00','reader'),
            ])
        cls.info = dict(source_id='a', db=cls.database, host='127.0.0.1', port=int(os.environ['P0_MYSQL_TEST_PORT']), user=cls.user, pwd='p3-synthetic-reader')

    @classmethod
    def cleanup(cls):
        try:
            with cls.admin.cursor() as c:
                c.execute(f'DROP DATABASE IF EXISTS {cls.database}')
                c.execute(f"DROP USER IF EXISTS '{cls.user}'@'%'")
        finally: cls.admin.close()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, ONTOLOGY_REGISTRY_PATH=str(Path(tmp.name) / 'registry.sqlite3'), ONTOLOGY_REGISTRY_NAMESPACE='default',
                         ONTOLOGY_PLANNER_SOURCES='["a"]', ONTOLOGY_PLANNER_ROLLOUT_PERCENT='100', ONTOLOGY_QUERY_SOURCES='[]')
        env.start(); self.addCleanup(env.stop)
        self.registry = Registry(); self.infos = {'a': self.info}; self.doc = fixture.document(self.database)
        self.publish(self.doc)

    def publish(self, document):
        state = self.registry.active('default')
        change = self.registry.propose(document, AUTHOR, state['release_id'], state['generation'], self.infos)
        self.registry.review(change['id'], REVIEWER, change['digest'], 'approved', '合成数据独立复核')
        return self.registry.publish(change['id'], AUTHOR, change['digest'], self.infos)

    def execute(self, intent):
        plan = runtime.create_plan(self.registry, READER, self.infos, intent)
        receipt = runtime.execute_plan(self.registry, READER, self.infos, plan['plan_id'], uuid4().hex)
        self.assertEqual(receipt['state'], 'succeeded', receipt)
        return receipt['result']

    def test_real_sum_avg_ratio_and_distinct_match_gold_with_nulls_and_orphans(self):
        result = self.execute(fixture.intent(['metric:sales', 'metric:average', 'metric:price', 'metric:buyers']))
        rows = {r[0]: r[1:] for r in result['rows']}
        self.assertEqual(set(rows), {None, '华东', '华南'})
        self.assertEqual([Decimal(x) for x in rows['华东'][:3]], [Decimal('120'), Decimal('60'), Decimal('40')])
        self.assertEqual(rows['华东'][3], 1)
        self.assertEqual(Decimal(rows['华南'][0]), Decimal('30'))
        self.assertEqual(Decimal(rows['华南'][1]), Decimal('30'))
        self.assertIsNone(rows['华南'][2])  # Zero denominator remains NULL, never fabricated zero.
        self.assertEqual(Decimal(rows[None][0]), Decimal('10'))  # LEFT JOIN preserves unmatched facts.

    def test_total_avg_and_ratio_use_original_grain_not_group_averages(self):
        result = self.execute(fixture.intent(['metric:sales', 'metric:average', 'metric:price', 'metric:count'], dimensions=False))
        total, avg, ratio, count = result['rows'][0]
        self.assertEqual(Decimal(total), Decimal('160'))
        self.assertEqual(Decimal(avg), Decimal('40'))
        self.assertEqual(Decimal(ratio), Decimal('40'))
        self.assertEqual(count, 5)  # COUNT(*) includes one NULL amount, AVG excludes it.

    def test_time_half_open_tenant_and_subject_policy_are_all_effective(self):
        result = self.execute(fixture.intent(['metric:count'], dimensions=False))
        self.assertEqual(result['rows'], [[5]])
        self.assertEqual(result['evidence']['database'], self.database)
        self.assertIn('policy:owner', result['evidence']['dependency_versions'])
        self.assertLessEqual(result['evidence']['sql_calls'], 16)

    def test_real_empty_result_and_injected_string_do_not_leak_rows(self):
        intent = fixture.intent(dimensions=False)
        intent['filters'].append({'field_id': 'field:orders:owner_id', 'op': 'eq', 'value': "x' OR 1=1; DROP TABLE orders --"})
        result = self.execute(intent)
        self.assertEqual(result['rows'], [[None]])
        self.assertNotIn('DROP TABLE', result['sql'])
        count = self.execute(fixture.intent(['metric:count'], dimensions=False))
        self.assertEqual(count['rows'], [[5]])

    def test_numeric_filters_preserve_decimal_boundary(self):
        request = fixture.intent(['metric:count'], dimensions=False)
        request['filters'].append({'field_id': 'field:orders:amount', 'op': 'gt', 'value': '99.99999999999999999999'})
        result = self.execute(request)
        self.assertEqual(result['rows'], [[1]])
        self.assertIn('DECIMAL(65,30)', result['sql'])

    def test_real_duplicate_target_cannot_be_used_for_join(self):
        with self.admin.cursor() as c:
            c.execute(f'ALTER TABLE {self.database}.customers DROP PRIMARY KEY')
            c.execute(f"INSERT INTO {self.database}.customers VALUES (1,1,'重复客户')")
        try:
            from server.errors import ServiceError
            with self.assertRaises(ServiceError) as error:
                runtime.create_plan(self.registry, READER, self.infos, fixture.intent())
            self.assertEqual(error.exception.code, 'UNSAFE_CARDINALITY')
        finally:
            with self.admin.cursor() as c:
                c.execute(f"DELETE FROM {self.database}.customers WHERE region='重复客户'")
                c.execute(f'ALTER TABLE {self.database}.customers ADD PRIMARY KEY(tenant_id,id)')

    def test_schema_changed_after_validation_blocks_sql_execution(self):
        plan = runtime.create_plan(self.registry, READER, self.infos, fixture.intent())
        with self.admin.cursor() as c: c.execute(f"ALTER TABLE {self.database}.orders COMMENT='drift'")
        try:
            with patch('ontology.planner.runtime.execute_source') as execute:
                result = runtime.execute_plan(self.registry, READER, self.infos, plan['plan_id'], 'drift')
                self.assertEqual(result['error_code'], 'STALE_BINDING'); execute.assert_not_called()
        finally:
            with self.admin.cursor() as c: c.execute(f"ALTER TABLE {self.database}.orders COMMENT=''")

    def test_truncation_returns_failure_not_incomplete_metric_answer(self):
        plan = runtime.create_plan(self.registry, READER, self.infos, fixture.intent())
        with patch.dict(os.environ, SQL_MAX_RESULT_ROWS='1'):
            result = runtime.execute_plan(self.registry, READER, self.infos, plan['plan_id'], 'limited')
        self.assertEqual(result['error_code'], 'PARTIAL_RESULT'); self.assertIsNone(result['result'])

    def test_e3_p2_candidate_then_human_metric_contract_then_execution(self):
        # E3 is an isolated domain with no pre-existing manual assets or policies to replace.
        self.registry = Registry(Path(self.registry.path).parent / 'e3.sqlite3')
        columns = ['tenant_id', 'id', 'customer_id', 'amount', 'units', 'created_at', 'owner_id']
        policy = {'a': {'tables': {'orders': dict(columns=columns, profile_columns=['amount'], sensitive_columns=[])}, 'sample_rows': 20, 'relations': []}}
        with patch.dict(os.environ, ONTOLOGY_DISCOVERY_ENABLED='true', ONTOLOGY_DISCOVERY_POLICY=json.dumps(policy)):
            job = workflow.run(self.registry, 'default', AUTHOR, self.infos, 'a', 'e3-discovery')
            self.assertEqual(job['state'], 'complete')
            metric = next(e for e in job['artifact']['document']['entities'] if e['kind'] == 'Metric')
            attrs = dict(metric['attributes'], unit='条', time_definition='全量记录数，无时间过滤')
            state = self.registry.active('default')
            change = workflow.prepare_review(self.registry, 'default', job['id'], AUTHOR, self.infos, job['digest'], [metric['id']], {metric['id']: {'attributes': attrs}}, state['release_id'], state['generation'], '人工确认全量记录含历史，独立测试数据域')
            self.registry.review(change['id'], REVIEWER, change['digest'], 'approved', '独立核验粒度')
            self.registry.publish(change['id'], AUTHOR, change['digest'], self.infos)
        dataset = next(e for e in change['document']['entities'] if e['id'] in metric['dependencies'] and e['kind'] == 'Dataset')
        # A separate approval deliberately grants execution scope; P2 reference publication alone cannot execute.
        contract = entity('MetricContract', 'contract:e3', 'E3 全量计数执行范围', 'fixture-owner', ['a'],
                          dict(metric_id=metric['id'], dataset_id=dataset['id'], dimensions=[], filter_fields=[], relation_ids=[], time_field=None, timezone='UTC', required_filters=[]),
                          dependencies=[metric['id'], dataset['id']], availability='reference')
        document = dict(change['document'], entities=[*change['document']['entities'], contract])
        self.publish(document)
        result = self.execute({'source_ids': ['a'], 'metric_ids': [metric['id']]})
        self.assertEqual(result['rows'], [[8]])
        self.assertTrue(all(e['availability'] == 'candidate' for e in workflow.get_run(self.registry, 'default', job['id'], AUTHOR)['artifact']['document']['entities']))


if __name__ == '__main__': unittest.main()
