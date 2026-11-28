"""Closed quality grammar; definitions never supply SQL or executable code."""
import json
from ontology.v2 import invalid
from ontology.discovery.policy import quote
from server.query_policy import PreparedQuery
from server.execution import execute_source
from ontology.registry import fail


def parse(rule, entities):
    try:
        spec = json.loads(rule['attributes']['definition'])
    except (TypeError, ValueError):
        raise invalid('质量规则必须使用受控 JSON 契约') from None
    if (not isinstance(spec, dict) or set(spec) != {'version', 'check', 'field_ids'} or type(spec['version']) is not int
            or spec['version'] != 1 or spec['check'] not in ('not_null', 'unique')
            or not isinstance(spec['field_ids'], list) or not 1 <= len(spec['field_ids']) <= 4
            or any(not isinstance(k, str) for k in spec['field_ids']) or len(set(spec['field_ids'])) != len(spec['field_ids'])):
        raise invalid('仅支持 version=1 的 not_null / unique 零违规规则')
    fields = [entities.get(k) for k in spec['field_ids']]
    if any(not f or f['kind'] != 'Field' or f['source_ids'] != rule['source_ids'] for f in fields):
        raise invalid('质量字段必须属于同一个批准源')
    if not set(spec['field_ids']) <= set(rule['dependencies']) or not set(spec['field_ids']) <= set(rule['attributes']['target_ids']):
        raise invalid('质量字段必须显式列为规则目标与依赖')
    datasets = {f['attributes']['dataset_id'] for f in fields}
    if len(datasets) != 1 or len(rule['source_ids']) != 1:
        raise invalid('质量规则仅支持单表、单源')
    table = entities[next(iter(datasets))]['attributes']['physical_name']
    columns = [quote(f['attributes']['physical_name']) for f in fields]
    if spec['check'] == 'not_null':
        sql = f"SELECT COUNT(*) AS violations FROM {quote(table)} WHERE " + ' OR '.join(c + ' IS NULL' for c in columns)
    else:
        # NULL is a value for this rule: repeated NULL key tuples also violate uniqueness.
        sql = f"SELECT COUNT(*) AS violations FROM (SELECT {','.join(columns)} FROM {quote(table)} GROUP BY {','.join(columns)} HAVING COUNT(*)>1) AS duplicate_groups"
    return spec, PreparedQuery(sql)


def evaluate(rule, entities, info, budget, *, prepared=None):
    _, table_query = parse(rule, entities)
    query = prepared or table_query
    result = execute_source(info, query, budget)
    if (result.get('status') != 'success' or result.get('truncated') or result.get('columns') != ['violations']
            or len(result.get('rows', [])) != 1 or len(result['rows'][0]) != 1
            or type(result['rows'][0][0]) is not int or result['rows'][0][0] < 0):
        fail('QUALITY_UNKNOWN', '质量检查未得到完整计数，不能视为通过', 422)
    return {'rule_id': rule['id'], 'version': rule['version'], 'violations': result['rows'][0][0],
            'status': 'passed' if result['rows'][0][0] == 0 else 'failed', 'method': 'exact_count', 'sampled': False,
            'scope': 'authorized_plan_population' if prepared else 'approved_source_table'}
