"""Secret and PII redaction helpers for headers, bodies, SQL params, and locals."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"


def is_sensitive_key(key: str, sensitive_fields: frozenset[str]) -> bool:
    """Return True if *key* matches or contains a known sensitive token."""
    normalized = key.strip().lower().replace("-", "_")
    if normalized in sensitive_fields:
        return True
    return any(
        token in normalized
        for token in (
            "password",
            "passwd",
            "secret",
            "api_key",
            "apikey",
            "access_token",
            "refresh_token",
            "private_key",
            "auth_token",
        )
    )


def redact_headers(
    headers: Mapping[str, str],
    sensitive_headers: frozenset[str],
    *,
    enabled: bool = True,
) -> dict[str, str]:
    """Return a copy of HTTP headers with sensitive values replaced."""
    if not enabled:
        return {str(k): str(v) for k, v in headers.items()}
    result: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in sensitive_headers:
            result[str(key)] = REDACTED
        else:
            result[str(key)] = str(value)
    return result


def redact_query_string(
    query_string: str,
    sensitive_fields: frozenset[str],
    *,
    enabled: bool = True,
) -> str:
    """Redact sensitive key/value pairs inside a URL query string."""
    if not enabled or not query_string:
        return query_string
    pairs = urllib.parse.parse_qsl(query_string, keep_blank_values=True)
    if not pairs:
        return query_string
    changed = False
    cleaned: list[tuple[str, str]] = []
    for k, v in pairs:
        if is_sensitive_key(k, sensitive_fields):
            cleaned.append((k, REDACTED))
            changed = True
        else:
            cleaned.append((k, v))
    if not changed:
        return query_string
    return urllib.parse.urlencode(cleaned)


def redact_url(
    url: str,
    sensitive_fields: frozenset[str],
    *,
    enabled: bool = True,
) -> str:
    """Redact userinfo credentials and sensitive query parameters in a full URL."""
    if not enabled or not url:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url

    netloc = parts.netloc
    if "@" in netloc:
        userinfo, hostport = netloc.rsplit("@", 1)
        if ":" in userinfo:
            user, _pwd = userinfo.split(":", 1)
            netloc = f"{user}:{REDACTED}@{hostport}"
        else:
            netloc = f"{REDACTED}@{hostport}"

    query = redact_query_string(parts.query, sensitive_fields, enabled=True)
    if netloc == parts.netloc and query == parts.query:
        return url
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact_data(
    value: Any,
    sensitive_fields: frozenset[str],
    *,
    enabled: bool = True,
    depth: int = 0,
    max_depth: int = 8,
) -> Any:
    """Recursively redact sensitive keys inside dicts/lists."""
    if not enabled or depth > max_depth:
        return value
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if is_sensitive_key(key_str, sensitive_fields):
                sanitized[key_str] = REDACTED
            else:
                sanitized[key_str] = redact_data(
                    v,
                    sensitive_fields,
                    enabled=enabled,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
        return sanitized
    if isinstance(value, list):
        return [
            redact_data(
                item, sensitive_fields, enabled=enabled, depth=depth + 1, max_depth=max_depth
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return [
            redact_data(
                item, sensitive_fields, enabled=enabled, depth=depth + 1, max_depth=max_depth
            )
            for item in value
        ]
    return value


def redact_body(
    raw_bytes: bytes,
    content_type: str | None,
    sensitive_fields: frozenset[str],
    *,
    max_bytes: int = 65_536,
    enabled: bool = True,
) -> str | None:
    """Safely decode, size-cap, and redact JSON or form-encoded HTTP bodies."""
    if not raw_bytes:
        return None
    truncated = len(raw_bytes) > max_bytes
    snippet = raw_bytes[:max_bytes]
    ctype = (content_type or "").lower()

    if any(
        binary in ctype
        for binary in (
            "image/",
            "audio/",
            "video/",
            "application/octet-stream",
            "application/pdf",
            "application/zip",
            "font/",
        )
    ):
        return f"<binary {ctype.split(';')[0]} ({len(raw_bytes)} bytes)>"

    try:
        text = snippet.decode("utf-8")
    except UnicodeDecodeError:
        return f"<binary payload ({len(raw_bytes)} bytes)>"

    if enabled and "application/json" in ctype and not truncated:
        try:
            parsed = json.loads(text)
            cleaned = redact_data(parsed, sensitive_fields, enabled=True)
            return json.dumps(cleaned, ensure_ascii=False, indent=2)
        except (ValueError, TypeError):
            pass

    if enabled and "application/x-www-form-urlencoded" in ctype and not truncated:
        pairs = urllib.parse.parse_qsl(text, keep_blank_values=True)
        cleaned_pairs = [
            (k, REDACTED if is_sensitive_key(k, sensitive_fields) else v) for k, v in pairs
        ]
        return urllib.parse.urlencode(cleaned_pairs)

    if truncated:
        return f"{text}\n... [truncated at {max_bytes} bytes of {len(raw_bytes)} total]"
    return text


def redact_sql_params(
    params: Any,
    sensitive_fields: frozenset[str],
    *,
    enabled: bool = True,
) -> Any:
    """Serialize and redact SQLAlchemy bind parameters safely for JSON storage."""
    if params is None:
        return None
    if isinstance(params, Mapping):
        out: dict[str, Any] = {}
        for k, v in params.items():
            key_str = str(k)
            if enabled and is_sensitive_key(key_str, sensitive_fields):
                out[key_str] = REDACTED
            else:
                out[key_str] = _safe_primitive(v)
        return out
    if isinstance(params, (list, tuple)):
        if len(params) > 50:
            items = [_safe_primitive(item) for item in params[:50]]
            items.append(f"... ({len(params) - 50} more)")
            return items
        return [_safe_primitive(item) for item in params]
    return _safe_primitive(params)


def redact_locals(
    frame_locals: Mapping[str, Any],
    sensitive_fields: frozenset[str],
    *,
    enabled: bool = True,
    max_items: int = 25,
    max_repr_len: int = 160,
) -> dict[str, str]:
    """Return a bounded, redacted string preview of exception frame local variables."""
    preview: dict[str, str] = {}
    count = 0
    for name, val in frame_locals.items():
        if name.startswith("__") and name.endswith("__"):
            continue
        if count >= max_items:
            preview["..."] = f"{len(frame_locals) - count} more locals omitted"
            break
        if enabled and is_sensitive_key(name, sensitive_fields):
            preview[name] = REDACTED
        else:
            try:
                rendered = repr(val)
            except Exception:
                rendered = f"<{type(val).__name__} (repr failed)>"
            if len(rendered) > max_repr_len:
                rendered = rendered[: max_repr_len - 3] + "..."
            preview[name] = rendered
        count += 1
    return preview


def _safe_primitive(val: Any) -> Any:
    if val is None or isinstance(val, (bool, int, float, str)):
        if isinstance(val, str) and len(val) > 256:
            return val[:253] + "..."
        return val
    if isinstance(val, bytes):
        return f"<bytes len={len(val)}>"
    if isinstance(val, (list, tuple)):
        return [_safe_primitive(x) for x in val[:25]]
    if isinstance(val, Mapping):
        return {str(k): _safe_primitive(v) for k, v in list(val.items())[:25]}
    try:
        rendered = str(val)
    except Exception:
        return f"<{type(val).__name__}>"
    return rendered[:253] + "..." if len(rendered) > 256 else rendered
