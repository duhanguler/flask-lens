"""Tests for header, JSON/form body, SQL params, and exception local redaction."""

from __future__ import annotations

import json

from flask_lens.config import DEFAULT_SENSITIVE_FIELDS, DEFAULT_SENSITIVE_HEADERS
from flask_lens.redaction import (
    REDACTED,
    redact_body,
    redact_headers,
    redact_locals,
    redact_sql_params,
)


def test_redact_headers() -> None:
    raw = {
        "Authorization": "Bearer super-secret-jwt",
        "Cookie": "session=abc123",
        "X-API-Key": "key_live_999",
        "Content-Type": "application/json",
    }
    redacted = redact_headers(raw, DEFAULT_SENSITIVE_HEADERS, enabled=True)
    assert redacted["Authorization"] == REDACTED
    assert redacted["Cookie"] == REDACTED
    assert redacted["X-API-Key"] == REDACTED
    assert redacted["Content-Type"] == "application/json"

    untouched = redact_headers(raw, DEFAULT_SENSITIVE_HEADERS, enabled=False)
    assert untouched["Authorization"] == "Bearer super-secret-jwt"


def test_redact_json_and_form_bodies() -> None:
    payload = {
        "email": "user@example.com",
        "password": "hunter2",
        "nested": {"stripe_api_key": "sk_live_123", "items": [{"token": "tok_abc", "qty": 2}]},
    }
    cleaned = redact_body(
        json.dumps(payload).encode("utf-8"),
        "application/json",
        DEFAULT_SENSITIVE_FIELDS,
    )
    assert cleaned is not None
    parsed = json.loads(cleaned)
    assert parsed["email"] == "user@example.com"
    assert parsed["password"] == REDACTED
    assert parsed["nested"]["stripe_api_key"] == REDACTED
    assert parsed["nested"]["items"][0]["token"] == REDACTED
    assert parsed["nested"]["items"][0]["qty"] == 2

    form_cleaned = redact_body(
        b"username=alice&password=secret123&role=admin",
        "application/x-www-form-urlencoded",
        DEFAULT_SENSITIVE_FIELDS,
    )
    assert form_cleaned is not None
    assert "secret123" not in form_cleaned
    assert "username=alice" in form_cleaned
    assert "role=admin" in form_cleaned


def test_redact_binary_and_truncated_bodies() -> None:
    binary = redact_body(b"\x89PNG\r\n\x1a\n\x00\x00", "image/png", DEFAULT_SENSITIVE_FIELDS)
    assert binary is not None and binary.startswith("<binary image/png")

    long_text = b"a" * 500
    truncated = redact_body(long_text, "text/plain", DEFAULT_SENSITIVE_FIELDS, max_bytes=100)
    assert truncated is not None and "truncated at 100 bytes" in truncated


def test_redact_sql_params_and_locals() -> None:
    params = {"user_id": 7, "password_hash": "pbkdf2$secret", "api_key": "k_123"}
    cleaned = redact_sql_params(params, DEFAULT_SENSITIVE_FIELDS)
    assert cleaned["user_id"] == 7
    assert cleaned["password_hash"] == REDACTED
    assert cleaned["api_key"] == REDACTED

    big_list = list(range(60))
    capped = redact_sql_params(big_list, DEFAULT_SENSITIVE_FIELDS)
    assert len(capped) == 51
    assert "10 more" in str(capped[-1])

    loc = redact_locals(
        {"__builtins__": {}, "stripe_secret": "sk_1", "count": 5, "huge": "x" * 300},
        DEFAULT_SENSITIVE_FIELDS,
    )
    assert "__builtins__" not in loc
    assert loc["stripe_secret"] == REDACTED
    assert loc["count"] == "5"
    assert loc["huge"].endswith("...")
