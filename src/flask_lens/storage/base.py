"""Storage backend protocol and shared analytics / diff calculations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

from flask_lens.models import CapturedRequest


@dataclass(frozen=True)
class RequestFilter:
    """Query parameters for filtering stored requests."""

    limit: int = 100
    offset: int = 0
    method: str | None = None
    status_group: str | None = None  # "2xx", "3xx", "4xx", "5xx"
    search: str | None = None
    only_n_plus_one: bool = False
    only_slow: bool = False
    only_errors: bool = False
    min_duration_ms: float | None = None


class StorageBackend(Protocol):
    """Interface implemented by all flask-lens storage backends."""

    def save(self, req: CapturedRequest) -> None: ...

    def get(self, request_id: str) -> CapturedRequest | None: ...

    def list_requests(
        self, flt: RequestFilter | None = None
    ) -> tuple[list[dict[str, Any]], int]: ...

    def overview_stats(self) -> dict[str, Any]: ...

    def endpoint_stats(self) -> list[dict[str, Any]]: ...

    def sql_stats(self, limit: int = 100) -> list[dict[str, Any]]: ...

    def exception_groups(self) -> list[dict[str, Any]]: ...

    def clear(self) -> int: ...

    def close(self) -> None: ...


def compute_percentile(values: list[float], percentile: float) -> float:
    """Linear-interpolated percentile calculation (0..100)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (percentile / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return round(ordered[lo], 3)
    weight = rank - lo
    return round(ordered[lo] * (1.0 - weight) + ordered[hi] * weight, 3)


def compute_request_diff(baseline: CapturedRequest, candidate: CapturedRequest) -> dict[str, Any]:
    """Compute a structured before/after comparison between two captured requests."""
    duration_delta_ms = round(candidate.duration_ms - baseline.duration_ms, 3)
    duration_delta_pct = (
        round(((candidate.duration_ms - baseline.duration_ms) / baseline.duration_ms) * 100.0, 2)
        if baseline.duration_ms > 0
        else 0.0
    )
    sql_count_delta = candidate.sql_count - baseline.sql_count
    sql_ms_delta = round(candidate.sql_duration_ms - baseline.sql_duration_ms, 3)

    base_fps: dict[str, dict[str, Any]] = {}
    for q in baseline.sql_queries:
        bucket = base_fps.setdefault(
            q.fingerprint,
            {
                "fingerprint": q.fingerprint,
                "normalized_sql": q.normalized_sql,
                "count": 0,
                "total_ms": 0.0,
            },
        )
        bucket["count"] += 1
        bucket["total_ms"] = round(bucket["total_ms"] + q.duration_ms, 3)

    cand_fps: dict[str, dict[str, Any]] = {}
    for q in candidate.sql_queries:
        bucket = cand_fps.setdefault(
            q.fingerprint,
            {
                "fingerprint": q.fingerprint,
                "normalized_sql": q.normalized_sql,
                "count": 0,
                "total_ms": 0.0,
            },
        )
        bucket["count"] += 1
        bucket["total_ms"] = round(bucket["total_ms"] + q.duration_ms, 3)

    all_fps = set(base_fps.keys()) | set(cand_fps.keys())
    query_diffs: list[dict[str, Any]] = []
    for fp in all_fps:
        b = base_fps.get(fp)
        c = cand_fps.get(fp)
        b_cnt = b["count"] if b else 0
        c_cnt = c["count"] if c else 0
        b_ms = b["total_ms"] if b else 0.0
        c_ms = c["total_ms"] if c else 0.0
        sql_text = (c or b or {}).get("normalized_sql", "")
        status = (
            "added"
            if b_cnt == 0
            else "removed"
            if c_cnt == 0
            else "changed"
            if b_cnt != c_cnt
            else "unchanged"
        )
        query_diffs.append(
            {
                "fingerprint": fp,
                "normalized_sql": sql_text,
                "status": status,
                "baseline_count": b_cnt,
                "candidate_count": c_cnt,
                "delta_count": c_cnt - b_cnt,
                "baseline_ms": b_ms,
                "candidate_ms": c_ms,
                "delta_ms": round(c_ms - b_ms, 3),
            }
        )
    query_diffs.sort(key=lambda item: (-abs(item["delta_count"]), -abs(item["delta_ms"])))

    return {
        "baseline": baseline.summary_dict(),
        "candidate": candidate.summary_dict(),
        "deltas": {
            "duration_ms": duration_delta_ms,
            "duration_pct": duration_delta_pct,
            "sql_count": sql_count_delta,
            "sql_duration_ms": sql_ms_delta,
            "n_plus_one_groups": len(candidate.n_plus_one_groups) - len(baseline.n_plus_one_groups),
            "n_plus_one_resolved": baseline.has_n_plus_one and not candidate.has_n_plus_one,
            "n_plus_one_introduced": not baseline.has_n_plus_one and candidate.has_n_plus_one,
            "status_changed": baseline.status_code != candidate.status_code,
        },
        "query_diffs": query_diffs,
    }
