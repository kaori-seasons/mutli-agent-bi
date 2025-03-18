
import boto3
import os
import json
import re

from uuid import uuid4
from . import llm
from . import conf
from . import aws
from . import prompt
from . import sql
from .api_helpler import Helper


import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger(__name__)

# 设置日志级别
logger.setLevel(logging.INFO)
# 创建一个handler，用于写入日志文件
handler = RotatingFileHandler('logs/chatbi.log', maxBytes=100000, backupCount=10)
logger.addHandler(handler)


# 创建一个handler，用于将日志输出到控制台
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
logger.addHandler(console_handler)

# 定义日志格式
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

meta = dict()

def init():
    logger.info("正在加载和分析模板SQL")
    _load_template_questions()

def get_result(msg:list,trace_id:str, user_id:str='', mode_type: str ='normal'):
    logger.info(f"user:{user_id}===>trace id:{trace_id}===>begin to query data")

    bedrock = aws.get('bedrock-runtime')
    bedrock_result = answer_template_sql(bedrock, msg, trace_id)
    if "error" in bedrock_result:

        prompt_content = prompt.get("PROMPT_FILE_NAME")
        is_hard = True
        bedrock_result =  answer(bedrock, msg, prompt_content, trace_id, is_hard)

    if "error" in bedrock_result:
        logger.info(f"user:{user_id}===>trace id:{trace_id}===>failed to query data")
        return  {
            "content":bedrock_result["error"],
            "mdData":"",
            "chartData":dict(),
            "sql":"",
            "chartType":""
        }

    fmt_sql = sql.format_md(bedrock_result['bedrockSQL'])
    logger.info(f"user:{user_id}===>trace id:{trace_id}===>get sql {fmt_sql}")
    last_item = msg[-1]
    raw_content = last_item['content']

    max_row_return = int(os.getenv("MAX_ROW_COUNT_RETURN", "50"))

    db_infos = conf.get_mysql_conf_by_question(raw_content)
    

    columns = bedrock_result['bedrockColumn']
    column_types = bedrock_result['column_type']
    if len(db_infos) == 1:
        db_info = db_infos[0]
        db_results =Helper.query_db(db_info, fmt_sql, user_id, trace_id)
    else:
        db_results= Helper.query_many_db(db_infos, fmt_sql)
        db_results = Helper.merge_data(db_results, columns, column_types)

    if "error" in db_results:
        db_results = retry_when_sql_error(user_id, trace_id,msg,fmt_sql, db_results, db_infos, bedrock)
        fmt_sql = db_results['finalSQL']


    if 'cn_column' in bedrock_result:
        cn_columns = bedrock_result['cn_column']
    else:
        cn_columns = columns
    
    md_table = Helper.mk_md_table(cn_columns, db_results, max_row_return)

    chart_data = Helper.mk_chart_data(cn_columns,column_types, db_results, max_row_return)

    chartType ="BarChartPic" if bedrock_result['chart_type'].find("错误") >=0 else bedrock_result['chart_type']

    result = {
        "mdData":md_table,
        "chartData":chart_data,
        "sql":fmt_sql,
        "chartType":chartType,
    }

    if "clarify" in bedrock_result:
        result['content'] = bedrock_result['clarify']
        

    total_row_count = db_results["row_count"]
    if total_row_count >= max_row_return:
        # 数据量太大，则保存到s3，生成下载链接让客户后台下载
        bucket_name = os.getenv("BUCKET_NAME")
        download_host =os.getenv("DOWNLOAD_HOST")
        if download_host:
            load_url =  aws.save_2_local(cn_columns, db_results, f"{user_id}_{trace_id}")
        else:
            load_url =  aws.upload_csv_to_s3(cn_columns, db_results, bucket_name, f"{user_id}_{trace_id}")

        result['extra'] = load_url
        many_msg = f"\n数据量较大，默认只显示了 {max_row_return}, 请点击下载查看全部数据。建议使用汇总数据而非明细数据分析"
        if 'content' in result:
            result['content'] =result['content'] +many_msg
        else:
             result['content'] = many_msg

    else:
        result['extra'] = ""
        
    logger.info(result)
    logger.info(f"user:{user_id}===>trace id:{trace_id}===>success to query data")
    return result

def answer(
        bedrock,
        msg:list, 
        promptConfig:dict,
        trace_id:str,
        is_hard_mode:bool):
    # 对问题进行提示词工程并查询bedrock
    last_item = msg[-1]
    raw_content = last_item['content']
    scenario_str = Helper.build_select_scenario_msg(raw_content, promptConfig)

    rag_str = Helper.get_rag_str(last_item['content'])

    questions  = list()
    # questions.extend(msg)
    questions.append({
        "role":"user",
        "content": scenario_str
    })
    logger.info("begin select scenario")
    scenario = llm.query(questions,bedrock_client=bedrock)
    

    if scenario not in promptConfig:
        # 如果有默认场景就尝试使用默认场景
        if 'DefaulteScenario' in promptConfig["Overall"]:
            error = f"{trace_id}===============>没有找到合适的场景: {scenario}，尝试使用默认场景查询{promptConfig['Overall']['DefaulteScenario']}"
            logger.info(error)
            scenario =promptConfig['Overall']['DefaulteScenario']
        else:
            error = f"{trace_id}===============>failed to find scenario in prompt config file: {scenario}"
            logger.info(error)
            return Helper.bad_response(error=error)
        

    logger.info(f"{trace_id}===============>{scenario} is selected")
               

    question_str = Helper.build_question_msg(raw_content,scenario,promptConfig,is_hard_mode, rag_str)
    questions  = Helper.mk_request_with_history(question_str, msg)


    result = llm.query(questions,bedrock_client=bedrock)
    result = llm.format_bedrock_result(result)
    
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        error = f"{trace_id}===================> 返回的结果不是json\n{result}"
        logger.info(error)
        return Helper.bad_response(error=error)

    if  "finalSQL" not in parsed and  (parsed['finalSQL'] =="" or parsed['finalSQL'].find("ERROR: You can only read data.") >= 0):

        error = f"{trace_id}===================> 返回的结果没有生成SQL"
        logger.info(error)
        return Helper.bad_response(error=error)

    if 'columnList' in parsed and isinstance(parsed["columnList"], list):
        columns = list()
        for item in parsed["columnList"]:
            parts = item.split(" AS ")
            if len(parts) > 1:
                columns.append(parts[1])
            else:
                columns.append(item)

        parsed["columnList"] = columns
    

    result_j = {
      "bedrockSQL": parsed['finalSQL'],
      "queryTableName": scenario,
      "bedrockColumn": columns,
      "cn_column":parsed['columnCNList'],
      "column_type": parsed['columnType'],
      "chart_type": parsed['chartType']
    }
    if is_hard_mode:
        result_j["clarify"] =parsed["clarify"]

    return result_j


def answer_template_sql(
        bedrock,
        msg:list, 
        trace_id:str):

    # 对问题进行提示词工程并查询bedrock
    last_item = msg[-1]
    raw_content = last_item['content']


    # 开始查询问题对应的模板问题
    question_prompt = prompt.build_template_question_meta_prompt(raw_content)

    questions=[{
        "role":"user",
        "content": question_prompt
    }]


    result = llm.query(questions,bedrock_client=bedrock)

    try:
        parsed = json.loads(result)
    except json.JSONDecodeError as ex:
        error  = f"{trace_id}===================> 没有找到模板问题,原因是:\n{result}\n{ex}"
        logger.info(error)
        # 如果解析失败，返回False
        return Helper.bad_response(error)


    template_question =  _find_template(raw_content, parsed)

    if not template_question:
        error  = f"{trace_id}===================> 没有找到模板问题:\n{parsed}"
        logger.info(error)
        # 如果解析失败，返回False
        return Helper.bad_response(error)

    logger.info(f"{trace_id}===================> 找到模板问题\n{parsed}")
    params = parsed["conditions"].values()
    new_params = list()
    for param in params:
        if isinstance(param, list):
            new_p = ",".join(param)
            new_params.append(new_p)
        else:
            new_params.append(param)


    # 获取模板问题对应的模板SQL
    template_sql = prompt.template_sql(template_question)
    if template_sql == "":
        error  = f"{trace_id}===================> 模板SQL为空\n{template_question}"
        logger.info(error)
        # 如果解析失败，返回False
        return Helper.bad_response(error)
    
    fmt_sql = template_sql.format(*new_params)

    sql_column_prompt = prompt.template_sql_columns(fmt_sql, raw_content)

    questions  = list()
    # questions.extend(msg)
    questions.append({
        "role":"user",
        "content": sql_column_prompt
    })
    result = llm.query(questions,bedrock_client=bedrock)
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        error  = f"{trace_id}===================> 没有找到模板sql列信息\n{result}"
        logger.info(error)
        return Helper.bad_response(error)

    columns = parsed["columns"]
    columns_ype = parsed["columns_type"]
    info  = f"{trace_id}===================> 返回的模板SQL为:\n{fmt_sql}"
    logger.info(info)
    result_j = {
      "bedrockSQL": fmt_sql,
      "queryTableName": "template",
      "bedrockColumn": columns,
      "column_type": columns_ype,
      "chart_type": "BarChart"
    }
    return result_j

def retry_when_sql_error(user_id:str, trace_id:str, msg:list,fmtsql:str, raw_db_results:dict, db_infos:list, bedrock_client):
    fix_query = prompt.template_fix_query_error(fmtsql, raw_db_results["error"])
    questions = Helper.mk_request_with_history(fix_query, msg)
    result = llm.query(questions,bedrock_client=bedrock_client)
    result = llm.format_bedrock_result(result)
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        error = f"{trace_id}===================> 返回的结果不是json\n{result}"
        logger.info(error)

    if  "finalSQL" not in parsed and  (parsed['finalSQL'] =="" or parsed['finalSQL'].find("ERROR: You can only read data.") >= 0):
        error = f"{trace_id}===================> 返回的结果没有生成SQL"
        logger.error(error)
        return {
            "row_count":0,
            "error":error
        }
    else:
        fmt_sql = parsed["finalSQL"]
        db_info = db_infos[0]
        db_results =Helper.query_db(db_info, fmt_sql, user_id, trace_id)
        db_results['finalSQL'] = fmt_sql
        return db_results


def _load_template_questions():
    if not meta:
        p = prompt.build_template_options_question()
        msg = [{
            "role":"user",
            "content":p
        }]
    
        try:
            bedrock_client = aws.get('bedrock-runtime')
            print("done")
            result_str = llm.query(msg, bedrock_client)
            parsed = json.loads(result_str)
        except Exception as ex:
            logger.error(f"分析模板问题出现错误:{ex}")
            return None

        logger.info(parsed)
        # templates = conf.get_sql_templates()
        # for key in parsed:
        #     params1 = templates[key]['params']
        #     item = parsed[key]
        #     conditions = item["conditions"]

        for key in parsed:
            meta[key] = parsed[key]
            meta[key]['querys'].sort()

        

    

def _compare_condition(con1, con2)->bool:
    for key in con1:
        if key not in con2:
            return False
    return True


def _find_template(raw_ques:str, user_question_meta):
    if raw_ques in meta:
        return raw_ques

    querys = user_question_meta["querys"]
    conditions = user_question_meta["conditions"]

    for key in meta:
        tp = meta[key]
        if len(conditions)!=len(tp['conditions']):
            continue

        if len(querys)!=len(tp['querys']):
            continue

        if not _compare_condition(conditions, tp['conditions']):
            logger.info("查询条件不同")
            continue
        
        
        querys.sort()
        if querys != tp['querys']:
            logger.info("查询内容不同")
            continue

        return key

    return ""

        







    

    

                                             
