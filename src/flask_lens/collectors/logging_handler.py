"""Request-scoped Python logging handler with ref-counted global registration."""

from __future__ import annotations

import logging
import threading

from flask_lens.collectors.context import get_active_request, is_capture_suspended
from flask_lens.models import LogEntry

_LOG_LOCK = threading.Lock()
_LOG_REF_COUNT = 0
_SHARED_HANDLER: LensLogHandler | None = None


class LensLogHandler(logging.Handler):
    """Captures stdlib log records emitted during an active Flask request."""

    def emit(self, record: logging.LogRecord) -> None:
        if is_capture_suspended():
            return
        ctx = get_active_request()
        if ctx is None or not ctx.config.capture_logs:
            return
        if record.levelno < ctx.config.log_level:
            return
        # Avoid capturing noisy werkzeug access logs that duplicate request records.
        if record.name == "werkzeug" and record.levelno < logging.WARNING:
            return
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        ctx.logs.append(
            LogEntry(
                level=record.levelname,
                levelno=record.levelno,
                logger=record.name,
                message=message,
                offset_ms=ctx.elapsed_ms(),
                filename=record.filename,
                lineno=record.lineno,
                function=record.funcName,
            )
        )


def install_log_handler() -> LensLogHandler:
    """Attach a single shared :class:`LensLogHandler` to the root logger (ref-counted)."""
    global _LOG_REF_COUNT, _SHARED_HANDLER
    with _LOG_LOCK:
        _LOG_REF_COUNT += 1
        if _SHARED_HANDLER is None:
            _SHARED_HANDLER = LensLogHandler()
            logging.getLogger().addHandler(_SHARED_HANDLER)
        return _SHARED_HANDLER


def uninstall_log_handler() -> None:
    """Decrement ref count and remove the shared handler when no Lens instances remain."""
    global _LOG_REF_COUNT, _SHARED_HANDLER
    with _LOG_LOCK:
        if _LOG_REF_COUNT <= 0:
            return
        _LOG_REF_COUNT -= 1
        if _LOG_REF_COUNT == 0 and _SHARED_HANDLER is not None:
            logging.getLogger().removeHandler(_SHARED_HANDLER)
            _SHARED_HANDLER = None
