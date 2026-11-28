"""P4 administrative governance and reader-scoped analysis endpoints."""
from flask import Blueprint, g
from ontology.registry import Registry
from ontology.runtime import namespace
from ontology.governance import runtime as governance
from ontology.analysis import runtime as analysis
from .ontology_api import admin, body, sources
from .planner_api import budget, reset_budget

blueprint = Blueprint('governance', __name__)
blueprint.before_request(budget)
blueprint.teardown_request(reset_budget)


@blueprint.post('/v1/ontology/governance/runs')
def scan():
    admin()
    return governance.scan(Registry(), g.principal, sources(), **body(('source_id', 'idempotency_key')))


@blueprint.get('/v1/ontology/governance/runs/<identity>')
def run(identity):
    admin()
    return governance.get_run(Registry(), g.principal, identity)


@blueprint.get('/v1/ontology/governance/incidents')
def incidents():
    admin()
    return governance.incidents(Registry(), g.principal)


@blueprint.post('/v1/ontology/governance/incidents/<identity>/transition')
def transition(identity):
    admin()
    return governance.transition(Registry(), g.principal, sources(), identity,
                                 **body(('expected_revision', 'action', 'reason'), ('owner', 'run_id')))


@blueprint.post('/v1/governance/status')
def status():
    return governance.status(Registry(), g.principal, **body(('identity',), ('release_id',)))


@blueprint.post('/v1/analysis/tasks')
def analyze():
    data = body(('intent', 'idempotency_key'), ('verification',))
    result = analysis.run(Registry(), g.principal, sources(), **data)
    return result, 202 if result['state'] == 'running' else 200 if result['state'] == 'succeeded' else 422


@blueprint.get('/v1/analysis/tasks/<identity>')
def task(identity):
    return analysis.get_task(Registry(), g.principal, sources(), identity)
