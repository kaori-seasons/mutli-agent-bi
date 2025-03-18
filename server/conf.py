import os
import json

mysql_info = dict()

template_info = dict()

def _load_db_conf():
    if len(mysql_info) == 0:
        hosts = os.getenv("MYSQL_HOST").split(",")
        ports =  os.getenv("MYSQL_PORT").split(",")
        databases = os.getenv("MYSQL_DATABASE").split(",")
        users = os.getenv("MYSQL_USER").split(",")
        pwds = os.getenv("MYSQL_PWD").split(",")
        keys = os.getenv("MYSQL_KEY").split(",")
        descs =  os.getenv("MYSQL_KEY_STR").split(",")

        for index in range(0, len(keys)):
            mysql_info[keys[index]] = {
                "host":hosts[index],
                "port":int(ports[index]),
                "db":databases[index],
                "user":users[index],
                "pwd":pwds[index],
                "desc":descs[index]
            }

# 定义一个函数来加载.env文件
def load_env(filename='.env'):
    # 确保.env文件存在
    if not os.path.exists(filename):
        raise FileNotFoundError(f"The specified file {filename} was not found.")

    # 读取.env文件中的每行
    with open(filename, 'r') as file:
        for line in file:
            # 忽略注释和空行
            line = line.strip()
            if line and not line.startswith('#'):
                # 分割键和值
                key_value_pair = line.split('=', 1)
                if len(key_value_pair) == 2:
                    key, value = key_value_pair
                    # 将键值对设置为环境变量
                    os.environ[key] = value

def get_env(key:str, default_v:str):
    return os.getenv(key, default_v)

def get_mysql_conf(db_key:str='au')->dict:
    _load_db_conf()

    if db_key in mysql_info:
        return mysql_info[db_key]

    return ValueError(f"db key {db_key} not found")

def get_mysql_conf_by_question(question:str)->list:
    _load_db_conf()
    
    for key in mysql_info:
        desc_info = mysql_info[key]['desc']
        if question.find(desc_info)>=0:
            return [mysql_info[key]]

    # 如果没找到则查询所有数据
    return [mysql_info[key] for key in mysql_info]


def load_sql_templates():
    sql_template_path = os.getenv("SQL_TEMPLATE_PATH")
    if sql_template_path.startswith("s3://"):
        parts = sql_template_path.split("/")
        bucket_name = parts[2]
        file_folder_kes = "/".join(parts[3:])
        pass

    else:
        result = []
        file_path = f"{os.getcwd()}/{sql_template_path}/summary.json"
        with open(file_path, mode='r') as f:
            summary = json.load(f)

        if summary:
            for item in summary:
                new_file_path = f"{os.getcwd()}/{sql_template_path}/{item['sql']}"
                with open(new_file_path, mode='r') as f:
                    lines = f.readlines()
                    sql = "\n".join(lines)
                    item['content'] = sql

                    template_info[item["question"]] ={
                        "params":item["params"],
                        "content":item["content"]
                    }


def get_sql_templates():
    return template_info
    
                    


    



    
