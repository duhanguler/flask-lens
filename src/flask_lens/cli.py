"""Flask CLI commands exposed under ``flask lens ...``."""

from __future__ import annotations

import json
from typing import Any

import click
from flask import current_app
from flask.cli import with_appcontext

from flask_lens.extension import LensState
from flask_lens.storage.base import RequestFilter


def _require_state() -> LensState:
    state: LensState | None = current_app.extensions.get("lens")
    if state is None:
        raise click.ClickException("flask-lens is not initialized on this Flask application.")
    return state


@click.group("lens")
def lens_cli() -> None:
    """Inspect and manage flask-lens telemetry."""


@lens_cli.command("status")
@with_appcontext
def lens_status() -> None:
    """Show active flask-lens configuration and storage summary."""
    state = _require_state()
    cfg = state.config
    stats = state.storage.overview_stats()
    click.echo(f"flask-lens enabled   : {cfg.enabled}")
    click.echo(f"Dashboard URL prefix : {cfg.url_prefix}")
    click.echo(
        f"Storage backend      : {cfg.storage} ({cfg.db_path if cfg.storage == 'sqlite' else 'in-memory'})"
    )
    click.echo(f"Profile mode         : {cfg.profile_mode}")
    click.echo(
        f"Watched engines      : {', '.join(sorted(state.watched_engines.keys())) or 'none'}"
    )
    click.echo(f"Stored requests      : {stats['total_requests']} / {cfg.max_requests}")
    click.echo(f"N+1 alerts           : {stats['n_plus_one_requests']}")
    click.echo(f"Captured exceptions  : {stats['exception_count']}")


@lens_cli.command("stats")
@click.option("--limit", default=15, show_default=True, type=int, help="Maximum endpoints to list.")
@with_appcontext
def lens_stats(limit: int) -> None:
    """Print latency and SQL query analytics grouped by endpoint."""
    state = _require_state()
    endpoints = state.storage.endpoint_stats()[:limit]
    if not endpoints:
        click.echo("No captured requests yet.")
        return
    click.echo(
        f"{'METHOD':<7} {'ROUTE':<32} {'REQS':>5} {'P50':>8} {'P95':>8} {'SQL':>6} {'N+1':>5} {'ERR%':>6}"
    )
    click.echo("-" * 84)
    for ep in endpoints:
        route = str(ep["route"])
        if len(route) > 31:
            route = route[:28] + "..."
        click.echo(
            f"{ep['method']:<7} {route:<32} {ep['count']:>5} "
            f"{ep['p50_ms']:>7.1f}m {ep['p95_ms']:>7.1f}m "
            f"{ep['avg_sql']:>6.1f} {ep['n_plus_one_hits']:>5} {ep['error_rate']:>5.1f}%"
        )


@lens_cli.command("slow")
@click.option(
    "--limit", default=10, show_default=True, type=int, help="Maximum slow queries to list."
)
@with_appcontext
def lens_slow(limit: int) -> None:
    """List top normalized SQL fingerprints ordered by total execution time."""
    state = _require_state()
    rows = state.storage.sql_stats(limit=limit)
    if not rows:
        click.echo("No captured SQL queries yet.")
        return
    for idx, row in enumerate(rows, start=1):
        n1_tag = f" [N+1 x{row['n_plus_one_hits']}]" if row["n_plus_one_hits"] else ""
        click.echo(
            f"{idx:>2}. {row['total_ms']:>7.2f}ms total | avg {row['avg_ms']:.2f}ms | "
            f"{row['calls']} calls{n1_tag}"
        )
        click.echo(f"    {row['normalized_sql'][:110]}")
        if row["sample_call_site"]:
            click.echo(f"    at {row['sample_call_site']}")


@lens_cli.command("clear")
@with_appcontext
def lens_clear() -> None:
    """Delete all stored telemetry records."""
    state = _require_state()
    count = state.storage.clear()
    click.echo(f"Cleared {count} captured request(s).")


@lens_cli.command("export")
@click.argument("output", type=click.Path(dir_okay=False, writable=True))
@click.option(
    "--limit", default=200, show_default=True, type=int, help="Maximum requests to export."
)
@with_appcontext
def lens_export(output: str, limit: int) -> None:
    """Export captured requests and full SQL/profile telemetry to a JSON file."""
    state = _require_state()
    summaries, total = state.storage.list_requests(RequestFilter(limit=limit))
    full_records: list[dict[str, Any]] = []
    for item in summaries:
        rec = state.storage.get(item["id"])
        if rec is not None:
            full_records.append(rec.to_dict())

    payload = {
        "total_stored": total,
        "exported": len(full_records),
        "overview": state.storage.overview_stats(),
        "requests": full_records,
    }
    with open(output, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    click.echo(f"Exported {len(full_records)} request(s) to {output}.")
