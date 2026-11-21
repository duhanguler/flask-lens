"""flask-lens: Real-time observability, SQL/N+1 profiler, flamegraph, and pytest guardrails for Flask."""

from __future__ import annotations

from flask_lens.collectors.context import event, span
from flask_lens.config import LensConfig
from flask_lens.extension import Lens, LensState
from flask_lens.models import (
    CapturedRequest,
    CustomSpan,
    ExceptionInfo,
    LogEntry,
    NPlusOneGroup,
    OutgoingHTTP,
    ProfileData,
    SQLQuery,
    StackFrame,
)
from flask_lens.storage.memory import MemoryStorage
from flask_lens.storage.sqlite import SQLiteStorage
from flask_lens.testing import (
    QueryAssertionError,
    QueryCapture,
    assert_max_queries,
    assert_no_n_plus_one,
    assert_num_queries,
    capture_queries,
)

__version__ = "0.1.0"

__all__ = [
    "CapturedRequest",
    "CustomSpan",
    "ExceptionInfo",
    "Lens",
    "LensConfig",
    "LensState",
    "LogEntry",
    "MemoryStorage",
    "NPlusOneGroup",
    "OutgoingHTTP",
    "ProfileData",
    "QueryAssertionError",
    "QueryCapture",
    "SQLQuery",
    "SQLiteStorage",
    "StackFrame",
    "__version__",
    "assert_max_queries",
    "assert_no_n_plus_one",
    "assert_num_queries",
    "capture_queries",
    "event",
    "span",
]
