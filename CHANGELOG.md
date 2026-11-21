# Changelog

All notable changes to `flask-lens` are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
