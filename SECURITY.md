# Security Policy

## Production Guidance

`flask-lens` is designed primarily for **local development, staging, and controlled diagnostic sessions**. It inspects HTTP headers, request/response bodies, SQL statements, execution plans, and exception tracebacks.

If you ever enable `flask-lens` outside localhost:

1. **Always set an `authorize` callback** to restrict `/__lens` to authenticated administrators or internal network allowlists:
   ```python
   Lens(app, db=db, authorize=lambda req: current_user.is_authenticated and current_user.is_admin)
   ```
2. **Keep redaction enabled** (`redact_sensitive=True`, the default). `flask-lens` scrubs `Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key`, and JSON/form/SQL/local-variable keys matching password/token/secret/card patterns before writing to storage.
3. **Disable Replay and EXPLAIN** in sensitive environments if not required:
   ```python
   Lens(app, db=db, allow_replay=False, allow_explain=False)
   ```
4. **EXPLAIN Safety**: `flask-lens` strictly rejects multi-statement queries and non-`SELECT` statements (`INSERT`, `UPDATE`, `DELETE`, `DDL`) and never runs `EXPLAIN ANALYZE` by default.

## Reporting a Vulnerability

If you discover a security vulnerability in `flask-lens`, please report it privately via GitHub Security Advisories on `duhanguler/flask-lens` rather than opening a public issue. We aim to acknowledge reports within 48 hours.
