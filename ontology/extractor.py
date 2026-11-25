"""从既有语义资产确定性抽取；不调用 LLM，不访问数据库或 S3。"""
from __future__ import annotations

import json
import logging
import math
import re
import string
import zipfile
from pathlib import Path

from .schema import (COLLECTIONS, Column, Metric, QueryTemplate, Relation, Scenario,
                     Slot, Snapshot, Source, Table, ValidationError, stable_id)
from .store import FileOntologyStore

logger = logging.getLogger(__name__)
MAX_ROWS = 20000


def text(value):
    if value is None or isinstance(value, float) and not math.isfinite(value):
        return ""
    return str(value).strip()


def synonyms(value):
    if isinstance(value, list):
        return [text(item) for item in value if text(item)]
    return [item.strip() for item in re.split(r"[,，;；|]", text(value)) if item.strip()]


def _enumerable(value):
    value = text(value).lower()
    if not value:
        return None
    if any(term in value for term in ("不可枚举", "不枚举", "非枚举", "false", "不能枚举")):
        return False
    if "可枚举" in value or value in {"true", "enumerable"}:
        return True
    return None


def _assemble(name, description, query_rule, join_rule, table_specs,
              namespace, asset, review_status, scenario_synonyms=None, metrics=None,
              structured_join_rule=None):
    source = Source(namespace, asset)
    scenario = Scenario(id=stable_id("scenario", namespace, name), source=source,
                        review_status=review_status, name=name, description=description,
                        query_rule=query_rule, join_rule=join_rule,
                        synonyms=scenario_synonyms or [])
    result = Snapshot(scenarios=[scenario])
    for spec in table_specs:
        table = Table(id=stable_id("table", namespace, name, spec["name"]),
                      source=Source(namespace, asset, spec["name"]),
                      review_status=review_status, scenario_id=scenario.id,
                      name=spec["name"], description=spec.get("description", ""),
                      query_rule=spec.get("query_rule", ""), synonyms=spec.get("synonyms", []))
        result.tables.append(table)
        for item in spec["columns"]:
            tips = text(item.get("tips", "")).lower()
            measure = "度量" in tips or tips == "measure"
            dimension = not measure and ("维度" in tips or tips == "dimension")
            column = Column(id=stable_id("column", namespace, name, table.name, item["name"]),
                            source=Source(namespace, asset, table.name + "." + item["name"]),
                            review_status=review_status, table_id=table.id,
                            name=item["name"], dtype=text(item.get("dtype")),
                            description=text(item.get("description")),
                            is_dimension=dimension, is_measure=measure,
                            enumerable=_enumerable(item.get("option", tips)),
                            option_desc=text(item.get("option_desc")),
                            synonyms=synonyms(item.get("synonyms")),
                            primary_key=item.get("primary_key", False))
            result.columns.append(column)
            if measure:
                # 度量标记不代表 SUM 等口径已经通过业务审核。
                result.metrics.append(Metric(
                    id=stable_id("metric", namespace, name, table.name, column.name),
                    source=column.source, name=column.description or column.name,
                    scenario_id=scenario.id, expression=table.name + "." + column.name,
                    column_ids=[column.id], synonyms=column.synonyms))
    _relations(result, structured_join_rule if structured_join_rule is not None else join_rule, review_status)
    if metrics:
        for definition in metrics:
            metric = Metric.from_dict(definition)
            result.metrics = [m for m in result.metrics if m.id != metric.id] + [metric]
    result.validate()
    return result


def _relations(snapshot, rule, status):
    if not rule.strip():
        return
    scenario = snapshot.scenarios[0]
    prefix = f"[{scenario.source.namespace}:{scenario.source.asset}]"
    try:
        if rule.lstrip().startswith("["):
            definitions = json.loads(rule)
            if not isinstance(definitions, list):
                raise ValidationError("关系必须是数组")
        else:
            definitions = []
            for clause in re.split(r"[;；\n]+", rule):
                if not clause.strip():
                    continue
                match = re.fullmatch(r"\s*`?([\w]+)`?\.`?([\w]+)`?\s*=\s*"
                                     r"`?([\w]+)`?\.`?([\w]+)`?\s*", clause)
                if not match:
                    raise ValidationError("自由文本不能可靠转换")
                lt, lk, rt, rk = match.groups()
                definitions.append(dict(left_table=lt, left_key=lk, right_table=rt,
                                        right_key=rk, join_type="unknown"))
        tables = {t.name: t.id for t in snapshot.tables}
        columns = {(c.table_id, c.name): c.id for c in snapshot.columns}
        relations = []
        for item in definitions:
            if set(item) - {"left_table", "left_key", "right_table", "right_key", "join_type"}:
                raise ValidationError("关系包含未知字段")
            lt, rt = tables[item["left_table"]], tables[item["right_table"]]
            lk, rk = columns[(lt, item["left_key"])], columns[(rt, item["right_key"])]
            join_type = item.get("join_type", "unknown").lower()
            relation = Relation(id=stable_id("relation", scenario.source.namespace,
                                           scenario.name, lt, lk, rt, rk, join_type),
                                source=scenario.source, scenario_id=scenario.id,
                                review_status=status if join_type != "unknown" else "pending",
                                left_table=lt, left_key=lk, right_table=rt, right_key=rk,
                                join_type=join_type)
            relation.validate()
            relations.append(relation)
        snapshot.relations = relations
        if any(r.join_type == "unknown" for r in relations):
            snapshot.warnings.append(prefix + " 关联类型未知，关系保持待审核")
    except (ValueError, KeyError, TypeError, AttributeError):
        snapshot.warnings.append(prefix + " 关联原文保留，未生成不可验证的结构化关系")


def extract_analysis(scenario_name, analyses, namespace="default", asset=None):
    """analyses 为 [(PROMPT_F 的 JSON 结果, get_table_schema 的结果), ...]。"""
    specs = []
    for analysis, schema in analyses:
        schema_by_name = {item["Name"]: item for item in schema}
        columns = []
        for item in analysis["columns"]:
            physical = schema_by_name[item["name"]]
            columns.append(dict(name=item["name"], dtype=physical["Type"],
                                tips=item.get("tips", ""), option=item.get("option", ""),
                                description=item.get("desc", ""),
                                option_desc=item.get("option_desc", ""),
                                synonyms=item.get("synonyms", []),
                                primary_key=physical.get("Key") in {"PRI", "PRIMARY KEY"}))
        specs.append(dict(name=analysis["table"], description=analysis.get("desc", ""),
                          columns=columns))
    result = _assemble(scenario_name, ",".join(s["description"] for s in specs), "", "",
                       specs, namespace, asset or scenario_name + ".xlsx", "pending")
    for entity in result.entities():
        entity.source.producer = "PROMPT_F"
    return result


def extract_workbook(path, namespace="default", review_status="pending"):
    from openpyxl import load_workbook
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        if sum(item.file_size for item in archive.infolist()) > 64 * 1024 * 1024:
            raise ValidationError("工作簿展开容量超过上限")
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = workbook.worksheets
        if not sheets or len(sheets) > 256:
            raise ValidationError("工作簿 sheet 数量不合法")
        summary = list(sheets[0].iter_rows(max_row=MAX_ROWS + 1, max_col=8, values_only=True))
        if len(summary) < 4 or len(summary) > MAX_ROWS:
            raise ValidationError("场景 summary 布局不合法")
        extras = {text(row[0]): row[1] for row in summary[4:] if text(row[0])}
        specs = []
        for sheet in sheets[1:]:
            rows = list(sheet.iter_rows(max_row=MAX_ROWS + 1, max_col=8, values_only=True))
            if len(rows) < 3 or len(rows) > MAX_ROWS:
                raise ValidationError("表 sheet 布局不合法")
            columns = []
            for row in rows[3:]:
                name = text(row[0])
                if not name:
                    break
                if name in {"字段名称", "字段名", "column_name", "name"} and text(row[1]) in {
                        "字段类型", "dtype", "type"}:
                    continue
                if text(row[3]).lower() in {"false", "0", "否"}:
                    continue
                columns.append(dict(name=name, dtype=text(row[1]), tips=text(row[2]),
                                    description=text(row[4]), option_desc=text(row[5]) or
                                    (text(row[4]) if _enumerable(row[2]) else ""),
                                    primary_key=text(row[3]).upper() == "PRIMARY KEY",
                                    synonyms=synonyms(row[6])))
            specs.append(dict(name=text(rows[0][1]), description=text(rows[1][1]),
                              query_rule=text(rows[2][1]), columns=columns))
        rule = text(summary[3][1])
        structured = text(extras["结构化关联"]) if "结构化关联" in extras else None
        return _assemble(text(summary[0][1]), text(summary[1][1]), text(summary[2][1]),
                         rule, specs, namespace, path.name, review_status,
                         synonyms(extras.get("同义词")), structured_join_rule=structured)
    finally:
        workbook.close()


def extract_templates(templates, namespace="default", asset="sql_templates",
                      review_status="approved"):
    result = Snapshot()
    for question, definition in sorted(templates.items()):
        values = definition.get("params", [])
        if not isinstance(values, list):
            raise ValidationError("模板 params 必须是数组")
        params = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list))
                  else text(v) for v in values]
        slots = []
        for index, value in enumerate(values):
            dtype = "boolean" if type(value) is bool else "integer" if type(value) is int else \
                    "number" if type(value) is float else "list" if type(value) is list else "string"
            slots.append(Slot(name=f"param_{index}", position=index, dtype=dtype))
        if "slots" in definition:
            slots = [Slot.from_dict(slot) for slot in definition["slots"]]
        pattern = question
        for index in sorted(range(len(params)), key=lambda i: len(params[i]), reverse=True):
            if params[index]:
                pattern = pattern.replace(params[index], "{slot}")
        content = definition["content"]
        positions = []
        for _, key, spec, conversion in string.Formatter().parse(content):
            if key is not None:
                if not key.isdigit() or spec or conversion:
                    raise ValidationError("仅支持 SQL 的显式位置占位符 {0}、{1}")
                positions.append(int(key))
        if set(positions) != set(range(len(params))):
            raise ValidationError("SQL 占位符与 params 不匹配")
        template = QueryTemplate(id=stable_id("template", namespace, question),
                                 source=Source(namespace, asset, producer="sql_template"),
                                 review_status=review_status, question=question, params=params,
                                 content=content, slots=slots,
                                 outputs=definition.get("outputs", []),
                                 synonyms=synonyms(definition.get("synonyms")),
                                 question_pattern=definition.get("question_pattern", pattern))
        result.templates.append(template)
    result.validate()
    return result


def extract_template_directory(path, namespace="default", review_status="approved"):
    path = Path(path).resolve()
    with (path / "summary.json").open(encoding="utf-8") as stream:
        summary = json.load(stream)
    templates = {}
    for definition in summary:
        sql_path = (path / definition["sql"]).resolve()
        if path not in sql_path.parents:
            raise ValidationError("SQL 文件不能位于模板目录之外")
        question = definition["question"]
        if question in templates:
            raise ValidationError("模板问题重复")
        templates[question] = dict(definition, content=sql_path.read_text(encoding="utf-8"))
    return extract_templates(templates, namespace, "sql_templates", review_status)


def combine(snapshots):
    result = Snapshot()
    for snapshot in snapshots:
        for name in COLLECTIONS:
            getattr(result, name).extend(getattr(snapshot, name))
        result.warnings.extend(snapshot.warnings)
    result.validate()
    return result


def publish_workbooks(folder, namespace="default", review_status="approved", store=None,
                      template_path=None):
    paths = sorted(Path(folder).glob("*.xlsx"))
    if not paths:
        raise ValidationError("没有可抽取的 Excel 工作簿")
    snapshots = [extract_workbook(path, namespace, review_status) for path in paths]
    sources = {(namespace, path.name) for path in paths}
    if template_path:
        snapshots.append(extract_template_directory(template_path, namespace, review_status))
        sources.add((namespace, "sql_templates"))
    store = store or FileOntologyStore()
    stage = "approved" if review_status == "approved" else "pending"
    # 目录发布是该命名空间的 Excel 权威清单，删除的工作簿也要清理。
    current = store.read(stage)
    sources.update((e.source.namespace, e.source.asset) for e in current.entities()
                   if e.source.namespace == namespace and e.source.asset.endswith(".xlsx"))
    return store.replace_sources(combine(snapshots), sources, stage)


def safe_write_analysis(scenario, analyses, namespace="default", store=None):
    try:
        snapshot = extract_analysis(scenario, analyses, namespace)
        output = (store or FileOntologyStore()).replace_sources(
            snapshot, {(namespace, scenario + ".xlsx")}, "pending")
        logger.info("ontology_dual_write stage=pending revision=%s", output.revision)
        return output
    except Exception as exc:
        logger.error("ontology_dual_write_failed stage=pending error_type=%s; 可离线重试", type(exc).__name__)
        return None


def safe_publish_workbooks(folder, namespace="default", template_path=None):
    try:
        output = publish_workbooks(folder, namespace, template_path=template_path)
        logger.info("ontology_dual_write stage=approved revision=%s", output.revision)
        return output
    except Exception as exc:
        logger.error("ontology_dual_write_failed stage=approved error_type=%s; 可离线重试", type(exc).__name__)
        return None
