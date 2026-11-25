"""第一阶段元模型：严格 JSON 契约、稳定身份与引用完整性。"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, fields
from functools import lru_cache
from typing import Optional, Union, get_args, get_origin, get_type_hints


class ValidationError(ValueError):
    """本体输入不符合契约。"""


def stable_id(kind: str, namespace: str, *keys: str) -> str:
    payload = json.dumps([namespace, *keys], ensure_ascii=False, separators=(",", ":"))
    return kind + ":" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _decode(annotation, value, path):
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Union:
        for option in args:
            try:
                return _decode(option, value, path)
            except ValidationError:
                pass
        raise ValidationError(f"{path}: 类型不匹配")
    if origin is list:
        if type(value) is not list:
            raise ValidationError(f"{path}: 必须是数组")
        return [_decode(args[0], item, path) for item in value]
    if annotation is type(None) and value is None:
        return None
    if isinstance(annotation, type) and issubclass(annotation, Model):
        return annotation.from_dict(value)
    if annotation in (str, bool, int, float):
        if type(value) is not annotation:
            raise ValidationError(f"{path}: 必须是 {annotation.__name__}")
        if annotation is float and not math.isfinite(value):
            raise ValidationError(f"{path}: 必须是有限数值")
        return value
    raise ValidationError(f"{path}: 无效类型")


@lru_cache(maxsize=64)
def _model_hints(model_class):
    return get_type_hints(model_class)


class Model:
    @classmethod
    def from_dict(cls, data):
        if type(data) is not dict:
            raise ValidationError(f"{cls.__name__}: 必须是对象")
        allowed = {f.name for f in fields(cls)}
        if set(data) - allowed:
            raise ValidationError(f"{cls.__name__}: 未知字段 {sorted(set(data) - allowed)}")
        hints = _model_hints(cls)
        values = {key: _decode(hints[key], value, key) for key, value in data.items()}
        try:
            model = cls(**values)
        except TypeError as exc:
            raise ValidationError(f"{cls.__name__}: 缺少必填字段") from exc
        model.validate()
        return model

    def to_dict(self):
        self.validate()
        return asdict(self)

    def validate(self):
        hints = _model_hints(type(self))
        for item in fields(self):
            value = getattr(self, item.name)
            _decode(hints[item.name], asdict(value) if isinstance(value, Model) else
                    [asdict(x) if isinstance(x, Model) else x for x in value]
                    if isinstance(value, list) else value, item.name)


@dataclass
class Source(Model):
    namespace: str
    asset: str
    location: str = ""
    producer: str = "excel"

    def validate(self):
        super().validate()
        if not self.namespace.strip() or not self.asset.strip():
            raise ValidationError("source: namespace 和 asset 不能为空")


@dataclass
class Entity(Model):
    id: str
    source: Source
    version: int = 1
    review_status: str = "pending"

    def validate(self):
        super().validate()
        if not self.id.strip() or self.version < 1:
            raise ValidationError("实体 id 不能为空且 version 必须为正整数")
        if hasattr(self, "name") and not self.name.strip():
            raise ValidationError("实体名称不能为空")
        if self.review_status not in {"pending", "approved", "rejected"}:
            raise ValidationError("无效审核状态")


@dataclass
class Scenario(Entity):
    name: str = ""
    description: str = ""
    query_rule: str = ""
    join_rule: str = ""
    synonyms: list[str] = field(default_factory=list)


@dataclass
class Table(Entity):
    scenario_id: str = ""
    name: str = ""
    description: str = ""
    query_rule: str = ""
    synonyms: list[str] = field(default_factory=list)


@dataclass
class Column(Entity):
    table_id: str = ""
    name: str = ""
    dtype: str = ""
    description: str = ""
    is_dimension: bool = False
    is_measure: bool = False
    enumerable: Optional[bool] = None
    option_desc: str = ""
    synonyms: list[str] = field(default_factory=list)
    primary_key: bool = False

    def validate(self):
        super().validate()
        if self.is_dimension and self.is_measure:
            raise ValidationError("列不能同时是维度与度量")


@dataclass
class Relation(Entity):
    scenario_id: str = ""
    left_table: str = ""
    left_key: str = ""
    right_table: str = ""
    right_key: str = ""
    join_type: str = "unknown"

    def validate(self):
        super().validate()
        if self.join_type not in {"inner", "left", "right", "full", "unknown"}:
            raise ValidationError("无效 join_type")


@dataclass
class Metric(Entity):
    scenario_id: str = ""
    name: str = ""
    expression: str = ""
    column_ids: list[str] = field(default_factory=list)
    aggregation: str = ""
    filters: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)


@dataclass
class Slot(Model):
    name: str
    position: int
    dtype: str = "string"
    required: bool = True
    synonyms: list[str] = field(default_factory=list)

    def validate(self):
        super().validate()
        if not self.name.strip() or self.position < 0:
            raise ValidationError("slot 名称不能为空且位置不能为负")
        if self.dtype not in {"string", "integer", "number", "boolean", "list"}:
            raise ValidationError("无效 slot 类型")


@dataclass
class QueryTemplate(Entity):
    question: str = ""
    params: list[str] = field(default_factory=list)
    content: str = ""
    slots: list[Slot] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)
    question_pattern: str = ""

    def validate(self):
        super().validate()
        if not self.question.strip() or not self.content.strip():
            raise ValidationError("模板问题和 SQL 内容不能为空")
        if len(self.slots) > 32 or len(self.question_pattern) > 4096 or len(self.question) > 4096:
            raise ValidationError("模板 slot/问题骨架超出上限")
        positions = [slot.position for slot in self.slots]
        names = [slot.name for slot in self.slots]
        if sorted(positions) != list(range(len(self.params))) or len(set(names)) != len(names):
            raise ValidationError("slot 必须与 params 位置一一对应")


COLLECTIONS = ("scenarios", "tables", "columns", "relations", "metrics", "templates")


@dataclass
class VersionCounter(Model):
    id: str
    version: int

    def validate(self):
        super().validate()
        if not self.id or self.version < 1:
            raise ValidationError("无效实体版本计数")


@dataclass
class Snapshot(Model):
    schema_version: int = 1
    revision: str = ""
    scenarios: list[Scenario] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    columns: list[Column] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    templates: list[QueryTemplate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    version_counters: list[VersionCounter] = field(default_factory=list)

    def entities(self):
        return [entity for name in COLLECTIONS for entity in getattr(self, name)]

    def validate(self):
        super().validate()
        if self.schema_version != 1:
            raise ValidationError("不支持的本体格式版本")
        entities = self.entities()
        by_id = {entity.id: entity for entity in entities}
        if len(by_id) != len(entities):
            raise ValidationError("实体 id 重复")
        counters = {c.id: c.version for c in self.version_counters}
        if len(counters) != len(self.version_counters):
            raise ValidationError("版本计数重复")
        for entity in entities:
            if counters.get(entity.id, entity.version) < entity.version:
                raise ValidationError("版本计数不能低于当前实体版本")
            entity.validate()
            if isinstance(entity, (Scenario, Table, Column, Metric)) and not entity.name.strip():
                raise ValidationError("实体名称不能为空")

        def ref(owner, identity, expected):
            target = by_id.get(identity)
            if not isinstance(target, expected):
                raise ValidationError(f"{owner.id}: 引用不存在或类型不匹配")
            if target.source.namespace != owner.source.namespace:
                raise ValidationError("实体引用不能跨命名空间")
            return target

        for table in self.tables:
            ref(table, table.scenario_id, Scenario)
        for column in self.columns:
            ref(column, column.table_id, Table)
        for relation in self.relations:
            ref(relation, relation.scenario_id, Scenario)
            for table_id, key_id in ((relation.left_table, relation.left_key),
                                     (relation.right_table, relation.right_key)):
                table = ref(relation, table_id, Table)
                key = ref(relation, key_id, Column)
                if key.table_id != table.id or table.scenario_id != relation.scenario_id:
                    raise ValidationError("关系端点不属于指定表/场景")
        for metric in self.metrics:
            ref(metric, metric.scenario_id, Scenario)
            for identity in metric.column_ids:
                column = ref(metric, identity, Column)
                if by_id[column.table_id].scenario_id != metric.scenario_id:
                    raise ValidationError("指标列不属于指定场景")

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_json(cls, text):
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValidationError("JSON 对象包含重复字段")
                result[key] = value
            return result
        try:
            return cls.from_dict(json.loads(text, object_pairs_hook=unique_pairs))
        except (ValueError, TypeError) as exc:
            raise ValidationError("无效本体 JSON: " + str(exc)) from exc

    def seal(self):
        self.revision = ""
        self.revision = hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()
        return self

    def verify_revision(self):
        original = self.revision
        copy = Snapshot.from_dict(self.to_dict()).seal()
        if original != copy.revision:
            raise ValidationError("快照修订校验失败")
