"""Strict registry v2 contracts. Missing business semantics remain non-executable."""
import copy
import hashlib
import json
import re

from server.errors import ServiceError

KINDS = {
    'DataSource': {'engine', 'database', 'credential_ref'},
    'BusinessConcept': {'definition'},
    'Dataset': {'physical_name', 'primary_key'},
    'Field': {'dataset_id', 'physical_name', 'dtype', 'primary_key'},
    'Relation': {'left_dataset', 'left_keys', 'right_dataset', 'right_keys', 'join_type', 'cardinality'},
    'Metric': {'expression', 'field_ids', 'aggregation', 'grain', 'unit', 'time_definition', 'filters'},
    'Mapping': {'concept_id', 'target_ids', 'expression'},
    'QualityRule': {'target_ids', 'definition'},
    'Policy': {'target_ids', 'definition'},
    'Evidence': {'target_ids', 'method', 'observed_at', 'summary'},
    'QueryTemplate': {'sql', 'parameters', 'outputs', 'column_types', 'question_pattern', 'merge'},
}
COMMON = {'id', 'kind', 'version', 'name', 'description', 'owner', 'source_ids',
          'synonyms', 'dependencies', 'availability', 'attributes', 'legacy'}


def invalid(message='本体 v2 契约无效'):
    return ServiceError('SEMANTIC_VALIDATION_FAILED', message, 422)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def strings(value, *, nonempty=False):
    return (isinstance(value, list) and (bool(value) or not nonempty)
            and all(isinstance(x, str) and 0 < len(x) <= 512 for x in value)
            and len(set(value)) == len(value))


def check_string(value, empty=False, maximum=65536):
    return isinstance(value, str) and len(value) <= maximum and (bool(value.strip()) or empty)


def validate(document):
    try:
        return _validate(document)
    except ServiceError:
        raise
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError):
        raise invalid() from None


def _validate(document):
    if (not isinstance(document, dict) or set(document) != {'schema_version', 'namespace', 'entities', 'migration'}
            or type(document['schema_version']) is not int or document['schema_version'] != 2
            or not isinstance(document['namespace'], str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', document['namespace'])
            or not isinstance(document['entities'], list) or len(document['entities']) > 10000
            or not isinstance(document['migration'], dict)):
        raise invalid()
    if len(canonical(document).encode()) > 8 * 1024 * 1024:
        raise invalid('注册文档超过 8 MiB')
    by_id = {}
    for entity in document['entities']:
        if not isinstance(entity, dict) or set(entity) != COMMON:
            raise invalid('实体字段缺失或含未知字段')
        if (not check_string(entity['id'], maximum=160) or not re.fullmatch(r'[A-Za-z0-9_:.-]+', entity['id']) or entity['id'] in by_id
                or not isinstance(entity['kind'], str) or entity['kind'] not in KINDS or type(entity['version']) is not int or entity['version'] < 1
                or not check_string(entity['name'], maximum=512) or not check_string(entity['owner'], maximum=160)
                or not check_string(entity['description'], empty=True)
                or not strings(entity['source_ids'], nonempty=True) or not strings(entity['synonyms'])
                or not strings(entity['dependencies']) or entity['id'] in entity['dependencies']
                or entity['availability'] not in ('candidate', 'reference', 'executable')
                or not isinstance(entity['legacy'], dict) or not isinstance(entity['attributes'], dict)
                or set(entity['attributes']) != KINDS[entity['kind']]):
            raise invalid()
        a, kind = entity['attributes'], entity['kind']
        if entity['availability'] == 'executable' and kind != 'QueryTemplate':
            raise invalid('P1 仅批准模板可直接执行；指标/关系仍为参考契约')
        if kind == 'DataSource':
            if a['engine'] != 'mysql' or not check_string(a['database'], maximum=128) or not check_string(a['credential_ref']):
                raise invalid()
            if len(entity['source_ids']) != 1 or a['credential_ref'] != 'source:' + entity['source_ids'][0]:
                raise invalid('凭据只允许引用已配置 source_id')
        elif kind == 'Dataset':
            if not check_string(a['physical_name']) or not strings(a['primary_key']):
                raise invalid()
        elif kind == 'Field':
            if not all(check_string(a[k], empty=k == 'dtype') for k in ('dataset_id', 'physical_name', 'dtype')) or type(a['primary_key']) is not bool:
                raise invalid()
        elif kind == 'Relation':
            if (not strings(a['left_keys'], nonempty=True) or not strings(a['right_keys'], nonempty=True)
                    or len(a['left_keys']) != len(a['right_keys'])
                    or a['join_type'] not in ('unknown', 'inner', 'left', 'right', 'full')
                    or a['cardinality'] not in ('unknown', 'one-to-one', 'many-to-one', 'one-to-many', 'many-to-many')):
                raise invalid('关系必须保留完整且等长的键组')
        elif kind == 'Metric':
            if (not strings(a['field_ids']) or not strings(a['grain']) or not strings(a['filters'])
                    or any(not check_string(a[k], empty=True) for k in ('expression', 'aggregation', 'unit', 'time_definition'))):
                raise invalid()
        elif kind == 'Mapping':
            if not strings(a['target_ids'], nonempty=True) or not check_string(a['expression'], empty=True):
                raise invalid()
        elif kind in ('QualityRule', 'Policy', 'Evidence'):
            if not strings(a['target_ids']):
                raise invalid()
            if any(not check_string(v, empty=True) for k, v in a.items() if k != 'target_ids'):
                raise invalid()
        elif kind == 'BusinessConcept' and not check_string(a['definition'], empty=True):
            raise invalid()
        elif kind == 'QueryTemplate':
            validate_template(a, entity['availability'] == 'executable')
        by_id[entity['id']] = entity

    def ref(owner, identity, kind=None):
        target = by_id.get(identity) if isinstance(identity, str) else None
        if (not target or (kind and target['kind'] != kind)
                or not set(target['source_ids']) <= set(owner['source_ids'])):
            raise invalid('依赖缺失、类型错误或跨越实体声明的数据源范围')
        if owner['availability'] != 'candidate' and target['availability'] == 'candidate':
            raise invalid('服务实体不能依赖尚未批准的候选')
        return target

    for e in by_id.values():
        for identity in e['dependencies']:
            ref(e, identity)
        a, required = e['attributes'], set()
        if e['kind'] == 'Dataset':
            for key in a['primary_key']:
                col = ref(e, key, 'Field')
                if col['attributes']['dataset_id'] != e['id']:
                    raise invalid('主键不属于该数据集')
            # Membership is not a DAG edge: Field already depends on Dataset.
        elif e['kind'] == 'Field':
            ref(e, a['dataset_id'], 'Dataset')
            required.add(a['dataset_id'])
        elif e['kind'] == 'Relation':
            for side in ('left', 'right'):
                table = a[side + '_dataset']
                ref(e, table, 'Dataset')
                required.add(table)
                for key in a[side + '_keys']:
                    col = ref(e, key, 'Field')
                    if col['attributes']['dataset_id'] != table:
                        raise invalid('关系键不属于声明的数据集')
                    required.add(key)
        elif e['kind'] == 'Metric':
            for identity in a['field_ids'] + a['grain']:
                ref(e, identity, 'Field')
                required.add(identity)
        elif e['kind'] in ('Mapping', 'QualityRule', 'Policy', 'Evidence'):
            required.update(a['target_ids'])
            if e['kind'] == 'Mapping':
                ref(e, a['concept_id'], 'BusinessConcept')
                required.add(a['concept_id'])
        if not required <= set(e['dependencies']):
            raise invalid('显式依赖清单不完整')
    # Iterative topological validation avoids recursion on hostile chains.
    visited = set()
    depth = 0
    while len(visited) < len(by_id):
        depth += 1
        if depth > 256:
            raise invalid('依赖图深度超过上限')
        ready = {k for k, e in by_id.items() if k not in visited and set(e['dependencies']) <= visited}
        if not ready:
            raise invalid('依赖图含循环')
        visited.update(ready)
    return document


def validate_template(a, executable):
    import string
    if (not check_string(a['sql']) or not check_string(a['question_pattern'], empty=True, maximum=4096)
            or not isinstance(a['parameters'], list) or len(a['parameters']) > 32
            or not strings(a['outputs']) or not isinstance(a['column_types'], list)
            or len(a['outputs']) != len(a['column_types'])
            or any(t not in ('维度', '度量') for t in a['column_types'])
            or (a['merge'] is not None and not isinstance(a['merge'], dict))):
        raise invalid('模板定义无效')
    names = set()
    for slot in a['parameters']:
        if (not isinstance(slot, dict) or set(slot) != {'name', 'dtype', 'example'}
                or not check_string(slot['name'], maximum=80) or slot['name'] in names
                or slot['dtype'] not in ('string', 'integer', 'number', 'boolean', 'list')):
            raise invalid('模板参数定义无效')
        names.add(slot['name'])
        parameter(slot, slot['example'])
    if executable and (not a['outputs'] or not a['question_pattern']):
        raise invalid('可执行模板必须提供结果列和问题骨架')
    if executable:
        try:
            fields = [(name, spec, conversion) for _, name, spec, conversion in string.Formatter().parse(a['question_pattern']) if name is not None]
            if (any(not name.isdecimal() or spec or conversion for name, spec, conversion in fields)
                    or {int(name) for name, _, _ in fields} != set(range(len(a['parameters'])))):
                raise ValueError()
        except ValueError:
            raise invalid('问题骨架必须完整使用位置参数') from None


def parameter(slot, value):
    import math
    dtype = slot['dtype']
    valid = ((dtype == 'string' and isinstance(value, str) and len(value) <= 4096)
             or (dtype == 'integer' and type(value) is int)
             or (dtype == 'number' and (type(value) is int or type(value) is float and math.isfinite(value)))
             or (dtype == 'boolean' and type(value) is bool)
             or (dtype == 'list' and isinstance(value, list) and 1 <= len(value) <= 100
                 and all((type(x) is str and len(x) <= 4096) or type(x) in (int, bool) or x is None
                         or type(x) is float and math.isfinite(x) for x in value)))
    if not valid:
        raise invalid('参数类型与发布定义不一致')
    return value


def entity(kind, identity, name, owner, sources, attributes, **kwargs):
    return dict(id=identity, kind=kind, version=1, name=name, owner=owner, source_ids=sources,
                description=kwargs.get('description', ''), synonyms=kwargs.get('synonyms', []),
                dependencies=kwargs.get('dependencies', []), availability=kwargs.get('availability', 'candidate'),
                attributes=attributes, legacy=kwargs.get('legacy', {}))


def migrate_v1(snapshot, namespace, source_map, owner):
    """Lossless archive plus explicit bindings. Never infer namespaces == database IDs."""
    from .schema import Snapshot, COLLECTIONS
    snapshot = Snapshot.from_dict(snapshot.to_dict())
    if snapshot.revision:
        snapshot.verify_revision()
    entities, report = [], []
    for collection in COLLECTIONS:
        for old in getattr(snapshot, collection):
            source = source_map.get(old.source.namespace)
            if not isinstance(source, str) or not source:
                raise invalid('每个 v1 命名空间必须显式映射到数据源')
            args = dict(legacy=old.to_dict(), description=getattr(old, 'description', ''),
                        synonyms=getattr(old, 'synonyms', []),
                        availability='reference' if old.review_status == 'approved' else 'candidate')
            deps = []
            if collection == 'scenarios':
                kind, attrs = 'BusinessConcept', {'definition': old.description}
            elif collection == 'tables':
                kind, attrs = 'Dataset', {'physical_name': old.name, 'primary_key':
                                         [c.id for c in snapshot.columns if c.table_id == old.id and c.primary_key]}
                deps = [old.scenario_id]
            elif collection == 'columns':
                kind, attrs = 'Field', dict(dataset_id=old.table_id, physical_name=old.name, dtype=old.dtype, primary_key=old.primary_key)
                deps = [old.table_id]
            elif collection == 'relations':
                kind, attrs = 'Relation', dict(left_dataset=old.left_table, left_keys=[old.left_key],
                                               right_dataset=old.right_table, right_keys=[old.right_key],
                                               join_type=old.join_type, cardinality='unknown')
                deps = list(dict.fromkeys([old.scenario_id, old.left_table, old.right_table, old.left_key, old.right_key]))
                args['availability'] = 'candidate'
                report.append({'id': old.id, 'blockers': ['cardinality_and_evidence_required']})
            elif collection == 'metrics':
                kind, attrs = 'Metric', dict(expression=old.expression, field_ids=old.column_ids, aggregation=old.aggregation,
                                            filters=old.filters, grain=[], unit='', time_definition='')
                deps = [old.scenario_id, *old.column_ids]
                args['availability'] = 'candidate'
                report.append({'id': old.id, 'blockers': ['grain_unit_time_and_business_review_required']})
            else:
                kind = 'QueryTemplate'
                params = []
                for slot in sorted(old.slots, key=lambda s: s.position):
                    example = old.params[slot.position]
                    if slot.dtype != 'string':
                        try:
                            example = json.loads(example)
                        except ValueError:
                            raise invalid('旧模板参数不能无损转换') from None
                    params.append(dict(name=slot.name, dtype=slot.dtype, example=example))
                attrs = dict(sql=old.content, parameters=params, outputs=old.outputs,
                             column_types=['维度'] * len(old.outputs), question_pattern=old.question_pattern, merge=None)
                args['availability'] = 'candidate'
                report.append({'id': old.id, 'blockers': ['physical_dependencies_outputs_and_sql_review_required']})
            name = getattr(old, 'name', getattr(old, 'question', '')) or old.id
            entities.append(entity(kind, old.id, name, owner,
                                   [source], attrs, dependencies=deps, **args))
    # Approval of a child cannot bypass an unapproved ancestor.
    by_id = {e['id']: e for e in entities}
    for _ in range(len(entities)):
        changed = False
        for e in entities:
            if e['availability'] != 'candidate' and any(by_id[d]['availability'] == 'candidate' for d in e['dependencies']):
                e['availability'], changed = 'candidate', True
        if not changed:
            break
    document = dict(schema_version=2, namespace=namespace, entities=entities,
                    migration={'adapter': 'v1-to-v2/1', 'source_revision': snapshot.revision,
                               'source_snapshot': snapshot.to_dict(), 'blockers': report,
                               'warnings': list(snapshot.warnings), 'source_map': copy.deepcopy(source_map)})
    return validate(document)
