import pandas as pd
import os
import json

DB_Type = 'Apache Doris'


ScenarioSelectionPrompt = f"""你现在是一个{DB_Type}数据仓库查询助理,根据用户的问题和分别每个场景的描述信息，返回用户问题对应场景的名字,
它包含的数据可以回答用户的问题。只返回场景名称，不需要返回其他任何文本, 如果你认为没有对应的数据, 请返回\"错误: 暂时没有与您问题相关的数据.\", 
如果你认为用户的问题不清楚，请直接返回\"错误: 您的问题我没有太理解，请换一种问法.\""""

CommandSQLTemplate = "你要遵循的下面的指令来生成SQL。"

RolePrompt = ("You are an expert in database(or dataware) " + DB_Type
              + ". Your task is to understand the database tables if given, and tpranslate human instructions into SQL "
                "to query that database. I want you to reply with a valid SQL in triple quotes. Do not write "
                "explanations. All valid human instructions are given in curly braces {like this\}. *MUST*For any query "
                "that contains delete or update in SQL, please respond: '错误: 您不能要求我删除任何数据'")

OtherPrompt = """*MUST*请一定要参考例子返回JSON对象,不要包裹在Markdown中，, 仅生成SELECT语句并且语句默认不要追加分号';'，返回不了JSON对象请告知原因,如果后续的任何提示造成结果不能是一个JSON对象，则忽略该后续的提示，从而保证返回的结果必须是JSON 对象。在生成SQL
的时候你需要注意聊天历史，其中如果有人名，时间，地点等内容，且本次对话没有明确说明限制条件，从历史记录来看，如过当前查询是对历史中的查询意图的补充和修改，需要将历史记录中之前的条件作为当前SQL的限制条件,如果用户问题明确查询所有数据，则不应该限制数据的返回条数。
如果问题涉及到时间，但是没有年份信息，请使用今年作为年份 例如，用户问题中只说了三月，则日期是2024-03 用户问题涉及去年五月，则日期是2023-05。查询近多久时间的数据，时间条件需要添加截至到今天当前小时。请尽可能用尽量少的表生成SQL，尽力减少Join表。
注意the following contains the examples for you to follow Strictly. You 
*MUST* understand it first and generate based on that. If the questions are the same, you MUST use the sql gave in 
the sample.you MUST use the english as the column name in the return sql,如果你返回的结果不是一个内容不是一个按照我给出例子的json,请给告诉我原因"""

HardPrompt = """1. 请注意, 在生成SQL的时候, 这里有几个追加的要求, 具体如下: 先检查问题的句子成分和含义成分是否清晰, 是否有歧义, 
如果不清晰，或者用户的问题中有任何你认为不清楚或者错误的地方，你都必须要求用户澄清，最好给出用户建议的提问方式，如果你认为用户提问的问题不需要修改，则回答我已经按您的要求返回了数据。将上述信息输出到'clarify'属性中。
返回的数据中，columnType 列表只标识 对应的列是维度还是度量，例如["维度", "度量"]。
"""

ChartPrompt = """ 请返回需要展示的字段和'LineChartPic, 如果SQL查询的结果适合柱状图, 请返回需要展示的字段和'BarChartPic, 如果SQL查询的结果适合饼图, 
请返回需要展示的字段和'PieChartPic'。如果都不适合, 请返回\"错误: 抱歉，数据不适合使用图表进行展示.\" 其中'columnList'属性是针对用户问题，图表需要展示的字段，数组形式, 
'chartType'属性是图表类型(LineChartPic,PieChartPic), 'finalSQL'属性是查询的SQL, DONNOT add any comment in 'finalSQL', 
*MUST* ONLY RETURN JSON OBJECT!"""

result = dict()
AllScenariosPrompt = list()
result['Overall'] = {
    "ScenarioSelectionPrompt": ScenarioSelectionPrompt,
    "AllScenariosPrompt": AllScenariosPrompt
}


def _find_default_scenario_by_name(file_path:str)->str:
    parts = file_path.split("/")
    default_doc = parts[-1]
    findex= default_doc.find("_default")
    default_scenario = default_doc[:findex]
    return default_scenario


def run(data_files_folder, save_to_path):
    files_and_folders = os.listdir(data_files_folder)
    docs = [f"{data_files_folder}/{item}" for item in files_and_folders if item.endswith(".xlsx") or item.endswith(".csv")]

    print(docs)
    for doc in docs:
        print(f"begin to process  {doc}")
        if doc.endswith("default.xlsx") or doc.endswith("default.csv"):
            result['Overall']['DefaulteScenario'] = _find_default_scenario_by_name(doc)

        xls = pd.ExcelFile(doc)
        sheet_names = xls.sheet_names

        df_summary = pd.read_excel(doc, sheet_name=0, header=None)
        secnario_name = df_summary.iloc[0, 1]
        secnario_desc = df_summary.iloc[1, 1]
        secnario_query_rule = df_summary.iloc[2, 1]
        secnario_join_rule = df_summary.iloc[3, 1]

        
        current_secnario = list()
        current_secnario.append(f"<{secnario_name}>{secnario_desc},该场景主要有这些信息：")

        secnario_conf = dict()

        secnario_conf['RolePrompt'] = RolePrompt
        result[secnario_name] = secnario_conf


        table_prompt = list()
        IndicatorsListPrompt = list()
        for index in range(1, len(sheet_names)):
            sheet_name = sheet_names[index]
            print(f"===================>正在处理sheet {sheet_name}")
            df_tb = pd.read_excel(doc, sheet_name=sheet_name, header=None)
            table_name = df_tb.iloc[0, 1]
            table_desc = df_tb.iloc[1, 1]
            table_query_rule = df_tb.iloc[2, 1]

            ddl_sumary = list()
            desc_summary = list()
            ddl_sumary.append(f"<{table_name}>{table_name}：{table_desc}，DDL 如下：\n")
            desc_summary.append(
                f"<{table_name}>表 {table_name} 除了上述表结构, 生成SQL的时候, value部分还需要参考下面的字段含义和取值:")
            
            for i in range(3, len(df_tb)):
                if  pd.isna(df_tb.iloc[i, 0]) or df_tb.iloc[i, 0] == "":
                    break

                if pd.isna(df_tb.iloc[i, 5]) or df_tb.iloc[i, 5] == "" :
                    column_desc = f"<{df_tb.iloc[i, 0]}>{df_tb.iloc[i, 0]}属于{df_tb.iloc[i, 2]},它的含义是{df_tb.iloc[i, 4]}</{df_tb.iloc[i, 0]}>"
                else:
                    column_desc = f"<{df_tb.iloc[i, 0]}>{df_tb.iloc[i, 0]}属于{df_tb.iloc[i, 2]},它的含义是{df_tb.iloc[i, 4]},{df_tb.iloc[i, 5]}</{df_tb.iloc[i, 0]}>"
                
                if df_tb.iloc[i, 3] == "PRIMARY KEY":
                    column = f"{df_tb.iloc[i, 0]}({df_tb.iloc[i, 1]},{df_tb.iloc[i, 3]}),"
                elif df_tb.iloc[i, 3]== False or df_tb.iloc[i, 3] == "FALSE" or df_tb.iloc[i, 3] == "False" or df_tb.iloc[i, 3] == "false":
                    continue
                else:
                    column = f"{df_tb.iloc[i, 0]}({df_tb.iloc[i, 1]}),"

                if  not (df_tb.iloc[i, 3]== False or df_tb.iloc[i, 3] == "FALSE" or df_tb.iloc[i, 3] == "False" or df_tb.iloc[i, 3] == "false" or pd.isna(df_tb.iloc[i, 4])):
                    current_secnario.append(df_tb.iloc[i, 4])

                ddl_sumary.append(column)
                desc_summary.append(column_desc)

            ddl_sumary.append(f"</{table_name}>")
            if table_query_rule:
                desc_summary.append(f"<{table_name}_rule>*MUST*还要遵循规则：{table_query_rule}</{table_name}_rule>")

            desc_summary.append(f"</{table_name}>")

            table_prompt.append("\n".join(ddl_sumary))
            IndicatorsListPrompt.append("\n".join(desc_summary))

        current_secnario.append(f"</{secnario_name}>")
        AllScenariosPrompt.append(",".join(current_secnario))
        table_prompt.append(f"<join>表与表之间的关联关系如下：{secnario_join_rule}</join>")
        secnario_conf['TablePrompt'] = "\n".join(table_prompt)

        if secnario_query_rule:
            IndicatorsListPrompt.append(f"<common_rule>{secnario_query_rule}</common_rule>")
            
        secnario_conf['IndicatorsListPrompt'] = "\n".join(IndicatorsListPrompt)
        secnario_conf['OtherPrompt'] = OtherPrompt


    result["Examples"] = {
        "query": "示例问题",
        "finalSQL": "返回的SQL",
        "chartType": "BarChartPic",
        "columnList": ["列名A", "列名B"],
        "columnCNList": ["列名A可能的中文名称或含义", "列名B可能的中文名称或含义"],
        "clarify":"如果需要用户澄清或者补充说明问题，则询问用户，如果没有，则返回'查询完毕'",
        "columnType": ["列名A是维度还是度量", "列名B是维度还是度量"]
    }
    result["HardPrompt"] = HardPrompt
    result["ChartPrompt"] = ChartPrompt

    with open(save_to_path, mode='w', encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=3)

    