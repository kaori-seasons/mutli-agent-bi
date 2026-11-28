"""Local release acceptance: real HTTP, RS256 validation and disposable MySQL, no cloud calls."""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import copy
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from werkzeug.serving import WSGIRequestHandler, make_server

from main import create_app
from ontology.registry import Registry
from ontology.planner import runtime
from server import api, conf
from tests import planner_fixtures as fixture
from tests import test_p3_mysql as mysql_fixture

OBSERVATIONS = {}


def percentile(values, p):
    values = sorted(values)
    return round(values[max(0, math.ceil(len(values) * p) - 1)], 3)


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs): pass


@unittest.skipUnless(os.getenv('P0_MYSQL_TEST_PORT') and os.getenv('P0_MYSQL_TEST_ALLOW_CREATE') == '1', 'disposable MySQL fixture not enabled')
class LocalAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_class = mysql_fixture.PlannerMySQLTests
        fixture_class.setUpClass()
        cls.addClassCleanup(fixture_class.doClassCleanups)
        cls.info, cls.admin, cls.database, cls.dbuser = fixture_class.info, fixture_class.admin, fixture_class.database, fixture_class.user
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with cls.admin.cursor() as c:
            c.execute('SELECT VERSION()')
            OBSERVATIONS['mysql_version'] = c.fetchone()[0]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name); self.infos = {'a': self.info}
        env = patch.dict(os.environ, ONTOLOGY_REGISTRY_PATH=str(self.path / 'registry.sqlite3'), ONTOLOGY_REGISTRY_NAMESPACE='default',
                         QUERY_EXPORT_PATH=str(self.path / 'exports'), ONTOLOGY_PLANNER_SOURCES='["a"]', ONTOLOGY_PLANNER_ROLLOUT_PERCENT='100',
                         ONTOLOGY_QUERY_SOURCES='[]', BACK_USER_POOL_REGION='us-east-1', BACK_USER_POOL_ID='us-east-1_localFixture',
                         BACK_USER_CLIENT_ID='LocalFixtureClient', AUTH_ADMIN_GROUPS='local-admins',
                         AUTH_SOURCE_GRANTS=json.dumps({'group:local-readers': ['a'], 'group:local-admins': ['a']}))
        env.start(); self.addCleanup(env.stop)
        infos = patch.dict(conf.mysql_info, self.infos, clear=True); infos.start(); self.addCleanup(infos.stop)
        self.issuer = 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_localFixture'
        def signing_key(token):
            if jwt.get_unverified_header(token).get('kid') != 'local-fixture': raise jwt.PyJWKClientError('unknown fixture key')
            return SimpleNamespace(key=self.key.public_key())
        # Only the key transport is local; production authenticate() verifies the actual RS256 token and claims.
        keys = patch('server.auth._jwks', return_value=SimpleNamespace(get_signing_key_from_jwt=signing_key))
        keys.start(); self.addCleanup(keys.stop)
        models = patch('server.api.aws.get', side_effect=AssertionError('local acceptance must not call models'))
        self.models = models.start(); self.addCleanup(models.stop)
        self.server = make_server('127.0.0.1', 0, create_app(query_service=api), threaded=True, request_handler=QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.registry = Registry(); self.document = fixture.document(self.database)
        self.release = self.publish(self.document)

    def stop_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)

    def token(self, subject='reader', admin=False, **claims):
        now = int(time.time())
        payload = dict(iss=self.issuer, client_id='LocalFixtureClient', sub=subject, token_use='access', iat=now, exp=now+600,
                       **{'cognito:groups': ['local-admins' if admin else 'local-readers']})
        payload.update(claims)
        return jwt.encode(payload, self.key, algorithm='RS256', headers={'kid': 'local-fixture'})

    def http(self, path, payload=None, token=None, subject='reader', admin=False):
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (self.token(subject, admin) if token is None else token)}
        req = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        try:
            response = urlopen(req, timeout=20)
        except HTTPError as error: response = error
        with response: return response.status, json.loads(response.read())

    def publish(self, document):
        state = self.registry.active('default')
        status, change = self.http('/v1/ontology/changesets', dict(document=document, expected_active=state['release_id'], expected_generation=state['generation']), subject='author', admin=True)
        self.assertEqual(status, 201, change)
        review = dict(expected_digest=change['digest'], decision='approved', reason='独立核验本地合成夹具')
        status, result = self.http('/v1/ontology/changesets/' + change['id'] + '/review', review, subject='reviewer', admin=True)
        self.assertEqual(status, 200, result)
        status, result = self.http('/v1/ontology/releases', dict(changeset_id=change['id'], expected_digest=change['digest']), subject='author', admin=True)
        self.assertEqual(status, 200, result)
        return result['release_id']

    def plan(self, subject='reader'):
        status, plan = self.http('/v1/query-plans/validate', {'intent': fixture.intent(['metric:count'], dimensions=False)}, subject=subject)
        self.assertEqual(status, 201, plan)
        return plan

    def execute(self, plan, key, subject='reader'):
        return self.http('/v1/queries/execute', {'plan_id': plan['plan_id'], 'idempotency_key': key}, subject=subject)

    def test_signed_http_authentication_admin_separation_and_readiness(self):
        for token in ('', 'forged', self.token(exp=int(time.time())-1), self.token(client_id='wrong'), self.token(token_use='id')):
            status, _ = self.http('/v1/query-plans/validate', {'intent': fixture.intent()}, token=token)
            self.assertEqual(status, 401)
        status, _ = self.http('/v1/ontology/changesets', {})
        self.assertEqual(status, 403)
        plan = self.plan(); status, _ = self.execute(plan, 'auth')
        self.assertEqual(status, 200)
        status, _ = self.http('/v1/query-plans/' + plan['plan_id'], subject='another-reader')
        self.assertEqual(status, 404)
        status, readiness = self.http('/readyz'); self.assertEqual(status, 200, readiness)
        self.models.assert_not_called()
        OBSERVATIONS['authentication'] = {'transport': 'real_loopback_http', 'signature': 'RS256-2048', 'verifier': 'production authenticate()',
                                         'jwks': 'local in-memory key resolver', 'real_cognito': False, 'negative_cases_passed': 5}

    def test_e1_catalog_only_abstains_then_e2_approved_contract_executes(self):
        catalog_only = copy.deepcopy(self.document)
        catalog_only['entities'] = [e for e in catalog_only['entities'] if e['kind'] not in ('Metric', 'MetricContract')]
        self.publish(catalog_only)
        with patch('ontology.planner.runtime.execute_source', wraps=runtime.execute_source) as sql:
            status, result = self.http('/v1/query-plans/validate', {'intent': fixture.intent(['metric:count'], dimensions=False)})
            self.assertEqual(status, 404, result); sql.assert_not_called()
        self.publish(self.document)
        status, result = self.execute(self.plan(), 'e2')
        self.assertEqual(status, 200, result); self.assertEqual(result['result']['rows'], [[5]])
        OBSERVATIONS['E1_local_control'] = {'status': 'passed', 'input': 'same source and structured metric intent',
                                          'catalog_only': 'abstained; zero business SQL', 'approved_contract': 'count=5',
                                          'claim': 'authorization/semantic boundary control, not an LLM answer-quality comparison'}

    def test_ten_client_load_preserves_admission_and_result_correctness(self):
        subjects = ['reader'] + ['reader' + str(i) for i in range(1, 10)]
        plans = {s: self.plan(s) for s in subjects}
        def client(subject):
            latency, statuses = [], Counter()
            for iteration in range(10):
                started = time.monotonic(); deadline = started + 15
                key = 'load-' + subject + '-' + str(iteration)
                while True:
                    status, receipt = self.execute(plans[subject], key, subject)
                    statuses[status] += 1
                    if status == 200:
                        self.assertEqual(receipt['state'], 'succeeded')
                        self.assertEqual(receipt['result']['rows'], [[5 if subject == 'reader' else 0]])
                        latency.append((time.monotonic()-started)*1000); break
                    self.assertIn(status, (202, 429), receipt)
                    self.assertLess(time.monotonic(), deadline, receipt)
                    time.sleep(.025)
            return latency, statuses
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=10) as pool: results = list(pool.map(client, subjects))
        durations = [v for latency, _ in results for v in latency]; statuses = sum((s for _, s in results), Counter())
        self.assertEqual(len(durations), 100)
        self.assertLess(percentile(durations, .95), 15000)
        OBSERVATIONS['execution_load'] = {'clients': 10, 'logical_queries': 100, 'correct_results': 100, 'wrong_results': 0,
                                          'http_status_counts': dict(statuses), 'retry': 'same idempotency key, 25ms bounded admission backoff',
                                          'wall_seconds': round(time.monotonic()-started, 3), 'p50_ms': percentile(durations, .50),
                                          'p95_ms': percentile(durations, .95), 'max_ms': round(max(durations), 3), 'includes_admission_wait': True,
                                          'fixture_rows': 8, 'production_scale_claim': False}

    def test_knowledge_latency_under_ten_clients(self):
        def search(_):
            start = time.monotonic(); status, body = self.http('/v1/knowledge/search', {'question': 'count', 'source_ids': ['a'], 'kinds': ['Metric']})
            self.assertEqual(status, 200, body); self.assertEqual(body['candidates'][0]['id'], 'metric:count')
            return (time.monotonic()-start)*1000
        with ThreadPoolExecutor(max_workers=10) as pool: timings = list(pool.map(search, range(200)))
        self.assertLess(percentile(timings, .95), 300)
        OBSERVATIONS['knowledge_load'] = {'clients': 10, 'requests': 200, 'p95_ms': percentile(timings, .95),
                                         'max_ms': round(max(timings), 3), 'temperature': 'warm local process',
                                         'note': '200 repeated load requests are not 200 independently reviewed business questions'}

    def test_source_outage_never_returns_success_and_recovery_needs_new_operation(self):
        plan = self.plan()
        with self.admin.cursor() as c: c.execute(f"ALTER USER '{self.dbuser}'@'%' ACCOUNT LOCK")
        try:
            status, failed = self.execute(plan, 'outage')
            self.assertEqual(status, 422, failed); self.assertEqual(failed['state'], 'failed'); self.assertIsNone(failed['result'])
        finally:
            with self.admin.cursor() as c: c.execute(f"ALTER USER '{self.dbuser}'@'%' ACCOUNT UNLOCK")
        status, replay = self.execute(plan, 'outage')
        self.assertEqual(status, 422); self.assertEqual(replay['execution_id'], failed['execution_id'])
        status, recovered = self.execute(self.plan(), 'recovered')
        self.assertEqual(status, 200, recovered); self.assertEqual(recovered['result']['rows'], [[5]])
        OBSERVATIONS['source_recovery'] = {'status': 'passed', 'fault': 'actual MySQL read account locked then unlocked', 'failed_result_reused': False}

    def test_consistent_backup_restore_preserves_release_and_idempotent_receipt(self):
        plan = self.plan(); status, receipt = self.execute(plan, 'backup')
        self.assertEqual(status, 200, receipt)
        backup_path = self.path / 'restored.sqlite3'
        with sqlite3.connect(backup_path) as target, self.registry.connection() as source: source.backup(target)
        with patch.dict(os.environ, ONTOLOGY_REGISTRY_PATH=str(backup_path)), patch('ontology.planner.runtime.execute_source', side_effect=AssertionError('must replay, not execute')):
            status, restored = self.execute(plan, 'backup')
            self.assertEqual(status, 200, restored); self.assertEqual(restored['execution_id'], receipt['execution_id'])
            self.assertEqual(restored['result']['evidence']['release_id'], self.release)
        OBSERVATIONS['restore'] = {'status': 'passed', 'method': 'sqlite Connection.backup', 'release_preserved': True, 'duplicate_sql': 0}

    def test_zero_rollout_disables_new_and_cached_execution_without_breaking_health(self):
        plan = self.plan(); status, receipt = self.execute(plan, 'switch')
        self.assertEqual(status, 200)
        with patch.dict(os.environ, ONTOLOGY_PLANNER_ROLLOUT_PERCENT='0'):
            self.assertEqual(self.http('/v1/query-plans/validate', {'intent': fixture.intent()})[0], 422)
            self.assertEqual(self.execute(plan, 'switch')[0], 422)
            self.assertEqual(self.http('/v1/executions/' + receipt['execution_id'])[0], 422)
            self.assertEqual(self.http('/healthz')[0], 200)
            self.assertEqual(self.http('/readyz')[0], 200)
        self.assertEqual(self.execute(plan, 'switch')[0], 200)
        OBSERVATIONS['rollout_rollback'] = {'status': 'passed', 'tested_percentages': [100, 0, 100], 'legacy_health_preserved': True}

    def test_five_percent_cohort_is_stable_and_does_not_expand_source_grants(self):
        with patch.dict(os.environ, ONTOLOGY_PLANNER_ROLLOUT_PERCENT='5'):
            # Independently computed SHA-256 fixtures: bucket 2 and bucket 16 in namespace default.
            for _ in range(2):
                status, plan = self.http('/v1/query-plans/validate', {'intent': fixture.intent(['metric:count'], dimensions=False)}, subject='local-reader-16')
                self.assertEqual(status, 201, plan)
                self.assertEqual(self.execute(plan, 'cohort-' + plan['plan_id'], subject='local-reader-16')[0], 200)
                self.assertEqual(self.http('/v1/query-plans/validate', {'intent': fixture.intent()}, subject='local-reader-0')[0], 422)
            with patch.dict(os.environ, AUTH_SOURCE_GRANTS='{}'):
                self.assertEqual(self.http('/v1/query-plans/validate', {'intent': fixture.intent()}, subject='local-reader-16')[0], 404)
        OBSERVATIONS['five_percent'] = {'status': 'passed', 'eligible_bucket': 2, 'ineligible_bucket': 16,
                                       'stable_repeat': True, 'source_grants_rechecked': True, 'changed_default_configuration': False}


if __name__ == '__main__': unittest.main()
