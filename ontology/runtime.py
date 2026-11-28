"""Permission-filtered knowledge reads and opt-in, pinned published-template execution."""
import json
import hashlib
import os
import re

from server.errors import ServiceError
from server.query_policy import bind_template
from .registry import Registry, validate_executable, fail
from .v2 import parameter, invalid
from .service import tokens


def query_sources():
    try:
        value = json.loads(os.getenv('ONTOLOGY_QUERY_SOURCES', '[]'))
        if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value) or len(set(value)) != len(value):
            raise ValueError()
        return set(value)
    except (ValueError, TypeError):
        fail('REGISTRY_CONFIGURATION', '本体查询灰度配置无效', 503)


def namespace():
    value = os.getenv('ONTOLOGY_REGISTRY_NAMESPACE', 'default')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value):
        fail('REGISTRY_CONFIGURATION', '本体命名空间配置无效', 503)
    return value


def search(registry, principal, question, release_id=None, kinds=None, source_ids=None, limit=10):
    if not isinstance(question, str) or not 1 <= len(question) <= 4096 or type(limit) is not int or not 1 <= limit <= 100:
        raise invalid('检索问题或数量限制无效')
    if kinds is not None and (not isinstance(kinds, list) or any(not isinstance(x, str) for x in kinds)):
        raise invalid()
    if source_ids is not None and (not isinstance(source_ids, list) or not source_ids
                                  or any(not isinstance(x, str) for x in source_ids)):
        raise invalid()
    if source_ids is not None and not set(source_ids) <= principal.sources:
        fail('FORBIDDEN_SCOPE', '无权检索所选数据源', 403)
    pin = registry.pin(namespace(), principal, release_id)
    terms, results = tokens(question), []
    for identity, entity in pin['entities'].items():
        if kinds is not None and entity['kind'] not in kinds:
            continue
        if source_ids is not None and not set(entity['source_ids']) <= set(source_ids):
            continue
        exact = question.strip().casefold() in [s.casefold() for s in [entity['name'], *entity['synonyms']]]
        score = 1.0 if exact else len(terms & set(pin['index'][identity])) / max(1, len(terms))
        if score > 0:
            # Never disclose raw legacy payloads or global migration reports to query clients.
            results.append({'id': identity, 'kind': entity['kind'], 'version': entity['version'], 'name': entity['name'],
                            'description': entity['description'], 'source_ids': entity['source_ids'],
                            'availability': entity['availability'], 'score': score})
    return {'release_id': pin['release_id'], 'candidates': sorted(results, key=lambda x: (-x['score'], x['id']))[:limit]}


def detail(registry, principal, identity, release_id=None):
    pin = registry.pin(namespace(), principal, release_id)
    entity = pin['entities'].get(identity)
    if not entity:
        fail('NOT_FOUND', '对象不存在或不可访问', 404)
    return {'release_id': pin['release_id'], 'entity': {k: v for k, v in entity.items() if k != 'legacy'}}


def match_parameters(question, template):
    """Bounded exact pattern match; never casefold/strip extracted values or guess a fuzzy winner."""
    pattern = template['attributes']['question_pattern']
    slots = template['attributes']['parameters']
    pieces = re.split(r'(\{\d+\})', pattern)
    states = [(0, {})]
    for piece in pieces:
        next_states = []
        if re.fullmatch(r'\{\d+\}', piece):
            index = int(piece[1:-1])
            if index >= len(slots):
                return []
            for position, captures in states:
                for end in range(position + 1, min(len(question), position + 96) + 1):
                    value = question[position:end]
                    if str(index) in captures and captures[str(index)] != value:
                        continue
                    next_states.append((end, dict(captures, **{str(index): value})))
                    if len(next_states) > 4096:
                        fail('AMBIGUOUS_DEFINITION', '模板槽位匹配过于模糊，请显式指定参数', 422)
        else:
            next_states = [(p + len(piece), captured) for p, captured in states if question.startswith(piece, p)]
        if len(next_states) > 4096:
            fail('AMBIGUOUS_DEFINITION', '模板槽位匹配过于模糊，请显式指定参数', 422)
        states = next_states
        if not states:
            return []
    matches = []
    for end, captured in states:
        if end != len(question) or set(captured) != {str(i) for i in range(len(slots))}:
            continue
        values = []
        try:
            for i, slot in enumerate(slots):
                raw = captured[str(i)]
                value = raw if slot['dtype'] == 'string' else json.loads(raw)
                values.append(parameter(slot, value))
            if values not in matches:
                matches.append(values)
        except (ValueError, ServiceError):
            continue
    return matches


def prepare(principal, source_ids, question, infos, options=None):
    enabled = query_sources()
    selected = set(source_ids)
    if not selected.intersection(enabled):
        if options is not None:
            fail('ONTOLOGY_NOT_ENABLED', '这些数据源未开启本体查询', 422)
        return None
    if not selected <= enabled:
        fail('ONTOLOGY_SCOPE_MISMATCH', '不能混用本体与旧路由的数据源', 422)
    options = {} if options is None else options
    if not isinstance(options, dict) or set(options) - {'release_id', 'template_id', 'parameters'}:
        raise invalid('本体查询选项无效')
    for key in ('release_id', 'template_id'):
        if key in options and (not isinstance(options[key], str) or not options[key]):
            raise invalid()
    if 'parameters' in options and 'template_id' not in options:
        raise invalid('显式参数必须绑定明确的发布模板')
    registry = Registry()
    pin = registry.pin(namespace(), principal, options.get('release_id'))
    candidates = [e for e in pin['entities'].values() if e['kind'] == 'QueryTemplate'
                  and e['availability'] == 'executable' and set(e['source_ids']) == selected]
    matches = []
    if 'template_id' in options:
        template = next((e for e in candidates if e['id'] == options['template_id']), None)
        if template is None:
            fail('NOT_FOUND', '模板不存在或不可执行', 404)
        values = options.get('parameters', [])
        if not isinstance(values, list) or len(values) != len(template['attributes']['parameters']):
            raise invalid('参数数量不匹配')
        for slot, value in zip(template['attributes']['parameters'], values):
            parameter(slot, value)
        matches = [(template, values)]
    else:
        for template in candidates:
            matches.extend((template, values) for values in match_parameters(question.strip(), template))
    if not matches:
        fail('UNANSWERABLE', '未命中已发布可执行模板，请明确模板与参数', 422)
    if len(matches) != 1:
        fail('AMBIGUOUS_DEFINITION', '匹配多个已发布定义，请明确模板', 422)
    template, values = matches[0]
    closure, pending = {}, [template['id']]
    while pending:
        identity = pending.pop()
        if identity not in closure:
            closure[identity] = pin['entities'][identity]
            pending.extend(closure[identity]['dependencies'])
    document = {'schema_version': 2, 'namespace': pin['namespace'], 'entities': list(closure.values()), 'migration': {}}
    validate_executable(document, infos)
    a = template['attributes']
    prepared = bind_template(a['sql'], values)
    return {'registry': registry, 'namespace': pin['namespace'], 'release_id': pin['release_id'],
            'template': template, 'dependencies': {k: e['version'] for k, e in closure.items()},
            'generated': {'bedrockSQL': prepared.sql, 'prepared_query': prepared, 'merge_contract': a['merge'],
                          'column_type': a['column_types'], 'chart_type': 'BarChartPic'},
            'sql_sha256': hashlib.sha256(prepared.sql.encode('utf-8')).hexdigest()}


def guard(context):
    if context:
        context['registry'].ensure_usable(context['namespace'], context['release_id'])
        from .governance.runtime import guard as governance_guard
        governance_guard(context['registry'], context['namespace'], context['release_id'], context['dependencies'])


def evidence(context):
    return {'release_id': context['release_id'], 'template_id': context['template']['id'],
            'template_version': context['template']['version'], 'dependency_versions': context['dependencies'],
            'sql_sha256': context['sql_sha256'], 'events': ['retrieved', 'selected', 'executed']}
