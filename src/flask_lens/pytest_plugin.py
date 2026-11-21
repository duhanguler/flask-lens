"""Auto-registered pytest plugin exposing query-budget fixtures and markers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from flask_lens.sql.listener import resolve_engines
from flask_lens.testing import (
    QueryAssertionError,
    QueryCapture,
    assert_max_queries,
    assert_no_n_plus_one,
    assert_num_queries,
    capture_queries,
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "num_queries(expected): fail the test unless exactly `expected` SQL queries execute.",
    )
    config.addinivalue_line(
        "markers",
        "max_queries(limit): fail the test if more than `limit` SQL queries execute.",
    )
    config.addinivalue_line(
        "markers",
        "no_n_plus_one(threshold=3): fail the test if an N+1 SQL query pattern is detected.",
    )


@pytest.fixture
def lens_capture() -> Callable[..., Any]:
    """Factory fixture returning :func:`flask_lens.testing.capture_queries`."""
    return capture_queries


@pytest.fixture
def lens_assert_num_queries() -> Callable[..., Any]:
    """Factory fixture returning :func:`flask_lens.testing.assert_num_queries`."""
    return assert_num_queries


@pytest.fixture
def lens_assert_max_queries() -> Callable[..., Any]:
    """Factory fixture returning :func:`flask_lens.testing.assert_max_queries`."""
    return assert_max_queries


@pytest.fixture
def lens_assert_no_n_plus_one() -> Callable[..., Any]:
    """Factory fixture returning :func:`flask_lens.testing.assert_no_n_plus_one`."""
    return assert_no_n_plus_one


@pytest.hookimpl(hookwrapper=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> Any:
    """Enforce ``num_queries``, ``max_queries``, and ``no_n_plus_one`` markers around test calls."""
    num_marker = pyfuncitem.get_closest_marker("num_queries")
    max_marker = pyfuncitem.get_closest_marker("max_queries")
    n1_marker = pyfuncitem.get_closest_marker("no_n_plus_one")

    if num_marker is None and max_marker is None and n1_marker is None:
        yield
        return

    config_error: Exception | None = None
    expected_num: int | None = None
    max_limit: int | None = None
    threshold = 3

    if num_marker is not None:
        if num_marker.args:
            expected_num = int(num_marker.args[0])
        elif "expected" in num_marker.kwargs:
            expected_num = int(num_marker.kwargs["expected"])
        else:
            config_error = ValueError(
                "@pytest.mark.num_queries requires an integer expected count."
            )

    if max_marker is not None and config_error is None:
        if max_marker.args:
            max_limit = int(max_marker.args[0])
        elif "limit" in max_marker.kwargs:
            max_limit = int(max_marker.kwargs["limit"])
        else:
            config_error = ValueError(
                "@pytest.mark.max_queries requires an integer limit argument."
            )

    if n1_marker is not None and config_error is None:
        if n1_marker.args:
            threshold = int(n1_marker.args[0])
        elif "threshold" in n1_marker.kwargs:
            threshold = int(n1_marker.kwargs["threshold"])

    target = _discover_target_from_funcargs(pyfuncitem.funcargs) if config_error is None else None
    if config_error is not None or target is None:
        outcome = yield
        if outcome.excinfo is None:
            outcome.force_exception(
                config_error
                or RuntimeError(
                    "flask-lens marker requires a SQLAlchemy target fixture "
                    "(e.g. 'db', 'engine', 'session', or 'app' with SQLAlchemy)."
                )
            )
        return

    cap: QueryCapture
    with capture_queries(target, n_plus_one_threshold=threshold) as cap:
        outcome = yield

    # Do not mask an existing test failure with a query-budget error.
    if outcome.excinfo is not None:
        return

    if expected_num is not None and cap.count != expected_num:
        outcome.force_exception(
            QueryAssertionError(
                f"@pytest.mark.num_queries({expected_num}) failed: "
                f"executed {cap.count} SQL queries.\n{cap.format_report()}"
            )
        )
        return

    if max_limit is not None and cap.count > max_limit:
        outcome.force_exception(
            QueryAssertionError(
                f"@pytest.mark.max_queries({max_limit}) failed: executed {cap.count} SQL queries.\n"
                f"{cap.format_report()}"
            )
        )
        return

    if n1_marker is not None and cap.has_n_plus_one:
        outcome.force_exception(
            QueryAssertionError(
                f"@pytest.mark.no_n_plus_one(threshold={threshold}) failed: "
                f"detected {len(cap.n_plus_one_groups)} N+1 pattern(s).\n"
                f"{cap.format_report()}"
            )
        )


def _discover_target_from_funcargs(funcargs: dict[str, Any]) -> Any | None:
    preferred_keys = ("db", "engine", "session", "app", "client")
    ordered_values: list[Any] = []
    seen_ids: set[int] = set()

    for key in preferred_keys:
        if key in funcargs and funcargs[key] is not None:
            val = funcargs[key]
            if key == "client" and hasattr(val, "application"):
                val = val.application
            if id(val) not in seen_ids:
                seen_ids.add(id(val))
                ordered_values.append(val)

    for val in funcargs.values():
        if val is not None and id(val) not in seen_ids:
            seen_ids.add(id(val))
            ordered_values.append(val)

    for candidate in ordered_values:
        try:
            engines = resolve_engines(candidate)
            if engines:
                return candidate
        except TypeError:
            continue
    return None
