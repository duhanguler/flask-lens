"""Optional outgoing HTTP instrumentation for urllib3 (requests) and httpx."""

from __future__ import annotations

import threading
import time
import urllib.parse
import uuid
from typing import Any

from flask_lens.collectors.context import get_active_request, is_capture_suspended
from flask_lens.models import OutgoingHTTP
from flask_lens.redaction import redact_headers

_LOCK = threading.Lock()
_REF_COUNT = 0

_ORIG_URLLIB3_URLOPEN: Any = None
_ORIG_HTTPX_SEND: Any = None
_ORIG_HTTPX_ASYNC_SEND: Any = None


def install_http_hooks() -> None:
    """Ref-counted installation of urllib3 and httpx outgoing HTTP wrappers."""
    global _REF_COUNT, _ORIG_URLLIB3_URLOPEN, _ORIG_HTTPX_SEND, _ORIG_HTTPX_ASYNC_SEND
    with _LOCK:
        _REF_COUNT += 1
        if _REF_COUNT > 1:
            return

        _install_urllib3()
        _install_httpx()


def uninstall_http_hooks() -> None:
    """Decrement ref count and restore original HTTP methods when no Lens instances remain."""
    global _REF_COUNT, _ORIG_URLLIB3_URLOPEN, _ORIG_HTTPX_SEND, _ORIG_HTTPX_ASYNC_SEND
    with _LOCK:
        if _REF_COUNT <= 0:
            return
        _REF_COUNT -= 1
        if _REF_COUNT > 0:
            return

        if _ORIG_URLLIB3_URLOPEN is not None:
            try:
                from urllib3.connectionpool import HTTPConnectionPool

                HTTPConnectionPool.urlopen = _ORIG_URLLIB3_URLOPEN  # type: ignore[method-assign]
            except Exception:
                pass
            _ORIG_URLLIB3_URLOPEN = None

        if _ORIG_HTTPX_SEND is not None or _ORIG_HTTPX_ASYNC_SEND is not None:
            try:
                import httpx

                if _ORIG_HTTPX_SEND is not None:
                    httpx.Client.send = _ORIG_HTTPX_SEND  # type: ignore[method-assign]
                if _ORIG_HTTPX_ASYNC_SEND is not None:
                    httpx.AsyncClient.send = _ORIG_HTTPX_ASYNC_SEND  # type: ignore[method-assign]
            except Exception:
                pass
            _ORIG_HTTPX_SEND = None
            _ORIG_HTTPX_ASYNC_SEND = None


def _install_urllib3() -> None:
    global _ORIG_URLLIB3_URLOPEN
    try:
        from urllib3.connectionpool import HTTPConnectionPool
    except ImportError:
        return

    orig = HTTPConnectionPool.urlopen
    _ORIG_URLLIB3_URLOPEN = orig

    def _wrapped_urlopen(
        self: Any,
        method: str,
        url: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        ctx = get_active_request()
        if ctx is None or is_capture_suspended() or not ctx.config.capture_outgoing_http:
            return orig(self, method, url, *args, **kwargs)

        scheme = getattr(self, "scheme", "http")
        host = getattr(self, "host", "")
        port = getattr(self, "port", None)
        full_url = _build_urllib3_url(scheme, host, port, url)
        raw_req_headers = kwargs.get("headers") or {}
        req_headers = redact_headers(
            {str(k): str(v) for k, v in dict(raw_req_headers).items()},
            ctx.config.sensitive_headers,
            enabled=ctx.config.redact_sensitive,
        )

        offset_ms = ctx.elapsed_ms()
        start = time.perf_counter()
        status_code: int | None = None
        resp_headers: dict[str, str] = {}
        error_msg: str | None = None
        try:
            response = orig(self, method, url, *args, **kwargs)
            status_code = getattr(response, "status", None)
            raw_resp_headers = getattr(response, "headers", None) or {}
            resp_headers = redact_headers(
                {str(k): str(v) for k, v in dict(raw_resp_headers).items()},
                ctx.config.sensitive_headers,
                enabled=ctx.config.redact_sensitive,
            )
            return response
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000.0, 3)
            ctx.outgoing_http.append(
                OutgoingHTTP(
                    id=uuid.uuid4().hex[:12],
                    method=str(method).upper(),
                    url=full_url,
                    host=str(host),
                    status_code=status_code,
                    duration_ms=duration_ms,
                    offset_ms=offset_ms,
                    request_headers=req_headers,
                    response_headers=resp_headers,
                    error=error_msg,
                    library="urllib3",
                )
            )

    HTTPConnectionPool.urlopen = _wrapped_urlopen  # type: ignore[method-assign]


def _install_httpx() -> None:
    global _ORIG_HTTPX_SEND, _ORIG_HTTPX_ASYNC_SEND
    try:
        import httpx
    except ImportError:
        return

    orig_send = httpx.Client.send
    orig_async_send = httpx.AsyncClient.send
    _ORIG_HTTPX_SEND = orig_send
    _ORIG_HTTPX_ASYNC_SEND = orig_async_send

    def _wrapped_send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        ctx = get_active_request()
        if ctx is None or is_capture_suspended() or not ctx.config.capture_outgoing_http:
            return orig_send(self, request, *args, **kwargs)

        url_str = str(request.url)
        host = str(getattr(request.url, "host", ""))
        req_headers = redact_headers(
            dict(request.headers.items()),
            ctx.config.sensitive_headers,
            enabled=ctx.config.redact_sensitive,
        )
        offset_ms = ctx.elapsed_ms()
        start = time.perf_counter()
        status_code: int | None = None
        resp_headers: dict[str, str] = {}
        error_msg: str | None = None
        try:
            response = orig_send(self, request, *args, **kwargs)
            status_code = response.status_code
            resp_headers = redact_headers(
                dict(response.headers.items()),
                ctx.config.sensitive_headers,
                enabled=ctx.config.redact_sensitive,
            )
            return response
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000.0, 3)
            ctx.outgoing_http.append(
                OutgoingHTTP(
                    id=uuid.uuid4().hex[:12],
                    method=str(request.method).upper(),
                    url=url_str,
                    host=host,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    offset_ms=offset_ms,
                    request_headers=req_headers,
                    response_headers=resp_headers,
                    error=error_msg,
                    library="httpx",
                )
            )

    async def _wrapped_async_send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        ctx = get_active_request()
        if ctx is None or is_capture_suspended() or not ctx.config.capture_outgoing_http:
            return await orig_async_send(self, request, *args, **kwargs)

        url_str = str(request.url)
        host = str(getattr(request.url, "host", ""))
        req_headers = redact_headers(
            dict(request.headers.items()),
            ctx.config.sensitive_headers,
            enabled=ctx.config.redact_sensitive,
        )
        offset_ms = ctx.elapsed_ms()
        start = time.perf_counter()
        status_code: int | None = None
        resp_headers: dict[str, str] = {}
        error_msg: str | None = None
        try:
            response = await orig_async_send(self, request, *args, **kwargs)
            status_code = response.status_code
            resp_headers = redact_headers(
                dict(response.headers.items()),
                ctx.config.sensitive_headers,
                enabled=ctx.config.redact_sensitive,
            )
            return response
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000.0, 3)
            ctx.outgoing_http.append(
                OutgoingHTTP(
                    id=uuid.uuid4().hex[:12],
                    method=str(request.method).upper(),
                    url=url_str,
                    host=host,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    offset_ms=offset_ms,
                    request_headers=req_headers,
                    response_headers=resp_headers,
                    error=error_msg,
                    library="httpx",
                )
            )

    httpx.Client.send = _wrapped_send  # type: ignore[method-assign]
    httpx.AsyncClient.send = _wrapped_async_send  # type: ignore[method-assign]


def _build_urllib3_url(scheme: str, host: str, port: int | None, url: str) -> str:
    if url.startswith(("http://", "https://")):
        return url
    default_port = 443 if scheme == "https" else 80
    netloc = host if (port is None or port == default_port) else f"{host}:{port}"
    path = url if url.startswith("/") else f"/{url}"
    return urllib.parse.urlunsplit((scheme, netloc, path, "", ""))
