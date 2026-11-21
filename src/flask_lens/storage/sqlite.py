"""Zero-dependency SQLite WAL ring-buffer storage backend."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections import defaultdict
from typing import Any

from flask_lens.models import CapturedRequest
from flask_lens.storage.base import RequestFilter, compute_percentile

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lens_requests (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT UNIQUE NOT NULL,
    timestamp       REAL NOT NULL,
    method          TEXT NOT NULL,
    path            TEXT NOT NULL,
    full_path       TEXT NOT NULL,
    endpoint        TEXT,
    rule            TEXT,
    status_code     INTEGER NOT NULL,
    duration_ms     REAL NOT NULL,
    sql_count       INTEGER NOT NULL DEFAULT 0,
    sql_duration_ms REAL NOT NULL DEFAULT 0.0,
    has_n_plus_one  INTEGER NOT NULL DEFAULT 0,
    n_plus_one_cnt  INTEGER NOT NULL DEFAULT 0,
    has_slow_sql    INTEGER NOT NULL DEFAULT 0,
    dup_sql_count   INTEGER NOT NULL DEFAULT 0,
    outgoing_count  INTEGER NOT NULL DEFAULT 0,
    log_count       INTEGER NOT NULL DEFAULT 0,
    has_exception   INTEGER NOT NULL DEFAULT 0,
    exc_type        TEXT,
    exc_message     TEXT,
    exc_fingerprint TEXT,
    has_profile     INTEGER NOT NULL DEFAULT 0,
    response_size   INTEGER NOT NULL DEFAULT 0,
    response_ctype  TEXT,
    is_replay       INTEGER NOT NULL DEFAULT 0,
    replayed_from   TEXT,
    payload_json    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lens_req_ts ON lens_requests (seq DESC);
CREATE INDEX IF NOT EXISTS idx_lens_req_route ON lens_requests (method, rule, path);
CREATE INDEX IF NOT EXISTS idx_lens_req_status ON lens_requests (status_code);
CREATE INDEX IF NOT EXISTS idx_lens_req_flags ON lens_requests (has_n_plus_one, has_slow_sql, has_exception);

CREATE TABLE IF NOT EXISTS lens_sql_queries (
    id              TEXT PRIMARY KEY,
    request_id      TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    normalized_sql  TEXT NOT NULL,
    operation       TEXT NOT NULL,
    tables_json     TEXT NOT NULL,
    duration_ms     REAL NOT NULL,
    is_slow         INTEGER NOT NULL DEFAULT 0,
    is_n_plus_one   INTEGER NOT NULL DEFAULT 0,
    call_site       TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (request_id) REFERENCES lens_requests(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_lens_sql_req ON lens_sql_queries (request_id);
CREATE INDEX IF NOT EXISTS idx_lens_sql_fp ON lens_sql_queries (fingerprint);
"""


class SQLiteStorage:
    """Persistent embedded SQLite WAL ring-buffer store.

    Uses a single ``check_same_thread=False`` connection guarded by an
    :class:`threading.RLock`, ensuring thread-safety for both file-backed WAL
    databases and ``:memory:`` databases.
    """

    def __init__(self, db_path: str = ".flask_lens.sqlite3", max_requests: int = 1000) -> None:
        self._db_path = db_path
        self._max_requests = max(1, max_requests)
        self._lock = threading.RLock()

        if db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)

        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON;")
            if db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL;")
                self._conn.execute("PRAGMA synchronous = NORMAL;")
            self._conn.execute("PRAGMA busy_timeout = 5000;")
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()

    def save(self, req: CapturedRequest) -> None:
        payload = json.dumps(req.to_dict(), ensure_ascii=False, default=str)
        exc = req.exception
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO lens_requests (
                    id, timestamp, method, path, full_path, endpoint, rule,
                    status_code, duration_ms, sql_count, sql_duration_ms,
                    has_n_plus_one, n_plus_one_cnt, has_slow_sql, dup_sql_count,
                    outgoing_count, log_count, has_exception, exc_type, exc_message,
                    exc_fingerprint, has_profile, response_size, response_ctype,
                    is_replay, replayed_from, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    req.id,
                    req.timestamp,
                    req.method,
                    req.path,
                    req.full_path,
                    req.endpoint,
                    req.rule,
                    req.status_code,
                    round(req.duration_ms, 3),
                    req.sql_count,
                    round(req.sql_duration_ms, 3),
                    int(req.has_n_plus_one),
                    len(req.n_plus_one_groups),
                    int(req.has_slow_sql),
                    req.duplicate_sql_count,
                    len(req.outgoing_http),
                    len(req.logs),
                    int(exc is not None),
                    exc.exc_type if exc else None,
                    exc.message if exc else None,
                    exc.fingerprint if exc else None,
                    int(req.profile is not None),
                    req.response_size,
                    req.response_content_type,
                    int(req.is_replay),
                    req.replayed_from,
                    payload,
                ),
            )

            if req.sql_queries:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO lens_sql_queries (
                        id, request_id, fingerprint, normalized_sql, operation,
                        tables_json, duration_ms, is_slow, is_n_plus_one, call_site
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            q.id,
                            req.id,
                            q.fingerprint,
                            q.normalized_sql,
                            q.operation,
                            json.dumps(q.tables),
                            round(q.duration_ms, 3),
                            int(q.is_slow),
                            int(q.is_n_plus_one),
                            q.call_site,
                        )
                        for q in req.sql_queries
                    ],
                )

            # Enforce ring-buffer capacity
            cur.execute(
                """
                DELETE FROM lens_requests
                WHERE seq NOT IN (
                    SELECT seq FROM lens_requests ORDER BY seq DESC LIMIT ?
                )
                """,
                (self._max_requests,),
            )
            self._conn.commit()

    def get(self, request_id: str) -> CapturedRequest | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM lens_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return CapturedRequest.from_dict(json.loads(row["payload_json"]))

    def list_requests(self, flt: RequestFilter | None = None) -> tuple[list[dict[str, Any]], int]:
        flt = flt or RequestFilter()
        where_clauses: list[str] = []
        params: list[Any] = []

        if flt.method:
            where_clauses.append("method = ?")
            params.append(flt.method.upper())

        if flt.status_group == "2xx":
            where_clauses.append("status_code >= 200 AND status_code < 300")
        elif flt.status_group == "3xx":
            where_clauses.append("status_code >= 300 AND status_code < 400")
        elif flt.status_group == "4xx":
            where_clauses.append("status_code >= 400 AND status_code < 500")
        elif flt.status_group == "5xx":
            where_clauses.append("status_code >= 500")

        if flt.only_n_plus_one:
            where_clauses.append("has_n_plus_one = 1")
        if flt.only_slow:
            where_clauses.append("(has_slow_sql = 1 OR duration_ms >= 200.0)")
        if flt.only_errors:
            where_clauses.append("(status_code >= 400 OR has_exception = 1)")
        if flt.min_duration_ms is not None:
            where_clauses.append("duration_ms >= ?")
            params.append(flt.min_duration_ms)
        if flt.search:
            escaped = flt.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            where_clauses.append(
                """
                (
                    path LIKE ? ESCAPE '\\' OR full_path LIKE ? ESCAPE '\\'
                    OR COALESCE(endpoint, '') LIKE ? ESCAPE '\\'
                    OR COALESCE(rule, '') LIKE ? ESCAPE '\\'
                    OR COALESCE(exc_type, '') LIKE ? ESCAPE '\\'
                    OR COALESCE(exc_message, '') LIKE ? ESCAPE '\\'
                    OR EXISTS (
                        SELECT 1 FROM lens_sql_queries q
                        WHERE q.request_id = lens_requests.id
                          AND q.normalized_sql LIKE ? ESCAPE '\\'
                    )
                )
                """
            )
            params.extend([like] * 7)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        with self._lock:
            total_row = self._conn.execute(
                f"SELECT COUNT(*) AS cnt FROM lens_requests {where_sql}",
                params,
            ).fetchone()
            total = int(total_row["cnt"]) if total_row else 0

            rows = self._conn.execute(
                f"""
                SELECT
                    id, timestamp, method, path, full_path, endpoint, rule,
                    status_code, duration_ms, sql_count, sql_duration_ms,
                    has_n_plus_one, n_plus_one_cnt, has_slow_sql, dup_sql_count,
                    outgoing_count, log_count, has_exception, exc_type, has_profile,
                    response_size, response_ctype, is_replay, replayed_from
                FROM lens_requests
                {where_sql}
                ORDER BY seq DESC
                LIMIT ? OFFSET ?
                """,
                [*params, flt.limit, flt.offset],
            ).fetchall()

        summaries = [
            {
                "id": row["id"],
                "timestamp": row["timestamp"],
                "method": row["method"],
                "path": row["path"],
                "full_path": row["full_path"],
                "endpoint": row["endpoint"],
                "rule": row["rule"],
                "status_code": row["status_code"],
                "duration_ms": round(float(row["duration_ms"]), 3),
                "sql_count": row["sql_count"],
                "sql_duration_ms": round(float(row["sql_duration_ms"]), 3),
                "has_n_plus_one": bool(row["has_n_plus_one"]),
                "n_plus_one_count": row["n_plus_one_cnt"],
                "has_slow_sql": bool(row["has_slow_sql"]),
                "duplicate_sql_count": row["dup_sql_count"],
                "outgoing_http_count": row["outgoing_count"],
                "log_count": row["log_count"],
                "has_exception": bool(row["has_exception"]),
                "exception_type": row["exc_type"],
                "has_profile": bool(row["has_profile"]),
                "response_size": row["response_size"],
                "response_content_type": row["response_ctype"],
                "is_replay": bool(row["is_replay"]),
                "replayed_from": row["replayed_from"],
            }
            for row in rows
        ]
        return summaries, total

    def overview_stats(self) -> dict[str, Any]:
        with self._lock:
            agg = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(AVG(duration_ms), 0.0) AS avg_ms,
                    COALESCE(SUM(CASE WHEN status_code >= 500 OR has_exception = 1 THEN 1 ELSE 0 END), 0) AS errors,
                    COALESCE(SUM(sql_count), 0) AS total_sql,
                    COALESCE(SUM(has_n_plus_one), 0) AS n1_reqs,
                    COALESCE(SUM(has_slow_sql), 0) AS slow_reqs,
                    COALESCE(SUM(has_exception), 0) AS exc_count
                FROM lens_requests
                """
            ).fetchone()
            durations = [
                float(r["duration_ms"])
                for r in self._conn.execute("SELECT duration_ms FROM lens_requests").fetchall()
            ]
            recent = self._conn.execute(
                """
                SELECT id, path, method, duration_ms, sql_count, status_code, has_n_plus_one
                FROM lens_requests
                ORDER BY seq DESC
                LIMIT 40
                """
            ).fetchall()

        total = int(agg["total"]) if agg else 0
        if total == 0:
            return {
                "total_requests": 0,
                "avg_duration_ms": 0.0,
                "p95_duration_ms": 0.0,
                "error_count": 0,
                "error_rate": 0.0,
                "total_sql_queries": 0,
                "n_plus_one_requests": 0,
                "slow_sql_requests": 0,
                "exception_count": 0,
                "latency_series": [],
            }

        errors = int(agg["errors"])
        series = [
            {
                "id": r["id"],
                "path": r["path"],
                "method": r["method"],
                "duration_ms": round(float(r["duration_ms"]), 2),
                "sql_count": int(r["sql_count"]),
                "status_code": int(r["status_code"]),
                "has_n_plus_one": bool(r["has_n_plus_one"]),
            }
            for r in reversed(recent)
        ]

        return {
            "total_requests": total,
            "avg_duration_ms": round(float(agg["avg_ms"]), 2),
            "p95_duration_ms": round(compute_percentile(durations, 95), 2),
            "error_count": errors,
            "error_rate": round((errors / total) * 100.0, 2),
            "total_sql_queries": int(agg["total_sql"]),
            "n_plus_one_requests": int(agg["n1_reqs"]),
            "slow_sql_requests": int(agg["slow_reqs"]),
            "exception_count": int(agg["exc_count"]),
            "latency_series": series,
        }

    def endpoint_stats(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, method, COALESCE(rule, path) AS route, endpoint,
                       duration_ms, sql_count, has_n_plus_one, status_code, has_exception
                FROM lens_requests
                ORDER BY seq ASC
                """
            ).fetchall()

        groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        for r in rows:
            groups[(str(r["method"]), str(r["route"]))].append(r)

        result: list[dict[str, Any]] = []
        for (method, route), bucket in groups.items():
            durations = [float(r["duration_ms"]) for r in bucket]
            sql_counts = [int(r["sql_count"]) for r in bucket]
            errors = sum(
                1 for r in bucket if int(r["status_code"]) >= 500 or bool(r["has_exception"])
            )
            n1_hits = sum(1 for r in bucket if bool(r["has_n_plus_one"]))
            result.append(
                {
                    "method": method,
                    "route": route,
                    "endpoint": bucket[-1]["endpoint"],
                    "count": len(bucket),
                    "avg_ms": round(sum(durations) / len(durations), 2),
                    "p50_ms": round(compute_percentile(durations, 50), 2),
                    "p95_ms": round(compute_percentile(durations, 95), 2),
                    "p99_ms": round(compute_percentile(durations, 99), 2),
                    "max_ms": round(max(durations), 2),
                    "avg_sql": round(sum(sql_counts) / len(sql_counts), 2),
                    "max_sql": max(sql_counts),
                    "n_plus_one_hits": n1_hits,
                    "error_count": errors,
                    "error_rate": round((errors / len(bucket)) * 100.0, 2),
                    "latest_request_id": bucket[-1]["id"],
                }
            )
        result.sort(key=lambda item: (-item["p95_ms"], -item["count"]))
        return result

    def sql_stats(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    q.fingerprint,
                    MAX(q.normalized_sql) AS normalized_sql,
                    MAX(q.operation) AS operation,
                    MAX(q.tables_json) AS tables_json,
                    COUNT(*) AS calls,
                    SUM(q.duration_ms) AS total_ms,
                    AVG(q.duration_ms) AS avg_ms,
                    MAX(q.duration_ms) AS max_ms,
                    SUM(q.is_slow) AS slow_hits,
                    SUM(q.is_n_plus_one) AS n_plus_one_hits,
                    (
                        SELECT q2.request_id
                        FROM lens_sql_queries q2
                        JOIN lens_requests r2 ON r2.id = q2.request_id
                        WHERE q2.fingerprint = q.fingerprint
                        ORDER BY r2.seq DESC
                        LIMIT 1
                    ) AS sample_request_id,
                    (
                        SELECT q2.call_site
                        FROM lens_sql_queries q2
                        JOIN lens_requests r2 ON r2.id = q2.request_id
                        WHERE q2.fingerprint = q.fingerprint
                        ORDER BY r2.seq DESC
                        LIMIT 1
                    ) AS sample_call_site
                FROM lens_sql_queries q
                GROUP BY q.fingerprint
                ORDER BY total_ms DESC, calls DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "fingerprint": row["fingerprint"],
                "normalized_sql": row["normalized_sql"],
                "operation": row["operation"],
                "tables": json.loads(row["tables_json"] or "[]"),
                "calls": int(row["calls"]),
                "total_ms": round(float(row["total_ms"]), 3),
                "avg_ms": round(float(row["avg_ms"]), 3),
                "max_ms": round(float(row["max_ms"]), 3),
                "slow_hits": int(row["slow_hits"]),
                "n_plus_one_hits": int(row["n_plus_one_hits"]),
                "sample_request_id": row["sample_request_id"],
                "sample_call_site": row["sample_call_site"],
            }
            for row in rows
        ]

    def exception_groups(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    COALESCE(exc_fingerprint, exc_type) AS fp,
                    exc_type,
                    exc_message,
                    timestamp,
                    id,
                    path,
                    method
                FROM lens_requests
                WHERE has_exception = 1
                ORDER BY seq ASC
                """
            ).fetchall()

        groups: dict[str, dict[str, Any]] = {}
        for r in rows:
            fp = str(r["fp"])
            g = groups.get(fp)
            if g is None:
                g = {
                    "fingerprint": fp,
                    "exc_type": r["exc_type"],
                    "message": r["exc_message"],
                    "count": 0,
                    "first_seen": float(r["timestamp"]),
                    "last_seen": float(r["timestamp"]),
                    "latest_request_id": r["id"],
                    "latest_path": r["path"],
                    "latest_method": r["method"],
                }
                groups[fp] = g
            g["count"] += 1
            g["last_seen"] = max(g["last_seen"], float(r["timestamp"]))
            g["first_seen"] = min(g["first_seen"], float(r["timestamp"]))
            g["latest_request_id"] = r["id"]
            g["latest_path"] = r["path"]
            g["latest_method"] = r["method"]
            g["message"] = r["exc_message"]

        result = list(groups.values())
        result.sort(key=lambda item: (-item["last_seen"], -item["count"]))
        return result

    def clear(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS cnt FROM lens_requests").fetchone()
            count = int(row["cnt"]) if row else 0
            self._conn.execute("DELETE FROM lens_sql_queries")
            self._conn.execute("DELETE FROM lens_requests")
            self._conn.commit()
            return count

    def close(self) -> None:
        with self._lock:
            self._conn.close()
