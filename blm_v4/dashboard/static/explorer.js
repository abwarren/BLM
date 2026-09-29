/* ═══════════════════════════════════════════════════════════════
   BLM COMMAND CENTER — DATABASE / DATA EXPLORER (read-only).

   Investigation surface over the EXISTING read-only API endpoints:
     games        → GET /api/v4/games            (recent games + quality partition)
     checkpoints  → GET /api/v4/scorecard/events (market-vs-fair checkpoint rows)
     executions   → GET /api/v4/betting/history  (execution ledger + audit)
     quarters     → GET /api/v4/collection/quarters (quarter-collection observability)
     integrity    → GET /api/v4/results/integrity   (result reconciliation audit)

   READ-ONLY: nothing here mutates data, settlements, alerts or the
   betting ledger.  Every value rendered comes verbatim from the API
   response — the explorer never invents a row, state or id, and a
   field the API does not serve is simply absent.
   ═══════════════════════════════════════════════════════════════ */
"use strict";

(() => {
  const POLL_MS = 30000;
  const EX_API = {
    games: "/api/v4/games",
    checkpoints: "/api/v4/scorecard/events",
    executions: "/api/v4/betting/history",
    quarters: "/api/v4/collection/quarters",
    integrity: "/api/v4/results/integrity",
  };
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const S = {
    dataset: "games",
    timer: null,
    inflight: false,
    lastRows: null,
  };

  /* ── dataset → display rows (transparent projections only) ── */
  function rowsFor(dataset, body) {
    if (dataset === "games") {
      return (body.games || []).map((g) => ({
        game_id: g.game_id, classification: g.classification,
        competition: g.competition, home_team: g.home_team,
        away_team: g.away_team, status: g.status,
        score: (g.home_score != null || g.away_score != null)
          ? `${g.home_score ?? "–"}–${g.away_score ?? "–"}` : null,
        data_quality: g.data_quality,
        snapshot_count: g.snapshot_count,
        first_seen_at: g.first_seen_at, last_seen_at: g.last_seen_at,
      }));
    }
    if (dataset === "checkpoints") {
      return (body.rows || []).map((r) => ({
        game_id: r.source_game_id, game: r.game,
        checkpoint_pct: r.checkpoint_pct,
        market_line: r.market_line ?? r.live_market_line,
        blm_fair_value: r.blm_fair_value,
        diff: r.diff,
        direction: r.direction,
        market_status: r.market_status,
        settlement: r.settlement ?? r.outcome,
        terminal: r.terminal,
        captured_at: r.captured_at ?? r.checkpoint_timestamp,
      }));
    }
    if (dataset === "executions") {
      return (body.executions || []).map((r) => ({
        execution_id: r.execution_id, game_id: r.game_id,
        alert_id: r.alert_id, checkpoint: r.checkpoint,
        market: r.market, selection: r.selection,
        line: r.line, stake_amount: r.stake_amount,
        status: r.status, execution_state: r.execution_state,
        provider_ref: r.provider_ref,
        error_code: r.error_code, error_message: r.error_message,
        requested_at_utc: r.requested_at_utc,
        resolved_at_utc: r.resolved_at_utc,
        elapsed_ms: r.elapsed_ms,
      }));
    }
    return null;
  }

  function renderTable(dataset, body) {
    const table = $("exTable");
    if (!table) return;
    const cols = {
      games: ["game_id", "classification", "competition", "home_team",
        "away_team", "score", "status", "data_quality",
        "snapshot_count", "last_seen_at"],
      checkpoints: ["game_id", "game", "checkpoint_pct", "market_line",
        "blm_fair_value", "diff", "direction", "market_status",
        "settlement", "terminal", "captured_at"],
      executions: ["execution_id", "game_id", "alert_id", "checkpoint",
        "selection", "line", "stake_amount", "status",
        "execution_state", "provider_ref", "error_message",
        "requested_at_utc", "elapsed_ms"],
    }[dataset];
    if (!cols) {
      // key/value view for the single-object datasets
      const rows = [];
      const flat = (obj, prefix) => {
        for (const [k, v] of Object.entries(obj || {})) {
          const key = prefix ? `${prefix}.${k}` : k;
          if (v && typeof v === "object" && !Array.isArray(v)) flat(v, key);
          else rows.push({ key, value: Array.isArray(v) ? JSON.stringify(v) : v });
        }
      };
      flat(body, "");
      table.innerHTML = "<thead><tr><th>FIELD</th><th>VALUE</th></tr></thead><tbody>"
        + rows.map((r) => `<tr><td>${esc(r.key)}</td><td>${esc(r.value)}</td></tr>`).join("")
        + "</tbody>";
      return;
    }
    const needle = ($("exQuery").value || "").trim().toLowerCase();
    let rows = rowsFor(dataset, body) || [];
    if (needle) {
      rows = rows.filter((r) => cols.some((c) =>
        r[c] != null && String(r[c]).toLowerCase().includes(needle)));
    }
    const th = cols.map((c) =>
      `<th>${esc(c.replace(/_/g, " ").toUpperCase())}</th>`).join("");
    const td = (r, c) => {
      const v = r[c];
      const cls = c === "data_quality" && v === "LEGACY" ? ' class="muted"'
        : c === "status" || c === "execution_state" ? ` class="ex-st ex-st-${esc(String(v || "").toLowerCase())}"`
        : "";
      return `<td${cls}>${v == null ? "–" : esc(v)}</td>`;
    };
    table.innerHTML = `<thead><tr>${th}</tr></thead><tbody>`
      + (rows.length
        ? rows.map((r) => `<tr>${cols.map((c) => td(r, c)).join("")}</tr>`).join("")
        : `<tr><td colspan="${cols.length}">No rows match</td></tr>`)
      + `</tbody>`;
    const meta = $("exMeta");
    if (meta) {
      meta.textContent = needle
        ? `${rows.length} of ${(rowsFor(dataset, body) || []).length} rows match "${needle}"`
        : `${rows.length} rows`;
    }
  }

  function renderFlat(dataset, body) {
    const table = $("exTable");
    if (!table) return;
    const rows = [];
    const flat = (obj, prefix) => {
      for (const [k, v] of Object.entries(obj || {})) {
        const key = prefix ? `${prefix}.${k}` : k;
        if (v != null && typeof v === "object" && !Array.isArray(v)) {
          if (key === "conflicts" || key === "missing_results" || key === "pending" || key === "by_period" || key === "sample_markets") {
            rows.push({ key, value: `${v.length} rows (see API for full list)` });
          } else flat(v, key);
        } else if (Array.isArray(v)) {
          rows.push({ key, value: `[${v.length} items]` });
        } else {
          rows.push({ key, value: v });
        }
      }
    };
    flat(body, "");
    table.innerHTML = "<thead><tr><th>FIELD</th><th>VALUE</th></tr></thead><tbody>"
      + rows.map((r) => `<tr><td>${esc(r.key)}</td><td>${esc(r.value)}</td></tr>`).join("")
      + "</tbody>";
  }

  async function pull() {
    if (S.inflight) return;
    const dataset = S.dataset;
    S.inflight = true;
    try {
      const limit = Math.max(10, Math.min(1000, Number($("exLimit").value) || 200));
      const params = new URLSearchParams();
      if (dataset === "games") params.set("limit", String(limit));
      if (dataset === "checkpoints") params.set("limit", String(limit));
      if (dataset === "executions") params.set("limit", String(limit));
      const resp = await fetch(`${EX_API[dataset]}?${params.toString()}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const body = await resp.json();
      if (S.dataset !== dataset) return;           // switched mid-fetch
      if (dataset === "games" || dataset === "checkpoints" || dataset === "executions") {
        renderTable(dataset, body);
      } else {
        renderFlat(dataset, body);
      }
      const meta = $("exMeta");
      if (meta && dataset !== "games" && dataset !== "checkpoints" && dataset !== "executions") {
        meta.textContent = `${dataset} payload`;
      }
      const note = $("exNote");
      if (note) {
        note.textContent = {
          games: "GET /api/v4/games — recent games with the CLEAN/LEGACY data-quality partition.",
          checkpoints: "GET /api/v4/scorecard/events — one row per (game, checkpoint): observed line, reference value, signed diff, settlement.",
          executions: "GET /api/v4/betting/history — the execution ledger. status describes the provider response; execution_state is the ledger state.",
          quarters: "GET /api/v4/collection/quarters — quarter-specific collection observability (coverage, parse failures, anomalies).",
          integrity: "GET /api/v4/results/integrity — result reconciliation audit. A missing result is an outstanding job, never a completed outcome.",
        }[dataset] || "";
      }
      const asOf = $("exAsOf");
      if (asOf) asOf.textContent = new Date().toISOString().slice(11, 19) + "Z";
      S.lastRows = body;
    } catch (err) {
      const table = $("exTable");
      if (table) {
        table.innerHTML = `<thead><tr><th>ERROR</th></tr></thead><tbody>`
          + `<tr><td>${esc(String(err.message || err))}</td></tr></tbody>`;
      }
    } finally {
      S.inflight = false;
    }
  }

  function setDataset(ds) {
    S.dataset = ds;
    $("exQuery").value = "";
    pull();
  }

  function startPolling() {
    if (S.timer) return;
    S.timer = setInterval(pull, POLL_MS);
  }
  function stopPolling() {
    if (S.timer) { clearInterval(S.timer); S.timer = null; }
  }

  /* ── view switching (shared with stats) ───────────────────── */
  // The canonical switcher lives in stats.js; it hides only viewLive and
  // viewStats.  This re-export keeps the DB EXPLORER tab working through
  // the same click path without changing stats.js behavior.
  function show(view) {
    const live = $("viewLive"), stats = $("viewStats"), explorer = $("viewExplorer");
    const tabLive = $("tabLive"), tabStats = $("tabStats"), tabExplorer = $("tabExplorer");
    const note = $("tabNote");
    if (!live || !stats || !explorer) return;
    const onExplorer = view === "explorer";
    const onStats = view === "stats";
    live.hidden = onStats || onExplorer;
    stats.hidden = !onStats;
    explorer.hidden = !onExplorer;
    for (const [tab, active] of [[tabLive, !onStats && !onExplorer],
                                 [tabStats, onStats], [tabExplorer, onExplorer]]) {
      if (!tab) continue;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    }
    if (note) {
      note.textContent = onExplorer ? "read-only data / database explorer"
        : onStats ? "fired-alert cohort · fingerprints · league standings"
        : "live games + alerts";
    }
    if (onExplorer) { pull(); startPolling(); }
    else stopPolling();
  }

  function init() {
    const tabs = $("tabs");
    if (tabs) {
      tabs.addEventListener("click", (ev) => {
        const btn = ev.target.closest(".tab");
        if (btn && btn.dataset.view) show(btn.dataset.view);
      });
    }
    $("exDataset").addEventListener("change", (ev) => setDataset(ev.target.value));
    $("exReload").addEventListener("click", pull);
    let t = null;
    $("exQuery").addEventListener("input", () => {
      if (t) clearTimeout(t);
      t = setTimeout(() => {
        if (S.lastRows
          && (S.dataset === "games" || S.dataset === "checkpoints" || S.dataset === "executions")) {
          renderTable(S.dataset, S.lastRows);      // client-side substring over loaded rows
        } else pull();
      }, 150);
    });
    show("live");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.__explorerTab = { pull, show, setDataset, state: S };
})();
