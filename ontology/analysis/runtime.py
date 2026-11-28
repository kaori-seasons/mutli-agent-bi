"""One task, one trusted principal/release and one shared SQL/time budget. No model privileges."""
import copy
from decimal import Decimal, InvalidOperation, localcontext
import json
import os
import re
import time
from uuid import uuid4

from ontology.registry import fail
from ontology.runtime import namespace
from ontology.v2 import canonical, digest, invalid
from ontology.planner import runtime as planner
from server.execution import QueryBudget, bounded_int
from server.errors import ServiceError


def enabled():
    if os.getenv('ONTOLOGY_ANALYSIS_ENABLED', 'false') != 'true': fail('ANALYSIS_DISABLED', '协作分析尚未开启', 422)


def reconcile(grouped, total, metrics):
    """Independent arithmetic check, never sum AVG/ratio/distinct outputs."""
    if any(m['aggregation'] not in ('sum', 'count') for m in metrics): raise invalid('独立总量复核仅支持可加 SUM/COUNT')
    if len(total['rows']) != 1 or len(total['rows'][0]) != len(metrics): fail('VERIFICATION_FAILED', '总量结果形状不一致', 422)
    offset = len(grouped['columns']) - len(metrics)
    checks = []
    for i, metric in enumerate(metrics):
        values = [row[offset+i] for row in grouped['rows'] if row[offset+i] is not None]
        expected = total['rows'][0][i]
        try:
            with localcontext() as context:
                context.prec = 100  # MySQL DECIMAL(65,30), plus bounded row-count carry.
                observed = sum((Decimal(str(v)) for v in values), Decimal(0)) if values else (Decimal(0) if metric['aggregation'] == 'count' else None)
            wanted = Decimal(str(expected)) if expected is not None else None
            if any(v is not None and not v.is_finite() for v in (observed, wanted)): raise ValueError()
        except (ValueError, InvalidOperation): fail('VERIFICATION_FAILED', '总量包含无效数值', 422)
        if observed != wanted: fail('VERIFICATION_FAILED', '分组与独立总量不一致，可能是数据水位变化或结果不完整', 422)
        checks.append({'metric_id': metric['id'], 'check': 'group_sum_equals_independent_total', 'status': 'passed'})
    return checks


def get_task(registry, principal, infos, identity):
    enabled()
    with registry.connection(write=True) as db:
        db.execute("UPDATE analysis_tasks SET state='interrupted' WHERE state='running' AND lease_until<?", (time.time(),))
        row = db.execute('SELECT * FROM analysis_tasks WHERE id=? AND namespace=? AND actor=?', (identity, namespace(), principal.subject)).fetchone()
        if not row: fail('NOT_FOUND', '任务不存在或不可访问', 404)
        planner.rollout(principal, [row['source_id']])
        registry.release_in(db, row['release_id'], namespace())
        if row['expires'] <= time.time(): fail('TASK_EXPIRED', '任务证据已过期')
        payload = json.loads(row['payload']) if row['payload'] else None
        if payload is not None and digest(payload) != row['digest']: fail('TASK_CORRUPT', '任务证据摘要不匹配', 503)
    # Every replay revalidates current policy, source binding, incident isolation and receipt TTL.
    if payload:
        for plan_id in payload.get('plan_ids', []): planner.get_plan(registry, principal, plan_id)
        for execution_id in payload.get('execution_ids', []): planner.get_execution(registry, principal, execution_id, infos)
    return dict(task_id=identity, release_id=row['release_id'], state=row['state'], expires_at=row['expires'], evidence=payload)


def run(registry, principal, infos, intent, idempotency_key, verification='auto'):
    enabled()
    if verification not in ('auto', 'required', 'off') or not isinstance(idempotency_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', idempotency_key): raise invalid('任务核验模式或幂等键无效')
    if not isinstance(intent, dict) or not isinstance(intent.get('source_ids'), list) or len(intent['source_ids']) != 1 or not isinstance(intent['source_ids'][0], str): raise invalid('协作任务必须指定单源意图')
    planner.rollout(principal, intent['source_ids'])
    request_digest = digest({'intent': intent, 'verification': verification})
    pin = registry.pin(namespace(), principal)
    with registry.connection(write=True) as db:
        db.execute("UPDATE analysis_tasks SET state='interrupted' WHERE state='running' AND lease_until<?", (time.time(),))
        row = db.execute('SELECT * FROM analysis_tasks WHERE namespace=? AND actor=? AND idempotency_key=?', (namespace(), principal.subject, idempotency_key)).fetchone()
        if row:
            if row['request_digest'] != request_digest: fail('IDEMPOTENCY_CONFLICT', '任务幂等键不能复用为其他意图')
            identity = row['id']
        else:
            if db.execute("SELECT COUNT(*) FROM analysis_tasks WHERE state='running'").fetchone()[0] >= 2: fail('ANALYSIS_BUSY', '协作任务并发已满', 429)
            count, size = db.execute('SELECT COUNT(*),COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) FROM analysis_tasks').fetchone()
            running = db.execute("SELECT COUNT(*) FROM analysis_tasks WHERE state='running'").fetchone()[0]
            if count >= 10000 or size + (running+1)*6*1024*1024 > 64*1024*1024: fail('ANALYSIS_CAPACITY', '任务证据存储已满', 503)
            identity, now = uuid4().hex, time.time()
            db.execute('INSERT INTO analysis_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (identity, namespace(), principal.subject, idempotency_key, request_digest, intent['source_ids'][0], pin['release_id'], 'running', None, None, now, now+600, now+90))
    if row: return get_task(registry, principal, infos, identity)
    budget = QueryBudget(max_queries=bounded_int('ANALYSIS_MAX_SQL_CALLS', 32, 1, 32))
    budget.deadline = min(budget.deadline, time.monotonic()+60)
    began = time.monotonic()
    payload = {'plan_ids': [], 'execution_ids': [], 'roles': [], 'checks': [], 'verification': 'not_run', 'result': None,
               'scope': intent['source_ids'], 'release_id': pin['release_id'], 'model_calls': 0,
               'principal_ref': digest({'namespace': namespace(), 'subject': principal.subject}), 'deadline_seconds': 60}
    state = 'failed'
    try:
        plan = planner.create_plan(registry, principal, infos, intent, pin['release_id'], budget)
        payload['plan_ids'].append(plan['plan_id'])
        payload['roles'].append({'role': 'planner', 'task_id': uuid4().hex, 'parent_task_id': identity, 'release_id': pin['release_id']})
        eligible = bool(intent.get('dimensions')) and all(m['aggregation'] in ('sum', 'count') for m in plan['metrics'])
        if verification == 'required' and not eligible: fail('VERIFICATION_UNSUPPORTED', '当前意图不支持独立总量核验', 422)
        receipt = planner.execute_plan(registry, principal, infos, plan['plan_id'], identity+'-primary', budget)
        payload['execution_ids'].append(receipt['execution_id'])
        if receipt['state'] != 'succeeded': fail(receipt['error_code'] or 'EXECUTION_RUNNING', '主查询未成功完成', 422)
        payload['roles'].append({'role': 'query_worker', 'task_id': uuid4().hex, 'parent_task_id': identity, 'release_id': pin['release_id']})
        if verification != 'off' and eligible:
            # Preserve joins and filters so the total has exactly the same population as the grouped query.
            total_intent = copy.deepcopy(intent); total_intent['dimensions'] = []
            total_plan = planner.create_plan(registry, principal, infos, total_intent, pin['release_id'], budget)
            payload['plan_ids'].append(total_plan['plan_id'])
            total = planner.execute_plan(registry, principal, infos, total_plan['plan_id'], identity+'-verify', budget)
            payload['execution_ids'].append(total['execution_id'])
            if total['state'] != 'succeeded': fail(total['error_code'] or 'VERIFICATION_FAILED', '复核查询未完成', 422)
            payload['checks'] = reconcile(receipt['result'], total['result'], plan['metrics'])
            payload['verification'] = 'passed'
            payload['roles'].append({'role': 'verifier', 'task_id': uuid4().hex, 'parent_task_id': identity, 'release_id': pin['release_id']})
        else:
            payload['verification'] = 'not_requested' if verification == 'off' else 'not_applicable'
        # No additional business facts or causal explanations are generated.
        payload['result'] = receipt['result']
        payload['limitations'] = ['独立查询之间无源快照保证；不一致即拒绝完整答案。', '核验只证明所列算术一致性，不证明业务口径或因果关系。']
        for eid in payload['execution_ids']: planner.get_execution(registry, principal, eid, infos)
        state = 'succeeded'
    except Exception as error:
        payload['result'] = None
        payload['error_code'] = error.code if isinstance(error, ServiceError) else 'ANALYSIS_FAILED'
        if verification != 'off': payload['verification'] = 'failed'
    payload['sql_calls'] = budget.calls; payload['max_sql_calls'] = budget.max_queries
    payload['elapsed_ms'] = round((time.monotonic()-began)*1000, 3)
    with registry.connection(write=True) as db:
        current = db.execute('SELECT state,lease_until FROM analysis_tasks WHERE id=?', (identity,)).fetchone()
        if current['state'] != 'running' or current['lease_until'] <= time.time(): fail('TASK_INTERRUPTED', '任务租约已失效')
        if len(canonical(payload).encode()) > 6*1024*1024: payload['result'] = None; payload['error_code'] = 'TASK_TOO_LARGE'; state = 'failed'
        db.execute('UPDATE analysis_tasks SET state=?,payload=?,digest=? WHERE id=?', (state, canonical(payload), digest(payload), identity))
    return get_task(registry, principal, infos, identity)
