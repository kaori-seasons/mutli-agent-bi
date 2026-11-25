from prompt import tool, gen
from server import conf, aws
import os
import json
import argparse
from server import conf, aws, llm
from server.db import mysql
from openpyxl import Workbook


def configure_assets():
    # 命令启动时才加载环境，导入模块不访问配置或外部服务。
    global bucket_name, example_file, prompt_file, rag_file
    global data_files, save_to_path, prompt_path, example_path, rag_path
    conf.load_env()
    bucket_name = os.getenv("BUCKET_NAME")
    example_file = os.getenv("EXAMPLE_FILE_NAME")
    prompt_file = os.getenv("PROMPT_FILE_NAME")
    rag_file = os.getenv("RAG_FILE_NAME")
    data_files = f"{os.getcwd()}/prompt/data/promptdata"
    save_to_path = f"{os.getcwd()}/prompt/prompt_conf"
    prompt_path = save_to_path + "/" + prompt_file.split("/")[1]
    example_path = save_to_path + "/" + example_file.split("/")[1]
    rag_path = save_to_path + "/" + rag_file.split("/")[1]
    os.makedirs(data_files, exist_ok=True)
    os.makedirs(save_to_path, exist_ok=True)


def confirm_action(prompt_input):
    """
    提示用户确认操作，返回用户的输入。
    """
    response = input(prompt_input).strip().lower()  # 去除首尾空格并转换为小写
    return response in ['y', 'yes']


def template_command(args):
    scenario = args.scenario
    tables = args.tables
    print(scenario)
    print(tables)

    db_infos = conf.get_mysql_conf_by_question(scenario)
    db_info = db_infos[0]
    conn = mysql.get_conn(
        db_info['host'], db_info['port'], db_info['user'], db_info['pwd'], db_info['db'])

    table_names = tables
    wb = Workbook()
    tables_desc = list()
    ontology_analyses = []
    for table_name in table_names:
        print(f"开始处理表{table_name}...")
        bedrock = aws.get('bedrock-runtime')

        schema = gen.get_table_schema(table_name, conn)
        schema_search = {
            item['Name']: item for item in schema
        }

        # 列可能会太多，超出大模型的长度，这里先写死一个分批的大小
        batch_count = 40
        begin_index = 0
        pyob_lst = None
        while begin_index < len(schema):
            end_index = min(begin_index + batch_count, len(schema))
            columns = schema[begin_index: end_index]
            sample_data = gen.get_sample_data(table_name, columns, conn)
            pmt = gen.PROMPT_F.format(table_name, columns, sample_data)

            questions = list()
            questions.append({
                "role": "user",
                "content": pmt
            })

            output = llm.query(questions, bedrock)
            print(output)
            pyob = json.loads(output)
            if pyob_lst:
                pyob_lst["columns"].extend(pyob["columns"])
            else:
                pyob_lst = pyob
                
            begin_index = end_index

        # 创建一个工作簿
        with open(f"{data_files}/{scenario}.json", mode='w') as f:
            output = json.dumps(pyob_lst)
            f.write(output)

        ws = wb.create_sheet(title=table_name)

        ontology_analyses.append((pyob_lst, schema))
        table_desc = pyob_lst['desc']
        columns = pyob_lst['columns']
        tables_desc.append(table_desc)
        ws.append(["表名", table_name])
        ws.append(["基本信息", table_desc])
        ws.append(["查询规则", ""])
        ws.append(["字段名称", "字段类型", "数据逻辑", "是否保留", "字段含义", "其他说明"])
        for column in columns:
            dtype = schema_search[column['name']]['Type']
            dw = f"{column['tips']},{column['option']}"
            desc = column['desc']
            if column['option_desc']:
                desc += "请注意，值*MUST*只能是这些值中的一个，这些值及它的含义是：" + \
                    column['option_desc']

            ws.append([column['name'], dtype, dw, "True", desc, ""])

    ws = wb.active
    ws.title = "summary"
    ws.append(["场景", scenario])
    ws.append(["场景描述", scenario+"包含这些信息："+",".join(tables_desc)])
    ws.append(["查询规则", ""])
    ws.append(["关联规则", ""])
    # 将DataFrame保存为Excel文件
    wb.save(f"{data_files}/{scenario}.xlsx")
    print(f"处理完毕，生成的template 文件位于{data_files}/{scenario}.xlsx")
    conn.close()
    _ontology_write("analysis", scenario=scenario, analyses=ontology_analyses)


def prompt_command():
    print("请确认您已经创建了好了提示词模板并进行了人工Review！默认情况下提示词模板excel文件存放在prompt/data/promptdata/路径下")
    if confirm_action("输入 'y' 表示你创建好了提示词模板并进行了reivew，输入 'n' 退出："):
        tool.run(data_files, prompt_path)
        published = [
            aws.upload_file_to_s3(prompt_path, bucket_name, prompt_file),
            aws.upload_file_to_s3(example_path, bucket_name, example_file),
            aws.upload_file_to_s3(rag_path, bucket_name, rag_file),
        ]
        if all(result is True for result in published):
            _ontology_write("publish")
        else:
            print("既有发布未全部成功，本体审核区未更新；请检查发布后离线重试")


def _ontology_write(mode, **kwargs):
    # 延迟导入与最终隔离，连本体包/日志故障也不能改变原命令结果。
    try:
        from ontology.extractor import safe_publish_workbooks, safe_write_analysis
        namespace = os.getenv("ONTOLOGY_NAMESPACE", "default")
        if mode == "analysis":
            return safe_write_analysis(kwargs["scenario"], kwargs["analyses"], namespace)
        return safe_publish_workbooks(data_files, namespace, os.getenv("SQL_TEMPLATE_PATH"))
    except Exception as exc:
        print(f"本体双写失败（{type(exc).__name__}），既有产物已保留，请用 python -m ontology extract 离线重试")
        return None


def main():
    # 创建一个解析器
    parser = argparse.ArgumentParser(description="提示词生成辅助工具")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # 创建 parser_t'';l'klk,.  emplate 子命令的解析器
    parser_template = subparsers.add_parser(
        "template", help="生成提示词模板，供人工review")
    parser_template.add_argument("--scenario", type=str, help="场景名称")
    parser_template.add_argument(
        "--tables", type=str, nargs="+", help="数据表列表，表名称之间用空格隔开")
    

    # 创建 print 子命令的解析器
    parser_prompt = subparsers.add_parser(
        "prompt", help="生成提示词文件，并上传到S3中，执行前请确保已经生成了提示词模板，并人工进行reivw")

    args = parser.parse_args()
    if args.command in {"template", "prompt"}:
        configure_assets()
    if args.command == "template":
        template_command(args)
    elif args.command == "prompt":
        prompt_command()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
