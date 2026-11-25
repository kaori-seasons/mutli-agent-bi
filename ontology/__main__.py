"""离线抽取/校验/重试/回退命令，失败返回非零退出码。"""
import argparse
import json
import sys
from pathlib import Path

from .extractor import (combine, extract_analysis, extract_template_directory,
                        extract_workbook, publish_workbooks)
from .schema import COLLECTIONS, Snapshot, ValidationError
from .store import FileOntologyStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="本体离线管理（不访问数据库或 AWS）")
    parser.add_argument("--store", help="本体仓库目录；默认使用 ONTOLOGY_STORE_PATH")
    parser.add_argument("--namespace", default="default", help="数据源/资产命名空间")
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract", help="抽取或重试双写")
    extract.add_argument("--workbooks", help="权威 Excel 目录，清除该命名空间已删除的工作簿")
    extract.add_argument("--workbook", action="append", default=[], help="单个工作簿，可重复")
    extract.add_argument("--templates", help="含 summary.json 的 SQL 模板目录")
    extract.add_argument("--analysis", help="[{analysis: PROMPT_F结果, schema:物理列数组}] JSON 文件")
    extract.add_argument("--scenario", help="分析结果的场景名")
    extract.add_argument("--reviewed", action="store_true", help="确认已人工审核，写入审核区")
    validate = commands.add_parser("validate", help="独立读取、格式/引用/修订校验")
    validate.add_argument("--file", help="直接校验快照文件")
    validate.add_argument("--stage", choices=["pending", "approved"], default="approved")
    history = commands.add_parser("history", help="列出可回退修订")
    history.add_argument("--stage", choices=["pending", "approved"], default="approved")
    rollback = commands.add_parser("rollback", help="恢复历史内容，保留实体版本递增")
    rollback.add_argument("revision")
    rollback.add_argument("--stage", choices=["pending", "approved"], default="approved")
    remove = commands.add_parser("remove", help="显式清除来源资产；不会影响其他来源")
    remove.add_argument("asset")
    remove.add_argument("--stage", choices=["pending", "approved"], default="pending")
    args = parser.parse_args(argv)
    store = FileOntologyStore(args.store)
    try:
        if args.command == "history":
            revisions = sorted(p.stem for p in (store.path / "history" / args.stage).glob("*.json"))
            print(json.dumps({"revisions": revisions}))
            return 0
        if args.command == "validate":
            if args.file:
                snapshot = store._read_file(Path(args.file))
            else:
                if store.revision_token(args.stage) is None:
                    raise ValidationError("指定审核区尚无快照")
                snapshot = store.read(args.stage)
        elif args.command == "rollback":
            snapshot = store.rollback(args.revision, args.stage)
        elif args.command == "remove":
            snapshot = store.replace_sources(Snapshot(), {(args.namespace, args.asset)}, args.stage)
        else:
            status = "approved" if args.reviewed else "pending"
            if args.workbooks:
                if args.workbook or args.analysis:
                    raise ValidationError("目录模式不能同时指定工作簿或分析 JSON")
                snapshot = publish_workbooks(args.workbooks, args.namespace, status, store, args.templates)
            else:
                parts = [extract_workbook(p, args.namespace, status) for p in args.workbook]
                sources = {(args.namespace, Path(p).name) for p in args.workbook}
                if args.templates:
                    parts.append(extract_template_directory(args.templates, args.namespace, status))
                    sources.add((args.namespace, "sql_templates"))
                if args.analysis:
                    if not args.scenario or args.reviewed:
                        raise ValidationError("分析 JSON 需要 --scenario，且不能绕过 Excel 审核")
                    data = json.loads(Path(args.analysis).read_text(encoding="utf-8"))
                    parts.append(extract_analysis(args.scenario, [(i["analysis"], i["schema"]) for i in data], args.namespace))
                    sources.add((args.namespace, args.scenario + ".xlsx"))
                if not parts:
                    raise ValidationError("至少指定一种抽取输入")
                snapshot = store.replace_sources(combine(parts), sources, status)
        print(json.dumps(dict(revision=snapshot.revision,
                              counts={name: len(getattr(snapshot, name)) for name in COLLECTIONS},
                              warnings=snapshot.warnings), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"event": "ontology_command_failed", "error_type": type(exc).__name__},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
