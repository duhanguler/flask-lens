"""SQL normalization, N+1 detection, EXPLAIN analysis, and SQLAlchemy listeners."""

from __future__ import annotations

from flask_lens.sql.analyzer import SQLAnalysisSummary, analyze_queries
from flask_lens.sql.explain import ExplainResult, is_safe_explainable_sql, run_explain
from flask_lens.sql.listener import (
    RawParamsCache,
    attach_engine,
    capture_stack_frames,
    discover_engines_from_app,
    get_registered_engine,
    resolve_engines,
)
from flask_lens.sql.normalizer import (
    NormalizedSQL,
    classify_operation,
    extract_tables,
    normalize_sql,
)

__all__ = [
    "ExplainResult",
    "NormalizedSQL",
    "RawParamsCache",
    "SQLAnalysisSummary",
    "analyze_queries",
    "attach_engine",
    "capture_stack_frames",
    "classify_operation",
    "discover_engines_from_app",
    "extract_tables",
    "get_registered_engine",
    "is_safe_explainable_sql",
    "normalize_sql",
    "resolve_engines",
    "run_explain",
]
