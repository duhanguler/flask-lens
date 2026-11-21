"""Integration tests for Lens Flask extension, SQLAlchemy N+1, profiler, spans, logs, and exceptions."""

from __future__ import annotations

import logging

import pytest
from flask import Flask, jsonify
from flask_sqlalchemy import SQLAlchemy

from flask_lens import Lens, LensConfig
from flask_lens.redaction import REDACTED
from tests.conftest import build_bookstore_app


def test_captures_n_plus_one_and_optimized_selectinload(
    bookstore_bundle: tuple[Flask, SQLAlchemy, Lens],
) -> None:
    app, _db, lens = bookstore_bundle
    client = app.test_client()

    # N+1 request: 1 query for authors + 6 lazy queries for books = 7 queries
    resp_n1 = client.get("/api/authors")
    assert resp_n1.status_code == 200
    assert "Server-Timing" in resp_n1.headers
    assert "sql;dur=" in resp_n1.headers["Server-Timing"]
    req_id_n1 = resp_n1.headers["X-Lens-Request-Id"]

    captured_n1 = lens.storage.get(req_id_n1)
    assert captured_n1 is not None
    assert captured_n1.sql_count == 7
    assert captured_n1.has_n_plus_one is True
    assert len(captured_n1.n_plus_one_groups) == 1
    assert captured_n1.n_plus_one_groups[0].count == 6
    assert captured_n1.profile is not None
    assert captured_n1.profile.flamegraph is not None

    # Optimized request: 1 query for authors + 1 IN (...) query via selectinload = 2 queries
    resp_opt = client.get("/api/authors?optimized=1")
    assert resp_opt.status_code == 200
    captured_opt = lens.storage.get(resp_opt.headers["X-Lens-Request-Id"])
    assert captured_opt is not None
    assert captured_opt.sql_count == 2
    assert captured_opt.has_n_plus_one is False


def test_captures_logs_spans_events_redaction_and_templates(
    bookstore_bundle: tuple[Flask, SQLAlchemy, Lens],
) -> None:
    app, _db, lens = bookstore_bundle
    client = app.test_client()
    logging.getLogger("test_bookstore").setLevel(logging.INFO)

    resp = client.post(
        "/api/echo",
        json={"user": "alice", "password": "secret-password", "api_key": "sk_123"},
        headers={"Authorization": "Bearer secret-jwt", "X-Custom": "visible"},
    )
    assert resp.status_code == 200
    rec = lens.storage.get(resp.headers["X-Lens-Request-Id"])
    assert rec is not None
    assert rec.request_headers["Authorization"] == REDACTED
    assert rec.request_headers["X-Custom"] == "visible"
    assert rec.request_body is not None
    assert "secret-password" not in rec.request_body
    assert "sk_123" not in rec.request_body
    assert "alice" in rec.request_body

    assert any(s.name == "echo.process" and s.kind == "span" for s in rec.spans)
    assert any(s.name == "echo.done" and s.kind == "event" for s in rec.spans)
    assert any("Echoing JSON payload" in entry.message for entry in rec.logs)

    # HTML template capture
    resp_html = client.get("/html")
    assert resp_html.status_code == 200
    rec_html = lens.storage.get(resp_html.headers["X-Lens-Request-Id"])
    assert rec_html is not None
    assert len(rec_html.templates) == 1


def test_streaming_response_passes_through_unharmed(
    bookstore_bundle: tuple[Flask, SQLAlchemy, Lens],
) -> None:
    app, _db, lens = bookstore_bundle
    client = app.test_client()
    resp = client.get("/stream")
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "chunk-1-chunk-2"
    rec = lens.storage.get(resp.headers["X-Lens-Request-Id"])
    assert rec is not None
    assert rec.response_body == "<streaming response>"


def test_unhandled_exception_captured_with_redacted_locals_in_both_modes() -> None:
    # Mode 1: TESTING=False (Flask converts exception into 500 response)
    app1, _db1, lens1 = build_bookstore_app(storage="memory")
    try:
        resp = app1.test_client().get("/api/boom")
        assert resp.status_code == 500
        items, total = lens1.storage.list_requests()
        assert total == 1
        rec = lens1.storage.get(items[0]["id"])
        assert rec is not None and rec.exception is not None
        assert rec.exception.exc_type == "ZeroDivisionError"
        boom_frame = next(f for f in rec.exception.frames if f.function == "boom")
        assert boom_frame.locals_preview["api_key"] == REDACTED
        assert boom_frame.locals_preview["visible_count"] == "42"
    finally:
        lens1.close()

    # Mode 2: TESTING=True (exception propagates out of client.get; teardown_request must still capture!)
    app2, _db2, lens2 = build_bookstore_app(storage="memory")
    app2.config["TESTING"] = True
    try:
        with pytest.raises(ZeroDivisionError):
            app2.test_client().get("/api/boom")
        items2, total2 = lens2.storage.list_requests()
        assert total2 == 1
        rec2 = lens2.storage.get(items2[0]["id"])
        assert rec2 is not None and rec2.exception is not None
        assert rec2.exception.exc_type == "ZeroDivisionError"
    finally:
        lens2.close()


def test_profile_modes_on_demand_decorator_header_and_off() -> None:
    app, _db, lens = build_bookstore_app(
        storage="memory",
        lens_kwargs={"profile_mode": "on_demand"},
    )
    try:
        client = app.test_client()
        # Regular request -> no profile
        r1 = client.get("/api/books/search?q=Book")
        rec1 = lens.storage.get(r1.headers["X-Lens-Request-Id"])
        assert rec1 is not None and rec1.profile is None

        # On-demand header -> profiled!
        r2 = client.get("/api/books/search?q=Book", headers={"X-Lens-Profile": "1"})
        rec2 = lens.storage.get(r2.headers["X-Lens-Request-Id"])
        assert rec2 is not None and rec2.profile is not None

        # @lens.profile decorated view -> profiled!
        r3 = client.get("/api/profiled")
        rec3 = lens.storage.get(r3.headers["X-Lens-Request-Id"])
        assert rec3 is not None and rec3.profile is not None
        assert any(fn.function == "profiled_only" for fn in rec3.profile.functions)
    finally:
        lens.close()


def test_disabled_sampling_ignore_paths_and_authorize_callback() -> None:
    # Disabled mode
    app_off = Flask(__name__)
    lens_off = Lens(app_off, enabled=False, storage="memory")
    try:

        @app_off.get("/ping")
        def ping() -> str:
            return "pong"

        resp = app_off.test_client().get("/ping")
        assert resp.status_code == 200
        assert "X-Lens-Request-Id" not in resp.headers
        assert app_off.test_client().get("/__lens/").status_code == 404
    finally:
        lens_off.close()

    # Authorization callback + ignore paths + sample_rate=0
    app_sec = Flask(__name__)
    lens_sec = Lens(
        app_sec,
        storage="memory",
        url_prefix="/admin-lens",
        ignore_paths=("/healthz",),
        authorize=lambda req: req.headers.get("X-Admin") == "yes",
        capture_outgoing_http=False,
    )
    try:

        @app_sec.get("/healthz")
        def healthz() -> str:
            return "ok"

        @app_sec.get("/work")
        def work() -> object:
            return jsonify({"ok": True})

        client = app_sec.test_client()
        client.get("/healthz")
        client.get("/work")
        assert lens_sec.storage.overview_stats()["total_requests"] == 1

        # Unauthorized UI & API -> 403
        assert client.get("/admin-lens/").status_code == 403
        assert client.get("/admin-lens/api/health").status_code == 403
        # Authorized -> 200
        assert client.get("/admin-lens/", headers={"X-Admin": "yes"}).status_code == 200
        assert client.get("/admin-lens/api/health", headers={"X-Admin": "yes"}).status_code == 200
    finally:
        lens_sec.close()


def test_config_validation_and_flask_config_loading() -> None:
    app = Flask(__name__)
    app.config["LENS_URL_PREFIX"] = "debug-lens/"
    app.config["LENS_SLOW_QUERY_MS"] = 25.0
    app.config["LENS_IGNORE_PATHS"] = ["/metrics"]
    cfg = LensConfig.from_flask_config(app)
    assert cfg.url_prefix == "/debug-lens"
    assert cfg.slow_query_ms == 25.0
    assert cfg.ignore_paths == ("/metrics",)

    with pytest.raises(ValueError, match="sample_rate"):
        LensConfig.from_flask_config(app, overrides={"sample_rate": 1.5})
    with pytest.raises(ValueError, match="profile_mode"):
        LensConfig.from_flask_config(app, overrides={"profile_mode": "invalid"})
    with pytest.raises(ValueError, match="storage"):
        LensConfig.from_flask_config(app, overrides={"storage": "redis"})
    with pytest.raises(ValueError, match="Unknown LensConfig option"):
        LensConfig.from_flask_config(app, overrides={"does_not_exist": True})
