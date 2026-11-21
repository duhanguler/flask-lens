"""Request-scoped Python logging handler."""

from __future__ import annotations

import logging

from flask_lens.collectors.context import get_active_request, is_capture_suspended
from flask_lens.models import LogEntry


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
