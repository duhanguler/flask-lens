"""Thread-safe in-memory ring-buffer storage backend."""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from typing import Any

from flask_lens.models import CapturedRequest
from flask_lens.storage.base import RequestFilter, compute_percentile


class MemoryStorage:
    """Ephemeral in-memory ring-buffer store ideal for tests and serverless/read-only disks."""

    def __init__(self, max_requests: int = 1000) -> None:
        self._max_requests = max(1, max_requests)
        self._lock = threading.RLock()
        self._order: deque[str] = deque()
        self._items: dict[str, CapturedRequest] = {}

    def save(self, req: CapturedRequest) -> None:
        with self._lock:
            if req.id in self._items:
                self._items[req.id] = req
                return
            self._items[req.id] = req
            self._order.append(req.id)
            while len(self._order) > self._max_requests:
                oldest_id = self._order.popleft()
                self._items.pop(oldest_id, None)

    def get(self, request_id: str) -> CapturedRequest | None:
        with self._lock:
            return self._items.get(request_id)

    def list_requests(self, flt: RequestFilter | None = None) -> tuple[list[dict[str, Any]], int]:
        flt = flt or RequestFilter()
        with self._lock:
            ordered = [self._items[rid] for rid in reversed(self._order) if rid in self._items]

        matched = [req for req in ordered if _matches(req, flt)]
        total = len(matched)
        page = matched[flt.offset : flt.offset + flt.limit]
        return [req.summary_dict() for req in page], total

    def overview_stats(self) -> dict[str, Any]:
        with self._lock:
            items = [self._items[rid] for rid in self._order if rid in self._items]

        total = len(items)
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

        durations = [r.duration_ms for r in items]
        errors = sum(1 for r in items if r.status_code >= 500 or r.exception is not None)
        n1_count = sum(1 for r in items if r.has_n_plus_one)
        slow_count = sum(1 for r in items if r.has_slow_sql)
        exc_count = sum(1 for r in items if r.exception is not None)
        total_sql = sum(r.sql_count for r in items)

        recent = items[-40:]
        series = [
            {
                "id": r.id,
                "path": r.path,
                "method": r.method,
                "duration_ms": round(r.duration_ms, 2),
                "sql_count": r.sql_count,
                "status_code": r.status_code,
                "has_n_plus_one": r.has_n_plus_one,
            }
            for r in recent
        ]

        return {
            "total_requests": total,
            "avg_duration_ms": round(sum(durations) / total, 2),
            "p95_duration_ms": round(compute_percentile(durations, 95), 2),
            "error_count": errors,
            "error_rate": round((errors / total) * 100.0, 2),
            "total_sql_queries": total_sql,
            "n_plus_one_requests": n1_count,
            "slow_sql_requests": slow_count,
            "exception_count": exc_count,
            "latency_series": series,
        }

    def endpoint_stats(self) -> list[dict[str, Any]]:
        with self._lock:
            items = [self._items[rid] for rid in self._order if rid in self._items]

        groups: dict[tuple[str, str], list[CapturedRequest]] = defaultdict(list)
        for req in items:
            key = (req.method, req.rule or req.path)
            groups[key].append(req)

        rows: list[dict[str, Any]] = []
        for (method, route), bucket in groups.items():
            durations = [r.duration_ms for r in bucket]
            sql_counts = [r.sql_count for r in bucket]
            errors = sum(1 for r in bucket if r.status_code >= 500 or r.exception is not None)
            n1_hits = sum(1 for r in bucket if r.has_n_plus_one)
            rows.append(
                {
                    "method": method,
                    "route": route,
                    "endpoint": bucket[-1].endpoint,
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
                    "latest_request_id": bucket[-1].id,
                }
            )
        rows.sort(key=lambda r: (-r["p95_ms"], -r["count"]))
        return rows

    def sql_stats(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            items = [self._items[rid] for rid in self._order if rid in self._items]

        buckets: dict[str, dict[str, Any]] = {}
        for req in items:
            for q in req.sql_queries:
                b = buckets.get(q.fingerprint)
                if b is None:
                    b = {
                        "fingerprint": q.fingerprint,
                        "normalized_sql": q.normalized_sql,
                        "operation": q.operation,
                        "tables": q.tables,
                        "calls": 0,
                        "total_ms": 0.0,
                        "max_ms": 0.0,
                        "slow_hits": 0,
                        "n_plus_one_hits": 0,
                        "sample_request_id": req.id,
                        "sample_call_site": q.call_site,
                    }
                    buckets[q.fingerprint] = b
                b["calls"] += 1
                b["total_ms"] += q.duration_ms
                if q.duration_ms > b["max_ms"]:
                    b["max_ms"] = q.duration_ms
                if q.is_slow:
                    b["slow_hits"] += 1
                if q.is_n_plus_one:
                    b["n_plus_one_hits"] += 1
                if q.call_site:
                    b["sample_call_site"] = q.call_site
                b["sample_request_id"] = req.id

        rows = []
        for b in buckets.values():
            b["avg_ms"] = round(b["total_ms"] / b["calls"], 3)
            b["total_ms"] = round(b["total_ms"], 3)
            b["max_ms"] = round(b["max_ms"], 3)
            rows.append(b)
        rows.sort(key=lambda r: (-r["total_ms"], -r["calls"]))
        return rows[:limit]

    def exception_groups(self) -> list[dict[str, Any]]:
        with self._lock:
            items = [self._items[rid] for rid in self._order if rid in self._items]

        groups: dict[str, dict[str, Any]] = {}
        for req in items:
            if req.exception is None:
                continue
            fp = req.exception.fingerprint or req.exception.exc_type
            g = groups.get(fp)
            if g is None:
                g = {
                    "fingerprint": fp,
                    "exc_type": req.exception.exc_type,
                    "message": req.exception.message,
                    "count": 0,
                    "first_seen": req.timestamp,
                    "last_seen": req.timestamp,
                    "latest_request_id": req.id,
                    "latest_path": req.path,
                    "latest_method": req.method,
                }
                groups[fp] = g
            g["count"] += 1
            g["last_seen"] = max(g["last_seen"], req.timestamp)
            g["first_seen"] = min(g["first_seen"], req.timestamp)
            g["latest_request_id"] = req.id
            g["latest_path"] = req.path
            g["latest_method"] = req.method
            g["message"] = req.exception.message

        rows = list(groups.values())
        rows.sort(key=lambda r: (-r["last_seen"], -r["count"]))
        return rows

    def clear(self) -> int:
        with self._lock:
            count = len(self._items)
            self._items.clear()
            self._order.clear()
            return count

    def close(self) -> None:
        return None


def _matches(req: CapturedRequest, flt: RequestFilter) -> bool:
    if flt.method and req.method.upper() != flt.method.upper():
        return False
    if flt.status_group:
        if flt.status_group == "2xx" and not (200 <= req.status_code < 300):
            return False
        if flt.status_group == "3xx" and not (300 <= req.status_code < 400):
            return False
        if flt.status_group == "4xx" and not (400 <= req.status_code < 500):
            return False
        if flt.status_group == "5xx" and req.status_code < 500:
            return False
    if flt.only_n_plus_one and not req.has_n_plus_one:
        return False
    if flt.only_slow and not (req.has_slow_sql or req.duration_ms >= 200.0):
        return False
    if flt.only_errors and not (req.status_code >= 400 or req.exception is not None):
        return False
    if flt.min_duration_ms is not None and req.duration_ms < flt.min_duration_ms:
        return False
    if flt.search:
        needle = flt.search.lower()
        haystack = " ".join(
            [
                req.path,
                req.full_path,
                req.endpoint or "",
                req.rule or "",
                req.exception.exc_type if req.exception else "",
                req.exception.message if req.exception else "",
            ]
        ).lower()
        if needle not in haystack and not any(needle in q.sql.lower() for q in req.sql_queries):
            return False
    return True
