"""Closed metric grammar. Published prose and caller text never become executable SQL."""
import copy
import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import re

from ontology.v2 import canonical, digest, invalid
from ontology.registry import fail
from ontology.discovery.policy import quote
from ontology.discovery.mysql import Collector
from server.query_policy import PreparedQuery

VERSION = 'metric-compiler/2'


def family(dtype):
    if not isinstance(dtype, str) or not dtype.strip(): raise invalid('字段类型缺失')
    name = dtype.lower().split('(')[0].split()[0]
    if name in ('tinyint', 'smallint', 'mediumint', 'int', 'integer', 'bigint'): return 'int'
    if name in ('decimal', 'numeric', 'float', 'double', 'real'): return 'number'
    if name in ('char', 'varchar', 'text', 'tinytext', 'mediumtext', 'longtext'): return 'text'
    if name in ('date', 'datetime'): return name
    raise invalid('当前编译器不支持该字段类型')


def value_for(field, value):
    kind = family(field['attributes']['dtype'])
    if kind == 'int':
        if type(value) is not int or not -(2**63) <= value <= 2**64 - 1: raise invalid('整数过滤值无效')
        return value
    if kind == 'number':
        if type(value) not in (str, int, float) or len(str(value)) > 80: raise invalid('数值过滤值无效')
        try:
            number = Decimal(str(value))
            if not number.is_finite() or abs(number) >= Decimal('1e35') or number.as_tuple().exponent < -30: raise ValueError()
        except (InvalidOperation, ValueError): raise invalid('数值过滤值无效') from None
        return format(number, 'f')
    if not isinstance(value, str) or len(value) > 1024: raise invalid('文本或日期过滤值无效')
    if kind in ('date', 'datetime'):
        pattern = r'\d{4}-\d{2}-\d{2}' if kind == 'date' else r'\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?'
        if not re.fullmatch(pattern, value): raise invalid('时间必须为明确的本地日期/时间，不接受相对日期或偏移')
        try: datetime.fromisoformat(value)
        except ValueError: raise invalid('日期无效') from None
    return value


def measure(metric, fields, dataset_id, column):
    """Only approved SUM/COUNT/AVG/MIN/MAX/distinct and ratio-of-sums are compiled."""
    a = metric['attributes']
    if not all(a[k] for k in ('expression', 'aggregation', 'grain', 'unit', 'time_definition')):
        raise invalid('指标缺少粒度、单位、时间或聚合口径')
    if any(fields[k]['attributes']['dataset_id'] != dataset_id for k in a['field_ids'] + a['grain']):
        raise invalid('指标度量及粒度必须来自同一事实数据集')
    lookup = {fields[k]['attributes']['physical_name']: k for k in a['field_ids']}
    if len(lookup) != len(a['field_ids']): raise invalid('指标字段物理名不唯一')
    expression = re.sub(r'\s+', '', a['expression']).upper()
    names = {name.upper(): identity for name, identity in lookup.items()}
    if len(names) != len(lookup): raise invalid('指标字段大小写绑定有歧义')
    def field(name, numeric=False):
        if name not in names: raise invalid('表达式字段未包含在批准依赖中')
        identity = names[name]
        if numeric and family(fields[identity]['attributes']['dtype']) not in ('int', 'number'):
            raise invalid('数值聚合只能用于数值字段')
        return column(identity)
    if a['aggregation'] == 'ratio':
        match = re.fullmatch(r'SUM\(([A-Z_][A-Z0-9_]*)\)/NULLIF\(SUM\(([A-Z_][A-Z0-9_]*)\),0\)', expression)
        if not match: raise invalid('比率只支持 SUM(x)/NULLIF(SUM(y),0)')
        return f'SUM({field(match[1], True)}) / NULLIF(SUM({field(match[2], True)}), 0)'
    if a['aggregation'] == 'count_distinct':
        match = re.fullmatch(r'COUNT\(DISTINCT([A-Z_][A-Z0-9_]*)\)', expression)
        if not match: raise invalid('去重计数表达式无效')
        return f'COUNT(DISTINCT {field(match[1])})'
    match = re.fullmatch(r'(SUM|AVG|MIN|MAX|COUNT)\((\*|[A-Z_][A-Z0-9_]*)\)', expression)
    if not match or a['aggregation'] != match[1].lower(): raise invalid('表达式和批准聚合口径不一致或超出受控语法')
    if match[2] == '*':
        if match[1] != 'COUNT': raise invalid('只有 COUNT 可使用星号')
        return 'COUNT(*)'
    return f'{match[1]}({field(match[2], match[1] != "COUNT")})'


def catalog_for(entities, info, budget):
    tables = {}
    for e in entities.values():
        if e['kind'] == 'Dataset':
            name = e['attributes']['physical_name']; quote(name)
            if name in tables: raise invalid('不同数据集不能绑定同一物理表')
            columns = sorted({f['attributes']['physical_name'] for f in entities.values()
                              if f['kind'] == 'Field' and f['attributes']['dataset_id'] == e['id']})
            tables[name] = dict(columns=columns, profile_columns=[], sensitive_columns=[])
    if not tables or len(tables) > 3 or any(not t['columns'] or len(t['columns']) > 64 for t in tables.values()):
        raise invalid('计划表列范围超限或缺少物理字段')
    catalog = Collector(info, dict(source_id=info['source_id'], tables=tables, sample_rows=1, relations=[]), budget).catalog()
    physical = {t['name']: t for t in catalog['tables']}
    for e in entities.values():
        a = e['attributes']
        if e['kind'] == 'DataSource' and (a['database'] != info['db'] or e['source_ids'] != [info['source_id']]):
            fail('STALE_BINDING', '数据源与已发布绑定不一致', 422)
        if e['kind'] == 'Field':
            table = entities[a['dataset_id']]['attributes']['physical_name']
            actual = next(c for c in physical[table]['columns'] if c['name'] == a['physical_name'])
            if a['dtype'].lower() != actual['dtype'].lower():
                fail('STALE_BINDING', '字段类型与批准定义不一致', 422)
    return catalog


def compile_plan(pin, full_document, intent, infos, budget):
    if (not isinstance(intent, dict) or set(intent) - {'source_ids', 'metric_ids', 'dimensions', 'filters', 'time', 'relation_ids'}
            or not {'source_ids', 'metric_ids'} <= set(intent)):
        raise invalid('计划仅接受结构化源、指标、维度、过滤、时间及关系 ID')
    request = dict(dimensions=[], filters=[], relation_ids=[], **{k: v for k, v in intent.items() if k not in ('dimensions', 'filters', 'relation_ids')})
    request.update({k: intent[k] for k in ('dimensions', 'filters', 'relation_ids') if k in intent})
    for key, maximum in (('source_ids', 1), ('metric_ids', 4), ('dimensions', 4), ('relation_ids', 2)):
        values = request[key]
        if not isinstance(values, list) or len(values) > maximum or any(not isinstance(v, str) for v in values) or len(set(values)) != len(values): raise invalid('计划列表无效或超限')
    if len(request['source_ids']) != 1 or not request['metric_ids']: raise invalid('P3 动态指标计划需要一个明确数据源和至少一个指标')
    if not isinstance(request['filters'], list) or len(request['filters']) > 16: raise invalid('过滤条件过多')
    source = request['source_ids'][0]
    visible = pin['entities']; selected = {}
    def get(identity, kind=None):
        e = visible.get(identity) if isinstance(identity, str) else None
        if not e or (kind and e['kind'] != kind) or e['source_ids'] != [source]: fail('NOT_FOUND', '定义不存在或不可访问', 404)
        selected[identity] = e
        return e
    metrics = [get(k, 'Metric') for k in request['metric_ids']]
    contracts = []
    for metric in metrics:
        matches = [e for e in visible.values() if e['kind'] == 'MetricContract' and e['attributes']['metric_id'] == metric['id'] and e['source_ids'] == [source]]
        if not matches: fail('UNANSWERABLE', '指标没有已批准的编译契约', 422)
        if len(matches) != 1: fail('AMBIGUOUS_DEFINITION', '指标存在多个批准契约，请治理定义冲突', 422)
        contracts.append(get(matches[0]['id'], 'MetricContract'))
    base_ids = {c['attributes']['dataset_id'] for c in contracts}
    time_specs = {(c['attributes']['time_field'], c['attributes']['timezone']) for c in contracts}
    if len(base_ids) != 1 or len(time_specs) != 1: raise invalid('同一计划的指标必须共享事实粒度和时间口径')
    base_id = next(iter(base_ids)); base = get(base_id, 'Dataset')
    for c in contracts:
        a = c['attributes']
        if not set(request['dimensions']) <= set(a['dimensions']) or not set(request['relation_ids']) <= set(a['relation_ids']): raise invalid('维度或关系未获指标契约批准')
    dataset_alias = {base_id: 't0'}; joins = []
    for i, identity in enumerate(request['relation_ids']):
        r = get(identity, 'Relation'); a = r['attributes']
        if a['left_dataset'] != base_id or a['right_dataset'] in dataset_alias or a['cardinality'] != 'many-to-one' or a['join_type'] not in ('left', 'inner'):
            raise invalid('只支持事实表到唯一目标的明确多对一星型连接；自关联、多跳和放大连接不允许')
        get(a['right_dataset'], 'Dataset'); dataset_alias[a['right_dataset']] = 't' + str(i + 1); joins.append(r)
    def column(identity):
        f = get(identity, 'Field'); a = f['attributes']
        if a['dataset_id'] not in dataset_alias: raise invalid('字段缺少显式批准关系路径')
        return dataset_alias[a['dataset_id']] + '.' + quote(a['physical_name'])
    dimensions = [column(k) for k in request['dimensions']]
    # Metric expressions see only explicit approved field IDs; no free-form SQL fragments survive.
    for metric in metrics:
        for identity in metric['attributes']['field_ids'] + metric['attributes']['grain']: get(identity, 'Field')
        if not base['attributes']['primary_key'] or metric['attributes']['grain'] != base['attributes']['primary_key']:
            raise invalid('指标原始粒度必须完整等于事实表批准主键')
    measures = [measure(m, visible, base_id, column) for m in metrics]
    if len({canonical(m['attributes']['filters']) for m in metrics}) != 1:
        raise invalid('不同固定过滤口径的指标必须拆分计划，不能合并 WHERE 改变含义')
    params, predicates = {}, []
    def bind(value):
        name = 'p' + str(len(params)); params[name] = value; return ':' + name
    def predicate(f, allowed=None, policy=False):
        if not isinstance(f, dict) or set(f) - {'field_id', 'op', 'value'} or not {'field_id', 'op'} <= set(f): raise invalid('过滤契约无效')
        identity, op = f['field_id'], f['op']
        if not isinstance(identity, str) or not isinstance(op, str): raise invalid('过滤字段和操作符无效')
        if allowed is not None and identity not in allowed: raise invalid('过滤字段未获指标契约批准')
        field = get(identity, 'Field'); col = column(identity)
        def typed_bind(value):
            placeholder = bind(value_for(field, value))
            return 'CAST(' + placeholder + ' AS DECIMAL(65,30))' if family(field['attributes']['dtype']) == 'number' else placeholder
        if op in ('is_null', 'not_null'):
            if 'value' in f: raise invalid('NULL 操作不接受值')
            return col + (' IS NULL' if op == 'is_null' else ' IS NOT NULL')
        if set(f) != {'field_id', 'op', 'value'}: raise invalid('缺少过滤值')
        value = f['value']
        if policy and value == {'principal': 'subject'}: value = pin['subject']
        if op == 'in':
            if not isinstance(value, list) or not 1 <= len(value) <= 100: raise invalid('IN 列表无效')
            return col + ' IN (' + ','.join(typed_bind(x) for x in value) + ')'
        ops = {'eq': '=', 'ne': '<>', 'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<='}
        if op not in ops: raise invalid('过滤操作符不支持')
        return col + ' ' + ops[op] + ' ' + typed_bind(value)
    for c in contracts:
        fields = {f.get('field_id') for f in request['filters'] if isinstance(f, dict) and isinstance(f.get('field_id'), str) and f.get('op') in ('eq', 'in')}
        if not set(c['attributes']['required_filters']) <= fields: raise invalid('缺少契约要求的等值范围过滤')
    allowed_filters = set.intersection(*(set(c['attributes']['filter_fields']) for c in contracts))
    predicates.extend(predicate(f, allowed_filters) for f in request['filters'])
    time_field, timezone = next(iter(time_specs))
    if time_field:
        window = request.get('time')
        if not isinstance(window, dict) or set(window) != {'start', 'end', 'timezone'} or window['timezone'] != timezone: raise invalid('必须提供与契约一致的明确半开时间区间和时区')
        field = get(time_field, 'Field')
        if family(field['attributes']['dtype']) not in ('date', 'datetime'): raise invalid('时间过滤仅支持显式 DATE/DATETIME 存储口径')
        start, end = value_for(field, window['start']), value_for(field, window['end'])
        if datetime.fromisoformat(start) >= datetime.fromisoformat(end): raise invalid('时间区间必须 start < end')
        predicates += [column(time_field) + ' >= ' + bind(start), column(time_field) + ' < ' + bind(end)]
    elif 'time' in request: raise invalid('全量指标契约不接受未批准时间维度')
    # Mandatory metric filters use typed JSON, never legacy arbitrary SQL predicates.
    for metric in metrics[:1]:
        for raw in metric['attributes']['filters']:
            try: parsed = json.loads(raw)
            except (TypeError, ValueError): raise invalid('指标固定过滤必须使用受控 JSON 契约') from None
            predicates.append(predicate(parsed))
    join_sql = []
    for r in joins:
        a = r['attributes']; target = get(a['right_dataset'], 'Dataset')
        condition = ' AND '.join(column(l) + ' = ' + column(rr) for l, rr in zip(a['left_keys'], a['right_keys']))
        join_sql.append(a['join_type'].upper() + ' JOIN ' + quote(target['attributes']['physical_name']) + ' AS ' + dataset_alias[target['id']] + ' ON ' + condition)
    # Policies are found in the full release, not just the caller-filtered index. Invisible applicable policies deny.
    for dataset in [e for e in selected.values() if e['kind'] == 'Dataset']:
        for key in dataset['dependencies']:
            if visible[key]['kind'] == 'DataSource': get(key, 'DataSource')
    targets = set(selected)
    # All declared metric/contract dependencies count for mandatory policy applicability.
    pending_targets = list(targets)
    while pending_targets:
        for key in visible[pending_targets.pop()]['dependencies']:
            if key not in targets: targets.add(key); pending_targets.append(key)
    remaining = [e for e in full_document['entities'] if e['availability'] != 'candidate' and e['kind'] in ('Policy', 'QualityRule')]
    full_entities = {e['id']: e for e in full_document['entities']}
    def physical_key(e):
        a = e['attributes']; scope = tuple(sorted(e['source_ids']))
        if e['kind'] == 'DataSource': return ('source', scope, a['database'])
        if e['kind'] == 'Dataset': return ('dataset', scope, a['physical_name'])
        if e['kind'] == 'Field': return ('field', scope, full_entities[a['dataset_id']]['attributes']['physical_name'], a['physical_name'])
        return None
    applied = 0
    while True:
        # Aliasing the same physical source/table/column under another entity ID cannot evade a policy.
        keys = {physical_key(full_entities[k]) for k in targets} - {None}
        targets.update(e['id'] for e in full_document['entities'] if e['availability'] != 'candidate' and physical_key(e) in keys)
        e = next((e for e in remaining if targets.intersection(e['attributes']['target_ids'])), None)
        if e is None: break
        remaining.remove(e); applied += 1
        if applied > 32: raise invalid('适用政策数量超过执行预算')
        if e['kind'] == 'QualityRule':
            from ontology.governance.rules import parse
            from server.errors import ServiceError
            try: spec, _ = parse(e, visible)
            except ServiceError: fail('POLICY_UNSUPPORTED', '相关质量规则超出受控语法，不能忽略', 422)
            if any(visible[k]['attributes']['dataset_id'] != base_id for k in spec['field_ids']):
                fail('POLICY_UNSUPPORTED', '在线质量检查仅支持事实表字段；维表规则须由治理扫描验证', 422)
            get(e['id'], 'QualityRule')
            for key in spec['field_ids']: column(key)
            targets.update(selected); targets.update(e['dependencies'])
            continue
        get(e['id'], 'Policy')
        try: rule = json.loads(e['attributes']['definition'])
        except (ValueError, TypeError): raise invalid('相关政策不是受支持的结构化规则') from None
        if rule == {'version': 1, 'deny': True}: fail('POLICY_DENIED', '批准政策禁止此计划', 403)
        if not isinstance(rule, dict) or set(rule) != {'version', 'filters'} or rule['version'] != 1 or not isinstance(rule['filters'], list) or not 1 <= len(rule['filters']) <= 16: raise invalid('相关政策不可执行，不能静默忽略')
        predicates.extend(predicate(f, policy=True) for f in rule['filters'])
        targets.update(selected)
        targets.update(e['dependencies'])
    # Include only actually used physical dependencies, plus every dataset PK for grain verification.
    for dataset in [e for e in selected.values() if e['kind'] == 'Dataset']:
        for key in dataset['attributes']['primary_key']: get(key, 'Field')
        for key in dataset['dependencies']:
            if visible[key]['kind'] == 'DataSource': get(key, 'DataSource')
    if source not in infos: fail('STALE_BINDING', '源配置已移除', 422)
    physical_entities = {k: e for k, e in selected.items() if e['kind'] in ('Dataset', 'Field', 'DataSource')}
    if len([e for e in physical_entities.values() if e['kind'] == 'DataSource']) != 1:
        raise invalid('物理计划必须绑定唯一的已批准 DataSource')
    catalog = catalog_for(physical_entities, infos[source], budget)
    catalog_tables = {t['name']: t for t in catalog['tables']}
    pk = [visible[k]['attributes']['physical_name'] for k in base['attributes']['primary_key']]
    if catalog_tables[base['attributes']['physical_name']]['primary_key'] != pk: fail('STALE_BINDING', '事实表实际主键与批准粒度不一致', 422)
    for r in joins:
        a = r['attributes']; target = visible[a['right_dataset']]['attributes']['physical_name']
        keys = [visible[k]['attributes']['physical_name'] for k in a['right_keys']]
        if keys not in catalog_tables[target]['unique_keys'].values(): fail('UNSAFE_CARDINALITY', '关系目标缺少完整实际唯一键，拒绝可能放大的连接', 422)
        for l, rr in zip(a['left_keys'], a['right_keys']):
            def actual(k):
                field = visible[k]['attributes']; table = visible[field['dataset_id']]['attributes']['physical_name']
                return next(c for c in catalog_tables[table]['columns'] if c['name'] == field['physical_name'])
            left, right = actual(l), actual(rr)
            if (left['dtype'], left['collation']) != (right['dtype'], right['collation']): raise invalid('关系键类型或排序规则不一致')
    outputs = ['d' + str(i) for i in range(len(dimensions))] + ['m' + str(i) for i in range(len(measures))]
    sql = 'SELECT ' + ', '.join(expr + ' AS ' + alias for expr, alias in zip(dimensions + measures, outputs))
    population = ' FROM ' + quote(base['attributes']['physical_name']) + ' AS t0'
    if join_sql: population += ' ' + ' '.join(join_sql)
    if predicates: population += ' WHERE ' + ' AND '.join('(' + p + ')' for p in predicates)
    sql += population
    if dimensions: sql += ' GROUP BY ' + ', '.join(dimensions) + ' ORDER BY ' + ', '.join('d' + str(i) for i in range(len(dimensions)))
    PreparedQuery(sql, params).driver_query(infos[source]['db'])
    # Reader-triggered quality SQL must use exactly the same authorized population,
    # including mandatory subject policies, time window, fixed filters and joins.
    quality_queries = {}
    for rule in [e for e in selected.values() if e['kind'] == 'QualityRule']:
        from ontology.governance.rules import parse
        spec, _ = parse(rule, visible)
        aliases = ['q' + str(i) for i in range(len(spec['field_ids']))]
        scoped = 'SELECT ' + ','.join(column(k) + ' AS ' + a for k, a in zip(spec['field_ids'], aliases)) + population
        if spec['check'] == 'not_null':
            query = 'SELECT COUNT(*) AS violations FROM (' + scoped + ') AS scoped WHERE ' + ' OR '.join(a + ' IS NULL' for a in aliases)
        else:
            query = 'SELECT COUNT(*) AS violations FROM (SELECT ' + ','.join(aliases) + ' FROM (' + scoped + ') AS scoped GROUP BY ' + ','.join(aliases) + ' HAVING COUNT(*)>1) AS duplicate_groups'
        PreparedQuery(query, params).driver_query(infos[source]['db'])
        quality_queries[rule['id']] = query
    # Record transitive semantic dependencies without adding unused tables to physical SQL scope.
    pending = list(selected)
    while pending:
        for identity in selected[pending.pop()]['dependencies']:
            if identity not in selected: get(identity); pending.append(identity)
    return dict(compiler=VERSION, intent=copy.deepcopy(request), source_ids=[source], release_id=pin['release_id'],
                sql=sql, parameters=params, sql_sha256=hashlib.sha256(sql.encode()).hexdigest(), outputs=outputs,
                labels=[visible[k]['name'] for k in request['dimensions']] + [m['name'] + ' [' + m['attributes']['unit'] + ']' for m in metrics],
                column_types=['维度'] * len(dimensions) + ['度量'] * len(measures),
                metrics=[{'id': m['id'], 'version': m['version'], 'name': m['name'], 'unit': m['attributes']['unit'],
                          'grain': m['attributes']['grain'], 'time_definition': m['attributes']['time_definition'], 'aggregation': m['attributes']['aggregation']} for m in metrics],
                relations=[{'id': r['id'], 'version': r['version']} for r in joins],
                dependencies={k: e['version'] for k, e in selected.items()},
                quality_rules=[e for e in selected.values() if e['kind'] == 'QualityRule'],
                quality_queries=quality_queries,
                catalog_fingerprint=catalog['fingerprint'], physical_entities=physical_entities,
                warnings=['源数据不具有跨请求快照保证；参考关系在执行时重新验证唯一键。'])
