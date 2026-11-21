"""Django-style ``assertNumQueries`` and N+1 SQL guardrails for Flask & SQLAlchemy tests."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from flask_lens.models import NPlusOneGroup, SQLQuery
from flask_lens.sql.analyzer import analyze_queries
from flask_lens.sql.listener import (
    attach_engine,
    register_query_sink,
    resolve_engines,
    unregister_query_sink,
)


class QueryAssertionError(AssertionError):
    """Raised when a query count or N+1 SQL budget assertion fails."""


@dataclass
class QueryCapture:
    """Result object yielded by :func:`capture_queries`."""

    queries: list[SQLQuery] = field(default_factory=list)
    n_plus_one_groups: list[NPlusOneGroup] = field(default_factory=list)
    duplicate_count: int = 0
    total_ms: float = 0.0
    has_slow_sql: bool = False

    @property
    def count(self) -> int:
        return len(self.queries)

    @property
    def duplicates(self) -> list[SQLQuery]:
        return [q for q in self.queries if q.is_duplicate]

    @property
    def slow_queries(self) -> list[SQLQuery]:
        return [q for q in self.queries if q.is_slow]

    @property
    def has_n_plus_one(self) -> bool:
        return bool(self.n_plus_one_groups)

    def format_report(self, *, max_queries_shown: int = 30) -> str:
        """Render a human-readable diagnostic report of captured queries."""
        lines: list[str] = [
            f"Captured {self.count} SQL quer{'y' if self.count == 1 else 'ies'} "
            f"({self.total_ms:.2f}ms total):"
        ]
        for idx, q in enumerate(self.queries[:max_queries_shown], start=1):
            tags: list[str] = []
            if q.is_n_plus_one:
                tags.append("N+1")
            if q.is_duplicate:
                tags.append("DUP")
            if q.is_slow:
                tags.append("SLOW")
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            sql_preview = " ".join(q.sql.split())
            if len(sql_preview) > 120:
                sql_preview = sql_preview[:117] + "..."
            loc = f"  ({q.call_site})" if q.call_site else ""
            lines.append(f"  {idx:>2}. [{q.duration_ms:>6.2f}ms]{tag_str} {sql_preview}{loc}")
        if self.count > max_queries_shown:
            lines.append(f"  ... and {self.count - max_queries_shown} more queries")

        if self.n_plus_one_groups:
            lines.append("")
            lines.append("Detected N+1 patterns:")
            for grp in self.n_plus_one_groups:
                loc = f" at {grp.call_site}" if grp.call_site else ""
                lines.append(
                    f"  - {grp.count}x ({grp.total_ms:.2f}ms){loc}: {grp.normalized_sql[:120]}"
                )
                lines.append(f"    Hint: {grp.suggestion}")
        return "\n".join(lines)


@contextlib.contextmanager
def capture_queries(
    target: Any,
    *,
    slow_query_ms: float = 50.0,
    n_plus_one_threshold: int = 3,
    include_transactions: bool = False,
) -> Iterator[QueryCapture]:
    """Capture and analyze all SQL queries executed against *target* inside a ``with`` block.

    *target* may be a SQLAlchemy ``Engine``, ``Session``, ``Flask-SQLAlchemy`` ``db``
    instance, or a ``Flask`` application with SQLAlchemy registered.
    """
    engines = resolve_engines(target)
    if not engines:
        raise RuntimeError(
            "No SQLAlchemy engine found on target. Pass an Engine, Session, "
            "Flask-SQLAlchemy db, or Flask app with SQLAlchemy initialized."
        )

    watched_names: set[str] = set()
    for name, eng in engines:
        watched_names.add(attach_engine(eng, name=name))

    capture = QueryCapture()

    def _sink(query: SQLQuery, engine_name: str, _raw_params: Any) -> None:
        if engine_name not in watched_names:
            return
        if not include_transactions and query.operation == "TRANSACTION":
            return
        capture.queries.append(query)

    register_query_sink(_sink)
    try:
        yield capture
    finally:
        unregister_query_sink(_sink)
        summary = analyze_queries(
            capture.queries,
            slow_query_ms=slow_query_ms,
            n_plus_one_threshold=n_plus_one_threshold,
        )
        capture.n_plus_one_groups = summary.n_plus_one_groups
        capture.duplicate_count = summary.duplicate_count
        capture.total_ms = summary.sql_duration_ms
        capture.has_slow_sql = summary.has_slow_sql


@contextlib.contextmanager
def assert_num_queries(
    target: Any,
    expected: int,
    *,
    include_transactions: bool = False,
) -> Iterator[QueryCapture]:
    """Assert that *exactly* ``expected`` SQL queries run inside the block.

    Direct Flask/SQLAlchemy counterpart to Django's ``TestCase.assertNumQueries``.
    """
    with capture_queries(target, include_transactions=include_transactions) as cap:
        yield cap
    if cap.count != expected:
        raise QueryAssertionError(
            f"Expected exactly {expected} SQL quer{'y' if expected == 1 else 'ies'}, "
            f"but executed {cap.count}.\n{cap.format_report()}"
        )


@contextlib.contextmanager
def assert_max_queries(
    target: Any,
    maximum: int,
    *,
    include_transactions: bool = False,
) -> Iterator[QueryCapture]:
    """Assert that *at most* ``maximum`` SQL queries execute inside the block."""
    with capture_queries(target, include_transactions=include_transactions) as cap:
        yield cap
    if cap.count > maximum:
        raise QueryAssertionError(
            f"Query budget exceeded: expected at most {maximum} SQL queries, "
            f"but executed {cap.count}.\n{cap.format_report()}"
        )


@contextlib.contextmanager
def assert_no_n_plus_one(
    target: Any,
    *,
    threshold: int = 3,
    include_transactions: bool = False,
) -> Iterator[QueryCapture]:
    """Assert that no N+1 SQL query pattern occurs inside the block."""
    with capture_queries(
        target,
        n_plus_one_threshold=threshold,
        include_transactions=include_transactions,
    ) as cap:
        yield cap
    if cap.has_n_plus_one:
        raise QueryAssertionError(
            f"Detected {len(cap.n_plus_one_groups)} N+1 SQL pattern(s) "
            f"(threshold={threshold}).\n{cap.format_report()}"
        )
