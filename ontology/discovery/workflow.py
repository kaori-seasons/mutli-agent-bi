"""Durable bounded discovery runs and explicit human handoff into the P1 approval workflow."""
import copy
import json
import re
import time
from uuid import uuid4
from server.errors import ServiceError
from ontology.registry import authorized, fail
from ontology.v2 import canonical, digest, validate, invalid
from .policy import load_policy
from .mysql import Collector
from .compiler import compile_candidates

EVIDENCE_TTL = 24 * 3600


def binding_digest(info):
    # Rotation of a password does not change the physical binding; changing the source endpoint does.
    return digest({key: info[key] for key in ('source_id', 'host', 'port', 'db', 'user')})


def _expire(db):
    timestamp = time.time()
    expired = db.execute("SELECT id,namespace FROM discovery_runs WHERE state='running' AND lease_until<?", (timestamp,)).fetchall()
    for row in expired:
        db.execute('INSERT INTO audit(namespace,actor,action,object_id,details,created) VALUES(?,?,?,?,?,?)',
                   (row['namespace'], 'system:lease', 'discovery_interrupted', row['id'], '{}', timestamp))
    db.execute("UPDATE discovery_runs SET state='failed',error_code='RUN_INTERRUPTED',updated=? WHERE state='running' AND lease_until<?",
               (timestamp, timestamp))


def _row(db, identity, principal, namespace):
    authorized(principal, [], admin=True)
    row = db.execute('SELECT * FROM discovery_runs WHERE id=? AND namespace=?', (identity, namespace)).fetchone()
    if not row:
        fail('NOT_FOUND', '采集作业不存在或不可访问', 404)
    authorized(principal, [row['source_id']], admin=True)
    return row


def get_run(registry, namespace, identity, principal):
    with registry.connection(write=True) as db:
        _expire(db)
        row = _row(db, identity, principal, namespace)
        result = {key: row[key] for key in ('id', 'namespace', 'source_id', 'actor', 'state', 'digest', 'error_code', 'created', 'updated')}
        artifact = json.loads(row['payload']) if row['payload'] else None
        if artifact is not None and digest(artifact) != row['digest']:
            fail('REGISTRY_CORRUPT', '采集产物摘要校验失败', 503)
        result['artifact'] = artifact
        return result


def list_runs(registry, namespace, principal):
    authorized(principal, [], admin=True)
    with registry.connection(write=True) as db:
        _expire(db)
        rows = db.execute('SELECT id,source_id,actor,state,error_code,created,updated FROM discovery_runs WHERE namespace=? ORDER BY created DESC LIMIT 100', (namespace,)).fetchall()
        return {'runs': [dict(row) for row in rows if row['source_id'] in principal.sources]}


def run(registry, namespace, principal, infos, source_id, idempotency_key, tables=None, upstream=None):
    authorized(principal, [source_id], admin=True)
    if not isinstance(idempotency_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', idempotency_key):
        raise invalid('采集必须携带有效幂等键')
    if source_id not in infos or infos[source_id]['db'] in ('information_schema', 'mysql', 'sys', 'performance_schema'):
        raise invalid('采集数据源无效')
    policy = load_policy(source_id, tables)
    request_digest = digest({'policy': policy, 'upstream': upstream, 'binding': binding_digest(infos[source_id])})
    identity, created = uuid4().hex, time.time()
    with registry.connection(write=True) as db:
        _expire(db)
        existing = db.execute('SELECT id,request_digest FROM discovery_runs WHERE namespace=? AND actor=? AND idempotency_key=?',
                              (namespace, principal.subject, idempotency_key)).fetchone()
        if existing:
            if existing['request_digest'] != request_digest:
                fail('IDEMPOTENCY_CONFLICT', '幂等键已用于不同采集范围')
            identity = existing['id']
        else:
            busy = db.execute("SELECT COUNT(*) FROM discovery_runs WHERE state='running' AND (source_id=? OR (SELECT COUNT(*) FROM discovery_runs WHERE state='running')>=4)", (source_id,)).fetchone()[0]
            if busy:
                fail('DISCOVERY_BUSY', '该源已有采集或全局采集并发已满', 429)
            db.execute('INSERT INTO discovery_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (identity, namespace, source_id, principal.subject, idempotency_key, request_digest, 'running', None, None, None,
                        created, created, created + 90))
            registry.audit_in(db, namespace, principal.subject, 'discovery_started', identity, {'source_id': source_id})
    if existing:
        return get_run(registry, namespace, identity, principal)
    try:
        observations = Collector(infos[source_id], policy).collect()
        document = compile_candidates(observations, namespace, principal.subject, identity, upstream)
        artifact = {'policy': policy, 'policy_digest': digest(policy), 'binding_digest': binding_digest(infos[source_id]), 'observations': observations,
                    'document': document, 'expires_at': created + EVIDENCE_TTL, 'automatic': True}
        if len(canonical(artifact).encode()) > 6 * 1024 * 1024:
            fail('DISCOVERY_LIMIT', '采集产物超出容量限制', 422)
        state = 'complete' if observations['schema_stable'] and not observations['errors'] else 'partial'
        error_code = None
    except Exception as error:
        artifact, state = None, 'failed'
        error_code = error.code if isinstance(error, ServiceError) else 'DISCOVERY_FAILED'
    with registry.connection(write=True) as db:
        _expire(db)
        row = _row(db, identity, principal, namespace)
        if row['state'] == 'running':
            db.execute('UPDATE discovery_runs SET state=?,payload=?,digest=?,error_code=?,updated=? WHERE id=?',
                       (state, canonical(artifact) if artifact else None, digest(artifact) if artifact else None, error_code, time.time(), identity))
            registry.audit_in(db, namespace, principal.subject, 'discovery_finished', identity, {'state': state, 'error_code': error_code})
    return get_run(registry, namespace, identity, principal)


def assert_current(artifact, info):
    if artifact['expires_at'] <= time.time():
        fail('STALE_EVIDENCE', '采集证据已过期，请重新采集', 409)
    if artifact['binding_digest'] != binding_digest(info):
        fail('STALE_BINDING', '数据源连接绑定已变化，请重新采集', 409)
    policy = load_policy(info['source_id'], list(artifact['policy']['tables']))
    if digest(policy) != artifact['policy_digest']:
        fail('STALE_EVIDENCE', '采集策略已变化，请重新采集', 409)
    if Collector(info, policy).catalog()['fingerprint'] != artifact['observations']['catalog']['fingerprint']:
        fail('STALE_BINDING', '数据库结构与采集快照不一致，请重新采集', 409)


def attach_review(db, changeset_id, link, principal):
    row = _row(db, link['run_id'], principal, link['namespace'])
    if row['state'] != 'complete' or row['digest'] != link['digest'] or digest(json.loads(row['payload'])) != row['digest']:
        fail('STALE_EVIDENCE', '只能移交完整且摘要匹配的候选产物')
    artifact = json.loads(row['payload'])
    if artifact['expires_at'] <= time.time():
        fail('STALE_EVIDENCE', '证据已过期')
    db.execute('INSERT INTO discovery_review_links VALUES(?,?,?,?)',
               (changeset_id, row['id'], row['digest'], artifact['expires_at']))


def check_review_link(db, changeset_id):
    link = db.execute('SELECT * FROM discovery_review_links WHERE changeset_id=?', (changeset_id,)).fetchone()
    if not link:
        return
    row = db.execute('SELECT * FROM discovery_runs WHERE id=?', (link['run_id'],)).fetchone()
    if (not row or row['state'] != 'complete' or link['expires'] <= time.time() or row['digest'] != link['artifact_digest']
            or digest(json.loads(row['payload'])) != row['digest']):
        fail('STALE_EVIDENCE', '审核关联的证据已失效')


def check_current_review(registry, changeset_id, principal, infos):
    with registry.connection() as db:
        link = db.execute('SELECT run_id FROM discovery_review_links WHERE changeset_id=?', (changeset_id,)).fetchone()
        if not link:
            return
        row = registry.change_in(db, changeset_id, principal)
        if row['status'] == 'published':
            return  # Preserve P1 idempotent receipt semantics; this does not reactivate anything.
        check_review_link(db, changeset_id)
        run_row = _row(db, link['run_id'], principal, row['namespace'])
        artifact = json.loads(run_row['payload']); source = run_row['source_id']
    if source not in infos:
        fail('STALE_BINDING', '采集源配置已移除')
    assert_current(artifact, infos[source])


def prepare_review(registry, namespace, identity, principal, infos, expected_digest, entity_ids, edits, expected_active, expected_generation, reason):
    run_row = get_run(registry, namespace, identity, principal)
    if run_row['state'] != 'complete' or run_row['digest'] != expected_digest:
        fail('DISCOVERY_NOT_READY', '只有完整且摘要匹配的候选可移交审核')
    if (not isinstance(entity_ids, list) or not entity_ids or any(not isinstance(x, str) for x in entity_ids)
            or len(set(entity_ids)) != len(entity_ids) or not isinstance(edits, dict)
            or not set(edits) <= set(entity_ids) or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000):
        raise invalid('请选择候选实体并填写人工核验理由')
    artifact = run_row['artifact']
    if run_row['source_id'] not in infos:
        fail('STALE_BINDING', '采集源配置已移除')
    assert_current(artifact, infos[run_row['source_id']])
    by_id = {e['id']: copy.deepcopy(e) for e in artifact['document']['entities']}
    if not set(entity_ids) <= set(by_id):
        raise invalid('选择了候选集合之外的实体')
    closure, pending = set(), list(entity_ids)
    while pending:
        key = pending.pop()
        if key not in closure:
            closure.add(key)
            e = by_id[key]
            pending.extend(e['dependencies'])
            if e['kind'] == 'Dataset':
                pending.extend(e['attributes']['primary_key'])
    for e in by_id.values():
        if e['kind'] == 'Evidence' and set(e['attributes']['target_ids']) <= closure:
            closure.add(e['id'])
    for key, edit in edits.items():
        if not isinstance(edit, dict) or set(edit) - {'name', 'description', 'owner', 'synonyms', 'attributes'}:
            raise invalid('人工修订字段不受支持')
        if 'attributes' in edit and by_id[key]['kind'] not in ('Metric', 'BusinessConcept', 'Relation'):
            raise invalid('物理绑定和采集证据不能通过人工修订改写')
        if 'attributes' in edit and by_id[key]['kind'] == 'Relation':
            old, new = by_id[key]['attributes'], edit['attributes']
            if not isinstance(new, dict) or any(new.get(k) != old[k] for k in ('left_dataset', 'left_keys', 'right_dataset', 'right_keys')):
                raise invalid('关系端点与证据不一致')
        by_id[key].update(copy.deepcopy(edit))
    validate(dict(artifact['document'], entities=list(by_id.values())))
    for key in closure:
        e = by_id[key]; a = e['attributes']
        if e['kind'] == 'Metric' and (not all(a.get(k) for k in ('expression', 'grain', 'unit', 'time_definition', 'aggregation'))
                                    or a['aggregation'] not in ('count', 'sum', 'avg', 'min', 'max')):
            raise invalid('指标需补齐表达式、粒度、单位、时间和聚合口径')
        if e['kind'] == 'BusinessConcept' and not a.get('definition', '').strip():
            raise invalid('业务概念必须补充人工确认的定义')
        if e['kind'] == 'Relation':
            evidence = [x for x in by_id.values() if x['kind'] == 'Evidence' and key in x['attributes']['target_ids']]
            observation = json.loads(evidence[0]['attributes']['summary']) if evidence else {}
            if observation.get('validation') != 'sampled_pass' or a['cardinality'] not in ('many-to-one', 'one-to-one') or a['join_type'] == 'unknown':
                raise invalid('关系证据未通过或尚未确认方向/基数；不能移交为参考定义')
            if a['cardinality'] == 'one-to-one':
                raise invalid('当前证据只验证目标端唯一性，不能据此批准一对一')
        e['availability'] = 'reference'
        e['legacy']['human_handoff'] = {'actor': principal.subject, 'reason': reason}
    # Keep existing published content intact unless the human explicitly selects the same stable ID.
    with registry.connection() as db:
        state = registry.active_in(db, namespace)
        if state != {'release_id': expected_active, 'generation': expected_generation}:
            fail('REVISION_CONFLICT', '活动版本已变化，请重新核对差异')
        base = registry.release_in(db, expected_active, namespace, usable=False)[0] if expected_active else dict(schema_version=2, namespace=namespace, entities=[], migration={})
    selected = {key: by_id[key] for key in closure}
    current = {e['id']: e for e in base['entities']}
    # Auto-included dependencies must not overwrite human-owned approved definitions.
    for key in (set(selected) & set(current)) - set(entity_ids):
        selected[key] = current[key]
    current.update(selected)
    document = dict(base, entities=list(current.values()), migration=dict(base['migration'], discovery_handoff={
        'run_id': identity, 'artifact_digest': expected_digest, 'selected_ids': entity_ids, 'reason': reason}))
    validate(document)
    return registry.propose(document, principal, expected_active, expected_generation, infos,
                            discovery_link={'run_id': identity, 'namespace': namespace, 'digest': expected_digest})
