"""Flask Blueprint serving the embedded Lens UI and JSON/SSE telemetry API."""

from __future__ import annotations

import json
import os
import queue
from collections.abc import Iterator
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request, send_from_directory

from flask_lens.extension import LensState
from flask_lens.storage.base import RequestFilter, compute_request_diff

_UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


def _get_state() -> LensState:
    state: LensState | None = current_app.extensions.get("lens")
    if state is None:
        raise RuntimeError("flask-lens is not initialized on this Flask application.")
    return state


def create_lens_blueprint() -> Blueprint:
    """Create the ``flask_lens`` Blueprint mounted at ``config.url_prefix``."""
    bp = Blueprint("flask_lens", __name__)

    @bp.before_request
    def _authorize() -> Response | None:
        state = _get_state()
        auth_fn = state.config.authorize
        if auth_fn is not None and not auth_fn(request):
            if request.path.endswith("/api") or "/api/" in request.path:
                resp = jsonify({"error": "Forbidden", "message": "Access to flask-lens denied."})
                resp.status_code = 403
                return resp
            return Response(
                "<h1>403 Forbidden</h1><p>Access to flask-lens is restricted.</p>",
                status=403,
                mimetype="text/html",
            )
        return None

    # ── Embedded UI routes ────────────────────────────────────────────────

    @bp.route("/")
    @bp.route("/requests/<request_id>")
    @bp.route("/endpoints")
    @bp.route("/sql")
    @bp.route("/exceptions")
    @bp.route("/compare")
    def ui_index(request_id: str | None = None) -> Response:
        state = _get_state()
        index_path = os.path.join(_UI_DIR, "index.html")
        with open(index_path, encoding="utf-8") as fh:
            html = fh.read()
        base_url = state.config.url_prefix
        html = html.replace("__LENS_BASE_URL__", base_url)
        return Response(html, mimetype="text/html; charset=utf-8")

    @bp.route("/assets/<path:filename>")
    def ui_asset(filename: str) -> Response:
        return send_from_directory(_UI_DIR, filename)

    # ── Telemetry REST API ────────────────────────────────────────────────

    @bp.get("/api/health")
    def api_health() -> Response:
        state = _get_state()
        cfg = state.config
        return jsonify(
            {
                "status": "ok",
                "version": "0.1.0",
                "url_prefix": cfg.url_prefix,
                "storage": cfg.storage,
                "profile_mode": cfg.profile_mode,
                "slow_query_ms": cfg.slow_query_ms,
                "n_plus_one_threshold": cfg.n_plus_one_threshold,
                "allow_explain": cfg.allow_explain,
                "allow_replay": cfg.allow_replay,
                "watched_engines": sorted(state.watched_engines.keys()),
            }
        )

    @bp.get("/api/overview")
    def api_overview() -> Response:
        state = _get_state()
        return jsonify(state.storage.overview_stats())

    @bp.get("/api/requests")
    def api_list_requests() -> Response:
        state = _get_state()
        limit = _parse_int(request.args.get("limit"), default=100, minimum=1, maximum=500)
        offset = _parse_int(request.args.get("offset"), default=0, minimum=0, maximum=100_000)
        min_ms_raw = request.args.get("min_ms")
        min_ms: float | None = None
        if min_ms_raw:
            try:
                min_ms = float(min_ms_raw)
            except ValueError:
                min_ms = None

        flt = RequestFilter(
            limit=limit,
            offset=offset,
            method=request.args.get("method") or None,
            status_group=request.args.get("status") or None,
            search=request.args.get("q") or None,
            only_n_plus_one=_parse_bool(request.args.get("n_plus_one")),
            only_slow=_parse_bool(request.args.get("slow")),
            only_errors=_parse_bool(request.args.get("errors")),
            min_duration_ms=min_ms,
        )
        items, total = state.storage.list_requests(flt)
        return jsonify({"items": items, "total": total, "limit": limit, "offset": offset})

    @bp.get("/api/requests/<request_id>")
    def api_get_request(request_id: str) -> tuple[Response, int] | Response:
        state = _get_state()
        captured = state.storage.get(request_id)
        if captured is None:
            return jsonify({"error": "Not found", "request_id": request_id}), 404
        return jsonify(captured.to_dict())

    @bp.post("/api/requests/<request_id>/explain/<query_id>")
    def api_explain_query(request_id: str, query_id: str) -> tuple[Response, int] | Response:
        state = _get_state()
        result = state.explain_query(request_id, query_id)
        status = 200 if result.error is None else 400
        return jsonify(result.to_dict()), status

    @bp.post("/api/replay")
    def api_replay() -> tuple[Response, int] | Response:
        state = _get_state()
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        method = str(payload.get("method", "GET"))
        path = str(payload.get("path", "/"))
        headers = payload.get("headers")
        body = payload.get("body")
        original_id = payload.get("original_request_id")

        if headers is not None and not isinstance(headers, dict):
            return jsonify({"error": "headers must be a JSON object"}), 400
        if body is not None and not isinstance(body, str):
            body = json.dumps(body)

        try:
            result = state.replay_request(
                method=method,
                path=path,
                headers=headers,
                body=body,
                original_request_id=str(original_id) if original_id else None,
            )
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result)

    @bp.get("/api/compare")
    def api_compare() -> tuple[Response, int] | Response:
        state = _get_state()
        baseline_id = request.args.get("baseline", "").strip()
        candidate_id = request.args.get("candidate", "").strip()
        if not baseline_id or not candidate_id:
            return jsonify(
                {"error": "Both 'baseline' and 'candidate' query parameters are required."}
            ), 400

        baseline = state.storage.get(baseline_id)
        candidate = state.storage.get(candidate_id)
        if baseline is None:
            return jsonify({"error": f"Baseline request {baseline_id!r} not found."}), 404
        if candidate is None:
            return jsonify({"error": f"Candidate request {candidate_id!r} not found."}), 404
        return jsonify(compute_request_diff(baseline, candidate))

    @bp.get("/api/endpoints")
    def api_endpoints() -> Response:
        state = _get_state()
        return jsonify({"items": state.storage.endpoint_stats()})

    @bp.get("/api/sql")
    def api_sql() -> Response:
        state = _get_state()
        limit = _parse_int(request.args.get("limit"), default=100, minimum=1, maximum=500)
        return jsonify({"items": state.storage.sql_stats(limit=limit)})

    @bp.get("/api/exceptions")
    def api_exceptions() -> Response:
        state = _get_state()
        return jsonify({"items": state.storage.exception_groups()})

    @bp.route("/api/clear", methods=["POST", "DELETE"])
    @bp.delete("/api/requests")
    def api_clear() -> Response:
        state = _get_state()
        deleted = state.storage.clear()
        return jsonify({"cleared": deleted})

    @bp.get("/api/stream")
    def api_stream() -> Response:
        state = _get_state()
        max_events = _parse_int(request.args.get("max_events"), default=0, minimum=0, maximum=1000)
        sub_queue = state.broadcaster.subscribe()

        def _event_stream() -> Iterator[str]:
            sent = 0
            try:
                yield "event: connected\ndata: {}\n\n"
                while True:
                    if max_events > 0 and sent >= max_events:
                        break
                    try:
                        item = sub_queue.get(timeout=15.0)
                        payload = json.dumps(item, ensure_ascii=False)
                        yield f"event: request\ndata: {payload}\n\n"
                        sent += 1
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        if max_events > 0:
                            break
            finally:
                state.broadcaster.unsubscribe(sub_queue)

        return Response(
            _event_stream(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return bp


def _parse_int(raw: str | None, *, default: int, minimum: int, maximum: int) -> int:
    if raw is None:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, val))


def _parse_bool(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}
