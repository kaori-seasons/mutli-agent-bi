"""确定性只读召回；返回候选而非业务裁决，默认仅消费审核通过实体。"""
from __future__ import annotations

import copy
import math
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass

from .schema import QueryTemplate, Scenario
from .store import FileOntologyStore

STOP_WORDS = ("请问", "查询", "统计", "多少", "哪些", "一下", "所有", "不同", "请", "的", "在")


def normalize(value):
    return re.sub(r"\s+", "", value.casefold())


def tokens(value):
    value = value.casefold()
    for word in STOP_WORDS:
        value = value.replace(word, " ")
    result = set(re.findall(r"[a-z_][a-z_0-9]*", value))
    for part in re.findall(r"[\u4e00-\u9fff]+", value):
        for size in (2, 3):
            result.update(part[i:i + size] for i in range(len(part) - size + 1))
    return result


@dataclass
class Candidate:
    entity: object
    score: float
    reason: str = "lexical"


class OntologyService:
    def __init__(self, store=None, top_k=5, refresh_seconds=5.0, namespace=None):
        if type(top_k) is not int or not 1 <= top_k <= 100:
            raise ValueError("top_k 必须在 1..100 范围内")
        if not math.isfinite(refresh_seconds) or refresh_seconds < 0:
            raise ValueError("刷新时间必须是非负有限数值")
        self.store = store or FileOntologyStore()
        self.top_k = top_k
        self.refresh_seconds = refresh_seconds
        self.namespace = namespace
        self._lock = threading.RLock()
        self._token = object()
        self._next_refresh = 0.0
        self._snapshot = None
        self._scenarios = []
        self._templates = []
        self._scenario_frequencies = Counter()
        self._template_frequencies = Counter()
        self._relations = []
        self.error_type = ""
        self.revision = ""

    def _visible(self, entity):
        return entity.review_status == "approved" and (
            self.namespace is None or entity.source.namespace == self.namespace)

    def _refresh(self):
        if time.monotonic() < self._next_refresh:
            return
        self._next_refresh = time.monotonic() + self.refresh_seconds
        try:
            token = self.store.revision_token()
            if token == self._token:
                self.error_type = ""
                return
            snapshot = self.store.read()
            # 先构建新索引，全部成功后再替换已知良好缓存。
            scenarios = {e.id: e for e in snapshot.scenarios if self._visible(e)}
            tables = {e.id: e for e in snapshot.tables
                      if self._visible(e) and e.scenario_id in scenarios}
            descriptions = {identity: [s.name, s.description, *s.synonyms]
                            for identity, s in scenarios.items()}
            for table in tables.values():
                descriptions[table.scenario_id].extend([table.name, table.description, *table.synonyms])
            for column in snapshot.columns:
                if self._visible(column) and column.table_id in tables:
                    descriptions[tables[column.table_id].scenario_id].extend(
                        [column.name, column.description, *column.synonyms])
            indexed_scenarios = [(entity, tokens(" ".join(descriptions[identity])))
                                 for identity, entity in scenarios.items()]
            indexed_templates = [(e, tokens(" ".join([e.question, *e.synonyms,
                                  *e.outputs, *(s for slot in e.slots for s in slot.synonyms)])))
                                 for e in snapshot.templates if self._visible(e)]
            scenario_frequencies = Counter(term for _, terms in indexed_scenarios for term in terms)
            template_frequencies = Counter(term for _, terms in indexed_templates for term in terms)
            self._scenarios, self._templates = indexed_scenarios, indexed_templates
            self._scenario_frequencies, self._template_frequencies = scenario_frequencies, template_frequencies
            visible_columns = {c.id for c in snapshot.columns if self._visible(c) and c.table_id in tables}
            self._relations = [r for r in snapshot.relations if self._visible(r)
                               and r.scenario_id in scenarios and r.left_table in tables
                               and r.right_table in tables and r.left_key in visible_columns
                               and r.right_key in visible_columns]
            self._snapshot = snapshot
            self._token = token
            self.revision = snapshot.revision
            self.error_type = ""
        except Exception as exc:
            self.error_type = type(exc).__name__

    def _rank(self, question, index, frequencies):
        query = tokens(question)
        if not query:
            return []
        size = len(index)
        weights = {term: 1 + math.log((size + 1) / (frequencies[term] + 1)) for term in query}
        total = sum(weights.values())
        ranked = []
        for entity, terms in index:
            common = query & terms
            score = sum(weights[term] for term in common) / total
            if score >= 0.15 and common:
                ranked.append(Candidate(entity, round(score, 6)))
        return ranked

    def scenario_candidates(self, question):
        with self._lock:
            self._refresh()
            if not isinstance(question, str) or not question.strip():
                return []
            question = question[:4096]
            ranked = {c.entity.id: c for c in self._rank(question, self._scenarios, self._scenario_frequencies)}
            for scenario, _ in self._scenarios:
                if any(normalize(alias) == normalize(question) for alias in [scenario.name, *scenario.synonyms]):
                    ranked[scenario.id] = Candidate(scenario, 1.0, "exact_alias")
            return copy.deepcopy(sorted(ranked.values(), key=lambda c: (-c.score, c.entity.id))[:self.top_k])

    @staticmethod
    def _slot_match(question, template):
        pattern = template.question_pattern or template.question
        pieces = re.split(r"(\{slot\}|\{\d+\})", pattern)
        value = normalize(question)
        positions = {0}
        slot_index = 0
        types = {"integer": r"[-+]?\d+", "number": r"[-+]?\d+(?:\.\d+)?",
                 "boolean": r"(?:true|false|是|否)"}
        # 用有限状态集合替代多个通配符正则，避免相邻 slot 的指数回溯。
        for piece in pieces:
            next_positions = set()
            if re.fullmatch(r"\{slot\}|\{\d+\}", piece):
                index = slot_index if piece == "{slot}" else int(piece[1:-1])
                if index >= len(template.slots):
                    return False
                dtype = template.slots[index].dtype
                for position in positions:
                    limit = 192 if dtype == "list" else 96
                    for end in range(position + 1, min(len(value), position + limit) + 1):
                        if dtype not in types or re.fullmatch(types[dtype], value[position:end]):
                            next_positions.add(end)
                slot_index += 1
            else:
                literal = normalize(piece)
                next_positions = {p + len(literal) for p in positions if value.startswith(literal, p)}
            positions = next_positions
            if not positions:
                return False
        return len(value) in positions

    def template_candidates(self, question):
        with self._lock:
            self._refresh()
            if not isinstance(question, str) or not question.strip():
                return []
            question = question[:4096]
            candidates = {c.entity.id: c for c in self._rank(question, self._templates, self._template_frequencies) if c.score >= 0.25}
            for template, _ in self._templates:
                if self._slot_match(question, template):
                    candidates[template.id] = Candidate(template, 1.0, "slot_pattern")
                elif any(normalize(alias) == normalize(question) for alias in template.synonyms):
                    candidates[template.id] = Candidate(template, 1.0, "synonym")
            return copy.deepcopy(sorted(candidates.values(), key=lambda c: (-c.score, c.entity.id))[:self.top_k])

    def find_scenario_candidates(self, question: str) -> list[Scenario]:
        return [c.entity for c in self.scenario_candidates(question)]

    def find_template_candidates(self, question: str) -> list[QueryTemplate]:
        return [c.entity for c in self.template_candidates(question)]

    def get_relations(self, scenario: str):
        with self._lock:
            self._refresh()
            identities = {e.id for e, _ in self._scenarios if e.id == scenario or e.name == scenario}
            return copy.deepcopy(sorted([r for r in self._relations if r.scenario_id in identities], key=lambda r: r.id))


_default = None
_default_lock = threading.Lock()


def default_service():
    global _default
    with _default_lock:
        if _default is None:
            _default = OntologyService()
        return _default


def find_scenario_candidates(question: str) -> list[Scenario]:
    return default_service().find_scenario_candidates(question)


def find_template_candidates(question: str) -> list[QueryTemplate]:
    return default_service().find_template_candidates(question)


def get_relations(scenario: str):
    return default_service().get_relations(scenario)
