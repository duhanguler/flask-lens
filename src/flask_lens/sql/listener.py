"""SQLAlchemy engine instrumentation and fast user-code stack-frame extraction."""

from __future__ import annotations

import linecache
import os
import sys
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from flask_lens.models import SQLQuery, StackFrame
from flask_lens.redaction import redact_sql_params
from flask_lens.sql.normalizer import normalize_sql

if TYPE_CHECKING:
    from flask import Flask


# Modules skipped when locating the user-code call site of an SQL query.
_SKIP_PATH_FRAGMENTS: tuple[str, ...] = (
    f"{os.sep}flask_lens{os.sep}",
    f"{os.sep}sqlalchemy{os.sep}",
    f"{os.sep}flask_sqlalchemy{os.sep}",
    f"{os.sep}flask{os.sep}",
    f"{os.sep}werkzeug{os.sep}",
    f"{os.sep}jinja2{os.sep}",
    f"{os.sep}click{os.sep}",
    f"{os.sep}contextlib.py",
    f"{os.sep}threading.py",
)

_STDLIB_PREFIXES: tuple[str, ...] = tuple(
    os.path.abspath(p)
    for p in {
        os.path.dirname(os.__file__),
        os.path.dirname(threading.__file__),
    }
    if p
)


class RawParamsCache:
    """Bounded thread-safe LRU cache holding raw DBAPI bind params for EXPLAIN."""

    def __init__(self, maxsize: int = 2048) -> None:
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[str, Any]] = OrderedDict()

    def put(self, query_id: str, engine_id: str, params: Any) -> None:
        with self._lock:
            self._data[query_id] = (engine_id, params)
            self._data.move_to_end(query_id)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def get(self, query_id: str) -> tuple[str, Any] | None:
        with self._lock:
            item = self._data.get(query_id)
            if item is not None:
                self._data.move_to_end(query_id)
            return item

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_ENGINE_LOCK = threading.Lock()
# Maps Engine -> (unique_engine_id, display_name)
_HOOKED_ENGINES: weakref.WeakKeyDictionary[Any, tuple[str, str]] = weakref.WeakKeyDictionary()
# Maps unique_engine_id -> Engine (never collides across multiple apps/engines!)
_ENGINES_BY_ID: weakref.WeakValueDictionary[str, Any] = weakref.WeakValueDictionary()
# Maps logical display_name -> most recently registered Engine (fallback)
_REGISTERED_ENGINES: weakref.WeakValueDictionary[str, Any] = weakref.WeakValueDictionary()
_QUERY_LISTENERS: list[Callable[[SQLQuery, str, Any], None]] = []
_RAW_PARAMS_CACHE = RawParamsCache()


def get_raw_params_cache() -> RawParamsCache:
    return _RAW_PARAMS_CACHE


def get_registered_engine(engine_key: str = "default") -> Any | None:
    with _ENGINE_LOCK:
        engine = _ENGINES_BY_ID.get(engine_key)
        if engine is not None:
            return engine
        engine = _REGISTERED_ENGINES.get(engine_key)
        if engine is not None:
            return engine
        for eng in _ENGINES_BY_ID.values():
            return eng
        return None


def register_query_sink(callback: Callable[[SQLQuery, str, Any], None]) -> None:
    """Register a process-wide query sink receiving ``(query, engine_id, raw_params)``."""
    with _ENGINE_LOCK:
        if callback not in _QUERY_LISTENERS:
            _QUERY_LISTENERS.append(callback)


def unregister_query_sink(callback: Callable[[SQLQuery, str, Any], None]) -> None:
    with _ENGINE_LOCK:
        if callback in _QUERY_LISTENERS:
            _QUERY_LISTENERS.remove(callback)


def attach_engine(engine: Any, name: str | None = None) -> str:
    """Idempotently attach SQLAlchemy cursor execution hooks to *engine*.

    Returns the unique ``engine_id`` for this engine instance. Safe to call
    repeatedly on the same engine and isolated across distinct engines even if
    they share the logical bind name ``"default"``.
    """
    from sqlalchemy import event  # optional dependency

    actual_engine = getattr(engine, "sync_engine", engine)
    with _ENGINE_LOCK:
        existing = _HOOKED_ENGINES.get(actual_engine)
        if existing is not None:
            existing_id, existing_name = existing
            _ENGINES_BY_ID[existing_id] = actual_engine
            _REGISTERED_ENGINES[name or existing_name] = actual_engine
            return existing_id

        display_name = name or "default"
        engine_id = f"eng_{uuid.uuid4().hex[:10]}"
        _HOOKED_ENGINES[actual_engine] = (engine_id, display_name)
        _ENGINES_BY_ID[engine_id] = actual_engine
        _REGISTERED_ENGINES[display_name] = actual_engine

    dialect_name = getattr(getattr(actual_engine, "dialect", None), "name", "unknown")

    @event.listens_for(actual_engine, "before_cursor_execute")
    def _before_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        stack: list[float] = conn.info.setdefault("_flask_lens_start_stack", [])
        stack.append(time.perf_counter())

    @event.listens_for(actual_engine, "after_cursor_execute")
    def _after_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        stack: list[float] = conn.info.get("_flask_lens_start_stack", [])
        start = stack.pop() if stack else time.perf_counter()
        duration_ms = (time.perf_counter() - start) * 1000.0
        _dispatch_sql_event(
            statement=statement,
            parameters=parameters,
            duration_ms=duration_ms,
            rowcount=getattr(cursor, "rowcount", None),
            engine_id=engine_id,
            engine_name=display_name,
            dialect=dialect_name,
        )

    @event.listens_for(actual_engine, "handle_error")
    def _handle_error(exception_context: Any) -> None:
        conn = getattr(exception_context, "connection", None)
        if conn is None:
            return
        stack: list[float] = conn.info.get("_flask_lens_start_stack", [])
        start = stack.pop() if stack else None
        if start is None:
            return
        statement = getattr(exception_context, "statement", None) or ""
        if not statement:
            return
        duration_ms = (time.perf_counter() - start) * 1000.0
        _dispatch_sql_event(
            statement=statement,
            parameters=getattr(exception_context, "parameters", None),
            duration_ms=duration_ms,
            rowcount=None,
            engine_id=engine_id,
            engine_name=display_name,
            dialect=dialect_name,
        )

    return engine_id


def _dispatch_sql_event(
    *,
    statement: str,
    parameters: Any,
    duration_ms: float,
    rowcount: int | None,
    engine_id: str,
    engine_name: str,
    dialect: str,
) -> None:
    from flask_lens.collectors.context import get_active_request, is_capture_suspended

    if is_capture_suspended():
        return

    req_ctx = get_active_request()
    with _ENGINE_LOCK:
        extra_sinks = list(_QUERY_LISTENERS)

    # Isolate multi-app engines when the owning LensState explicitly tracks engines.
    if req_ctx is not None and (
        not req_ctx.config.capture_sql
        or (req_ctx.watched_engine_ids and engine_id not in req_ctx.watched_engine_ids)
    ):
        req_ctx = None

    if req_ctx is None and not extra_sinks:
        return

    norm = normalize_sql(statement)
    query_id = uuid.uuid4().hex[:12]
    offset_ms = 0.0
    frames: list[StackFrame] = []
    call_site = ""
    redacted_params: Any = parameters

    if req_ctx is not None:
        cfg = req_ctx.config
        offset_ms = round((time.perf_counter() - req_ctx.start_perf) * 1000.0 - duration_ms, 3)
        if offset_ms < 0.0:
            offset_ms = 0.0
        if cfg.capture_stack_frames:
            frames, call_site = capture_stack_frames(
                app_root=cfg.app_root,
                max_frames=cfg.max_stack_frames,
            )
        redacted_params = redact_sql_params(
            parameters,
            cfg.sensitive_fields,
            enabled=cfg.redact_sensitive,
        )
    elif extra_sinks:
        frames, call_site = capture_stack_frames(app_root=None, max_frames=6)
        redacted_params = redact_sql_params(parameters, frozenset(), enabled=False)

    query = SQLQuery(
        id=query_id,
        sql=statement,
        normalized_sql=norm.normalized,
        fingerprint=norm.fingerprint,
        operation=norm.operation,
        tables=norm.tables,
        params=redacted_params,
        duration_ms=round(duration_ms, 3),
        offset_ms=offset_ms,
        rowcount=rowcount if isinstance(rowcount, int) and rowcount >= 0 else None,
        engine_name=engine_name,
        dialect=dialect,
        stack=frames,
        call_site=call_site,
    )

    if req_ctx is not None:
        req_ctx.sql_queries.append(query)
        _RAW_PARAMS_CACHE.put(query_id, engine_id, parameters)

    for sink in extra_sinks:
        sink(query, engine_id, parameters)


def capture_stack_frames(
    *,
    app_root: str | None = None,
    max_frames: int = 8,
    context_radius: int = 2,
) -> tuple[list[StackFrame], str]:
    """Capture clean call frames pointing to user application code via ``sys._getframe``."""
    frames: list[StackFrame] = []
    norm_root = os.path.abspath(app_root) if app_root else None

    try:
        frame: Any = sys._getframe(2)
    except ValueError:
        return [], ""

    while frame is not None and len(frames) < max_frames:
        code = frame.f_code
        filename = code.co_filename
        lineno = frame.f_lineno
        func_name = code.co_name

        if not _should_skip_frame(filename):
            is_user = _is_user_frame(filename, norm_root)
            display_path = _shorten_path(filename, norm_root)
            code_line = linecache.getline(filename, lineno).rstrip()
            ctx_lines: list[tuple[int, str]] = []
            if is_user and context_radius > 0:
                start_ln = max(1, lineno - context_radius)
                for ln in range(start_ln, lineno + context_radius + 1):
                    text = linecache.getline(filename, ln)
                    if text:
                        ctx_lines.append((ln, text.rstrip("\n")))
            frames.append(
                StackFrame(
                    filename=display_path,
                    lineno=lineno,
                    function=func_name,
                    code_line=code_line.strip(),
                    context_lines=ctx_lines,
                    is_user_code=is_user,
                )
            )
        frame = frame.f_back

    primary = next((f for f in frames if f.is_user_code), frames[0] if frames else None)
    call_site = f"{primary.filename}:{primary.lineno} in {primary.function}" if primary else ""
    return frames, call_site


def discover_engines_from_app(app: Flask) -> list[tuple[str, Any]]:
    """Auto-discover SQLAlchemy engines registered on a Flask app."""
    found: list[tuple[str, Any]] = []
    sqla = app.extensions.get("sqlalchemy")
    if sqla is None:
        return found

    try:
        with app.app_context():
            engines = getattr(sqla, "engines", None)
            if isinstance(engines, dict):
                for bind_key, eng in engines.items():
                    name = "default" if bind_key is None else str(bind_key)
                    found.append((name, eng))
            else:
                eng = getattr(sqla, "engine", None)
                if eng is not None:
                    found.append(("default", eng))
    except Exception:
        pass
    return found


def resolve_engines(target: Any, *, app: Flask | None = None) -> list[tuple[str, Any]]:
    """Resolve one or more SQLAlchemy engines from an Engine, Session, Flask-SQLAlchemy, or Flask app."""
    if target is None:
        return []

    if hasattr(target, "dialect") and hasattr(target, "connect"):
        return [("default", target)]
    if hasattr(target, "sync_engine"):
        return [("default", target.sync_engine)]

    if hasattr(target, "get_bind"):
        try:
            bind = target.get_bind()
            if bind is not None:
                return [("default", getattr(bind, "engine", bind))]
        except Exception:
            pass

    if hasattr(target, "extensions") and hasattr(target, "app_context"):
        return discover_engines_from_app(target)

    if app is not None:
        try:
            with app.app_context():
                engines = getattr(target, "engines", None)
                if isinstance(engines, dict):
                    return [
                        ("default" if k is None else str(k), eng)
                        for k, eng in engines.items()
                        if eng is not None
                    ]
                eng = getattr(target, "engine", None)
                if eng is not None:
                    return [("default", eng)]
        except RuntimeError:
            pass

    try:
        engines = getattr(target, "engines", None)
        if isinstance(engines, dict):
            return [
                ("default" if k is None else str(k), eng)
                for k, eng in engines.items()
                if eng is not None
            ]
    except RuntimeError:
        app_engines = getattr(target, "_app_engines", None)
        if app_engines is not None:
            resolved: list[tuple[str, Any]] = []
            for bind_map in app_engines.values():
                if isinstance(bind_map, dict):
                    for k, eng in bind_map.items():
                        if eng is not None:
                            resolved.append(("default" if k is None else str(k), eng))
            if resolved:
                return resolved

    try:
        eng = getattr(target, "engine", None)
        if eng is not None:
            return [("default", eng)]
    except RuntimeError:
        pass

    raise TypeError(
        f"Cannot resolve SQLAlchemy engine from {type(target).__name__!r}. "
        "Pass an Engine, Session, Flask-SQLAlchemy db, or Flask app."
    )


def _is_within(path: str, directory: str) -> bool:
    try:
        return os.path.commonpath([path, directory]) == directory
    except ValueError:
        return False


def _should_skip_frame(filename: str) -> bool:
    if not filename or filename.startswith("<"):
        return True
    return any(frag in filename for frag in _SKIP_PATH_FRAGMENTS)


def _is_user_frame(filename: str, app_root: str | None) -> bool:
    abs_path = os.path.abspath(filename)
    if "site-packages" in abs_path or "dist-packages" in abs_path:
        return False
    if any(_is_within(abs_path, prefix) for prefix in _STDLIB_PREFIXES):
        return False
    if app_root:
        return _is_within(abs_path, app_root)
    return True


def _shorten_path(filename: str, app_root: str | None) -> str:
    abs_path = os.path.abspath(filename)
    if app_root and _is_within(abs_path, app_root):
        return os.path.relpath(abs_path, app_root)
    parts = abs_path.split(os.sep)
    if len(parts) > 3:
        return os.sep.join(parts[-3:])
    return abs_path
