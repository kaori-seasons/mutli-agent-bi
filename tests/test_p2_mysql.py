"""Opt-in P2 tests on a disposable MySQL 8 container. Never uses application .env."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import pymysql

from ontology.discovery.mysql import Collector
from ontology.discovery.policy import load_policy
from ontology.discovery import workflow
from ontology.registry import Registry
from server.errors import ServiceError
from tests.test_registry_v2 import AUTHOR, REVIEWER, READER


@unittest.skipUnless(os.getenv('P0_MYSQL_TEST_PORT') and os.getenv('P0_MYSQL_TEST_ALLOW_CREATE') == '1', 'disposable MySQL fixture not enabled')
class DiscoveryMySQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        suffix = uuid4().hex[:10]
        cls.database, cls.user = 'p2_' + suffix, 'p2_reader_' + suffix
        cls.admin = pymysql.connect(host='127.0.0.1', port=int(os.environ['P0_MYSQL_TEST_PORT']), user='root', password=os.environ['P0_MYSQL_TEST_PASSWORD'], autocommit=True)
        cls.addClassCleanup(cls.cleanup)
        with cls.admin.cursor() as c:
            c.execute(f'CREATE DATABASE {cls.database}')
            c.execute(f"CREATE USER '{cls.user}'@'%' IDENTIFIED BY 'p2-synthetic-reader'")
            c.execute(f"GRANT SELECT ON {cls.database}.* TO '{cls.user}'@'%'")
            c.execute(f'USE {cls.database}')
            c.execute('CREATE TABLE parents (tenant_id INT, id INT, PRIMARY KEY(tenant_id,id))')
            c.execute('INSERT INTO parents VALUES (1,10),(1,20),(2,10)')
            c.execute('CREATE TABLE children (seq INT PRIMARY KEY, tenant_id INT, parent_id INT, email VARCHAR(100), FOREIGN KEY (tenant_id,parent_id) REFERENCES parents(tenant_id,id))')
            c.execute("INSERT INTO children VALUES (1,1,10,'never-export@example.invalid'),(2,1,10,'private@example.invalid'),(3,2,10,NULL),(4,NULL,10,'sensitive')")
            c.execute('CREATE TABLE orphans (seq INT PRIMARY KEY, tenant_id INT, parent_id INT)')
            c.execute('INSERT INTO orphans VALUES (1,1,10),(2,3,10),(3,NULL,10),(4,1,20)')
            c.execute('CREATE TABLE duplicates (tenant_id INT, id INT)')
            c.execute('INSERT INTO duplicates VALUES (1,10),(1,10),(1,20),(2,10)')
            c.execute('CREATE TABLE empty_rows (seq INT PRIMARY KEY, tenant_id INT, parent_id INT)')
            c.execute('CREATE VIEW forbidden_view AS SELECT * FROM parents')
        cls.info = dict(source_id='a', host='127.0.0.1', port=int(os.environ['P0_MYSQL_TEST_PORT']), user=cls.user, pwd='p2-synthetic-reader', db=cls.database)

    @classmethod
    def cleanup(cls):
        try:
            with cls.admin.cursor() as c:
                c.execute(f'DROP DATABASE IF EXISTS {cls.database}')
                c.execute(f"DROP USER IF EXISTS '{cls.user}'@'%'")
        finally:
            cls.admin.close()

    def setUp(self):
        config = {'sample_rows': 10, 'tables': {}, 'relations': []}
        for name, columns in [('parents', ['tenant_id', 'id']), ('children', ['seq', 'tenant_id', 'parent_id', 'email']), ('orphans', ['seq', 'tenant_id', 'parent_id']), ('duplicates', ['tenant_id', 'id']), ('empty_rows', ['seq', 'tenant_id', 'parent_id'])]:
            config['tables'][name] = dict(columns=columns, profile_columns=columns, sensitive_columns=[])
        for left, right in [('orphans', 'parents'), ('children', 'duplicates'), ('empty_rows', 'parents')]:
            config['relations'].append(dict(left_table=left, left_keys=['tenant_id', 'parent_id'], right_table=right, right_keys=['tenant_id', 'id']))
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, ONTOLOGY_DISCOVERY_ENABLED='true', ONTOLOGY_DISCOVERY_POLICY=json.dumps({'a': config}), ONTOLOGY_REGISTRY_PATH=str(Path(tmp.name) / 'registry.sqlite3'))
        env.start(); self.addCleanup(env.stop)
        self.policy = load_policy('a'); self.collector = Collector(self.info, self.policy)

    def test_catalog_declared_composite_fk_and_pk_are_not_split(self):
        catalog = self.collector.catalog()
        declared = next(r for r in catalog['relations'] if r['origin'] == 'declared')
        self.assertEqual(declared['left_keys'], ['tenant_id', 'parent_id'])
        self.assertEqual(declared['right_keys'], ['tenant_id', 'id'])
        self.assertEqual(next(t for t in catalog['tables'] if t['name'] == 'parents')['primary_key'], ['tenant_id', 'id'])
        self.assertEqual(catalog['fingerprint'], self.collector.catalog()['fingerprint'])

    def test_real_profile_has_counts_but_never_sensitive_values(self):
        table = next(t for t in self.collector.catalog()['tables'] if t['name'] == 'children')
        result = self.collector.profile(table)
        self.assertEqual(result['sampled_rows'], 4)
        self.assertEqual(result['columns']['tenant_id']['null_fraction'], .25)
        self.assertEqual(result['columns']['parent_id']['distinct_count'], 1)
        self.assertEqual(result['excluded'], {'email': 'sensitive'})
        self.assertNotIn('example.invalid', json.dumps(result))

    def test_real_relation_membership_orphans_nulls_and_join_fanout(self):
        catalog = self.collector.catalog(); tables = {t['name']: t for t in catalog['tables']}
        evidence = {(r['left_table'], r['right_table']): self.collector.relation(r, tables) for r in catalog['relations']}
        declared = evidence[('children', 'parents')]
        self.assertEqual(declared['validation'], 'sampled_pass')
        self.assertEqual(declared['nonnull_count'], 3)
        self.assertEqual(declared['fanout_ratio'], 1)
        orphan = evidence[('orphans', 'parents')]
        self.assertEqual(orphan['nonnull_count'], 3)
        self.assertEqual(orphan['orphan_count'], 1)
        self.assertAlmostEqual(orphan['orphan_fraction'], 1 / 3)
        duplicate = evidence[('children', 'duplicates')]
        self.assertEqual(duplicate['validation'], 'sampled_fail')
        self.assertEqual(duplicate['target_uniqueness']['duplicate_groups'], 1)
        self.assertAlmostEqual(duplicate['fanout_ratio'], 5 / 3)
        self.assertEqual(evidence[('empty_rows', 'parents')]['validation'], 'unknown')

    def test_real_full_collection_is_candidate_only_and_source_credentials_never_persist(self):
        registry = Registry()
        job = workflow.run(registry, 'default', AUTHOR, {'a': self.info}, 'a', 'real-collection')
        self.assertEqual(job['state'], 'complete', job)
        self.assertTrue(all(e['availability'] == 'candidate' for e in job['artifact']['document']['entities']))
        self.assertEqual(registry.pending('default', AUTHOR)['changesets'], [])
        self.assertNotIn('p2-synthetic-reader', json.dumps(job))
        self.assertNotIn('example.invalid', json.dumps(job))

    def test_real_handoff_review_publish_preserves_machine_candidates(self):
        registry = Registry(); infos = {'a': self.info}
        job = workflow.run(registry, 'default', AUTHOR, infos, 'a', 'real-handoff')
        relation = next(e for e in job['artifact']['document']['entities'] if e['kind'] == 'Relation' and e['legacy']['origin'] == 'declared')
        edits = {relation['id']: {'attributes': dict(relation['attributes'], cardinality='many-to-one', join_type='left')}}
        change = workflow.prepare_review(registry, 'default', job['id'], AUTHOR, infos, job['digest'], [relation['id']], edits, None, 0, '核对租户隔离和父子业务角色')
        workflow.check_current_review(registry, change['id'], REVIEWER, infos)
        registry.review(change['id'], REVIEWER, change['digest'], 'approved', '独立核验复合键与连接方向')
        registry.publish(change['id'], AUTHOR, change['digest'], infos)
        self.assertEqual(registry.pin('default', READER)['entities'][relation['id']]['availability'], 'reference')
        self.assertEqual(next(e for e in workflow.get_run(registry, 'default', job['id'], AUTHOR)['artifact']['document']['entities'] if e['id'] == relation['id'])['availability'], 'candidate')

    def test_schema_drift_blocks_handoff_and_view_is_rejected(self):
        registry = Registry(); infos = {'a': self.info}
        job = workflow.run(registry, 'default', AUTHOR, infos, 'a', 'drift')
        with self.admin.cursor() as c:
            c.execute(f"ALTER TABLE {self.database}.parents COMMENT='drift fixture'")
        try:
            with self.assertRaises(ServiceError) as error:
                workflow.assert_current(job['artifact'], self.info)
            self.assertEqual(error.exception.code, 'STALE_BINDING')
        finally:
            with self.admin.cursor() as c: c.execute(f"ALTER TABLE {self.database}.parents COMMENT=''")
        self.policy['tables'] = {'forbidden_view': dict(columns=['tenant_id', 'id'], profile_columns=[], sensitive_columns=[])}
        with self.assertRaises(ServiceError) as error: Collector(self.info, self.policy).catalog()
        self.assertEqual(error.exception.code, 'CATALOG_SCOPE_INVALID')


if __name__ == '__main__':
    unittest.main()
