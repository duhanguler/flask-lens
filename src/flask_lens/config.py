"""Configuration for flask-lens."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from flask import Flask, Request


DEFAULT_SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-xsrf-token",
    }
)

DEFAULT_SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "secret_key",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "private_key",
        "credit_card",
        "card_number",
        "cvv",
        "cvc",
        "ssn",
        "authorization",
    }
)

DEFAULT_IGNORE_PATHS: tuple[str, ...] = (
    "/static/",
    "/favicon.ico",
    "/robots.txt",
)


@dataclass
class LensConfig:
    """Runtime configuration for :class:`flask_lens.Lens`.

    Every option can be passed directly to :class:`LensConfig`, passed as a
    keyword argument to ``Lens(app, ...)``, or configured via Flask's
    ``app.config`` using the ``LENS_*`` prefix (e.g., ``LENS_SLOW_QUERY_MS``).
    """

    enabled: bool = True
    url_prefix: str = "/__lens"
    storage: str = "sqlite"  # "sqlite" or "memory"
    db_path: str = ".flask_lens.sqlite3"
    max_requests: int = 1000
    sample_rate: float = 1.0  # 0.0..1.0

    # Filtering & capture limits
    ignore_paths: tuple[str, ...] = DEFAULT_IGNORE_PATHS
    ignore_methods: tuple[str, ...] = ("OPTIONS",)
    max_body_bytes: int = 65_536  # 64 KiB cap for stored request/response bodies
    capture_request_body: bool = True
    capture_response_body: bool = True

    # SQL analysis
    capture_sql: bool = True
    slow_query_ms: float = 50.0
    n_plus_one_threshold: int = 3
    capture_stack_frames: bool = True
    max_stack_frames: int = 8
    allow_explain: bool = True

    # Profiler (cProfile)
    # Modes: "off", "on_demand" (header X-Lens-Profile: 1 or @lens.profile), "slow", "always"
    profile_mode: str = "always"
    profile_slow_ms: float = 100.0
    profile_max_functions: int = 120

    # Other collectors
    capture_outgoing_http: bool = True
    capture_logs: bool = True
    log_level: int = 20  # logging.INFO
    capture_templates: bool = True
    capture_exceptions: bool = True
    server_timing_header: bool = True

    # Security & redaction
    authorize: Callable[[Request], bool] | None = None
    allow_replay: bool = True
    redact_sensitive: bool = True
    sensitive_headers: frozenset[str] = field(default_factory=lambda: DEFAULT_SENSITIVE_HEADERS)
    sensitive_fields: frozenset[str] = field(default_factory=lambda: DEFAULT_SENSITIVE_FIELDS)

    # Project root hint for classifying "user code" vs third-party/stdlib frames
    app_root: str | None = None

    @classmethod
    def from_flask_config(
        cls,
        app: Flask,
        overrides: dict[str, Any] | None = None,
    ) -> LensConfig:
        """Build a :class:`LensConfig` by reading ``LENS_*`` keys from ``app.config``."""
        cfg = cls()
        mapping: dict[str, str] = {
            "LENS_ENABLED": "enabled",
            "LENS_URL_PREFIX": "url_prefix",
            "LENS_STORAGE": "storage",
            "LENS_DB_PATH": "db_path",
            "LENS_MAX_REQUESTS": "max_requests",
            "LENS_SAMPLE_RATE": "sample_rate",
            "LENS_IGNORE_PATHS": "ignore_paths",
            "LENS_IGNORE_METHODS": "ignore_methods",
            "LENS_MAX_BODY_BYTES": "max_body_bytes",
            "LENS_CAPTURE_REQUEST_BODY": "capture_request_body",
            "LENS_CAPTURE_RESPONSE_BODY": "capture_response_body",
            "LENS_CAPTURE_SQL": "capture_sql",
            "LENS_SLOW_QUERY_MS": "slow_query_ms",
            "LENS_N_PLUS_ONE_THRESHOLD": "n_plus_one_threshold",
            "LENS_CAPTURE_STACK_FRAMES": "capture_stack_frames",
            "LENS_MAX_STACK_FRAMES": "max_stack_frames",
            "LENS_ALLOW_EXPLAIN": "allow_explain",
            "LENS_PROFILE_MODE": "profile_mode",
            "LENS_PROFILE_SLOW_MS": "profile_slow_ms",
            "LENS_PROFILE_MAX_FUNCTIONS": "profile_max_functions",
            "LENS_CAPTURE_OUTGOING_HTTP": "capture_outgoing_http",
            "LENS_CAPTURE_LOGS": "capture_logs",
            "LENS_LOG_LEVEL": "log_level",
            "LENS_CAPTURE_TEMPLATES": "capture_templates",
            "LENS_CAPTURE_EXCEPTIONS": "capture_exceptions",
            "LENS_SERVER_TIMING_HEADER": "server_timing_header",
            "LENS_AUTHORIZE": "authorize",
            "LENS_ALLOW_REPLAY": "allow_replay",
            "LENS_REDACT_SENSITIVE": "redact_sensitive",
            "LENS_SENSITIVE_HEADERS": "sensitive_headers",
            "LENS_SENSITIVE_FIELDS": "sensitive_fields",
            "LENS_APP_ROOT": "app_root",
        }
        for flask_key, attr in mapping.items():
            if flask_key in app.config:
                value = app.config[flask_key]
                setattr(cfg, attr, _coerce_field(attr, value))

        if overrides:
            for key, value in overrides.items():
                if not hasattr(cfg, key):
                    raise ValueError(f"Unknown LensConfig option: {key!r}")
                setattr(cfg, key, _coerce_field(key, value))

        if not cfg.url_prefix.startswith("/"):
            cfg.url_prefix = "/" + cfg.url_prefix
        cfg.url_prefix = cfg.url_prefix.rstrip("/") or "/__lens"

        if cfg.app_root is None and getattr(app, "root_path", None):
            cfg.app_root = app.root_path

        if not (0.0 <= cfg.sample_rate <= 1.0):
            raise ValueError("sample_rate must be between 0.0 and 1.0")
        if cfg.profile_mode not in {"off", "on_demand", "slow", "always"}:
            raise ValueError(
                f"Invalid profile_mode {cfg.profile_mode!r}; expected 'off', 'on_demand', 'slow', or 'always'"
            )
        if cfg.storage not in {"sqlite", "memory"}:
            raise ValueError(f"Invalid storage {cfg.storage!r}; expected 'sqlite' or 'memory'")

        return cfg


def _coerce_field(attr: str, value: Any) -> Any:
    if attr in {"ignore_paths", "ignore_methods"} and isinstance(value, (list, set)):
        return tuple(value)
    if attr in {"sensitive_headers", "sensitive_fields"} and isinstance(value, (list, tuple, set)):
        return frozenset(str(item).lower() for item in value)
    return value
