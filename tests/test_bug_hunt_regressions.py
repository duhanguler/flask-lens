"""Regression tests verifying every bug discovered during the deep audit."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from flask import Flask, jsonify

from flask_lens import Lens, LensConfig, capture_queries
from flask_lens.collectors.profiler import classify_frame
from flask_lens.redaction import REDACTED, redact_query_string, redact_url
from flask_lens.sql.explain import is_safe_explainable_sql
from flask_lens.sql.listener import _is_user_frame, _shorten_path
from flask_lens.sql.normalizer import classify_operation, extract_tables, normalize_sql
from flask_lens.storage.base import RequestFilter
from flask_lens.storage.sqlite import SQLiteStorage

pytest_plugins = ["pytester"]


def test_bug1_multi_engine_isolation_same_default_name() -> None:
    eng_a = sa.create_engine("sqlite:///:memory:")
    eng_b = sa.create_engine("sqlite:///:memory:")
    with eng_a.begin() as conn:
        conn.execute(sa.text("CREATE TABLE a_items (id INTEGER PRIMARY KEY)"))
    with eng_b.begin() as conn:
        conn.execute(sa.text("CREATE TABLE b_items (id INTEGER PRIMARY KEY)"))

    app_a = Flask("app_a_iso")
    app_b = Flask("app_b_iso")
    lens_a = Lens(
        app_a, engine=eng_a, storage="memory", profile_mode="off", capture_outgoing_http=False
    )
    lens_b = Lens(
        app_b, engine=eng_b, storage="memory", profile_mode="off", capture_outgoing_http=False
    )

    try:
        # Both engines have logical name "default", yet capture_queries(eng_a) MUST ignore eng_b!
        with capture_queries(eng_a) as cap_a:
            with eng_a.connect() as ca:
                ca.execute(sa.text("SELECT * FROM a_items")).fetchall()
            with eng_b.connect() as cb:
                cb.execute(sa.text("SELECT * FROM b_items")).fetchall()
        assert cap_a.count == 1
        assert "a_items" in cap_a.queries[0].sql

        @app_a.get("/run")
        def run_both() -> object:
            with eng_a.connect() as ca:
                ca.execute(sa.text("SELECT * FROM a_items")).fetchall()
            with eng_b.connect() as cb:
                cb.execute(sa.text("SELECT * FROM b_items")).fetchall()
            return jsonify({"ok": True})

        resp = app_a.test_client().get("/run")
        rec = lens_a.storage.get(resp.headers["X-Lens-Request-Id"])
        assert rec is not None
        assert rec.sql_count == 1
        assert "a_items" in rec.sql_queries[0].sql
    finally:
        lens_a.close()
        lens_b.close()
        eng_a.dispose()
        eng_b.dispose()


def test_bug2_config_post_init_validation_and_immutability() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        LensConfig(sample_rate=2.0)
    with pytest.raises(ValueError, match="max_requests"):
        LensConfig(max_requests=0)
    with pytest.raises(ValueError, match="n_plus_one_threshold"):
        LensConfig(n_plus_one_threshold=1)
    with pytest.raises(ValueError, match="slow_query_ms"):
        LensConfig(slow_query_ms=-5.0)
    with pytest.raises(ValueError, match="profile_mode"):
        LensConfig(profile_mode="invalid")

    shared = LensConfig(storage="memory", url_prefix="/__lens", capture_outgoing_http=False)
    lens = Lens(config=shared)
    app1 = Flask("cfg_app1")
    app2 = Flask("cfg_app2")
    s1 = lens.init_app(app1, url_prefix="/lens-a")
    s2 = lens.init_app(app2, url_prefix="/lens-b")
    try:
        assert shared.url_prefix == "/__lens"  # Never mutated!
        assert s1.config.url_prefix == "/lens-a"
        assert s2.config.url_prefix == "/lens-b"
    finally:
        s1.close()
        s2.close()


def test_bug3_replay_path_traversal_and_query_bypass_blocked() -> None:
    app = Flask(__name__)
    lens = Lens(app, storage="memory", profile_mode="off", capture_outgoing_http=False)
    try:
        client = app.test_client()
        for bad_path in [
            "/__lens",
            "/__lens/",
            "/__lens?foo=1",
            "/__lens/api/clear",
            "/foo/../__lens/api/clear",
            "/%5f%5flens/api/health",
            "//evil.example.com/path",
            "https://evil.example.com/path",
            "/\\evil",
        ]:
            resp = client.post("/__lens/api/replay", json={"method": "GET", "path": bad_path})
            assert resp.status_code == 400, f"Expected 400 for {bad_path!r}, got {resp.status_code}"
    finally:
        lens.close()


def test_bug4_sqlite_like_escaping_and_chronological_sql_stats() -> None:
    from flask_lens.models import CapturedRequest, SQLQuery

    store = SQLiteStorage(db_path=":memory:", max_requests=50)
    try:
        # Use non-monotonic IDs ("zzz" saved FIRST, "aaa" saved SECOND) to prove chronological order!
        q1 = SQLQuery(
            id="q1",
            sql="SELECT * FROM users WHERE id = 1",
            normalized_sql="SELECT * FROM users WHERE id = ?",
            fingerprint="fp_users",
            operation="SELECT",
            tables=["users"],
            params=[1],
            duration_ms=2.0,
            offset_ms=0.0,
            call_site="old.py:10",
        )
        q2 = SQLQuery(
            id="q2",
            sql="SELECT * FROM users WHERE id = 2",
            normalized_sql="SELECT * FROM users WHERE id = ?",
            fingerprint="fp_users",
            operation="SELECT",
            tables=["users"],
            params=[2],
            duration_ms=3.0,
            offset_ms=0.0,
            call_site="new.py:20",
        )
        store.save(
            CapturedRequest(
                id="zzz_older",
                timestamp=100.0,
                method="GET",
                path="/userXid",
                full_path="/userXid",
                query_string="",
                endpoint="ep1",
                rule="/userXid",
                status_code=200,
                duration_ms=5.0,
                remote_addr="127.0.0.1",
                sql_queries=[q1],
                sql_count=1,
                sql_duration_ms=2.0,
            )
        )
        store.save(
            CapturedRequest(
                id="aaa_newer",
                timestamp=200.0,
                method="GET",
                path="/user_id",
                full_path="/user_id",
                query_string="",
                endpoint="ep2",
                rule="/user_id",
                status_code=200,
                duration_ms=6.0,
                remote_addr="127.0.0.1",
                sql_queries=[q2],
                sql_count=1,
                sql_duration_ms=3.0,
            )
        )

        # Searching literal underscore "user_id" must NOT match "/userXid"!
        matched, total = store.list_requests(RequestFilter(search="user_id"))
        assert total == 1
        assert matched[0]["id"] == "aaa_newer"

        # sql_stats must point to chronologically newest request ("aaa_newer"), not MAX("zzz_older")!
        stats = store.sql_stats()
        assert len(stats) == 1
        assert stats[0]["sample_request_id"] == "aaa_newer"
        assert stats[0]["sample_call_site"] == "new.py:20"
    finally:
        store.close()


def test_bug5_and_6_explain_security_and_sql_normalization() -> None:
    # Comment-apostrophe injection attempt MUST be rejected
    safe, _ = is_safe_explainable_sql("SELECT 1 -- don't fail\n; DROP TABLE users")
    assert safe is False

    # Unclosed block comment MUST be rejected
    safe, _ = is_safe_explainable_sql("SELECT 1 /* unclosed")
    assert safe is False

    # Data-modifying CTE MUST be rejected
    safe, _ = is_safe_explainable_sql("WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d")
    assert safe is False

    # String literal containing ') DELETE' is safe and classified as SELECT
    sql_safe_cte = (
        "WITH a AS (SELECT * FROM books WHERE note = ') DELETE FROM books') SELECT * FROM a"
    )
    assert classify_operation(sql_safe_cte) == "SELECT"
    assert is_safe_explainable_sql(sql_safe_cte)[0] is True

    # Schema-qualified quoted tables
    tables = extract_tables(
        'SELECT * FROM "public"."orders" o JOIN `shop`.`books` b ON b.id = o.book_id'
    )
    assert tables == ["public.orders", "shop.books"]

    # IN-list with NULL & Postgres E'...' escape strings
    n1 = normalize_sql(r"SELECT * FROM logs WHERE msg = E'it\'s ok' AND id IN (1, 2, NULL)")
    n2 = normalize_sql("SELECT * FROM logs WHERE msg = 'other' AND id IN (9)")
    assert n1.fingerprint == n2.fingerprint


def test_bug7_query_string_and_url_secret_redaction() -> None:
    qs = redact_query_string(
        "access_token=gho_secret&page=2&api_key=sk_999", frozenset({"access_token", "api_key"})
    )
    assert "gho_secret" not in qs
    assert "sk_999" not in qs
    assert "page=2" in qs

    redacted_u = redact_url(
        "https://alice:topsecret@api.example.com/v1/items?token=tok_123&limit=10",
        frozenset({"token"}),
    )
    assert "topsecret" not in redacted_u
    assert "tok_123" not in redacted_u
    assert "limit=10" in redacted_u

    app = Flask(__name__)
    lens = Lens(
        app, storage="memory", profile_mode="off", capture_sql=False, capture_outgoing_http=True
    )
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json={"ok": True}))
    try:

        @app.get("/callback")
        def callback() -> object:
            with httpx.Client(transport=transport) as hc:
                hc.get("https://svc.local/v1?api_key=sk_live_out&page=1")
            return jsonify({"ok": True})

        resp = app.test_client().get("/callback?access_token=gho_in&code=abc")
        rec = lens.storage.get(resp.headers["X-Lens-Request-Id"])
        assert rec is not None
        assert "gho_in" not in rec.full_path
        assert "gho_in" not in rec.query_string
        assert "code=abc" in rec.full_path
        assert len(rec.outgoing_http) == 1
        assert "sk_live_out" not in rec.outgoing_http[0].url
        assert "page=1" in rec.outgoing_http[0].url
        assert REDACTED in rec.outgoing_http[0].url or "%5BREDACTED%5D" in rec.outgoing_http[0].url
    finally:
        lens.close()


def test_bug8_async_flask_view_with_lens_profile_decorator() -> None:
    import asyncio
    import inspect

    app = Flask(__name__)
    lens = Lens(
        app,
        storage="memory",
        profile_mode="on_demand",
        capture_sql=False,
        capture_outgoing_http=False,
    )
    try:

        @app.get("/async-work")
        @lens.profile
        async def async_work() -> object:
            await asyncio.sleep(0.002)
            total = sum(x * x for x in range(200))
            return jsonify({"total": total})

        assert inspect.iscoroutinefunction(async_work)
        resp = app.test_client().get("/async-work")
        assert resp.status_code == 200
        assert resp.get_json()["total"] == sum(x * x for x in range(200))
        rec = lens.storage.get(resp.headers["X-Lens-Request-Id"])
        assert rec is not None and rec.profile is not None
    finally:
        lens.close()


def test_bug9_multi_app_does_not_duplicate_log_entries() -> None:
    app1 = Flask("log_app1")
    app2 = Flask("log_app2")
    lens1 = Lens(
        app1, storage="memory", profile_mode="off", capture_sql=False, capture_outgoing_http=False
    )
    lens2 = Lens(
        app2, storage="memory", profile_mode="off", capture_sql=False, capture_outgoing_http=False
    )
    logger = logging.getLogger("multi_app_log_test")
    logger.setLevel(logging.INFO)
    try:

        @app1.get("/log-once")
        def log_once() -> str:
            logger.info("hello exactly once")
            return "ok"

        resp = app1.test_client().get("/log-once")
        rec = lens1.storage.get(resp.headers["X-Lens-Request-Id"])
        assert rec is not None
        matches = [entry for entry in rec.logs if entry.message == "hello exactly once"]
        assert len(matches) == 1
    finally:
        lens1.close()
        lens2.close()


def test_bug10_asset_allowlist_and_sse_shutdown() -> None:
    app = Flask(__name__)
    lens = Lens(
        app, storage="memory", profile_mode="off", capture_sql=False, capture_outgoing_http=False
    )
    client = app.test_client()

    # Non-allowlisted files inside ui/ MUST 404
    assert client.get("/__lens/assets/__init__.py").status_code == 404
    assert client.get("/__lens/assets/index.html").status_code == 404
    assert client.get("/__lens/assets/styles.css").status_code == 200
    assert client.get("/__lens/assets/app.js").status_code == 200

    # Broadcaster close() wakes up SSE immediately
    t0 = time.perf_counter()

    def _close_soon() -> None:
        time.sleep(0.05)
        lens.close()

    t = threading.Thread(target=_close_soon)
    t.start()
    resp = client.get("/__lens/api/stream")
    body = resp.get_data(as_text=True)
    t.join(timeout=2.0)
    elapsed = time.perf_counter() - t0

    assert "event: connected" in body
    assert elapsed < 2.0


def test_bug12_pytest_num_queries_marker_and_invalid_marker_no_internalerror(pytester: Any) -> None:
    pytester.makepyfile(
        """
        import pytest
        import sqlalchemy as sa

        @pytest.fixture
        def engine():
            eng = sa.create_engine("sqlite:///:memory:")
            with eng.begin() as conn:
                conn.execute(sa.text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
                conn.execute(sa.text("INSERT INTO t (id) VALUES (1)"))
            yield eng
            eng.dispose()

        @pytest.mark.num_queries(1)
        def test_exact_passes(engine):
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT * FROM t")).fetchall()

        @pytest.mark.num_queries(2)
        def test_exact_fails(engine):
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT * FROM t")).fetchall()

        @pytest.mark.max_queries
        def test_missing_arg_clean_failure(engine):
            pass
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1, failed=2)
    result.stdout.fnmatch_lines(["*@pytest.mark.num_queries(2) failed*"])
    result.stdout.fnmatch_lines(["*ValueError: @pytest.mark.max_queries requires*"])


def test_bug13_app_root_prefix_collision_safety() -> None:
    # /srv/app_vendor/lib.py MUST NOT match app_root="/srv/app"
    assert _is_user_frame("/srv/app_vendor/lib.py", "/srv/app") is False
    assert _is_user_frame("/srv/app/views.py", "/srv/app") is True
    assert _shorten_path("/srv/app/views.py", "/srv/app") == "views.py"
    assert not _shorten_path("/srv/app_vendor/lib.py", "/srv/app").startswith("..")
    assert classify_frame("/srv/app/views.py", "/srv/app") == "user"
