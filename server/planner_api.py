"""Reader-facing P3 tools. Every call is authenticated by the application's gateway."""
import time
from flask import Blueprint, g
from ontology.registry import Registry, fail
from ontology.runtime import namespace, detail
from ontology.v2 import invalid
from ontology.planner import runtime
from .ontology_api import body, sources
from .execution import request_deadline, bounded_int

blueprint = Blueprint('planner', __name__)


@blueprint.before_request
def budget():
    g.planner_deadline_token = request_deadline.set(time.monotonic() + bounded_int('QUERY_DEADLINE_SECONDS', 60, 1, 600))


@blueprint.teardown_request
def reset_budget(error):
    token = getattr(g, 'planner_deadline_token', None)
    if token is not None:
        request_deadline.reset(token)
        g.planner_deadline_token = None


@blueprint.post('/v1/questions/check')
def check():
    return runtime.check_question(Registry(), g.principal, **body(('source_ids', 'question'), ('release_id',)))


@blueprint.post('/v1/query-plans/validate')
def validate():
    return runtime.create_plan(Registry(), g.principal, sources(), **body(('intent',), ('release_id',))), 201


@blueprint.get('/v1/query-plans/<identity>')
def plan(identity):
    return runtime.get_plan(Registry(), g.principal, identity)


@blueprint.post('/v1/queries/execute')
def execute():
    data = body(('plan_id', 'idempotency_key'))
    if not isinstance(data['plan_id'], str): raise invalid('计划标识无效')
    result = runtime.execute_plan(Registry(), g.principal, sources(), **data)
    return result, 202 if result['state'] == 'running' else 200 if result['state'] == 'succeeded' else 422


@blueprint.get('/v1/executions/<identity>')
def execution(identity):
    return runtime.get_execution(Registry(), g.principal, identity, sources())


@blueprint.get('/v1/metrics/<path:identity>')
def metric(identity):
    result = detail(Registry(), g.principal, identity)
    if result['entity']['kind'] != 'Metric': fail('NOT_FOUND', '指标不存在或不可访问', 404)
    return result


@blueprint.post('/v1/relations/plan')
def relations():
    data = body(('source_ids', 'left_dataset', 'right_dataset'), ('release_id',))
    if not isinstance(data['source_ids'], list) or len(data['source_ids']) != 1 or not isinstance(data['source_ids'][0], str): raise invalid('需明确单源范围')
    runtime.rollout(g.principal, data['source_ids'])
    pin = Registry().pin(namespace(), g.principal, data.get('release_id'))
    for key in ('left_dataset', 'right_dataset'):
        e = pin['entities'].get(data[key]) if isinstance(data[key], str) else None
        if not e or e['kind'] != 'Dataset' or e['source_ids'] != data['source_ids']: fail('NOT_FOUND', '数据集不存在或不可访问', 404)
    matches = [e for e in pin['entities'].values() if e['kind'] == 'Relation' and e['source_ids'] == data['source_ids']
               and e['attributes']['left_dataset'] == data['left_dataset'] and e['attributes']['right_dataset'] == data['right_dataset']
               and e['attributes']['cardinality'] == 'many-to-one' and e['attributes']['join_type'] in ('left', 'inner')]
    return {'release_id': pin['release_id'], 'status': 'unanswerable' if not matches else 'ambiguous' if len(matches) > 1 else 'requires_validation',
            'relations': [{'id': e['id'], 'version': e['version'], 'attributes': e['attributes']} for e in matches]}
