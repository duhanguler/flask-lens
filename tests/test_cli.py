"""Tests for the ``flask lens`` CLI command group."""

from __future__ import annotations

import json
from pathlib import Path

from flask import Flask
from flask_sqlalchemy import SQLAlchemy

from flask_lens import Lens
from flask_lens.cli import lens_cli


def test_cli_status_stats_slow_export_and_clear(
    bookstore_bundle: tuple[Flask, SQLAlchemy, Lens],
    tmp_path: Path,
) -> None:
    app, _db, _lens = bookstore_bundle
    app.cli.add_command(lens_cli)

    client = app.test_client()
    client.get("/api/authors")
    client.get("/api/authors?optimized=1")

    runner = app.test_cli_runner()

    res_status = runner.invoke(args=["lens", "status"])
    assert res_status.exit_code == 0
    assert "Stored requests      : 2" in res_status.output
    assert "N+1 alerts           : 1" in res_status.output

    res_stats = runner.invoke(args=["lens", "stats"])
    assert res_stats.exit_code == 0
    assert "/api/authors" in res_stats.output

    res_slow = runner.invoke(args=["lens", "slow", "--limit", "5"])
    assert res_slow.exit_code == 0
    assert "SELECT" in res_slow.output

    export_file = tmp_path / "telemetry.json"
    res_export = runner.invoke(args=["lens", "export", str(export_file)])
    assert res_export.exit_code == 0
    exported = json.loads(export_file.read_text(encoding="utf-8"))
    assert exported["exported"] == 2
    assert len(exported["requests"]) == 2

    res_clear = runner.invoke(args=["lens", "clear"])
    assert res_clear.exit_code == 0
    assert "Cleared 2 captured request(s)." in res_clear.output
