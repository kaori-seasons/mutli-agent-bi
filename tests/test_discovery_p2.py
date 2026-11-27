"""Candidate isolation, bounded evidence, and the P2 → P1 human review boundary."""
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from main import create_app
from ontology.discovery import workflow
from ontology.discovery.compiler import compile_candidates
from ontology.discovery.mysql import Collector
from ontology.discovery.policy import load_policy, quote
from ontology.discovery.upstream import adapt, PROTOCOL, UPSTREAM_SHA
from ontology.registry import Registry
from ontology.v2 import digest
from server.auth import Principal
from server.errors import ServiceError
from tests.test_registry_v2 import AUTHOR, REVIEWER, READER, INFOS, document


def policy_config():
    return {'a': {'sample_rows': 10, 'tables': {
        name: {'columns': ['tenant_id', 'id', 'email'], 'profile_columns': ['id', 'email'], 'sensitive_columns': []}
        for name in ('parents', 'children')}, 'relations': []}}


def observations():
    tables = []
    for table in ('parents', 'children'):
        tables.append(dict(name=table, engine='InnoDB', comment='', primary_key=['tenant_id', 'id'],
                           unique_keys={'PRIMARY': ['tenant_id', 'id']}, columns=[
            dict(name=name, dtype='int' if name != 'email' else 'varchar(100)', nullable='NO', collation=None, comment='')
            for name in ('tenant_id', 'id', 'email')]))
    relation = dict(left_table='children', left_keys=['tenant_id', 'id'], right_table='parents', right_keys=['tenant_id', 'id'], origin='declared', constraint='fk_parent')
    catalog = dict(source_id='a', database='db_a', tables=tables, relations=[relation])
    catalog['fingerprint'] = digest(catalog)
    return dict(catalog=catalog, catalog_after=catalog['fingerprint'], schema_stable=True, profiles=[],
                relations=[dict(relation, observed_at='2026-11-27T00:00:00Z', validation='sampled_pass', containment=1,
                                target_uniqueness={'unique': True}, fanout_ratio=1)], errors=[], observed_at='2026-11-27T00:00:00Z', sql_calls=12)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.config = policy_config()
        env = patch.dict(os.environ, ONTOLOGY_REGISTRY_PATH=str(Path(tmp.name) / 'registry.sqlite3'),
                         ONTOLOGY_REGISTRY_NAMESPACE='default', ONTOLOGY_DISCOVERY_ENABLED='true',
                         ONTOLOGY_DISCOVERY_POLICY=json.dumps(self.config))
        env.start(); self.addCleanup(env.stop)
        self.registry = Registry(); self.obs = observations()
        collect = patch.object(Collector, 'collect', side_effect=lambda: copy.deepcopy(self.obs))
        self.collect = collect.start(); self.addCleanup(collect.stop)
        catalog = patch.object(Collector, 'catalog', side_effect=lambda: copy.deepcopy(self.obs['catalog']))
        self.catalog = catalog.start(); self.addCleanup(catalog.stop)

    def run_job(self, key='request-1', **kwargs):
        return workflow.run(self.registry, 'default', AUTHOR, INFOS, 'a', key, **kwargs)

    def handoff(self, job=None, kind='Dataset', edits=None, **kwargs):
        job = job or self.run_job()
        target = next(e for e in job['artifact']['document']['entities'] if e['kind'] == kind)
        state = self.registry.active('default')
        return workflow.prepare_review(self.registry, 'default', job['id'], AUTHOR, INFOS, job['digest'], [target['id']],
                                       {target['id']: edits} if edits else {}, state['release_id'], state['generation'], '人工确认了业务口径', **kwargs)

    def assert_code(self, code, fn):
        with self.assertRaises(ServiceError) as result: fn()
        self.assertEqual(result.exception.code, code)

    def test_automatic_outputs_never_enter_changesets_releases_or_query_index(self):
        job = self.run_job()
        self.assertEqual(job['state'], 'complete')
        self.assertTrue(all(e['availability'] == 'candidate' for e in job['artifact']['document']['entities']))
        self.assertNotIn('QueryTemplate', [e['kind'] for e in job['artifact']['document']['entities']])
        self.assertEqual(self.registry.pending('default', AUTHOR)['changesets'], [])
        self.assertEqual(self.registry.history('default', AUTHOR)['releases'], [])
        self.assert_code('RELEASE_NOT_READY', lambda: self.registry.pin('default', READER))

    def test_composite_keys_stable_ids_and_no_credentials(self):
        one = compile_candidates(self.obs, 'default', 'operator', 'run1')
        two = compile_candidates(self.obs, 'default', 'operator', 'run2')
        self.assertEqual([e['id'] for e in one['entities']], [e['id'] for e in two['entities']])
        relation = next(e for e in one['entities'] if e['kind'] == 'Relation')
        self.assertEqual(len(relation['attributes']['left_keys']), 2)
        self.assertEqual(relation['attributes']['cardinality'], 'unknown')
        self.assertNotIn('pwd', json.dumps(one))

    def test_idempotency_survives_restart_and_conflicting_scope_rejected(self):
        first = self.run_job()
        self.registry = Registry()
        self.assertEqual(first['id'], self.run_job()['id'])
        self.assertEqual(self.collect.call_count, 1)
        self.assert_code('IDEMPOTENCY_CONFLICT', lambda: self.run_job(tables=['parents']))

    def test_partial_and_failed_runs_cannot_handoff_and_errors_are_sanitized(self):
        self.obs['errors'] = [{'stage': 'relation', 'code': 'SOURCE_UNAVAILABLE'}]
        job = self.run_job()
        self.assertEqual(job['state'], 'partial')
        self.assert_code('DISCOVERY_NOT_READY', lambda: self.handoff(job))
        self.collect.side_effect = RuntimeError('secret password and database rows')
        failed = self.run_job('failure')
        self.assertEqual(failed['error_code'], 'DISCOVERY_FAILED')
        self.assertNotIn('secret', json.dumps(failed))

    def test_interrupted_lease_is_failed_without_reexecution(self):
        job = self.run_job()
        with self.registry.connection(write=True) as db:
            db.execute("UPDATE discovery_runs SET state='running',lease_until=0 WHERE id=?", (job['id'],))
        retry = self.run_job()
        self.assertEqual(retry['error_code'], 'RUN_INTERRUPTED')
        self.assertEqual(self.collect.call_count, 1)

    def test_same_source_concurrency_rejected(self):
        job = self.run_job()
        with self.registry.connection(write=True) as db:
            db.execute("UPDATE discovery_runs SET state='running',lease_until=? WHERE id=?", (time.time() + 60, job['id']))
        self.assert_code('DISCOVERY_BUSY', lambda: self.run_job('other'))

    def test_admin_and_source_scope_enforced_before_collect_and_audit(self):
        job = self.run_job()
        outsider = Principal('outsider', frozenset({'b'}), True)
        for principal in (READER, outsider):
            with self.assertRaises(ServiceError):
                workflow.run(self.registry, 'default', principal, INFOS, 'a', 'denied')
            with self.assertRaises(ServiceError):
                workflow.get_run(self.registry, 'default', job['id'], principal)
        self.assertEqual(workflow.list_runs(self.registry, 'default', outsider)['runs'], [])
        with self.assertRaises(ServiceError): self.registry.audit('default', outsider)
        self.assertEqual(self.collect.call_count, 1)

    def test_reference_handoff_independent_review_and_explicit_publish(self):
        job = self.run_job(); change = self.handoff(job)
        self.assertIsNone(self.registry.active('default')['release_id'])
        self.assertTrue(all(e['availability'] == 'reference' for e in change['document']['entities']))
        self.assert_code('REVIEW_SEPARATION_REQUIRED', lambda: self.registry.review(change['id'], AUTHOR, change['digest'], 'approved', 'reviewed'))
        self.assert_code('REVIEW_REQUIRED', lambda: self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS))
        self.registry.review(change['id'], REVIEWER, change['digest'], 'approved', 'reviewed')
        receipt = self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)
        self.assertTrue(self.registry.pin('default', READER)['entities'])
        self.assertEqual(self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)['release_id'], receipt['release_id'])
        self.assertTrue(all(e['availability'] == 'candidate' for e in workflow.get_run(self.registry, 'default', job['id'], AUTHOR)['artifact']['document']['entities']))

    def test_existing_query_templates_are_preserved(self):
        initial = self.registry.propose(document(), AUTHOR, None, 0, INFOS)
        self.registry.review(initial['id'], REVIEWER, initial['digest'], 'approved', 'approved')
        self.registry.publish(initial['id'], AUTHOR, initial['digest'], INFOS)
        change = self.handoff()
        template = next(e for e in change['document']['entities'] if e['kind'] == 'QueryTemplate')
        self.assertEqual(template, next(e for e in initial['document']['entities'] if e['kind'] == 'QueryTemplate'))

    def test_missing_metric_scope_and_empty_business_definition_rejected(self):
        for kind in ('Metric', 'BusinessConcept'):
            with self.assertRaises(ServiceError): self.handoff(kind=kind)
        job = self.run_job()
        metric = next(e for e in job['artifact']['document']['entities'] if e['kind'] == 'Metric')
        attrs = dict(metric['attributes'], unit='条', time_definition='截至采集时刻的记录数')
        change = self.handoff(job, 'Metric', {'attributes': attrs})
        self.assertEqual(next(e for e in change['document']['entities'] if e['kind'] == 'Metric')['attributes']['unit'], '条')

    def test_relation_requires_evidence_and_human_direction_never_infers_one_to_one(self):
        job = self.run_job()
        relation = next(e for e in job['artifact']['document']['entities'] if e['kind'] == 'Relation')
        with self.assertRaises(ServiceError): self.handoff(job, 'Relation')
        attrs = dict(relation['attributes'], join_type='left', cardinality='one-to-one')
        with self.assertRaises(ServiceError): self.handoff(job, 'Relation', {'attributes': attrs})
        attrs['cardinality'] = 'many-to-one'
        change = self.handoff(job, 'Relation', {'attributes': attrs})
        self.assertTrue(any(e['kind'] == 'Evidence' for e in change['document']['entities']))
        self.obs['relations'][0]['validation'] = 'sampled_fail'
        failed = self.run_job('orphans')
        with self.assertRaises(ServiceError): self.handoff(failed, 'Relation', {'attributes': attrs})

    def test_cannot_edit_evidence_bindings_or_promote_executable(self):
        for edit in ({'availability': 'executable'}, {'attributes': {}}, {'dependencies': []}):
            with self.assertRaises(ServiceError): self.handoff(edits=edit)
        with self.assertRaises(ServiceError): self.handoff(kind='Evidence', edits={'attributes': {}})

    def test_schema_drift_policy_changes_and_expiration_block_publish(self):
        change = self.handoff()
        self.registry.review(change['id'], REVIEWER, change['digest'], 'approved', 'ok')
        self.catalog.side_effect = lambda: {'fingerprint': 'changed'}
        self.assert_code('STALE_BINDING', lambda: self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS))
        self.catalog.side_effect = lambda: self.obs['catalog']
        with patch.dict(os.environ, ONTOLOGY_DISCOVERY_POLICY=json.dumps(dict(self.config, a=dict(self.config['a'], sample_rows=11)))):
            self.assert_code('STALE_EVIDENCE', lambda: self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS))
        with self.registry.connection(write=True) as db: db.execute('UPDATE discovery_review_links SET expires=0')
        self.assert_code('STALE_EVIDENCE', lambda: self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS))
        self.assertIsNone(self.registry.active('default')['release_id'])

    def test_tampered_run_digest_and_handoff_compare_and_swap(self):
        job = self.run_job()
        with self.assertRaises(ServiceError):
            workflow.prepare_review(self.registry, 'default', job['id'], AUTHOR, INFOS, 'wrong', ['x'], {}, None, 0, 'reason')
        with self.registry.connection(write=True) as db: db.execute("UPDATE discovery_runs SET payload='{}'")
        self.assert_code('REGISTRY_CORRUPT', lambda: workflow.get_run(self.registry, 'default', job['id'], AUTHOR))

    def test_disabled_and_invalid_policy_fail_closed(self):
        with patch.dict(os.environ, ONTOLOGY_DISCOVERY_ENABLED='false'):
            self.assert_code('DISCOVERY_DISABLED', lambda: self.run_job())
        with patch.dict(os.environ, ONTOLOGY_DISCOVERY_POLICY='{}'):
            self.assert_code('DISCOVERY_CONFIGURATION', lambda: self.run_job())
        for value in ('orders; DROP TABLE x', 'a.b', '`orders`'):
            with self.assertRaises(ServiceError): quote(value)

    def test_physical_endpoint_remap_and_expired_artifact_block_handoff(self):
        job = self.run_job()
        with self.assertRaises(ServiceError) as error:
            workflow.assert_current(job['artifact'], dict(INFOS['a'], host='other-host'))
        self.assertEqual(error.exception.code, 'STALE_BINDING')
        workflow.assert_current(job['artifact'], dict(INFOS['a'], pwd='rotated-password'))
        artifact = copy.deepcopy(job['artifact']); artifact['expires_at'] = 0
        self.assert_code('STALE_EVIDENCE', lambda: workflow.assert_current(artifact, INFOS['a']))

    def test_handoff_rejects_stale_generation_and_namespace_access(self):
        job = self.run_job()
        target = next(e for e in job['artifact']['document']['entities'] if e['kind'] == 'Dataset')
        self.assert_code('REVISION_CONFLICT', lambda: workflow.prepare_review(self.registry, 'default', job['id'], AUTHOR, INFOS,
                         job['digest'], [target['id']], {}, None, 9, '核验'))
        self.assert_code('NOT_FOUND', lambda: workflow.get_run(self.registry, 'another', job['id'], AUTHOR))
        self.assertEqual(self.registry.pending('default', AUTHOR)['changesets'], [])

    def test_late_worker_cannot_overwrite_interrupted_run(self):
        def interrupt():
            with self.registry.connection(write=True) as db:
                db.execute('UPDATE discovery_runs SET lease_until=0')
            return copy.deepcopy(self.obs)
        self.collect.side_effect = interrupt
        job = self.run_job()
        self.assertEqual(job['state'], 'failed')
        self.assertIsNone(job['artifact'])

    def test_publish_index_failure_rolls_back_and_rejection_requires_new_change(self):
        change = self.handoff()
        self.registry.review(change['id'], REVIEWER, change['digest'], 'approved', '核验')
        with patch('ontology.registry.build_index', side_effect=RuntimeError('synthetic index failure')):
            with self.assertRaises(RuntimeError): self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)
        self.assertIsNone(self.registry.active('default')['release_id'])
        self.assertEqual(self.registry.changeset(change['id'], AUTHOR)['status'], 'approved')
        self.assertTrue(self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)['release_id'])
        rejected = self.handoff()
        self.registry.review(rejected['id'], REVIEWER, rejected['digest'], 'rejected', '业务不符')
        with self.assertRaises(ServiceError): self.registry.publish(rejected['id'], AUTHOR, rejected['digest'], INFOS)

    def test_approved_definitions_are_not_overwritten_by_unselected_dependencies(self):
        job = self.run_job()
        change = self.handoff(job, 'Dataset', {'description': '人工维护的表说明'})
        self.registry.review(change['id'], REVIEWER, change['digest'], 'approved', '核验')
        self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)
        dataset = next(e for e in change['document']['entities'] if e['kind'] == 'Dataset')
        metric = next(e for e in job['artifact']['document']['entities'] if e['kind'] == 'Metric')
        next_change = self.handoff(job, 'Metric', {'attributes': dict(metric['attributes'], unit='条', time_definition='当前存量')})
        self.assertEqual(next(e for e in next_change['document']['entities'] if e['id'] == dataset['id']), dataset)

    def test_http_authorization_body_identity_and_complete_minimum_flow(self):
        actor = [AUTHOR]
        def verifier(token):
            if token != 'Bearer fixture':
                raise ServiceError('AUTH_REQUIRED', '请登录', 401)
            return actor[0]
        client = create_app(verifier=verifier).test_client()
        headers = {'Authorization': 'Bearer fixture'}
        with patch('server.ontology_api.sources', return_value=INFOS):
            self.assertEqual(client.get('/v1/ontology/discovery/config').status_code, 401)
            cfg = client.get('/v1/ontology/discovery/config', headers=headers).json
            self.assertEqual([s['source_id'] for s in cfg['sources']], ['a'])
            path = '/v1/ontology/discovery/runs'
            self.assertEqual(client.post(path, headers=headers, json={'source_id': 'a', 'idempotency_key': 'http', 'admin': True}).status_code, 422)
            job = client.post(path, headers=headers, json={'source_id': 'a', 'idempotency_key': 'http'}).json
            entity = next(e for e in job['artifact']['document']['entities'] if e['kind'] == 'Dataset')
            change = client.post(path + '/' + job['id'] + '/prepare-review', headers=headers, json=dict(expected_digest=job['digest'], entity_ids=[entity['id']], edits={}, expected_active=None, expected_generation=0, reason='核验')).json
            actor[0] = REVIEWER
            self.assertEqual(client.post('/v1/ontology/changesets/' + change['id'] + '/review', headers=headers, json=dict(expected_digest=change['digest'], decision='approved', reason='复核')).status_code, 200)
            self.assertEqual(client.post('/v1/ontology/releases', headers=headers, json=dict(changeset_id=change['id'], expected_digest=change['digest'])).status_code, 200)
            actor[0] = READER
            self.assertEqual(client.get(path, headers=headers).status_code, 403)


class ProbeAndAdapterTests(unittest.TestCase):
    def setUp(self):
        self.policy = dict(policy_config()['a'], source_id='a')
        self.obs = observations()
        self.collector = Collector(INFOS['a'], self.policy)

    def test_profile_aggregate_only_and_sensitive_columns_excluded(self):
        with patch.object(self.collector, 'read', return_value=[dict(sampled_rows=4, n0=1, d0=2)]) as read:
            output = self.collector.profile(self.obs['catalog']['tables'][0])
        self.assertEqual(output['columns']['id']['null_fraction'], .25)
        self.assertEqual(output['excluded'], {'email': 'sensitive'})
        self.assertNotIn('`email`', read.call_args.args[0])
        self.assertIn('LIMIT 10', read.call_args.args[0])
        self.assertTrue(output['not_random'])

    def test_empty_profile_and_fanout_or_orphans_never_pass(self):
        with patch.object(self.collector, 'read', return_value=[dict(sampled_rows=0, n0=None, d0=0)]):
            self.assertIsNone(self.collector.profile(self.obs['catalog']['tables'][0])['columns']['id']['null_fraction'])
        tables = {t['name']: t for t in self.obs['catalog']['tables']}
        for duplicate, n, orphan, matched, state in [(0, 0, 0, 0, 'unknown'), (0, 4, 1, 3, 'sampled_fail'), (1, 4, 0, 8, 'sampled_fail'), (0, 4, 0, 8, 'sampled_fail'), (0, 4, 0, 4, 'sampled_pass')]:
            with patch.object(self.collector, 'read', side_effect=[[{'duplicate_groups': duplicate}], [dict(sampled_rows=n, nonnull_count=n, orphan_count=orphan, matched_rows=matched)]]):
                self.assertEqual(self.collector.relation(self.obs['catalog']['relations'][0], tables)['validation'], state)

    def test_type_mismatch_and_sensitive_relation_skip_data_query(self):
        tables = {t['name']: t for t in self.obs['catalog']['tables']}
        tables['parents']['columns'][0]['dtype'] = 'bigint'
        with patch.object(self.collector, 'read') as read:
            self.assertEqual(self.collector.relation(self.obs['catalog']['relations'][0], tables)['validation'], 'type_mismatch')
            self.policy['tables']['children']['sensitive_columns'] = ['id']
            self.assertEqual(self.collector.relation(self.obs['catalog']['relations'][0], tables)['validation'], 'restricted')
            read.assert_not_called()

    def test_truncation_is_fatal_and_probe_reuses_readonly_execution_gate(self):
        with patch('ontology.discovery.mysql.execute_read', return_value={'truncated': True}) as execute:
            with self.assertRaises(ServiceError): self.collector.read('SELECT 1', catalog=True)
            self.assertEqual(execute.call_args.args[0]['db'], 'information_schema')
            self.assertLessEqual(execute.call_args.kwargs['timeout'], 5)

    def test_budget_exhaustion_never_opens_a_connection(self):
        self.collector.budget.max_queries = 0
        with patch('ontology.discovery.mysql.execute_read') as execute:
            with self.assertRaises(ServiceError): self.collector.read('SELECT 1')
            execute.assert_not_called()

    def test_catalog_schema_filter_is_bound_and_missing_visibility_fails(self):
        with patch.object(self.collector, 'read', return_value=[]) as read:
            with self.assertRaises(ServiceError): self.collector.catalog()
        self.assertIn('TABLE_SCHEMA=:p0', read.call_args.args[0])
        self.assertEqual(read.call_args.args[1]['p0'], INFOS['a']['db'])
        self.assertNotIn(INFOS['a']['db'], read.call_args.args[0])

    def test_upstream_version_scope_extra_fields_and_sql_cannot_escape_candidate_area(self):
        catalog = self.obs['catalog']
        envelope = dict(protocol=PROTOCOL, upstream_sha=UPSTREAM_SHA, catalog_fingerprint=catalog['fingerprint'], tables=[dict(table='parents', terms=[dict(name='用户', description='人工待确认', attributes=[dict(source_column='id', display_name='编号')])])])
        self.assertEqual(adapt(envelope, catalog)[0]['name'], '用户')
        compiled = compile_candidates(self.obs, 'default', 'operator', 'run', envelope)
        self.assertTrue(all(e['availability'] == 'candidate' for e in compiled['entities']))
        for key, value in [('upstream_sha', 'main'), ('catalog_fingerprint', 'different'), ('sql', 'DROP TABLE parents')]:
            invalid = dict(envelope, **{key: value})
            with self.assertRaises(ValueError): adapt(invalid, catalog)
        invalid = copy.deepcopy(envelope); invalid['tables'][0]['terms'][0]['attributes'][0]['source_column'] = 'unlisted'
        with self.assertRaises(ValueError): adapt(invalid, catalog)


if __name__ == '__main__':
    unittest.main()
