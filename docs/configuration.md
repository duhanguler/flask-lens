# Configuration Reference

`flask-lens` supports three configuration styles, evaluated in increasing precedence:

1. Flask `app.config["LENS_*"]` keys
2. A `LensConfig(...)` instance passed to `Lens(app, config=...)`
3. Keyword arguments passed directly to `Lens(app, ...)` or `lens.init_app(app, ...)`

## Options

| Attribute | Flask `app.config` Key | Default | Description |
| :--- | :--- | :--- | :--- |
| `enabled` | `LENS_ENABLED` | `True` | Master switch. When `False`, registers zero hooks and does not mount the `/__lens` blueprint. |
| `url_prefix` | `LENS_URL_PREFIX` | `"/__lens"` | Mount path for the embedded UI and REST/SSE API. |
| `storage` | `LENS_STORAGE` | `"sqlite"` | `"sqlite"` (embedded WAL ring buffer) or `"memory"` (ephemeral in-memory ring buffer). You may also pass a custom `StorageBackend` instance. |
| `db_path` | `LENS_DB_PATH` | `".flask_lens.sqlite3"` | SQLite database file path (or `":memory:"`). |
| `max_requests` | `LENS_MAX_REQUESTS` | `1000` | Maximum number of recent requests retained in the ring buffer. |
| `sample_rate` | `LENS_SAMPLE_RATE` | `1.0` | Float in `0.0..1.0`. Replayed requests (`X-Lens-Replay: 1`) always bypass sampling. |
| `ignore_paths` | `LENS_IGNORE_PATHS` | `("/static/", "/favicon.ico", "/robots.txt")` | URL path prefixes excluded from capture. `url_prefix` is always ignored automatically. |
| `ignore_methods` | `LENS_IGNORE_METHODS` | `("OPTIONS",)` | HTTP methods excluded from capture. |
| `max_body_bytes` | `LENS_MAX_BODY_BYTES` | `65536` | Maximum request/response payload size stored (in bytes). |
| `capture_request_body` | `LENS_CAPTURE_REQUEST_BODY` | `True` | Record request payloads (respecting redaction and size cap). |
| `capture_response_body` | `LENS_CAPTURE_RESPONSE_BODY` | `True` | Record non-streaming response payloads (streaming/passthrough responses are never buffered). |
| `capture_sql` | `LENS_CAPTURE_SQL` | `True` | Instrument watched or auto-discovered SQLAlchemy engines. |
| `slow_query_ms` | `LENS_SLOW_QUERY_MS` | `50.0` | Threshold in milliseconds for flagging a query as `SLOW`. |
| `n_plus_one_threshold` | `LENS_N_PLUS_ONE_THRESHOLD` | `3` | Minimum repeated normalized `SELECT` queries within one request to flag an N+1 pattern. |
| `capture_stack_frames` | `LENS_CAPTURE_STACK_FRAMES` | `True` | Capture application call sites and source snippets for each SQL query. |
| `max_stack_frames` | `LENS_MAX_STACK_FRAMES` | `8` | Maximum non-framework frames retained per SQL query. |
| `allow_explain` | `LENS_ALLOW_EXPLAIN` | `True` | Enable read-only `EXPLAIN` / `EXPLAIN QUERY PLAN` from the UI and API. |
| `profile_mode` | `LENS_PROFILE_MODE` | `"always"` | `"always"`, `"slow"`, `"on_demand"`, or `"off"`. |
| `profile_slow_ms` | `LENS_PROFILE_SLOW_MS` | `100.0` | Latency threshold used when `profile_mode="slow"`. |
| `profile_max_functions` | `LENS_PROFILE_MAX_FUNCTIONS` | `120` | Maximum functions retained in flat cProfile breakdown. |
| `capture_outgoing_http` | `LENS_CAPTURE_OUTGOING_HTTP` | `True` | Instrument `urllib3` (`requests`) and `httpx` outgoing HTTP calls. |
| `capture_logs` | `LENS_CAPTURE_LOGS` | `True` | Attach Python `logging` records to their originating request. |
| `log_level` | `LENS_LOG_LEVEL` | `20` (`logging.INFO`) | Minimum log level captured. |
| `capture_templates` | `LENS_CAPTURE_TEMPLATES` | `True` | Record Jinja2 `template_rendered` signals. |
| `capture_exceptions` | `LENS_CAPTURE_EXCEPTIONS` | `True` | Record unhandled exception tracebacks and redacted frame locals. |
| `server_timing_header` | `LENS_SERVER_TIMING_HEADER` | `True` | Emit W3C `Server-Timing` response headers (`total;dur=...`, `sql;dur=...`). |
| `authorize` | `LENS_AUTHORIZE` | `None` | Optional `Callable[[flask.Request], bool]` gating access to `/__lens` UI and APIs. |
| `allow_replay` | `LENS_ALLOW_REPLAY` | `True` | Enable live request replay via `/__lens/api/replay`. |
| `redact_sensitive` | `LENS_REDACT_SENSITIVE` | `True` | Automatically scrub sensitive headers, JSON/form keys, SQL params, and exception locals. |
| `sensitive_headers` | `LENS_SENSITIVE_HEADERS` | See `config.py` | Lowercase HTTP header names replaced with `"[REDACTED]"`. |
| `sensitive_fields` | `LENS_SENSITIVE_FIELDS` | See `config.py` | Lowercase payload/param/local names replaced with `"[REDACTED]"`. |
| `app_root` | `LENS_APP_ROOT` | `app.root_path` | Root directory used to distinguish application code from third-party libraries. |

## Recommended Staging Setup

```python
from flask import Flask, request
from flask_lens import Lens

app = Flask(__name__)

Lens(
    app,
    db=db,
    profile_mode="on_demand",          # Keep overhead <0.4ms; trigger via @lens.profile or X-Lens-Profile: 1
    sample_rate=0.25,                  # Capture 25% of traffic
    allow_replay=False,                # Disable mutation replay
    authorize=lambda req: req.headers.get("X-Internal-Admin") == os.environ["LENS_ADMIN_TOKEN"],
)
```
