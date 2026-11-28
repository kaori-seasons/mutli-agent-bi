"""Reproducible P0–P4 local regression and scoped deterministic E4 evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import unittest
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def fault_controls():
    from ontology.analysis.runtime import reconcile
    from server.errors import ServiceError
    metric=[{'id':'metric:sales','aggregation':'sum'}]
    cases=[('valid', [['east','120'],['south','30'],[None,'10']], False),
           ('missing_group', [['east','120'],['south','30']], True),
           ('duplicated_group', [['east','120'],['east','120'],['south','30'],[None,'10']], True),
           ('wrong_amount', [['east','121'],['south','30'],[None,'10']], True)]
    results=[]
    for name,rows,expected in cases:
        caught=False
        try:reconcile({'columns':['d0','m0'],'rows':rows},{'rows':[['160']]},metric)
        except ServiceError as error:
            if error.code!='VERIFICATION_FAILED':raise
            caught=True
        results.append({'case':name,'injected_fault':expected,'independent_verifier_rejected':caught,'matches_expectation':caught==expected})
    return {'scope':'deterministic result-artifact fault controls; not an LLM accuracy dataset', 'cases':results,
            'baseline_independent_checks':0,'faults_detected':sum(r['injected_fault'] and r['independent_verifier_rejected'] for r in results),
            'false_rejections':sum(not r['injected_fault'] and r['independent_verifier_rejected'] for r in results)}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    if not os.getenv('P0_MYSQL_TEST_PORT') or os.getenv('P0_MYSQL_TEST_ALLOW_CREATE')!='1':parser.error('requires a disposable local MySQL fixture; application .env is never loaded')
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),top_level_dir=str(ROOT))
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    from tests.test_p4_mysql import OBSERVATIONS
    controls=fault_controls()
    passed=result.wasSuccessful() and not result.skipped and all(r['matches_expectation'] for r in controls['cases'])
    files=sorted([*ROOT.glob('ontology/governance/*.py'),*ROOT.glob('ontology/analysis/*.py'),*ROOT.glob('integrations/*.py'),
                  ROOT/'ontology/planner/compiler.py',ROOT/'ontology/planner/runtime.py',ROOT/'server/governance_api.py',ROOT/'main.py'])
    report={'schema_version':1,'recorded_at':datetime.now(timezone.utc).isoformat(),'scope':'local_synthetic_engineering',
            'local_gate':'passed' if passed else 'failed','python':platform.python_version(),'tests':result.testsRun,'skipped':len(result.skipped),
            'failures':len(result.failures),'errors':len(result.errors),'failed_cases':[test.id() for test,_ in result.failures+result.errors],
            'measurements':OBSERVATIONS,'E4_fault_controls':controls,'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
            'online_rollout_percent':0,'business_E1_E4_gate':'not_evaluated',
            'limitations':['Synthetic MySQL fixture and local JWKS; no production-volume or real Cognito/model calls.',
                           'E4 checks additive arithmetic and isolated injected faults, not causal explanation or business accuracy uplift.',
                           'Independent SQL statements do not share a source snapshot; concurrent changes may cause conservative rejection.']}
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'local_gate':report['local_gate'],'tests':result.testsRun,'output':str(path)}))
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
