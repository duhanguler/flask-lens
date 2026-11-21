"""Per-request cProfile collector converting pstats into flamegraphs and flat tables."""

from __future__ import annotations

import cProfile
import os
import pstats
import threading
from collections import defaultdict
from typing import Any

from flask_lens.models import ProfileData, ProfileFunction

_STDLIB_DIRS: tuple[str, ...] = tuple(
    p
    for p in {
        os.path.dirname(os.__file__),
        os.path.dirname(threading.__file__),
    }
    if p
)

_FRAMEWORK_MARKERS: tuple[str, ...] = (
    f"{os.sep}flask{os.sep}",
    f"{os.sep}werkzeug{os.sep}",
    f"{os.sep}jinja2{os.sep}",
    f"{os.sep}sqlalchemy{os.sep}",
    f"{os.sep}flask_sqlalchemy{os.sep}",
    f"{os.sep}flask_lens{os.sep}",
)

FuncKey = tuple[str, int, str]  # (filename, lineno, func_name)


class RequestProfiler:
    """Wraps :class:`cProfile.Profile` for a single request lifecycle."""

    def __init__(self, *, app_root: str | None = None, max_functions: int = 120) -> None:
        self._app_root = os.path.abspath(app_root) if app_root else None
        self._max_functions = max_functions
        self._profile = cProfile.Profile()
        self._active = False

    def start(self) -> None:
        if not self._active:
            try:
                self._profile.enable()
                self._active = True
            except ValueError:
                # Another profiler (e.g. coverage/debugger) is already active on this thread.
                self._active = False

    def stop(self) -> ProfileData | None:
        if not self._active:
            return None
        self._profile.disable()
        self._active = False
        return build_profile_data(
            self._profile,
            app_root=self._app_root,
            max_functions=self._max_functions,
        )


def build_profile_data(
    profiler: cProfile.Profile,
    *,
    app_root: str | None = None,
    max_functions: int = 120,
) -> ProfileData:
    """Convert a finished ``cProfile.Profile`` into structured stats + a flamegraph tree."""
    stats_obj = pstats.Stats(profiler)
    raw_stats: dict[
        FuncKey, tuple[int, int, float, float, dict[FuncKey, tuple[int, int, float, float]]]
    ] = getattr(stats_obj, "stats", {})
    if not raw_stats:
        return ProfileData(total_ms=0.0, total_calls=0, primitive_calls=0)

    total_tt = sum(entry[2] for entry in raw_stats.values())
    total_ms = round(total_tt * 1000.0, 3)
    total_calls = sum(entry[1] for entry in raw_stats.values())
    primitive_calls = sum(entry[0] for entry in raw_stats.values())

    denom = (
        total_tt if total_tt > 0 else max((entry[3] for entry in raw_stats.values()), default=1e-9)
    )
    if denom <= 0:
        denom = 1e-9

    functions: list[ProfileFunction] = []
    callees_map: dict[FuncKey, list[tuple[FuncKey, float, int]]] = defaultdict(list)

    for key, (cc, nc, tt, ct, callers) in raw_stats.items():
        filename, lineno, func_name = key
        # Skip internal cProfile disable call noise
        if func_name == "disable" and "cProfile" in filename:
            continue
        category = classify_frame(filename, app_root)
        short_file = shorten_filename(filename, app_root)
        functions.append(
            ProfileFunction(
                function=func_name,
                filename=short_file,
                lineno=lineno,
                calls=nc,
                primitive_calls=cc,
                self_ms=round(tt * 1000.0, 3),
                cum_ms=round(ct * 1000.0, 3),
                pct_total=round(min(100.0, (ct / denom) * 100.0), 2),
                category=category,
            )
        )
        for caller_key, caller_stats in callers.items():
            if caller_key in raw_stats:
                edge_nc = caller_stats[1]
                edge_ct = caller_stats[3]
                callees_map[caller_key].append((key, edge_ct, edge_nc))

    functions.sort(key=lambda fn: (-fn.cum_ms, -fn.self_ms))
    functions = functions[:max_functions]

    flamegraph = _build_flamegraph(
        raw_stats=raw_stats,
        callees_map=callees_map,
        app_root=app_root,
        total_sec=denom,
    )

    return ProfileData(
        total_ms=total_ms,
        total_calls=total_calls,
        primitive_calls=primitive_calls,
        functions=functions,
        flamegraph=flamegraph,
    )


def _build_flamegraph(
    *,
    raw_stats: dict[
        FuncKey, tuple[int, int, float, float, dict[FuncKey, tuple[int, int, float, float]]]
    ],
    callees_map: dict[FuncKey, list[tuple[FuncKey, float, int]]],
    app_root: str | None,
    total_sec: float,
    max_depth: int = 18,
    max_children: int = 14,
) -> dict[str, Any]:
    # Identify root nodes (functions with no recorded callers inside this profile)
    roots: list[tuple[FuncKey, float]] = []
    for key, (_cc, _nc, _tt, ct, callers) in raw_stats.items():
        if key[2] == "disable" and "cProfile" in key[0]:
            continue
        internal_callers = [c for c in callers if c in raw_stats]
        if not internal_callers:
            roots.append((key, ct))

    if not roots:
        # Fallback to highest cumulative-time function
        best_key = max(raw_stats.items(), key=lambda item: item[1][3])[0]
        roots = [(best_key, raw_stats[best_key][3])]

    roots.sort(key=lambda item: -item[1])
    min_threshold = total_sec * 0.004  # prune <0.4% noise leaves

    def _build_node(
        key: FuncKey,
        value_sec: float,
        calls: int,
        depth: int,
        path: set[FuncKey],
    ) -> dict[str, Any]:
        filename, lineno, func_name = key
        entry = raw_stats[key]
        self_ms = round(entry[2] * 1000.0, 3)
        cum_ms = round(value_sec * 1000.0, 3)
        node: dict[str, Any] = {
            "name": func_name,
            "file": shorten_filename(filename, app_root),
            "line": lineno,
            "value": max(cum_ms, 0.001),
            "self_ms": self_ms,
            "calls": calls,
            "category": classify_frame(filename, app_root),
            "children": [],
        }

        if depth >= max_depth:
            return node

        raw_children = callees_map.get(key, [])
        if not raw_children:
            return node

        # Sort children by attributed edge cumulative time
        sorted_children = sorted(raw_children, key=lambda item: -item[1])
        children_nodes: list[dict[str, Any]] = []
        child_total = 0.0

        for callee_key, edge_ct, edge_nc in sorted_children:
            if callee_key in path:
                continue
            if edge_ct < min_threshold and len(children_nodes) >= 4:
                continue
            if len(children_nodes) >= max_children:
                break
            effective_sec = min(edge_ct, value_sec)
            if effective_sec <= 0:
                effective_sec = raw_stats[callee_key][2]
            if effective_sec <= 0:
                continue
            path.add(callee_key)
            children_nodes.append(_build_node(callee_key, effective_sec, edge_nc, depth + 1, path))
            path.remove(callee_key)
            child_total += effective_sec

        # Normalize child values if overlapping call edges exceed parent width
        if child_total > value_sec > 0 and children_nodes:
            scale = value_sec / child_total
            for child in children_nodes:
                child["value"] = round(child["value"] * scale, 3)

        node["children"] = children_nodes
        return node

    root_children: list[dict[str, Any]] = []
    for root_key, root_ct in roots[:8]:
        root_calls = raw_stats[root_key][1]
        root_children.append(_build_node(root_key, root_ct, root_calls, 1, {root_key}))

    if len(root_children) == 1:
        return root_children[0]

    return {
        "name": "request",
        "file": "",
        "line": 0,
        "value": round(sum(c["value"] for c in root_children), 3) or round(total_sec * 1000.0, 3),
        "self_ms": 0.0,
        "calls": 1,
        "category": "framework",
        "children": root_children,
    }


def _is_within(path: str, directory: str) -> bool:
    try:
        return os.path.commonpath([path, directory]) == directory
    except ValueError:
        return False


def classify_frame(filename: str, app_root: str | None) -> str:
    """Classify a filename as 'user', 'framework', 'stdlib', or 'third_party'."""
    if not filename or filename.startswith("<") or filename == "~":
        return "stdlib"
    abs_path = os.path.abspath(filename)
    if any(marker in abs_path for marker in _FRAMEWORK_MARKERS):
        return "framework"
    if "site-packages" in abs_path or "dist-packages" in abs_path:
        return "third_party"
    if any(_is_within(abs_path, os.path.abspath(prefix)) for prefix in _STDLIB_DIRS):
        return "stdlib"
    if app_root and _is_within(abs_path, app_root):
        return "user"
    return "user"


def shorten_filename(filename: str, app_root: str | None) -> str:
    if not filename or filename == "~":
        return "<built-in>"
    if filename.startswith("<"):
        return filename
    abs_path = os.path.abspath(filename)
    if app_root and _is_within(abs_path, app_root):
        return os.path.relpath(abs_path, app_root)
    parts = abs_path.split(os.sep)
    if len(parts) > 3:
        return os.sep.join(parts[-3:])
    return abs_path
