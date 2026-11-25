"""MySQL query gate: one read statement, safe functions, bound template values."""
from dataclasses import dataclass, field
import math
import re
import string
from uuid import uuid4

import sqlglot
from sqlglot import exp

from .errors import ServiceError


SAFE_FUNCTIONS = frozenset("""
AND OR ABS AVG CEIL CEILING FLOOR ROUND TRUNCATE SUM COUNT MIN MAX COALESCE IFNULL NULLIF IF
CASE CAST TRY_CAST CONVERT CONCAT CONCAT_WS LOWER UPPER LENGTH CHAR_LENGTH SUBSTRING
SUBSTR TRIM LTRIM RTRIM REPLACE LEFT RIGHT YEAR MONTH DAY DAY_OF_MONTH DAY_OF_WEEK
DAY_OF_YEAR WEEK WEEK_OF_YEAR QUARTER DATE DATE_ADD DATE_SUB DATE_DIFF DATEDIFF
TIMESTAMPDIFF TIMESTAMP_DIFF TIMESTAMP_ADD TIMESTAMP_SUB TIME_TO_STR STR_TO_TIME
STR_TO_DATE DATE_FORMAT TS_OR_DS_TO_DATE TS_OR_DS_TO_TIMESTAMP CURRENT_DATE
CURRENT_TIMESTAMP CURRENT_TIME NOW EXTRACT LAST_DAY YEARWEEK UNIX_TIMESTAMP
FROM_UNIXTIME TIME_TO_UNIX UNIX_TO_TIME TO_CHAR ISNULL GREATEST LEAST
ROW_NUMBER RANK DENSE_RANK LAG LEAD FIRST_VALUE LAST_VALUE NTH_VALUE NTILE
PERCENT_RANK CUME_DIST MOD POW POWER SQRT STDDEV STDDEV_POP STDDEV_SAMP
VARIANCE VAR_POP VAR_SAMP GROUP_CONCAT ARRAY_AGG JSON_EXTRACT JSON_UNQUOTE
""".split())
FORBIDDEN_NODES = frozenset("""
Insert Update Delete Merge Create Drop Alter Command Transaction Commit Rollback
Set Use Grant Revoke Into Lock Hint Parameter SessionParameter Assignment
LoadData Copy Execute Pragma Attach Detach Directory UserDefinedFunction
""".split())


def rejected(message="仅允许受控的单条只读查询"):
    return ServiceError("SQL_REJECTED", message, 422)


def parse_read(sql, database=None, allow_parameters=False):
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 65536:
        raise rejected()
    # Includes MySQL executable comments and timeout hints. Bound values bypass parsing.
    if any(x in sql for x in ("/*", "--", "#")):
        raise rejected("查询注释及优化器提示不可执行")
    try:
        statements = [s for s in sqlglot.parse(sql, read="mysql") if s is not None]
    except sqlglot.errors.SqlglotError:
        raise rejected("SQL 无法安全解析") from None
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
        raise rejected()
    tree = statements[0]
    for node in tree.walk():
        if type(node).__name__ in FORBIDDEN_NODES:
            raise rejected()
        if isinstance(node, exp.Func):
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
            if name not in SAFE_FUNCTIONS or isinstance(node.parent, exp.Dot):
                raise rejected("不支持的函数或存在副作用风险")
        if isinstance(node, exp.Table):
            if not isinstance(node.this, exp.Identifier) or node.catalog:
                raise rejected("不允许动态表名或跨目录查询")
            if node.db and node.db != database:
                raise rejected("不允许访问当前数据源之外的数据库")
        if isinstance(node, exp.Placeholder):
            if not allow_parameters or not re.fullmatch(r"p\d+", node.name or ""):
                raise rejected("参数绑定无效")
    return tree


@dataclass(frozen=True)
class PreparedQuery:
    sql: str
    parameters: dict = field(default_factory=dict)

    def driver_query(self, database):
        for value in self.parameters.values():
            _value(value)
        tree = parse_read(self.sql, database, allow_parameters=bool(self.parameters))
        names = {n.name for n in tree.find_all(exp.Placeholder)}
        if names != set(self.parameters):
            raise rejected("参数数量不匹配")
        markers = {}
        for node in list(tree.find_all(exp.Placeholder)):
            marker = "bi_" + uuid4().hex
            markers[":" + marker] = "%(" + node.name + ")s"
            node.set("this", marker)
        rendered = tree.sql(dialect="mysql")
        if self.parameters:
            rendered = rendered.replace("%", "%%")
            for marker, placeholder in markers.items():
                rendered = rendered.replace(marker, placeholder)
        return rendered, self.parameters or None


def _value(value):
    if value is None or type(value) in (str, bool, int):
        if isinstance(value, str) and len(value) > 4096:
            raise rejected("参数过长")
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise rejected("参数必须为标量或 IN 列表")


def bind_template(template, values):
    """Migrate legacy '{0}' and {0} value slots to binding, never interpolation."""
    if not isinstance(template, str) or len(template) > 65536 or len(values) > 32:
        raise rejected("模板无效")
    if any(x in template for x in ("/*", "--", "#")):
        raise rejected()
    try:
        fields = list(string.Formatter().parse(template))
    except ValueError:
        raise rejected("模板占位符无效") from None
    positions = []
    for _, name, spec, conversion in fields:
        if name is not None:
            if not name.isdecimal() or spec or conversion or int(name) >= len(values):
                raise rejected("模板仅支持位置值参数")
            positions.append(int(name))
    if set(positions) != set(range(len(values))) or ":p" in template:
        raise rejected("模板参数不完整")
    for index in set(positions):
        for quote in ("'", '"'):
            template = template.replace(quote + "{" + str(index) + "}" + quote, ":p" + str(index))
        template = template.replace("{" + str(index) + "}", ":p" + str(index))
    try:
        statements = [s for s in sqlglot.parse(template, read="mysql") if s is not None]
        if len(statements) != 1:
            raise rejected()
        tree = statements[0]
    except sqlglot.errors.SqlglotError:
        raise rejected("模板无法解析") from None
    parameters = {}
    for node in list(tree.find_all(exp.Placeholder)):
        if not re.fullmatch(r"p\d+", node.name):
            raise rejected()
        value = values[int(node.name[1:])]
        if isinstance(node.parent, (exp.Table, exp.Ordered, exp.Limit, exp.Offset)):
            raise rejected("标识符和排序/分页位置不能使用模板参数")
        if isinstance(value, list):
            if not value or len(value) > 100 or not isinstance(node.parent, exp.In) or node.parent.expressions != [node]:
                raise rejected("列表参数只能用于完整 IN 值列表")
            replacements = []
            for item in value:
                name = "p" + str(len(parameters))
                parameters[name] = _value(item)
                replacements.append(exp.Placeholder(this=name))
            node.parent.set("expressions", replacements)
        else:
            name = "p" + str(len(parameters))
            parameters[name] = _value(value)
            node.replace(exp.Placeholder(this=name))
    if any(":p" in n.this for n in tree.find_all(exp.Literal) if n.is_string):
        raise rejected("请将完整值绑定到参数，不支持字符串内嵌占位符")
    if not parameters and values:
        raise rejected("模板参数未绑定到值")
    return PreparedQuery(tree.sql(dialect="mysql"), parameters)
