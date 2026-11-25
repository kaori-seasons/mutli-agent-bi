"""Compatible ChatBI response surface over authenticated, bounded query execution."""
import copy
import json
import logging
import os
import time

from . import llm, conf, aws, prompt, sql
from .api_helpler import Helper
from .errors import ServiceError
from .execution import QueryBudget, bounded_int
from .federation import validate_contract, validate_merge_query
from .query_policy import PreparedQuery, bind_template

logger = logging.getLogger(__name__)
meta = {}


def init(load_templates=True):
    if load_templates:
        _load_template_questions()
    try:
        from ontology.shadow import configure
        configure()
    except Exception:
        pass


def _ontology_compare(*args, **kwargs):
    try:
        from ontology.shadow import record_comparison
        return record_comparison(*args, **kwargs)
    except Exception:
        return False


def _object(raw):
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        return {}


def _columns(parsed, names_key, types_key):
    names, types = parsed.get(names_key), parsed.get(types_key)
    if (not isinstance(names, list) or not names or len(names) > 256
            or any(not isinstance(n, str) or not n for n in names)
            or not isinstance(types, list) or len(names) != len(types)
            or any(not isinstance(t, str) or t not in {"维度", "度量"} for t in types)):
        raise ServiceError("INVALID_MODEL_OUTPUT", "模型返回的列说明无效", 502)
    return names, types


def _scoped_prompt(config, source_id):
    if len(conf.mysql_info) == 1:
        return config
    try:
        mapping = json.loads(os.getenv("SOURCE_SCENARIOS", "{}"))
        names = mapping[source_id]
        if not isinstance(names, list) or not names or any(n not in config for n in names):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise ServiceError("SCENARIO_CONFIGURATION", "请配置该数据源的场景范围", 503) from None
    scoped = {k: copy.deepcopy(config[k]) for k in
              ("Examples", "ChartPrompt", "HardPrompt", "DefaultPrompt") if k in config}
    scoped.update({name: copy.deepcopy(config[name]) for name in names})
    scoped["Overall"] = copy.deepcopy(config.get("Overall", {}))
    scoped["Overall"]["AllScenariosPrompt"] = json.dumps(names, ensure_ascii=False)
    if scoped["Overall"].get("DefaulteScenario") not in names:
        scoped["Overall"].pop("DefaulteScenario", None)
    return scoped


def get_result(msg, trace_id, user_id="", mode_type="normal", *, principal=None, source_ids=None, ontology_options=None):
    if principal is None:
        raise ServiceError("AUTH_REQUIRED", "查询需要可信身份", 401)
    infos = conf.get_mysql_conf_by_question(msg[-1]["content"], principal.sources, source_ids)
    ids = [info["source_id"] for info in infos]
    budget = QueryBudget()
    templates = conf.get_sql_templates()
    scope = {name for name, entry in templates.items()
             if (entry.get("source_ids") is not None and set(ids) <= set(entry["source_ids"]))
             or (entry.get("source_ids") is None and len(conf.mysql_info) == 1)}
    from ontology.runtime import prepare, guard, evidence
    ontology_context = prepare(principal, ids, msg[-1]["content"], conf.mysql_info, ontology_options)
    bedrock = None
    if ontology_context:
        generated = ontology_context["generated"]
    else:
        _load_template_questions()
        bedrock = aws.get("bedrock-runtime")
        generated = answer_template_sql(bedrock, msg, trace_id, template_scope=scope)
    if "error" in generated:
        if len(infos) > 1:
            raise ServiceError("MERGE_CONTRACT_REQUIRED", "多库查询须匹配具有审核聚合契约的模板", 422)
        config = _scoped_prompt(prompt.get("PROMPT_FILE_NAME"), ids[0])
        generated = answer(bedrock, msg, config, trace_id, True)
    if "error" in generated:
        raise ServiceError("UNANSWERABLE", "无法根据当前授权场景生成查询，请补充问题范围", 422)

    prepared = generated.get("prepared_query") or PreparedQuery(sql.format_md(generated["bedrockSQL"]))
    merge = generated.get("merge_contract")
    guard(ontology_context)
    if len(infos) > 1:
        validate_contract(merge, ids)
        for info in infos:
            validate_merge_query(prepared, merge, info["db"])
        results = Helper.query_many_db(infos, prepared, budget=budget)
        result = Helper.merge_data(results, contract=merge)
        columns = result["columns"]
        types = ["维度"] * len(merge["dimensions"]) + ["度量"] * len(merge["measures"])
    else:
        result = Helper.query_db(infos[0], prepared, principal.subject, trace_id, budget=budget)
        # Template values must not be reinterpreted by a repair model; multi-source plans
        # cannot be repaired by silently rerunning only the first source.
        if result.get("status") == "failed" and result.get("retryable") and not generated.get("prepared_query"):
            result = retry_when_sql_error(principal.subject, trace_id, msg, prepared.sql,
                                         result, infos, bedrock, budget=budget)
            if result.get("finalSQL"):
                prepared = PreparedQuery(result["finalSQL"])
        if result.get("status") != "success":
            raise ServiceError("SOURCE_QUERY_FAILED", "数据库查询未完成，请稍后重试", 502)
        columns = result["columns"]  # Actual cursor metadata is authoritative.
        types = generated["column_type"] if len(generated["column_type"]) == len(columns) else ["维度"] * len(columns)

    if ontology_context:
        guard(ontology_context)
        expected_columns = (list(merge["dimensions"]) + list(merge["measures"]) if len(infos) > 1
                            else ontology_context["template"]["attributes"]["outputs"])
        if columns != expected_columns:
            raise ServiceError("STALE_BINDING", "实际结果列与发布模板不一致", 422)
    limit = bounded_int("MAX_ROW_COUNT_RETURN", 50, 1, 10000)
    labels = generated.get("cn_column")
    if (len(infos) > 1 or not isinstance(labels, list) or len(labels) != len(columns)
            or any(not isinstance(label, str) for label in labels)):
        labels = columns
    content = generated.get("clarify", "")
    content = content if isinstance(content, str) else ""
    chart_type = generated.get("chart_type", "BarChartPic")
    charts = {"barchart": "BarChartPic", "linechart": "LineChartPic", "piechart": "PieChartPic",
              "barchartpic": "BarChartPic", "linechartpic": "LineChartPic", "piechartpic": "PieChartPic"}
    chart_type = charts.get(chart_type.lower(), "BarChartPic") if isinstance(chart_type, str) else "BarChartPic"
    response = {"status": "success", "content": content,
                "mdData": Helper.mk_md_table(labels, result, limit),
                "chartData": Helper.mk_chart_data(labels, types, result, limit),
                "sql": prepared.sql, "chartType": chart_type,
                "extra": "", "truncated": result.get("truncated", False),
                "sources": result.get("sources", [{"source_id": ids[0], "status": "success"}])}
    if result.get("truncated"):
        response["content"] += "\n结果达到执行上限，仅展示截断结果；请缩小范围后获取完整数据。"
    elif result["row_count"] > limit:
        from .exports import save_export
        try:
            release = ({"namespace": ontology_context["namespace"], "release_id": ontology_context["release_id"]}
                       if ontology_context else None)
            response["extra"] = save_export(principal, ids, columns, result["rows"], release=release)
            response["content"] += f"\n当前展示 {limit} 行，可下载本次完整结果（10 分钟内有效）。"
        except Exception as error:
            logger.warning("export_failed trace=%s type=%s", trace_id, type(error).__name__)
            response["content"] += "\n查询成功，导出暂时不可用。"
    logger.info("query_complete trace=%s sources=%d rows=%d sql_calls=%d",
                trace_id, len(ids), result["row_count"], budget.calls)
    if ontology_context:
        guard(ontology_context)
        response["ontology"] = evidence(ontology_context)
    return response


def answer(bedrock, msg, promptConfig, trace_id, is_hard_mode):
    raw = msg[-1]["content"]
    questions = [{"role": "user", "content": Helper.build_select_scenario_msg(raw, promptConfig)}]
    start = time.monotonic()
    scenario = llm.query(questions, bedrock_client=bedrock)
    raw_scenario = scenario
    elapsed = (time.monotonic() - start) * 1000
    if scenario not in promptConfig or scenario == "Overall":
        scenario = promptConfig.get("Overall", {}).get("DefaulteScenario")
        if not scenario or scenario not in promptConfig:
            _ontology_compare("scenario", raw, trace_id, selected=raw_scenario, status="no_match", llm_elapsed_ms=elapsed)
            return Helper.bad_response("没有找到合适的场景")
    _ontology_compare("scenario", raw, trace_id, selected=raw_scenario, final_selection=scenario,
                      status="matched" if scenario == raw_scenario else "default_fallback", llm_elapsed_ms=elapsed)
    question = Helper.build_question_msg(raw, scenario, promptConfig, is_hard_mode, Helper.get_rag_str(raw))
    parsed = _object(llm.format_bedrock_result(llm.query(Helper.mk_request_with_history(question, msg), bedrock_client=bedrock)))
    if not isinstance(parsed.get("finalSQL"), str) or not parsed["finalSQL"].strip():
        return Helper.bad_response("模型未生成有效 SQL")
    try:
        columns, types = _columns(parsed, "columnList", "columnType")
    except ServiceError:
        return Helper.bad_response("模型列说明无效")
    columns = [item.split(" AS ")[-1] for item in columns]
    return {"bedrockSQL": parsed["finalSQL"], "queryTableName": scenario,
            "bedrockColumn": columns, "cn_column": parsed.get("columnCNList"),
            "column_type": types, "chart_type": parsed.get("chartType", "BarChart"),
            **({"clarify": parsed.get("clarify", "")} if is_hard_mode else {})}


def answer_template_sql(bedrock, msg, trace_id, template_scope=None):
    raw = msg[-1]["content"]
    questions = [{"role": "user", "content": prompt.build_template_question_meta_prompt(raw)}]
    start = time.monotonic()
    parsed = _object(llm.query(questions, bedrock_client=bedrock))
    if not parsed:
        _ontology_compare("template", raw, trace_id, status="invalid_legacy_json",
                          llm_elapsed_ms=(time.monotonic() - start) * 1000)
        return Helper.bad_response("模板匹配响应无效")
    selected = _find_template(raw, parsed, template_scope)
    _ontology_compare("template", raw, trace_id, selected=selected, final_selection=selected,
                      status="matched" if selected else "no_match",
                      llm_elapsed_ms=(time.monotonic() - start) * 1000)
    if not selected:
        return Helper.bad_response("没有找到匹配模板")
    entry = conf.get_sql_templates().get(selected, {})
    conditions = parsed["conditions"]
    keys = entry.get("condition_keys")
    if keys is None:
        if len(conditions) > 1:
            raise ServiceError("TEMPLATE_CONFIGURATION", "多参数模板须显式配置 condition_keys 顺序", 503)
        keys = list(conditions)
    if not isinstance(keys, list) or len(keys) != len(set(keys)) or set(keys) != set(conditions):
        raise ServiceError("INVALID_MODEL_OUTPUT", "模板参数与审核定义不一致", 502)
    prepared = bind_template(prompt.template_sql(selected), [conditions[key] for key in keys])
    descriptions = _object(llm.query([{"role": "user",
                                      "content": prompt.template_sql_columns(prepared.sql, raw)}], bedrock_client=bedrock))
    columns, types = _columns(descriptions, "columns", "columns_type")
    return {"bedrockSQL": prepared.sql, "prepared_query": prepared,
            "merge_contract": entry.get("merge"), "queryTableName": "template",
            "bedrockColumn": columns, "column_type": types, "chart_type": "BarChart"}


def retry_when_sql_error(user_id, trace_id, msg, fmtsql, raw_db_results, db_infos, bedrock_client, budget=None):
    if len(db_infos) != 1:
        raise ServiceError("PARTIAL_RESULT", "跨库失败不能降级为单库重试", 502)
    fix = prompt.template_fix_query_error(fmtsql, "查询语法或目录绑定失败，请核对授权场景。")
    parsed = _object(llm.format_bedrock_result(llm.query(Helper.mk_request_with_history(fix, msg), bedrock_client=bedrock_client)))
    if not isinstance(parsed.get("finalSQL"), str) or not parsed["finalSQL"].strip():
        raise ServiceError("INVALID_MODEL_OUTPUT", "SQL 修复未返回有效查询", 502)
    final_sql = sql.format_md(parsed["finalSQL"])
    result = Helper.query_db(db_infos[0], PreparedQuery(final_sql), user_id, trace_id, budget=budget)
    return dict(result, finalSQL=final_sql)


def _load_template_questions():
    if meta or not conf.get_sql_templates():
        return
    try:
        raw = llm.query([{"role": "user", "content": prompt.build_template_options_question()}],
                        aws.get("bedrock-runtime"))
        parsed = _object(raw)
        for key, value in parsed.items():
            if key in conf.get_sql_templates() and _valid_selection(value):
                meta[key] = value
                meta[key]["querys"].sort()
    except Exception as error:
        logger.warning("template_metadata_failed type=%s", type(error).__name__)


def _valid_selection(value):
    return (isinstance(value, dict) and isinstance(value.get("conditions"), dict)
            and isinstance(value.get("querys"), list) and bool(value["querys"])
            and all(isinstance(x, str) for x in value["querys"])
            and all(isinstance(k, str) for k in value["conditions"]))


def _compare_condition(con1, con2):
    return set(con1) == set(con2)


def _find_template(raw_ques, user_question_meta, scope=None):
    if not _valid_selection(user_question_meta):
        return ""
    for key, template in meta.items():
        if scope is not None and key not in scope:
            continue
        if (_valid_selection(template)
                and _compare_condition(template["conditions"], user_question_meta["conditions"])
                and sorted(template["querys"]) == sorted(user_question_meta["querys"])):
            return key
    return ""
