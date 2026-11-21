"""SQL analysis engine: N+1 detection, exact duplicate detection, and slow query tagging."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass

from flask_lens.models import NPlusOneGroup, SQLQuery


@dataclass
class SQLAnalysisSummary:
    """Aggregate findings for all SQL queries within a single request."""

    sql_count: int
    sql_duration_ms: float
    n_plus_one_groups: list[NPlusOneGroup]
    duplicate_count: int
    has_slow_sql: bool


def analyze_queries(
    queries: list[SQLQuery],
    *,
    slow_query_ms: float = 50.0,
    n_plus_one_threshold: int = 3,
) -> SQLAnalysisSummary:
    """Annotate *queries* in-place with slow, duplicate, and N+1 flags."""
    if not queries:
        return SQLAnalysisSummary(
            sql_count=0,
            sql_duration_ms=0.0,
            n_plus_one_groups=[],
            duplicate_count=0,
            has_slow_sql=False,
        )

    total_ms = 0.0
    has_slow = False

    exact_buckets: dict[tuple[str, str], list[SQLQuery]] = defaultdict(list)
    select_buckets: dict[tuple[str, str], list[SQLQuery]] = defaultdict(list)
    by_fingerprint: dict[str, list[SQLQuery]] = defaultdict(list)

    for query in queries:
        total_ms += query.duration_ms
        if query.duration_ms >= slow_query_ms:
            query.is_slow = True
            has_slow = True
        else:
            query.is_slow = False

        query.is_duplicate = False
        query.is_n_plus_one = False
        query.n_plus_one_group = None

        exact_key = (query.sql.strip(), _stable_params_key(query.params))
        exact_buckets[exact_key].append(query)

        if query.operation == "SELECT":
            select_buckets[(query.fingerprint, query.call_site)].append(query)
            by_fingerprint[query.fingerprint].append(query)

    duplicate_excess = 0
    for bucket in exact_buckets.values():
        if len(bucket) > 1:
            duplicate_excess += len(bucket) - 1
            for q in bucket:
                q.is_duplicate = True

    n_plus_one_groups: list[NPlusOneGroup] = []
    seen_fingerprints: set[str] = set()

    for (fp, call_site), bucket in select_buckets.items():
        if len(bucket) >= n_plus_one_threshold:
            seen_fingerprints.add(fp)
            group = _build_n_plus_one_group(fp, call_site, bucket)
            n_plus_one_groups.append(group)
            for q in bucket:
                q.is_n_plus_one = True
                q.n_plus_one_group = fp

    for fp, bucket in by_fingerprint.items():
        if fp in seen_fingerprints:
            continue
        if len(bucket) >= n_plus_one_threshold:
            primary_site = bucket[0].call_site
            group = _build_n_plus_one_group(fp, primary_site, bucket)
            n_plus_one_groups.append(group)
            for q in bucket:
                q.is_n_plus_one = True
                q.n_plus_one_group = fp

    n_plus_one_groups.sort(key=lambda g: (-g.count, -g.total_ms))

    return SQLAnalysisSummary(
        sql_count=len(queries),
        sql_duration_ms=round(total_ms, 3),
        n_plus_one_groups=n_plus_one_groups,
        duplicate_count=duplicate_excess,
        has_slow_sql=has_slow,
    )


def _build_n_plus_one_group(
    fingerprint: str,
    call_site: str,
    bucket: list[SQLQuery],
) -> NPlusOneGroup:
    first = bucket[0]
    tables_hint = ", ".join(first.tables[:2]) if first.tables else "related rows"
    suggestion = (
        f"Executed {len(bucket)} similar SELECT queries against {tables_hint}. "
        "Consider eager loading with SQLAlchemy `selectinload(...)` / `joinedload(...)` "
        "or batching into a single `IN (...)` query."
    )
    return NPlusOneGroup(
        fingerprint=fingerprint,
        normalized_sql=first.normalized_sql,
        count=len(bucket),
        total_ms=round(sum(q.duration_ms for q in bucket), 3),
        call_site=call_site,
        query_ids=[q.id for q in bucket],
        suggestion=suggestion,
    )


def _stable_params_key(params: object) -> str:
    try:
        return json.dumps(params, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(params)
