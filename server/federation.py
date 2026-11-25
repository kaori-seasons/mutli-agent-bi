"""Merge only a reviewed template's explicit algebra; never infer it from LLM labels."""
from decimal import Decimal, InvalidOperation
from .errors import ServiceError
from .query_policy import parse_read
from sqlglot import exp


def _invalid(message="多库查询需要已审核的聚合契约"):
    return ServiceError("MERGE_CONTRACT_REQUIRED", message, 422)


def validate_contract(contract, source_ids):
    if not isinstance(contract, dict) or set(contract) != {
        "version", "sources", "disjoint_partitions", "dimensions", "measures"
    }:
        raise _invalid()
    if contract["version"] != 1 or contract["disjoint_partitions"] is not True:
        raise _invalid("必须确认各数据源分区互斥且口径、单位一致")
    sources, dimensions, measures = contract["sources"], contract["dimensions"], contract["measures"]
    if (not isinstance(sources, list) or any(not isinstance(s, str) for s in sources)
            or len(set(sources)) != len(sources) or set(sources) != set(source_ids)):
        raise _invalid("数据源集合与审核契约不一致")
    if (not isinstance(dimensions, list) or any(not isinstance(d, str) or not d for d in dimensions)
            or len(set(dimensions)) != len(dimensions) or not isinstance(measures, dict) or not measures):
        raise _invalid()
    inputs = set(dimensions)
    if set(dimensions) & set(measures):
        raise _invalid()
    for output, rule in measures.items():
        if not isinstance(output, str) or not output or not isinstance(rule, dict):
            raise _invalid()
        op = rule.get("op")
        keys = {"op", "input"} if op in {"sum", "min", "max"} else {"op", "numerator", "denominator"}
        if op not in {"sum", "min", "max", "weighted_avg", "ratio"} or set(rule) != keys:
            raise _invalid("该指标不支持跨库合并；去重计数与分位数需另行建模")
        for k in keys - {"op"}:
            if not isinstance(rule[k], str) or not rule[k] or rule[k] in dimensions:
                raise _invalid()
            inputs.add(rule[k])
    return inputs


def validate_merge_query(query, contract, database):
    """P0 supports partitioned, single-table partial aggregates, not arbitrary federation."""
    tree = parse_read(query.sql, database, allow_parameters=bool(query.parameters))
    if (not isinstance(tree, exp.Select) or len(list(tree.find_all(exp.Select))) != 1
            or len(list(tree.find_all(exp.Table))) != 1
            or any(tree.args.get(k) for k in ("joins", "having", "limit", "offset", "distinct", "with_"))
            or tree.find(exp.Window)):
        raise _invalid("P0 多库仅支持单表分区聚合，禁止局部排序截断、HAVING、JOIN、子查询和窗口")
    outputs = {x.alias_or_name: x.this if isinstance(x, exp.Alias) else x for x in tree.expressions}
    required = set(contract["dimensions"])
    rules = {}
    for rule in contract["measures"].values():
        if rule["op"] in {"ratio", "weighted_avg"}:
            rules.setdefault(rule["numerator"], set()).add("SUM")
            rules.setdefault(rule["denominator"], set()).add("COUNT" if rule["op"] == "weighted_avg" else "SUM")
        else:
            rules.setdefault(rule["input"], set()).add(rule["op"].upper())
    required.update(rules)
    if len(outputs) != len(tree.expressions) or set(outputs) != required:
        raise _invalid("SQL 输出必须与合并契约精确对应")
    for name, expected in rules.items():
        node = outputs[name]
        kind = node.sql_name() if isinstance(node, exp.AggFunc) else ""
        # COUNT rows is additive only across the explicitly disjoint partition contract.
        if (kind == "COUNT" and "SUM" in expected and len(expected) == 1):
            kind = "SUM"
        if expected != {kind} or node.find(exp.Distinct):
            raise _invalid("聚合表达式与合并代数不一致，不能累计 AVG 或 DISTINCT 计数")
    dimensions = [outputs[name] for name in contract["dimensions"]]
    if any(x.find(exp.AggFunc) for x in dimensions):
        raise _invalid("维度不能是聚合表达式")
    grouped = tree.args.get("group")
    actual = set()
    for node in grouped.expressions if grouped else []:
        if isinstance(node, exp.Column) and not node.table and node.name in outputs:
            node = outputs[node.name]
        actual.add(node.sql(dialect="mysql"))
    if actual != {x.sql(dialect="mysql") for x in dimensions}:
        raise _invalid("GROUP BY 必须与合并粒度一致")


def _number(value):
    if value is None or isinstance(value, bool):
        raise _invalid("度量为 NULL 或类型无效，不能伪装成零")
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise InvalidOperation()
        return number
    except (InvalidOperation, ValueError):
        raise _invalid("聚合度量必须是有限数值") from None


def merge_results(results, contract):
    ids = [r["source_id"] for r in results]
    inputs = validate_contract(contract, ids)
    if len(set(ids)) != len(ids):
        raise _invalid("数据源重复")
    if any(r.get("status") != "success" for r in results):
        raise ServiceError("PARTIAL_RESULT", "至少一个数据源查询失败，未返回不完整总计", 502)
    if any(r.get("truncated") for r in results):
        raise ServiceError("INCOMPLETE_RESULT", "源结果超出上限，不能合并截断数据", 422)
    dimensions, measures = contract["dimensions"], contract["measures"]
    groups = {}
    for result in results:
        columns = result["columns"]
        if len(set(columns)) != len(columns) or set(columns) != inputs:
            raise _invalid("源查询输出与合并契约不一致")
        seen = set()
        for row in result["rows"]:
            if len(row) != len(columns):
                raise _invalid()
            record = dict(zip(columns, row))
            key = tuple(record[d] for d in dimensions)
            if key in seen:
                raise _invalid("源结果并非每个维度一行，须先在源内按约定粒度聚合")
            seen.add(key)
            state = groups.setdefault(key, {})
            for output, rule in measures.items():
                op = rule["op"]
                if op in {"ratio", "weighted_avg"}:
                    numerator, denominator = _number(record[rule["numerator"]]), _number(record[rule["denominator"]])
                    if op == "weighted_avg" and (denominator < 0 or denominator != denominator.to_integral_value()):
                        raise _invalid("平均值的 count 必须是非负整数")
                    old = state.get(output, (Decimal(0), Decimal(0)))
                    state[output] = (old[0] + numerator, old[1] + denominator)
                else:
                    value = _number(record[rule["input"]])
                    previous = state.get(output)
                    state[output] = value if previous is None else (
                        previous + value if op == "sum" else min(previous, value) if op == "min" else max(previous, value))
    rows = []
    for key, state in sorted(groups.items(), key=lambda x: repr(x[0])):
        values = []
        for output, rule in measures.items():
            value = state[output]
            if rule["op"] in {"ratio", "weighted_avg"}:
                value = None if value[1] == 0 else value[0] / value[1]
            values.append(value)
        rows.append(tuple(key) + tuple(values))
    return {"status": "success", "rows": rows, "row_count": len(rows),
            "columns": dimensions + list(measures), "truncated": False,
            "sources": [{"source_id": r["source_id"], "status": r["status"]} for r in results]}
