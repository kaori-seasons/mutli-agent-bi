"""P1 end-to-end lifecycle/security tests using isolated local registries and P0 execution spies."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, MagicMock

from main import create_app
from server import api, conf
from server.auth import Principal
from server.errors import ServiceError
from ontology.v2 import entity, validate, migrate_v1
from ontology.registry import Registry
from ontology import runtime
from tests.fixtures import snapshot

AUTHOR = Principal('author', frozenset({'a', 'b'}), True)
REVIEWER = Principal('reviewer', frozenset({'a', 'b'}), True)
READER = Principal('reader', frozenset({'a'}))
INFOS = {s: dict(source_id=s, db='db_' + s, desc=s, host='localhost', port=3306, user='fixture', pwd='fixture') for s in ('a', 'b')}


def document(sources=None):
    sources = sources or ['a']
    entities = [entity('DataSource', 'source:' + s, s, 'owner', [s],
                       dict(engine='mysql', database=INFOS[s]['db'], credential_ref='source:' + s), availability='reference') for s in sources]
    entities += [entity('Dataset', 'dataset:orders', '订单', 'owner', sources,
                        dict(physical_name='orders', primary_key=[]), dependencies=['source:' + s for s in sources], availability='reference')]
    for name in ('city', 'amount'):
        entities.append(entity('Field', 'field:' + name, name, 'owner', sources,
                               dict(dataset_id='dataset:orders', physical_name=name, dtype='varchar' if name == 'city' else 'decimal', primary_key=False),
                               dependencies=['dataset:orders'], availability='reference'))
    merge = None if len(sources) == 1 else {'version': 1, 'sources': sources, 'disjoint_partitions': True,
                                          'dimensions': ['city'], 'measures': {'grand_total': {'op': 'sum', 'input': 'total'}}}
    entities.append(entity('QueryTemplate', 'template:sales', '城市销售额', 'owner', sources,
                           dict(sql="SELECT city, SUM(amount) AS total FROM orders WHERE city='{0}' GROUP BY city",
                                parameters=[dict(name='city', dtype='string', example='上海')], outputs=['city', 'total'],
                                column_types=['维度', '度量'], question_pattern='{0}销售额', merge=merge),
                           dependencies=['dataset:orders', 'field:city', 'field:amount'], availability='executable'))
    return dict(schema_version=2, namespace='default', entities=entities, migration={})


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'registry.sqlite3')
        self.env = patch.dict(os.environ, {'ONTOLOGY_REGISTRY_PATH': self.path, 'ONTOLOGY_REGISTRY_NAMESPACE': 'default',
                                         'ONTOLOGY_QUERY_SOURCES': '[]'}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.registry = Registry()

    def propose(self, doc=None):
        state = self.registry.active('default')
        return self.registry.propose(doc or document(), AUTHOR, state['release_id'], state['generation'], INFOS)

    def publish(self, doc=None):
        change = self.propose(doc)
        self.registry.review(change['id'], REVIEWER, change['digest'], 'approved', 'Reviewed physical mapping and business scope')
        receipt = self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)
        return change, receipt['release_id']

    def test_full_lifecycle_review_binding_and_restart(self):
        change = self.propose()
        self.assertIsNone(self.registry.active('default')['release_id'])
        with self.assertRaises(ServiceError):
            self.registry.pin('default', READER)
        for principal, hashed in ((AUTHOR, change['digest']), (REVIEWER, 'incorrect')):
            with self.assertRaises(ServiceError):
                self.registry.review(change['id'], principal, hashed, 'approved', 'checked')
        with self.assertRaises(ServiceError):
            self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)
        self.registry.review(change['id'], REVIEWER, change['digest'], 'approved', 'checked')
        release = self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)['release_id']
        self.assertEqual(Registry(self.path).pin('default', READER)['release_id'], release)
        self.assertEqual(self.registry.changeset(change['id'], AUTHOR)['review']['reviewer'], 'reviewer')
        self.assertEqual(self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)['release_id'], release)

    def test_rejection_is_terminal_and_requires_new_changeset(self):
        change = self.propose()
        self.registry.review(change['id'], REVIEWER, change['digest'], 'rejected', 'metric scope is wrong')
        with self.assertRaises(ServiceError):
            self.registry.review(change['id'], REVIEWER, change['digest'], 'approved', 'changed mind')
        with self.assertRaises(ServiceError):
            self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)

    def test_concurrent_publish_compare_and_swap(self):
        changes = [self.propose(), self.propose()]
        for change in changes:
            self.registry.review(change['id'], REVIEWER, change['digest'], 'approved', 'checked')
        def publish(change):
            try:
                return self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)['release_id']
            except ServiceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish, changes))
        self.assertEqual(results.count('REVISION_CONFLICT'), 1)
        self.assertEqual(self.registry.active('default')['generation'], 1)

    def test_failed_index_build_has_no_release_or_active_pointer(self):
        change = self.propose()
        self.registry.review(change['id'], REVIEWER, change['digest'], 'approved', 'checked')
        with patch('ontology.registry.build_index', side_effect=RuntimeError('build failed')), self.assertRaises(RuntimeError):
            self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)
        self.assertIsNone(self.registry.active('default')['release_id'])
        with self.registry.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM releases').fetchone()[0], 0)
        self.assertEqual(self.registry.changeset(change['id'], AUTHOR)['status'], 'approved')
        self.assertIsNotNone(self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)['release_id'])

    def test_rollback_preserves_versions_and_prevents_aba_approval(self):
        _, first = self.publish()
        stale = self.propose()
        newer = document()
        newer['entities'][-1]['description'] = 'updated'
        _, second = self.publish(newer)
        state = self.registry.active('default')
        self.registry.activate('default', first, AUTHOR, second, state['generation'], 'rollback')
        with self.assertRaises(ServiceError):
            self.registry.review(stale['id'], REVIEWER, stale['digest'], 'approved', 'old baseline')
        third = self.propose(newer)
        self.assertGreater(third['document']['entities'][-1]['version'], 2)
        self.assertEqual(self.registry.pin('default', READER, first)['entities']['template:sales']['version'], 1)

    def test_idempotent_publish_does_not_reactivate_superseded_release(self):
        change, first = self.publish()
        _, second = self.publish()
        receipt = self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)
        self.assertTrue(receipt['idempotent'])
        self.assertEqual(receipt['release_id'], first)
        self.assertEqual(self.registry.active('default')['release_id'], second)

    def test_unauthorized_admin_cannot_delete_hidden_sources(self):
        self.publish(document(['a', 'b']))
        partial_admin = Principal('partial', frozenset({'a'}), True)
        state = self.registry.active('default')
        with self.assertRaises(ServiceError) as error:
            self.registry.propose(document(), partial_admin, state['release_id'], state['generation'], INFOS)
        self.assertEqual(error.exception.status, 404)

    def test_drafts_and_cross_source_metadata_never_appear_in_knowledge(self):
        self.propose()
        with self.assertRaises(ServiceError):
            runtime.search(self.registry, READER, '销售额')
        _, release = self.publish(document(['a', 'b']))
        found = runtime.search(self.registry, READER, '销售额')
        self.assertEqual(found['candidates'], [])
        with self.assertRaises(ServiceError) as error:
            runtime.detail(self.registry, READER, 'template:sales', release)
        self.assertEqual(error.exception.status, 404)
        allowed = runtime.search(self.registry, AUTHOR, '销售额')
        self.assertEqual(allowed['candidates'][0]['id'], 'template:sales')
        self.assertNotIn('legacy', runtime.detail(self.registry, AUTHOR, 'template:sales')['entity'])

    def test_quarantine_blocks_pinned_history_and_rollback(self):
        _, release = self.publish()
        self.registry.quarantine('default', release, REVIEWER, 'suspected bad definition')
        for action in (lambda: self.registry.pin('default', READER, release),
                       lambda: self.registry.ensure_usable('default', release),
                       lambda: self.registry.activate('default', release, AUTHOR, release, 1, 'bad rollback')):
            with self.assertRaises(ServiceError) as error:
                action()
            self.assertEqual(error.exception.code, 'RELEASE_QUARANTINED')
        self.assertTrue(self.registry.history('default', AUTHOR)['releases'][0]['quarantined'])

    def test_corrupted_release_or_index_fails_closed(self):
        _, release = self.publish()
        with self.registry.connection(write=True) as db:
            db.execute('UPDATE releases SET search_index=? WHERE id=?', ('{}', release))
        with self.assertRaises(ServiceError) as error:
            self.registry.pin('default', READER)
        self.assertEqual(error.exception.code, 'REGISTRY_CORRUPT')

    def test_diff_contains_transitive_impact(self):
        self.publish()
        doc = document()
        doc['entities'][2]['description'] = 'column meaning changed'
        proposal = self.propose(doc)
        self.assertIn('field:city', proposal['diff']['changed'])
        self.assertIn('template:sales', proposal['diff']['impacted'])

    def test_candidate_is_not_searchable_after_snapshot_review(self):
        doc = document()
        doc['entities'].append(entity('Metric', 'metric:unknown', '未知指标', 'owner', ['a'],
                                     dict(expression='amount', field_ids=[], aggregation='', grain=[], unit='', time_definition='', filters=[])))
        self.publish(doc)
        self.assertEqual(runtime.search(self.registry, READER, '未知指标')['candidates'], [])

    def test_schema_rejects_missing_refs_cycles_unknown_fields_and_key_groups(self):
        cases = []
        doc = document(); doc['entities'][-1]['dependencies'].append('missing'); cases.append(doc)
        doc = document(); doc['entities'][0]['dependencies'] = ['template:sales']; cases.append(doc)
        doc = document(); doc['entities'][-1]['typo'] = True; cases.append(doc)
        doc = document(); doc['entities'][-1]['version'] = True; cases.append(doc)
        doc = document(); doc['entities'][2]['availability'] = 'candidate'; cases.append(doc)
        for doc in cases:
            with self.subTest(doc=doc['entities'][-1]['id']), self.assertRaises(ServiceError):
                validate(doc)
        doc = document()
        doc['entities'].append(entity('Relation', 'r', '复合关系', 'owner', ['a'],
            dict(left_dataset='dataset:orders', left_keys=['field:city','field:amount'], right_dataset='dataset:orders',
                 right_keys=['field:city'], join_type='left', cardinality='unknown'),
            dependencies=['dataset:orders','field:city','field:amount']))
        with self.assertRaises(ServiceError):
            validate(doc)

    def test_sql_binding_missing_fields_mutation_and_stale_database_are_blocked(self):
        cases = ["DELETE FROM orders", "SELECT secret AS total FROM orders", "SELECT * FROM orders",
                 "SELECT city, SUM(amount) total FROM other GROUP BY city"]
        for sql in cases:
            doc = document(); doc['entities'][-1]['attributes']['sql'] = sql
            with self.subTest(sql=sql), self.assertRaises(ServiceError):
                self.propose(doc)
        doc = document(); doc['entities'][0]['attributes']['database'] = 'other'
        with self.assertRaises(ServiceError) as error:
            self.propose(doc)
        self.assertEqual(error.exception.code, 'STALE_BINDING')

    def test_v1_migration_retains_entire_original_and_blocks_unknown_semantics(self):
        old = snapshot(Path(self.tmp.name) / 'fixture.xlsx').seal()
        migrated = migrate_v1(old, 'default', {'default': 'a'}, 'owner')
        self.assertEqual(migrated['migration']['source_snapshot'], old.to_dict())
        self.assertEqual({e['id'] for e in migrated['entities']}, {e.id for e in old.entities()})
        for e in migrated['entities']:
            self.assertEqual(e['legacy'], next(x for x in old.entities() if x.id == e['id']).to_dict())
        blocked = [e for e in migrated['entities'] if e['kind'] in ('Metric', 'Relation', 'QueryTemplate')]
        self.assertTrue(all(e['availability'] == 'candidate' for e in blocked))
        self.assertTrue(any(e['attributes']['primary_key'] for e in migrated['entities'] if e['kind'] == 'Field'))
        self.publish(migrated)
        with self.assertRaises(ServiceError):
            migrate_v1(old, 'default', {}, 'owner')

    def test_audit_and_versions_survive_reopening(self):
        _, release = self.publish()
        other = Registry(self.path)
        with other.connection() as db:
            events = [row[0] for row in db.execute('SELECT action FROM audit ORDER BY seq')]
        self.assertEqual(events, ['proposed', 'approved', 'published'])
        self.assertEqual(other.active('default')['release_id'], release)

    def query(self, question='上海销售额', options=None, principal=READER, ids=None):
        with patch.dict(conf.mysql_info, INFOS, clear=True), patch.dict(conf.template_info, {}, clear=True):
            return api.get_result([{'role': 'user', 'content': question}], 'trace', principal=principal,
                                  source_ids=ids or ['a'], ontology_options=options)

    def test_query_published_template_uses_p0_gate_without_llm(self):
        _, release = self.publish()
        with patch.dict(os.environ, {'ONTOLOGY_QUERY_SOURCES': '["a"]'}), \
                patch.object(api.aws, 'get') as aws, patch.object(api.llm, 'query') as llm, \
                patch('server.db.mysql.execute_read', return_value={'rows': [('上海', 100)], 'columns': ['city', 'total'],
                                                                   'row_count': 1, 'truncated': False}) as execute:
            output = self.query()
        aws.assert_not_called(); llm.assert_not_called()
        self.assertEqual(execute.call_args.args[1].parameters, {'p0': '上海'})
        self.assertEqual(output['ontology']['release_id'], release)
        self.assertEqual(output['ontology']['events'], ['retrieved', 'selected', 'executed'])
        self.assertIn('100', output['mdData'])
        self.assertEqual(output['chartType'], 'BarChartPic')

    def test_query_no_release_no_match_and_ambiguity_never_fall_back(self):
        with patch.dict(os.environ, {'ONTOLOGY_QUERY_SOURCES': '["a"]'}), patch.object(api, 'answer') as answer:
            with self.assertRaises(ServiceError) as error:
                self.query()
            self.assertEqual(error.exception.code, 'RELEASE_NOT_READY')
            self.publish()
            with self.assertRaises(ServiceError) as error:
                self.query('unknown')
            self.assertEqual(error.exception.code, 'UNANSWERABLE')
            doc = document()
            alias = copy.deepcopy(doc['entities'][-1]); alias['id'] = 'template:duplicate'
            doc['entities'].append(alias)
            self.publish(doc)
            with self.assertRaises(ServiceError) as error:
                self.query()
            self.assertEqual(error.exception.code, 'AMBIGUOUS_DEFINITION')
            answer.assert_not_called()

    def test_query_explicit_binding_preserves_injection_string_and_version(self):
        _, first = self.publish()
        self.publish()
        value = "A' OR 1=1 -- Mixed CASE"
        with patch.dict(os.environ, {'ONTOLOGY_QUERY_SOURCES': '["a"]'}), \
                patch('server.db.mysql.execute_read', return_value={'rows': [], 'columns': ['city', 'total'], 'row_count': 0}) as execute:
            output = self.query(options={'template_id': 'template:sales', 'parameters': [value], 'release_id': first})
        self.assertEqual(execute.call_args.args[1].parameters, {'p0': value})
        self.assertNotIn(value, output['sql'])
        self.assertEqual(output['ontology']['release_id'], first)

    def test_query_pinned_release_does_not_switch_during_execution(self):
        _, first = self.publish()
        def run(*args, **kwargs):
            self.publish()
            return {'rows': [], 'columns': ['city', 'total'], 'row_count': 0}
        with patch.dict(os.environ, {'ONTOLOGY_QUERY_SOURCES': '["a"]'}), patch('server.db.mysql.execute_read', side_effect=run):
            output = self.query()
        self.assertEqual(output['ontology']['release_id'], first)
        self.assertNotEqual(self.registry.active('default')['release_id'], first)

    def test_query_quarantine_while_running_suppresses_result(self):
        _, release = self.publish()
        def run(*args, **kwargs):
            self.registry.quarantine('default', release, REVIEWER, 'invalid metric')
            return {'rows': [('sensitive', 100)], 'columns': ['city', 'total'], 'row_count': 1}
        with patch.dict(os.environ, {'ONTOLOGY_QUERY_SOURCES': '["a"]'}), \
                patch('server.db.mysql.execute_read', side_effect=run), self.assertRaises(ServiceError) as error:
            self.query()
        self.assertEqual(error.exception.code, 'RELEASE_QUARANTINED')

    def test_query_enforces_source_scope_and_runtime_binding(self):
        self.publish()
        with patch.dict(os.environ, {'ONTOLOGY_QUERY_SOURCES': '["a"]'}):
            with self.assertRaises(ServiceError):
                self.query(ids=['a', 'b'], principal=AUTHOR)
            with patch.dict(INFOS['a'], {'db': 'rebound_db'}), self.assertRaises(ServiceError) as error:
                self.query()
            self.assertEqual(error.exception.code, 'STALE_BINDING')
            with self.assertRaises(ServiceError):
                self.query(principal=Principal('blocked', frozenset()))

    def test_query_multisource_retains_p0_algebra_and_failure_semantics(self):
        self.publish(document(['a', 'b']))
        def run(info, *args, **kwargs):
            return {'rows': [('上海', 10 if info['source_id'] == 'a' else 20)], 'columns': ['city', 'total'], 'row_count': 1}
        with patch.dict(os.environ, {'ONTOLOGY_QUERY_SOURCES': '["a","b"]'}), patch('server.db.mysql.execute_read', side_effect=run):
            output = self.query(ids=['a', 'b'], principal=AUTHOR)
        self.assertIn('30', output['mdData'])
        def partial(info, *args, **kwargs):
            if info['source_id'] == 'b':
                raise OSError('failed')
            return run(info)
        with patch.dict(os.environ, {'ONTOLOGY_QUERY_SOURCES': '["a","b"]'}), \
                patch('server.db.mysql.execute_read', side_effect=partial), self.assertRaises(ServiceError) as error:
            self.query(ids=['a', 'b'], principal=AUTHOR)
        self.assertEqual(error.exception.code, 'PARTIAL_RESULT')

    def test_query_disabled_does_not_open_registry_or_change_legacy_response(self):
        generated = {'bedrockSQL': 'SELECT 1 AS n', 'column_type': ['度量']}
        with patch('ontology.runtime.Registry', side_effect=AssertionError('must stay disabled')), \
                patch.object(api.aws, 'get'), patch.object(api, 'answer_template_sql', return_value=generated), \
                patch('server.db.mysql.execute_read', return_value={'rows': [(1,)], 'columns': ['n'], 'row_count': 1}):
            output = self.query()
        self.assertNotIn('ontology', output)
        self.assertIn('1', output['mdData'])

    def test_query_metadata_mismatch_blocks_result(self):
        self.publish()
        with patch.dict(os.environ, {'ONTOLOGY_QUERY_SOURCES': '["a"]'}), \
                patch('server.db.mysql.execute_read', return_value={'rows': [], 'columns': ['wrong'], 'row_count': 0}), \
                self.assertRaises(ServiceError) as error:
            self.query()
        self.assertEqual(error.exception.code, 'STALE_BINDING')

    def test_slot_matching_is_bounded_and_preserves_case(self):
        template = document()['entities'][-1]
        self.assertEqual(runtime.match_parameters('New York销售额', template), [['New York']])
        template['attributes']['question_pattern'] = '{0}/{0}'
        self.assertEqual(runtime.match_parameters('a/b', template), [])
        self.assertEqual(runtime.match_parameters('a/a', template), [['a']])

    def test_http_admin_identity_and_readonly_scope(self):
        identities = {'Bearer author': AUTHOR, 'Bearer reviewer': REVIEWER, 'Bearer reader': READER}
        def verifier(header):
            if header not in identities:
                raise ServiceError('AUTH_REQUIRED', 'login', 401)
            return identities[header]
        client = create_app(verifier=verifier).test_client()
        with patch.dict(conf.mysql_info, INFOS, clear=True):
            payload = {'document': document(), 'expected_active': None, 'expected_generation': 0}
            self.assertEqual(client.post('/v1/ontology/changesets', json=payload).status_code, 401)
            self.assertEqual(client.post('/v1/ontology/changesets', json=payload, headers={'Authorization': 'Bearer reader'}).status_code, 403)
            response = client.post('/v1/ontology/changesets', json=payload, headers={'Authorization': 'Bearer author'})
            self.assertEqual(response.status_code, 201)
            change = response.json
            review_payload = {'expected_digest': change['digest'], 'decision': 'approved', 'reason': 'checked'}
            self.assertEqual(client.post('/v1/ontology/changesets/' + change['id'] + '/review', json=review_payload,
                                         headers={'Authorization': 'Bearer author'}).status_code, 403)
            self.assertEqual(client.post('/v1/ontology/changesets/' + change['id'] + '/review', json=review_payload,
                                         headers={'Authorization': 'Bearer reviewer'}).status_code, 200)
            response = client.post('/v1/ontology/releases', json={'changeset_id': change['id'], 'expected_digest': change['digest']},
                                   headers={'Authorization': 'Bearer author'})
            self.assertEqual(response.status_code, 200)
            response = client.post('/v1/knowledge/search', json={'question': '城市销售额'}, headers={'Authorization': 'Bearer reader'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json['candidates']), 1)


    def test_http_query_real_service_initialization_does_not_call_models(self):
        self.publish()
        client = create_app(verifier=lambda _: READER).test_client()
        with patch.dict(conf.mysql_info, INFOS, clear=True), patch.dict(os.environ, {'ONTOLOGY_QUERY_SOURCES': '["a"]'}), \
                patch.object(api.llm, 'query', side_effect=AssertionError('LLM must not initialize')), \
                patch.object(api.aws, 'get', side_effect=AssertionError('AWS must not initialize')), \
                patch('server.db.mysql.execute_read', return_value={'rows': [], 'columns':['city','total'], 'row_count':0}):
            response = client.post('/queryllm', json={'messages':[{'role':'user','content':'上海销售额'}], 'source_ids':['a']})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIn('ontology', response.json)


    def test_pinned_export_cannot_outlive_release_quarantine(self):
        from server.exports import save_export, read_export
        _, release = self.publish()
        with patch.dict(os.environ, {'QUERY_EXPORT_PATH': str(Path(self.tmp.name) / 'exports')}):
            url = save_export(READER, ['a'], ['total'], [(1,)], release={'namespace': 'default', 'release_id': release})
            identity = url.rsplit('/', 1)[1]
            self.assertIn(b'total', read_export(READER, identity))
            self.registry.quarantine('default', release, REVIEWER, 'wrong definition')
            with self.assertRaises(ServiceError) as error:
                read_export(READER, identity)
            self.assertEqual(error.exception.code, 'RELEASE_QUARANTINED')

    def test_failure_after_pointer_update_rolls_back_everything(self):
        _, old = self.publish()
        change = self.propose()
        self.registry.review(change['id'], REVIEWER, change['digest'], 'approved', 'checked')
        with patch.object(self.registry, 'audit_in', side_effect=OSError('disk failure')), self.assertRaises(OSError):
            self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)
        self.assertEqual(self.registry.active('default'), {'release_id': old, 'generation': 1})
        self.assertEqual(self.registry.changeset(change['id'], AUTHOR)['status'], 'approved')
        with self.registry.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM releases').fetchone()[0], 1)

    def test_file_migration_adapter_round_trip_and_no_automatic_publication(self):
        from ontology.registry_cli import main
        from contextlib import redirect_stdout
        import io
        original = snapshot(Path(self.tmp.name) / 'source.xlsx').seal()
        input_path = Path(self.tmp.name) / 'v1.json'
        output_path = Path(self.tmp.name) / 'v2.json'
        archive_path = Path(self.tmp.name) / 'archive.json'
        input_path.write_text(original.to_json())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['migrate-v1', '--input', str(input_path), '--output', str(output_path),
                                   '--source-map', '{"default":"a"}', '--owner', 'owner']), 0)
            self.assertEqual(main(['validate', '--input', str(output_path)]), 0)
            self.assertEqual(main(['export-v1-archive', '--input', str(output_path), '--output', str(archive_path)]), 0)
        self.assertEqual(json.loads(archive_path.read_text()), original.to_dict())
        self.assertIsNone(self.registry.active('default')['release_id'])

    def test_sqlite_consistent_backup_can_restore_registry(self):
        _, release = self.publish()
        target = str(Path(self.tmp.name) / 'backup.sqlite3')
        with self.registry.connection() as source:
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                destination.close()
        restored = Registry(target)
        self.assertEqual(restored.pin('default', READER)['release_id'], release)
        self.assertEqual(len(restored.audit('default', AUTHOR)['events']), 3)

    def test_composite_key_arrays_remain_one_relation(self):
        doc = document()
        for e in doc['entities']:
            if e['kind'] == 'Field':
                e['attributes']['primary_key'] = True
            if e['kind'] == 'Dataset':
                e['attributes']['primary_key'] = ['field:city', 'field:amount']
        doc['entities'].append(entity('Relation', 'relation:composite', '完整复合键', 'owner', ['a'],
            dict(left_dataset='dataset:orders', left_keys=['field:city', 'field:amount'], right_dataset='dataset:orders',
                 right_keys=['field:city', 'field:amount'], join_type='left', cardinality='one-to-one'),
            dependencies=['dataset:orders', 'field:city', 'field:amount'], availability='reference'))
        self.publish(doc)
        relation = runtime.detail(self.registry, READER, 'relation:composite')['entity']
        self.assertEqual(relation['attributes']['left_keys'], ['field:city', 'field:amount'])
        self.assertEqual(relation['attributes']['right_keys'], ['field:city', 'field:amount'])


if __name__ == '__main__':
    unittest.main()
