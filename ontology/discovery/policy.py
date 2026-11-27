"""Operator-owned discovery scope. No connection details or SQL are accepted from callers."""
import json
import os
import re
from server.errors import ServiceError
from ontology.v2 import invalid

IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
SENSITIVE = re.compile(r'password|passwd|secret|token|email|phone|mobile|address|身份证|密码|邮箱|电话', re.I)


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise invalid('采集标识符只允许 1–64 位字母、数字和下划线')
    return value


def quote(value):
    return '`' + identifier(value) + '`'


def load_policy(source_id, tables=None):
    if os.getenv('ONTOLOGY_DISCOVERY_ENABLED', 'false') != 'true':
        raise ServiceError('DISCOVERY_DISABLED', '自动采集尚未启用', 503)
    try:
        config = json.loads(os.getenv('ONTOLOGY_DISCOVERY_POLICY', '{}'))
        source = config[source_id]
        if not isinstance(source, dict) or set(source) - {'tables', 'sample_rows', 'relations'}:
            raise ValueError()
        allowed = source['tables']
        if not isinstance(allowed, dict) or not 1 <= len(allowed) <= 8:
            raise ValueError()
        for table, definition in allowed.items():
            identifier(table)
            if not isinstance(definition, dict) or set(definition) != {'columns', 'profile_columns', 'sensitive_columns'}:
                raise ValueError()
            for key in definition:
                values = definition[key]
                if not isinstance(values, list) or len(values) > 64 or len(set(values)) != len(values):
                    raise ValueError()
                for value in values:
                    identifier(value)
            if not definition['columns'] or not set(definition['profile_columns'] + definition['sensitive_columns']) <= set(definition['columns']):
                raise ValueError()
        selected = list(allowed) if tables is None else tables
        if (not isinstance(selected, list) or not selected or any(not isinstance(t, str) for t in selected)
                or len(set(selected)) != len(selected) or not set(selected) <= set(allowed)):
            raise ValueError()
        sample = source.get('sample_rows', 200)
        if type(sample) is not int or not 1 <= sample <= 1000:
            raise ValueError()
        relations = source.get('relations', [])
        if not isinstance(relations, list) or len(relations) > 16:
            raise ValueError()
        for relation in relations:
            if not isinstance(relation, dict) or set(relation) != {'left_table', 'left_keys', 'right_table', 'right_keys'}:
                raise ValueError()
            for side in ('left', 'right'):
                table, keys = relation[side + '_table'], relation[side + '_keys']
                if (table not in allowed or not isinstance(keys, list) or not 1 <= len(keys) <= 8
                        or len(set(keys)) != len(keys) or not set(keys) <= set(allowed[table]['columns'])):
                    raise ValueError()
            if len(relation['left_keys']) != len(relation['right_keys']):
                raise ValueError()
        result = {'source_id': source_id, 'tables': {t: allowed[t] for t in sorted(selected)},
                  'sample_rows': sample, 'relations': [r for r in relations if r['left_table'] in selected and r['right_table'] in selected]}
        return result
    except (KeyError, TypeError, ValueError):
        raise ServiceError('DISCOVERY_CONFIGURATION', '采集源、表列白名单或预算配置无效', 503) from None


def sensitive(policy, table, column):
    return column in policy['tables'][table]['sensitive_columns'] or bool(SENSITIVE.search(column))
