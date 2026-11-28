"""Single-host governance scans. Detection isolates; only reviewed remediation can resolve."""
import json
import os
import re
import time
from uuid import uuid4

from ontology.registry import Registry, authorized, fail
from ontology.runtime import namespace
from ontology.v2 import canonical, digest, invalid
from ontology.discovery.mysql import Collector
from ontology.discovery.workflow import binding_digest
from server.execution import QueryBudget
from server.errors import ServiceError
from . import rules


def enabled_sources():
    try:
        sources = json.loads(os.getenv('ONTOLOGY_GOVERNANCE_SOURCES', '[]'))
        if not isinstance(sources, list) or any(not isinstance(s, str) or not s for s in sources) or len(set(sources)) != len(sources): raise ValueError()
        return set(sources)
    except (ValueError, TypeError): fail('GOVERNANCE_CONFIGURATION', '治理源配置无效', 503)


def checked_payload(row):
    try:
        payload = json.loads(row['payload'])
        if digest(payload) != row['digest']: raise ValueError()
        return payload
    except (ValueError, TypeError): fail('GOVERNANCE_CORRUPT', '治理证据完整性校验失败', 503)


def physical(e, entities):
    a = e['attributes']
    if e['kind'] == 'DataSource': return ['source', e['source_ids'][0]]
    if e['kind'] == 'Dataset': return ['table', a['physical_name']]
    if e['kind'] == 'Field': return ['column', entities[a['dataset_id']]['attributes']['physical_name'], a['physical_name']]
    if e['kind'] == 'Relation': return ['table', entities[a['right_dataset']]['attributes']['physical_name']]
    return ['entity', e['id']]


def closure(entities, identities):
    found, todo = set(), list(identities)
    while todo:
        k = todo.pop()
        if k not in found and k in entities:
            found.add(k); todo.extend(entities[k]['dependencies'])
    return found


def impact(entities, seeds):
    found = set(seeds)
    while True:
        expanded = found | {k for k, e in entities.items() if found.intersection(e['dependencies'])}
        if expanded == found: return sorted(found)
        found = expanded


def guard_in(db, ns, document, identities):
    """Always enforce existing isolation, including when the scan switch is later disabled."""
    entities = {e['id']: e for e in document['entities'] if e['availability'] != 'candidate'}
    targets = closure(entities, identities)
    keys = {canonical(physical(entities[k], entities)) for k in targets}
    sources = {s for k in targets for s in entities[k]['source_ids']}
    for row in db.execute("SELECT * FROM governance_incidents WHERE namespace=? AND state!='resolved'", (ns,)):
        if row['source_id'] not in sources: continue
        data = json.loads(row['payload'])
        if targets.intersection(data['targets']) or keys.intersection(canonical(k) for k in data['physical_keys']):
            fail('GOVERNANCE_BLOCKED', '相关资产存在未处置治理问题，请完成复验和审核', 409)


def guard(registry, ns, release, identities):
    with registry.connection() as db:
        doc, _ = registry.release_in(db, release, ns)
        guard_in(db, ns, doc, identities)


def status(registry, principal, identity, release_id=None):
    pin = registry.pin(namespace(), principal, release_id)
    if identity not in pin['entities']: fail('NOT_FOUND', '对象不存在或不可访问', 404)
    try:
        guard(registry, namespace(), pin['release_id'], [identity])
        blocked = False
    except ServiceError as error:
        if error.code != 'GOVERNANCE_BLOCKED': raise
        blocked = True
    return dict(release_id=pin['release_id'], entity_id=identity, blocked=blocked)


def scoped_document(document, source):
    return {e['id']: e for e in document['entities'] if e['availability'] != 'candidate' and e['source_ids'] == [source]}


def inspect_catalog(entities, catalog, previous=None):
    tables = {t['name']: t for t in catalog['tables']}
    issues, changes = [], []
    def problem(kind, target):
        issues.append({'kind': kind, 'targets': [target]})
    for identity, e in entities.items():
        a = e['attributes']
        if e['kind'] == 'Dataset':
            actual = tables.get(a['physical_name'])
            if not actual or actual.get('kind', 'BASE TABLE') != 'BASE TABLE': problem('table_missing_or_kind_changed', identity); continue
            primary = [entities[k]['attributes']['physical_name'] for k in a['primary_key']]
            if primary != actual['primary_key']: problem('primary_key_changed', identity)
        elif e['kind'] == 'Field':
            table = entities[a['dataset_id']]['attributes']['physical_name']
            col = next((c for c in tables.get(table, {}).get('columns', []) if c['name'] == a['physical_name']), None)
            if not col: problem('column_missing', identity)
            elif col['dtype'].lower() != a['dtype'].lower(): problem('column_type_changed', identity)
        elif e['kind'] == 'Relation' and a['cardinality'] == 'many-to-one':
            table = entities[a['right_dataset']]['attributes']['physical_name']
            keys = [entities[k]['attributes']['physical_name'] for k in a['right_keys']]
            if keys not in tables.get(table, {}).get('unique_keys', {}).values(): problem('unique_constraint_changed', identity)
    if previous:
        old = {t['name']: t for t in previous['tables']}
        for name, table in tables.items():
            if name not in old: changes.append({'kind': 'table_added', 'table': name}); continue
            before, after = ({c['name']: c for c in t['columns']} for t in (old[name], table))
            for column in sorted(set(before) | set(after)):
                kind = 'column_added' if column not in before else 'column_removed' if column not in after else 'column_changed' if before[column] != after[column] else None
                if kind: changes.append({'kind': kind, 'table': name, 'column': column})
                if column in before and column in after and any(before[column].get(k) != after[column].get(k) for k in ('collation', 'nullable')):
                    for e in entities.values():
                        if e['kind'] == 'Field' and e['attributes']['physical_name'] == column and entities[e['attributes']['dataset_id']]['attributes']['physical_name'] == name:
                            problem('column_constraint_changed', e['id'])
            if old[name]['unique_keys'] != table['unique_keys']:
                changes.append({'kind': 'indexes_changed', 'table': name})
                if any(key not in table['unique_keys'].values() for key in old[name]['unique_keys'].values()):
                    for e in entities.values():
                        if e['kind'] == 'Dataset' and e['attributes']['physical_name'] == name:
                            problem('unique_constraint_removed', e['id'])
        for name in sorted(set(old) - set(tables)): changes.append({'kind': 'table_removed', 'table': name})
        if previous['relations'] != catalog['relations']: changes.append({'kind': 'foreign_keys_changed'})
    return issues, changes


def storage_guard(db):
    count, size = db.execute('SELECT (SELECT COUNT(*) FROM governance_runs)+(SELECT COUNT(*) FROM governance_incidents), '
                            '(SELECT COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) FROM governance_runs)+'
                            '(SELECT COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) FROM governance_incidents)').fetchone()
    # Reserve a run plus at most 32 incidents and their bounded aggregate payloads.
    if count + 33 > 10000 or size + 4 * 1024 * 1024 > 128 * 1024 * 1024:
        fail('GOVERNANCE_CAPACITY', '治理证据存储已满，请归档', 503)


def scan(registry, principal, infos, source_id, idempotency_key):
    if not isinstance(source_id, str) or not isinstance(idempotency_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', idempotency_key): raise invalid('治理源或幂等键无效')
    authorized(principal, [source_id], admin=True)
    if source_id not in enabled_sources(): fail('GOVERNANCE_DISABLED', '此源尚未开启治理采集', 422)
    if source_id not in infos: fail('NOT_FOUND', '对象不存在或不可访问', 404)
    ns = namespace(); pin = registry.pin(ns, principal)
    with registry.connection() as db: doc, _ = registry.release_in(db, pin['release_id'], ns)
    entities = scoped_document(doc, source_id)
    if any(not set(e['dependencies']) <= set(entities) for e in entities.values()): raise invalid('治理采集只支持依赖闭合的单源资产')
    bindings = [e for e in entities.values() if e['kind'] == 'DataSource']
    if len(bindings) != 1 or bindings[0]['attributes']['database'] != infos[source_id]['db']: fail('STALE_BINDING', '治理源与批准绑定不一致', 422)
    tables = {e['attributes']['physical_name']: {'columns': [f['attributes']['physical_name'] for f in entities.values() if f['kind'] == 'Field' and f['attributes']['dataset_id'] == e['id']]} for e in entities.values() if e['kind'] == 'Dataset'}
    if not 1 <= len(tables) <= 8 or any(not 1 <= len(t['columns']) <= 128 for t in tables.values()): raise invalid('每轮治理范围限 1–8 张表、每表 1–128 个批准字段')
    approved_rules = [e for e in entities.values() if e['kind'] == 'QualityRule']
    if len(approved_rules) > 8: raise invalid('每轮最多 8 条质量规则')
    with registry.connection(write=True) as db:
        db.execute("UPDATE governance_runs SET state='interrupted' WHERE state='running' AND lease_until<?", (time.time(),))
        existing = db.execute('SELECT * FROM governance_runs WHERE namespace=? AND actor=? AND idempotency_key=?', (ns, principal.subject, idempotency_key)).fetchone()
        if existing:
            if existing['source_id'] != source_id: fail('IDEMPOTENCY_CONFLICT', '治理幂等键已用于其他源')
            identity = existing['id']
        else:
            if db.execute("SELECT 1 FROM governance_runs WHERE state='running' LIMIT 1").fetchone(): fail('GOVERNANCE_BUSY', '已有治理采集运行', 429)
            storage_guard(db)
            identity = uuid4().hex
            db.execute('INSERT INTO governance_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)', (identity, ns, source_id, principal.subject, idempotency_key, pin['release_id'], 'running', None, None, time.time(), time.time()+90))
    if existing: return get_run(registry, principal, identity)
    budget = QueryBudget(); budget.deadline = min(budget.deadline, time.monotonic()+60)
    binding = binding_digest(infos[source_id]); findings, quality, changes = [], [], []
    catalog = None
    try:
        collector = Collector(infos[source_id], {'tables': tables, 'relations': []}, budget)
        catalog = collector.catalog(tolerate_missing=True)
        with registry.connection() as db:
            previous_row = db.execute("SELECT * FROM governance_runs WHERE namespace=? AND source_id=? AND state='complete' ORDER BY created DESC LIMIT 1", (ns, source_id)).fetchone()
            previous = checked_payload(previous_row) if previous_row else None
        findings, changes = inspect_catalog(entities, catalog, previous['catalog'] if previous and previous['binding_digest'] == binding else None)
        for rule in approved_rules:
            try:
                result = rules.evaluate(rule, entities, infos[source_id], budget)
                quality.append(result)
                if result['status'] != 'passed': findings.append({'kind': 'quality_failed', 'targets': [rule['id'], *rule['attributes']['target_ids']]})
            except ServiceError as error:
                quality.append({'rule_id': rule['id'], 'status': 'unknown', 'error_code': error.code})
                findings.append({'kind': 'quality_unknown', 'targets': [rule['id'], *rule['attributes']['target_ids']]})
        if collector.catalog(tolerate_missing=True)['fingerprint'] != catalog['fingerprint']: fail('SCHEMA_CHANGED', '采集期间目录改变')
        state, error_code = 'complete', None
    except Exception as error:
        state, error_code = 'failed', error.code if isinstance(error, ServiceError) else 'SOURCE_UNAVAILABLE'
        findings.append({'kind': 'probe_unknown', 'targets': [bindings[0]['id']]})
    payload = dict(catalog=catalog, binding_digest=binding, findings=findings, changes=changes, quality=quality,
                   error_code=error_code, sql_calls=budget.calls, source_id=source_id, release_id=pin['release_id'])
    incident_data = [dict(f, impacted=impact(entities, f['targets']), physical_keys=[physical(entities[k], entities) for k in f['targets']]) for f in findings[:33]]
    if (len(findings) > 32 or len(canonical(payload).encode()) > 2 * 1024 * 1024
            or len(canonical(incident_data).encode()) > 2 * 1024 * 1024):
        # Oversized/incomplete evidence must still leave an explicit blocking record.
        state = 'failed'
        findings = [{'kind': 'evidence_limit', 'targets': [bindings[0]['id']]}]
        incident_data = [dict(findings[0], impacted=[bindings[0]['id']], impact_truncated=True,
                              physical_keys=[physical(bindings[0], entities)])]
        payload.update(catalog=None, findings=findings, changes=[], quality=[], error_code='GOVERNANCE_LIMIT')
    with registry.connection(write=True) as db:
        current = db.execute('SELECT state,lease_until FROM governance_runs WHERE id=?', (identity,)).fetchone()
        if current['state'] != 'running' or current['lease_until'] <= time.time(): fail('GOVERNANCE_INTERRUPTED', '采集租约已失效')
        # Even a stale scan can isolate; it can never mark a new release healthy or resolve an incident.
        db.execute('UPDATE governance_runs SET state=?,payload=?,digest=? WHERE id=?', (state, canonical(payload), digest(payload), identity))
        for finding, data in zip(findings, incident_data):
            targets = finding['targets']; key = digest(finding)
            old = db.execute("SELECT * FROM governance_incidents WHERE namespace=? AND source_id=? AND issue_key=? AND state!='resolved'", (ns, source_id, key)).fetchone()
            if old:
                db.execute("UPDATE governance_incidents SET last_run=?,revision=revision+1,updated=? WHERE id=?", (identity, time.time(), old['id']))
            else:
                db.execute('INSERT INTO governance_incidents VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (uuid4().hex, ns, source_id, key, pin['release_id'], entities[targets[0]]['owner'], 'open', 1, canonical(data), identity, time.time()))
        registry.audit_in(db, ns, principal.subject, 'governance_scanned', identity, {'state': state, 'finding_count': len(findings), 'release_id': pin['release_id']})
    return get_run(registry, principal, identity)


def get_run(registry, principal, identity):
    authorized(principal, [], admin=True)
    with registry.connection() as db:
        row = db.execute('SELECT * FROM governance_runs WHERE id=? AND namespace=?', (identity, namespace())).fetchone()
        if not row: fail('NOT_FOUND', '治理记录不存在或不可访问', 404)
        authorized(principal, [row['source_id']], admin=True)
        return dict(id=row['id'], source_id=row['source_id'], release_id=row['release_id'], state=row['state'], created=row['created'],
                    digest=row['digest'], evidence=checked_payload(row) if row['payload'] else None)


def incidents(registry, principal):
    authorized(principal, [], admin=True)
    with registry.connection() as db:
        rows = db.execute('SELECT * FROM governance_incidents WHERE namespace=? ORDER BY updated DESC LIMIT 1000', (namespace(),)).fetchall()
        return {'incidents': [dict(row, payload=json.loads(row['payload'])) for row in rows if row['source_id'] in principal.sources]}


def transition(registry, principal, infos, identity, expected_revision, action, reason, owner=None, run_id=None):
    authorized(principal, [], admin=True)
    if action not in ('acknowledge', 'resolve') or type(expected_revision) is not int or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000: raise invalid('治理处置需要动作、版本和理由')
    if action == 'acknowledge' and (not isinstance(owner, str) or not 1 <= len(owner.strip()) <= 160): raise invalid('确认处置必须指定 owner')
    with registry.connection(write=True) as db:
        row = db.execute('SELECT * FROM governance_incidents WHERE id=? AND namespace=?', (identity, namespace())).fetchone()
        if not row: fail('NOT_FOUND', '治理记录不存在或不可访问', 404)
        authorized(principal, [row['source_id']], admin=True)
        if row['revision'] != expected_revision or row['state'] == 'resolved': fail('REVISION_CONFLICT', '治理状态已变化')
        if action == 'resolve':
            if row['state'] != 'acknowledged': fail('REVIEW_REQUIRED', '先确认 owner 和处置责任')
            run = db.execute("SELECT * FROM governance_runs WHERE id=? AND namespace=? AND source_id=? AND state='complete'", (run_id, namespace(), row['source_id'])).fetchone()
            active = registry.active_in(db, namespace())['release_id']
            last = db.execute('SELECT created FROM governance_runs WHERE id=?', (row['last_run'],)).fetchone()
            if not run or run['release_id'] != active or active == row['release_id'] or run['created'] <= last['created'] or run['created']+300 < time.time():
                fail('REVERIFY_REQUIRED', '需新审批发布后五分钟内的完整复验，旧证据不可解隔离')
            evidence = checked_payload(run)
            if row['source_id'] not in infos or evidence['binding_digest'] != binding_digest(infos[row['source_id']]) or evidence['findings']:
                fail('REVERIFY_REQUIRED', '复验仍有问题或绑定已变化')
            registry.release_in(db, active, namespace())
        state = 'acknowledged' if action == 'acknowledge' else 'resolved'
        db.execute('UPDATE governance_incidents SET owner=?,state=?,revision=revision+1,updated=? WHERE id=?', (owner if action == 'acknowledge' else row['owner'], state, time.time(), identity))
        registry.audit_in(db, namespace(), principal.subject, 'governance_' + state, identity, {'reason': reason, 'run_id': run_id, 'owner': owner or row['owner']})
    return {'id': identity, 'state': state, 'revision': expected_revision+1}
