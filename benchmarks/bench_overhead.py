"""Reproducible WSGI overhead benchmark for flask-lens.

Measures throughput and latency across:
  1. Plain Flask (baseline)
  2. Flask + Lens (disabled)
  3. Flask + Lens (memory storage, profile_mode="off")
  4. Flask + Lens (sqlite WAL storage, profile_mode="off")
  5. Flask + Lens (sqlite WAL storage, profile_mode="always" — SQL + N+1 + cProfile)

Run with:
    python benchmarks/bench_overhead.py
"""

from __future__ import annotations

import math
import os
import tempfile
import time
from collections.abc import Callable

from flask import Flask, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from flask_lens import Lens


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    idx = (pct / 100.0) * (len(ordered) - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[lo]
    w = idx - lo
    return ordered[lo] * (1.0 - w) + ordered[hi] * w


def _build_app(
    configure_lens: Callable[[Flask, SQLAlchemy], Lens | None],
) -> tuple[Flask, Lens | None]:
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    class LocalBase(DeclarativeBase):
        pass

    db = SQLAlchemy(model_class=LocalBase)

    class Item(db.Model):  # type: ignore[name-defined]
        __tablename__ = "bench_items"
        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        name: Mapped[str] = mapped_column(String(80), nullable=False)

    db.init_app(app)
    lens_ext = configure_lens(app, db)

    @app.get("/ping")
    def ping() -> object:
        return jsonify({"ok": True})

    @app.get("/items")
    def items() -> object:
        rows = db.session.scalars(select(Item).limit(10)).all()
        return jsonify({"items": [{"id": r.id, "name": r.name} for r in rows]})

    with app.app_context():
        db.create_all()
        for i in range(10):
            db.session.add(Item(name=f"item-{i}"))
        db.session.commit()

    return app, lens_ext


def _run_bench(app: Flask, path: str, iterations: int = 300, warmup: int = 30) -> dict[str, float]:
    latencies_ms: list[float] = []
    with app.test_client() as client:
        for _ in range(warmup):
            resp = client.get(path)
            assert resp.status_code == 200
        start_all = time.perf_counter()
        for _ in range(iterations):
            t0 = time.perf_counter()
            resp = client.get(path)
            t1 = time.perf_counter()
            assert resp.status_code == 200
            latencies_ms.append((t1 - t0) * 1000.0)
        total_sec = time.perf_counter() - start_all

    return {
        "rps": iterations / total_sec,
        "mean_ms": sum(latencies_ms) / len(latencies_ms),
        "p50_ms": _percentile(latencies_ms, 50),
        "p95_ms": _percentile(latencies_ms, 95),
        "p99_ms": _percentile(latencies_ms, 99),
    }


def main() -> None:
    iterations = 300
    with tempfile.TemporaryDirectory() as tmpdir:
        db_off = os.path.join(tmpdir, "lens_off.sqlite3")
        db_prof = os.path.join(tmpdir, "lens_prof.sqlite3")

        scenarios: list[tuple[str, Callable[[Flask, SQLAlchemy], Lens | None]]] = [
            ("Plain Flask (baseline)", lambda _a, _d: None),
            ("Lens (enabled=False)", lambda a, d: Lens(a, db=d, enabled=False)),
            (
                "Lens (memory, profile=off)",
                lambda a, d: Lens(
                    a, db=d, storage="memory", profile_mode="off", capture_outgoing_http=False
                ),
            ),
            (
                "Lens (sqlite WAL, profile=off)",
                lambda a, d: Lens(
                    a,
                    db=d,
                    storage="sqlite",
                    db_path=db_off,
                    profile_mode="off",
                    capture_outgoing_http=False,
                ),
            ),
            (
                "Lens (sqlite WAL, profile=always)",
                lambda a, d: Lens(
                    a,
                    db=d,
                    storage="sqlite",
                    db_path=db_prof,
                    profile_mode="always",
                    capture_outgoing_http=False,
                ),
            ),
        ]

        for route_label, path in [
            ("JSON route (/ping)", "/ping"),
            ("SQLAlchemy route (/items)", "/items"),
        ]:
            print(f"\n=== {route_label} ({iterations} iterations) ===")
            print(f"{'Scenario':<36} {'RPS':>8} {'Mean':>8} {'P50':>8} {'P95':>8} {'Delta':>9}")
            print("-" * 82)
            baseline_mean: float | None = None
            for name, builder in scenarios:
                app, ext = _build_app(builder)
                try:
                    stats = _run_bench(app, path, iterations=iterations)
                    if baseline_mean is None:
                        baseline_mean = stats["mean_ms"]
                    delta_ms = stats["mean_ms"] - baseline_mean
                    delta_str = "baseline" if name.startswith("Plain") else f"{delta_ms:+.3f}ms"
                    print(
                        f"{name:<36} {stats['rps']:>8.0f} "
                        f"{stats['mean_ms']:>7.3f}m {stats['p50_ms']:>7.3f}m "
                        f"{stats['p95_ms']:>7.3f}m {delta_str:>9}"
                    )
                finally:
                    if ext is not None:
                        ext.close()


if __name__ == "__main__":
    main()
