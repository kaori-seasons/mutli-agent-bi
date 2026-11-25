"""最小业务资产，只包含虚构结构和示例值。"""
import json
from pathlib import Path

from openpyxl import Workbook

from ontology.extractor import combine, extract_templates, extract_workbook


def workbook(path, scenario="订单", join_rule=None):
    wb = Workbook()
    summary = wb.active
    summary.title = "summary"
    if join_rule is None:
        join_rule = json.dumps([dict(left_table="orders", left_key="customer_id",
                                    right_table="customers", right_key="id", join_type="left")])
    for row in [("场景", scenario), ("场景描述", "订单销售业务"), ("查询规则", "仅查询有效订单"),
                ("关联规则", join_rule), ("同义词", "交易,销售")]:
        summary.append(row)
    table = wb.create_sheet("orders")
    for row in [("表名", "orders"), ("基本信息", "订单明细"), ("查询规则", "不包含取消订单"),
                ("字段名称", "字段类型", "数据逻辑", "是否保留", "字段含义", "其他说明"),
                ("id", "bigint", "维度", "PRIMARY KEY", "订单编号", ""),
                ("customer_id", "bigint", "维度", True, "客户编号", ""),
                ("amount", "decimal(12,2)", "度量", True, "销售额", "", "营收,GMV"),
                ("status", "varchar(20)", "维度,可枚举", True, "订单状态", "paid=已支付"),
                ("private_note", "text", "维度", False, "禁用字段", ""),
                (None,), ("ignored", "text", "维度", True, "空行后不处理", "")]:
        table.append(row)
    customers = wb.create_sheet("customers")
    for row in [("表名", "customers"), ("基本信息", "客户资料"), ("查询规则", ""),
                ("id", "bigint", "维度", "PRIMARY KEY", "客户编号", "")]:
        customers.append(row)
    wb.save(path)
    wb.close()
    return Path(path)


def snapshot(path, namespace="default"):
    workbook(path)
    data = extract_workbook(path, namespace, "approved")
    templates = extract_templates({"上海订单数量": dict(params=["上海"],
                    content="SELECT COUNT(*) FROM orders WHERE city='{0}'", outputs=["订单数量"],
                    synonyms=["上海订单总数"])}, namespace)
    return combine([data, templates])
