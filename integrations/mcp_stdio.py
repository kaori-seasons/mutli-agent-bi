"""Bounded MCP 2025-06-18 stdio adapter over authenticated REST. No administration tools."""
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

VERSION = '2025-06-18'
MAX_LINE = 128 * 1024


def schema(required, optional=None):
    return {'type': 'object', 'properties': dict(required, **(optional or {})), 'required': list(required), 'additionalProperties': False}


STRING = {'type': 'string', 'minLength': 1, 'maxLength': 4096}
ID = {'type': 'string', 'pattern': '^[0-9a-f]{32}$'}
SOURCES = {'type': 'array', 'items': STRING, 'minItems': 1, 'maxItems': 8}
INTENT = {'type': 'object', 'description': 'P3 structured intent: source_ids, metric_ids, dimensions, filters, time, relation_ids. No SQL or identity fields.'}
TOOLS = {
    'knowledge_search': ('/v1/knowledge/search', schema({'question': STRING, 'source_ids': SOURCES, 'release_id': ID}, {'kinds': {'type': 'array', 'items': STRING}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}), 'Search approved knowledge in an explicitly pinned release.'),
    'question_check': ('/v1/questions/check', schema({'question': STRING, 'source_ids': SOURCES}, {'release_id': ID}), 'Check exact approved metric names and required clarification.'),
    'query_plan_validate': ('/v1/query-plans/validate', schema({'intent': INTENT}, {'release_id': ID}), 'Create a principal-bound controlled plan; never accepts SQL.'),
    'query_execute': ('/v1/queries/execute', schema({'plan_id': ID, 'idempotency_key': STRING}), 'Execute an approved plan under current scope and budgets.'),
    'execution_get': ('/v1/executions/', schema({'identity': ID}), 'Read your own execution, with current policy and incident checks.'),
    'analysis_run': ('/v1/analysis/tasks', schema({'intent': INTENT, 'idempotency_key': STRING}, {'verification': {'type': 'string', 'enum': ['auto', 'required', 'off']}}), 'Run a bounded task with optional independent additive-total verification.'),
    'analysis_get': ('/v1/analysis/tasks/', schema({'identity': ID}), 'Read your own task evidence; all child receipts are reauthorized.'),
    'governance_status': ('/v1/governance/status', schema({'identity': STRING}, {'release_id': ID}), 'Check isolation for a visible entity; no administrative incident data.'),
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def backend(name, arguments):
    base = os.getenv('MCP_BACKEND_URL', 'http://127.0.0.1:5020')
    url = urlsplit(base)
    if (url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password or url.query or url.fragment
            or url.path not in ('', '/') or (url.scheme == 'http' and url.hostname not in ('127.0.0.1', '::1', 'localhost'))):
        raise ValueError('invalid backend configuration')
    # Credentials are supplied by the operator, never by model arguments, URLs or protocol messages.
    token_path = os.getenv('MCP_ACCESS_TOKEN_FILE')
    if not token_path: raise ValueError('missing token configuration')
    path = Path(token_path)
    if path.stat().st_mode & 0o077: raise ValueError('token file must be private')
    with path.open() as source: token = source.read(16385).strip()
    if not token or len(token) > 16384 or any(c.isspace() for c in token): raise ValueError('invalid credential')
    route, _, _ = TOOLS[name]
    read = name in ('execution_get', 'analysis_get')
    if read: route += arguments['identity']
    request = Request(base.rstrip('/') + route, data=None if read else json.dumps(arguments).encode(),
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    opener = build_opener(NoRedirect(), ProxyHandler({}))
    try: response = opener.open(request, timeout=65)
    except HTTPError as error: response = error
    with response:
        raw = response.read(8*1024*1024+1)
        if len(raw) > 8*1024*1024: raise ValueError('response too large')
        result = json.loads(raw)
        if not isinstance(result, dict): raise ValueError('invalid response')
        return result, response.status >= 400


def validate_arguments(arguments, spec):
    if not isinstance(arguments, dict) or set(arguments) - set(spec['properties']) or not set(spec['required']) <= set(arguments): return False
    for key, value in arguments.items():
        rule = spec['properties'][key]; kind = rule['type']
        if kind == 'string' and (not isinstance(value, str) or not rule.get('minLength', 0) <= len(value) <= rule.get('maxLength', 4096)): return False
        if kind == 'integer' and (type(value) is not int or not rule.get('minimum', 0) <= value <= rule.get('maximum', 100)): return False
        if kind == 'object' and not isinstance(value, dict): return False
        if kind == 'array' and (not isinstance(value, list) or not rule.get('minItems', 0) <= len(value) <= rule.get('maxItems', 100) or any(not isinstance(x, str) for x in value)): return False
        if 'pattern' in rule and not re.fullmatch(rule['pattern'], value): return False
        if 'enum' in rule and value not in rule['enum']: return False
    return True


class Session:
    def __init__(self, call=backend):
        self.call, self.initialized, self.ready = call, False, False

    def dispatch(self, message):
        identity = message.get('id') if isinstance(message, dict) else None
        def error(code, text): return {'jsonrpc': '2.0', 'id': identity, 'error': {'code': code, 'message': text}}
        if (not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str)
                or ('id' in message and type(identity) not in (str, int))): return error(-32600, 'Invalid Request')
        method, params = message['method'], message.get('params', {})
        if 'id' not in message:
            if method == 'notifications/initialized' and self.initialized: self.ready = True
            return None
        if not isinstance(params, dict): return error(-32602, 'Invalid params')
        if method == 'initialize':
            if self.initialized or not isinstance(params.get('protocolVersion'), str) or not isinstance(params.get('capabilities'), dict) or not isinstance(params.get('clientInfo'), dict): return error(-32602, 'Invalid initialization')
            self.initialized = True
            result = {'protocolVersion': VERSION, 'capabilities': {'tools': {'listChanged': False}}, 'serverInfo': {'name': 'controlled-bi', 'version': '1.0.0'}}
        elif method == 'ping': result = {}
        elif not self.ready: return error(-32002, 'Server not initialized')
        elif method == 'tools/list':
            if set(params) - {'_meta'}: return error(-32602, 'Pagination is not supported')
            result = {'tools': [{'name': name, 'description': description, 'inputSchema': spec,
                                 'annotations': {'readOnlyHint': name not in ('query_plan_validate', 'query_execute', 'analysis_run'), 'destructiveHint': False, 'openWorldHint': False}}
                                for name, (_, spec, description) in TOOLS.items()]}
        elif method == 'tools/call':
            name, arguments = params.get('name'), params.get('arguments', {})
            if not isinstance(name, str) or name not in TOOLS: return error(-32602, 'Unknown tool')
            if set(params) - {'name', 'arguments', '_meta'} or not validate_arguments(arguments, TOOLS[name][1]): return error(-32602, 'Invalid tool arguments')
            try: data, failed = self.call(name, arguments)
            except Exception: data, failed = {'error': {'code': 'BACKEND_UNAVAILABLE', 'message': 'Authenticated backend request failed'}}, True
            result = {'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False, allow_nan=False)}], 'structuredContent': data, 'isError': failed}
        else: return error(-32601, 'Method not found')
        return {'jsonrpc': '2.0', 'id': identity, 'result': result}


def main():
    session = Session()
    def reject_constant(_): raise ValueError('Non-finite JSON number')
    while True:
        line = sys.stdin.buffer.readline(MAX_LINE+1)
        if not line: return
        if len(line) > MAX_LINE: return  # Never buffer unbounded attacker input.
        try: response = session.dispatch(json.loads(line, parse_constant=reject_constant))
        except (ValueError, UnicodeError, RecursionError): response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + '\n'); sys.stdout.flush()


if __name__ == '__main__': main()
