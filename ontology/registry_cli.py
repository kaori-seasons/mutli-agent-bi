"""Offline v1 compatibility adapter. Produces proposals only; never publishes."""
import argparse
import json
from pathlib import Path
import sys

from .schema import Snapshot
from .v2 import migrate_v1, validate, canonical


def main(argv=None):
    parser = argparse.ArgumentParser(description='Registry v2 离线迁移/校验；审核发布走鉴权 API')
    commands = parser.add_subparsers(dest='command', required=True)
    migration = commands.add_parser('migrate-v1')
    migration.add_argument('--input', required=True)
    migration.add_argument('--source-map', required=True, help='namespace 到 source_id 的 JSON 映射')
    migration.add_argument('--owner', required=True)
    migration.add_argument('--namespace', default='default')
    migration.add_argument('--output', required=True)
    check = commands.add_parser('validate')
    check.add_argument('--input', required=True)
    restore = commands.add_parser('export-v1-archive', help='恢复迁移时原始 v1 快照；不表示当前 v2 发布内容')
    restore.add_argument('--input', required=True)
    restore.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        path = Path(args.input)
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('input limit')
        raw = path.read_text(encoding='utf-8')
        if args.command == 'migrate-v1':
            document = migrate_v1(Snapshot.from_json(raw), args.namespace, json.loads(args.source_map), args.owner)
            output = canonical(document)
        else:
            document = validate(json.loads(raw))
            if args.command == 'export-v1-archive':
                snapshot = Snapshot.from_dict(document['migration']['source_snapshot'])
                if snapshot.revision:
                    snapshot.verify_revision()
                output = snapshot.to_json()
        if args.command != 'validate':
            # Refuse overwriting a user's reviewed file accidentally.
            with open(args.output, 'x', encoding='utf-8') as stream:
                stream.write(output + '\n')
        print(json.dumps({'schema_version': 2, 'entities': len(document['entities']),
                          'published': False, 'blockers': document['migration'].get('blockers', [])}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'error_type': type(exc).__name__, 'published': False}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
