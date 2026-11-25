"""Bounded expiring exports, fetched through authenticated HTTP, not public URLs."""
import csv
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from .errors import ServiceError


def _connect():
    root = Path(os.getenv("QUERY_EXPORT_PATH", "var/exports"))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "exports.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("""CREATE TABLE IF NOT EXISTS exports
        (id TEXT PRIMARY KEY, owner TEXT NOT NULL, sources TEXT NOT NULL,
         expires REAL NOT NULL, data BLOB NOT NULL)""")
    conn.execute("DELETE FROM exports WHERE expires <= ?", (time.time(),))
    conn.commit()
    return conn


def _cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def save_export(principal, sources, columns, rows, *, release=None):
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow([_cell(x) for x in columns])
    writer.writerows([_cell(x) for x in row] for row in rows)
    data = text.getvalue().encode("utf-8-sig")
    if len(data) > 16 * 1024 * 1024:
        raise ServiceError("EXPORT_TOO_LARGE", "导出超过容量上限", 422)
    identity = uuid4().hex
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        count, size = conn.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(data)), 0) FROM exports").fetchone()
        if count >= 1000 or size + len(data) > 128 * 1024 * 1024:
            raise ServiceError("EXPORT_CAPACITY", "导出暂时不可用", 503)
        manifest = sources if release is None else dict(source_ids=sources, **release)
        conn.execute("INSERT INTO exports VALUES (?, ?, ?, ?, ?)",
                     (identity, principal.subject, json.dumps(manifest), time.time() + 600, data))
        conn.commit()
    finally:
        conn.close()
    return "/api/downloads/" + identity


def read_export(principal, identity):
    if not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise ServiceError("NOT_FOUND", "文件不存在或已过期", 404)
    conn = _connect()
    try:
        row = conn.execute("SELECT owner, sources, data FROM exports WHERE id=? AND expires>?",
                           (identity, time.time())).fetchone()
        if not row or row[0] != principal.subject:
            raise ServiceError("NOT_FOUND", "文件不存在或已过期", 404)
        manifest = json.loads(row[1])
        source_ids = manifest if isinstance(manifest, list) else manifest["source_ids"]
        if not set(source_ids) <= principal.sources:
            raise ServiceError("NOT_FOUND", "文件不存在或已过期", 404)
        if isinstance(manifest, dict):
            from ontology.registry import Registry
            Registry().ensure_usable(manifest["namespace"], manifest["release_id"])
        return row[2]
    finally:
        conn.close()
