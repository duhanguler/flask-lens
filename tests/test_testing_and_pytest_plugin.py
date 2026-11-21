"""Tests for Django assertNumQueries counterparts and the auto-registered pytest plugin."""

from __future__ import annotations

from typing import Any

import pytest
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

from flask_lens import (
    Lens,
    QueryAssertionError,
    assert_max_queries,
    assert_no_n_plus_one,
    assert_num_queries,
    capture_queries,
)

pytest_plugins = ["pytester"]


def test_capture_and_assert_helpers_pass_and_fail(
    bookstore_bundle: tuple[Flask, SQLAlchemy, Lens],
) -> None:
    app, db, _lens = bookstore_bundle
    client = app.test_client()

    # Exact query count passes for optimized route (2 queries)
    with assert_num_queries(db, 2) as cap:
        resp = client.get("/api/authors?optimized=1")
        assert resp.status_code == 200
    assert cap.count == 2
    assert cap.has_n_plus_one is False

    # Exact query count fails with clear SQL diagnostic report for N+1 route (7 queries)
    with pytest.raises(QueryAssertionError) as exc_info, assert_num_queries(app, 2):
        client.get("/api/authors")
    msg = str(exc_info.value)
    assert "Expected exactly 2 SQL queries, but executed 7" in msg
    assert "Detected N+1 patterns" in msg

    # Max queries passes & fails
    with assert_max_queries(db, 3):
        client.get("/api/authors?optimized=1")

    with (
        pytest.raises(QueryAssertionError, match="Query budget exceeded"),
        assert_max_queries(db, 3),
    ):
        client.get("/api/authors")

    # assert_no_n_plus_one passes & fails
    with assert_no_n_plus_one(db):
        client.get("/api/authors?optimized=1")

    with (
        pytest.raises(QueryAssertionError, match="N\\+1 SQL pattern"),
        assert_no_n_plus_one(db, threshold=3),
    ):
        client.get("/api/authors")


def test_inner_exception_is_not_masked_by_query_assertion(
    bookstore_bundle: tuple[Flask, SQLAlchemy, Lens],
) -> None:
    _app, db, _lens = bookstore_bundle
    with pytest.raises(RuntimeError, match="real failure"), assert_num_queries(db, 99):
        raise RuntimeError("real failure")


def test_capture_queries_standalone_without_lens_extension() -> None:
    import sqlalchemy as sa

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)"))
        conn.execute(sa.text("INSERT INTO notes (body) VALUES ('a'), ('b'), ('c')"))

    with capture_queries(engine) as cap, engine.connect() as conn:
        for i in (1, 2, 3):
            conn.execute(sa.text("SELECT * FROM notes WHERE id = :i"), {"i": i}).fetchall()

    assert cap.count == 3
    assert cap.has_n_plus_one is True
    assert "N+1" in cap.format_report()
    engine.dispose()


def test_pytest_plugin_markers_enforced(pytester: Any) -> None:
    pytester.makepyfile(
        """
        import pytest
        import sqlalchemy as sa

        @pytest.fixture
        def engine():
            eng = sa.create_engine("sqlite:///:memory:")
            with eng.begin() as conn:
                conn.execute(sa.text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
                conn.execute(sa.text("INSERT INTO t (v) VALUES ('a'), ('b'), ('c'), ('d')"))
            yield eng
            eng.dispose()

        @pytest.mark.max_queries(2)
        @pytest.mark.no_n_plus_one
        def test_passes_budget(engine):
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT * FROM t")).fetchall()

        @pytest.mark.max_queries(2)
        def test_fails_max_queries(engine):
            with engine.connect() as conn:
                for i in (1, 2, 3):
                    conn.execute(sa.text("SELECT * FROM t WHERE id = :i"), {"i": i}).fetchall()

        @pytest.mark.no_n_plus_one(threshold=3)
        def test_fails_n_plus_one(engine):
            with engine.connect() as conn:
                for i in (1, 2, 3, 4):
                    conn.execute(sa.text("SELECT * FROM t WHERE id = :i"), {"i": i}).fetchall()
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1, failed=2)
    result.stdout.fnmatch_lines(["*@pytest.mark.max_queries(2) failed*"])
    result.stdout.fnmatch_lines(["*@pytest.mark.no_n_plus_one(threshold=3) failed*"])
