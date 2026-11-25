"""Authenticated registry and read-only Knowledge API; no body-supplied identity."""
import re
from flask import Blueprint, g, request

from ontology.registry import Registry, authorized
from ontology.v2 import migrate_v1, invalid
from ontology.schema import Snapshot, ValidationError
from ontology.runtime import namespace, search, detail
from . import conf
from .errors import ServiceError

blueprint = Blueprint('ontology_v2', __name__)


def body(required, optional=()):
    data = request.get_json()
    if not isinstance(data, dict) or set(data) - set(required) - set(optional) or not set(required) <= set(data):
        raise invalid('请求字段缺失或包含不支持的字段')
    for name in ('release_id', 'changeset_id', 'expected_active'):
        if name in data and not (name == 'expected_active' and data[name] is None):
            if not isinstance(data[name], str) or not re.fullmatch(r'[0-9a-f]{32}', data[name]):
                raise invalid('版本标识格式无效')
    if 'expected_generation' in data and (type(data['expected_generation']) is not int or data['expected_generation'] < 0):
        raise invalid('版本代次无效')
    return data


def admin():
    authorized(g.principal, [], admin=True)


def sources():
    conf._load_db_conf()
    return conf.mysql_info


@blueprint.post('/v1/ontology/migrate-v1')
def migrate():
    admin()
    data = body(('snapshot', 'source_map'))
    if (not isinstance(data['source_map'], dict)
            or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in data['source_map'].items())):
        raise invalid()
    authorized(g.principal, data['source_map'].values(), admin=True)
    try:
        snapshot = Snapshot.from_dict(data['snapshot'])
        document = migrate_v1(snapshot, namespace(), data['source_map'], g.principal.subject)
    except ValidationError:
        raise invalid('v1 快照无法校验，未创建变更') from None
    return {'document': document, 'published': False}


@blueprint.post('/v1/ontology/changesets')
def propose():
    admin()
    data = body(('document', 'expected_active', 'expected_generation'))
    if not isinstance(data['document'], dict) or data['document'].get('namespace') != namespace():
        raise invalid('只允许管理配置中的命名空间')
    return Registry().propose(data['document'], g.principal, data['expected_active'], data['expected_generation'], sources()), 201


@blueprint.get('/v1/ontology/changesets')
def pending():
    admin()
    return Registry().pending(namespace(), g.principal)


@blueprint.get('/v1/ontology/audit')
def audit():
    admin()
    return Registry().audit(namespace(), g.principal)


@blueprint.get('/v1/ontology/changesets/<identity>')
def changeset(identity):
    admin()
    result = Registry().changeset(identity, g.principal)
    if result['namespace'] != namespace():
        raise ServiceError('NOT_FOUND', '对象不存在或不可访问', 404)
    return result


@blueprint.post('/v1/ontology/changesets/<identity>/review')
def review(identity):
    admin()
    data = body(('expected_digest', 'decision', 'reason'))
    changeset(identity)
    return Registry().review(identity, g.principal, data['expected_digest'], data['decision'], data['reason'])


@blueprint.post('/v1/ontology/releases')
def publish():
    admin()
    data = body(('changeset_id', 'expected_digest'))
    changeset(data['changeset_id'])
    return Registry().publish(data['changeset_id'], g.principal, data['expected_digest'], sources())


@blueprint.get('/v1/ontology/releases')
def history():
    admin()
    return Registry().history(namespace(), g.principal)


@blueprint.post('/v1/ontology/activate')
def activate():
    admin()
    data = body(('release_id', 'expected_active', 'expected_generation', 'reason'))
    registry = Registry()
    # Verify the old physical bindings against current configured sources before rollback.
    with registry.connection() as db:
        document, _ = registry.release_in(db, data['release_id'], namespace())
    from ontology.registry import validate_executable, scope_of
    authorized(g.principal, scope_of(document), admin=True)
    validate_executable(document, sources())
    return registry.activate(namespace(), data['release_id'], g.principal, data['expected_active'], data['expected_generation'], data['reason'])


@blueprint.post('/v1/ontology/releases/<identity>/quarantine')
def quarantine(identity):
    admin()
    data = body(('reason',))
    return Registry().quarantine(namespace(), identity, g.principal, data['reason'])


@blueprint.post('/v1/knowledge/search')
def knowledge_search():
    data = body(('question',), ('release_id', 'kinds', 'source_ids', 'limit'))
    return search(Registry(), g.principal, **data)


@blueprint.get('/v1/knowledge/entities/<path:identity>')
def knowledge_entity(identity):
    return detail(Registry(), g.principal, identity, request.args.get('release_id'))
