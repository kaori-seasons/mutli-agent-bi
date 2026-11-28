"""Emit an honest synthetic E0–E3 validation report; never imply an unrun LLM benchmark."""
import argparse
import io
import json
import os
from pathlib import Path
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run(names):
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in names)
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=0).run(suite)
    return dict(status='passed' if result.wasSuccessful() and not result.skipped else 'failed' if not result.wasSuccessful() else 'skipped',
                tests=result.testsRun, skipped=len(result.skipped), failures=len(result.failures), errors=len(result.errors),
                failed_cases=[test.id() for test, _ in result.failures + result.errors])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    report = {'schema_version': 1, 'recorded_at': datetime.now(timezone.utc).isoformat(), 'data': 'synthetic_only',
              'production_gate': 'not_evaluated', 'model_calls': 0, 'layers': {},
              'limitations': ['No real Cognito, Bedrock, business data, 200-question human-reviewed set, latency/load or statistical uplift benchmark.',
                              'E0 infrastructure regression is not the original LLM answer-quality baseline; E1 catalog-only comparison is not implemented.']}
    report['contract_tests'] = run(['tests.test_planner_p3'])
    enabled = bool(os.getenv('P0_MYSQL_TEST_PORT')) and os.getenv('P0_MYSQL_TEST_ALLOW_CREATE') == '1'
    if enabled:
        report['layers']['E0'] = dict(run(['tests.test_p0_mysql']), scope='existing P0 SQL/federation infrastructure regression only', answer_quality='not_evaluated')
        from tests.test_p3_mysql import PlannerMySQLTests
        names = unittest.defaultTestLoader.getTestCaseNames(PlannerMySQLTests)
        for layer, selected in [('E2', [n for n in names if 'e3_' not in n]), ('E3', [n for n in names if 'e3_' in n])]:
            report['layers'][layer] = dict(run(['tests.test_p3_mysql.PlannerMySQLTests.' + n for n in selected]),
                                           scope='synthetic approved metric gold results' if layer == 'E2' else 'P2 candidate → human review → explicit contract → execution')
    else:
        for layer in ('E0', 'E2', 'E3'): report['layers'][layer] = {'status': 'not_run', 'reason': 'disposable MySQL opt-in environment absent'}
    report['layers']['E1'] = {'status': 'not_run', 'reason': 'A comparable catalog-only query loop and model baseline are not available; no fabricated scores.'}
    path = Path(args.output); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'output': str(path), 'contract_tests': report['contract_tests'], 'layers': report['layers'], 'production_gate': report['production_gate']}, ensure_ascii=False))
    return int(any(v.get('status') == 'failed' for v in [report['contract_tests'], *report['layers'].values()]))


if __name__ == '__main__': raise SystemExit(main())
