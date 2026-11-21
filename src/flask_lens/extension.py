"""Core Flask extension orchestrating request capture, profiling, SSE, and replay."""

from __future__ import annotations

import contextlib
import functools
import logging
import queue
import random
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from flask import Flask, Response, g, got_request_exception, request, template_rendered

from flask_lens.collectors.context import (
    RequestContext,
    event,
    get_active_request,
    reset_active_request,
    set_active_request,
    span,
    suspend_capture,
)
from flask_lens.collectors.exceptions import capture_exception
from flask_lens.collectors.http_outgoing import install_http_hooks, uninstall_http_hooks
from flask_lens.collectors.logging_handler import LensLogHandler
from flask_lens.collectors.profiler import RequestProfiler
from flask_lens.config import LensConfig
from flask_lens.models import CapturedRequest, CustomSpan
from flask_lens.redaction import redact_body, redact_headers
from flask_lens.sql.analyzer import analyze_queries
from flask_lens.sql.explain import ExplainResult, run_explain
from flask_lens.sql.listener import (
    attach_engine,
    discover_engines_from_app,
    get_raw_params_cache,
    get_registered_engine,
    resolve_engines,
)
from flask_lens.storage.base import StorageBackend, compute_request_diff
from flask_lens.storage.memory import MemoryStorage
from flask_lens.storage.sqlite import SQLiteStorage

if TYPE_CHECKING:
    from collections.abc import Iterator

F = TypeVar("F", bound=Callable[..., Any])

_PROFILE_ATTR = "_flask_lens_profile"


class StreamBroadcaster:
    """Thread-safe pub/sub fan-out for Server-Sent Events (SSE) live updates."""

    def __init__(self, max_queue_size: int = 128) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._max_queue_size = max_queue_size

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self._max_queue_size)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, payload: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    q.get_nowait()
                with contextlib.suppress(queue.Full):
                    q.put_nowait(payload)


@dataclass
class LensState:
    """Per-Flask-app state stored inside ``app.extensions['lens']``."""

    app: Flask
    config: LensConfig
    storage: StorageBackend
    broadcaster: StreamBroadcaster = field(default_factory=StreamBroadcaster)
    watched_engines: dict[str, Any] = field(default_factory=dict)
    engines_discovered: bool = False
    log_handler: LensLogHandler | None = None
    http_hooks_installed: bool = False
    _signal_receivers: list[tuple[Any, Callable[..., Any]]] = field(default_factory=list)
    _closed: bool = False

    def watch_engine(self, engine_or_db: Any, name: str | None = None) -> list[str]:
        """Attach SQLAlchemy instrumentation to an Engine, Session, or Flask-SQLAlchemy db."""
        attached: list[str] = []
        for resolved_name, eng in resolve_engines(engine_or_db, app=self.app):
            target_name = name or resolved_name
            reg_name = attach_engine(eng, name=target_name)
            self.watched_engines[reg_name] = eng
            attached.append(reg_name)
        return attached

    def ensure_engines_discovered(self) -> None:
        if self.engines_discovered or not self.config.capture_sql:
            return
        discovered = discover_engines_from_app(self.app)
        if discovered:
            for name, eng in discovered:
                reg_name = attach_engine(eng, name=name)
                self.watched_engines[reg_name] = eng
            self.engines_discovered = True

    def explain_query(self, request_id: str, query_id: str) -> ExplainResult:
        """Run read-only EXPLAIN on a previously captured SELECT query."""
        if not self.config.allow_explain:
            return ExplainResult(
                supported=False,
                dialect="unknown",
                statement="",
                error="EXPLAIN is disabled in LensConfig (allow_explain=False).",
            )

        captured = self.storage.get(request_id)
        if captured is None:
            return ExplainResult(
                supported=False,
                dialect="unknown",
                statement="",
                error=f"Request {request_id!r} not found.",
            )

        query = next((q for q in captured.sql_queries if q.id == query_id), None)
        if query is None:
            return ExplainResult(
                supported=False,
                dialect="unknown",
                statement="",
                error=f"Query {query_id!r} not found in request {request_id!r}.",
            )

        cached = get_raw_params_cache().get(query_id)
        if cached is not None:
            engine_name, raw_params = cached
        else:
            engine_name = query.engine_name
            raw_params = query.params

        engine = self.watched_engines.get(engine_name) or get_registered_engine(engine_name)
        if engine is None:
            self.ensure_engines_discovered()
            engine = self.watched_engines.get(engine_name) or get_registered_engine(engine_name)
        if engine is None:
            return ExplainResult(
                supported=False,
                dialect=query.dialect,
                statement=query.sql,
                error="No watched SQLAlchemy engine available to run EXPLAIN.",
            )

        with suspend_capture():
            return run_explain(engine, query.sql, raw_params)

    def replay_request(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: str | None = None,
        original_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Replay a request through the live Flask app and return the newly captured trace."""
        if not self.config.allow_replay:
            raise PermissionError("Request replay is disabled (allow_replay=False).")

        clean_method = method.strip().upper()
        if clean_method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            raise ValueError(f"Unsupported HTTP method: {clean_method!r}")

        clean_path = path.strip()
        if not clean_path.startswith("/") or clean_path.startswith("//"):
            raise ValueError("Replay path must be a relative path starting with '/'.")
        if clean_path == self.config.url_prefix or clean_path.startswith(
            self.config.url_prefix + "/"
        ):
            raise ValueError("Replaying Lens internal endpoints is not permitted.")

        replay_headers: dict[str, str] = {}
        if headers:
            for k, v in headers.items():
                lk = k.lower()
                # Skip hop-by-hop headers and redacted placeholders
                if lk in {"host", "content-length", "transfer-encoding"}:
                    continue
                if v == "[REDACTED]":
                    continue
                replay_headers[k] = v

        replay_headers["X-Lens-Replay"] = "1"
        if original_request_id:
            replay_headers["X-Lens-Replay-From"] = original_request_id

        with self.app.test_client() as client:
            resp = client.open(
                path=clean_path,
                method=clean_method,
                headers=replay_headers,
                data=body.encode("utf-8") if body is not None and body != "" else None,
            )
            new_request_id = resp.headers.get("X-Lens-Request-Id")

        new_captured = self.storage.get(new_request_id) if new_request_id else None
        diff: dict[str, Any] | None = None
        if original_request_id and new_captured is not None:
            orig_captured = self.storage.get(original_request_id)
            if orig_captured is not None:
                diff = compute_request_diff(orig_captured, new_captured)

        return {
            "request_id": new_request_id,
            "status_code": resp.status_code,
            "request": new_captured.summary_dict() if new_captured else None,
            "diff": diff,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.log_handler is not None:
            logging.getLogger().removeHandler(self.log_handler)
            self.log_handler = None
        if self.http_hooks_installed:
            uninstall_http_hooks()
            self.http_hooks_installed = False
        for signal, receiver in self._signal_receivers:
            signal.disconnect(receiver, sender=self.app)
        self._signal_receivers.clear()
        self.storage.close()


class Lens:
    """Real-time developer observability, SQL/N+1 profiler, and debugger for Flask.

    Supports both direct initialization and the Flask application factory pattern::

        app = Flask(__name__)
        db = SQLAlchemy(app)
        lens = Lens(app, db=db)

    Or::

        lens = Lens()
        lens.init_app(app, db=db)
    """

    def __init__(
        self,
        app: Flask | None = None,
        *,
        db: Any = None,
        engine: Any = None,
        config: LensConfig | None = None,
        storage: StorageBackend | str | None = None,
        **options: Any,
    ) -> None:
        self._default_config = config
        self._default_storage = storage
        self._default_options = options
        self._state: LensState | None = None
        if app is not None:
            self.init_app(app, db=db, engine=engine, config=config, storage=storage, **options)

    def init_app(
        self,
        app: Flask,
        *,
        db: Any = None,
        engine: Any = None,
        config: LensConfig | None = None,
        storage: StorageBackend | str | None = None,
        **options: Any,
    ) -> LensState:
        """Register flask-lens hooks, collectors, and dashboard blueprint on *app*."""
        merged_options = {**self._default_options, **options}
        storage_arg = storage if storage is not None else self._default_storage
        custom_backend: StorageBackend | None = None
        if isinstance(storage_arg, str):
            merged_options["storage"] = storage_arg
        elif storage_arg is not None:
            custom_backend = storage_arg

        cfg = config or self._default_config
        if cfg is None:
            cfg = LensConfig.from_flask_config(app, overrides=merged_options)
        elif merged_options:
            for k, v in merged_options.items():
                if not hasattr(cfg, k):
                    raise ValueError(f"Unknown LensConfig option: {k!r}")
                setattr(cfg, k, v)

        if not cfg.url_prefix.startswith("/"):
            cfg.url_prefix = "/" + cfg.url_prefix
        cfg.url_prefix = cfg.url_prefix.rstrip("/") or "/__lens"

        if cfg.app_root is None and getattr(app, "root_path", None):
            cfg.app_root = app.root_path

        backend: StorageBackend
        if custom_backend is not None:
            backend = custom_backend
        elif cfg.storage == "memory":
            backend = MemoryStorage(max_requests=cfg.max_requests)
        else:
            backend = SQLiteStorage(db_path=cfg.db_path, max_requests=cfg.max_requests)

        state = LensState(app=app, config=cfg, storage=backend)
        app.extensions["lens"] = state
        self._state = state

        if not cfg.enabled:
            return state

        # Attach explicit or auto-discovered SQLAlchemy engines
        if cfg.capture_sql:
            if db is not None:
                state.watch_engine(db)
            if engine is not None:
                state.watch_engine(engine)
            state.ensure_engines_discovered()

        # Install request-scoped log handler
        if cfg.capture_logs:
            handler = LensLogHandler()
            logging.getLogger().addHandler(handler)
            state.log_handler = handler

        # Install outgoing HTTP instrumentation
        if cfg.capture_outgoing_http:
            install_http_hooks()
            state.http_hooks_installed = True

        # Register Flask request lifecycle hooks
        self._register_hooks(app, state)

        # Register dashboard & API blueprint
        from flask_lens.blueprint import create_lens_blueprint

        bp_name = "flask_lens"
        if bp_name not in app.blueprints:
            app.register_blueprint(create_lens_blueprint(), url_prefix=cfg.url_prefix)

        return state

    @property
    def state(self) -> LensState:
        if self._state is None:
            raise RuntimeError("Lens has not been initialized with a Flask app yet.")
        return self._state

    @property
    def config(self) -> LensConfig:
        return self.state.config

    @property
    def storage(self) -> StorageBackend:
        return self.state.storage

    def watch_engine(self, engine_or_db: Any, name: str | None = None) -> list[str]:
        """Watch an additional SQLAlchemy Engine, Session, or Flask-SQLAlchemy db."""
        return self.state.watch_engine(engine_or_db, name=name)

    def clear(self) -> int:
        """Clear all captured telemetry from storage."""
        return self.state.storage.clear()

    def close(self) -> None:
        """Detach global hooks and close the storage backend."""
        if self._state is not None:
            self._state.close()

    @staticmethod
    def span(name: str, **metadata: Any) -> Iterator[dict[str, Any]]:
        """Context manager recording a custom timed span on the active request."""
        return span(name, **metadata)  # type: ignore[return-value]

    @staticmethod
    def event(name: str, metadata: dict[str, Any] | None = None) -> None:
        """Record an instantaneous event marker on the active request."""
        event(name, metadata)

    @staticmethod
    def profile(func: F) -> F:
        """Decorator ensuring cProfile runs whenever this view function is invoked."""
        setattr(func, _PROFILE_ATTR, True)

        @functools.wraps(func)
        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        setattr(_wrapped, _PROFILE_ATTR, True)
        return _wrapped  # type: ignore[return-value]

    def _register_hooks(self, app: Flask, state: LensState) -> None:
        @app.before_request
        def _lens_before_request() -> None:
            cfg = state.config
            if not cfg.enabled or _should_ignore_request(cfg):
                return

            if (
                cfg.sample_rate < 1.0
                and request.headers.get("X-Lens-Replay") != "1"
                and random.random() > cfg.sample_rate
            ):
                return

            state.ensure_engines_discovered()

            req_id = uuid.uuid4().hex[:16]
            is_replay = request.headers.get("X-Lens-Replay") == "1"
            replayed_from = request.headers.get("X-Lens-Replay-From")

            should_profile = _should_profile_request(app, cfg)
            profiler: RequestProfiler | None = None
            if should_profile:
                profiler = RequestProfiler(
                    app_root=cfg.app_root,
                    max_functions=cfg.profile_max_functions,
                )

            ctx = RequestContext(
                request_id=req_id,
                config=cfg,
                start_time=time.time(),
                start_perf=time.perf_counter(),
                is_replay=is_replay,
                replayed_from=replayed_from,
                force_profile=should_profile,
                profiler=profiler,
            )
            token = set_active_request(ctx)
            g._flask_lens_ctx = ctx
            g._flask_lens_token = token
            g._flask_lens_finalized = False

            # Eagerly snapshot request body without consuming downstream parsers
            if cfg.capture_request_body:
                try:
                    raw_body = request.get_data(cache=True)
                    g._flask_lens_req_body = redact_body(
                        raw_body,
                        request.content_type,
                        cfg.sensitive_fields,
                        max_bytes=cfg.max_body_bytes,
                        enabled=cfg.redact_sensitive,
                    )
                except Exception:
                    g._flask_lens_req_body = None
            else:
                g._flask_lens_req_body = None

            if profiler is not None:
                profiler.start()

        @app.after_request
        def _lens_after_request(response: Response) -> Response:
            ctx: RequestContext | None = getattr(g, "_flask_lens_ctx", None)
            if ctx is None or getattr(g, "_flask_lens_finalized", False):
                return response

            g._flask_lens_finalized = True
            captured = _finalize_request(state, ctx, response=response)
            if state.config.server_timing_header:
                timing_parts = [f"total;dur={captured.duration_ms:.2f}"]
                if captured.sql_count > 0:
                    timing_parts.append(
                        f'sql;dur={captured.sql_duration_ms:.2f};desc="{captured.sql_count} queries"'
                    )
                existing = response.headers.get("Server-Timing")
                header_val = ", ".join(timing_parts)
                response.headers["Server-Timing"] = (
                    f"{existing}, {header_val}" if existing else header_val
                )
            response.headers["X-Lens-Request-Id"] = captured.id
            return response

        @app.teardown_request
        def _lens_teardown_request(exc: BaseException | None) -> None:
            ctx: RequestContext | None = getattr(g, "_flask_lens_ctx", None)
            token = getattr(g, "_flask_lens_token", None)
            try:
                if ctx is not None and not getattr(g, "_flask_lens_finalized", False):
                    g._flask_lens_finalized = True
                    if (
                        exc is not None
                        and ctx.exception is None
                        and state.config.capture_exceptions
                    ):
                        ctx.exception = capture_exception(exc, state.config)
                    _finalize_request(state, ctx, response=None)
            finally:
                if ctx is not None and ctx.profiler is not None:
                    ctx.profiler.stop()
                if token is not None:
                    reset_active_request(token)
                g._flask_lens_ctx = None
                g._flask_lens_token = None

        # Flask signals
        def _on_exception(sender: Flask, exception: BaseException, **extra: Any) -> None:
            ctx = get_active_request()
            if ctx is not None and state.config.capture_exceptions:
                ctx.exception = capture_exception(exception, state.config)

        def _on_template_rendered(
            sender: Flask, template: Any, context: dict[str, Any], **extra: Any
        ) -> None:
            ctx = get_active_request()
            if ctx is not None and state.config.capture_templates:
                tpl_name = getattr(template, "name", None) or "<inline>"
                ctx.templates.append(tpl_name)
                ctx.spans.append(
                    CustomSpan(
                        name=f"template:{tpl_name}",
                        kind="template",
                        offset_ms=ctx.elapsed_ms(),
                        duration_ms=0.0,
                        metadata={"context_keys": sorted(str(k) for k in context)[:20]},
                    )
                )

        got_request_exception.connect(_on_exception, app)
        template_rendered.connect(_on_template_rendered, app)
        state._signal_receivers.append((got_request_exception, _on_exception))
        state._signal_receivers.append((template_rendered, _on_template_rendered))


def _should_ignore_request(cfg: LensConfig) -> bool:
    path = request.path
    if path == cfg.url_prefix or path.startswith(cfg.url_prefix + "/"):
        return True
    if request.method.upper() in cfg.ignore_methods:
        return True
    for prefix in cfg.ignore_paths:
        if prefix and (path == prefix or path.startswith(prefix)):
            return True
    return False


def _should_profile_request(app: Flask, cfg: LensConfig) -> bool:
    if cfg.profile_mode == "off":
        return False
    if cfg.profile_mode in {"always", "slow"}:
        return True
    # on_demand mode: check header or @lens.profile view marker
    if request.headers.get("X-Lens-Profile") in {"1", "true", "yes"}:
        return True
    if request.endpoint:
        view_func = app.view_functions.get(request.endpoint)
        if view_func is not None and getattr(view_func, _PROFILE_ATTR, False):
            return True
    return False


def _finalize_request(
    state: LensState,
    ctx: RequestContext,
    *,
    response: Response | None,
) -> CapturedRequest:
    cfg = state.config
    profile_data = None
    if ctx.profiler is not None:
        profile_data = ctx.profiler.stop()

    duration_ms = round((time.perf_counter() - ctx.start_perf) * 1000.0, 3)

    # In "slow" profile mode, only persist profile data if duration exceeded threshold
    if (
        cfg.profile_mode == "slow"
        and duration_ms < cfg.profile_slow_ms
        and request.headers.get("X-Lens-Profile") not in {"1", "true", "yes"}
    ):
        profile_data = None

    sql_summary = analyze_queries(
        ctx.sql_queries,
        slow_query_ms=cfg.slow_query_ms,
        n_plus_one_threshold=cfg.n_plus_one_threshold,
    )

    req_headers = redact_headers(
        dict(request.headers.items()),
        cfg.sensitive_headers,
        enabled=cfg.redact_sensitive,
    )

    status_code = 500
    resp_headers: dict[str, str] = {}
    resp_body: str | None = None
    resp_ctype: str | None = None
    resp_size = 0

    if response is not None:
        status_code = response.status_code
        resp_ctype = response.content_type
        resp_headers = redact_headers(
            dict(response.headers.items()),
            cfg.sensitive_headers,
            enabled=cfg.redact_sensitive,
        )
        # Never touch streaming or direct_passthrough responses (send_file, SSE, etc.)
        if not response.is_streamed and not response.direct_passthrough:
            raw_resp = response.get_data()
            resp_size = len(raw_resp)
            if cfg.capture_response_body:
                resp_body = redact_body(
                    raw_resp,
                    resp_ctype,
                    cfg.sensitive_fields,
                    max_bytes=cfg.max_body_bytes,
                    enabled=cfg.redact_sensitive,
                )
        else:
            resp_size = response.calculate_content_length() or 0
            resp_body = "<streaming response>"

    query_string = request.query_string.decode("utf-8", errors="replace")
    full_path = f"{request.path}?{query_string}" if query_string else request.path

    captured = CapturedRequest(
        id=ctx.request_id,
        timestamp=ctx.start_time,
        method=request.method,
        path=request.path,
        full_path=full_path,
        query_string=query_string,
        endpoint=request.endpoint,
        rule=request.url_rule.rule if request.url_rule is not None else None,
        status_code=status_code,
        duration_ms=duration_ms,
        remote_addr=request.remote_addr or "",
        request_headers=req_headers,
        request_body=getattr(g, "_flask_lens_req_body", None),
        request_content_type=request.content_type,
        response_headers=resp_headers,
        response_body=resp_body,
        response_content_type=resp_ctype,
        response_size=resp_size,
        sql_queries=ctx.sql_queries,
        n_plus_one_groups=sql_summary.n_plus_one_groups,
        sql_count=sql_summary.sql_count,
        sql_duration_ms=sql_summary.sql_duration_ms,
        has_n_plus_one=bool(sql_summary.n_plus_one_groups),
        has_slow_sql=sql_summary.has_slow_sql,
        duplicate_sql_count=sql_summary.duplicate_count,
        outgoing_http=ctx.outgoing_http,
        logs=ctx.logs,
        spans=ctx.spans,
        templates=ctx.templates,
        exception=ctx.exception,
        profile=profile_data,
        is_replay=ctx.is_replay,
        replayed_from=ctx.replayed_from,
    )

    with suspend_capture():
        state.storage.save(captured)
        state.broadcaster.publish(captured.summary_dict())

    return captured
