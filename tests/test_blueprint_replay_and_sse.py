"""End-to-end tests for the Lens Blueprint REST API, EXPLAIN, Replay, Diff, UI, and SSE."""

from __future__ import annotations

import threading
import time

from flask import Flask
from flask_sqlalchemy import SQLAlchemy

from flask_lens import Lens


def test_blueprint_ui_assets_and_rest_api(
    bookstore_bundle: tuple[Flask, SQLAlchemy, Lens],
) -> None:
    app, _db, lens = bookstore_bundle
    client = app.test_client()

    # Embedded UI & static assets
    ui_resp = client.get("/__lens/")
    assert ui_resp.status_code == 200
    assert 'content="/__lens"' in ui_resp.get_data(as_text=True)
    assert client.get("/__lens/assets/styles.css").status_code == 200
    assert client.get("/__lens/assets/app.js").status_code == 200

    # Health
    health = client.get("/__lens/api/health").get_json()
    assert health["status"] == "ok"
    assert "default" in health["watched_engines"]

    # Fire N+1, optimized, search, and boom requests
    r_n1 = client.get("/api/authors")
    id_n1 = r_n1.headers["X-Lens-Request-Id"]

    r_opt = client.get("/api/authors?optimized=1")
    id_opt = r_opt.headers["X-Lens-Request-Id"]

    r_search = client.get("/api/books/search?q=Book")
    id_search = r_search.headers["X-Lens-Request-Id"]

    client.get("/api/boom")

    # Overview & lists (Lens own API routes must NOT pollute request counts!)
    overview = client.get("/__lens/api/overview").get_json()
    assert overview["total_requests"] == 4
    assert overview["n_plus_one_requests"] == 1
    assert overview["exception_count"] == 1

    reqs = client.get("/__lens/api/requests?n_plus_one=1").get_json()
    assert reqs["total"] == 1
    assert reqs["items"][0]["id"] == id_n1

    detail = client.get(f"/__lens/api/requests/{id_search}").get_json()
    assert detail["sql_count"] == 1
    qid = detail["sql_queries"][0]["id"]

    # EXPLAIN endpoint
    explain_resp = client.post(f"/__lens/api/requests/{id_search}/explain/{qid}")
    assert explain_resp.status_code == 200
    explain_data = explain_resp.get_json()
    assert explain_data["supported"] is True
    assert explain_data["dialect"] == "sqlite"
    assert len(explain_data["plan_lines"]) >= 1
    assert any("Full table scan" in w for w in explain_data["warnings"])

    # Compare endpoint (N+1 vs Optimized)
    cmp_resp = client.get(f"/__lens/api/compare?baseline={id_n1}&candidate={id_opt}")
    assert cmp_resp.status_code == 200
    cmp_data = cmp_resp.get_json()
    assert cmp_data["deltas"]["sql_count"] == -5
    assert cmp_data["deltas"]["n_plus_one_resolved"] is True

    # Endpoints, SQL Explorer, Exceptions
    assert len(client.get("/__lens/api/endpoints").get_json()["items"]) >= 3
    assert len(client.get("/__lens/api/sql").get_json()["items"]) >= 2
    exc_items = client.get("/__lens/api/exceptions").get_json()["items"]
    assert len(exc_items) == 1 and exc_items[0]["exc_type"] == "ZeroDivisionError"

    # 404 for unknown request
    assert client.get("/__lens/api/requests/does-not-exist").status_code == 404

    # Clear
    cleared = client.post("/__lens/api/clear").get_json()
    assert cleared["cleared"] == 4
    assert lens.storage.overview_stats()["total_requests"] == 0


def test_replay_endpoint_and_security_guards(
    bookstore_bundle: tuple[Flask, SQLAlchemy, Lens],
) -> None:
    app, _db, lens = bookstore_bundle
    client = app.test_client()

    orig_resp = client.get("/api/authors")
    orig_id = orig_resp.headers["X-Lens-Request-Id"]

    # Replay with ?optimized=1 and verify automatic diff against original
    replay_resp = client.post(
        "/__lens/api/replay",
        json={
            "method": "GET",
            "path": "/api/authors?optimized=1",
            "headers": {"X-Test": "1", "Authorization": "[REDACTED]"},
            "original_request_id": orig_id,
        },
    )
    assert replay_resp.status_code == 200
    payload = replay_resp.get_json()
    assert payload["status_code"] == 200
    assert payload["request"]["is_replay"] is True
    assert payload["request"]["replayed_from"] == orig_id
    assert payload["diff"]["deltas"]["n_plus_one_resolved"] is True
    assert payload["diff"]["deltas"]["sql_count"] == -5

    # Guard: cannot replay /__lens endpoints (anti-recursion)
    bad1 = client.post("/__lens/api/replay", json={"method": "GET", "path": "/__lens/api/health"})
    assert bad1.status_code == 400

    # Guard: cannot replay external URLs (anti-SSRF)
    bad2 = client.post("/__lens/api/replay", json={"method": "GET", "path": "https://example.com"})
    assert bad2.status_code == 400

    # Guard: disabled replay returns 403
    lens.config.allow_replay = False
    bad3 = client.post("/__lens/api/replay", json={"method": "GET", "path": "/api/authors"})
    assert bad3.status_code == 403


def test_sse_live_stream_delivers_events(
    bookstore_bundle: tuple[Flask, SQLAlchemy, Lens],
) -> None:
    app, _db, lens = bookstore_bundle
    client = app.test_client()

    def _emit_later() -> None:
        time.sleep(0.05)
        lens.state.broadcaster.publish({"id": "sse-test-1", "method": "GET", "path": "/sse"})

    t = threading.Thread(target=_emit_later)
    t.start()
    resp = client.get("/__lens/api/stream?max_events=1")
    body = resp.get_data(as_text=True)
    t.join(timeout=2.0)

    assert resp.status_code == 200
    assert "event: connected" in body
    assert "event: request" in body
    assert "sse-test-1" in body
