"""Synthetic P3 contracts, shared by unit, SQL integration, and documentation fixtures."""
import copy
from ontology.v2 import entity, canonical, digest


def document(database='db_a', policy=True):
    entities = []
    def add(kind, identity, name, attrs, deps=()):
        entities.append(entity(kind, identity, name, 'fixture-owner', ['a'], attrs, dependencies=list(deps), availability='reference'))
    add('DataSource', 'source:a', '合成订单源', dict(engine='mysql', database=database, credential_ref='source:a'))
    for table, columns, pk in [
        ('orders', [('tenant_id', 'int'), ('id', 'int'), ('customer_id', 'int'), ('amount', 'decimal(12,2)'), ('units', 'int'), ('created_at', 'datetime'), ('owner_id', 'varchar(80)')], ['tenant_id', 'id']),
        ('customers', [('tenant_id', 'int'), ('id', 'int'), ('region', 'varchar(80)')], ['tenant_id', 'id']),
    ]:
        add('Dataset', 'dataset:' + table, table, dict(physical_name=table, primary_key=['field:' + table + ':' + k for k in pk]), ['source:a'])
        for name, dtype in columns:
            add('Field', 'field:' + table + ':' + name, name, dict(dataset_id='dataset:' + table, physical_name=name, dtype=dtype, primary_key=name in pk), ['dataset:' + table])
    relation = dict(left_dataset='dataset:orders', left_keys=['field:orders:tenant_id', 'field:orders:customer_id'], right_dataset='dataset:customers', right_keys=['field:customers:tenant_id', 'field:customers:id'], join_type='left', cardinality='many-to-one')
    add('Relation', 'relation:customer', '订单客户', relation, [relation['left_dataset'], relation['right_dataset'], *relation['left_keys'], *relation['right_keys']])
    for name, expression, aggregation, fields, unit in [
        ('sales', 'SUM(amount)', 'sum', ['amount'], 'CNY'), ('average', 'AVG(amount)', 'avg', ['amount'], 'CNY'),
        ('price', 'SUM(amount)/NULLIF(SUM(units),0)', 'ratio', ['amount', 'units'], 'CNY/件'),
        ('buyers', 'COUNT(DISTINCT customer_id)', 'count_distinct', ['customer_id'], '人'),
        ('count', 'COUNT(*)', 'count', [], '条'),
    ]:
        field_ids = ['field:orders:' + k for k in fields]; grain = ['field:orders:tenant_id', 'field:orders:id']
        add('Metric', 'metric:' + name, name, dict(expression=expression, aggregation=aggregation, field_ids=field_ids, grain=grain, unit=unit,
                                                 time_definition='created_at 存储为 Asia/Shanghai 本地时间，按订单主键聚合', filters=[]), ['dataset:orders', *grain, *field_ids])
        attrs = dict(metric_id='metric:' + name, dataset_id='dataset:orders', dimensions=['field:customers:region'],
                     filter_fields=['field:orders:tenant_id', 'field:orders:owner_id', 'field:orders:amount', 'field:customers:region'],
                     relation_ids=['relation:customer'], time_field='field:orders:created_at', timezone='Asia/Shanghai', required_filters=['field:orders:tenant_id'])
        add('MetricContract', 'contract:' + name, name + ' 编译契约', attrs, list(dict.fromkeys([attrs['metric_id'], attrs['dataset_id'], *attrs['dimensions'], *attrs['filter_fields'], *attrs['relation_ids'], attrs['time_field']])))
    if policy:
        definition = canonical({'version': 1, 'filters': [{'field_id': 'field:orders:owner_id', 'op': 'eq', 'value': {'principal': 'subject'}}]})
        add('Policy', 'policy:owner', '主体记录范围', dict(target_ids=['source:a'], definition=definition), ['source:a', 'field:orders:owner_id'])
    return dict(schema_version=2, namespace='default', entities=entities, migration={})


def intent(metrics=None, dimensions=True):
    return dict(source_ids=['a'], metric_ids=metrics or ['metric:sales'],
                dimensions=['field:customers:region'] if dimensions else [], relation_ids=['relation:customer'] if dimensions else [],
                filters=[{'field_id': 'field:orders:tenant_id', 'op': 'eq', 'value': 1}],
                time={'start': '2026-11-01', 'end': '2026-12-01', 'timezone': 'Asia/Shanghai'})


def catalog(collector, doc=None):
    doc = doc or document(collector.info['db'])
    result = dict(source_id='a', database=collector.info['db'], tables=[], relations=[])
    for name, policy in sorted(collector.policy['tables'].items()):
        dataset = next(e for e in doc['entities'] if e['kind'] == 'Dataset' and e['attributes']['physical_name'] == name)
        fields = [e for e in doc['entities'] if e['kind'] == 'Field' and e['attributes']['dataset_id'] == dataset['id'] and e['attributes']['physical_name'] in policy['columns']]
        cols = [dict(name=e['attributes']['physical_name'], dtype=e['attributes']['dtype'], base_type=e['attributes']['dtype'].split('(')[0],
                     nullable='NO' if e['attributes']['primary_key'] else 'YES', collation='utf8mb4_0900_ai_ci' if 'varchar' in e['attributes']['dtype'] else None, position=i+1, comment='', table_name=name) for i, e in enumerate(fields)]
        pk = [e['attributes']['physical_name'] for e in fields if e['id'] in dataset['attributes']['primary_key']]
        result['tables'].append(dict(name=name, columns=cols, primary_key=pk, unique_keys={'PRIMARY': pk}, engine='InnoDB', comment=''))
    result['fingerprint'] = digest(result)
    return copy.deepcopy(result)
