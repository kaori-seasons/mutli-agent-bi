"""Governance isolation, reviewed remediation, quality and shared-budget contracts."""
import copy
import json
import os
import tempfile
import time
from pathlib import Path
from decimal import Decimal, localcontext
import unittest
from unittest.mock import patch

from main import create_app
from ontology.registry import Registry
from ontology.v2 import canonical, entity
from ontology.discovery.mysql import Collector
from ontology.governance import runtime as governance, rules
from ontology.analysis import runtime as analysis
from ontology.planner import runtime as planner
from server.auth import Principal
from server.errors import ServiceError
from server.execution import QueryBudget
from server import conf
from server.exports import save_export, read_export
from tests import planner_fixtures as fixture
from tests.test_registry_v2 import AUTHOR, REVIEWER, READER, INFOS


class P4Fixture:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, ONTOLOGY_REGISTRY_PATH=str(Path(tmp.name)/'registry.sqlite3'), QUERY_EXPORT_PATH=str(Path(tmp.name)/'exports'),
                         ONTOLOGY_REGISTRY_NAMESPACE='default', ONTOLOGY_PLANNER_SOURCES='["a"]', ONTOLOGY_PLANNER_ROLLOUT_PERCENT='100',
                         ONTOLOGY_GOVERNANCE_SOURCES='["a"]', ONTOLOGY_ANALYSIS_ENABLED='true', ANALYSIS_MAX_SQL_CALLS='32')
        env.start(); self.addCleanup(env.stop)
        config = patch.dict(conf.mysql_info, INFOS, clear=True); config.start(); self.addCleanup(config.stop)
        self.registry = Registry(); self.doc = fixture.document()
        self.physical_doc = copy.deepcopy(self.doc)
        def catalog(collector, **kwargs):
            result = fixture.catalog(collector, self.physical_doc)
            if kwargs.get('tolerate_missing'):
                for table in result['tables']: table['kind'] = 'BASE TABLE'
            # Model the real collector's four fixed probes, sharing the same budget.
            for _ in range(4): collector.budget.consume()
            return result
        mock = patch.object(Collector, 'catalog', autospec=True, side_effect=catalog)
        self.catalog = mock.start(); self.addCleanup(mock.stop)
        def execute(info, query, budget):
            budget.consume()
            grouped = 'GROUP BY' in query.sql
            return dict(status='success', columns=['d0','m0'] if grouped else ['m0'], rows=[['east', Decimal('120')],['south',Decimal('30')],[None,Decimal('10')]] if grouped else [[Decimal('160')]], row_count=3 if grouped else 1, truncated=False)
        mock = patch('ontology.planner.runtime.execute_source', side_effect=execute)
        self.execute = mock.start(); self.addCleanup(mock.stop)
        self.release = self.publish(self.doc)

    def publish(self, document):
        state = self.registry.active('default')
        change = self.registry.propose(document, AUTHOR, state['release_id'], state['generation'], INFOS)
        self.registry.review(change['id'], REVIEWER, change['digest'], 'approved', 'P4 independent fixture review')
        return self.registry.publish(change['id'], AUTHOR, change['digest'], INFOS)['release_id']

    def code(self, expected, fn):
        with self.assertRaises(ServiceError) as error: fn()
        self.assertEqual(error.exception.code, expected)

    def scan(self, key='scan'): return governance.scan(self.registry, AUTHOR, INFOS, 'a', key)
    def issue(self): return governance.incidents(self.registry, AUTHOR)['incidents'][0]
    def plan(self): return planner.create_plan(self.registry, READER, INFOS, fixture.intent())
    def task(self, key='task', **kwargs): return analysis.run(self.registry, READER, INFOS, fixture.intent(), key, **kwargs)
    def quality(self, field='amount', check='not_null'):
        identity = 'field:orders:' + field
        return entity('QualityRule', 'quality:fixture', 'approved fixture rule', 'quality-owner', ['a'],
                      {'target_ids':[identity], 'definition':canonical({'version':1,'check':check,'field_ids':[identity]})}, dependencies=[identity], availability='reference')


class GovernanceTests(P4Fixture, unittest.TestCase):
    def test_defaults_are_opt_in_and_rollout_stays_zero(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(governance.enabled_sources(), set())
            self.assertEqual(planner.configuration(), (set(), 0))
            self.code('ANALYSIS_DISABLED', lambda: analysis.enabled())
        self.assertIn('ONTOLOGY_PLANNER_ROLLOUT_PERCENT=0', Path('.env_template').read_text())

    def test_scan_is_immutable_idempotent_and_never_publishes_candidates(self):
        first = self.scan(); second = self.scan()
        self.assertEqual(first, second); self.assertEqual(first['state'], 'complete')
        self.assertEqual(first['evidence']['sql_calls'], 8)
        self.assertEqual(self.registry.active('default')['release_id'], self.release)
        self.assertEqual(governance.incidents(self.registry, AUTHOR)['incidents'], [])
        self.code('FORBIDDEN', lambda: governance.scan(self.registry, READER, INFOS, 'a', 'reader'))
        self.code('NOT_FOUND', lambda: governance.get_run(self.registry, Principal('limited', frozenset(), True), first['id']))

    def test_drift_blocks_cached_receipts_download_and_physical_aliases(self):
        plan = self.plan(); receipt = planner.execute_plan(self.registry, READER, INFOS, plan['plan_id'], 'before')
        self.assertEqual(receipt['state'], 'succeeded')
        export = save_export(READER, ['a'], ['value'], [[160]], release={'namespace':'default', 'release_id':self.release, 'execution_id':receipt['execution_id']})
        next(e for e in self.physical_doc['entities'] if e['id']=='field:orders:amount')['attributes']['dtype']='decimal(14,2)'
        run = self.scan(); self.assertEqual(run['evidence']['findings'][0]['kind'], 'column_type_changed')
        issue = self.issue(); self.assertEqual(issue['owner'], 'fixture-owner'); self.assertIn('metric:sales', issue['payload']['impacted'])
        self.code('GOVERNANCE_BLOCKED', lambda: planner.get_execution(self.registry, READER, receipt['execution_id'], INFOS))
        self.code('GOVERNANCE_BLOCKED', lambda: read_export(READER, export.rsplit('/',1)[-1]))
        with patch.dict(os.environ, ONTOLOGY_GOVERNANCE_SOURCES='[]'):
            self.code('GOVERNANCE_BLOCKED', lambda: planner.get_plan(self.registry, READER, plan['plan_id']))
        # A new logical ID for the same physical column still encounters the incident.
        alias = copy.deepcopy(next(e for e in self.doc['entities'] if e['id']=='field:orders:amount')); alias['id']='field:alias'
        document = copy.deepcopy(self.doc); document['entities'].append(alias)
        with self.registry.connection() as db:
            self.code('GOVERNANCE_BLOCKED', lambda: governance.guard_in(db, 'default', document, ['field:alias']))
        governance.guard(self.registry, 'default', self.release, ['field:customers:region'])

    def test_resolution_requires_owner_cas_new_approved_release_and_fresh_reverification(self):
        next(e for e in self.physical_doc['entities'] if e['id']=='field:orders:amount')['attributes']['dtype']='decimal(14,2)'
        failed = self.scan(); issue=self.issue()
        args = (self.registry, AUTHOR, INFOS, issue['id'])
        self.code('REVIEW_REQUIRED', lambda: governance.transition(*args, 1, 'resolve', 'skip', run_id=failed['id']))
        governance.transition(*args, 1, 'acknowledge', 'investigating', owner='data-owner')
        self.code('REVISION_CONFLICT', lambda: governance.transition(*args, 1, 'acknowledge', 'stale', owner='other'))
        self.physical_doc = copy.deepcopy(self.doc)
        old_release_recheck = self.scan('old-recheck')
        self.code('REVERIFY_REQUIRED', lambda: governance.transition(*args, 2, 'resolve', 'old version', run_id=old_release_recheck['id']))
        self.doc['entities'][0]['description']='reviewed remediation record'
        self.publish(self.doc)
        clean = self.scan('new-recheck')
        resolved = governance.transition(*args, 2, 'resolve', 'schema restored and approved', run_id=clean['id'])
        self.assertEqual(resolved['state'], 'resolved'); self.plan()
        self.assertTrue(any(e['action']=='governance_resolved' for e in self.registry.audit('default', AUTHOR)['events']))

    def test_failed_probe_is_unknown_and_isolates_instead_of_green(self):
        with patch.object(Collector, 'catalog', side_effect=RuntimeError('secret driver details')):
            run=self.scan()
        self.assertEqual(run['state'], 'failed'); self.assertNotIn('secret', canonical(run))
        self.assertEqual(self.issue()['payload']['kind'], 'probe_unknown')
        self.code('GOVERNANCE_BLOCKED', lambda: governance.guard(self.registry, 'default', self.release, ['metric:count']))

    def test_first_scan_checks_approved_primary_key_without_trusting_a_baseline(self):
        collector = Collector(INFOS['a'], {'tables': {'orders': {'columns':['tenant_id','id']}}})
        catalog = fixture.catalog(collector); catalog['tables'][0]['primary_key']=[]
        findings, _ = governance.inspect_catalog({e['id']:e for e in self.doc['entities']}, catalog)
        self.assertTrue(any(f['kind']=='primary_key_changed' for f in findings))

    def test_quality_rules_are_closed_and_unknown_is_not_passed(self):
        rule = self.quality(); entities={e['id']:e for e in self.doc['entities']}
        _, query=rules.parse(rule, entities); self.assertIn('IS NULL', query.sql)
        rule['attributes']['definition']='SELECT secret FROM users'
        self.code('SEMANTIC_VALIDATION_FAILED', lambda: rules.parse(rule, entities))
        with patch('ontology.governance.rules.execute_source', return_value={'status':'success','columns':['violations'],'rows':[[0]],'truncated':True}):
            self.code('QUALITY_UNKNOWN', lambda: rules.evaluate(self.quality(),entities,INFOS['a'],QueryBudget()))

    def test_quality_failure_blocks_plan_execution_and_opens_owned_incident(self):
        self.doc['entities'].append(self.quality()); self.publish(self.doc)
        with patch('ontology.governance.rules.execute_source', return_value={'status':'success','columns':['violations'],'rows':[[1]],'truncated':False}):
            plan=self.plan(); result=planner.execute_plan(self.registry,READER,INFOS,plan['plan_id'],'quality')
            self.assertEqual(result['error_code'],'QUALITY_FAILED'); self.execute.assert_not_called()
            self.scan()
        self.assertEqual(self.issue()['payload']['kind'],'quality_failed'); self.assertEqual(self.issue()['owner'],'quality-owner')

    def test_http_admin_separation_and_strict_payloads(self):
        reader=create_app(verifier=lambda _: READER).test_client()
        self.assertEqual(reader.post('/v1/ontology/governance/runs',json={'source_id':'a','idempotency_key':'http'}).status_code,403)
        self.assertEqual(reader.post('/v1/analysis/tasks',json={'intent':fixture.intent(),'idempotency_key':'http','principal':'admin'}).status_code,422)
        self.assertEqual(reader.post('/v1/governance/status',json={'identity':'missing'}).status_code,404)
        self.assertEqual(reader.post('/v1/governance/status',json={'identity':'metric:sales'}).json['blocked'],False)
        unauth=create_app().test_client()
        self.assertEqual(unauth.post('/v1/analysis/tasks',json={}).status_code,401)

    def test_corrupt_evidence_and_expired_running_lease_fail_closed(self):
        run=self.scan()
        with self.registry.connection(write=True) as db: db.execute("UPDATE governance_runs SET payload='{}' WHERE id=?",(run['id'],))
        self.code('GOVERNANCE_CORRUPT',lambda:governance.get_run(self.registry,AUTHOR,run['id']))

    def test_expired_recheck_and_source_rebinding_cannot_resolve_incident(self):
        next(e for e in self.physical_doc['entities'] if e['id']=='field:orders:amount')['attributes']['dtype']='decimal(14,2)'
        self.scan(); issue=self.issue(); args=(self.registry,AUTHOR,INFOS,issue['id'])
        governance.transition(*args,1,'acknowledge','repair',owner='owner')
        self.physical_doc=copy.deepcopy(self.doc);self.doc['entities'][0]['description']='new review';self.publish(self.doc)
        run=self.scan('clean')
        rebound=copy.deepcopy(INFOS);rebound['a']['host']='another-host'
        self.code('REVERIFY_REQUIRED',lambda:governance.transition(self.registry,AUTHOR,rebound,issue['id'],2,'resolve','rebound',run_id=run['id']))
        with patch('ontology.governance.runtime.time.time',return_value=time.time()+301):
            self.code('REVERIFY_REQUIRED',lambda:governance.transition(*args,2,'resolve','expired',run_id=run['id']))

    def test_governance_store_capacity_and_running_scan_limit(self):
        self.scan()
        with self.registry.connection(write=True) as db:
            db.execute("UPDATE governance_runs SET state='running',lease_until=?",(time.time()+90,))
        self.code('GOVERNANCE_BUSY',lambda:self.scan('parallel'))
        with self.registry.connection(write=True) as db:
            db.execute("UPDATE governance_runs SET lease_until=0")
        result=self.scan('after-interruption');self.assertEqual(result['state'],'complete')

    def test_oversized_finding_set_is_recorded_as_blocking_unknown(self):
        findings=[{'kind':'many_changes_'+str(i),'targets':['field:orders:amount']} for i in range(33)]
        with patch('ontology.governance.runtime.inspect_catalog',return_value=(findings,[])):
            result=self.scan()
        self.assertEqual(result['state'],'failed');self.assertEqual(result['evidence']['error_code'],'GOVERNANCE_LIMIT')
        self.assertEqual(self.issue()['payload']['kind'],'evidence_limit')
        self.code('GOVERNANCE_BLOCKED',lambda:governance.guard(self.registry,'default',self.release,['metric:count']))

    def test_p1_template_guard_and_legacy_exports_observe_scoped_incidents(self):
        from ontology.runtime import guard
        next(e for e in self.physical_doc['entities'] if e['id']=='field:orders:amount')['attributes']['dtype']='decimal(14,2)'
        export=save_export(READER,['a'],['value'],[[1]],release={'namespace':'default','release_id':self.release,'dependency_ids':['field:orders:amount']})
        self.scan()
        context={'registry':self.registry,'namespace':'default','release_id':self.release,'dependencies':['field:orders:amount']}
        self.code('GOVERNANCE_BLOCKED',lambda:guard(context))
        self.code('GOVERNANCE_BLOCKED',lambda:read_export(READER,export.rsplit('/',1)[-1]))


class AnalysisTests(P4Fixture, unittest.TestCase):
    def test_shared_release_budget_roles_and_idempotent_replay(self):
        result=self.task(); self.assertEqual(result['state'],'succeeded',result)
        evidence=result['evidence']; self.assertEqual(evidence['verification'],'passed')
        self.assertEqual(evidence['sql_calls'],26)
        self.assertEqual({r['release_id'] for r in evidence['roles']},{self.release})
        self.assertEqual({r['parent_task_id'] for r in evidence['roles']},{result['task_id']})
        self.assertEqual(len(evidence['execution_ids']),2)
        calls=self.execute.call_count; self.assertEqual(self.task(),result); self.assertEqual(self.execute.call_count,calls)
        self.code('IDEMPOTENCY_CONFLICT',lambda:self.task(verification='off'))
        self.code('NOT_FOUND',lambda:analysis.get_task(self.registry,Principal('other',frozenset({'a'})),INFOS,result['task_id']))

    def test_verifier_catches_incomplete_group_output_without_fabricating_an_answer(self):
        original=self.execute.side_effect
        def omit(info,query,budget):
            result=original(info,query,budget)
            if 'GROUP BY' in query.sql: result['rows']=result['rows'][:-1]; result['row_count']-=1
            return result
        self.execute.side_effect=omit
        result=self.task(); self.assertEqual(result['state'],'failed'); self.assertEqual(result['evidence']['error_code'],'VERIFICATION_FAILED')
        self.assertIsNone(result['evidence']['result'])

    def test_budget_is_shared_and_verifier_cannot_restart_it(self):
        with patch.dict(os.environ,ANALYSIS_MAX_SQL_CALLS='16'):
            result=self.task()
        self.assertEqual(result['state'],'failed'); self.assertEqual(result['evidence']['error_code'],'BUDGET_EXCEEDED')
        self.assertLessEqual(result['evidence']['sql_calls'],16)
        self.assertIsNone(result['evidence']['result'])

    def test_nonadditive_required_verifier_rejected_before_execution(self):
        result=analysis.run(self.registry,READER,INFOS,fixture.intent(['metric:average']),'avg',verification='required')
        self.assertEqual(result['evidence']['error_code'],'VERIFICATION_UNSUPPORTED'); self.execute.assert_not_called()

    def test_opt_out_does_not_claim_independent_verification(self):
        result=self.task(verification='off'); self.assertEqual(result['state'],'succeeded')
        self.assertEqual(result['evidence']['verification'],'not_requested'); self.assertEqual(self.execute.call_count,1)

    def test_replay_revalidates_rollout_and_policy(self):
        result=self.task()
        with patch.dict(os.environ,ONTOLOGY_PLANNER_ROLLOUT_PERCENT='0'):
            self.code('PLANNER_NOT_ENABLED',lambda:analysis.get_task(self.registry,READER,INFOS,result['task_id']))
        policy=next(e for e in self.doc['entities'] if e['kind']=='Policy'); policy['attributes']['definition']=canonical({'version':1,'deny':True})
        self.publish(self.doc)
        self.code('STALE_POLICY',lambda:analysis.get_task(self.registry,READER,INFOS,result['task_id']))

    def test_reconcile_preserves_null_and_rejects_nonadditive_claims(self):
        grouped={'columns':['d0','m0'],'rows':[['x',None]]}; total={'rows':[[None]]}
        self.assertEqual(analysis.reconcile(grouped,total,[{'id':'m','aggregation':'sum'}])[0]['status'],'passed')
        self.code('SEMANTIC_VALIDATION_FAILED',lambda:analysis.reconcile(grouped,total,[{'id':'m','aggregation':'ratio'}]))

    def test_reconcile_preserves_precision_beyond_default_decimal_context(self):
        grouped={'columns':['d0','m0'],'rows':[['a','123456789012345678901234567890.12345678901234567890'],['b','0.00000000000000000001']]}
        total={'rows':[['123456789012345678901234567890.12345678901234567891']]}
        self.assertEqual(analysis.reconcile(grouped,total,[{'id':'m','aggregation':'sum'}])[0]['status'],'passed')

    def test_task_lease_expiry_never_reexecutes_same_operation(self):
        result=self.task();calls=self.execute.call_count
        with self.registry.connection(write=True) as db:db.execute("UPDATE analysis_tasks SET state='running',lease_until=0,payload=NULL,digest=NULL WHERE id=?",(result['task_id'],))
        replay=self.task();self.assertEqual(replay['state'],'interrupted');self.assertEqual(self.execute.call_count,calls)

    def test_tasks_share_current_governance_revocation(self):
        result=self.task()
        next(e for e in self.physical_doc['entities'] if e['id']=='field:orders:amount')['attributes']['dtype']='decimal(14,2)'
        self.scan()
        self.code('GOVERNANCE_BLOCKED',lambda:analysis.get_task(self.registry,READER,INFOS,result['task_id']))


if __name__=='__main__': unittest.main()
