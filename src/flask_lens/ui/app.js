/* flask-lens DevTools SPA — Zero external dependencies */

(function () {
  "use strict";

  const BASE = (
    document.querySelector('meta[name="lens-base-url"]')?.getAttribute("content") || "/__lens"
  ).replace(/\/$/, "");

  const state = {
    view: "requests",
    live: true,
    sse: null,
    requests: [],
    totalRequests: 0,
    selectedId: null,
    selectedDetail: null,
    detailTab: "overview",
    sqlFilter: "all",
    explainCache: {},
    expandedStacks: {},
    flameZoomNode: null,
    profileUserOnly: false,
    selectedExcFrame: -1,
    compareBaseline: "",
    compareCandidate: "",
    filters: {
      q: "",
      method: "",
      status: "",
      n1: false,
      slow: false,
      errors: false,
    },
  };

  // ── Utilities ──────────────────────────────────────────────────────────

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function esc(val) {
    return String(val ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtInt(val) {
    return Number(val || 0).toLocaleString("en-US");
  }

  function fmtSigned(val, decimals = 2) {
    const n = Number(val || 0);
    const fixed = Math.abs(n) < 1e-9 ? (0).toFixed(decimals) : n.toFixed(decimals);
    return n > 1e-9 ? `+${fixed}` : fixed;
  }

  function fmtSignedInt(val) {
    const n = Number(val || 0);
    return n > 0 ? `+${n}` : `${n}`;
  }

  function renderInlineCode(text) {
    return esc(text).replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>');
  }

  function fmtMs(ms) {
    const n = Number(ms || 0);
    if (n >= 1000) return `${(n / 1000).toFixed(2)}s`;
    if (n >= 10) return `${n.toFixed(1)}ms`;
    return `${n.toFixed(2)}ms`;
  }

  function fmtBytes(bytes) {
    const b = Number(bytes || 0);
    if (b < 1024) return `${b} B`;
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
    return `${(b / (1024 * 1024)).toFixed(2)} MB`;
  }

  function fmtRelative(epochSec) {
    const diff = Math.max(0, Math.floor(Date.now() / 1000 - Number(epochSec || 0)));
    if (diff < 3) return "just now";
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return new Date(epochSec * 1000).toLocaleTimeString();
  }

  function methodClass(method) {
    const m = String(method || "").toUpperCase();
    return ["GET", "POST", "PUT", "PATCH", "DELETE"].includes(m) ? `m-${m}` : "m-OTHER";
  }

  function statusClass(code) {
    const c = Number(code || 0);
    if (c >= 500) return "s-5xx";
    if (c >= 400) return "s-4xx";
    if (c >= 300) return "s-3xx";
    return "s-2xx";
  }

  function highlightSQL(sql) {
    const keywords = new Set([
      "SELECT", "FROM", "WHERE", "JOIN", "INNER", "LEFT", "RIGHT", "OUTER", "FULL",
      "ON", "AS", "AND", "OR", "NOT", "IN", "IS", "NULL", "ORDER", "BY", "GROUP",
      "HAVING", "LIMIT", "OFFSET", "INSERT", "INTO", "VALUES", "UPDATE", "SET",
      "DELETE", "CREATE", "ALTER", "DROP", "TABLE", "INDEX", "DISTINCT", "UNION",
      "ALL", "WITH", "CASE", "WHEN", "THEN", "ELSE", "END", "ASC", "DESC", "LIKE",
      "ILIKE", "BETWEEN", "EXISTS", "RETURNING", "COUNT", "SUM", "AVG", "MIN", "MAX",
    ]);
    const tokenRe = /('(?:''|[^'])*'|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_]*\b)/g;
    let out = "";
    let last = 0;
    let m;
    while ((m = tokenRe.exec(sql)) !== null) {
      out += esc(sql.slice(last, m.index));
      const tok = m[0];
      if (tok.startsWith("'")) {
        out += `<span class="sql-str">${esc(tok)}</span>`;
      } else if (/^\d/.test(tok)) {
        out += `<span class="sql-num">${esc(tok)}</span>`;
      } else if (keywords.has(tok.toUpperCase())) {
        out += `<span class="sql-kw">${esc(tok)}</span>`;
      } else {
        out += esc(tok);
      }
      last = tokenRe.lastIndex;
    }
    out += esc(sql.slice(last));
    return out;
  }

  let toastTimer = null;
  function showToast(msg) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 2200);
  }

  async function apiFetch(path, options = {}) {
    const res = await fetch(`${BASE}${path}`, options);
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || data.message || `HTTP ${res.status}`);
    }
    return data;
  }

  // ── Theme ──────────────────────────────────────────────────────────────

  function initTheme() {
    const saved = localStorage.getItem("flask-lens-theme") || "dark";
    document.documentElement.setAttribute("data-theme", saved);
    updateThemeIcon(saved);
  }

  function toggleTheme() {
    const current = document.documentElement.getAttribute("data-theme") || "dark";
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("flask-lens-theme", next);
    updateThemeIcon(next);
  }

  function updateThemeIcon(theme) {
    const svg = $("#theme-icon");
    if (theme === "dark") {
      svg.innerHTML = `<path d="M13.5 9.5A5.5 5.5 0 016.5 2.5a5.5 5.5 0 107 7z"/>`;
    } else {
      svg.innerHTML = `<circle cx="8" cy="8" r="3"/><path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1"/>`;
    }
  }

  // ── Data Fetching ──────────────────────────────────────────────────────

  async function refreshAll() {
    await Promise.all([loadOverview(), loadRequests()]);
    if (state.view === "endpoints") await loadEndpoints();
    if (state.view === "sql") await loadSqlExplorer();
    if (state.view === "exceptions") await loadExceptions();
    if (state.view === "compare") populateComparePickers();
  }

  async function loadOverview() {
    try {
      const ov = await apiFetch("/api/overview");
      $("#kpi-total").textContent = ov.total_requests;
      $("#kpi-avg").textContent = fmtMs(ov.avg_duration_ms);
      $("#kpi-p95").textContent = fmtMs(ov.p95_duration_ms);
      $("#kpi-sql").textContent = ov.total_sql_queries;

      const n1El = $("#kpi-n1");
      n1El.textContent = ov.n_plus_one_requests;
      n1El.className = `metric-value mono ${ov.n_plus_one_requests > 0 ? "warn" : ""}`;

      const errEl = $("#kpi-err");
      errEl.textContent = `${ov.error_rate}%`;
      errEl.className = `metric-value mono ${ov.error_count > 0 ? "danger" : ""}`;

      $("#nav-count-requests").textContent = ov.total_requests;
      const excBadge = $("#nav-count-exceptions");
      excBadge.textContent = ov.exception_count;
      excBadge.classList.toggle("hidden", ov.exception_count === 0);

      renderSparkline(ov.latency_series || []);
    } catch (err) {
      console.error("Failed to load overview:", err);
    }
  }

  function renderSparkline(series) {
    const svg = $("#latency-sparkline");
    if (!series.length) {
      svg.innerHTML = `<line x1="0" y1="22" x2="220" y2="22" stroke="var(--border)" stroke-width="1"/>`;
      return;
    }
    const maxMs = Math.max(...series.map((s) => s.duration_ms), 1);
    const barW = Math.max(3, Math.floor(216 / Math.max(series.length, 1)) - 1);
    const gap = 1;
    const totalW = series.length * (barW + gap);
    const startX = Math.max(0, 220 - totalW);

    svg.innerHTML = series
      .map((item, i) => {
        const h = Math.max(3, Math.round((item.duration_ms / maxMs) * 22));
        const x = startX + i * (barW + gap);
        const y = 24 - h;
        let fill = "var(--accent)";
        if (item.status_code >= 500) fill = "var(--red)";
        else if (item.has_n_plus_one) fill = "var(--amber)";
        return `<rect x="${x}" y="${y}" width="${barW}" height="${h}" rx="1.2" fill="${fill}" opacity="0.88"><title>${esc(item.method)} ${esc(item.path)} — ${fmtMs(item.duration_ms)} (${item.sql_count} SQL)</title></rect>`;
      })
      .join("");
  }

  async function loadRequests() {
    const params = new URLSearchParams({ limit: "150" });
    if (state.filters.q) params.set("q", state.filters.q);
    if (state.filters.method) params.set("method", state.filters.method);
    if (state.filters.status) params.set("status", state.filters.status);
    if (state.filters.n1) params.set("n_plus_one", "1");
    if (state.filters.slow) params.set("slow", "1");
    if (state.filters.errors) params.set("errors", "1");

    try {
      const data = await apiFetch(`/api/requests?${params.toString()}`);
      state.requests = data.items || [];
      state.totalRequests = data.total || 0;
      renderRequestList();
      if (!state.selectedId && state.requests.length > 0) {
        selectRequest(state.requests[0].id);
      }
    } catch (err) {
      console.error("Failed to load requests:", err);
    }
  }

  function renderRequestList() {
    const container = $("#request-list");
    if (!state.requests.length) {
      container.innerHTML = `
        <div class="empty-state">
          <div class="empty-title">No matching requests</div>
          <div class="empty-sub">Trigger a request in your Flask app or relax active filters.</div>
        </div>`;
      return;
    }

    container.innerHTML = state.requests
      .map((req) => {
        const selected = req.id === state.selectedId ? "selected" : "";
        const tags = [];
        if (req.sql_count > 0) {
          tags.push(`<span class="tag tag-neutral mono">${req.sql_count} SQL</span>`);
        }
        if (req.has_n_plus_one) {
          tags.push(`<span class="tag tag-n1">N+1</span>`);
        }
        if (req.has_slow_sql) {
          tags.push(`<span class="tag tag-slow">SLOW</span>`);
        }
        if (req.has_exception) {
          tags.push(`<span class="tag tag-err">EXC</span>`);
        }
        if (req.is_replay) {
          tags.push(`<span class="tag tag-replay">REPLAY</span>`);
        }
        return `
          <div class="req-row ${selected}" data-id="${esc(req.id)}" role="option" aria-selected="${Boolean(selected)}">
            <div class="req-row-top">
              <span class="method-badge mono ${methodClass(req.method)}">${esc(req.method)}</span>
              <span class="req-path mono" title="${esc(req.full_path)}">${esc(req.full_path)}</span>
              <span class="status-pill mono ${statusClass(req.status_code)}">${req.status_code}</span>
            </div>
            <div class="req-row-meta">
              <div class="req-tags">${tags.join("")}</div>
              <div class="mono">
                <span>${fmtMs(req.duration_ms)}</span>
                <span class="muted" data-ts="${esc(req.timestamp)}"> · ${fmtRelative(req.timestamp)}</span>
              </div>
            </div>
          </div>`;
      })
      .join("");
  }

  async function selectRequest(id, preferredTab = null) {
    state.selectedId = id;
    state.flameZoomNode = null;
    state.selectedExcFrame = -1;
    renderRequestList();
    if (state.view === "requests" && id) {
      window.history?.replaceState?.(null, "", `${BASE}/requests/${encodeURIComponent(id)}`);
    }

    try {
      const detail = await apiFetch(`/api/requests/${encodeURIComponent(id)}`);
      state.selectedDetail = detail;
      if (preferredTab) {
        state.detailTab = preferredTab;
      } else if (state.detailTab === "exception" && !detail.exception) {
        state.detailTab = "overview";
      } else if (state.detailTab === "flamegraph" && !detail.profile) {
        state.detailTab = "overview";
      }
      renderDetailPane();
    } catch (err) {
      showToast(`Failed to load request: ${err.message}`);
    }
  }

  // ── Detail Inspector ───────────────────────────────────────────────────

  function renderDetailPane() {
    const pane = $("#detail-pane");
    const req = state.selectedDetail;
    if (!req) {
      pane.innerHTML = `<div class="empty-state full-height"><div class="empty-title">Select a request to inspect</div></div>`;
      return;
    }

    const tabs = [
      { id: "overview", label: "Overview" },
      { id: "sql", label: `SQL (${req.sql_count})`, alert: req.has_n_plus_one },
      { id: "flamegraph", label: "Flamegraph", hidden: !req.profile },
      { id: "payload", label: "Request / Response" },
      { id: "http", label: `Outgoing HTTP (${(req.outgoing_http || []).length})` },
      { id: "logs", label: `Logs & Spans (${(req.logs || []).length + (req.spans || []).length})` },
      { id: "exception", label: "Exception", hidden: !req.exception, danger: true },
    ];

    pane.innerHTML = `
      <div class="detail-header">
        <div class="detail-title-group">
          <span class="method-badge mono ${methodClass(req.method)}">${esc(req.method)}</span>
          <span class="detail-path mono" title="${esc(req.full_path)}">${esc(req.full_path)}</span>
          <span class="status-pill mono ${statusClass(req.status_code)}">${req.status_code}</span>
          <span class="tag tag-neutral mono">${fmtMs(req.duration_ms)}</span>
          ${req.has_n_plus_one ? `<span class="tag tag-n1">N+1 Detected</span>` : ""}
          ${req.is_replay ? `<span class="tag tag-replay">Replayed</span>` : ""}
        </div>
        <div class="detail-actions">
          <button class="btn" id="btn-replay">Replay</button>
          <button class="btn" id="btn-compare-pin">Compare</button>
          <button class="btn" id="btn-copy-curl">Copy cURL</button>
        </div>
      </div>
      <div class="sub-tabs" role="tablist">
        ${tabs
          .filter((t) => !t.hidden)
          .map(
            (t) => `
            <button class="sub-tab ${state.detailTab === t.id ? "active" : ""} ${t.danger ? "has-alert" : ""}" data-subtab="${t.id}">
              ${esc(t.label)}
              ${t.alert ? `<span class="tag tag-n1">!</span>` : ""}
            </button>`
          )
          .join("")}
      </div>
      <div class="detail-body" id="detail-body"></div>
    `;

    $$(".sub-tab", pane).forEach((btn) => {
      btn.addEventListener("click", () => {
        state.detailTab = btn.dataset.subtab;
        renderDetailPane();
      });
    });

    $("#btn-replay", pane).addEventListener("click", () => openReplayModal(req));
    $("#btn-copy-curl", pane).addEventListener("click", () => copyAsCurl(req));
    $("#btn-compare-pin", pane).addEventListener("click", () => {
      if (!state.compareBaseline || state.compareBaseline === req.id) {
        state.compareBaseline = req.id;
        showToast("Pinned as Compare Baseline (Before). Pick another request to diff.");
      } else {
        state.compareCandidate = req.id;
        switchView("compare");
        runComparison();
      }
    });

    const body = $("#detail-body", pane);
    if (state.detailTab === "overview") renderOverviewTab(body, req);
    else if (state.detailTab === "sql") renderSqlTab(body, req);
    else if (state.detailTab === "flamegraph") renderFlamegraphTab(body, req);
    else if (state.detailTab === "payload") renderPayloadTab(body, req);
    else if (state.detailTab === "http") renderOutgoingHttpTab(body, req);
    else if (state.detailTab === "logs") renderLogsTab(body, req);
    else if (state.detailTab === "exception") renderExceptionTab(body, req);
  }

  function renderOverviewTab(container, req) {
    const totalMs = Math.max(req.duration_ms || 0.01, 0.01);
    const sqlMs = req.sql_duration_ms || 0;
    const httpMs = (req.outgoing_http || []).reduce((acc, h) => acc + (h.duration_ms || 0), 0);
    const appMs = Math.max(0, totalMs - sqlMs - httpMs);

    const sqlPct = Math.min(100, (sqlMs / totalMs) * 100);
    const httpPct = Math.min(100 - sqlPct, (httpMs / totalMs) * 100);
    const appPct = Math.max(0, 100 - sqlPct - httpPct);

    let html = "";

    if (req.exception) {
      html += `
        <div class="callout-err">
          <div class="callout-title">
            <span>Unhandled ${esc(req.exception.exc_type)}: ${esc(req.exception.message)}</span>
            <button class="btn sm" id="jump-exc-btn">Inspect Traceback →</button>
          </div>
        </div>`;
    }

    if (req.n_plus_one_groups && req.n_plus_one_groups.length > 0) {
      html += req.n_plus_one_groups
        .map(
          (grp) => `
          <div class="callout-warn">
            <div class="callout-title">
              <span>N+1 Query Pattern — ${grp.count}× executions (${fmtMs(grp.total_ms)})</span>
              <span class="mono" style="font-size:11.5px">${esc(grp.call_site || "")}</span>
            </div>
            <div style="font-size:12px;margin-bottom:6px;color:var(--text-primary)">${renderInlineCode(grp.suggestion)}</div>
            <pre class="code-block" style="border-radius:5px;border:1px solid var(--border)">${highlightSQL(grp.normalized_sql)}</pre>
          </div>`
        )
        .join("");
    }

    html += `
      <div class="card">
        <div class="card-header">
          <span>Execution Breakdown</span>
          <span class="mono muted">Total ${fmtMs(req.duration_ms)}</span>
        </div>
        <div class="card-body">
          <div class="breakdown-bar">
            <div class="seg-app" style="width:${appPct.toFixed(1)}%" title="Python / Flask: ${fmtMs(appMs)}"></div>
            <div class="seg-sql" style="width:${sqlPct.toFixed(1)}%" title="SQL: ${fmtMs(sqlMs)}"></div>
            <div class="seg-http" style="width:${httpPct.toFixed(1)}%" title="Outgoing HTTP: ${fmtMs(httpMs)}"></div>
          </div>
          <div class="legend-row mono">
            <span class="legend-item"><span class="legend-dot seg-app"></span>App Python: ${fmtMs(appMs)} (${appPct.toFixed(0)}%)</span>
            <span class="legend-item"><span class="legend-dot seg-sql"></span>SQL (${req.sql_count}): ${fmtMs(sqlMs)} (${sqlPct.toFixed(0)}%)</span>
            <span class="legend-item"><span class="legend-dot seg-http"></span>Outgoing HTTP (${(req.outgoing_http || []).length}): ${fmtMs(httpMs)} (${httpPct.toFixed(0)}%)</span>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-header"><span>Request Metadata</span><span class="mono muted">ID: ${esc(req.id)}</span></div>
        <div class="card-body">
          <div class="kv-grid">
            <div class="kv-item"><div class="kv-label">Route Rule</div><div class="kv-val mono">${esc(req.rule || req.path)}</div></div>
            <div class="kv-item"><div class="kv-label">Endpoint</div><div class="kv-val mono">${esc(req.endpoint || "—")}</div></div>
            <div class="kv-item"><div class="kv-label">Response Size</div><div class="kv-val mono">${fmtBytes(req.response_size)}</div></div>
            <div class="kv-item"><div class="kv-label">Content-Type</div><div class="kv-val mono">${esc(req.response_content_type || "—")}</div></div>
            <div class="kv-item"><div class="kv-label">Duplicate SQL</div><div class="kv-val mono">${req.duplicate_sql_count}</div></div>
            <div class="kv-item"><div class="kv-label">Templates</div><div class="kv-val mono">${(req.templates || []).length ? esc(req.templates.join(", ")) : "—"}</div></div>
          </div>
        </div>
      </div>
    `;

    // Build unified waterfall timeline
    const timeline = [];
    for (const q of req.sql_queries || []) {
      timeline.push({
        kind: q.is_n_plus_one ? "sql-n1" : q.is_slow ? "sql-slow" : "sql",
        badge: q.operation,
        label: q.normalized_sql,
        offset: q.offset_ms || 0,
        duration: q.duration_ms || 0,
        jumpTab: "sql",
      });
    }
    for (const h of req.outgoing_http || []) {
      timeline.push({
        kind: "http",
        badge: h.method,
        label: h.url,
        offset: h.offset_ms || 0,
        duration: h.duration_ms || 0,
        jumpTab: "http",
      });
    }
    for (const sp of req.spans || []) {
      timeline.push({
        kind: sp.kind === "template" ? "template" : "span",
        badge: sp.kind.toUpperCase(),
        label: sp.name,
        offset: sp.offset_ms || 0,
        duration: sp.duration_ms || 0,
        jumpTab: "logs",
      });
    }
    timeline.sort((a, b) => a.offset - b.offset);

    html += `
      <div class="card">
        <div class="card-header">
          <span>Timeline Waterfall</span>
          <span class="mono muted">${timeline.length} event${timeline.length === 1 ? "" : "s"}</span>
        </div>
        ${
          timeline.length === 0
            ? `<div class="card-body muted">No SQL queries, outgoing HTTP calls, or custom spans recorded for this request.</div>`
            : timeline
                .map((ev) => {
                  const leftPct = Math.min(98, Math.max(0, (ev.offset / totalMs) * 100));
                  const widthPct = Math.min(100 - leftPct, Math.max(0.8, (ev.duration / totalMs) * 100));
                  return `
                    <div class="waterfall-row clickable" data-jump="${ev.jumpTab}">
                      <div class="wf-label" title="${esc(ev.label)}">
                        <span class="tag tag-neutral mono">${esc(ev.badge)}</span>
                        <span class="mono" style="overflow:hidden;text-overflow:ellipsis">${esc(ev.label)}</span>
                      </div>
                      <div class="wf-track">
                        <div class="wf-bar ${ev.kind}" style="left:${leftPct.toFixed(2)}%;width:${widthPct.toFixed(2)}%"></div>
                      </div>
                      <div class="wf-dur mono">${fmtMs(ev.duration)}</div>
                    </div>`;
                })
                .join("")
        }
      </div>`;

    container.innerHTML = html;

    const jumpExc = $("#jump-exc-btn", container);
    if (jumpExc) {
      jumpExc.addEventListener("click", () => {
        state.detailTab = "exception";
        renderDetailPane();
      });
    }
    $$(".waterfall-row[data-jump]", container).forEach((row) => {
      row.addEventListener("click", () => {
        state.detailTab = row.dataset.jump;
        renderDetailPane();
      });
    });
  }

  function renderSqlTab(container, req) {
    const queries = req.sql_queries || [];
    if (!queries.length) {
      container.innerHTML = `<div class="empty-state"><div class="empty-title">No SQL queries captured</div><div class="empty-sub">Pass <code>db=db</code> or <code>engine=engine</code> to <code>Lens(app, db=db)</code> to trace SQLAlchemy queries.</div></div>`;
      return;
    }

    const filtered = queries.filter((q) => {
      if (state.sqlFilter === "n1") return q.is_n_plus_one;
      if (state.sqlFilter === "slow") return q.is_slow;
      if (state.sqlFilter === "dup") return q.is_duplicate;
      return true;
    });

    const filtersHtml = `
      <div class="sql-toolbar">
        <div class="req-tags">
          ${[
            ["all", `All (${queries.length})`],
            ["n1", `N+1 (${queries.filter((q) => q.is_n_plus_one).length})`],
            ["slow", `Slow (${queries.filter((q) => q.is_slow).length})`],
            ["dup", `Duplicates (${queries.filter((q) => q.is_duplicate).length})`],
          ]
            .map(
              ([id, label]) =>
                `<button class="btn sm ${state.sqlFilter === id ? "primary" : ""}" data-sqlfilter="${id}">${label}</button>`
            )
            .join("")}
        </div>
        <span class="mono muted">Total SQL: ${fmtMs(req.sql_duration_ms)}</span>
      </div>`;

    const cardsHtml = filtered
      .map((q, idx) => {
        const expKey = `${req.id}:${q.id}`;
        const explain = state.explainCache[expKey];
        const stackOpen = Boolean(state.expandedStacks[q.id]);
        const cls = q.is_n_plus_one ? "is-n1" : q.is_slow ? "is-slow" : "";
        const paramsJson =
          q.params !== null && q.params !== undefined && JSON.stringify(q.params) !== "[]" && JSON.stringify(q.params) !== "{}"
            ? JSON.stringify(q.params)
            : null;
        const userFrame = (q.stack || []).find((f) => f.is_user_code) || (q.stack || [])[0];

        return `
          <div class="sql-card ${cls}">
            <div class="sql-card-head">
              <div class="sql-card-head-left">
                <span class="mono muted">#${idx + 1}</span>
                <span class="tag tag-neutral mono">${esc(q.operation)}</span>
                ${(q.tables || []).map((t) => `<span class="tag tag-neutral mono">${esc(t)}</span>`).join("")}
                ${q.is_n_plus_one ? `<span class="tag tag-n1">N+1</span>` : ""}
                ${q.is_slow ? `<span class="tag tag-err">SLOW</span>` : ""}
                ${q.is_duplicate ? `<span class="tag tag-dup">DUP</span>` : ""}
              </div>
              <div class="sql-card-head-right">
                <span class="mono" style="font-weight:600">${fmtMs(q.duration_ms)}</span>
                <button class="btn sm copy-sql-btn" data-qid="${esc(q.id)}">Copy</button>
                ${q.operation === "SELECT" ? `<button class="btn sm explain-btn" data-qid="${esc(q.id)}">EXPLAIN</button>` : ""}
                ${q.stack && q.stack.length ? `<button class="btn sm stack-btn" data-qid="${esc(q.id)}">${stackOpen ? "Hide Source" : "Source"}</button>` : ""}
              </div>
            </div>
            <pre class="code-block">${highlightSQL(q.sql)}</pre>
            <div class="sql-meta-row">
              <span class="mono">${q.call_site ? `at ${esc(q.call_site)}` : "—"}</span>
              ${paramsJson ? `<span class="mono muted" title="${esc(paramsJson)}">params: ${esc(paramsJson.slice(0, 110))}${paramsJson.length > 110 ? "…" : ""}</span>` : ""}
            </div>
            ${
              stackOpen && userFrame
                ? `
                <div class="stack-box">
                  <div class="mono" style="font-size:11.5px;font-weight:600">${esc(userFrame.filename)}:${userFrame.lineno} in ${esc(userFrame.function)}</div>
                  ${
                    userFrame.context_lines && userFrame.context_lines.length
                      ? `<div class="source-snippet mono">${userFrame.context_lines
                          .map(
                            ([ln, line]) =>
                              `<div class="src-line ${ln === userFrame.lineno ? "highlight" : ""}"><span class="src-ln">${ln}</span><span>${esc(line)}</span></div>`
                          )
                          .join("")}</div>`
                      : ""
                  }
                </div>`
                : ""
            }
            ${
              explain
                ? `
                <div class="explain-box">
                  <div style="display:flex;justify-content:space-between;margin-bottom:6px">
                    <span class="mono" style="font-size:11px;font-weight:600;text-transform:uppercase">Query Plan (${esc(explain.dialect)})</span>
                  </div>
                  ${(explain.warnings || []).map((w) => `<div class="explain-warning">${esc(w)}</div>`).join("")}
                  ${
                    explain.error
                      ? `<div style="color:var(--red);font-size:12px">${esc(explain.error)}</div>`
                      : `<pre class="code-block" style="border:1px solid var(--border);border-radius:5px">${esc((explain.plan_lines || []).join("\n"))}</pre>`
                  }
                </div>`
                : ""
            }
          </div>`;
      })
      .join("");

    container.innerHTML = filtersHtml + cardsHtml;

    $$("[data-sqlfilter]", container).forEach((btn) => {
      btn.addEventListener("click", () => {
        state.sqlFilter = btn.dataset.sqlfilter;
        renderSqlTab(container, req);
      });
    });

    $$(".copy-sql-btn", container).forEach((btn) => {
      btn.addEventListener("click", () => {
        const q = queries.find((item) => item.id === btn.dataset.qid);
        if (!q) return;
        navigator.clipboard?.writeText(q.sql).then(
          () => showToast("Copied SQL query to clipboard"),
          () => showToast("Copy failed — clipboard permission required")
        );
      });
    });

    $$(".stack-btn", container).forEach((btn) => {
      btn.addEventListener("click", () => {
        const qid = btn.dataset.qid;
        state.expandedStacks[qid] = !state.expandedStacks[qid];
        renderSqlTab(container, req);
      });
    });

    $$(".explain-btn", container).forEach((btn) => {
      btn.addEventListener("click", async () => {
        const qid = btn.dataset.qid;
        btn.textContent = "Running…";
        btn.disabled = true;
        try {
          const res = await fetch(`${BASE}/api/requests/${encodeURIComponent(req.id)}/explain/${encodeURIComponent(qid)}`, {
            method: "POST",
          });
          const data = await res.json();
          state.explainCache[`${req.id}:${qid}`] = data;
          renderSqlTab(container, req);
        } catch (err) {
          showToast(`EXPLAIN failed: ${err.message}`);
          btn.textContent = "EXPLAIN";
          btn.disabled = false;
        }
      });
    });
  }

  function renderFlamegraphTab(container, req) {
    const prof = req.profile;
    if (!prof) {
      container.innerHTML = `<div class="empty-state"><div class="empty-title">No profile captured</div><div class="empty-sub">Enable <code>profile_mode="always"</code>, decorate your view with <code>@lens.profile</code>, or pass header <code>X-Lens-Profile: 1</code>.</div></div>`;
      return;
    }

    const rootNode = state.flameZoomNode || prof.flamegraph;
    const fns = (prof.functions || []).filter((f) => !state.profileUserOnly || f.category === "user");

    container.innerHTML = `
      <div class="flame-controls">
        <div class="legend-row">
          <span class="mono" style="font-weight:600;color:var(--text-primary)">${fmtMs(prof.total_ms)} CPU</span>
          <span class="mono muted">${fmtInt(prof.total_calls)} calls (${fmtInt(prof.primitive_calls)} primitive)</span>
          <span class="legend-item"><span class="legend-dot" style="background:#f59e0b"></span>User Code</span>
          <span class="legend-item"><span class="legend-dot" style="background:#38bdf8"></span>Framework</span>
          <span class="legend-item"><span class="legend-dot" style="background:#818cf8"></span>Third-Party</span>
          <span class="legend-item"><span class="legend-dot" style="background:#475569"></span>Stdlib</span>
        </div>
        <div class="req-tags">
          ${state.flameZoomNode ? `<button class="btn sm" id="flame-reset-btn">Reset Zoom</button>` : ""}
          <button class="btn sm ${state.profileUserOnly ? "primary" : ""}" id="prof-user-only">User Code Only</button>
        </div>
      </div>
      <div class="flame-container" id="flame-svg-wrap"></div>
      <div class="table-card">
        <table class="data-table">
          <thead>
            <tr>
              <th>Function</th>
              <th>Location</th>
              <th>Layer</th>
              <th class="num">Calls</th>
              <th class="num">Self</th>
              <th class="num">Cumulative</th>
              <th class="num">%</th>
            </tr>
          </thead>
          <tbody>
            ${fns
              .slice(0, 60)
              .map(
                (fn) => `
                <tr>
                  <td class="mono cell-trunc" style="font-weight:600;max-width:165px" title="${esc(fn.function)}">${esc(fn.function)}</td>
                  <td class="mono muted cell-trunc" title="${esc(fn.filename)}:${fn.lineno}">${esc(fn.filename)}:${fn.lineno}</td>
                  <td><span class="tag tag-neutral">${esc(fn.category)}</span></td>
                  <td class="num mono">${fmtInt(fn.calls)}</td>
                  <td class="num mono">${fmtMs(fn.self_ms)}</td>
                  <td class="num mono" style="font-weight:600">${fmtMs(fn.cum_ms)}</td>
                  <td class="num mono">${fn.pct_total.toFixed(1)}%</td>
                </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </div>
    `;

    const resetBtn = $("#flame-reset-btn", container);
    if (resetBtn) {
      resetBtn.addEventListener("click", () => {
        state.flameZoomNode = null;
        renderFlamegraphTab(container, req);
      });
    }
    $("#prof-user-only", container).addEventListener("click", () => {
      state.profileUserOnly = !state.profileUserOnly;
      renderFlamegraphTab(container, req);
    });

    if (rootNode) {
      drawFlamegraph($("#flame-svg-wrap", container), rootNode, container, req);
    }
  }

  function drawFlamegraph(wrap, rootNode, tabContainer, req) {
    const width = Math.max(680, wrap.clientWidth - 20);
    const rowH = 22;
    const gap = 2;
    const rects = [];
    let maxDepth = 0;

    function layout(node, depth, x, w) {
      if (w < 1.5) return;
      maxDepth = Math.max(maxDepth, depth);
      rects.push({ node, depth, x, w });
      const children = node.children || [];
      if (!children.length) return;
      const parentVal = Math.max(node.value || 0.001, 0.001);
      let cursor = x;
      for (const child of children) {
        const cw = Math.min(x + w - cursor, (child.value / parentVal) * w);
        if (cw >= 1.5) {
          layout(child, depth + 1, cursor, cw);
          cursor += cw;
        }
      }
    }

    layout(rootNode, 0, 0, width);
    const totalHeight = (maxDepth + 1) * (rowH + gap) + 4;

    const palette = {
      user: "#d97706",
      framework: "#0284c7",
      third_party: "#6366f1",
      stdlib: "#475569",
    };

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("class", "flame-svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${totalHeight}`);
    svg.setAttribute("height", String(totalHeight));

    const tooltip = $("#flame-tooltip");

    rects.forEach((item) => {
      const y = item.depth * (rowH + gap);
      const fill = palette[item.node.category] || palette.third_party;
      const g = document.createElementNS(svgNS, "g");
      g.setAttribute("class", "flame-rect");

      const rect = document.createElementNS(svgNS, "rect");
      rect.setAttribute("x", item.x.toFixed(1));
      rect.setAttribute("y", String(y));
      rect.setAttribute("width", Math.max(1, item.w - 1).toFixed(1));
      rect.setAttribute("height", String(rowH));
      rect.setAttribute("rx", "3");
      rect.setAttribute("fill", fill);
      g.appendChild(rect);

      if (item.w > 46) {
        const text = document.createElementNS(svgNS, "text");
        text.setAttribute("x", (item.x + 5).toFixed(1));
        text.setAttribute("y", String(y + 14.5));
        text.setAttribute("fill", "#f8fafc");
        text.setAttribute("font-size", "11");
        text.setAttribute("font-family", "JetBrains Mono, SF Mono, ui-monospace, monospace");
        const maxChars = Math.floor((item.w - 10) / 6.7);
        const label = `${item.node.name} (${fmtMs(item.node.value)})`;
        text.textContent = label.length > maxChars ? `${label.slice(0, Math.max(1, maxChars - 1))}…` : label;
        g.appendChild(text);
      }

      g.addEventListener("mouseenter", (ev) => {
        tooltip.innerHTML = `
          <div class="mono" style="font-weight:600;margin-bottom:3px">${esc(item.node.name)}</div>
          <div class="mono muted" style="margin-bottom:4px">${esc(item.node.file || "")}${item.node.line ? `:${item.node.line}` : ""}</div>
          <div class="mono">Cumulative: <b>${fmtMs(item.node.value)}</b> · Self: <b>${fmtMs(item.node.self_ms)}</b> · Calls: <b>${item.node.calls}</b></div>
          <div class="muted" style="margin-top:3px">Click frame to zoom</div>`;
        tooltip.classList.remove("hidden");
        positionTooltip(ev);
      });
      g.addEventListener("mousemove", positionTooltip);
      g.addEventListener("mouseleave", () => tooltip.classList.add("hidden"));
      g.addEventListener("click", () => {
        tooltip.classList.add("hidden");
        state.flameZoomNode = item.node;
        renderFlamegraphTab(tabContainer, req);
      });

      svg.appendChild(g);
    });

    wrap.innerHTML = "";
    wrap.appendChild(svg);
  }

  function positionTooltip(ev) {
    const tooltip = $("#flame-tooltip");
    const pad = 14;
    const x = Math.min(window.innerWidth - 375, ev.clientX + pad);
    const y = Math.min(window.innerHeight - 120, ev.clientY + pad);
    tooltip.style.left = `${Math.max(8, x)}px`;
    tooltip.style.top = `${Math.max(8, y)}px`;
  }

  function renderPayloadTab(container, req) {
    const renderHeaders = (headers) => {
      const entries = Object.entries(headers || {});
      if (!entries.length) return `<div class="card-body muted">No headers recorded.</div>`;
      return `
        <table class="data-table">
          <tbody>
            ${entries
              .map(
                ([k, v]) => `
                <tr>
                  <td class="mono" style="width:220px;font-weight:600">${esc(k)}</td>
                  <td class="mono">${v === "[REDACTED]" ? `<span class="tag tag-n1">[REDACTED]</span>` : esc(v)}</td>
                </tr>`
              )
              .join("")}
          </tbody>
        </table>`;
    };

    container.innerHTML = `
      <div class="card">
        <div class="card-header"><span>Request Headers</span><span class="mono muted">${Object.keys(req.request_headers || {}).length} headers</span></div>
        ${renderHeaders(req.request_headers)}
      </div>
      <div class="card">
        <div class="card-header"><span>Request Body</span><span class="mono muted">${esc(req.request_content_type || "none")}</span></div>
        ${req.request_body ? `<pre class="code-block">${esc(req.request_body)}</pre>` : `<div class="card-body muted">Empty request body.</div>`}
      </div>
      <div class="card">
        <div class="card-header"><span>Response Headers</span><span class="mono muted">${Object.keys(req.response_headers || {}).length} headers</span></div>
        ${renderHeaders(req.response_headers)}
      </div>
      <div class="card">
        <div class="card-header"><span>Response Body</span><span class="mono muted">${fmtBytes(req.response_size)}</span></div>
        ${req.response_body ? `<pre class="code-block">${esc(req.response_body)}</pre>` : `<div class="card-body muted">Empty response body.</div>`}
      </div>
    `;
  }

  function renderOutgoingHttpTab(container, req) {
    const calls = req.outgoing_http || [];
    if (!calls.length) {
      container.innerHTML = `<div class="empty-state"><div class="empty-title">No outgoing HTTP calls</div><div class="empty-sub">Calls made with <code>requests</code>, <code>urllib3</code>, or <code>httpx</code> during this request appear here automatically.</div></div>`;
      return;
    }

    container.innerHTML = `
      <div class="table-card">
        <table class="data-table">
          <thead>
            <tr>
              <th>Method</th>
              <th>URL</th>
              <th>Status</th>
              <th>Library</th>
              <th class="num">Offset</th>
              <th class="num">Duration</th>
            </tr>
          </thead>
          <tbody>
            ${calls
              .map(
                (c) => `
                <tr>
                  <td><span class="method-badge mono ${methodClass(c.method)}">${esc(c.method)}</span></td>
                  <td class="mono">${esc(c.url)}${c.error ? `<div style="color:var(--red);font-size:11px">${esc(c.error)}</div>` : ""}</td>
                  <td>${c.status_code ? `<span class="status-pill mono ${statusClass(c.status_code)}">${c.status_code}</span>` : `<span class="tag tag-err">ERR</span>`}</td>
                  <td class="mono muted">${esc(c.library)}</td>
                  <td class="num mono muted">+${fmtMs(c.offset_ms)}</td>
                  <td class="num mono" style="font-weight:600">${fmtMs(c.duration_ms)}</td>
                </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </div>`;
  }

  function renderLogsTab(container, req) {
    const logs = req.logs || [];
    const spans = req.spans || [];

    let html = "";
    if (spans.length) {
      html += `
        <div class="card">
          <div class="card-header"><span>Custom Spans, Events &amp; Templates</span><span class="mono muted">${spans.length}</span></div>
          <table class="data-table">
            <thead><tr><th>Kind</th><th>Name</th><th>Metadata</th><th class="num">Offset</th><th class="num">Duration</th></tr></thead>
            <tbody>
              ${spans
                .map(
                  (s) => `
                  <tr>
                    <td><span class="tag tag-neutral mono">${esc(s.kind)}</span></td>
                    <td class="mono" style="font-weight:600">${esc(s.name)}</td>
                    <td class="mono muted">${esc(JSON.stringify(s.metadata || {}))}</td>
                    <td class="num mono muted">+${fmtMs(s.offset_ms)}</td>
                    <td class="num mono">${fmtMs(s.duration_ms)}</td>
                  </tr>`
                )
                .join("")}
            </tbody>
          </table>
        </div>`;
    }

    html += `
      <div class="card">
        <div class="card-header"><span>Application Logs</span><span class="mono muted">${logs.length}</span></div>
        ${
          logs.length === 0
            ? `<div class="card-body muted">No log records emitted at or above configured log level.</div>`
            : `<table class="data-table">
                <thead><tr><th>Level</th><th>Logger</th><th>Message</th><th>Location</th><th class="num">Offset</th></tr></thead>
                <tbody>
                  ${logs
                    .map((l) => {
                      const lvlCls = l.levelno >= 40 ? "tag-err" : l.levelno >= 30 ? "tag-n1" : "tag-neutral";
                      return `
                        <tr>
                          <td><span class="tag ${lvlCls} mono">${esc(l.level)}</span></td>
                          <td class="mono muted">${esc(l.logger)}</td>
                          <td class="mono">${esc(l.message)}</td>
                          <td class="mono muted">${esc(l.filename)}:${l.lineno}</td>
                          <td class="num mono muted">+${fmtMs(l.offset_ms)}</td>
                        </tr>`;
                    })
                    .join("")}
                </tbody>
              </table>`
        }
      </div>`;

    container.innerHTML = html;
  }

  function renderExceptionTab(container, req) {
    const exc = req.exception;
    if (!exc) return;

    const frames = exc.frames || [];
    if (state.selectedExcFrame < 0 || state.selectedExcFrame >= frames.length) {
      let defaultIdx = frames.length - 1;
      for (let i = frames.length - 1; i >= 0; i--) {
        if (frames[i].is_user_code) {
          defaultIdx = i;
          break;
        }
      }
      state.selectedExcFrame = Math.max(0, defaultIdx);
    }

    const activeFrame = frames[state.selectedExcFrame];

    container.innerHTML = `
      <div class="callout-err">
        <div class="callout-title"><span>${esc(exc.exc_type)}</span><span class="mono muted">fp:${esc(exc.fingerprint)}</span></div>
        <div class="mono" style="font-size:13px;color:var(--text-primary)">${esc(exc.message)}</div>
      </div>
      <div class="exc-layout">
        <div class="frame-list">
          ${frames
            .slice()
            .reverse()
            .map((f, revIdx) => {
              const origIdx = frames.length - 1 - revIdx;
              return `
                <div class="frame-item ${origIdx === state.selectedExcFrame ? "selected" : ""}" data-fidx="${origIdx}">
                  <div class="frame-fn mono">${esc(f.function)} ${f.is_user_code ? `<span class="tag tag-n1">APP</span>` : ""}</div>
                  <div class="frame-file mono">${esc(f.filename)}:${f.lineno}</div>
                </div>`;
            })
            .join("")}
        </div>
        <div>
          ${
            activeFrame
              ? `
              <div class="card">
                <div class="card-header"><span class="mono">${esc(activeFrame.filename)}:${activeFrame.lineno} in ${esc(activeFrame.function)}</span></div>
                <div class="card-body">
                  <div class="source-snippet mono" style="margin-top:0">
                    ${(activeFrame.context_lines || [])
                      .map(
                        ([ln, line]) =>
                          `<div class="src-line ${ln === activeFrame.lineno ? "highlight" : ""}"><span class="src-ln">${ln}</span><span>${esc(line)}</span></div>`
                      )
                      .join("")}
                  </div>
                </div>
              </div>
              <div class="card">
                <div class="card-header"><span>Local Variables (Redacted)</span></div>
                ${
                  Object.keys(activeFrame.locals_preview || {}).length
                    ? `<table class="data-table"><tbody>${Object.entries(activeFrame.locals_preview)
                        .map(
                          ([k, v]) =>
                            `<tr><td class="mono" style="width:180px;font-weight:600">${esc(k)}</td><td class="mono">${v === "[REDACTED]" ? `<span class="tag tag-n1">[REDACTED]</span>` : esc(v)}</td></tr>`
                        )
                        .join("")}</tbody></table>`
                    : `<div class="card-body muted">No local variables captured for this frame.</div>`
                }
              </div>`
              : ""
          }
          <div class="card">
            <div class="card-header"><span>Raw Traceback</span></div>
            <pre class="code-block">${esc(exc.traceback_text)}</pre>
          </div>
        </div>
      </div>
    `;

    $$(".frame-item", container).forEach((el) => {
      el.addEventListener("click", () => {
        state.selectedExcFrame = Number(el.dataset.fidx);
        renderExceptionTab(container, req);
      });
    });
  }

  // ── Secondary Views ────────────────────────────────────────────────────

  async function loadEndpoints() {
    const wrap = $("#endpoints-table-wrap");
    try {
      const data = await apiFetch("/api/endpoints");
      const items = data.items || [];
      if (!items.length) {
        wrap.innerHTML = `<div class="empty-state"><div class="empty-title">No endpoint telemetry yet</div></div>`;
        return;
      }
      wrap.innerHTML = `
        <table class="data-table">
          <thead>
            <tr>
              <th>Method</th>
              <th>Route</th>
              <th class="num">Requests</th>
              <th class="num">Avg</th>
              <th class="num">P50</th>
              <th class="num">P95</th>
              <th class="num">P99</th>
              <th class="num">Avg SQL</th>
              <th class="num">N+1</th>
              <th class="num">Errors</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${items
              .map(
                (ep) => `
                <tr>
                  <td><span class="method-badge mono ${methodClass(ep.method)}">${esc(ep.method)}</span></td>
                  <td class="mono" style="font-weight:600">${esc(ep.route)}</td>
                  <td class="num mono">${ep.count}</td>
                  <td class="num mono">${fmtMs(ep.avg_ms)}</td>
                  <td class="num mono">${fmtMs(ep.p50_ms)}</td>
                  <td class="num mono" style="font-weight:600">${fmtMs(ep.p95_ms)}</td>
                  <td class="num mono muted">${fmtMs(ep.p99_ms)}</td>
                  <td class="num mono">${ep.avg_sql}</td>
                  <td class="num">${ep.n_plus_one_hits > 0 ? `<span class="tag tag-n1 mono">${ep.n_plus_one_hits}</span>` : `<span class="muted">0</span>`}</td>
                  <td class="num">${ep.error_count > 0 ? `<span class="tag tag-err mono">${ep.error_rate}%</span>` : `<span class="muted">0%</span>`}</td>
                  <td class="num"><button class="btn sm jump-req-btn" data-id="${esc(ep.latest_request_id)}">Inspect</button></td>
                </tr>`
              )
              .join("")}
          </tbody>
        </table>`;
      $$(".jump-req-btn", wrap).forEach((btn) =>
        btn.addEventListener("click", () => {
          switchView("requests");
          selectRequest(btn.dataset.id);
        })
      );
    } catch (err) {
      wrap.innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    }
  }

  async function loadSqlExplorer() {
    const wrap = $("#sql-explorer-wrap");
    try {
      const data = await apiFetch("/api/sql?limit=100");
      const items = data.items || [];
      if (!items.length) {
        wrap.innerHTML = `<div class="empty-state"><div class="empty-title">No SQL queries captured yet</div></div>`;
        return;
      }
      wrap.innerHTML = `
        <table class="data-table">
          <thead>
            <tr>
              <th>Op</th>
              <th>Normalized SQL</th>
              <th class="num">Calls</th>
              <th class="num">Total</th>
              <th class="num">Avg</th>
              <th class="num">Max</th>
              <th class="num">N+1</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${items
              .map(
                (row) => `
                <tr>
                  <td><span class="tag tag-neutral mono">${esc(row.operation)}</span></td>
                  <td>
                    <div class="mono" style="max-width:560px;word-break:break-word">${highlightSQL(row.normalized_sql)}</div>
                    ${row.sample_call_site ? `<div class="mono muted" style="font-size:11px;margin-top:2px">at ${esc(row.sample_call_site)}</div>` : ""}
                  </td>
                  <td class="num mono">${row.calls}</td>
                  <td class="num mono" style="font-weight:600">${fmtMs(row.total_ms)}</td>
                  <td class="num mono">${fmtMs(row.avg_ms)}</td>
                  <td class="num mono">${fmtMs(row.max_ms)}</td>
                  <td class="num">${row.n_plus_one_hits > 0 ? `<span class="tag tag-n1 mono">${row.n_plus_one_hits}</span>` : `<span class="muted">0</span>`}</td>
                  <td class="num"><button class="btn sm jump-sql-btn" data-id="${esc(row.sample_request_id)}">Inspect</button></td>
                </tr>`
              )
              .join("")}
          </tbody>
        </table>`;
      $$(".jump-sql-btn", wrap).forEach((btn) =>
        btn.addEventListener("click", () => {
          switchView("requests");
          selectRequest(btn.dataset.id, "sql");
        })
      );
    } catch (err) {
      wrap.innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    }
  }

  async function loadExceptions() {
    const wrap = $("#exceptions-wrap");
    try {
      const data = await apiFetch("/api/exceptions");
      const items = data.items || [];
      if (!items.length) {
        wrap.innerHTML = `<div class="empty-state"><div class="empty-title">Zero exceptions captured</div><div class="empty-sub">Your application has not raised any unhandled exceptions.</div></div>`;
        return;
      }
      wrap.innerHTML = `
        <table class="data-table">
          <thead>
            <tr>
              <th>Exception</th>
              <th>Route</th>
              <th class="num">Count</th>
              <th class="num">Last Seen</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${items
              .map(
                (ex) => `
                <tr>
                  <td>
                    <div class="mono" style="color:var(--red);font-weight:600">${esc(ex.exc_type)}</div>
                    <div class="muted" style="font-size:12px">${esc(ex.message)}</div>
                  </td>
                  <td class="mono"><span class="method-badge ${methodClass(ex.latest_method)}">${esc(ex.latest_method)}</span> ${esc(ex.latest_path)}</td>
                  <td class="num mono"><span class="tag tag-err">${ex.count}</span></td>
                  <td class="num mono muted">${fmtRelative(ex.last_seen)}</td>
                  <td class="num"><button class="btn sm jump-exc-row" data-id="${esc(ex.latest_request_id)}">Traceback</button></td>
                </tr>`
              )
              .join("")}
          </tbody>
        </table>`;
      $$(".jump-exc-row", wrap).forEach((btn) =>
        btn.addEventListener("click", () => {
          switchView("requests");
          selectRequest(btn.dataset.id, "exception");
        })
      );
    } catch (err) {
      wrap.innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    }
  }

  // ── Compare / Diff ─────────────────────────────────────────────────────

  function populateComparePickers() {
    const baseSel = $("#compare-baseline");
    const candSel = $("#compare-candidate");
    const opts = state.requests
      .map(
        (r) =>
          `<option value="${esc(r.id)}">${esc(r.method)} ${esc(r.full_path)} — ${fmtMs(r.duration_ms)} (${r.sql_count} SQL)${r.has_n_plus_one ? " [N+1]" : ""}</option>`
      )
      .join("");
    baseSel.innerHTML = opts || `<option value="">No requests captured</option>`;
    candSel.innerHTML = opts || `<option value="">No requests captured</option>`;

    if (state.compareBaseline) baseSel.value = state.compareBaseline;
    else if (state.requests.length > 1) baseSel.value = state.requests[1].id;

    if (state.compareCandidate) candSel.value = state.compareCandidate;
    else if (state.requests.length > 0) candSel.value = state.requests[0].id;
  }

  async function runComparison() {
    const baseId = $("#compare-baseline").value || state.compareBaseline;
    const candId = $("#compare-candidate").value || state.compareCandidate;
    const out = $("#compare-result");
    if (!baseId || !candId) {
      out.innerHTML = `<div class="empty-state">Select two requests to compare.</div>`;
      return;
    }
    try {
      const diff = await apiFetch(
        `/api/compare?baseline=${encodeURIComponent(baseId)}&candidate=${encodeURIComponent(candId)}`
      );
      const d = diff.deltas;
      const durClass = d.duration_ms <= 0 ? "delta-good" : "delta-bad";
      const sqlClass = d.sql_count <= 0 ? "delta-good" : "delta-bad";

      out.innerHTML = `
        <div class="delta-grid">
          <div class="delta-card">
            <div class="metric-label">Latency Delta</div>
            <div class="delta-val mono ${durClass}">${fmtSigned(d.duration_ms, 2)}ms (${fmtSigned(d.duration_pct, 1)}%)</div>
            <div class="mono muted">${fmtMs(diff.baseline.duration_ms)} → ${fmtMs(diff.candidate.duration_ms)}</div>
          </div>
          <div class="delta-card">
            <div class="metric-label">SQL Query Count</div>
            <div class="delta-val mono ${sqlClass}">${fmtSignedInt(d.sql_count)} queries</div>
            <div class="mono muted">${diff.baseline.sql_count} → ${diff.candidate.sql_count}</div>
          </div>
          <div class="delta-card">
            <div class="metric-label">SQL Time Delta</div>
            <div class="delta-val mono ${d.sql_duration_ms <= 0 ? "delta-good" : "delta-bad"}">${fmtSigned(d.sql_duration_ms, 2)}ms</div>
            <div class="mono muted">${fmtMs(diff.baseline.sql_duration_ms)} → ${fmtMs(diff.candidate.sql_duration_ms)}</div>
          </div>
          <div class="delta-card">
            <div class="metric-label">N+1 Status</div>
            <div class="delta-val">
              ${
                d.n_plus_one_resolved
                  ? `<span class="tag tag-ok">N+1 Resolved</span>`
                  : d.n_plus_one_introduced
                    ? `<span class="tag tag-err">N+1 Introduced</span>`
                    : `<span class="tag tag-neutral">Unchanged</span>`
              }
            </div>
            <div class="mono muted">${diff.baseline.n_plus_one_count} → ${diff.candidate.n_plus_one_count} groups</div>
          </div>
        </div>
        <div class="table-card">
          <table class="data-table">
            <thead>
              <tr>
                <th>Change</th>
                <th>Normalized SQL Fingerprint</th>
                <th class="num">Before</th>
                <th class="num">After</th>
                <th class="num">Delta</th>
              </tr>
            </thead>
            <tbody>
              ${(diff.query_diffs || [])
                .map((q) => {
                  const stTag =
                    q.status === "removed"
                      ? `<span class="tag tag-ok">REMOVED</span>`
                      : q.status === "added"
                        ? `<span class="tag tag-n1">ADDED</span>`
                        : q.status === "changed"
                          ? `<span class="tag tag-replay">CHANGED</span>`
                          : `<span class="tag tag-neutral">SAME</span>`;
                  return `
                    <tr>
                      <td>${stTag}</td>
                      <td class="mono" style="max-width:560px;word-break:break-word">${highlightSQL(q.normalized_sql)}</td>
                      <td class="num mono">${q.baseline_count}× (${fmtMs(q.baseline_ms)})</td>
                      <td class="num mono">${q.candidate_count}× (${fmtMs(q.candidate_ms)})</td>
                      <td class="num mono ${q.delta_count <= 0 ? "delta-good" : "delta-bad"}">${fmtSignedInt(q.delta_count)}</td>
                    </tr>`;
                })
                .join("")}
            </tbody>
          </table>
        </div>`;
    } catch (err) {
      out.innerHTML = `<div class="callout-err">${esc(err.message)}</div>`;
    }
  }

  // ── Replay Modal & cURL Export ─────────────────────────────────────────

  let replayOriginalId = null;

  function openReplayModal(req) {
    replayOriginalId = req.id;
    $("#replay-method").value = req.method;
    $("#replay-path").value = req.full_path;
    const editableHeaders = {};
    for (const [k, v] of Object.entries(req.request_headers || {})) {
      if (["host", "content-length", "connection", "accept-encoding"].includes(k.toLowerCase())) continue;
      if (v === "[REDACTED]") continue;
      editableHeaders[k] = v;
    }
    $("#replay-headers").value = JSON.stringify(editableHeaders, null, 2);
    $("#replay-body").value = req.request_body || "";
    $("#replay-feedback").classList.add("hidden");
    $("#replay-modal").classList.remove("hidden");
  }

  function closeReplayModal() {
    $("#replay-modal").classList.add("hidden");
  }

  async function submitReplay() {
    const feedback = $("#replay-feedback");
    const sendBtn = $("#replay-send");
    let headersObj = {};
    try {
      const rawH = $("#replay-headers").value.trim();
      if (rawH) headersObj = JSON.parse(rawH);
    } catch {
      feedback.className = "replay-feedback error";
      feedback.textContent = "Headers must be valid JSON.";
      feedback.classList.remove("hidden");
      return;
    }

    sendBtn.disabled = true;
    sendBtn.textContent = "Sending…";
    try {
      const res = await apiFetch("/api/replay", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          method: $("#replay-method").value,
          path: $("#replay-path").value,
          headers: headersObj,
          body: $("#replay-body").value,
          original_request_id: replayOriginalId,
        }),
      });
      await refreshAll();
      feedback.className = "replay-feedback";
      feedback.innerHTML = `
        <span>Replayed → HTTP <b>${res.status_code}</b> ${res.request ? `(${fmtMs(res.request.duration_ms)}, ${res.request.sql_count} SQL)` : ""}</span>
        <div class="req-tags">
          ${res.request_id ? `<button class="btn sm" id="replay-open-new">Inspect</button>` : ""}
          ${res.request_id && replayOriginalId ? `<button class="btn sm primary" id="replay-open-diff">Diff vs Original</button>` : ""}
        </div>`;
      feedback.classList.remove("hidden");
      const openBtn = $("#replay-open-new", feedback);
      if (openBtn) {
        openBtn.addEventListener("click", () => {
          closeReplayModal();
          selectRequest(res.request_id);
        });
      }
      const diffBtn = $("#replay-open-diff", feedback);
      if (diffBtn) {
        diffBtn.addEventListener("click", () => {
          closeReplayModal();
          state.compareBaseline = replayOriginalId;
          state.compareCandidate = res.request_id;
          switchView("compare");
          populateComparePickers();
          runComparison();
        });
      }
    } catch (err) {
      feedback.className = "replay-feedback error";
      feedback.textContent = err.message;
      feedback.classList.remove("hidden");
    } finally {
      sendBtn.disabled = false;
      sendBtn.textContent = "Send Request";
    }
  }

  function copyAsCurl(req) {
    const parts = [`curl -X ${req.method} '${window.location.origin}${req.full_path}'`];
    for (const [k, v] of Object.entries(req.request_headers || {})) {
      if (["host", "content-length", "accept-encoding", "connection"].includes(k.toLowerCase())) continue;
      parts.push(`  -H '${k}: ${String(v).replace(/'/g, "'\\''")}'`);
    }
    if (req.request_body) {
      parts.push(`  --data-raw '${req.request_body.replace(/'/g, "'\\''")}'`);
    }
    navigator.clipboard?.writeText(parts.join(" \\\n")).then(
      () => showToast("Copied cURL command to clipboard"),
      () => showToast("Copy failed — clipboard permission required")
    );
  }

  // ── Command Palette (Cmd/Ctrl+K) ───────────────────────────────────────

  let cmdkItems = [];
  let cmdkActiveIdx = 0;

  function openCmdK() {
    $("#cmdk-modal").classList.remove("hidden");
    const input = $("#cmdk-input");
    input.value = "";
    cmdkActiveIdx = 0;
    renderCmdKList("");
    input.focus();
  }

  function closeCmdK() {
    $("#cmdk-modal").classList.add("hidden");
  }

  function renderCmdKList(query) {
    const q = query.trim().toLowerCase();
    const actions = [
      { label: "View: Requests", hint: "Navigation", run: () => switchView("requests") },
      { label: "View: Endpoint Analytics", hint: "Navigation", run: () => switchView("endpoints") },
      { label: "View: SQL Fingerprint Explorer", hint: "Navigation", run: () => switchView("sql") },
      { label: "View: Exceptions", hint: "Navigation", run: () => switchView("exceptions") },
      { label: "View: Compare / Diff", hint: "Navigation", run: () => switchView("compare") },
      { label: "Toggle Dark / Light Theme", hint: "Action", run: toggleTheme },
      { label: state.live ? "Pause Live Stream" : "Resume Live Stream", hint: "Action", run: toggleLiveStream },
      { label: "Clear All Captured Telemetry", hint: "Action", run: clearTelemetry },
    ];

    const reqMatches = state.requests.slice(0, 25).map((r) => ({
      label: `${r.method} ${r.full_path}`,
      hint: `${r.status_code} · ${fmtMs(r.duration_ms)} · ${r.sql_count} SQL`,
      run: () => {
        switchView("requests");
        selectRequest(r.id);
      },
    }));

    cmdkItems = [...actions, ...reqMatches]
      .filter((it) => !q || it.label.toLowerCase().includes(q) || it.hint.toLowerCase().includes(q))
      .slice(0, 14);

    if (cmdkActiveIdx >= cmdkItems.length) cmdkActiveIdx = 0;

    const list = $("#cmdk-list");
    if (!cmdkItems.length) {
      list.innerHTML = `<div class="empty-state" style="padding:18px">No matching commands or requests</div>`;
      return;
    }

    list.innerHTML = cmdkItems
      .map(
        (it, idx) => `
        <div class="cmdk-item ${idx === cmdkActiveIdx ? "active" : ""}" data-idx="${idx}">
          <span class="mono" style="font-size:12.5px">${esc(it.label)}</span>
          <span class="muted mono" style="font-size:11px">${esc(it.hint)}</span>
        </div>`
      )
      .join("");

    $$(".cmdk-item", list).forEach((el) => {
      el.addEventListener("click", () => {
        cmdkItems[Number(el.dataset.idx)].run();
        closeCmdK();
      });
    });
  }

  function updateCmdKSelection(delta) {
    if (!cmdkItems.length) return;
    cmdkActiveIdx = (cmdkActiveIdx + delta + cmdkItems.length) % cmdkItems.length;
    $$(".cmdk-item", $("#cmdk-list")).forEach((el, idx) => {
      el.classList.toggle("active", idx === cmdkActiveIdx);
      if (idx === cmdkActiveIdx) el.scrollIntoView({ block: "nearest" });
    });
  }

  // ── SSE Live Stream ────────────────────────────────────────────────────

  function startSSE() {
    stopSSE();
    if (!state.live || typeof EventSource === "undefined") return;
    const es = new EventSource(`${BASE}/api/stream`);
    es.addEventListener("request", (ev) => {
      try {
        const summary = JSON.parse(ev.data);
        if (matchesCurrentFilters(summary)) {
          const hadNone = !state.selectedId && state.requests.length === 0;
          state.requests = [summary, ...state.requests.filter((r) => r.id !== summary.id)].slice(0, 150);
          renderRequestList();
          if (hadNone) selectRequest(summary.id);
        }
        loadOverview();
      } catch (err) {
        console.error("SSE parse error:", err);
      }
    });
    state.sse = es;
  }

  function stopSSE() {
    if (state.sse) {
      state.sse.close();
      state.sse = null;
    }
  }

  function toggleLiveStream() {
    state.live = !state.live;
    const btn = $("#live-toggle");
    btn.classList.toggle("active", state.live);
    $("#live-label").textContent = state.live ? "Live" : "Paused";
    if (state.live) {
      startSSE();
      refreshAll();
    } else {
      stopSSE();
    }
  }

  function matchesCurrentFilters(r) {
    const f = state.filters;
    if (f.method && r.method !== f.method) return false;
    if (f.status === "2xx" && !(r.status_code >= 200 && r.status_code < 300)) return false;
    if (f.status === "3xx" && !(r.status_code >= 300 && r.status_code < 400)) return false;
    if (f.status === "4xx" && !(r.status_code >= 400 && r.status_code < 500)) return false;
    if (f.status === "5xx" && r.status_code < 500) return false;
    if (f.n1 && !r.has_n_plus_one) return false;
    if (f.slow && !(r.has_slow_sql || r.duration_ms >= 200)) return false;
    if (f.errors && !(r.status_code >= 400 || r.has_exception)) return false;
    if (f.q && !`${r.full_path} ${r.endpoint || ""}`.toLowerCase().includes(f.q.toLowerCase())) return false;
    return true;
  }

  // ── Navigation & Actions ───────────────────────────────────────────────

  function switchView(viewName, syncUrl = true) {
    state.view = viewName;
    $$(".nav-tab").forEach((t) => t.classList.toggle("active", t.dataset.view === viewName));
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${viewName}`));
    if (syncUrl && window.history?.replaceState) {
      const targetUrl =
        viewName === "requests"
          ? state.selectedId
            ? `${BASE}/requests/${encodeURIComponent(state.selectedId)}`
            : `${BASE}/`
          : `${BASE}/${viewName}`;
      window.history.replaceState(null, "", targetUrl);
    }
    if (viewName === "endpoints") loadEndpoints();
    if (viewName === "sql") loadSqlExplorer();
    if (viewName === "exceptions") loadExceptions();
    if (viewName === "compare") populateComparePickers();
  }

  async function clearTelemetry() {
    try {
      await apiFetch("/api/clear", { method: "POST" });
      state.requests = [];
      state.selectedId = null;
      state.selectedDetail = null;
      state.explainCache = {};
      renderRequestList();
      renderDetailPane();
      await refreshAll();
      showToast("Telemetry cleared");
    } catch (err) {
      showToast(`Clear failed: ${err.message}`);
    }
  }

  // ── Event Wiring ───────────────────────────────────────────────────────

  function bindEvents() {
    $$(".nav-tab").forEach((tab) => {
      tab.addEventListener("click", () => switchView(tab.dataset.view));
    });

    $("#brand-home").addEventListener("click", (e) => {
      e.preventDefault();
      switchView("requests");
    });

    $("#live-toggle").addEventListener("click", toggleLiveStream);
    $("#theme-btn").addEventListener("click", toggleTheme);
    $("#refresh-btn").addEventListener("click", refreshAll);
    $("#clear-btn").addEventListener("click", clearTelemetry);
    $("#cmdk-btn").addEventListener("click", openCmdK);

    $("#kpi-n1-btn").addEventListener("click", () => {
      switchView("requests");
      state.filters.n1 = !state.filters.n1;
      $("#chip-n1").classList.toggle("active", state.filters.n1);
      loadRequests();
    });

    $("#kpi-err-btn").addEventListener("click", () => {
      switchView("requests");
      state.filters.errors = !state.filters.errors;
      $("#chip-errors").classList.toggle("active", state.filters.errors);
      loadRequests();
    });

    let searchTimer = null;
    $("#filter-search").addEventListener("input", (e) => {
      state.filters.q = e.target.value;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(loadRequests, 160);
    });
    $("#filter-method").addEventListener("change", (e) => {
      state.filters.method = e.target.value;
      loadRequests();
    });
    $("#filter-status").addEventListener("change", (e) => {
      state.filters.status = e.target.value;
      loadRequests();
    });

    $$(".chip-toggle").forEach((chip) => {
      chip.addEventListener("click", () => {
        const key = chip.dataset.filter;
        state.filters[key] = !state.filters[key];
        chip.classList.toggle("active", state.filters[key]);
        loadRequests();
      });
    });

    $("#request-list").addEventListener("click", (e) => {
      const row = e.target.closest(".req-row");
      if (row) selectRequest(row.dataset.id);
    });

    $("#compare-run-btn").addEventListener("click", runComparison);

    // Replay modal
    $("#replay-close").addEventListener("click", closeReplayModal);
    $("#replay-cancel").addEventListener("click", closeReplayModal);
    $("#replay-send").addEventListener("click", submitReplay);
    $("#replay-modal").addEventListener("click", (e) => {
      if (e.target.id === "replay-modal") closeReplayModal();
    });

    // Command palette
    $("#cmdk-input").addEventListener("input", (e) => {
      cmdkActiveIdx = 0;
      renderCmdKList(e.target.value);
    });
    $("#cmdk-input").addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        updateCmdKSelection(1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        updateCmdKSelection(-1);
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (cmdkItems[cmdkActiveIdx]) {
          cmdkItems[cmdkActiveIdx].run();
          closeCmdK();
        }
      }
    });
    $("#cmdk-modal").addEventListener("click", (e) => {
      if (e.target.id === "cmdk-modal") closeCmdK();
    });

    // Keyboard shortcuts
    document.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        openCmdK();
        return;
      }
      if (e.key === "Escape") {
        closeCmdK();
        closeReplayModal();
        return;
      }
      if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) {
        return;
      }
      if (state.view === "requests" && (e.key === "j" || e.key === "ArrowDown" || e.key === "k" || e.key === "ArrowUp")) {
        if (!state.requests.length) return;
        e.preventDefault();
        const curIdx = state.requests.findIndex((r) => r.id === state.selectedId);
        const delta = e.key === "j" || e.key === "ArrowDown" ? 1 : -1;
        const nextIdx = Math.max(0, Math.min(state.requests.length - 1, curIdx + delta));
        selectRequest(state.requests[nextIdx].id);
      } else if (e.key === "r" && !e.metaKey && !e.ctrlKey) {
        refreshAll();
      }
    });
  }

  // ── Bootstrap ──────────────────────────────────────────────────────────

  function applyInitialRoute() {
    const path = window.location.pathname || "";
    const rel = path.startsWith(BASE) ? path.slice(BASE.length).replace(/^\/+|\/+$/g, "") : "";
    if (["endpoints", "sql", "exceptions", "compare"].includes(rel)) {
      switchView(rel, false);
    } else if (rel.startsWith("requests/")) {
      const reqId = decodeURIComponent(rel.slice("requests/".length));
      if (reqId) {
        state.selectedId = reqId;
        selectRequest(reqId);
      }
    }
  }

  initTheme();
  bindEvents();
  applyInitialRoute();
  refreshAll();
  startSSE();

  setInterval(() => {
    $$("[data-ts]").forEach((el) => {
      el.textContent = ` · ${fmtRelative(el.dataset.ts)}`;
    });
  }, 15000);
})();
