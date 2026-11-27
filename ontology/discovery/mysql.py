"""Fixed catalog probes and aggregate-only bounded samples. Never export source rows."""
from datetime import datetime, timezone
import time
from server.db.mysql import execute_read
from server.query_policy import PreparedQuery
from server.execution import QueryBudget
from server.errors import ServiceError
from ontology.v2 import digest
from .policy import quote, sensitive


def now():
    return datetime.now(timezone.utc).isoformat()


class Collector:
    def __init__(self, info, policy, budget=None):
        self.info, self.policy = info, policy
        self.budget = budget or QueryBudget(deadline=time.monotonic() + 45, max_queries=64)

    def read(self, sql, params=None, *, catalog=False, max_rows=2048):
        info = dict(self.info, db='information_schema') if catalog else self.info
        # Catalog access is private to fixed probes below; online user SQL still cannot cross databases.
        output = execute_read(info, PreparedQuery(sql, params or {}), timeout=min(5, self.budget.consume()),
                              max_rows=max_rows, max_bytes=2 * 1024 * 1024)
        if output.get('truncated'):
            raise ServiceError('DISCOVERY_TRUNCATED', '元数据或证据不完整，未编译候选', 422)
        return [dict(zip(output['columns'], row)) for row in output['rows']]

    def catalog(self):
        names = list(self.policy['tables'])
        params = {'p0': self.info['db'], **{'p' + str(i + 1): value for i, value in enumerate(names)}}
        slots = ','.join(':p' + str(i + 1) for i in range(len(names)))
        tables = self.read('SELECT TABLE_NAME AS name,TABLE_TYPE AS kind,ENGINE AS engine,TABLE_COMMENT AS comment '
                           f'FROM information_schema.TABLES WHERE TABLE_SCHEMA=:p0 AND TABLE_NAME IN ({slots}) ORDER BY TABLE_NAME', params, catalog=True)
        if {t['name'] for t in tables} != set(names) or any(t['kind'] != 'BASE TABLE' for t in tables):
            raise ServiceError('CATALOG_SCOPE_INVALID', '配置表不可见或不是基础表', 422)
        columns = self.read('SELECT TABLE_NAME AS table_name,COLUMN_NAME AS name,COLUMN_TYPE AS dtype,DATA_TYPE AS base_type,'
                            'IS_NULLABLE AS nullable,COLLATION_NAME AS collation,ORDINAL_POSITION AS position,COLUMN_COMMENT AS comment '
                            f'FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=:p0 AND TABLE_NAME IN ({slots}) ORDER BY TABLE_NAME,ORDINAL_POSITION', params, catalog=True)
        indexes = self.read('SELECT TABLE_NAME AS table_name,INDEX_NAME AS name,NON_UNIQUE AS non_unique,SEQ_IN_INDEX AS position,'
                            'COLUMN_NAME AS column_name,SUB_PART AS prefix_length '
                            f'FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=:p0 AND TABLE_NAME IN ({slots}) ORDER BY TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX', params, catalog=True)
        fks = self.read('SELECT TABLE_NAME AS table_name,CONSTRAINT_NAME AS name,COLUMN_NAME AS column_name,ORDINAL_POSITION AS position,'
                       'REFERENCED_TABLE_SCHEMA AS target_schema,REFERENCED_TABLE_NAME AS target_table,REFERENCED_COLUMN_NAME AS target_column '
                       f'FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=:p0 AND TABLE_NAME IN ({slots}) '
                       'AND REFERENCED_TABLE_NAME IS NOT NULL ORDER BY TABLE_NAME,CONSTRAINT_NAME,ORDINAL_POSITION', params, catalog=True)
        result = {'source_id': self.info['source_id'], 'database': self.info['db'], 'tables': [], 'relations': []}
        for table in tables:
            name = table['name']
            wanted = self.policy['tables'][name]['columns']
            cols = [dict(c, comment=(c['comment'] or '')[:2000]) for c in columns if c['table_name'] == name and c['name'] in wanted]
            if {c['name'] for c in cols} != set(wanted):
                raise ServiceError('CATALOG_SCOPE_INVALID', '配置列不可见，未编译候选', 422)
            keys = {}
            for index in indexes:
                if index['table_name'] == name:
                    keys.setdefault(index['name'], []).append(index)
            unique = {k: [r['column_name'] for r in rows] for k, rows in keys.items()
                      if all(r['non_unique'] == 0 and r['prefix_length'] is None and r['column_name'] in wanted for r in rows)}
            result['tables'].append(dict(name=name, engine=table['engine'], comment=(table['comment'] or '')[:2000],
                                         columns=cols, primary_key=unique.get('PRIMARY', []), unique_keys=unique))
        groups = {}
        for fk in fks:
            groups.setdefault((fk['table_name'], fk['name']), []).append(fk)
        for (table, name), keys in groups.items():
            target = keys[0]['target_table']
            if (target in names and all(k['target_schema'] == self.info['db'] and k['target_table'] == target
                                       and k['column_name'] in self.policy['tables'][table]['columns']
                                       and k['target_column'] in self.policy['tables'][target]['columns'] for k in keys)):
                result['relations'].append(dict(left_table=table, left_keys=[k['column_name'] for k in keys],
                                                right_table=target, right_keys=[k['target_column'] for k in keys],
                                                origin='declared', constraint=name))
        seen = {(r['left_table'], tuple(r['left_keys']), r['right_table'], tuple(r['right_keys'])) for r in result['relations']}
        for relation in self.policy['relations']:
            key = (relation['left_table'], tuple(relation['left_keys']), relation['right_table'], tuple(relation['right_keys']))
            if key not in seen:
                result['relations'].append(dict(relation, origin='operator_candidate', constraint=None))
                seen.add(key)
        if len(result['relations']) > 16:
            raise ServiceError('DISCOVERY_LIMIT', '关系数量超出本轮预算，请缩小表范围', 422)
        result['fingerprint'] = digest(result)
        return result

    def profile(self, table):
        name = table['name']
        allowed = self.policy['tables'][name]['profile_columns']
        usable = [c for c in allowed if not sensitive(self.policy, name, c)]
        excluded = [c for c in allowed if c not in usable]
        base = {'table': name, 'method': 'ordered_prefix' if table['primary_key'] else 'unordered_prefix',
                'limit': self.policy['sample_rows'], 'observed_at': now(), 'not_random': True, 'columns': {},
                'excluded': {c: 'sensitive' for c in excluded}}
        if not usable:
            return dict(base, status='restricted', sampled_rows=None)
        fields = ','.join(quote(c) for c in usable)
        order = ' ORDER BY ' + ','.join(quote(c) for c in table['primary_key']) if table['primary_key'] else ''
        aggregates = ['COUNT(*) AS sampled_rows']
        for i, col in enumerate(usable):
            q = quote(col)
            aggregates.extend([f'SUM(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END) AS n{i}', f'COUNT(DISTINCT {q}) AS d{i}'])
        sql = f"SELECT {','.join(aggregates)} FROM (SELECT {fields} FROM {quote(name)}{order} LIMIT {self.policy['sample_rows']}) AS sampled"
        row = self.read(sql, max_rows=1)[0]
        count = int(row['sampled_rows'])
        for i, col in enumerate(usable):
            nulls = int(row['n' + str(i)] or 0)
            distinct = int(row['d' + str(i)])
            base['columns'][col] = {'null_count': nulls, 'distinct_count': distinct,
                                     'null_fraction': nulls / count if count else None,
                                     'distinct_nonnull_fraction': distinct / (count - nulls) if count > nulls else None}
        return dict(base, status='sampled', sampled_rows=count)

    def relation(self, relation, tables):
        left, right = tables[relation['left_table']], tables[relation['right_table']]
        lk, rk = relation['left_keys'], relation['right_keys']
        base = dict(relation, observed_at=now(), sample_limit=self.policy['sample_rows'], method='prefix_membership_and_exact_target_uniqueness',
                    target_uniqueness=None, orphan_fraction=None, containment=None, fanout_ratio=None, sampled_rows=None,
                    nonnull_count=None, orphan_count=None, matched_rows=None, validation='unknown')
        if any(sensitive(self.policy, t, c) for t, keys in ((left['name'], lk), (right['name'], rk)) for c in keys):
            return dict(base, validation='restricted')
        lookup = {t['name']: {c['name']: c for c in t['columns']} for t in (left, right)}
        if any((lookup[left['name']][a]['dtype'], lookup[left['name']][a]['collation']) !=
               (lookup[right['name']][b]['dtype'], lookup[right['name']][b]['collation']) for a, b in zip(lk, rk)):
            return dict(base, validation='type_mismatch')
        keys = ','.join(quote(c) for c in rk)
        nonnull_target = ' AND '.join(f'{quote(c)} IS NOT NULL' for c in rk)
        duplicates = self.read(f'SELECT COUNT(*) AS duplicate_groups FROM (SELECT {keys} FROM {quote(right["name"])} '
                               f'WHERE {nonnull_target} GROUP BY {keys} HAVING COUNT(*) > 1) AS duplicates', max_rows=1)[0]
        duplicate_groups = int(duplicates['duplicate_groups'])
        order = ' ORDER BY ' + ','.join(quote(c) for c in left['primary_key']) if left['primary_key'] else ''
        left_select = ','.join(f'{quote(k)} AS k{i}' for i, k in enumerate(lk))
        right_select = ','.join(f'{quote(k)} AS k{i}' for i, k in enumerate(rk))
        conditions = ' AND '.join(f's.k{i} = t.k{i}' for i in range(len(lk)))
        nonnull_left = ' AND '.join(f's.k{i} IS NOT NULL' for i in range(len(lk)))
        sql = (f'SELECT COUNT(*) AS sampled_rows,SUM(CASE WHEN {nonnull_left} THEN 1 ELSE 0 END) AS nonnull_count,'
               f'SUM(CASE WHEN {nonnull_left} AND t.hits IS NULL THEN 1 ELSE 0 END) AS orphan_count,'
               'SUM(COALESCE(t.hits,0)) AS matched_rows '
               f'FROM (SELECT {left_select} FROM {quote(left["name"])}{order} LIMIT {self.policy["sample_rows"]}) AS s '
               f'LEFT JOIN (SELECT {right_select},COUNT(*) AS hits FROM {quote(right["name"])} '
               f'WHERE {nonnull_target} GROUP BY {keys}) AS t ON {conditions}')
        row = self.read(sql, max_rows=1)[0]
        n, orphans, matched = (int(row[k] or 0) for k in ('nonnull_count', 'orphan_count', 'matched_rows'))
        return dict(base, sampled_rows=int(row['sampled_rows']), nonnull_count=n, orphan_count=orphans, matched_rows=matched,
                    target_uniqueness={'method': 'exact_non_null_scan', 'duplicate_groups': duplicate_groups, 'unique': duplicate_groups == 0},
                    orphan_fraction=orphans / n if n else None, containment=1 - orphans / n if n else None,
                    fanout_ratio=matched / (n - orphans) if n > orphans else None,
                    validation='unknown' if not n else 'sampled_pass' if not duplicate_groups and not orphans and matched == n else 'sampled_fail')

    def collect(self):
        before = self.catalog()
        profiles, evidence, errors = [], [], []
        for table in before['tables']:
            try:
                profiles.append(self.profile(table))
            except Exception as error:
                errors.append({'stage': 'profile', 'table': table['name'], 'code': error.code if isinstance(error, ServiceError) else 'SOURCE_UNAVAILABLE'})
        lookup = {t['name']: t for t in before['tables']}
        for relation in before['relations']:
            try:
                evidence.append(self.relation(relation, lookup))
            except Exception as error:
                errors.append({'stage': 'relation', 'code': error.code if isinstance(error, ServiceError) else 'SOURCE_UNAVAILABLE'})
        after = self.catalog()
        stable = before['fingerprint'] == after['fingerprint']
        if not stable:
            errors.append({'stage': 'catalog', 'code': 'SCHEMA_CHANGED'})
        return {'catalog': before, 'catalog_after': after['fingerprint'], 'schema_stable': stable,
                'profiles': profiles, 'relations': evidence, 'errors': errors, 'observed_at': now(), 'sql_calls': self.budget.calls}
