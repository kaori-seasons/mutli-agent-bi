import os
import logging
import threading
from logging.handlers import RotatingFileHandler
from queue import Queue
import pandas as pd


from .db import mysql

logger = logging.getLogger(__name__)

# 设置日志级别
logger.setLevel(logging.INFO)
# 创建一个handler，用于写入日志文件
handler = RotatingFileHandler('chatbi.log', maxBytes=100000, backupCount=3)
logger.addHandler(handler)

class Helper:

    @staticmethod
    def format(raw:str):
        # 定义一个字典，将中文字符映射为英文字符
        char_mapping = {
            '“': ' ',  # 中文左双引号
            '”': ' ',   # 中文右双引号
            '‘': "'",  # 中文左单引号
            '’': "'",   # 中文右单引号
            '"': "'",  # 中文逗号
        }
        
        # 使用字典推导式和 join() 方法替换所有匹配的字符
        content = ''.join(char_mapping.get(char, char) for char in raw)
        
        return content

    @staticmethod
    def build_select_scenario_msg(question:str, promptConfig:dict)->str:

        question = Helper.format(question)
        if 'DefaultPrompt' in promptConfig and promptConfig['DefaultPrompt'] !="":
            content = f"{promptConfig['Overall']['AllScenariosPrompt']} {promptConfig['Overall']['ScenarioSelectionPrompt']} {promptConfig['DefaultPrompt']}。"
        else:
            content = f"{promptConfig['Overall']['AllScenariosPrompt']} {promptConfig['Overall']['ScenarioSelectionPrompt']}。"

        content += "你要回答的问题是:{" + question +"}。"
        return content

    @staticmethod
    def build_question_msg(question_str:str, scenario:str, prompt_conf:dict, is_hard_mode = False, rag_str=""):

        a = f"""{prompt_conf[scenario]['RolePrompt']} {prompt_conf[scenario]['TablePrompt']} {prompt_conf[scenario]['IndicatorsListPrompt']}
                {prompt_conf[scenario]['OtherPrompt']},示例输出结构  {prompt_conf['Examples']}。{rag_str}。
                {prompt_conf['ChartPrompt']}。
                """

        if is_hard_mode:
            a += prompt_conf['HardPrompt']

        a += f"。现在你要回答的问题是: {question_str}"
        return a


    @staticmethod
    def get_rag_str(question:str)->str:
        return ""

    @staticmethod
    def bad_response(error:str="")->dict:
        r  = {
            "content": '哎呀，我思路有点乱，请重新问一次，多个点提示吧！'
        }
        if error !="":
            r["error"] = error

        return r


    @staticmethod
    def mk_chart_data(columns, columns_type, db_results, max_row=50)->dict:
        entity_name = dict()
        index_value = dict()
        chartData = dict()

        chartData['entity_name'] = entity_name
        chartData['index_value'] = index_value
        # 前端UI 暂时只支持一个度量，一个维度
        if len(columns) != 2 or "rows" not in db_results:
            return chartData

        # 前端目前只支持一个维度，一个度量
        if columns_type[0] == columns_type[1]:
            return chartData
       
        rows = db_results["rows"]
        try:
            index = 0
            for row in rows:
                if index > max_row:
                    break
                
                items = [item for item in row]
                for i in range(0, len(columns)):
                    if columns_type[i] == "度量":
                        index_value[index] = float(items[i])
                    else:
                        entity_name[index] = str(items[i])
                index+=1
        finally:
            return chartData

        
        
        
    
    @staticmethod
    def query_db(db_info:dict, fmt_sql:str, user_id:str, trace_id:str):
        logger.info(f"user:{user_id}===>trace id:{trace_id}===>query {db_info['desc']} data")
        conn = None
        try:
            conn = mysql.get_conn(db_info['host'], db_info['port'], db_info['user'], db_info['pwd'], db_info['db'])
            rows, row_count = mysql.fetch(fmt_sql, conn)
            return {
                "rows":rows,
                "row_count":row_count,
                "desc":db_info["desc"]
            }
        except Exception as ex:
            logger.error(f"user:{user_id}===>trace id:{trace_id}===>query {fmt_sql} with exception:\n{ex}")
            return {
                "row_count":0,
                "error":ex
            }
        finally:
            if conn:
                conn.close()

    @staticmethod
    def query_db_async(db_info:dict, fmt_sql:str, result_queue:Queue):
        logger.info(f"=======================>正在查询{db_info['desc']}的数据")
        conn = None
        try:
            conn = mysql.get_conn(db_info['host'], db_info['port'], db_info['user'], db_info['pwd'], db_info['db'])
            df = pd.read_sql(fmt_sql, conn) 
            r = {
                "rows":df,
                "row_count":len(df)
            }
            result_queue.put(r)
        except Exception as ex:
            logger.info(f"=====================>查询数据出现异常{fmt_sql}：\n{ex}")
            r = {
                "row_count":0,
                "error":ex
            }
        finally:
            if conn:
                conn.close()
        

    @staticmethod
    def query_many_db(db_infos:list, fmt_sql:str)->list:
        threads = []
        result_queue = Queue()
        for db_info in db_infos:
            # 从连接池获取连接
            thread = threading.Thread(target=Helper.query_db_async, args=(db_info, fmt_sql, result_queue))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        db_results = list()
        while not result_queue.empty():
            db_result = result_queue.get()
            if "rows" in db_result:
                db_results.append(db_result)
        
        return db_results

    @staticmethod
    def merge_data(db_results:list, columns:list, columns_type:list):
        if len(db_results) == 0:
            return {
            "rows":[],
            "row_count":0
        }

        dfs = [item["rows"] for item in db_results]

        merged_df = pd.concat(dfs, ignore_index=True)

        # 假设dimension1和dimension2是维度，measure1和measure2是度量  
        dimensions = list() 
        measures = list() 

        for index in range(0, len(columns)):
            if columns_type[index] =="度量":
                measures.append(columns[index])
            else:
                dimensions.append(columns[index])

        # 按照维度分组，并对度量求和  
        aggregated_df = merged_df.groupby(dimensions)[measures].sum().reset_index()
        # 查看聚合后的数据  
        records = aggregated_df.to_records(index=False).tolist()

        return {
            "rows":records,
            "row_count":len(records)
        }

    @staticmethod
    def mk_md_table(headers:list, db_result:list, max_row_return:int):

        # 开始构建Markdown表格
        md_table = "| " + " | ".join(headers) + " |\n"

        # 添加分隔线
        md_table += "| " + " | ".join(["---" for _ in headers]) + " |\n"

        index = 0

        if "rows" in db_result:
            rows = db_result["rows"]
            for row in rows:
                if index >= max_row_return:
                    break
                md_row = "| " + " | ".join([str(element) for element in row]) + " |"
                md_table += md_row + "\n"

                index +=1

        return md_table

    @staticmethod
    def mk_request_with_history(question_str:str, msg:list)->list:
        questions  = list()
        history_count = int(os.getenv("HISTORY_COUNT", 5))

        last_item = None
        counter = 1 # 不包含最后一个问题，最后一个问题需要pe
        while counter <  history_count and len(msg) - counter - 1 > 0:
            msg_item = msg[len(msg) - counter - 1]
            if last_item:
                if last_item["role"] == msg_item["role"]:
                    counter +=1
                    continue

            last_item = msg_item

            if msg_item["role"] == "assistant":
                if "finalSQL" in msg_item:
                    q =f"根据前面的讨论，生成的SQL是:{msg_item['finalSQL']}"
                elif "clarify" in msg_item:
                    q = msg_item['clarify']
                else:
                    q = "查询完毕"
            else:
                q = msg_item["content"]

            questions.append({
                "role":msg_item["role"],
                "content": q
            })
            counter+=1

        questions.reverse()
        if len(questions) > 0:
            first = questions[0]
            if first["role"] != "user":
                questions.remove(first)

        if len(questions) > 0:
            last = questions[-1]
            if last["role"] == "user":
                questions.remove(last)

            
        questions.append({
            "role":"user",
            "content": question_str
        })
        return questions

