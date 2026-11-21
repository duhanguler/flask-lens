"""Tests for outgoing HTTP instrumentation (httpx sync/async and urllib3)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import urllib3
from flask import Flask, jsonify

from flask_lens import Lens
from flask_lens.redaction import REDACTED


def test_httpx_sync_and_async_outgoing_http_captured() -> None:
    app = Flask(__name__)
    lens = Lens(
        app,
        storage="memory",
        profile_mode="off",
        capture_sql=False,
        capture_outgoing_http=True,
    )
    try:
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={"service": "payments", "path": req.url.path})
        )

        @app.get("/checkout")
        def checkout() -> object:
            with httpx.Client(transport=transport) as client:
                r = client.post(
                    "https://api.payments.local/v1/charge",
                    headers={"Authorization": "Bearer secret-stripe-key"},
                )
            return jsonify(r.json())

        @app.get("/checkout-async")
        def checkout_async() -> object:
            async def _call() -> dict[str, Any]:
                async with httpx.AsyncClient(transport=transport) as client:
                    r = await client.get("https://api.payments.local/v1/status")
                    return dict(r.json())

            return jsonify(asyncio.run(_call()))

        client = app.test_client()
        resp1 = client.get("/checkout")
        assert resp1.status_code == 200
        rec1 = lens.storage.get(resp1.headers["X-Lens-Request-Id"])
        assert rec1 is not None
        assert len(rec1.outgoing_http) == 1
        call1 = rec1.outgoing_http[0]
        assert call1.method == "POST"
        assert call1.url == "https://api.payments.local/v1/charge"
        assert call1.status_code == 200
        assert call1.library == "httpx"
        assert call1.request_headers["authorization"] == REDACTED

        resp2 = client.get("/checkout-async")
        assert resp2.status_code == 200
        rec2 = lens.storage.get(resp2.headers["X-Lens-Request-Id"])
        assert rec2 is not None
        assert len(rec2.outgoing_http) == 1
        assert rec2.outgoing_http[0].method == "GET"
        assert rec2.outgoing_http[0].status_code == 200
    finally:
        lens.close()


def test_urllib3_outgoing_http_captured(monkeypatch: Any) -> None:
    class _FakeResp:
        def __init__(self) -> None:
            self.status = 201
            self.headers = {"X-Upstream": "ok", "Set-Cookie": "sid=secret"}

    from urllib3.connectionpool import HTTPConnectionPool

    # Patch the underlying method BEFORE Lens installs its wrapper
    monkeypatch.setattr(
        HTTPConnectionPool, "urlopen", lambda self, method, url, *a, **kw: _FakeResp()
    )

    app = Flask(__name__)
    lens = Lens(
        app,
        storage="memory",
        profile_mode="off",
        capture_sql=False,
        capture_outgoing_http=True,
    )
    try:

        @app.get("/notify")
        def notify() -> object:
            pool = urllib3.HTTPSConnectionPool("hooks.example.test", 443)
            resp = pool.urlopen("POST", "/webhook", headers={"X-API-Key": "secret-hook-key"})
            return jsonify({"upstream_status": resp.status})

        r = app.test_client().get("/notify")
        assert r.status_code == 200
        rec = lens.storage.get(r.headers["X-Lens-Request-Id"])
        assert rec is not None
        assert len(rec.outgoing_http) == 1
        call = rec.outgoing_http[0]
        assert call.method == "POST"
        assert call.url == "https://hooks.example.test/webhook"
        assert call.status_code == 201
        assert call.library == "urllib3"
        assert call.request_headers["X-API-Key"] == REDACTED
        assert call.response_headers["Set-Cookie"] == REDACTED
    finally:
        lens.close()
