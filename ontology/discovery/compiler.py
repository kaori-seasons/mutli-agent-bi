"""Deterministic physical compiler plus optional pinned semantic proposals; every output is candidate."""
from ontology.schema import stable_id
from ontology.v2 import entity, validate, canonical, digest
from .upstream import adapt, UPSTREAM_SHA

VERSION = 'mysql-candidates/1'


def identities(namespace, source, database):
    return lambda kind, *parts: stable_id(kind, namespace, source, database, *parts)


def compile_candidates(observations, namespace, owner, run_id, upstream=None):
    catalog = observations['catalog']
    source, database = catalog['source_id'], catalog['database']
    ident = identities(namespace, source, database)
    entities = []
    provenance = {'compiler': VERSION, 'run_id': run_id, 'schema_fingerprint': catalog['fingerprint']}
    def add(kind, identity, name, attributes, deps=None, description='', extra=None, synonyms=None):
        value = entity(kind, identity, name, owner, [source], attributes, dependencies=deps or [],
                       description=description, legacy=dict(provenance, **(extra or {})), synonyms=synonyms or [])
        entities.append(value)
        return identity
    source_id = add('DataSource', ident('source'), source, dict(engine='mysql', database=database, credential_ref='source:' + source))
    for table in catalog['tables']:
        name = table['name']; table_id = ident('dataset', name)
        pk = [ident('field', name, k) for k in table['primary_key']]
        add('Dataset', table_id, name, dict(physical_name=name, primary_key=pk), [source_id], table['comment'])
        for column in table['columns']:
            add('Field', ident('field', name, column['name']), column['name'],
                dict(dataset_id=table_id, physical_name=column['name'], dtype=column['dtype'], primary_key=column['name'] in table['primary_key']),
                [table_id], column['comment'], {'nullable': column['nullable'], 'collation': column['collation']})
        concept = add('BusinessConcept', ident('concept', name), name, {'definition': table['comment']}, [table_id],
                      extra={'blockers': ['business_definition_review_required']})
        add('Mapping', ident('mapping', name), name + ' 物理映射', dict(concept_id=concept, target_ids=[table_id], expression=''), [concept, table_id])
        add('Metric', ident('metric', name, 'row_count'), name + ' 记录数',
            dict(expression='COUNT(*)', field_ids=pk, aggregation='count', grain=pk, unit='', time_definition='', filters=[]),
            [table_id, *pk], extra={'blockers': ['unit_time_scope_and_business_review_required']})
    # Include every declared/operator relation even if its probe failed: it stays an unknown candidate.
    evidence_by_key = {(r['left_table'], tuple(r['left_keys']), r['right_table'], tuple(r['right_keys'])): r for r in observations['relations']}
    for relation in catalog['relations']:
        key = (relation['left_table'], tuple(relation['left_keys']), relation['right_table'], tuple(relation['right_keys']))
        identity = ident('relation', *[canonical(v) for v in key])
        lt, rt = ident('dataset', key[0]), ident('dataset', key[2])
        lk = [ident('field', key[0], c) for c in key[1]]; rk = [ident('field', key[2], c) for c in key[3]]
        deps = list(dict.fromkeys([lt, rt, *lk, *rk]))
        add('Relation', identity, key[0] + ' → ' + key[2],
            dict(left_dataset=lt, left_keys=lk, right_dataset=rt, right_keys=rk, join_type='unknown', cardinality='unknown'), deps,
            extra={'origin': relation['origin'], 'blockers': ['relationship_business_role_review_required']})
        evidence = evidence_by_key.get(key, dict(relation, validation='error', observed_at=observations['observed_at']))
        add('Evidence', ident('evidence', identity), '关系观测',
            dict(target_ids=[identity], method='mysql_relation_probe/v1', observed_at=evidence['observed_at'], summary=canonical(evidence)), [identity])
    for profile in observations['profiles']:
        table_id = ident('dataset', profile['table'])
        add('Evidence', ident('profile', profile['table']), profile['table'] + ' 画像',
            dict(target_ids=[table_id], method='mysql_aggregate_prefix/v1', observed_at=profile['observed_at'], summary=canonical(profile)), [table_id])
    if upstream is not None:
        for term in adapt(upstream, catalog):
            table, name = term['table'], term['name']
            refs = [ident('field', table, attr['source_column']) for attr in term['attributes']]
            concept = add('BusinessConcept', ident('upstream-concept', table, name), name, {'definition': term['description']},
                          [ident('dataset', table)], term['description'], {'upstream_sha': UPSTREAM_SHA}, term['synonyms'])
            add('Mapping', ident('upstream-mapping', table, name), name + ' 字段映射',
                dict(concept_id=concept, target_ids=refs, expression=''), [concept, *refs],
                extra={'upstream_sha': UPSTREAM_SHA, 'display_names': {a['source_column']: a['display_name'] for a in term['attributes']}})
    result = dict(schema_version=2, namespace=namespace, entities=entities,
                  migration={'adapter': VERSION, 'run_id': run_id, 'schema_fingerprint': catalog['fingerprint'],
                             'observations_digest': digest(observations), 'automatic': True})
    assert all(e['availability'] == 'candidate' for e in entities)
    return validate(result)
