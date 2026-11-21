"""Parity tests for MemoryStorage and SQLiteStorage backends plus request diffing."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from flask_lens.models import CapturedRequest, ExceptionInfo, NPlusOneGroup, SQLQuery
from flask_lens.storage.base import RequestFilter, StorageBackend, compute_request_diff
from flask_lens.storage.memory import MemoryStorage
from flask_lens.storage.sqlite import SQLiteStorage


@pytest.fixture(params=["memory", "sqlite_memory", "sqlite_file"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StorageBackend]:
    kind = request.param
    backend: StorageBackend
    if kind == "memory":
        backend = MemoryStorage(max_requests=5)
    elif kind == "sqlite_memory":
        backend = SQLiteStorage(db_path=":memory:", max_requests=5)
    else:
        backend = SQLiteStorage(db_path=str(tmp_path / "lens.sqlite3"), max_requests=5)
    try:
        yield backend
    finally:
        backend.close()


def _sample_request(
    rid: str,
    *,
    method: str = "GET",
    path: str = "/api/authors",
    status: int = 200,
    duration_ms: float = 12.5,
    n_plus_one: bool = False,
    slow: bool = False,
    exc_type: str | None = None,
    sql_count: int = 2,
) -> CapturedRequest:
    queries = [
        SQLQuery(
            id=f"{rid}-q{i}",
            sql=f"SELECT * FROM books WHERE author_id = {i}",
            normalized_sql="SELECT * FROM books WHERE author_id = ?",
            fingerprint="fp_books_by_author",
            operation="SELECT",
            tables=["books"],
            params=[i],
            duration_ms=60.0 if slow and i == 0 else 1.5,
            offset_ms=float(i),
            is_slow=slow and i == 0,
            is_n_plus_one=n_plus_one,
            call_site="app.py:42 in list_authors",
        )
        for i in range(sql_count)
    ]
    groups = (
        [
            NPlusOneGroup(
                fingerprint="fp_books_by_author",
                normalized_sql="SELECT * FROM books WHERE author_id = ?",
                count=sql_count,
                total_ms=sum(q.duration_ms for q in queries),
                call_site="app.py:42 in list_authors",
                query_ids=[q.id for q in queries],
                suggestion="Use selectinload",
            )
        ]
        if n_plus_one
        else []
    )
    exc = (
        ExceptionInfo(
            exc_type=exc_type,
            message="boom",
            traceback_text="Traceback...",
            fingerprint=f"fp_{exc_type}",
        )
        if exc_type
        else None
    )
    return CapturedRequest(
        id=rid,
        timestamp=1700000000.0 + int(rid.lstrip("r")),
        method=method,
        path=path,
        full_path=path,
        query_string="",
        endpoint="ep",
        rule=path,
        status_code=status,
        duration_ms=duration_ms,
        remote_addr="127.0.0.1",
        sql_queries=queries,
        n_plus_one_groups=groups,
        sql_count=len(queries),
        sql_duration_ms=sum(q.duration_ms for q in queries),
        has_n_plus_one=n_plus_one,
        has_slow_sql=slow,
        exception=exc,
    )


def test_save_get_ring_buffer_and_filters(storage: StorageBackend) -> None:
    # Insert 7 requests into a ring buffer of size 5 -> r1 and r2 should be evicted
    storage.save(_sample_request("r1", path="/old-1"))
    storage.save(_sample_request("r2", path="/old-2"))
    storage.save(
        _sample_request(
            "r3", method="GET", path="/api/authors", duration_ms=25.0, n_plus_one=True, sql_count=5
        )
    )
    storage.save(
        _sample_request(
            "r4", method="POST", path="/api/orders", status=201, duration_ms=10.0, sql_count=1
        )
    )
    storage.save(
        _sample_request(
            "r5", method="GET", path="/api/slow", duration_ms=240.0, slow=True, sql_count=1
        )
    )
    storage.save(
        _sample_request(
            "r6",
            method="GET",
            path="/api/boom",
            status=500,
            duration_ms=8.0,
            exc_type="ZeroDivisionError",
            sql_count=0,
        )
    )
    storage.save(
        _sample_request("r7", method="GET", path="/api/authors", duration_ms=5.0, sql_count=2)
    )

    assert storage.get("r1") is None
    assert storage.get("r2") is None
    fetched = storage.get("r3")
    assert fetched is not None and fetched.has_n_plus_one is True

    all_items, total = storage.list_requests(RequestFilter(limit=20))
    assert total == 5
    assert [it["id"] for it in all_items] == ["r7", "r6", "r5", "r4", "r3"]

    n1_items, n1_total = storage.list_requests(RequestFilter(only_n_plus_one=True))
    assert n1_total == 1 and n1_items[0]["id"] == "r3"

    err_items, err_total = storage.list_requests(RequestFilter(only_errors=True))
    assert err_total == 1 and err_items[0]["id"] == "r6"

    post_items, post_total = storage.list_requests(RequestFilter(method="POST"))
    assert post_total == 1 and post_items[0]["id"] == "r4"

    search_items, _ = storage.list_requests(RequestFilter(search="orders"))
    assert len(search_items) == 1 and search_items[0]["id"] == "r4"

    # Analytics
    overview = storage.overview_stats()
    assert overview["total_requests"] == 5
    assert overview["n_plus_one_requests"] == 1
    assert overview["exception_count"] == 1
    assert len(overview["latency_series"]) == 5

    endpoints = storage.endpoint_stats()
    authors_ep = next(ep for ep in endpoints if ep["route"] == "/api/authors")
    assert authors_ep["count"] == 2
    assert authors_ep["n_plus_one_hits"] == 1

    sql_rows = storage.sql_stats()
    assert len(sql_rows) == 1
    assert sql_rows[0]["calls"] == 9  # 5 + 1 + 1 + 0 + 2

    exc_groups = storage.exception_groups()
    assert len(exc_groups) == 1
    assert exc_groups[0]["exc_type"] == "ZeroDivisionError"

    cleared = storage.clear()
    assert cleared == 5
    assert storage.overview_stats()["total_requests"] == 0


def test_compute_request_diff() -> None:
    before = _sample_request("r1", duration_ms=40.0, n_plus_one=True, sql_count=10)
    after = _sample_request("r2", duration_ms=8.0, n_plus_one=False, sql_count=2)
    diff = compute_request_diff(before, after)

    assert diff["deltas"]["sql_count"] == -8
    assert diff["deltas"]["duration_ms"] == -32.0
    assert diff["deltas"]["duration_pct"] == -80.0
    assert diff["deltas"]["n_plus_one_resolved"] is True
    assert diff["deltas"]["n_plus_one_introduced"] is False
    assert diff["query_diffs"][0]["delta_count"] == -8
