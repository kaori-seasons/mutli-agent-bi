import os
import json
from pathlib import Path
from .errors import ServiceError

mysql_info = dict()

template_info = dict()

def _load_db_conf():
    if len(mysql_info) == 0:
        names = ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE", "MYSQL_USER",
                 "MYSQL_PWD", "MYSQL_KEY", "MYSQL_KEY_STR")
        arrays = [os.getenv(name, "").split(",") for name in names]
        if (any(any(not item.strip() for item in values) for values in arrays)
                or len({len(values) for values in arrays}) != 1
                or len(set(arrays[5])) != len(arrays[5])):
            raise ServiceError("SOURCE_CONFIGURATION", "数据库配置缺失或长度不一致", 503)
        hosts = os.getenv("MYSQL_HOST").split(",")
        ports =  os.getenv("MYSQL_PORT").split(",")
        databases = os.getenv("MYSQL_DATABASE").split(",")
        users = os.getenv("MYSQL_USER").split(",")
        pwds = os.getenv("MYSQL_PWD").split(",")
        keys = os.getenv("MYSQL_KEY").split(",")
        descs =  os.getenv("MYSQL_KEY_STR").split(",")

        loaded = {}
        for index in range(0, len(keys)):
            try:
                port = int(ports[index])
                if not 1 <= port <= 65535:
                    raise ValueError()
            except ValueError:
                raise ServiceError("SOURCE_CONFIGURATION", "数据库端口配置无效", 503) from None
            loaded[keys[index]] = {
                "host":hosts[index],
                "port":port,
                "db":databases[index],
                "user":users[index],
                "pwd":pwds[index],
                "desc":descs[index]
            }
            loaded[keys[index]]["source_id"] = keys[index]
        mysql_info.update(loaded)

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
                    os.environ.setdefault(key, value)

def get_env(key:str, default_v:str):
    return os.getenv(key, default_v)

def get_mysql_conf(db_key:str='au')->dict:
    _load_db_conf()

    if db_key in mysql_info:
        return mysql_info[db_key]

    return ValueError(f"db key {db_key} not found")

def get_mysql_conf_by_question(question:str, allowed_sources=None, requested=None)->list:
    _load_db_conf()
    allowed = set(mysql_info) if allowed_sources is None else set(allowed_sources) & set(mysql_info)
    if requested is not None:
        if (not isinstance(requested, list) or not requested or len(requested) > 8
                or any(not isinstance(k, str) for k in requested) or len(set(requested)) != len(requested)):
            raise ServiceError("INVALID_SOURCE_SELECTION", "数据源选择无效")
        if not set(requested) <= allowed:
            raise ServiceError("FORBIDDEN_SCOPE", "无权访问所选数据源", 403)
        return [dict(mysql_info[k], source_id=k) for k in requested]
    if not allowed:
        raise ServiceError("FORBIDDEN_SCOPE", "没有可查询的数据源权限", 403)
    matched = [k for k in sorted(allowed) if mysql_info[k]["desc"]
               and mysql_info[k]["desc"].casefold() in question.casefold()]
    if matched:
        return [dict(mysql_info[k], source_id=k) for k in matched]
    if len(allowed) == 1:
        k = next(iter(allowed))
        return [dict(mysql_info[k], source_id=k)]
    raise ServiceError("AMBIGUOUS_SOURCE", "请明确选择数据源，未自动查询所有数据库", 422)


def load_sql_templates(*, publish=True):
    sql_template_path = os.getenv("SQL_TEMPLATE_PATH", "server/prompt_sqls")
    try:
        if sql_template_path.startswith("s3://"):
            raise ValueError("local reviewed templates required")
        root = Path(sql_template_path).resolve()
        summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
        if not isinstance(summary, list):
            raise ValueError()
        loaded = {}
        for item in summary:
            if (not isinstance(item, dict) or not isinstance(item.get("question"), str)
                    or not item["question"] or item["question"] in loaded
                    or not isinstance(item.get("sql"), str) or not item["sql"]
                    or not isinstance(item.get("params"), list)):
                raise ValueError()
            sources, keys = item.get("source_ids"), item.get("condition_keys")
            for values in (sources, keys):
                if values is not None and (not isinstance(values, list)
                        or any(not isinstance(x, str) or not x for x in values)
                        or len(set(values)) != len(values)):
                    raise ValueError()
            if sources is not None and not sources:
                raise ValueError()
            if keys is not None and len(keys) != len(item["params"]):
                raise ValueError()
            if item.get("merge") is not None:
                from .federation import validate_contract
                if sources is None:
                    raise ValueError()
                validate_contract(item["merge"], sources)
            path = (root / item["sql"]).resolve()
            if not path.is_relative_to(root):
                raise ValueError()
            content = path.read_text(encoding="utf-8")
            if not content.strip() or len(content) > 65536:
                raise ValueError()
            loaded[item["question"]] = {
                "params": item["params"], "content": content,
                "condition_keys": keys, "source_ids": sources, "merge": item.get("merge"),
            }
        # Never mutate the live set if any file/configuration failed validation.
        if publish:
            template_info.clear()
            template_info.update(loaded)
    except (OSError, ValueError, TypeError, KeyError, ServiceError):
        raise ServiceError("TEMPLATE_CONFIGURATION", "本地审核模板配置无效", 503) from None


def get_sql_templates():
    return template_info
