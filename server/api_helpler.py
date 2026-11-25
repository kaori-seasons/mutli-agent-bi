import os

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
                if index >= max_row:
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
    def query_db(db_info, fmt_sql, user_id="", trace_id="", budget=None):
        from .execution import execute_source, QueryBudget
        return execute_source(db_info, fmt_sql, budget or QueryBudget())

    @staticmethod
    def query_many_db(db_infos, fmt_sql, budget=None):
        from .execution import execute_many, QueryBudget
        return execute_many(db_infos, fmt_sql, budget or QueryBudget())

    @staticmethod
    def merge_data(db_results, columns=None, columns_type=None, contract=None):
        from .federation import merge_results
        return merge_results(db_results, contract)

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

