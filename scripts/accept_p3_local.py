"""Execute all Python regression and local acceptance checks, and write a scoped release report."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if not os.getenv('P0_MYSQL_TEST_PORT') or os.getenv('P0_MYSQL_TEST_ALLOW_CREATE') != '1':
        parser.error('requires the documented disposable local MySQL fixture; never loads application .env')
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    from tests.test_p3_local_acceptance import OBSERVATIONS
    files = [*ROOT.glob('ontology/planner/*.py'), ROOT / 'server/planner_api.py', ROOT / 'server/auth.py', ROOT / 'server/exports.py', ROOT / 'main.py']
    report = dict(schema_version=1, recorded_at=datetime.now(timezone.utc).isoformat(), acceptance_scope='local_production_engineering',
                  local_gate='passed' if result.wasSuccessful() and not result.skipped else 'failed',
                  python=platform.python_version(), tests=result.testsRun, skipped=len(result.skipped),
                  failures=len(result.failures), errors=len(result.errors), failed_cases=[test.id() for test, _ in result.failures + result.errors],
                  measurements=OBSERVATIONS, source_sha256={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                  online_rollout_authorized=False, online_rollout_percent=0,
                  limitations=['Local synthetic fixture, not production-volume data.', 'RS256 gateway validated with local JWKS resolver; actual Cognito not contacted.',
                               'E1 is a catalog-only abstention control, not a model quality baseline.', 'No real-business 200-question owner-reviewed dataset or E0/E1 LLM statistical comparison.'])
    path = Path(args.output); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'local_gate': report['local_gate'], 'tests': result.testsRun, 'output': str(path)}, ensure_ascii=False))
    return 0 if report['local_gate'] == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())
