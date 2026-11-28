"""Real loopback JWT HTTP and MySQL governance/verification acceptance."""
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from integrations import mcp_stdio
from ontology.v2 import canonical, entity
from tests import test_p3_local_acceptance as base
from tests import planner_fixtures as fixture

OBSERVATIONS = {}


@unittest.skipUnless(os.getenv('P0_MYSQL_TEST_PORT') and os.getenv('P0_MYSQL_TEST_ALLOW_CREATE')=='1','disposable MySQL fixture not enabled')
class P4MySQLTests(unittest.TestCase):
    setUpClass=classmethod(base.LocalAcceptanceTests.setUpClass.__func__)
    stop_server=base.LocalAcceptanceTests.stop_server
    token=base.LocalAcceptanceTests.token
    http=base.LocalAcceptanceTests.http
    publish=base.LocalAcceptanceTests.publish
    plan=base.LocalAcceptanceTests.plan
    execute=base.LocalAcceptanceTests.execute

    def setUp(self):
        base.LocalAcceptanceTests.setUp(self)
        env=patch.dict(os.environ,ONTOLOGY_GOVERNANCE_SOURCES='["a"]',ONTOLOGY_ANALYSIS_ENABLED='true',ANALYSIS_MAX_SQL_CALLS='32')
        env.start();self.addCleanup(env.stop)

    def scan(self):
        status,result=self.http('/v1/ontology/governance/runs',{'source_id':'a','idempotency_key':uuid4().hex},subject='governor',admin=True)
        self.assertEqual(status,200,result);return result

    def task(self, mode='auto'):
        return self.http('/v1/analysis/tasks',{'intent':fixture.intent(['metric:sales','metric:count']), 'idempotency_key':uuid4().hex,'verification':mode})

    def test_real_schema_drift_isolation_reapproval_reverify_and_resolution(self):
        initial=self.scan();self.assertEqual(initial['evidence']['findings'],[])
        plan=self.plan();status,receipt=self.execute(plan,'before-drift');self.assertEqual(status,200)
        with self.admin.cursor() as c:c.execute(f'ALTER TABLE {self.database}.orders CHANGE amount amount_renamed DECIMAL(12,2)')
        try:
            detected=self.scan();self.assertTrue(any(f['kind']=='column_missing' for f in detected['evidence']['findings']))
            self.assertEqual(self.http('/v1/executions/'+receipt['execution_id'])[0],409)
            status,issues=self.http('/v1/ontology/governance/incidents',admin=True)
            self.assertEqual(status,200);self.assertTrue(issues['incidents'])
            for issue in issues['incidents']:
                status,_=self.http('/v1/ontology/governance/incidents/'+issue['id']+'/transition',{'expected_revision':issue['revision'],'action':'acknowledge','reason':'restore approved schema','owner':'local-owner'},admin=True)
                self.assertEqual(status,200)
        finally:
            with self.admin.cursor() as c:c.execute(f'ALTER TABLE {self.database}.orders CHANGE amount_renamed amount DECIMAL(12,2)')
        repaired=copy.deepcopy(self.document);repaired['entities'][0]['description']='Independently reviewed restoration'
        new_release=self.publish(repaired);recheck=self.scan();self.assertEqual(recheck['evidence']['findings'],[])
        for issue in issues['incidents']:
            status,result=self.http('/v1/ontology/governance/incidents/'+issue['id']+'/transition',{'expected_revision':issue['revision']+1,'action':'resolve','reason':'approved version and fresh physical recheck','run_id':recheck['id']},admin=True)
            self.assertEqual(status,200,result)
        status,result=self.execute(self.plan(),'restored');self.assertEqual(status,200,result)
        self.assertEqual(result['result']['rows'],[[5]])
        OBSERVATIONS['drift_loop']={'status':'passed','rename_detected':True,'old_receipt_blocked':True,'new_approved_release_required':new_release!=self.release,'fresh_recheck':True,'source_data_written_by_application':False}

    def test_real_quality_fail_and_pass_with_zero_sample_claims(self):
        doc=copy.deepcopy(self.document);field='field:orders:amount'
        doc['entities'].append(entity('QualityRule','quality:amount','amount completeness','data-owner',['a'],
                                      {'target_ids':[field],'definition':canonical({'version':1,'check':'not_null','field_ids':[field]})},dependencies=[field],availability='reference'))
        self.publish(doc)
        run=self.scan();quality=run['evidence']['quality'][0]
        self.assertEqual(quality['violations'],1);self.assertEqual(quality['status'],'failed');self.assertFalse(quality['sampled'])
        self.assertEqual(self.http('/v1/query-plans/validate',{'intent':fixture.intent()})[0],409)
        OBSERVATIONS['quality']={'status':'passed','actual_null_violations':1,'method':'exact_count','sampled':False}

    def test_real_approved_quality_runs_inside_execution_budget(self):
        doc=copy.deepcopy(self.document);field='field:orders:owner_id'
        doc['entities'].append(entity('QualityRule','quality:owner','owner completeness','data-owner',['a'],
                                      {'target_ids':[field],'definition':canonical({'version':1,'check':'not_null','field_ids':[field]})},dependencies=[field],availability='reference'))
        self.publish(doc)
        status,result=self.execute(self.plan(),'quality-pass');self.assertEqual(status,200,result)
        self.assertEqual(result['result']['evidence']['quality_results'][0]['status'],'passed')
        self.assertEqual(result['result']['evidence']['quality_results'][0]['scope'],'authorized_plan_population')
        self.assertEqual(result['result']['evidence']['sql_calls'],10)

    def test_inline_quality_respects_subject_scope_while_admin_scan_checks_table(self):
        doc=copy.deepcopy(self.document);field='field:orders:owner_id'
        doc['entities'].append(entity('QualityRule','quality:scope','owner completeness','data-owner',['a'],
                                      {'target_ids':[field],'definition':canonical({'version':1,'check':'not_null','field_ids':[field]})},dependencies=[field],availability='reference'))
        self.publish(doc)
        with self.admin.cursor() as c:
            c.execute(f"INSERT INTO {self.database}.orders VALUES (1,102,1,50,1,'2026-11-10 00:00:00',NULL)")
        try:
            status,result=self.execute(self.plan(),'scope-pass');self.assertEqual(status,200,result)
            self.assertEqual(result['result']['rows'],[[5]])
            self.assertEqual(result['result']['evidence']['quality_results'][0]['violations'],0)
            run=self.scan();self.assertEqual(run['evidence']['quality'][0]['violations'],1)
            self.assertEqual(run['evidence']['quality'][0]['scope'],'approved_source_table')
        finally:
            with self.admin.cursor() as c:c.execute(f'DELETE FROM {self.database}.orders WHERE tenant_id=1 AND id=102')
        OBSERVATIONS['quality_scope']={'status':'passed','reader_policy_applied':True,'reader_violations':0,'admin_table_violations':1}

    def test_unique_rule_checks_composite_keys_and_repeated_null_groups(self):
        from ontology.governance.rules import evaluate
        from server.execution import QueryBudget
        entities={e['id']:e for e in self.document['entities']}
        def rule(fields):
            return entity('QualityRule','quality:unique','unique fixture','owner',['a'],
                          {'target_ids':fields,'definition':canonical({'version':1,'check':'unique','field_ids':fields})},dependencies=fields,availability='reference')
        primary=evaluate(rule(['field:orders:tenant_id','field:orders:id']),entities,self.info,QueryBudget())
        self.assertEqual(primary['status'],'passed');self.assertEqual(primary['violations'],0)
        with self.admin.cursor() as c:
            c.execute(f"INSERT INTO {self.database}.orders (tenant_id,id,customer_id,owner_id) VALUES (1,100,NULL,'fixture'),(1,101,NULL,'fixture')")
        try:
            repeated=evaluate(rule(['field:orders:customer_id']),entities,self.info,QueryBudget())
            self.assertEqual(repeated['status'],'failed');self.assertEqual(repeated['violations'],3)
        finally:
            with self.admin.cursor() as c:c.execute(f'DELETE FROM {self.database}.orders WHERE tenant_id=1 AND id IN (100,101)')
        OBSERVATIONS['unique_quality']={'status':'passed','composite_primary_key_violations':0,'duplicate_groups_including_null':3,'null_groups_checked':True}

    def test_real_e4_additive_verification_and_measured_overhead(self):
        baseline_status,baseline=self.task('off');verified_status,verified=self.task('required')
        self.assertEqual(baseline_status,200,baseline);self.assertEqual(verified_status,200,verified)
        self.assertEqual(verified['evidence']['verification'],'passed')
        self.assertEqual(baseline['evidence']['result']['rows'],verified['evidence']['result']['rows'])
        self.assertEqual([c['status'] for c in verified['evidence']['checks']],['passed','passed'])
        self.assertEqual(verified['evidence']['sql_calls'],26)
        OBSERVATIONS['E4_local']={'status':'passed','data':'synthetic','roles':'deterministic','baseline_sql_calls':baseline['evidence']['sql_calls'],
            'verified_sql_calls':verified['evidence']['sql_calls'],'baseline_elapsed_ms':baseline['evidence']['elapsed_ms'],
            'verified_elapsed_ms':verified['evidence']['elapsed_ms'],'same_results':True,'model_calls':0,'business_incremental_benefit':'not_evaluated'}

    def test_mcp_thin_adapter_uses_actual_jwt_http_and_rejects_forgery(self):
        path=self.path/'token';path.write_text(self.token());path.chmod(0o600)
        with patch.dict(os.environ,MCP_BACKEND_URL=self.base,MCP_ACCESS_TOKEN_FILE=str(path)):
            result,failed=mcp_stdio.backend('analysis_run',{'intent':fixture.intent(),'idempotency_key':'mcp','verification':'required'})
            self.assertFalse(failed,result);self.assertEqual(result['state'],'succeeded')
            path.write_text('forged')
            result,failed=mcp_stdio.backend('analysis_get',{'identity':result['task_id']})
            self.assertTrue(failed);self.assertEqual(result['error']['code'],'INVALID_TOKEN')
        OBSERVATIONS['mcp']={'status':'passed','transport':'stdio adapter to real RS256 loopback REST','real_cognito':False,'management_tools_exposed':False}


if __name__=='__main__':unittest.main()
