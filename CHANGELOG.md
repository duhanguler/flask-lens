# Changelog

All notable changes to `flask-lens` are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `@pytest.mark.num_queries(N)` declarative exact query-count pytest marker alongside `max_queries` and `no_n_plus_one`.
- Automatic redaction of sensitive query-string parameters and URL credentials (`redact_query_string`, `redact_url`) across incoming requests and outgoing HTTP traces.
- Deep-link SPA URL routing (`/__lens/requests/<id>`, `/__lens/endpoints`, `/__lens/sql`, `/__lens/exceptions`, `/__lens/compare`), keyboard navigation (`↑`/`↓`/`Enter`) inside `⌘K`, per-query **Copy SQL**, and live relative timestamp refresh.

### Fixed
- **Multi-app & multi-engine isolation:** Assign unique internal engine IDs so multiple Flask apps or SQLAlchemy engines sharing `"default"` bind names never cross-contaminate captured queries or `capture_queries(...)` assertions.
- **`async def` Flask 3.x views with `@lens.profile`:** Mark view functions directly without wrapping them in a sync function, preserving `inspect.iscoroutinefunction` and removing wrapper frame noise from Flamegraphs.
- **Security hardening:**
  - Block replay path-traversal, URL-encoded, and query-string bypass attempts targeting `url_prefix` (e.g., `/foo/../__lens/api/clear`, `/__lens?a=1`, `/%5f%5flens`).
  - Harden `is_safe_explainable_sql()` against apostrophes inside `--` comments, unclosed `/*` block comments, and data-modifying CTEs (`WITH d AS (DELETE ...)`).
  - Restrict `/__lens/assets/<filename>` to an explicit static asset allowlist (`styles.css`, `app.js`).
- **Config immutability & validation:** Validate `LensConfig` in `__post_init__` and use `copy_with()` inside `init_app()` so shared configs are never mutated across apps.
- **SQLite storage:** Escape SQL `LIKE` wildcards (`%`, `_`, `\`) during search and select the chronologically latest `sample_request_id` and `sample_call_site` in `sql_stats()`.
- **Multi-app logging deduplication:** Use a single ref-counted `LensLogHandler` so log records are never duplicated when multiple `Lens` instances coexist in one process.
- **SSE clean shutdown:** Signal active `/__lens/api/stream` subscribers immediately on `lens.close()`.
- **Path prefix classification:** Replace string `startswith` with `os.path.commonpath` when checking `app_root` and stdlib boundaries.
- **Schema-qualified SQL tables:** Extract `"schema"."table"` accurately in `extract_tables()`, and normalize `IN (..., NULL)` and PostgreSQL `E'...'` strings.

## [0.1.0] - 2026-11-21

### Added
- Core `Lens` Flask extension supporting direct initialization and the `init_app()` factory pattern.
- Zero-dependency embedded DevTools SPA mounted at `/__lens` with dark/light themes, real-time SSE streaming, `⌘K` command palette, and keyboard navigation.
- SQLAlchemy 2.0 & Flask-SQLAlchemy 3.x query instrumentation with SQL canonicalization, N+1 detection, exact duplicate detection, slow-query tagging, user-code source snippets, and safe read-only `EXPLAIN` / `EXPLAIN QUERY PLAN` diagnostics.
- Per-request `cProfile` profiler generating interactive zoomable SVG flamegraphs and flat hotspot tables (`profile_mode`: `always`, `slow`, `on_demand`, `off`, plus `@lens.profile` decorator and `X-Lens-Profile: 1` header).
- Outgoing HTTP instrumentation for `urllib3` (`requests`) and `httpx` (sync & async).
- Request-scoped structured logging, template rendering spans, custom `lens.span()` / `lens.event()` hooks, and interactive exception tracebacks with redacted local variables.
- Request Replay modal, cURL export, and Before/After Request Diff comparator.
- Endpoint latency analytics (p50/p95/p99), SQL Fingerprint Explorer, and grouped Exceptions view.
- W3C `Server-Timing` HTTP response header integration.
- Embedded SQLite WAL ring-buffer storage backend and thread-safe in-memory storage backend.
- Django `assertNumQueries` counterparts (`capture_queries`, `assert_num_queries`, `assert_max_queries`, `assert_no_n_plus_one`) and auto-registered `pytest11` plugin (`@pytest.mark.max_queries`, `@pytest.mark.no_n_plus_one`).
- Flask CLI commands under `flask lens` (`status`, `stats`, `slow`, `clear`, `export`).
