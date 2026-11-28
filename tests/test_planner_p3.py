"""P3 semantic safety, isolation, replay, failure, and compatibility regression tests."""
import copy
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from main import create_app
from ontology.discovery.mysql import Collector
from ontology.planner import runtime
from ontology.registry import Registry
from ontology.v2 import canonical, digest, validate
from server.auth import Principal
from server.errors import ServiceError
from server.execution import QueryBudget
from tests.test_registry_v2 import AUTHOR, REVIEWER, READER, INFOS
from tests import planner_fixtures as fixture


class PlannerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, ONTOLOGY_REGISTRY_PATH=str(Path(tmp.name) / 'registry.sqlite3'), ONTOLOGY_REGISTRY_NAMESPACE='default',
                         ONTOLOGY_PLANNER_SOURCES='["a"]', ONTOLOGY_PLANNER_ROLLOUT_PERCENT='100', ONTOLOGY_QUERY_SOURCES='[]')
        env.start(); self.addCleanup(env.stop)
        self.registry = Registry(); self.doc = fixture.document()
        catalog = patch.object(Collector, 'catalog', autospec=True, side_effect=fixture.catalog)
        self.catalog = catalog.start(); self.addCleanup(catalog.stop)
        execution = patch('ontology.planner.runtime.execute_source', return_value={'status': 'success', 'columns': ['d0', 'm0'], 'rows': [('华东', Decimal('120.50'))], 'row_count': 1, 'truncated': False})
        self.execution = execution.start(); self.addCleanup(execution.stop)
        self.release = self.publish(self.doc)

    def publish(self, doc):
        state = self.registry.active('default')
        change = self.registry.propose(doc, AUTHOR, state['release_id'], state['generation'], INFOS)
        self.registry.review(change['id'], REVIEWER, change['digest'], 'approved', '独立确认指标语义和执行范围')
        return self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)['release_id']

    def plan(self, intent=None, **kwargs):
        return runtime.create_plan(self.registry, READER, INFOS, intent or fixture.intent(), **kwargs)

    def execute(self, plan=None, key='execute-1'):
        return runtime.execute_plan(self.registry, READER, INFOS, (plan or self.plan())['plan_id'], key)

    def code(self, expected, fn):
        with self.assertRaises(ServiceError) as result: fn()
        self.assertEqual(result.exception.code, expected)

    def entity(self, identity): return next(e for e in self.doc['entities'] if e['id'] == identity)

    def test_approved_plan_has_pinned_release_bound_parameters_and_policy(self):
        plan = self.plan()
        self.assertEqual(plan['release_id'], self.release)
        self.assertIn('LEFT JOIN', plan['sql'])
        self.assertIn('t0.`tenant_id` = t1.`tenant_id`', plan['sql'])
        self.assertNotIn('reader', plan['sql'])
        self.assertNotIn('parameters', plan)
        self.assertNotIn('executed', plan['events'])
        with self.registry.connection() as db:
            _, payload = runtime.plan_row(db, plan['plan_id'], READER)
        self.assertIn('reader', payload['parameters'].values())
        self.assertEqual(payload['metrics'][0]['unit'], 'CNY')

    def test_all_supported_aggregates_compile_without_averaging_averages(self):
        for name, expression in [('sales', 'SUM('), ('average', 'AVG('), ('price', '/ NULLIF(SUM('), ('buyers', 'COUNT(DISTINCT'), ('count', 'COUNT(*)')]:
            with self.subTest(name=name): self.assertIn(expression, self.plan(fixture.intent(['metric:' + name]))['sql'])

    def test_empty_metric_contract_or_candidate_is_unanswerable(self):
        self.entity('contract:sales')['availability'] = 'candidate'; self.publish(self.doc)
        self.code('UNANSWERABLE', self.plan)
        self.entity('metric:sales')['availability'] = 'candidate'; self.publish(self.doc)
        self.code('NOT_FOUND', self.plan)
        self.execution.assert_not_called()

    def test_ambiguous_contract_is_not_arbitrarily_selected(self):
        duplicate = copy.deepcopy(self.entity('contract:sales')); duplicate['id'] = 'contract:sales2'; self.doc['entities'].append(duplicate)
        self.publish(self.doc); self.code('AMBIGUOUS_DEFINITION', self.plan)

    def test_arbitrary_sql_and_unapproved_expression_grammar_rejected(self):
        for expression in ('SUM(amount); DROP TABLE orders', 'SLEEP(3)', 'SUM(amount)+1', 'COUNT(*) FROM other', 'AVG(amount)'):
            self.entity('metric:sales')['attributes']['expression'] = expression
            self.publish(self.doc)
            with self.assertRaises(ServiceError): self.plan()
        self.execution.assert_not_called()

    def test_arbitrary_plan_fields_rejected_before_metadata(self):
        before = self.catalog.call_count
        for key in ('sql', 'principal', 'admin', 'limit', 'database', 'availability'):
            with self.assertRaises(ServiceError): self.plan(dict(fixture.intent(), **{key: 'injected'}))
        self.assertEqual(self.catalog.call_count, before)

    def test_scope_and_private_plan_ownership(self):
        plan = self.plan()
        other = Principal('other-reader', READER.sources)
        self.code('NOT_FOUND', lambda: runtime.get_plan(self.registry, other, plan['plan_id']))
        self.code('NOT_FOUND', lambda: runtime.execute_plan(self.registry, other, INFOS, plan['plan_id'], 'other'))
        self.code('NOT_FOUND', lambda: runtime.create_plan(self.registry, Principal('no-scope', frozenset()), INFOS, fixture.intent()))
        self.execution.assert_not_called()

    def test_missing_time_bad_timezone_and_required_scope_are_rejected(self):
        cases = []
        no_time = fixture.intent(); no_time.pop('time'); cases.append(no_time)
        cases += [dict(fixture.intent(), time=x) for x in ({'start': '本月', 'end': '下月', 'timezone': 'Asia/Shanghai'}, {'start': '2026-12-01', 'end': '2026-11-01', 'timezone': 'Asia/Shanghai'}, {'start': '2026-11-01', 'end': '2026-12-01', 'timezone': 'UTC'})]
        cases.append(dict(fixture.intent(), filters=[]))
        for case in cases:
            with self.assertRaises(ServiceError): self.plan(case)

    def test_injection_values_are_bound_and_numeric_coercion_rejected(self):
        request = fixture.intent(); request['filters'].append({'field_id': 'field:orders:owner_id', 'op': 'eq', 'value': "x' OR 1=1 --"})
        plan = self.plan(request); self.assertNotIn('OR 1=1', plan['sql'])
        request['filters'][0]['value'] = '1 OR 1=1'
        with self.assertRaises(ServiceError): self.plan(request)

    def test_unsupported_precision_and_quality_rules_fail_closed(self):
        for value in ('1e-31', '1e35', 'NaN'):
            request = fixture.intent(); request['filters'].append({'field_id': 'field:orders:amount', 'op': 'eq', 'value': value})
            with self.assertRaises(ServiceError): self.plan(request)
        rule = copy.deepcopy(self.entity('policy:owner')); rule.update(id='quality:check', kind='QualityRule')
        rule['attributes']['definition'] = 'unsupported quality rule'; self.doc['entities'].append(rule)
        self.publish(self.doc); self.code('POLICY_UNSUPPORTED', self.plan)

    def test_unapproved_dimension_filter_or_relation_is_rejected(self):
        for updates in ({'dimensions': ['field:orders:amount']}, {'relation_ids': []}, {'filters': [{'field_id': 'field:orders:id', 'op': 'eq', 'value': 1}]}, {'relation_ids': ['unknown']}):
            with self.assertRaises(ServiceError): self.plan(dict(fixture.intent(), **updates))

    def test_fanout_unknown_direction_or_self_join_not_compiled(self):
        for cardinality in ('many-to-many', 'one-to-many', 'unknown', 'one-to-one'):
            self.entity('relation:customer')['attributes']['cardinality'] = cardinality
            self.publish(self.doc)
            with self.assertRaises(ServiceError): self.plan()

    def test_target_unique_key_and_fact_grain_checked_against_actual_metadata(self):
        original = fixture.catalog
        def no_unique(collector):
            cat = original(collector)
            next(t for t in cat['tables'] if t['name'] == 'customers')['unique_keys'] = {}
            return cat
        self.catalog.side_effect = no_unique
        self.code('UNSAFE_CARDINALITY', self.plan)
        self.catalog.side_effect = original
        self.entity('metric:sales')['attributes']['grain'] = ['field:orders:id']; self.publish(self.doc)
        with self.assertRaises(ServiceError): self.plan()

    def test_unsupported_or_deny_policies_are_never_ignored(self):
        for definition, code in [('tenant_id=1', 'SEMANTIC_VALIDATION_FAILED'), (canonical({'version': 1, 'deny': True}), 'POLICY_DENIED')]:
            self.entity('policy:owner')['attributes']['definition'] = definition; self.publish(self.doc)
            self.code(code, self.plan)

    def test_physical_alias_cannot_bypass_source_policy(self):
        source = copy.deepcopy(self.entity('source:a')); source['id'] = 'source:alias'; self.doc['entities'].append(source)
        self.entity('dataset:orders')['dependencies'] = ['source:alias']
        self.entity('dataset:customers')['dependencies'] = ['source:alias']
        self.entity('policy:owner')['attributes']['definition'] = canonical({'version': 1, 'deny': True})
        self.publish(self.doc)
        self.code('POLICY_DENIED', self.plan)

    def test_different_fixed_filters_cannot_be_combined_as_one_where(self):
        self.entity('metric:average')['attributes']['filters'] = [canonical({'field_id': 'field:orders:amount', 'op': 'gt', 'value': 0})]
        self.publish(self.doc)
        with self.assertRaises(ServiceError): self.plan(fixture.intent(['metric:sales', 'metric:average']))

    def test_contract_schema_rejects_missing_explicit_dependencies(self):
        self.entity('contract:sales')['dependencies'].remove('field:orders:created_at')
        with self.assertRaises(ServiceError): validate(self.doc)

    def test_multi_source_dynamic_plan_is_not_silently_summed(self):
        with patch.dict(os.environ, ONTOLOGY_PLANNER_SOURCES='["a","b"]'):
            with self.assertRaises(ServiceError): runtime.create_plan(self.registry, AUTHOR, INFOS, dict(fixture.intent(), source_ids=['a', 'b']))
        self.execution.assert_not_called()

    def test_question_check_clarifies_time_and_does_not_guess_unknown_names(self):
        result = runtime.check_question(self.registry, READER, ['a'], 'sales')
        self.assertEqual(result['status'], 'needs_clarification'); self.assertIn('time', result['missing'])
        self.assertEqual(runtime.check_question(self.registry, READER, ['a'], 'why is sales falling')['status'], 'unanswerable')
        self.execution.assert_not_called()

    def test_success_receipt_keeps_decimal_and_selected_executed_evidence(self):
        receipt = self.execute()
        self.assertEqual(receipt['state'], 'succeeded')
        self.assertEqual(receipt['result']['rows'], [['华东', '120.50']])
        self.assertIn('verified', receipt['result']['evidence']['events'])
        self.assertNotIn('parameters', receipt['result']['evidence'])

    def test_execution_idempotency_survives_restart_and_is_plan_bound(self):
        plan = self.plan(); first = self.execute(plan)
        self.registry = Registry()
        self.assertEqual(self.execute(plan)['execution_id'], first['execution_id'])
        self.assertEqual(self.execution.call_count, 1)
        self.code('IDEMPOTENCY_CONFLICT', lambda: self.execute(self.plan()))

    def test_concurrent_same_key_does_not_duplicate_sql_and_other_key_is_busy(self):
        plan = self.plan(); entered = threading.Event(); finish = threading.Event()
        def slow(*args):
            entered.set()
            if not finish.wait(5): raise RuntimeError('fixture timed out')
            return {'status': 'success', 'columns': ['d0', 'm0'], 'rows': [('华东', 1)], 'truncated': False}
        self.execution.side_effect = slow
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.execute, plan)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.execute(plan)['state'], 'running')
                self.code('EXECUTION_BUSY', lambda: self.execute(plan, 'another-key'))
            finally: finish.set()
            self.assertEqual(future.result()['state'], 'succeeded')
        self.assertEqual(self.execution.call_count, 1)

    def test_plan_and_result_expiration_never_trigger_implicit_reexecution(self):
        plan = self.plan(); receipt = self.execute(plan)
        with self.registry.connection(write=True) as db: db.execute('UPDATE query_plans SET expires=0')
        self.code('PLAN_EXPIRED', lambda: self.execute(plan, 'new-key'))
        self.assertEqual(self.execute(plan)['execution_id'], receipt['execution_id'])
        with self.registry.connection(write=True) as db: db.execute('UPDATE query_executions SET expires=0')
        self.code('RESULT_EXPIRED', lambda: self.execute(plan))
        self.assertEqual(self.execution.call_count, 1)

    def test_active_version_changes_do_not_mutate_pinned_plan(self):
        plan = self.plan(); self.entity('metric:sales')['name'] = 'new sales'; self.publish(self.doc)
        self.assertEqual(self.execute(plan)['result']['evidence']['release_id'], self.release)

    def test_policy_revision_revokes_old_plans_and_cached_results(self):
        plan = self.plan(); receipt = self.execute(plan)
        self.entity('policy:owner')['attributes']['definition'] = canonical({'version': 1, 'deny': True})
        self.publish(self.doc)
        self.code('STALE_POLICY', lambda: self.execute(plan, 'new'))
        self.code('STALE_POLICY', lambda: runtime.get_execution(self.registry, READER, receipt['execution_id'], INFOS))
        self.code('STALE_POLICY', lambda: self.plan(release_id=self.release))
        self.assertEqual(self.execution.call_count, 1)

    def test_policy_revision_also_revokes_export_downloads(self):
        from server.exports import read_export
        from server import conf
        self.execution.return_value['rows'] = [('华东', Decimal('1')), ('华南', Decimal('2'))]
        plan = self.plan()
        with patch.dict(conf.mysql_info, INFOS, clear=True), patch.dict(os.environ, MAX_ROW_COUNT_RETURN='1', QUERY_EXPORT_PATH=str(self.registry.path.parent / 'exports')):
            response = runtime.chat(READER, INFOS, ['a'], {'plan_id': plan['plan_id'], 'idempotency_key': 'export'})
            identity = response['extra'].rsplit('/', 1)[-1]
            self.assertIn(b'm0', read_export(READER, identity))
            self.entity('policy:owner')['attributes']['definition'] = canonical({'version': 1, 'deny': True})
            self.publish(self.doc)
            self.code('STALE_POLICY', lambda: read_export(READER, identity))

    def test_quarantine_blocks_plan_execution_and_cached_results(self):
        plan = self.plan(); receipt = self.execute(plan)
        self.registry.quarantine('default', self.release, AUTHOR, '隔离')
        with self.assertRaises(ServiceError): self.execute(plan)
        with self.assertRaises(ServiceError): runtime.get_execution(self.registry, READER, receipt['execution_id'], INFOS)
        self.assertEqual(self.execution.call_count, 1)

    def test_revoked_source_and_disabled_rollout_block_cached_results(self):
        receipt = self.execute()
        with self.assertRaises(ServiceError): runtime.get_execution(self.registry, Principal(READER.subject, frozenset()), receipt['execution_id'], INFOS)
        with patch.dict(os.environ, ONTOLOGY_PLANNER_ROLLOUT_PERCENT='0'):
            self.code('PLANNER_NOT_ENABLED', lambda: runtime.get_execution(self.registry, READER, receipt['execution_id'], INFOS))
        changed = copy.deepcopy(INFOS); changed['a']['host'] = 'other-host'
        self.code('STALE_BINDING', lambda: runtime.get_execution(self.registry, READER, receipt['execution_id'], changed))

    def test_endpoint_remap_and_schema_drift_fail_before_business_sql(self):
        plan = self.plan()
        changed = copy.deepcopy(INFOS); changed['a']['host'] = 'remapped'
        receipt = runtime.execute_plan(self.registry, READER, changed, plan['plan_id'], 'remap')
        self.assertEqual(receipt['error_code'], 'STALE_BINDING')
        self.catalog.side_effect = lambda c: dict(fixture.catalog(c), fingerprint='changed')
        self.assertEqual(self.execute(plan, 'drift')['error_code'], 'STALE_BINDING')
        self.execution.assert_not_called()

    def test_drift_during_execution_discards_result(self):
        plan = self.plan()
        calls = [0]
        def drift(c):
            calls[0] += 1
            return dict(fixture.catalog(c), fingerprint='changed') if calls[0] == 2 else fixture.catalog(c)
        self.catalog.side_effect = drift
        result = self.execute(plan); self.assertEqual(result['error_code'], 'STALE_BINDING'); self.assertIsNone(result['result'])

    def test_failure_truncation_output_mismatch_and_nan_never_form_complete_answer(self):
        for n, updates in enumerate([{'status': 'failed'}, {'truncated': True}, {'columns': ['wrong', 'm0']}, {'rows': [('华东', float('nan'))]}, {'rows': [('华东', '123')]}]):
            self.execution.return_value = dict(status='success', columns=['d0', 'm0'], rows=[('华东', Decimal('1'))], truncated=False, **{})
            self.execution.return_value.update(updates)
            result = self.execute(key='fail-' + str(n))
            self.assertEqual(result['state'], 'failed'); self.assertIsNone(result['result'])

    def test_failure_is_sanitized_and_replay_does_not_retry(self):
        self.execution.side_effect = RuntimeError('secret password and private rows')
        plan = self.plan(); receipt = self.execute(plan)
        self.assertEqual(receipt['error_code'], 'EXECUTION_FAILED'); self.assertNotIn('secret', canonical(receipt))
        self.execute(plan); self.assertEqual(self.execution.call_count, 1)

    def test_interrupted_running_receipt_is_terminal_and_late_worker_discarded(self):
        plan = self.plan()
        def interrupted(*args):
            with self.registry.connection(write=True) as db: db.execute('UPDATE query_executions SET lease_until=0')
            return {'status': 'success', 'columns': ['d0', 'm0'], 'rows': [('华东', 1)], 'truncated': False}
        self.execution.side_effect = interrupted
        result = self.execute(plan)
        self.assertEqual(result['error_code'], 'EXECUTION_INTERRUPTED'); self.assertIsNone(result['result'])

    def test_tampered_plan_and_result_fail_integrity_checks(self):
        plan = self.plan(); receipt = self.execute(plan)
        with self.registry.connection(write=True) as db: db.execute("UPDATE query_executions SET payload='{}'")
        self.code('EXECUTION_CORRUPT', lambda: runtime.get_execution(self.registry, READER, receipt['execution_id'], INFOS))
        with self.registry.connection(write=True) as db: db.execute("UPDATE query_plans SET payload='{}'")
        self.code('PLAN_CORRUPT', lambda: runtime.get_plan(self.registry, READER, plan['plan_id']))

    def test_artifact_quota_fails_before_sql(self):
        plan = self.plan()
        with patch('ontology.planner.runtime.MAX_ARTIFACT_BYTES', 0):
            self.code('PLANNER_CAPACITY', self.plan)
            self.code('PLANNER_CAPACITY', lambda: self.execute(plan))
        self.execution.assert_not_called()

    def test_chat_plan_path_preserves_response_shape_without_llm_or_repair(self):
        from server import api, conf
        plan = self.plan()
        with patch.dict(conf.mysql_info, INFOS, clear=True), patch('server.api.aws.get', side_effect=AssertionError('must not call models')), patch('server.api.retry_when_sql_error', side_effect=AssertionError('must not repair')):
            response = api.get_result([{'role': 'user', 'content': 'sales'}], 'trace', principal=READER, source_ids=['a'], ontology_options={'plan_id': plan['plan_id'], 'idempotency_key': 'chat'})
        self.assertEqual(response['status'], 'success'); self.assertIn('mdData', response); self.assertIn('execution_id', response['ontology'])

    def test_http_planning_and_execution_trust_only_gateway_identity(self):
        def verifier(header):
            if header != 'Bearer reader': raise ServiceError('AUTH_REQUIRED', 'login', 401)
            return READER
        client = create_app(verifier=verifier).test_client(); headers = {'Authorization': 'Bearer reader'}
        with patch('server.planner_api.sources', return_value=INFOS):
            self.assertEqual(client.post('/v1/query-plans/validate', json={'intent': fixture.intent()}).status_code, 401)
            self.assertEqual(client.post('/v1/query-plans/validate', headers=headers, json={'intent': fixture.intent(), 'actor': 'admin'}).status_code, 422)
            plan = client.post('/v1/query-plans/validate', headers=headers, json={'intent': fixture.intent()})
            self.assertEqual(plan.status_code, 201, plan.json)
            receipt = client.post('/v1/queries/execute', headers=headers, json={'plan_id': plan.json['plan_id'], 'idempotency_key': 'http'})
            self.assertEqual(receipt.status_code, 200, receipt.json)
            self.assertEqual(client.get('/v1/executions/' + receipt.json['execution_id'], headers=headers).status_code, 200)


if __name__ == '__main__': unittest.main()
