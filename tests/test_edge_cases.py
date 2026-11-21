"""Edge-case, resilience, multi-app isolation, and example-app smoke tests."""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from typing import Any

import httpx
import sqlalchemy as sa
from flask import Flask, jsonify

from flask_lens import Lens, LensConfig
from flask_lens.cli import lens_cli
from flask_lens.models import CapturedRequest
from flask_lens.sql.explain import _extract_plan_warnings


def test_factory_pattern_and_multi_app_isolation() -> None:
    lens = Lens(storage="memory", profile_mode="off", capture_outgoing_http=False)
    app_a = Flask("app_a")
    app_b = Flask("app_b")

    @app_a.get("/a")
    def route_a() -> str:
        return "a"

    @app_b.get("/b")
    def route_b() -> str:
        return "b"

    state_a = lens.init_app(app_a)
    state_b = lens.init_app(app_b)
    try:
        app_a.test_client().get("/a")
        app_b.test_client().get("/b")
        app_b.test_client().get("/b")

        assert state_a.storage.overview_stats()["total_requests"] == 1
        assert state_b.storage.overview_stats()["total_requests"] == 2
        assert state_a.storage.list_requests()[0][0]["path"] == "/a"
        assert state_b.storage.list_requests()[0][0]["path"] == "/b"
    finally:
        state_a.close()
        state_b.close()


def test_sample_rate_zero_and_slow_profile_mode() -> None:
    app = Flask(__name__)
    lens = Lens(
        app,
        storage="memory",
        sample_rate=0.0,
        profile_mode="slow",
        profile_slow_ms=15.0,
        capture_outgoing_http=False,
    )
    try:

        @app.get("/fast")
        def fast() -> str:
            return "fast"

        @app.get("/slow")
        def slow() -> str:
            time.sleep(0.025)
            return "slow"

        client = app.test_client()
        # sample_rate=0 skips regular requests
        client.get("/fast")
        assert lens.storage.overview_stats()["total_requests"] == 0

        # Explicit replay bypasses sample_rate
        r_fast = client.get("/fast", headers={"X-Lens-Replay": "1"})
        rec_fast = lens.storage.get(r_fast.headers["X-Lens-Request-Id"])
        assert rec_fast is not None and rec_fast.profile is None

        r_slow = client.get("/slow", headers={"X-Lens-Replay": "1"})
        rec_slow = lens.storage.get(r_slow.headers["X-Lens-Request-Id"])
        assert rec_slow is not None and rec_slow.profile is not None
    finally:
        lens.close()


def test_sql_error_hook_and_outgoing_http_error_captured() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    app = Flask(__name__)
    lens = Lens(
        app,
        engine=engine,
        storage="memory",
        profile_mode="off",
        capture_outgoing_http=True,
    )

    def _boom_transport(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    try:

        @app.get("/sql-err")
        def sql_err() -> tuple[object, int]:
            try:
                with engine.connect() as conn:
                    conn.execute(sa.text("SELECT * FROM nonexistent_table_xyz"))
            except Exception:
                return jsonify({"handled": True}), 400
            return jsonify({"ok": True}), 200

        @app.get("/http-err")
        def http_err() -> tuple[object, int]:
            with httpx.Client(transport=httpx.MockTransport(_boom_transport)) as hc:
                try:
                    hc.get("https://down.service.local/ping")
                except httpx.ConnectError:
                    return jsonify({"upstream_down": True}), 502
            return jsonify({"ok": True}), 200

        client = app.test_client()
        r1 = client.get("/sql-err")
        assert r1.status_code == 400
        rec1 = lens.storage.get(r1.headers["X-Lens-Request-Id"])
        assert rec1 is not None and rec1.sql_count == 1
        assert "nonexistent_table_xyz" in rec1.sql_queries[0].sql

        r2 = client.get("/http-err")
        assert r2.status_code == 502
        rec2 = lens.storage.get(r2.headers["X-Lens-Request-Id"])
        assert rec2 is not None and len(rec2.outgoing_http) == 1
        assert rec2.outgoing_http[0].status_code is None
        assert rec2.outgoing_http[0].error is not None
        assert "ConnectError" in rec2.outgoing_http[0].error
    finally:
        lens.close()
        engine.dispose()


def test_blueprint_filter_and_error_edge_cases() -> None:
    app = Flask(__name__)
    lens = Lens(
        app,
        config=LensConfig(
            storage="sqlite",
            db_path=":memory:",
            profile_mode="off",
            capture_outgoing_http=False,
            allow_explain=False,
        ),
    )
    app.cli.add_command(lens_cli)
    try:

        @app.get("/ok")
        def ok() -> tuple[str, int]:
            return "ok", 200

        @app.get("/redir")
        def redir() -> tuple[str, int, dict[str, str]]:
            return "", 302, {"Location": "/ok"}

        @app.get("/missing")
        def missing() -> tuple[str, int]:
            return "nope", 404

        @app.get("/fail")
        def fail() -> tuple[str, int]:
            return "err", 500

        client = app.test_client()
        # Empty CLI commands
        runner = app.test_cli_runner()
        assert "No captured requests yet." in runner.invoke(args=["lens", "stats"]).output
        assert "No captured SQL queries yet." in runner.invoke(args=["lens", "slow"]).output

        r200 = client.get("/ok")
        client.get("/redir")
        client.get("/missing")
        client.get("/fail")

        assert client.get("/__lens/api/requests?status=2xx").get_json()["total"] == 1
        assert client.get("/__lens/api/requests?status=3xx").get_json()["total"] == 1
        assert client.get("/__lens/api/requests?status=4xx").get_json()["total"] == 1
        assert client.get("/__lens/api/requests?status=5xx").get_json()["total"] == 1
        assert client.get("/__lens/api/requests?errors=1").get_json()["total"] == 2
        assert client.get("/__lens/api/requests?min_ms=99999").get_json()["total"] == 0
        assert client.get("/__lens/api/requests?min_ms=notanumber").get_json()["total"] == 4

        # Compare validation errors
        assert client.get("/__lens/api/compare").status_code == 400
        assert client.get("/__lens/api/compare?baseline=nope&candidate=also").status_code == 404
        req_id = r200.headers["X-Lens-Request-Id"]
        assert (
            client.get(f"/__lens/api/compare?baseline={req_id}&candidate=missing").status_code
            == 404
        )

        # Explain disabled -> 400
        exp = client.post(f"/__lens/api/requests/{req_id}/explain/q1")
        assert exp.status_code == 400
        assert "disabled" in exp.get_json()["error"]

        # Replay validation errors
        assert (
            client.post("/__lens/api/replay", json={"method": "TRACE", "path": "/ok"}).status_code
            == 400
        )
        assert (
            client.post(
                "/__lens/api/replay", json={"method": "GET", "path": "/ok", "headers": "bad"}
            ).status_code
            == 400
        )
    finally:
        lens.close()


def test_model_roundtrip_and_explain_dialect_warnings() -> None:
    pg_warnings = _extract_plan_warnings(
        "postgresql",
        ["Seq Scan on books  (cost=0.00..22.00 rows=100)", "Sort Method: external merge"],
    )
    assert len(pg_warnings) == 2

    mysql_warnings = _extract_plan_warnings(
        "mysql",
        ["1 | SIMPLE | books | ALL | NULL | Using temporary; Using filesort"],
    )
    assert len(mysql_warnings) == 3

    sqlite_warnings = _extract_plan_warnings(
        "sqlite",
        ["SCAN books", "USE TEMP B-TREE FOR ORDER BY"],
    )
    assert len(sqlite_warnings) == 2

    # Roundtrip CapturedRequest.to_dict -> from_dict
    sample = CapturedRequest(
        id="rt1",
        timestamp=1700000000.0,
        method="GET",
        path="/test",
        full_path="/test?a=1",
        query_string="a=1",
        endpoint="test",
        rule="/test",
        status_code=200,
        duration_ms=4.2,
        remote_addr="127.0.0.1",
    )
    restored = CapturedRequest.from_dict(sample.to_dict())
    assert restored.id == "rt1" and restored.full_path == "/test?a=1"


def test_pytest_plugin_fixtures(
    lens_capture: Any,
    lens_assert_num_queries: Any,
    lens_assert_max_queries: Any,
    lens_assert_no_n_plus_one: Any,
) -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE demo (id INTEGER PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO demo (id) VALUES (1), (2)"))
    try:
        with lens_capture(engine) as cap, engine.connect() as conn:
            conn.execute(sa.text("SELECT * FROM demo")).fetchall()
        assert cap.count == 1

        with lens_assert_num_queries(engine, 1), engine.connect() as conn:
            conn.execute(sa.text("SELECT * FROM demo")).fetchall()

        with lens_assert_max_queries(engine, 2), engine.connect() as conn:
            conn.execute(sa.text("SELECT * FROM demo")).fetchall()

        with lens_assert_no_n_plus_one(engine), engine.connect() as conn:
            conn.execute(sa.text("SELECT * FROM demo")).fetchall()
    finally:
        engine.dispose()


def test_bookstore_example_app_smoke() -> None:
    example_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "examples", "bookstore_app.py")
    )
    spec = importlib.util.spec_from_file_location("bookstore_app_smoke", example_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bookstore_app_smoke"] = mod
    try:
        spec.loader.exec_module(mod)
        app = mod.create_app(db_uri="sqlite:///:memory:", lens_db=":memory:")
        state = app.extensions["lens"]
        try:
            client = app.test_client()
            assert client.get("/").status_code == 200
            assert client.get("/api/authors").status_code == 200
            assert client.get("/api/authors?optimized=1").status_code == 200
            assert client.get("/api/books/search?q=Patterns").status_code == 200
            assert client.get("/api/orders/report").status_code == 200
            assert client.post("/api/orders", json={"book_id": 1, "quantity": 2}).status_code == 201
            assert client.get("/api/boom").status_code == 500
            assert client.get("/__lens/").status_code == 200

            ov = client.get("/__lens/api/overview").get_json()
            assert ov["total_requests"] == 7
            assert ov["n_plus_one_requests"] == 1
            assert ov["exception_count"] == 1
        finally:
            state.close()
    finally:
        sys.modules.pop("bookstore_app_smoke", None)
