"""Run only in the documented disposable, network-disabled DTO experiment container.

The upstream models.py is supplied externally and checked by Git blob SHA before import.
No upstream traversal, database DAL, graph writer, model call, or production credential is loaded.
"""
import copy
import hashlib
import importlib.util
import json
import pathlib
import sys


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


bridge = module('bridge', '/experiment/upstream.py')
path = pathlib.Path('/experiment/models.py')
data = path.read_bytes()
assert hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() == bridge.MODELS_BLOB_SHA
models = module('pinned_models', path)
raw = models.RawTableTermsResult.model_validate({'terms': [{
    'name': 'Purchase Order', 'description': 'Synthetic order fixture',
    'attributes': [{'source_column': 'order_id', 'display_name': 'Order ID'}],
}]})
sanitized = models.TableTermsResult.model_validate(raw.model_dump()).model_dump()
catalog = {'fingerprint': 'synthetic-catalog', 'tables': [{'name': 'orders', 'columns': [{'name': 'order_id'}]}]}
envelope = dict(protocol=bridge.PROTOCOL, upstream_sha=bridge.UPSTREAM_SHA,
                catalog_fingerprint=catalog['fingerprint'], tables=[dict(table='orders', **sanitized)])
assert bridge.adapt(envelope, catalog)[0]['attributes'][0]['source_column'] == 'order_id'
checks = ['pinned_blob_integrity', 'real_raw_dto', 'real_sanitized_dto', 'bounded_bridge']
for label, mutate in [
    ('unknown_column_rejected', lambda e: e['tables'][0]['terms'][0]['attributes'][0].update(source_column='secret')),
    ('extra_sql_rejected', lambda e: e['tables'][0]['terms'][0].update(sql='DROP TABLE orders')),
    ('wrong_version_rejected', lambda e: e.update(upstream_sha='main')),
    ('raw_rows_rejected', lambda e: e['tables'][0].update(sample_rows=[{'order_id': 'private'}])),
]:
    invalid = copy.deepcopy(envelope); mutate(invalid)
    try:
        bridge.adapt(invalid, catalog)
    except ValueError:
        checks.append(label)
    else:
        raise AssertionError(label)
print(json.dumps({'checks': checks, 'upstream_sha': bridge.UPSTREAM_SHA,
                  'models_blob_sha': bridge.MODELS_BLOB_SHA, 'published': False}))
