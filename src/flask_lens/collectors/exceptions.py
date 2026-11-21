"""Structured exception capture with source context and redacted local variables."""

from __future__ import annotations

import hashlib
import linecache
import os
import traceback
from typing import TYPE_CHECKING

from flask_lens.models import ExceptionFrame, ExceptionInfo
from flask_lens.redaction import redact_locals
from flask_lens.sql.listener import _is_user_frame, _shorten_path

if TYPE_CHECKING:
    from flask_lens.config import LensConfig


def capture_exception(exc: BaseException, config: LensConfig) -> ExceptionInfo:
    """Convert an unhandled exception into a structured, secret-redacted :class:`ExceptionInfo`."""
    exc_type = f"{type(exc).__module__}.{type(exc).__qualname__}"
    if exc_type.startswith("builtins."):
        exc_type = type(exc).__qualname__
    message = str(exc)
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    norm_root = os.path.abspath(config.app_root) if config.app_root else None
    frames: list[ExceptionFrame] = []
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        lineno = tb.tb_lineno
        filename = frame.f_code.co_filename
        func_name = frame.f_code.co_name

        is_user = _is_user_frame(filename, norm_root)
        display_path = _shorten_path(filename, norm_root)
        code_line = linecache.getline(filename, lineno).rstrip()

        context_lines: list[tuple[int, str]] = []
        start_ln = max(1, lineno - 4)
        for ln in range(start_ln, lineno + 5):
            text = linecache.getline(filename, ln)
            if text:
                context_lines.append((ln, text.rstrip("\n")))

        locals_preview = redact_locals(
            frame.f_locals,
            config.sensitive_fields,
            enabled=config.redact_sensitive,
        )

        frames.append(
            ExceptionFrame(
                filename=display_path,
                lineno=lineno,
                function=func_name,
                code_line=code_line.strip(),
                context_lines=context_lines,
                locals_preview=locals_preview,
                is_user_code=is_user,
            )
        )
        tb = tb.tb_next

    # Group recurring exceptions by (type + deepest user frame, or deepest frame overall)
    anchor = next((f for f in reversed(frames) if f.is_user_code), frames[-1] if frames else None)
    fp_source = (
        f"{exc_type}:{anchor.filename}:{anchor.lineno}:{anchor.function}" if anchor else exc_type
    )
    fingerprint = hashlib.sha1(fp_source.encode("utf-8")).hexdigest()[:16]

    return ExceptionInfo(
        exc_type=exc_type,
        message=message,
        traceback_text=tb_text,
        frames=frames,
        fingerprint=fingerprint,
    )
