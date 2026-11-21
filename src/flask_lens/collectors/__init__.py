"""Request-scoped collectors for SQL, profiling, outgoing HTTP, logs, and exceptions."""

from __future__ import annotations

from flask_lens.collectors.context import (
    RequestContext,
    event,
    get_active_request,
    is_capture_suspended,
    reset_active_request,
    set_active_request,
    span,
    suspend_capture,
)
from flask_lens.collectors.exceptions import capture_exception
from flask_lens.collectors.http_outgoing import (
    install_http_hooks,
    uninstall_http_hooks,
)
from flask_lens.collectors.logging_handler import LensLogHandler
from flask_lens.collectors.profiler import RequestProfiler

__all__ = [
    "LensLogHandler",
    "RequestContext",
    "RequestProfiler",
    "capture_exception",
    "event",
    "get_active_request",
    "install_http_hooks",
    "is_capture_suspended",
    "reset_active_request",
    "set_active_request",
    "span",
    "suspend_capture",
    "uninstall_http_hooks",
]
