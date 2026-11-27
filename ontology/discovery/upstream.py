"""Bounded DTO bridge for a pinned NVIDIA export. No imports, tools, SQL or network execution."""
import json

UPSTREAM_SHA = 'a12fec75226a54ffc91752cca9538f1f01f760eb'
MODELS_BLOB_SHA = '2e5f44141434e15d09f4b0bbeb73437f2b4a9e38'
PROTOCOL = 'nvidia-term-proposals/v1'


def adapt(envelope, catalog):
    if not isinstance(envelope, dict) or set(envelope) != {'protocol', 'upstream_sha', 'catalog_fingerprint', 'tables'}:
        raise ValueError('invalid upstream envelope')
    if (envelope['protocol'] != PROTOCOL or envelope['upstream_sha'] != UPSTREAM_SHA
            or envelope['catalog_fingerprint'] != catalog['fingerprint']
            or len(json.dumps(envelope, ensure_ascii=False).encode()) > 131072):
        raise ValueError('untrusted upstream version or catalog')
    records = envelope['tables']
    if not isinstance(records, list) or len(records) > 8:
        raise ValueError('table limit')
    lookup = {t['name']: {c['name'] for c in t['columns']} for t in catalog['tables']}
    result, seen = [], set()
    for record in records:
        if (not isinstance(record, dict) or set(record) != {'table', 'terms'} or not isinstance(record['table'], str)
                or record['table'] not in lookup or record['table'] in seen or not isinstance(record['terms'], list)
                or len(record['terms']) > 16):
            raise ValueError('table binding invalid')
        table = record['table']; seen.add(table)
        names = set()
        for term in record['terms']:
            if not isinstance(term, dict) or set(term) - {'name', 'description', 'synonyms', 'attributes'} or not {'name', 'attributes'} <= set(term):
                raise ValueError('term fields invalid')
            if not isinstance(term['name'], str) or not 1 <= len(term['name']) <= 256 or term['name'] in names:
                raise ValueError('term name invalid')
            names.add(term['name'])
            description, synonyms = term.get('description', ''), term.get('synonyms', [])
            if (not isinstance(description, str) or len(description) > 4000 or not isinstance(synonyms, list)
                    or len(synonyms) > 16 or any(not isinstance(s, str) or not 1 <= len(s) <= 128 for s in synonyms)):
                raise ValueError('term description invalid')
            attributes = term['attributes']
            if not isinstance(attributes, list) or not 1 <= len(attributes) <= 64:
                raise ValueError('empty or oversized attributes')
            columns = set()
            for attr in attributes:
                if (not isinstance(attr, dict) or set(attr) != {'source_column', 'display_name'}
                        or not isinstance(attr['source_column'], str) or attr['source_column'] not in lookup[table]
                        or attr['source_column'] in columns or not isinstance(attr['display_name'], str)
                        or not 1 <= len(attr['display_name']) <= 256):
                    raise ValueError('column assignment invalid')
                columns.add(attr['source_column'])
            result.append({'table': table, 'name': term['name'], 'description': description,
                           'synonyms': list(dict.fromkeys(synonyms)), 'attributes': attributes})
    return result


if __name__ == '__main__':
    import sys
    try:
        raw = sys.stdin.buffer.read(262145)
        if len(raw) > 262144:
            raise ValueError('input limit')
        data = json.loads(raw)
        print(json.dumps({'terms': adapt(data['upstream'], data['catalog']), 'published': False}, ensure_ascii=False))
    except Exception:
        print('{"error":"INVALID_UPSTREAM_ARTIFACT","published":false}')
        raise SystemExit(1)
