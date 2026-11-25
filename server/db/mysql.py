import pymysql.cursors
import os
import math
import time
from ..errors import ServiceError
from ..query_policy import PreparedQuery, parse_read


def get_conn(host,port,user, pwd, db_name, timeout=15):
    connection = pymysql.connect(host=host,
                                 port=port,
                                 user=user,
                                 password=pwd,
                                 database=db_name,
                                 connect_timeout=max(1, min(10, math.ceil(timeout))),
                                 read_timeout=max(1, math.ceil(timeout) + 1),
                                 write_timeout=max(1, math.ceil(timeout)),
                                 charset="utf8mb4",
                                 autocommit=False)
    return connection


def get_binlog_info(conn):
    with conn.cursor() as cursor:
        sql = 'show master status;'
        cursor.execute(sql)
        conn.commit()
        t = cursor.fetchone()
        return t['File'], t['Position']


def fetch_one(sql: str, conn):
    with conn.cursor() as cursor:
        cursor.execute(sql)
        conn.commit()
        t = cursor.fetchone()
        return t


def fetch(sql: str, conn):
    # Offline compatibility helper. Online requests use execute_read with source grants.
    database = conn.db.decode() if isinstance(conn.db, bytes) else conn.db
    query, params = PreparedQuery(sql).driver_query(database)
    with conn.cursor() as cursor:
        cursor.execute("SET SESSION MAX_EXECUTION_TIME = 15000")
        cursor.execute("START TRANSACTION READ ONLY")
        try:
            cursor.execute(query, params)
            rows = cursor.fetchmany(10001)
            if len(rows) > 10000:
                raise ServiceError("RESULT_TOO_LARGE", "离线结果超出上限", 422)
            return rows, len(rows)
        finally:
            conn.rollback()


def execute_read(info, query, *, timeout, max_rows, max_bytes):
    deadline = time.monotonic() + timeout
    tree = parse_read(query.sql, info["db"], allow_parameters=bool(query.parameters))
    limit = tree.args.get("limit")
    if limit is not None:
        value = limit.expression
        if not getattr(value, "is_int", False) or int(value.this) < 0:
            raise ServiceError("SQL_REJECTED", "LIMIT 必须为非负整数", 422)
        row_limit = min(int(value.this), max_rows + 1)
    else:
        row_limit = max_rows + 1
    tree = tree.limit(row_limit)
    sql, params = PreparedQuery(tree.sql(dialect="mysql"), query.parameters).driver_query(info["db"])
    conn = get_conn(info["host"], info["port"], info["user"], info["pwd"], info["db"], timeout)
    cursor = None
    try:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise ServiceError("BUDGET_EXCEEDED", "连接数据库时查询预算已用尽", 429)
        # Failure to establish read-only/timeout protection fails closed (no downgrade).
        with conn.cursor() as control:
            control.execute("SET SESSION MAX_EXECUTION_TIME = %s", (max(1, int(timeout * 1000)),))
            control.execute("START TRANSACTION READ ONLY")
        cursor = conn.cursor(pymysql.cursors.SSCursor)
        cursor.execute(sql, params)
        columns = [entry[0] for entry in cursor.description]
        if len(set(columns)) != len(columns):
            raise ServiceError("AMBIGUOUS_COLUMNS", "查询输出列必须使用唯一别名", 422)
        rows, size = [], 0
        for row in cursor:
            if time.monotonic() > deadline:
                raise ServiceError("BUDGET_EXCEEDED", "读取结果时查询预算已用尽", 429)
            size += sum(len(str(value).encode("utf-8")) for value in row)
            if size > max_bytes:
                raise ServiceError("RESULT_TOO_LARGE", "查询结果字节数超出上限，请缩小范围", 422)
            rows.append(row)
            if len(rows) > max_rows:
                break
        return {"rows": rows[:max_rows], "row_count": min(len(rows), max_rows),
                "columns": columns, "truncated": len(rows) > max_rows}
    finally:
        # Close the transport, not a streaming cursor (which can drain an enormous result).
        # Closing a connection also rolls back the read-only transaction.
        conn.close()
        # PyMySQL 1.2.4 destructors otherwise try to drain the now-closed socket.
        # This connection is discarded, never returned to a pool.
        for pending in (getattr(conn, "_result", None), getattr(cursor, "_result", None)):
            if pending is not None:
                pending.unbuffered_active = False
        if cursor is not None:
            cursor.connection = None
