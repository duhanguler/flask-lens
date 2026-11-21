"""Thread-safe and async-safe per-request collector state via ContextVar."""

from __future__ import annotations

import contextlib
import contextvars
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from flask_lens.models import CustomSpan, ExceptionInfo, LogEntry, OutgoingHTTP, SQLQuery

if TYPE_CHECKING:
    from flask_lens.collectors.profiler import RequestProfiler
    from flask_lens.config import LensConfig


@dataclass
class RequestContext:
    """Mutable collector state bound to the lifetime of a single Flask request."""

    request_id: str
    config: LensConfig
    start_time: float
    start_perf: float
    is_replay: bool = False
    replayed_from: str | None = None
    force_profile: bool = False
    profiler: RequestProfiler | None = None

    sql_queries: list[SQLQuery] = field(default_factory=list)
    outgoing_http: list[OutgoingHTTP] = field(default_factory=list)
    logs: list[LogEntry] = field(default_factory=list)
    spans: list[CustomSpan] = field(default_factory=list)
    templates: list[str] = field(default_factory=list)
    exception: ExceptionInfo | None = None

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.start_perf) * 1000.0, 3)


_ACTIVE_REQUEST: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "flask_lens_active_request",
    default=None,
)
_SUSPENDED: contextvars.ContextVar[int] = contextvars.ContextVar(
    "flask_lens_capture_suspended",
    default=0,
)


def get_active_request() -> RequestContext | None:
    return _ACTIVE_REQUEST.get()


def set_active_request(ctx: RequestContext | None) -> contextvars.Token[RequestContext | None]:
    return _ACTIVE_REQUEST.set(ctx)


def reset_active_request(token: contextvars.Token[RequestContext | None]) -> None:
    _ACTIVE_REQUEST.reset(token)


def is_capture_suspended() -> bool:
    return _SUSPENDED.get() > 0


@contextlib.contextmanager
def suspend_capture() -> Iterator[None]:
    """Temporarily pause SQL/HTTP/log collection (used internally by EXPLAIN & storage)."""
    token = _SUSPENDED.set(_SUSPENDED.get() + 1)
    try:
        yield
    finally:
        _SUSPENDED.reset(token)


@contextlib.contextmanager
def span(name: str, **metadata: Any) -> Iterator[dict[str, Any]]:
    """Record a timed custom span on the current request's waterfall timeline.

    Usage::

        with lens.span("stripe.charge", customer_id=42) as meta:
            result = charge_customer()
            meta["status"] = result.status
    """
    ctx = get_active_request()
    meta: dict[str, Any] = dict(metadata)
    if ctx is None:
        yield meta
        return

    start_offset = ctx.elapsed_ms()
    start_perf = time.perf_counter()
    try:
        yield meta
    finally:
        duration_ms = round((time.perf_counter() - start_perf) * 1000.0, 3)
        ctx.spans.append(
            CustomSpan(
                name=name,
                kind="span",
                offset_ms=start_offset,
                duration_ms=duration_ms,
                metadata=meta,
            )
        )


def event(name: str, metadata: dict[str, Any] | None = None) -> None:
    """Record an instantaneous custom event marker on the current request."""
    ctx = get_active_request()
    if ctx is None:
        return
    ctx.spans.append(
        CustomSpan(
            name=name,
            kind="event",
            offset_ms=ctx.elapsed_ms(),
            duration_ms=0.0,
            metadata=dict(metadata) if metadata else {},
        )
    )
