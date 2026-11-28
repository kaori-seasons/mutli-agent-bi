"""Single-host transactional registry: immutable proposals/releases and CAS activation."""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from server.errors import ServiceError
from .v2 import canonical, digest, validate, invalid
from .service import tokens


def fail(code, message, status=409):
    raise ServiceError(code, message, status)


def scope_of(document):
    return set(s for e in document['entities'] for s in e['source_ids'])


def authorized(principal, sources, admin=False):
    if not principal or (admin and not principal.admin):
        fail('FORBIDDEN', '需要可信的本体管理权限', 403)
    if not set(sources) <= principal.sources:
        fail('NOT_FOUND', '对象不存在或不可访问', 404)


def content(entity):
    return {k: v for k, v in entity.items() if k != 'version'}


def validate_executable(document, infos):
    """Bind approved template tables/columns to explicit physical dependencies, never LLM metadata."""
    from sqlglot import exp
    from server.query_policy import bind_template, parse_read
    from server.federation import validate_contract, validate_merge_query
    by_id = {e['id']: e for e in document['entities']}
    for e in document['entities']:
        if e['availability'] != 'executable':
            continue
        a = e['attributes']
        prepared = bind_template(a['sql'], [p['example'] for p in a['parameters']])
        closure, pending = set(), list(e['dependencies'])
        while pending:
            identity = pending.pop()
            if identity not in closure:
                closure.add(identity)
                pending.extend(by_id[identity]['dependencies'])
        datasets = [by_id[k] for k in closure if by_id[k]['kind'] == 'Dataset']
        fields = [by_id[k] for k in closure if by_id[k]['kind'] == 'Field']
        sources = [by_id[k] for k in closure if by_id[k]['kind'] == 'DataSource']
        if len(datasets) != 1:
            raise invalid('P1 可执行模板必须显式依赖一个物理数据集及使用的字段')
        dataset = datasets[0]
        if set(dataset['source_ids']) != set(e['source_ids']):
            raise invalid('模板与物理数据集源范围必须一致')
        physical = dataset['attributes']['physical_name']
        declared = {c['attributes']['physical_name'] for c in fields if c['attributes']['dataset_id'] == dataset['id']}
        for source in e['source_ids']:
            if source not in infos:
                raise invalid('模板引用未配置的数据源')
            bindings = [s for s in sources if s['source_ids'] == [source]]
            if (len(bindings) != 1 or bindings[0]['attributes']['database'] != infos[source]['db']
                    or bindings[0]['attributes']['credential_ref'] != 'source:' + source):
                fail('STALE_BINDING', '发布中的数据源绑定与当前配置不一致', 422)
            tree = parse_read(prepared.sql, infos[source]['db'], allow_parameters=bool(prepared.parameters))
            tables = list(tree.find_all(exp.Table))
            if (not isinstance(tree, exp.Select) or len(list(tree.find_all(exp.Select))) != 1
                    or len(tables) != 1 or tree.args.get('with_') or tree.args.get('joins')):
                raise invalid('P1 发布模板仅支持单表 SELECT；复杂 SQL 继续使用未灰度的 P0 路径')
            table = tables[0]
            if table.name != physical.split('.')[-1] or ('.' in physical and physical != infos[source]['db'] + '.' + table.name):
                raise invalid('SQL 表与物理映射不一致')
            for col in tree.find_all(exp.Column):
                # Reject SELECT *; COUNT(*) is not a Column and is safe.
                if col.name not in declared or (col.table and col.table not in (table.name, table.alias)):
                    # Output aliases in ORDER/GROUP BY are permitted only there.
                    if not (not col.table and col.name in a['outputs'] and isinstance(col.parent, (exp.Ordered, exp.Group))):
                        raise invalid('SQL 字段不在显式批准依赖中')
            if any(isinstance(x, exp.Star) for x in tree.expressions):
                raise invalid('发布模板不能使用 SELECT *')
            if [x.alias_or_name for x in tree.expressions] != a['outputs']:
                raise invalid('SQL 输出必须精确对应审核列名及顺序')
            prepared.driver_query(infos[source]['db'])
            if len(e['source_ids']) > 1:
                validate_contract(a['merge'], e['source_ids'])
                validate_merge_query(prepared, a['merge'], infos[source]['db'])
        if len(e['source_ids']) == 1 and a['merge'] is not None:
            raise invalid('单源模板不应附带多源合并契约')


def build_index(document):
    # An immutable deterministic lexical index is committed with the release. No external index/outbox gap.
    return {e['id']: sorted(tokens(' '.join([e['name'], e['description'], *e['synonyms']])))
            for e in document['entities'] if e['availability'] != 'candidate'}


class Registry:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv('ONTOLOGY_REGISTRY_PATH', 'var/registry/registry.sqlite3'))
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata (version INTEGER NOT NULL);
            INSERT INTO metadata SELECT 2 WHERE NOT EXISTS (SELECT 1 FROM metadata);
            CREATE TABLE IF NOT EXISTS active (namespace TEXT PRIMARY KEY, release_id TEXT, generation INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS counters (namespace TEXT, entity_id TEXT, version INTEGER, PRIMARY KEY(namespace,entity_id));
            CREATE TABLE IF NOT EXISTS changesets (
                id TEXT PRIMARY KEY, namespace TEXT, author TEXT, base_release TEXT, base_generation INTEGER,
                payload TEXT, digest TEXT, scope TEXT, diff TEXT, status TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS reviews (
                changeset_id TEXT PRIMARY KEY, reviewer TEXT, decision TEXT, reason TEXT, digest TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS releases (
                id TEXT PRIMARY KEY, namespace TEXT, changeset_id TEXT UNIQUE, payload TEXT, digest TEXT,
                search_index TEXT, index_digest TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS quarantine (release_id TEXT PRIMARY KEY, reason TEXT, actor TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT, actor TEXT, action TEXT, object_id TEXT,
                details TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS discovery_runs (
                id TEXT PRIMARY KEY, namespace TEXT, source_id TEXT, actor TEXT, idempotency_key TEXT,
                request_digest TEXT, state TEXT, payload TEXT, digest TEXT, error_code TEXT,
                created REAL, updated REAL, lease_until REAL,
                UNIQUE(namespace,actor,idempotency_key));
            CREATE TABLE IF NOT EXISTS discovery_review_links (
                changeset_id TEXT PRIMARY KEY, run_id TEXT, artifact_digest TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS query_plans (
                id TEXT PRIMARY KEY, namespace TEXT, actor TEXT, source_id TEXT, release_id TEXT,
                payload TEXT, digest TEXT, created REAL, expires REAL);
            CREATE TABLE IF NOT EXISTS query_executions (
                id TEXT PRIMARY KEY, namespace TEXT, actor TEXT, idempotency_key TEXT, plan_id TEXT,
                state TEXT, payload TEXT, digest TEXT, error_code TEXT, created REAL, expires REAL, lease_until REAL,
                UNIQUE(namespace,actor,idempotency_key));
            ''')
            if db.execute('SELECT version FROM metadata').fetchone()[0] != 2:
                fail('REGISTRY_CONFIGURATION', '注册仓库格式不兼容', 503)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            if write:
                db.commit()
        except sqlite3.OperationalError:
            if write:
                db.rollback()
            fail('REGISTRY_UNAVAILABLE', '注册仓库暂时不可用', 503)
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def active_in(db, namespace):
        row = db.execute('SELECT release_id,generation FROM active WHERE namespace=?', (namespace,)).fetchone()
        return dict(row) if row else {'release_id': None, 'generation': 0}

    def active(self, namespace):
        with self.connection() as db:
            return self.active_in(db, namespace)

    @staticmethod
    def audit_in(db, namespace, actor, action, identity, details):
        db.execute('INSERT INTO audit(namespace,actor,action,object_id,details,created) VALUES(?,?,?,?,?,?)',
                   (namespace, actor, action, identity, canonical(details), time.time()))

    @staticmethod
    def release_in(db, identity, namespace, usable=True):
        row = db.execute('SELECT * FROM releases WHERE id=? AND namespace=?', (identity, namespace)).fetchone()
        if not row:
            fail('RELEASE_NOT_READY', '指定发布尚不可用', 409)
        if usable and db.execute('SELECT 1 FROM quarantine WHERE release_id=?', (identity,)).fetchone():
            fail('RELEASE_QUARANTINED', '发布已隔离，禁止继续使用', 409)
        try:
            document, index = json.loads(row['payload']), json.loads(row['search_index'])
        except (ValueError, TypeError):
            fail('REGISTRY_CORRUPT', '发布内容无法解析', 503)
        if digest(document) != row['digest'] or digest(index) != row['index_digest']:
            fail('REGISTRY_CORRUPT', '发布完整性校验失败', 503)
        validate(document)
        return document, index

    def propose(self, document, principal, expected_active, expected_generation, infos, *, discovery_link=None):
        authorized(principal, [], admin=True)
        document = copy.deepcopy(validate(document))
        validate_executable(document, infos)
        namespace = document['namespace']
        with self.connection(write=True) as db:
            state = self.active_in(db, namespace)
            if type(expected_generation) is not int or state != {'release_id': expected_active, 'generation': expected_generation}:
                fail('REVISION_CONFLICT', '活动版本已变化，请重新取基线')
            old = self.release_in(db, expected_active, namespace, usable=False)[0] if expected_active else {'entities': []}
            scope = scope_of(document) | scope_of(old)
            authorized(principal, scope, admin=True)
            previous = {e['id']: e for e in old['entities']}
            changed = []
            for e in document['entities']:
                prior = previous.get(e['id'])
                if prior and content(prior) == content(e):
                    e['version'] = prior['version']
                else:
                    counter = db.execute('SELECT version FROM counters WHERE namespace=? AND entity_id=?', (namespace, e['id'])).fetchone()
                    e['version'] = max(counter[0] if counter else 0, prior['version'] if prior else 0) + 1
                    db.execute('INSERT OR REPLACE INTO counters VALUES(?,?,?)', (namespace, e['id'], e['version']))
                    changed.append(e['id'])
            removed = sorted(set(previous) - {e['id'] for e in document['entities']})
            impact = set(changed + removed)
            for _ in range(len(document['entities']) + len(previous)):
                expanded = impact | {e['id'] for e in list(previous.values()) + document['entities'] if impact.intersection(e['dependencies'])}
                if expanded == impact:
                    break
                impact = expanded
            diff = {'changed': sorted(changed), 'removed': removed, 'impacted': sorted(impact)}
            identity, hashed = uuid4().hex, digest(document)
            db.execute('INSERT INTO changesets VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (identity, namespace, principal.subject, expected_active, expected_generation, canonical(document),
                        hashed, canonical(sorted(scope)), canonical(diff), 'validated', time.time()))
            if discovery_link is not None:
                from .discovery.workflow import attach_review
                attach_review(db, identity, discovery_link, principal)
            self.audit_in(db, namespace, principal.subject, 'proposed', identity, {'digest': hashed, 'diff': diff})
        return self.changeset(identity, principal)

    @staticmethod
    def change_in(db, identity, principal):
        authorized(principal, [], admin=True)
        row = db.execute('SELECT * FROM changesets WHERE id=?', (identity,)).fetchone()
        if not row:
            fail('NOT_FOUND', '对象不存在或不可访问', 404)
        authorized(principal, json.loads(row['scope']), admin=True)
        if digest(json.loads(row['payload'])) != row['digest']:
            fail('REGISTRY_CORRUPT', '变更集完整性校验失败', 503)
        return row

    def changeset(self, identity, principal):
        with self.connection() as db:
            row = self.change_in(db, identity, principal)
            result = {k: row[k] for k in ('id', 'namespace', 'author', 'base_release', 'base_generation', 'digest', 'status', 'created')}
            result.update(document=json.loads(row['payload']), diff=json.loads(row['diff']))
            review = db.execute('SELECT * FROM reviews WHERE changeset_id=?', (identity,)).fetchone()
            result['review'] = dict(review) if review else None
            return result

    def review(self, identity, principal, expected_digest, decision, reason):
        if decision not in ('approved', 'rejected') or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise invalid('审核须填写决定与理由')
        with self.connection(write=True) as db:
            row = self.change_in(db, identity, principal)
            if decision == 'approved':
                from .discovery.workflow import check_review_link
                check_review_link(db, identity)
            if row['author'] == principal.subject:
                fail('REVIEW_SEPARATION_REQUIRED', '提交者不能审核自己的变更', 403)
            if row['digest'] != expected_digest or row['status'] != 'validated':
                fail('REVISION_CONFLICT', '审核摘要或状态不匹配')
            if self.active_in(db, row['namespace']) != {'release_id': row['base_release'], 'generation': row['base_generation']}:
                fail('REVISION_CONFLICT', '基线已变化，旧审批不能复用')
            db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?)', (identity, principal.subject, decision, reason, expected_digest, time.time()))
            db.execute('UPDATE changesets SET status=? WHERE id=?', (decision, identity))
            self.audit_in(db, row['namespace'], principal.subject, decision, identity, {'digest': expected_digest})
        return self.changeset(identity, principal)

    def publish(self, identity, principal, expected_digest, infos):
        from .discovery.workflow import check_current_review
        check_current_review(self, identity, principal, infos)
        with self.connection(write=True) as db:
            row = self.change_in(db, identity, principal)
            if row['digest'] != expected_digest:
                fail('REVISION_CONFLICT', '发布摘要不匹配')
            existing = db.execute('SELECT id FROM releases WHERE changeset_id=?', (identity,)).fetchone()
            if existing:
                # Idempotent receipt; never silently reactivate an old release.
                return {'release_id': existing['id'], 'active': self.active_in(db, row['namespace']), 'idempotent': True}
            from .discovery.workflow import check_review_link
            check_review_link(db, identity)
            review = db.execute('SELECT * FROM reviews WHERE changeset_id=?', (identity,)).fetchone()
            if row['status'] != 'approved' or not review or review['digest'] != row['digest'] or review['decision'] != 'approved':
                fail('REVIEW_REQUIRED', '只有摘要匹配且审核通过的变更可以发布')
            state = self.active_in(db, row['namespace'])
            if state != {'release_id': row['base_release'], 'generation': row['base_generation']}:
                fail('REVISION_CONFLICT', '活动版本已变化，需重新提交和审核')
            document = validate(json.loads(row['payload']))
            validate_executable(document, infos)
            index = build_index(document)  # Any failure rolls back the entire transaction.
            release = uuid4().hex
            db.execute('INSERT INTO releases VALUES(?,?,?,?,?,?,?,?)',
                       (release, row['namespace'], identity, row['payload'], row['digest'], canonical(index), digest(index), time.time()))
            db.execute('INSERT OR REPLACE INTO active VALUES(?,?,?)', (row['namespace'], release, state['generation'] + 1))
            db.execute('UPDATE changesets SET status=? WHERE id=?', ('published', identity))
            self.audit_in(db, row['namespace'], principal.subject, 'published', release, {'changeset_id': identity, 'digest': row['digest']})
            return {'release_id': release, 'active': self.active_in(db, row['namespace']), 'idempotent': False}

    def pin(self, namespace, principal, release_id=None):
        with self.connection() as db:
            identity = release_id or self.active_in(db, namespace)['release_id']
            if not identity:
                fail('RELEASE_NOT_READY', '当前没有已发布本体', 409)
            document, index = self.release_in(db, identity, namespace)
            visible = {e['id']: e for e in document['entities'] if e['availability'] != 'candidate'
                       and set(e['source_ids']) <= principal.sources}
            # Dependency closure filtering happens before any lexical scoring or response.
            for _ in range(len(visible)):
                filtered = {k: e for k, e in visible.items() if set(e['dependencies']) <= set(visible)}
                if len(filtered) == len(visible):
                    break
                visible = filtered
            return {'release_id': identity, 'namespace': namespace, 'entities': copy.deepcopy(visible),
                    'index': {k: index[k] for k in visible}}

    def ensure_usable(self, namespace, identity):
        with self.connection() as db:
            self.release_in(db, identity, namespace)

    def activate(self, namespace, target, principal, expected_active, expected_generation, reason):
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise invalid('回滚必须提供理由')
        with self.connection(write=True) as db:
            state = self.active_in(db, namespace)
            if type(expected_generation) is not int or state != {'release_id': expected_active, 'generation': expected_generation}:
                fail('REVISION_CONFLICT', '活动指针已变化')
            document, _ = self.release_in(db, target, namespace)
            sources = scope_of(document)
            if state['release_id']:
                sources |= scope_of(self.release_in(db, state['release_id'], namespace, usable=False)[0])
            authorized(principal, sources, admin=True)
            db.execute('INSERT OR REPLACE INTO active VALUES(?,?,?)', (namespace, target, state['generation'] + 1))
            self.audit_in(db, namespace, principal.subject, 'activated', target, {'previous': expected_active, 'reason': reason})
            return self.active_in(db, namespace)

    def quarantine(self, namespace, identity, principal, reason):
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise invalid('隔离必须提供理由')
        with self.connection(write=True) as db:
            document, _ = self.release_in(db, identity, namespace, usable=False)
            authorized(principal, scope_of(document), admin=True)
            db.execute('INSERT OR IGNORE INTO quarantine VALUES(?,?,?,?)', (identity, reason, principal.subject, time.time()))
            self.audit_in(db, namespace, principal.subject, 'quarantined', identity, {'reason': reason})
        return {'release_id': identity, 'quarantined': True}

    def history(self, namespace, principal):
        authorized(principal, [], admin=True)
        with self.connection() as db:
            rows = db.execute('SELECT id,changeset_id,created FROM releases WHERE namespace=? ORDER BY created DESC LIMIT 100', (namespace,)).fetchall()
            result = []
            for row in rows:
                doc, _ = self.release_in(db, row['id'], namespace, usable=False)
                if scope_of(doc) <= principal.sources:
                    result.append(dict(row, quarantined=bool(db.execute('SELECT 1 FROM quarantine WHERE release_id=?', (row['id'],)).fetchone())))
            return {'active': self.active_in(db, namespace), 'releases': result}

    def pending(self, namespace, principal):
        authorized(principal, [], admin=True)
        with self.connection() as db:
            rows = db.execute('SELECT id,status,author,digest,scope,created FROM changesets WHERE namespace=? ORDER BY created DESC LIMIT 100', (namespace,)).fetchall()
            return {'changesets': [{k: row[k] for k in ('id', 'status', 'author', 'digest', 'created')}
                                   for row in rows if set(json.loads(row['scope'])) <= principal.sources]}

    def audit(self, namespace, principal):
        authorized(principal, [], admin=True)
        with self.connection() as db:
            # Audit may mention removed assets, so require coverage of historical proposal scopes.
            scopes = db.execute('SELECT scope FROM changesets WHERE namespace=?', (namespace,)).fetchall()
            authorized(principal, {s for row in scopes for s in json.loads(row[0])}, admin=True)
            discovery_sources = db.execute('SELECT DISTINCT source_id FROM discovery_runs WHERE namespace=?', (namespace,)).fetchall()
            authorized(principal, {row[0] for row in discovery_sources}, admin=True)
            rows = db.execute('SELECT * FROM audit WHERE namespace=? ORDER BY seq DESC LIMIT 100', (namespace,)).fetchall()
            return {'events': [dict(row, details=json.loads(row['details'])) for row in rows]}
