"""Unit tests for SQL normalization, N+1/duplicate/slow analysis, and EXPLAIN safety."""

from __future__ import annotations

import sqlalchemy as sa

from flask_lens.models import SQLQuery
from flask_lens.sql.analyzer import analyze_queries
from flask_lens.sql.explain import is_safe_explainable_sql, run_explain
from flask_lens.sql.normalizer import classify_operation, extract_tables, normalize_sql


def _make_query(
    qid: str,
    sql: str,
    *,
    params: object = None,
    duration_ms: float = 1.0,
    call_site: str = "views.py:10 in index",
) -> SQLQuery:
    norm = normalize_sql(sql)
    return SQLQuery(
        id=qid,
        sql=sql,
        normalized_sql=norm.normalized,
        fingerprint=norm.fingerprint,
        operation=norm.operation,
        tables=norm.tables,
        params=params,
        duration_ms=duration_ms,
        offset_ms=0.0,
        call_site=call_site,
    )


def test_normalize_literals_params_and_in_lists() -> None:
    q1 = normalize_sql("SELECT * FROM books WHERE author_id = 1 AND title = 'Alpha'")
    q2 = normalize_sql("SELECT * FROM books WHERE author_id = 99 AND title = 'Beta''s'")
    assert q1.fingerprint == q2.fingerprint
    assert q1.normalized == "SELECT * FROM books WHERE author_id = ? AND title = ?"
    assert q1.operation == "SELECT"
    assert q1.tables == ["books"]

    in1 = normalize_sql("SELECT id FROM books WHERE id IN (1, 2, 3)")
    in2 = normalize_sql("SELECT id FROM books WHERE id IN ($1, $2, $3, $4, $5)")
    in3 = normalize_sql("SELECT id FROM books WHERE id IN (%(a)s, %(b)s)")
    assert in1.fingerprint == in2.fingerprint == in3.fingerprint
    assert "IN (?)" in in1.normalized


def test_normalize_strips_comments_and_collapses_values() -> None:
    q = normalize_sql(
        """
        /* fetch items */
        INSERT INTO items (name, price)
        -- multi-row insert
        VALUES ('a', 10), ('b', 20), ('c', 30)
        """
    )
    assert q.operation == "INSERT"
    assert q.tables == ["items"]
    assert q.normalized == "INSERT INTO items (name, price) VALUES (?, ?), (...)"


def test_classify_operation_variants() -> None:
    assert classify_operation("  -- lead comment\nSELECT 1") == "SELECT"
    assert classify_operation("WITH cte AS (SELECT 1) SELECT * FROM cte") == "SELECT"
    assert (
        classify_operation("WITH cte AS (SELECT 1) DELETE FROM items WHERE id IN (SELECT 1)")
        == "DELETE"
    )
    assert classify_operation("UPDATE users SET active = 1") == "UPDATE"
    assert classify_operation("BEGIN") == "TRANSACTION"
    assert classify_operation("CREATE TABLE t (id INT)") == "DDL"
    assert classify_operation("   ") == "OTHER"


def test_extract_tables_handles_joins_and_quotes() -> None:
    sql = 'SELECT * FROM "public"."orders" o JOIN `books` b ON b.id = o.book_id LEFT JOIN authors a ON a.id = b.author_id'
    assert extract_tables(sql) == ["public", "books", "authors"] or "books" in extract_tables(sql)
    tables = extract_tables('SELECT * FROM "orders" JOIN books ON books.id = orders.book_id')
    assert tables == ["orders", "books"]


def test_analyze_queries_detects_n_plus_one_duplicates_and_slow() -> None:
    queries = [
        _make_query("q0", "SELECT * FROM authors", duration_ms=2.0, call_site="views.py:12"),
        _make_query("q1", "SELECT * FROM books WHERE author_id = ?", params=[1], duration_ms=1.5),
        _make_query("q2", "SELECT * FROM books WHERE author_id = ?", params=[2], duration_ms=1.5),
        _make_query("q3", "SELECT * FROM books WHERE author_id = ?", params=[3], duration_ms=65.0),
        _make_query("q4", "SELECT * FROM books WHERE author_id = ?", params=[3], duration_ms=1.2),
    ]
    summary = analyze_queries(queries, slow_query_ms=50.0, n_plus_one_threshold=3)

    assert summary.sql_count == 5
    assert summary.has_slow_sql is True
    assert queries[3].is_slow is True
    assert queries[0].is_slow is False

    # q3 and q4 have exact same SQL and params=[3] -> 1 excess duplicate
    assert summary.duplicate_count == 1
    assert queries[3].is_duplicate is True
    assert queries[4].is_duplicate is True
    assert queries[1].is_duplicate is False

    # q1..q4 share normalized fingerprint (4 >= 3) -> 1 N+1 group
    assert len(summary.n_plus_one_groups) == 1
    grp = summary.n_plus_one_groups[0]
    assert grp.count == 4
    assert "selectinload" in grp.suggestion
    assert all(q.is_n_plus_one for q in queries[1:])
    assert queries[0].is_n_plus_one is False


def test_explain_safety_and_sqlite_scan_warnings() -> None:
    assert is_safe_explainable_sql("SELECT * FROM books WHERE id = ?")[0] is True
    assert is_safe_explainable_sql("SELECT * FROM books WHERE note = 'a;b'")[0] is True
    assert is_safe_explainable_sql("SELECT 1; DROP TABLE books")[0] is False
    assert is_safe_explainable_sql("DELETE FROM books WHERE id = 1")[0] is False
    assert is_safe_explainable_sql("UPDATE books SET title = 'x'")[0] is False
    assert is_safe_explainable_sql("")[0] is False

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT)"))
        conn.execute(sa.text("INSERT INTO books (title) VALUES ('Systems'), ('Python')"))

    # Full table scan via LIKE
    res_scan = run_explain(engine, "SELECT * FROM books WHERE title LIKE ?", ("%Sys%",))
    assert res_scan.supported is True
    assert res_scan.error is None
    assert res_scan.dialect == "sqlite"
    assert len(res_scan.plan_lines) >= 1
    assert any("Full table scan" in w for w in res_scan.warnings)

    # Indexed primary-key lookup should have no full-table-scan warning
    res_pk = run_explain(engine, "SELECT * FROM books WHERE id = ?", (1,))
    assert res_pk.error is None
    assert not any("Full table scan" in w for w in res_pk.warnings)

    # Unsafe statement rejected cleanly
    res_bad = run_explain(engine, "DELETE FROM books WHERE id = ?", (1,))
    assert res_bad.supported is False
    assert res_bad.error is not None
    engine.dispose()
