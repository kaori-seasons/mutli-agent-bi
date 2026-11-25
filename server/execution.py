"""All online SQL, including repairs and fan-out, passes through this boundary."""
from dataclasses import dataclass, field
import os
import threading
import time
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor

from .db import mysql
from .errors import ServiceError
from .query_policy import PreparedQuery

request_deadline = ContextVar("request_deadline", default=None)


def remaining_seconds():
    deadline = request_deadline.get()
    remaining = (deadline - time.monotonic()) if deadline is not None else 60
    if remaining <= 0:
        raise ServiceError("BUDGET_EXCEEDED", "查询预算已用尽", 429)
    return remaining


def bounded_int(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
        if not minimum <= value <= maximum:
            raise ValueError()
        return value
    except ValueError:
        raise ServiceError("EXECUTION_CONFIGURATION", "查询资源限制配置无效", 503) from None


@dataclass
class QueryBudget:
    deadline: float = field(default_factory=lambda: request_deadline.get() or
                           (time.monotonic() + bounded_int("QUERY_DEADLINE_SECONDS", 60, 1, 600)))
    max_queries: int = field(default_factory=lambda: bounded_int("QUERY_MAX_SQL_CALLS", 16, 1, 32))
    calls: int = 0
    lock: object = field(default_factory=threading.Lock)

    def consume(self):
        with self.lock:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or self.calls >= self.max_queries:
                raise ServiceError("BUDGET_EXCEEDED", "查询预算已用尽", 429)
            self.calls += 1
            return min(remaining, bounded_int("SQL_TIMEOUT_SECONDS", 15, 1, 120))


def execute_source(info, query, budget):
    identity = info["source_id"]
    query = query if isinstance(query, PreparedQuery) else PreparedQuery(query)
    # Validate before consuming a database connection. Denials must not become retryable SQL errors.
    query.driver_query(info["db"])
    timeout = budget.consume()
    try:
        result = mysql.execute_read(info, query, timeout=timeout,
                                    max_rows=bounded_int("SQL_MAX_RESULT_ROWS", 10000, 1, 100000),
                                    max_bytes=bounded_int("SQL_MAX_RESULT_BYTES", 8388608, 1024, 33554432))
        return dict(result, source_id=identity, status="success")
    except ServiceError:
        raise
    except Exception as exc:
        # SQL syntax/unknown-column errors may be repaired once; never expose driver text.
        number = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
        return {"source_id": identity, "status": "failed", "rows": [], "row_count": 0,
                "error": "数据库查询未完成", "error_code": "SOURCE_QUERY_FAILED",
                "retryable": number in {1054, 1064, 1146}}


def execute_many(infos, query, budget):
    if not 1 <= len(infos) <= 8:
        raise ServiceError("INVALID_SOURCE_SELECTION", "单次最多查询八个数据源")
    # Preflight every source before any SQL runs.
    prepared = query if isinstance(query, PreparedQuery) else PreparedQuery(query)
    for info in infos:
        prepared.driver_query(info["db"])

    def run(info):
        try:
            return execute_source(info, prepared, budget)
        except ServiceError as exc:
            return {"source_id": info["source_id"], "status": "failed", "rows": [],
                    "row_count": 0, "error_code": exc.code}

    with ThreadPoolExecutor(max_workers=min(4, len(infos))) as executor:
        return list(executor.map(run, infos))
