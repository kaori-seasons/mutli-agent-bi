"""Immutable, principal-bound plans and idempotent read-only execution receipts."""
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
import re
import time
from uuid import uuid4

from ontology.registry import Registry, authorized, fail
from ontology.runtime import namespace
from ontology.v2 import canonical, digest, invalid
from ontology.discovery.workflow import binding_digest
from server.errors import ServiceError
from server.execution import QueryBudget, execute_source
from server.query_policy import PreparedQuery
from .compiler import compile_plan, catalog_for

PLAN_TTL = 600
RESULT_TTL = 600
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024


def capacity(db, extra_bytes=0, new_rows=0):
    counts = db.execute('SELECT (SELECT COUNT(*) FROM query_plans)+(SELECT COUNT(*) FROM query_executions), '
                        '(SELECT COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) FROM query_plans)+'
                        '(SELECT COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) FROM query_executions)').fetchone()
    if counts[0] + new_rows > 10000 or counts[1] + extra_bytes > MAX_ARTIFACT_BYTES or extra_bytes > 6 * 1024 * 1024:
        fail('PLANNER_CAPACITY', '计划/结果存储达到限额，请由运维归档后恢复', 503)


def policy_fingerprint(document, source):
    rules = [e for e in document['entities'] if e['kind'] in ('Policy', 'QualityRule')
             and e['availability'] != 'candidate' and source in e['source_ids']]
    return digest(sorted(rules, key=lambda e: e['id']))


def policy_guard(registry, data, db=None):
    if db is None:
        with registry.connection() as connection: return policy_guard(registry, data, connection)
    active = registry.active_in(db, namespace())['release_id']
    if not active: fail('STALE_POLICY', '当前政策权威版本不可用')
    document, _ = registry.release_in(db, active, namespace())
    if policy_fingerprint(document, data['source_ids'][0]) != data['policy_fingerprint']:
        fail('STALE_POLICY', '当前批准政策已变化，请重新规划')


def configuration():
    try:
        enabled = json.loads(os.getenv('ONTOLOGY_PLANNER_SOURCES', '[]'))
        percent = int(os.getenv('ONTOLOGY_PLANNER_ROLLOUT_PERCENT', '0'))
        if (not isinstance(enabled, list) or any(not isinstance(x, str) or not x for x in enabled)
                or len(set(enabled)) != len(enabled) or not 0 <= percent <= 100): raise ValueError()
    except (TypeError, ValueError): fail('PLANNER_CONFIGURATION', '规划灰度配置无效', 503)
    return set(enabled), percent


def rollout(principal, sources):
    authorized(principal, sources)
    enabled, percent = configuration()
    bucket = int(hashlib.sha256((namespace() + ':' + principal.subject).encode()).hexdigest()[:8], 16) % 100
    if not set(sources) <= set(enabled) or bucket >= percent:
        fail('PLANNER_NOT_ENABLED', '该源或主体尚未进入受控规划灰度', 422)


def public_plan(row, payload):
    # Bind values, physical column maps and policy literals stay in the private store.
    keys = ('compiler', 'source_ids', 'release_id', 'outputs', 'labels', 'column_types', 'metrics', 'relations',
            'dependencies', 'catalog_fingerprint', 'sql_sha256', 'warnings')
    return dict({k: payload[k] for k in keys}, plan_id=row['id'], plan_digest=row['digest'], expires_at=row['expires'],
                status='validated', sql=payload['sql'], parameter_count=len(payload['parameters']),
                events=['retrieved', 'selected', 'validated'])


def plan_row(db, identity, principal):
    if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{32}', identity): raise invalid('计划标识格式无效')
    row = db.execute('SELECT * FROM query_plans WHERE id=? AND namespace=? AND actor=?', (identity, namespace(), principal.subject)).fetchone()
    if not row: fail('NOT_FOUND', '计划不存在或不可访问', 404)
    rollout(principal, [row['source_id']])
    data = json.loads(row['payload'])
    if digest(data) != row['digest']: fail('PLAN_CORRUPT', '计划完整性校验失败', 503)
    return row, data


def create_plan(registry, principal, infos, intent, release_id=None, budget=None):
    if not isinstance(intent, dict) or not isinstance(intent.get('source_ids'), list) or not intent['source_ids'] or any(not isinstance(s, str) for s in intent['source_ids']): raise invalid('必须显式指定源范围')
    rollout(principal, intent['source_ids'])
    pin = registry.pin(namespace(), principal, release_id)
    pin['subject'] = principal.subject
    with registry.connection() as db:
        document, _ = registry.release_in(db, pin['release_id'], namespace())
    budget = budget or QueryBudget()
    budget.deadline = min(budget.deadline, time.monotonic() + 60)
    data = compile_plan(pin, document, intent, infos, budget)
    data['policy_fingerprint'] = policy_fingerprint(document, data['source_ids'][0])
    data['binding_digest'] = binding_digest(infos[data['source_ids'][0]])
    data['parameter_ref'] = uuid4().hex  # An opaque reference, never a low-entropy value hash.
    now = time.time(); identity = uuid4().hex
    registry.ensure_usable(namespace(), pin['release_id'])
    with registry.connection(write=True) as db:
        registry.release_in(db, pin['release_id'], namespace())
        policy_guard(registry, data, db)
        capacity(db, len(canonical(data).encode()), 1)
        db.execute('INSERT INTO query_plans VALUES(?,?,?,?,?,?,?,?,?)',
                   (identity, namespace(), principal.subject, data['source_ids'][0], pin['release_id'], canonical(data), digest(data), now, now + PLAN_TTL))
    return public_plan({'id': identity, 'digest': digest(data), 'expires': now + PLAN_TTL}, data)


def get_plan(registry, principal, identity):
    with registry.connection() as db:
        row, data = plan_row(db, identity, principal)
        registry.release_in(db, row['release_id'], namespace())
        policy_guard(registry, data, db)
        if row['expires'] <= time.time(): fail('PLAN_EXPIRED', '计划已过期，请重新规划')
        return public_plan(row, data)


def _expire(db):
    db.execute("UPDATE query_executions SET state='failed',error_code='EXECUTION_INTERRUPTED' WHERE state='running' AND lease_until<?", (time.time(),))


def get_execution(registry, principal, identity, infos):
    with registry.connection(write=True) as db:
        _expire(db)
        row = db.execute('SELECT * FROM query_executions WHERE id=? AND namespace=? AND actor=?', (identity, namespace(), principal.subject)).fetchone()
        if not row: fail('NOT_FOUND', '执行不存在或不可访问', 404)
        plan, data = plan_row(db, row['plan_id'], principal)
        registry.release_in(db, plan['release_id'], namespace())
        policy_guard(registry, data, db)
        source = data['source_ids'][0]
        if row['state'] == 'succeeded' and (source not in infos or binding_digest(infos[source]) != data['binding_digest']):
            fail('STALE_BINDING', '结果关联的源绑定已变化')
        if row['expires'] <= time.time(): fail('RESULT_EXPIRED', '结果已过期，请使用新计划及新幂等键查询')
        payload = json.loads(row['payload']) if row['payload'] else None
        if payload is not None and digest(payload) != row['digest']: fail('EXECUTION_CORRUPT', '执行产物摘要不匹配', 503)
        return dict(execution_id=identity, plan_id=row['plan_id'], state=row['state'], error_code=row['error_code'],
                    expires_at=row['expires'], result=payload)


def normalize(value):
    if value is None or type(value) in (str, int, bool): return value
    if isinstance(value, Decimal):
        if not value.is_finite(): raise invalid('结果包含非有限数值')
        return str(value)  # Preserve decimal precision in JSON; charts are only a display view.
    if isinstance(value, float) and math.isfinite(value): return value
    if isinstance(value, (datetime, date)): return value.isoformat()
    raise invalid('结果类型无法安全序列化')


def execute_plan(registry, principal, infos, plan_id, idempotency_key, budget=None):
    if not isinstance(idempotency_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', idempotency_key): raise invalid('必须提供有效执行幂等键')
    budget = budget or QueryBudget()
    budget.deadline = min(budget.deadline, time.monotonic() + 60)
    with registry.connection(write=True) as db:
        _expire(db)
        row, data = plan_row(db, plan_id, principal)
        registry.release_in(db, row['release_id'], namespace())
        policy_guard(registry, data, db)
        existing = db.execute('SELECT id,plan_id FROM query_executions WHERE namespace=? AND actor=? AND idempotency_key=?', (namespace(), principal.subject, idempotency_key)).fetchone()
        if existing:
            if existing['plan_id'] != plan_id: fail('IDEMPOTENCY_CONFLICT', '幂等键已用于另一计划')
            identity = existing['id']
        else:
            if row['expires'] <= time.time(): fail('PLAN_EXPIRED', '计划已过期，请重新规划')
            busy = db.execute("SELECT COUNT(*) FROM query_executions WHERE state='running' AND (actor=? OR (SELECT COUNT(*) FROM query_executions WHERE state='running')>=4)", (principal.subject,)).fetchone()[0]
            if busy: fail('EXECUTION_BUSY', '已有执行或全局并发已满', 429)
            capacity(db, new_rows=1)
            identity, now = uuid4().hex, time.time()
            db.execute('INSERT INTO query_executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (identity, namespace(), principal.subject, idempotency_key, plan_id, 'running', None, None, None, now, now + RESULT_TTL, now + 90))
    if existing: return get_execution(registry, principal, identity, infos)
    try:
        source = data['source_ids'][0]
        if source not in infos or binding_digest(infos[source]) != data['binding_digest']: fail('STALE_BINDING', '物理源绑定已变化', 422)
        if catalog_for(data['physical_entities'], infos[source], budget)['fingerprint'] != data['catalog_fingerprint']: fail('STALE_BINDING', '计划的数据库结构已变化', 422)
        registry.ensure_usable(namespace(), data['release_id'])
        policy_guard(registry, data)
        started = datetime.now(timezone.utc).isoformat()
        result = execute_source(infos[source], PreparedQuery(data['sql'], data['parameters']), budget)
        ended = datetime.now(timezone.utc).isoformat()
        if result['status'] != 'success': fail('SOURCE_QUERY_FAILED', '查询失败，未产生完整答案', 502)
        if result.get('truncated'): fail('PARTIAL_RESULT', '结果超出执行限额，不作为完整指标答案', 422)
        if result['columns'] != data['outputs']: fail('STALE_BINDING', '实际输出与批准计划不一致', 422)
        dim_count = len(data['outputs']) - len(data['metrics'])
        for values in result['rows']:
            if len(values) != len(data['outputs']): raise invalid('结果列数不一致')
            for value in values[dim_count:]:
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float, Decimal))): raise invalid('指标结果必须为数值或 NULL')
        if catalog_for(data['physical_entities'], infos[source], budget)['fingerprint'] != data['catalog_fingerprint']: fail('STALE_BINDING', '执行期间结构变化，结果不发布', 422)
        rollout(principal, data['source_ids']); registry.ensure_usable(namespace(), data['release_id'])
        policy_guard(registry, data)
        rows = [[normalize(v) for v in values] for values in result['rows']]
        evidence = dict(plan_id=plan_id, execution_id=identity, release_id=data['release_id'], compiler=data['compiler'],
                        metrics=data['metrics'], relations=data['relations'], dependency_versions=data['dependencies'],
                        sql_sha256=data['sql_sha256'], parameter_ref=data['parameter_ref'], catalog_fingerprint=data['catalog_fingerprint'],
                        source_id=source, database=infos[source]['db'], scope=data['source_ids'],
                        policy_context_ref=digest({'subject': principal.subject, 'source_ids': data['source_ids'], 'dependencies': data['dependencies']}),
                        read_started_at=started, read_finished_at=ended, snapshot='statement_only',
                        row_count=len(rows), truncated=False, checks=['scope', 'release', 'schema_before_after', 'output_columns', 'numeric_values', 'complete'],
                        events=['retrieved', 'selected', 'validated', 'executed', 'verified'], sql_calls=budget.calls)
        payload = dict(columns=data['outputs'], labels=data['labels'], column_types=data['column_types'], rows=rows,
                       row_count=len(rows), sql=data['sql'], evidence=evidence,
                       content='已按批准指标口径完成查询；NULL 表示无适用值或零分母，不作因果解释。', warnings=data['warnings'])
        if len(canonical(payload).encode()) > 6 * 1024 * 1024: fail('RESULT_TOO_LARGE', '执行证据和结果超过存储预算', 422)
        state, error_code = 'succeeded', None
    except Exception as error:
        payload, state = None, 'failed'
        error_code = error.code if isinstance(error, ServiceError) else 'EXECUTION_FAILED'
    with registry.connection(write=True) as db:
        _expire(db)
        current = db.execute('SELECT state FROM query_executions WHERE id=?', (identity,)).fetchone()
        if current['state'] == 'running':
            if payload:
                try: capacity(db, len(canonical(payload).encode()))
                except ServiceError:
                    payload, state, error_code = None, 'failed', 'PLANNER_CAPACITY'
            db.execute('UPDATE query_executions SET state=?,payload=?,digest=?,error_code=? WHERE id=?',
                       (state, canonical(payload) if payload else None, digest(payload) if payload else None, error_code, identity))
    return get_execution(registry, principal, identity, infos)


def check_question(registry, principal, source_ids, question, release_id=None):
    if not isinstance(source_ids, list) or len(source_ids) != 1 or not isinstance(source_ids[0], str): raise invalid('需明确一个源')
    rollout(principal, source_ids)
    if not isinstance(question, str) or not 1 <= len(question) <= 4096: raise invalid('问题无效')
    pin = registry.pin(namespace(), principal, release_id)
    matches = [e for e in pin['entities'].values() if e['kind'] == 'Metric' and e['source_ids'] == source_ids
               and question.strip().casefold() in [n.casefold() for n in [e['name'], *e['synonyms']]]]
    result = {'release_id': pin['release_id'], 'status': 'unanswerable', 'metric_ids': [], 'missing': []}
    if len(matches) > 1: return dict(result, status='ambiguous', metric_ids=[m['id'] for m in matches])
    if matches:
        contracts = [e for e in pin['entities'].values() if e['kind'] == 'MetricContract' and e['attributes']['metric_id'] == matches[0]['id']]
        if len(contracts) > 1: return dict(result, status='ambiguous')
        if contracts:
            a = contracts[0]['attributes']
            missing = (['time'] if a['time_field'] else []) + ['filter:' + k for k in a['required_filters']]
            return dict(result, status='needs_clarification' if missing else 'ready_for_validation', metric_ids=[matches[0]['id']], missing=missing,
                        contract_id=contracts[0]['id'], dimensions=a['dimensions'], relation_ids=a['relation_ids'], timezone=a['timezone'])
    return result


def chat(principal, infos, source_ids, options):
    if not isinstance(options, dict) or set(options) != {'plan_id', 'idempotency_key'}: raise invalid('计划查询只接受 plan_id 和 idempotency_key')
    registry = Registry()
    with registry.connection() as db:
        _, data = plan_row(db, options['plan_id'], principal)
        if set(source_ids) != set(data['source_ids']): raise invalid('聊天源范围与计划不一致')
    receipt = execute_plan(registry, principal, infos, **options)
    if receipt['state'] != 'succeeded': fail(receipt['error_code'] or 'EXECUTION_RUNNING', '计划尚未成功完成，不能生成完整答案', 409)
    from server.api_helpler import Helper
    from server.execution import bounded_int
    result = receipt['result']; limit = bounded_int('MAX_ROW_COUNT_RETURN', 50, 1, 10000)
    response = dict(status='success', content=result['content'], sql=result['sql'], chartType='BarChartPic',
                    chartData=Helper.mk_chart_data(result['labels'], result['column_types'], result, limit),
                    mdData=Helper.mk_md_table(result['labels'], result, limit), extra='', truncated=False,
                    sources=[{'source_id': source_ids[0], 'status': 'success'}], ontology=result['evidence'])
    if result['row_count'] > limit:
        from server.exports import save_export
        response['extra'] = save_export(principal, source_ids, result['columns'], result['rows'],
                                        release={'namespace': namespace(), 'release_id': data['release_id'], 'execution_id': receipt['execution_id']})
        response['content'] += f' 当前展示 {limit} 行，其余结果可通过受控下载访问。'
    registry.ensure_usable(namespace(), data['release_id'])
    policy_guard(registry, data)
    return response
