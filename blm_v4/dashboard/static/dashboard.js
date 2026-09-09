/* ═══════════════════════════════════════════════════════════════
   BLM LIVE ANALYTICS — operator dashboard
   Polls /api/v4/live every 5s; renders game cards + detail charts.
   No manual refresh required.

   SCOPE: LIVE + CLEAN + DESCRIPTIVE + COMPACT.  This is an
   observation surface for clean post-epoch data ONLY.  Cards and the
   detail modal present measured state — score/clock/elapsed, the live
   total line with freshness, and pace as Pts/Min measurements (current,
   required, gap, recent, acceleration).   Charts plot observed
   time-series only.  There are NO model totals, no fair values, no
   projections, no probabilities, no edges, no confidence, no signals
   and no recommendations anywhere in the live view.  The expanded game
   view shows exactly ONE primary chart — SCORE vs LIVE LINE — MARKET
   MOVEMENT — whose Z-score is consumed from the authoritative stored
   deviation data (never computed in the frontend).  Model/audit
   diagnostics live exclusively in the collapsed HISTORICAL / AUDIT
   sections at the bottom, behind a top-level HISTORICAL / RESEARCH
   collapse that is closed by default and persisted to localStorage;
   raw JSON is only available via the API or "Technical / Raw Data" in
   the modal DETAILS.
   ═══════════════════════════════════════════════════════════════ */
"use strict";

const POLL_MS = 5000;
const API_LIVE = "/api/v4/live";
const API_GAME = (id) => `/api/v4/game/${encodeURIComponent(id)}`;
// READ-ONLY additive exposure: the observed live-line history (every
// valid observed line at its exact observation time — the same
// market_observations rows the collector writes; no fabrication).
const API_GAME_LINES = (id) => `/api/v4/game/${encodeURIComponent(id)}/market-lines`;

const state = {
  filter: "",
  games: [],
  cards: new Map(),        // game_id -> {el, spark, detOpen, chartsOpen}
  modalGameId: null,
  modalDetailOk: null,     // detail-endpoint liveness for the open modal (null = pending)
  modalCharts: {},
  modalZText: null,        // cached PACE Z readout across header re-renders
  modalGapText: null,      // cached SCORE − LINE readout across header re-renders
  hideNonLive: true,       // default view: LIVE games only
};

/* ── UI section preferences — persisted in localStorage ─────
   Frontend presentation state only (no backend involvement).
     blm.historicalResearchCollapsed  — top-level HISTORICAL / RESEARCH
     blm.gameDetailsCollapsed         — DETAILS in game cards / detail modal
     blm.chartsCollapsed              — CHARTS (cards' sparkline / modal)
   Defaults: historical + game details collapsed; charts visible.
   "1" = collapsed, "0" = expanded.  New users get the defaults. */
const PREF = {
  HISTORICAL: "blm.historicalResearchCollapsed",
  GAME_DETAILS: "blm.gameDetailsCollapsed",
  CHARTS: "blm.chartsCollapsed",
  CHECKPOINTS: "blm.checkpointsCollapsed",
  MARKET_TRAJ: "blm.marketTrajectoryCollapsed",
  DEVIATION_Z: "blm.deviationCollapsed",
  VALIDATION: "blm.validationCollapsed",
};
const prefGet = (key, dflt) => {
  try {
    const v = localStorage.getItem(key);
    return v == null ? dflt : v === "1";
  } catch (_) { return dflt; }
};
const prefSet = (key, collapsed) => {
  try { localStorage.setItem(key, collapsed ? "1" : "0"); } catch (_) {}
};

// keep a collapsible section's ▸/▾ arrow in sync with its state
function setSectionLabel(det) {
  if (!det) return;
  const sum = det.querySelector(":scope > summary");
  const lab = sum && sum.dataset.label;
  if (sum && lab) sum.textContent = `${lab} ${det.open ? "▾" : "▸"}`;
}
// bind a collapsible <details>; syncs its label and, when a pref key is
// given, persists the collapsed preference on user toggles.
function bindCollapsible(det, prefKey) {
  if (!det) return;
  setSectionLabel(det);
  det.addEventListener("toggle", () => {
    setSectionLabel(det);
    if (prefKey) prefSet(prefKey, !det.open);
  });
}

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (iso) => {
  if (!iso) return "--";
  const d = new Date(iso);
  return d.toLocaleTimeString("en-GB", { hour12: false });
};
const fmtAge = (s) => {
  if (s == null) return "--";
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
};
// exact age for market freshness (M009-M5 integrity): "18s", "4m 21s",
// "1h 05m" — never fabricated; null renders the en dash.
const fmtAgeExact = (s) => {
  if (s == null) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${Math.round(s % 60)}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
};
// market freshness status at the 300s M3 threshold — LIVE/STALE/MISSING,
// exactly the backend classification; never re-derived differently.
const mktStatusWord = (age, hasLine) => {
  if (!hasLine) return "MISSING";
  if (age == null) return null;
  return age <= 300 ? "LIVE" : "STALE";
};
const num = (v, d = 1) => (v == null ? "–" : Number(v).toFixed(d));
const pct = (v, d = 0) => (v == null ? "–" : `${(v * 100).toFixed(d)}%`);
const sig = (x) => x == null ? "–" : (x > 0 ? "+" : "") + x;
const hasChart = () => typeof Chart !== "undefined";
const ChartColor = {
  home: "rgba(34,211,238,1)", away: "rgba(251,191,36,1)",
  market: "rgba(251,191,36,1)", momentum: "rgba(52,211,153,1)",
  grid: "rgba(30,42,58,.6)", tick: "#6b7a90",
};

/* ── Header / status ─────────────────────────────────────── */

function renderStatus(payload) {
  const now = Date.now();
  const gen = payload.generated_at ? new Date(payload.generated_at).getTime() : 0;
  const fresh = now - gen < 15000;
  const livePill = $("livePill"), dot = $("liveDot");
  if (fresh) {
    livePill.className = "pill live-pill ok";
    $("liveLabel").textContent = "LIVE";
  } else {
    livePill.className = "pill live-pill bad";
    $("liveLabel").textContent = "STALE";
  }
  const col = payload.collector;
  const cpill = $("collectorPill");
  if (col) {
    const lastTick = col.last_tick_at ? new Date(col.last_tick_at).getTime() : 0;
    const tickAge = (now - lastTick) / 1000;
    let label;
    if (tickAge < 90) label = col.status === "running" ? "collector: RUNNING" : "collector: STALLED";
    else label = "collector: OFFLINE";
    cpill.textContent = label;
    cpill.style.color = label === "collector: RUNNING" ? "var(--green)" :
      label === "collector: STALLED" ? "var(--orange)" : "var(--red)";
  } else {
    cpill.textContent = "collector: --";
    cpill.style.color = "";
  }
  $("lastUpdatePill").textContent = `update ${fmtTime(payload.generated_at)}`;
  const totals = payload.totals || {};
  $("gamesMonitoredPill").textContent =
    `${totals.live || 0} live / ${totals.total || 0} games`;
}

function renderSummary(payload) {
  const games = payload.games || [];
  const per = { CYBER_2K26: { n: 0, live: 0 }, BETUAL_NBA: { n: 0, live: 0 } };
  let snaps = 0, live = 0;
  for (const g of games) {
    if (per[g.classification]) {
      per[g.classification].n++;
      if (g.live) per[g.classification].live++;
    }
    if (g.live) live++;
    snaps += g.snapshot_count || 0;
  }
  $("sumCyber").textContent = per.CYBER_2K26.n || 0;
  $("sumCyber").className = "sum-value cyber";
  $("sumCyberSub").textContent = `${per.CYBER_2K26.live} live`;
  $("sumBetual").textContent = per.BETUAL_NBA.n || 0;
  $("sumBetual").className = "sum-value betual";
  $("sumBetualSub").textContent = `${per.BETUAL_NBA.live} live`;
  $("sumLive").textContent = live;
  $("sumSnaps").textContent = snaps;
  let newest = null;
  for (const g of games) {
    const t = g.last_update ? new Date(g.last_update).getTime() : 0;
    if (t > (newest || 0)) newest = t;
  }
  $("sumAge").textContent = newest ? fmtAge((Date.now() - newest) / 1000) : "--";
  $("sumAgeSub").textContent = newest ? fmtTime(new Date(newest).toISOString()) : "--";
}

/* ── Model scorecard (projection accuracy) ─────────────────── */

const API_SCORECARD = "/api/v4/scorecard";
let scorecardTimer = null;

function renderScorecard(d) {
  const grid = $("scorecardGrid");
  if (!grid) return;
  const versions = (d.summary && d.summary.versions) || {};
  const ver = versions["v4-pace-1"] || {};
  const q = versions._quality || {};
  const fx = d.fixed_checkpoints || [];
  const mc = d.market_compare || {};
  const recent = d.recent || [];
  const html = [];
  // Clean-data boundary: the scorecard is the CLEAN statistical surface.
  // Pre-epoch rows are excluded from every aggregate (audit only).
  html.push(`<div class="sc-block sc-wide">
    <div class="legacy-banner" style="margin:0">CLEAN DATA — FROM ${esc(String(d.data_epoch || "").slice(0, 19))}Z · legacy / pre-clean observations excluded from every aggregate (audit only, via /api/v4/history and the events LEGACY view)</div>
  </div>`);
  // ── MARKET VS FAIR — PRIMARY (M009-M2 refined) ────────────────
  const mvF = d.market_vs_fair || {};
  const mvc = mvF.checkpoints || [];
  if (mvc.length) {
    html.push(`<div class="sc-block sc-wide">
      <h4>MARKET VS FAIR VALUE <span class="muted">· PRIMARY · Market − Fair, signed · clean completed games only</span></h4>
      <table class="sc-table">
        <tr><th>Checkpoint</th><th>N</th><th>Avg Market</th><th>Avg Fair</th><th>Avg M-F</th><th>Med M-F</th><th>Under Value %</th><th>Over Value %</th></tr>
        ${mvc.map((c) => `<tr>
          <td>${c.checkpoint_pct}%</td>
          <td class="sc-num">${c.n}</td>
          <td class="sc-num">${c.avg_market ?? "–"}</td>
          <td class="sc-num">${c.avg_fair ?? "–"}</td>
          <td class="sc-num">${sig(c.avg_mf)}</td>
          <td class="sc-num">${sig(c.median_mf)}</td>
          <td class="sc-num">${c.under_value_n} / ${c.n}${c.under_value_pct != null ? " (" + (c.under_value_pct * 100).toFixed(0) + "%)" : ""}</td>
          <td class="sc-num">${c.over_value_n} / ${c.n}${c.over_value_pct != null ? " (" + (c.over_value_pct * 100).toFixed(0) + "%)" : ""}</td>
        </tr>`).join("")}
      </table>
      <table class="sc-table" style="margin-top:8px">
        <tr><th>Checkpoint</th><th>OVER WIN</th><th>OVER LOSS</th><th>UNDER WIN</th><th>UNDER LOSS</th><th>PUSH</th><th>NO EDGE</th><th>Posn win rate</th><th>Avg OLV→CLV</th><th>→BLM</th><th>←BLM</th></tr>
        ${mvc.map((c) => `<tr>
          <td>${c.checkpoint_pct}%</td>
          <td class="sc-num">${c.over_win}</td><td class="sc-num">${c.over_loss}</td>
          <td class="sc-num">${c.under_win}</td><td class="sc-num">${c.under_loss}</td>
          <td class="sc-num">${c.push_outcome}</td>
          <td class="sc-num">${c.no_edge ?? 0}</td>
          <td class="sc-num">${c.position_win_rate != null ? (c.position_win_rate * 100).toFixed(0) + "%" : "–"}</td>
          <td class="sc-num">${c.avg_olv_to_clv ?? "–"}</td>
          <td class="sc-num">${c.move_toward}</td><td class="sc-num">${c.move_away}</td>
        </tr>`).join("")}
      </table>
    </div>`);
    const games = mvF.games || [];
    if (games.length) {
      html.push(`<div class="sc-block sc-wide">
        <h4>GAME-LEVEL SCORECARD <span class="muted">· progressive Market vs Fair per clean completed game</span></h4>
        ${games.map((g) => `<details class="mvf-game">
          <summary>${esc(g.home_team || "")} vs ${esc(g.away_team || "")} · OLV ${g.olv ?? "–"} · CLV ${g.clv ?? "–"} · Final ${g.final_total ?? "–"} · vs OLV ${g.outcome_olv ?? "–"} · vs CLV ${g.outcome_clv ?? "–"}</summary>
          <table class="sc-table">
            <tr><th>%</th><th>Market</th><th>BLM Fair</th><th>M-F</th><th>Signal</th><th>Actual</th><th>Outcome</th></tr>
            ${(g.rows || []).map((r) => `<tr>
              <td>${obsPctLabel(r)}<div class="muted" style="font-size:9px">${r.elapsed_minutes != null ? num(r.elapsed_minutes, 2) + " min" : ""}</div></td>
              <td class="sc-num">${r.fair ?? "–"}</td>
              <td class="sc-num">${sig(r.mf)}</td>
              <td>${r.signal ? esc(r.signal) : "–"}</td>
              <td class="sc-num">${r.actual ?? "–"}</td>
              <td>${r.outcome ? esc(r.outcome) : "–"}${r.terminal ? ` <span class="st st-stale" title="${esc(r.exclusion_reason || "TERMINAL CHECKPOINT")}">SETTLEMENT ONLY</span>` : ""}</td>
            </tr>`).join("")}
          </table>
        </details>`).join("")}
      </div>`);
    }
  }
  // ── DISPARITY BANDS (M009-M5) — |fair − market| magnitude × direction ──
  // Magnitude and direction stay separate.  Rates are observed with N —
  // never a strategy claim; small samples (reliable=false) are flagged.
  const ebs = mvF.edge_buckets || [];
  if (ebs.length) {
    const dirLabel = (d) => d === "BLM_OVER" ? "BLM_OVER · fair above market"
      : d === "BLM_UNDER" ? "BLM_UNDER · fair below market" : d;
    const bandRate = (e) => {
      const base = `N=${e.n} | BLM position win rate ${pct(e.win_rate)}`;
      return e.reliable === false ? `${base} · SMALL SAMPLE` : base;
    };
    html.push(`<div class="sc-block sc-wide">
      <h4>DISPARITY BANDS <span class="muted">· |BLM fair − market| magnitude × direction · observed rates with N, never a strategy claim${mvF.edge_bucket_min_sample != null ? ` · min sample ${mvF.edge_bucket_min_sample}` : ""}</span></h4>
      ${["BLM_OVER", "BLM_UNDER"].map((dir) => {
        const rows = ebs.filter((e) => e.direction === dir);
        if (!rows.length) return "";
        return `<div class="band-dir">${dirLabel(dir)}</div>
        <table class="sc-table">
          <tr><th>Bucket</th><th>Dir</th><th>N</th><th>Over/Under/Push</th><th>BLM position win rate</th><th>Market win rate</th><th>Avg Δ (signed)</th><th>Fresh/Stale N</th><th>Fresh/Stale WR</th><th>Avg age</th></tr>
          ${rows.map((e) => `<tr>
            <td>${esc(e.bucket)}</td>
            <td>${esc(e.direction)}</td>
            <td class="sc-num">${e.n ?? "–"}</td>
            <td class="sc-num">${[e.over_n, e.under_n, e.push_n].map((v) => v ?? "–").join(" / ")}</td>
            <td class="sc-num">${bandRate(e)}</td>
            <td class="sc-num">${pct(e.market_win_rate)}</td>
            <td class="sc-num">${sig(e.avg_diff)}</td>
            <td class="sc-num">${[e.fresh_n, e.stale_n].map((v) => v ?? "–").join(" / ")}</td>
            <td class="sc-num">${[e.fresh_win_rate, e.stale_win_rate].map((v) => pct(v)).join(" / ")}</td>
            <td class="sc-num">${e.avg_age != null ? e.avg_age.toFixed(0) + "s" : "–"}</td>
          </tr>`).join("")}
        </table>`;
      }).join("")}
    </div>`);
  }
  // ── TIME-OF-DAY (M009-M5) — start hour x outcome, observed only ─────
  const tod = mvF.time_of_day;
  if (tod && (tod.hours || []).length) {
    const todRow = (r, lab) => `<tr>
      <td>${lab}</td>
      <td class="sc-num">${r.n ?? "–"}</td>
      <td class="sc-num">${r.over_n ?? "–"}</td>
      <td class="sc-num">${r.under_n ?? "–"}</td>
      <td class="sc-num">${pct(r.blm_win_rate)}</td>
      <td class="sc-num">${pct(r.market_win_rate)}</td>
      <td class="sc-num">${sig(r.avg_diff)}</td>
    </tr>`;
    const todHours = tod.hours.filter((h) => (h.n || 0) > 0)
      .map((h) => todRow(h, `${String(h.hour).padStart(2, "0")}:00`)).join("");
    const todBands = (tod.bands || []).filter((b) => (b.n || 0) > 0)
      .map((b) => todRow(b, esc(b.band))).join("");
    html.push(`<div class="sc-block sc-wide">
      <h4>TIME-OF-DAY <span class="muted">· first-observed hour, local${tod.band_def ? ` · bands: ${esc(tod.band_def)}` : ""}</span></h4>
      <table class="sc-table">
        <tr><th>Hour</th><th>N</th><th>Over</th><th>Under</th><th>BLM position win rate</th><th>Market win rate</th><th>Avg Δ</th></tr>
        ${todHours || `<tr><td colspan="7" class="sc-num">no rows yet</td></tr>`}
      </table>
      ${todBands ? `<table class="sc-table" style="margin-top:8px">
        <tr><th>Band</th><th>N</th><th>Over</th><th>Under</th><th>BLM win rate</th><th>Market win rate</th><th>Avg Δ</th></tr>
        ${todBands}
      </table>` : ""}
    </div>`);
  }
  // current model performance
  const v = { model_version: d.model_version || "v4-pace-1", ...ver };
  html.push(`<div class="sc-block">
    <h4>MODEL ${esc(v.model_version || "?")} <span class="muted">· DIAGNOSTIC — prediction-vs-actual accuracy (population: prediction_scores fragment=0)</span></h4>
    <table class="sc-table">
      <tr><td>Predictions</td><td class="sc-num">${v.predictions ?? "–"}</td></tr>
      <tr><td>Completed games</td><td class="sc-num">${v.completed_games ?? "–"}</td></tr>
      <tr><td>MAE (total)</td><td class="sc-num">${v.mae ?? "–"}</td></tr>
      <tr><td>RMSE</td><td class="sc-num">${v.rmse ?? "–"}</td></tr>
      <tr><td>Median abs err</td><td class="sc-num">${v.median_abs_error ?? "–"}</td></tr>
      <tr><td>Bias (signed)</td><td class="sc-num">${v.bias ?? "–"}</td></tr>
      <tr><td>Home MAE</td><td class="sc-num">${v.home_mae ?? "–"}</td></tr>
      <tr><td>Away MAE</td><td class="sc-num">${v.away_mae ?? "–"}</td></tr>
      <tr><td>MAPE</td><td class="sc-num">${v.mape ?? "–"}</td></tr>
    </table>
  </div>`);
  // accuracy by fixed checkpoint
  html.push(`<div class="sc-block">
    <h4>ACCURACY BY GAME PROGRESS</h4>
    <table class="sc-table">
      <tr><th>Checkpoint</th><th>N</th><th>MAE</th><th>Median</th></tr>
      ${fx.map((c) => `<tr><td>${c.percent}%</td><td class="sc-num">${c.n}</td><td class="sc-num">${c.mae ?? "–"}</td><td class="sc-num">${c.median ?? "–"}</td></tr>`).join("")}
    </table>
  </div>`);
  // MODEL vs MARKET (forensic, M008-SCORE-M1)
  html.push(`<div class="sc-block">
    <h4>MODEL vs MARKET — DIAGNOSTIC <span class="muted">(line: ${mc.line_type || "checkpoint_market"}; population: prediction_scores fragment=0)</span></h4>
    <table class="sc-table">
      <tr><td>Valid comparisons</td><td class="sc-num">${mc.n ?? 0}</td></tr>
      <tr><td>BLM MAE</td><td class="sc-num">${mc.model_mae ?? "–"}</td></tr>
      <tr><td>BLM Bias</td><td class="sc-num">${mc.model_bias ?? "–"}</td></tr>
      <tr><td>Market MAE</td><td class="sc-num">${mc.market_mae ?? "–"}</td></tr>
      <tr><td>Market Bias</td><td class="sc-num">${mc.market_bias ?? "–"}</td></tr>
      <tr><td>BLM beat market</td><td class="sc-num">${mc.model_beat_market_n ?? 0} / ${mc.model_beat_market_d ?? mc.n ?? 0} = ${(mc.model_beat_market_rate ?? 0) * 100}%</td></tr>
      <tr><td>Market beat BLM</td><td class="sc-num">${mc.market_beat_blm_n ?? 0} / ${mc.n ?? 0}</td></tr>
      <tr><td>Ties</td><td class="sc-num">${mc.ties_n ?? 0} / ${mc.n ?? 0}</td></tr>
    </table>
  </div>`);
  // O/U — names the line type (BLM vs checkpoint market)
  html.push(`<div class="sc-block">
    <h4>O/U PERFORMANCE — DIAGNOSTIC <span class="muted">(${mc.ou_line_type || "checkpoint market"}; population: prediction_scores fragment=0)</span></h4>
    <table class="sc-table">
      <tr><td colspan="2" class="muted">BLM selection · BLM prediction vs market line</td></tr>
      <tr><td>BLM Over</td><td class="sc-num">${mc.ou_over ?? 0}</td></tr>
      <tr><td>BLM Under</td><td class="sc-num">${mc.ou_under ?? 0}</td></tr>
      <tr><td>BLM No edge</td><td class="sc-num">${mc.ou_push ?? 0}</td></tr>
      <tr><td colspan="2" class="muted">Actual outcome · final total vs market line</td></tr>
      <tr><td>Actual Over</td><td class="sc-num">${mc.actual_over ?? 0}</td></tr>
      <tr><td>Actual Under</td><td class="sc-num">${mc.actual_under ?? 0}</td></tr>
      <tr><td>Actual Push</td><td class="sc-num">${mc.actual_push ?? 0}</td></tr>
      <tr><td>Hit rate</td><td class="sc-num">${mc.ou_hit_n ?? 0} / ${mc.ou_hit_d ?? 0} = ${((mc.ou_hit_rate ?? 0) * 100).toFixed(1)}%</td></tr>
    </table>
  </div>`);
  // data quality — RECORDED vs COMPLETED vs VALID vs EXCLUDED (never conflated)
  html.push(`<div class="sc-block">
    <h4>DATA QUALITY</h4>
    <table class="sc-table">
      <tr><td>Recorded predictions</td><td class="sc-num">${q.recorded_predictions ?? "–"}</td></tr>
      <tr><td>Headline predictions (valid games)</td><td class="sc-num">${q.headline_predictions ?? "–"}</td></tr>
      <tr><td>Completed games (OK result)</td><td class="sc-num">${q.completed_games ?? "–"}</td></tr>
      <tr><td>Valid scored games</td><td class="sc-num">${q.valid_scored_games ?? "–"}</td></tr>
      <tr><td>Invalid / excluded</td><td class="sc-num">${q.invalid ?? "–"}</td></tr>
      <tr><td>Excluded games total</td><td class="sc-num">${q.excluded_games ?? "–"}</td></tr>
      <tr><td>Reasons</td><td>${Object.entries(q.excluded_reasons || {}).map(([k, n]) => `${esc(k)}: ${n}`).join(", ") || "–"}</td></tr>
    </table>
  </div>`);
  // M007-M8: per-game eligibility audit — every headline metric is
  // traceable to game_id -> quality -> result -> eligible -> contribution.
  const audit = d.summary && d.summary.eligible_games || [];
  if (audit.length) {
    html.push(`<div class="sc-block">
    <h4>GAME ELIGIBILITY (audit)</h4>
    <table class="sc-table">
      <tr><th>Game</th><th>Teams</th><th>Result</th><th>Final</th><th>Qual</th><th>Preds</th><th>Contrib MAE</th><th>✓</th></tr>
      ${audit.map((g) => `<tr>
        <td class="sc-num">${esc(g.source_game_id)}</td>
        <td>${esc((g.home_team || "").slice(0, 14))} vs ${esc((g.away_team || "").slice(0, 14))}</td>
        <td class="sc-num">${esc(g.result_status || "–")}</td>
        <td class="sc-num">${g.final_home != null ? `${g.final_home}-${g.final_away}` : "–"}</td>
        <td class="sc-num">${esc(g.quality_status || "–")}</td>
        <td class="sc-num">${g.predictions_used ?? 0}</td>
        <td class="sc-num">${g.contribution_mae != null ? g.contribution_mae : "–"}</td>
        <td class="sc-num">${g.eligible ? "✓" : "✗"}</td>
      </tr>`).join("")}
    </table>
  </div>`);
  }
  // recent predictions
  if (recent.length) {
    html.push(`<div class="sc-block sc-wide">
      <h4>RECENT PREDICTIONS <span style="color:var(--muted);font-weight:400">· fragment rows are diagnostics, excluded from headline</span></h4>
      <table class="sc-table">
        <tr><th>Game</th><th>Check</th><th>Pred</th><th>Actual</th><th>Err</th><th>Time</th></tr>
        ${recent.map((r) => `<tr>
          <td>${esc(r.home_team || r.source_game_id)} vs ${esc(r.away_team || "")}${r.fragment ? ` <span style="color:#e8a13d">FRAGMENT</span>` : ""}</td>
          <td>${r.checkpoint_percent != null ? (r.checkpoint_percent * 100).toFixed(0) + "%" : esc(r.checkpoint || "")}</td>
          <td class="sc-num">${r.model_total ?? "–"}</td>
          <td class="sc-num">${r.actual_total ?? "–"}</td>
          <td class="sc-num">${r.total_error ?? "–"}</td>
          <td>${fmtTime(r.scored_at)}</td>
        </tr>`).join("")}
      </table>
    </div>`);
  }
  grid.innerHTML = html.join("");
  $("scorecardSub").textContent =
    `${v.predictions ?? 0} predictions · ${v.completed_games ?? 0} games · MAE ${v.mae ?? "–"}`;
}

async function refreshScorecard() {
  try {
    const resp = await fetch(API_SCORECARD);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const d = await resp.json();
    renderScorecard(d);
  } catch (err) {
    const grid = $("scorecardGrid");
    if (grid) grid.innerHTML = `<div class="empty">Scorecard unavailable: ${esc(err.message)}</div>`;
  }
}

$("scorecardToggle").addEventListener("click", () => {
  const body = $("scorecardBody");
  const open = body.hidden;
  body.hidden = !open;
  $("scorecardToggle").setAttribute("aria-expanded", String(open));
  $("scorecardToggle").classList.toggle("open", open);
  if (open) {
    refreshScorecard();
    if (!scorecardTimer) scorecardTimer = setInterval(refreshScorecard, 30000);
  } else if (scorecardTimer) {
    clearInterval(scorecardTimer);
    scorecardTimer = null;
  }
});

/* ── Market & historical trends (clean games, observations only) ── */

const API_TRENDS = "/api/v4/trends";
let trendsTimer = null;

function pctCell(n, denom) {
  if (!denom) return `<td class="sc-num">–</td>`;
  return `<td class="sc-num">${n} / ${denom} (${((n / denom) * 100).toFixed(1)}%)</td>`;
}

function renderTrends(d) {
  const grid = $("trendsGrid");
  if (!grid) return;
  const mp = d.market_performance || {};
  const mv = d.market_movement || {};
  const mm = d.model_vs_market || {};
  const tod = d.time_of_day || {};
  const html = [];

  // MARKET PERFORMANCE — OLVC vs CLV, counts WITH sample sizes
  html.push(`<div class="sc-block sc-wide">
    <h4>MARKET PERFORMANCE · tz ${esc(d.analytics_tz || "?")}</h4>
    <table class="sc-table">
      <tr><th></th><th>OLVC</th><th>CLV</th></tr>
      <tr><td>OVER</td>${[mp.olvc, mp.clv].map((s) => pctCell(s.over, s.n)).join("")}</tr>
      <tr><td>UNDER</td>${[mp.olvc, mp.clv].map((s) => pctCell(s.under, s.n)).join("")}</tr>
      <tr><td>PUSH</td>${[mp.olvc, mp.clv].map((s) => pctCell(s.push, s.n)).join("")}</tr>
      <tr><td>Avg Δ (actual − line)</td>
        <td class="sc-num">${mp.olvc.avg_edge ?? "–"}</td>
        <td class="sc-num">${mp.clv.avg_edge ?? "–"}</td></tr>
      <tr><td>Median Δ CLV</td><td class="sc-num">–</td>
        <td class="sc-num">${mp.clv.median_edge ?? "–"}</td></tr>
    </table>
  </div>`);

  // TIME-OF-DAY — grouped buckets
  const g = (tod.grouped || []).map((b) => `<tr>
      <td>${esc(b.period)}</td>
      <td class="sc-num">${b.games}</td>
      <td class="sc-num">${b.clv_n}</td>
      ${pctCell(b.over_clv, b.clv_n)}
      ${pctCell(b.under_clv, b.clv_n)}
      <td class="sc-num">${b.avg_delta_clv ?? "–"}</td>
      <td class="sc-num">${b.mae_clv ?? "–"}</td>
    </tr>`).join("");
  html.push(`<div class="sc-block sc-wide">
    <h4>TIME-OF-DAY (first-observed hour, local)</h4>
    <table class="sc-table">
      <tr><th>Period</th><th>Games</th><th>CLV N</th><th>CLV OVER</th>
        <th>CLV UNDER</th><th>Avg ΔCLV</th><th>MAE CLV</th></tr>
      ${g || `<tr><td colspan="7" class="sc-num">no clean games yet</td></tr>`}
    </table>
  </div>`);

  // MARKET MOVEMENT
  html.push(`<div class="sc-block">
    <h4>MARKET MOVEMENT (OLVC→CLV)</h4>
    <table class="sc-table">
      <tr><td>Games (both lines)</td><td class="sc-num">${mv.n ?? 0}</td></tr>
      <tr><td>Avg move</td><td class="sc-num">${mv.avg_move ?? "–"}</td></tr>
      <tr><td>Median move</td><td class="sc-num">${mv.median_move ?? "–"}</td></tr>
      <tr><td>UP</td><td class="sc-num">${mv.up ?? 0}</td></tr>
      <tr><td>DOWN</td><td class="sc-num">${mv.down ?? 0}</td></tr>
      <tr><td>UNCHANGED</td><td class="sc-num">${mv.unchanged ?? 0}</td></tr>
    </table>
  </div>`);

  // MODEL VS MARKET
  const verRows = Object.entries(mm.by_version || {}).map(([ver, v]) => `<tr>
      <td>${esc(ver)}</td>
      <td class="sc-num">${v.n ?? 0}</td>
      <td class="sc-num">${v.avg_model_edge ?? "–"}</td>
      <td class="sc-num">${v.model_over_pct ?? "–"}%</td>
      <td class="sc-num">${v.dir_hit_rate ?? "–"}%</td>
      <td class="sc-num">${v.beat_market_rate ?? "–"}%</td>
    </tr>`).join("");
  html.push(`<div class="sc-block sc-wide">
    <h4>MODEL VS MARKET (clean games)</h4>
    <table class="sc-table">
      <tr><th>Version</th><th>N</th><th>Avg edge</th><th>Model OVER %</th>
        <th>Direction hit %</th><th>Beat market %</th></tr>
      ${verRows || `<tr><td colspan="6" class="sc-num">no scored clean predictions yet</td></tr>`}
    </table>
  </div>`);

  grid.innerHTML = html.join("");
  $("trendsSub").textContent =
    `clean games: ${mp.clv.n ?? 0} w/ line · tz ${d.analytics_tz || "?"} · observations only`;
}

async function refreshTrends() {
  try {
    const resp = await fetch(API_TRENDS);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const d = await resp.json();
    renderTrends(d);
  } catch (err) {
    const grid = $("trendsGrid");
    if (grid) grid.innerHTML = `<div class="empty">Trends unavailable: ${esc(err.message)}</div>`;
  }
}

$("trendsToggle").addEventListener("click", () => {
  const body = $("trendsBody");
  const open = body.hidden;
  body.hidden = !open;
  $("trendsToggle").setAttribute("aria-expanded", String(open));
  $("trendsToggle").classList.toggle("open", open);
  if (open) {
    refreshTrends();
    if (!trendsTimer) trendsTimer = setInterval(refreshTrends, 60000);
  } else if (trendsTimer) {
    clearInterval(trendsTimer);
    trendsTimer = null;
  }
});

/* ── Event dataset (M009-M5, inspection) ────────────────── */

const API_EVENTS = "/api/v4/scorecard/events";
let eventsTimer = null;
const eventsState = {
  direction: "", freshness: "", checkpoint: "",
  minDiff: "", maxDiff: "", game: "", quality: "CLEAN", limit: 200,
};

function eventsURL() {
  const p = new URLSearchParams();
  if (eventsState.direction) p.set("direction", eventsState.direction);
  if (eventsState.freshness) p.set("freshness", eventsState.freshness);
  if (eventsState.checkpoint) p.set("checkpoint", eventsState.checkpoint);
  if (eventsState.minDiff) p.set("min_diff", eventsState.minDiff);
  if (eventsState.maxDiff) p.set("max_diff", eventsState.maxDiff);
  if (eventsState.game) p.set("game", eventsState.game);
  if (eventsState.quality) p.set("quality", eventsState.quality);
  p.set("limit", String(eventsState.limit));
  return `${API_EVENTS}?${p.toString()}`;
}

const evStatusCls = (s) => s === "LIVE" ? "st-live"
  : s === "STALE" ? "st-stale" : s === "MISSING" ? "st-missing" : "";

// B2: under a checkpoint target % (e.g. "30%"), show the ACTUAL game
// state the API supplies at that checkpoint — period, clock, elapsed
// minutes over the classification duration, and true progress — so the
// target position is never mistaken for game time.  Empty when the API
// has no game state (historical rows without a resolvable snapshot).
// ACTUAL OBSERVATION PROGRESS (label-semantics directive): the
// checkpoint bucket (10..100) is a grouping target, not game time.  The
// displayed progress must be the observation's real elapsed/full
// duration (authoritative elapsed_game_minutes / total_game_minutes),
// e.g. a final snapshot at 39.25/40.00 min displays 98.1% — never the
// 100% bucket label.  Falls back to the bucket only when the row has no
// recorded game time.
function obsPctLabel(r) {
  if (r.progress != null) return (r.progress * 100).toFixed(1) + "%";
  if (r.elapsed_minutes != null) {
    const full = r.classification === "CYBER_2K26" ? 48 : 40;
    return (r.elapsed_minutes / full * 100).toFixed(1) + "%";
  }
  return r.checkpoint_pct != null ? r.checkpoint_pct + "%" : "–";
}

function cpStateHTML(r, classification) {
  const parts = [];
  if (r.period_label_at_checkpoint) parts.push(esc(r.period_label_at_checkpoint));
  if (r.clock_at_checkpoint) parts.push(esc(r.clock_at_checkpoint));
  if (r.elapsed_minutes != null) {
    const full = classification === "CYBER_2K26" ? 48 : 40;
    parts.push(num(r.elapsed_minutes, 2) + "/" + full.toFixed(2) + " min");
  }
  if (r.progress != null) parts.push((r.progress * 100).toFixed(1) + "%");
  // ACTUAL PROGRESS vs CHECKPOINT BUCKET (label-semantics directive):
  // when the row's actual observation time differs from its analytical
  // bucket target, name the bucket explicitly — the 100% bucket must
  // never be mistaken for game time or for terminal status.
  if (r.progress != null && r.checkpoint_pct != null
      && Math.round(r.progress * 100) !== r.checkpoint_pct) {
    parts.push(`bucket ${r.checkpoint_pct}%`);
  }
  return parts.length
    ? `<div class="muted" style="font-size:9px">${parts.join(" · ")}</div>` : "";
}

function renderEvents(d) {
  const grid = $("eventsGrid");
  if (!grid) return;
  const rows = d.rows || [];
  const total = d.total ?? rows.length;
  const rowsHtml = rows.map((r) => `<tr>
    <td>${esc(r.home_team || r.game || "–")} vs ${esc(r.away_team || "")}<div class="muted" style="font-size:9px">${esc(r.game || "")}</div></td>
    <td>${obsPctLabel(r)}<div class="muted" style="font-size:9px">${fmtTime(r.checkpoint_ts)}</div>${cpStateHTML(r, r.classification)}</td>
    <td class="sc-num">${num(r.market_line, 1)}</td>
    <td class="sc-num">${num(r.blm_fair, 1)}</td>
    <td class="sc-num ${r.diff > 0 ? "pos" : r.diff < 0 ? "neg" : ""}">${sig(r.diff)}</td>
    <td>${esc(r.direction || "–")}</td>
    <td>${r.market_status == null ? "–" : `<span class="st ${evStatusCls(r.market_status)}">${esc(r.market_status)}</span>`}</td>
    <td class="sc-num">${r.market_age_seconds != null ? num(r.market_age_seconds, 0) + "s" : "–"}</td>
    <td>${esc(r.momentum_state || "–")}${r.momentum_strength != null ? ` · ${num(r.momentum_strength, 1)}` : ""}</td>
    <td class="sc-num">${r.false_momentum == null ? "–" : r.false_momentum ? "1" : "0"}</td>
    <td>${esc(r.blm_side || "–")}</td>
    <td class="sc-num">${num(r.actual, 1)}</td>
    <td>${esc(r.outcome || "–")}</td>
    <td class="sc-num">${r.blm_won == null ? "–" : r.blm_won ? "✓" : "✗"}</td>
    <td>${r.terminal
      ? `<span class="st st-stale" title="${esc(r.exclusion_reason || "TERMINAL CHECKPOINT")}">EXCLUDED</span> <span class="muted" style="font-size:9px">settlement only</span>`
      : `<span class="st" title="terminal: NO — actual game time is authoritative, the checkpoint bucket is analytical grouping only" style="color:var(--ok,#2e7d32)">ELIGIBLE</span>`}</td>
  </tr>`).join("");
  const epochNote = d.data_quality === "LEGACY"
    ? `LEGACY / PRE-CLEAN DATA · audit only · before ${esc(String(d.data_epoch || "").slice(0, 19))}Z`
    : `CLEAN DATA — FROM ${esc(String(d.data_epoch || "").slice(0, 19))}Z`;
  grid.innerHTML = `<div class="sc-block sc-wide">
    <h4>SCORECARD EVENTS <span class="muted">· ${epochNote} · inspection dataset — observed rows, never a strategy claim</span></h4>
    <table class="sc-table">
      <tr><th>Game</th><th>CP</th><th>Market line</th><th>BLM fair</th><th>Diff</th><th>Direction</th><th>Market status</th><th>Market age s</th><th>Momentum</th><th>False mom</th><th>BLM side</th><th>Actual</th><th>Outcome</th><th>BLM won</th><th>PRED VALIDATION</th></tr>
      ${rowsHtml || `<tr><td colspan="15" class="sc-num">no events match filters</td></tr>`}
    </table>
    <div class="muted" style="margin-top:6px">Total ${total} · showing ${rows.length} of ${total}${rows.length < total ? " — narrow filters or raise limit" : ""}${d.n_terminal != null ? ` · terminal (settlement/audit only) <b>${d.n_terminal}</b> · predictive-eligible <b>${d.n_predictive_eligible ?? total - d.n_terminal}</b> · TERMINAL = SETTLEMENT/AUDIT ONLY, never predictive evidence` : ""}</div>
  </div>`;
  $("eventsSub").textContent = `total ${total} · showing ${rows.length}`;
}

async function refreshEvents() {
  try {
    const resp = await fetch(eventsURL());
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const d = await resp.json();
    renderEvents(d);
  } catch (err) {
    const grid = $("eventsGrid");
    if (grid) grid.innerHTML = `<div class="empty">Events unavailable: ${esc(err.message)}</div>`;
  }
}

function syncEventsControls() {
  eventsState.direction = $("evDir").value;
  eventsState.freshness = $("evFresh").value;
  eventsState.checkpoint = $("evCp").value;
  eventsState.minDiff = $("evMinDiff").value;
  eventsState.game = $("evGame").value.trim();
  eventsState.quality = $("evQuality").value || "CLEAN";
  eventsState.limit = Number($("evLimit").value) || 200;
}

function bindEventsControls() {
  $("evApply").addEventListener("click", () => { syncEventsControls(); refreshEvents(); });
  // large-edges preset — INSPECTION ONLY, never a profitability claim
  $("evLarge").addEventListener("click", () => {
    syncEventsControls();
    eventsState.minDiff = "10";
    eventsState.maxDiff = "";
    $("evMinDiff").value = "10";
    refreshEvents();
  });
  $("evReset").addEventListener("click", () => {
    eventsState.direction = eventsState.freshness = eventsState.checkpoint = "";
    eventsState.minDiff = eventsState.maxDiff = eventsState.game = "";
    eventsState.quality = "CLEAN";
    eventsState.limit = 200;
    $("evDir").value = $("evFresh").value = $("evCp").value = "";
    $("evMinDiff").value = $("evGame").value = "";
    $("evQuality").value = "CLEAN";
    $("evLimit").value = "200";
    refreshEvents();
  });
}

$("eventsToggle").addEventListener("click", () => {
  const body = $("eventsBody");
  const open = body.hidden;
  body.hidden = !open;
  $("eventsToggle").setAttribute("aria-expanded", String(open));
  $("eventsToggle").classList.toggle("open", open);
  if (open) {
    refreshEvents();
    if (!eventsTimer) eventsTimer = setInterval(refreshEvents, 60000);
  } else if (eventsTimer) {
    clearInterval(eventsTimer);
    eventsTimer = null;
  }
});
bindEventsControls();

/* ── Game cards ──────────────────────────────────────────── */

// Regulation duration for elapsed/remaining/progress display
// (CYBER 2K26 = 48 min, BETUAL NBA = 40 min).
const fullMin = (g) => g.classification === "CYBER_2K26" ? 48 : 40;

// GAME STATE — observed score, period, clock, and (when the clean
// trajectory row exists) elapsed / remaining / progress minutes.  All
// measurements; nothing here is a forecast.
function gameStateHTML(g) {
  const p = g.projector || {};
  const full = fullMin(g);
  const bits = [];
  if (p.elapsed_game_minutes != null) bits.push(`${num(p.elapsed_game_minutes, 1)}/${full.toFixed(0)} min`);
  if (p.remaining_game_minutes != null) bits.push(`${num(p.remaining_game_minutes, 1)} left`);
  if (p.progress_pct != null) bits.push(`${(p.progress_pct * 100).toFixed(0)}%`);
  const extra = bits.length ? `<span class="muted">· ${bits.join(" · ")}</span>` : "";
  return `<div class="game-meta">
    <span class="period">${esc(g.period_label || (g.quarter ? "Q" + g.quarter : "–"))}</span>
    <span>${esc(g.clock || "–")}</span>
    <span>${g.snapshot_count} snaps</span>
    ${extra}
  </div>`;
}

// LIVE MARKET — the observed market total line with its freshness state
// (LIVE/STALE/MISSING at the M3 300s threshold, exact age).  A line is
// presented as CURRENT only when the game is live AND the observation is
// fresh; otherwise the last observed line is labeled as such (ENDED/STALE)
// and is never presented as current.  No comparison to any model value.
function liveMarketHTML(g) {
  const m = g.market || {};
  const p = g.projector || {};
  const isLive = g.live === true;
  // Prefer the clean trajectory row's line + freshness; fall back to the
  // API market block (same observed line, same M3 rules).
  const line = p.live_total_line != null ? p.live_total_line : m.total_line;
  const age = p.market_age_seconds != null ? p.market_age_seconds : m.total_line_age_s;
  const mstatus = p.market_status || mktStatusWord(age, line != null);
  const liveLine = isLive && mstatus === "LIVE" ? line : null;
  const lastObs = liveLine == null && line != null ? line : null;
  const statusWord = g.status === "ended" ? "ENDED" : "STALE";
  // exact market age (M009-M5 integrity): LIVE · 18s / STALE · 4m 21s /
  // MISSING · — ; never fabricated, absent age renders unavailable.
  const freshTxt = mstatus == null ? "" : `${mstatus} ${mstatus === "MISSING" ? "—" : fmtAgeExact(age)}`;
  const lastMeta = lastObs != null
    ? ` @ ${((m.total_line_at || p.market_captured_at) || "").slice(11, 19)}Z · ${statusWord}`
    : "";
  const src = m.market_source ? ` · ${m.market_source}` : "";
  return `<div class="live-market">
    <div class="live-market-label">Live Market</div>
    <div class="live-market-row">
      <span class="lm-line">${liveLine != null ? `LIVE TOTAL <b>${num(liveLine, 1)}</b>`
        : lastObs != null ? `LAST OBSERVED <b>${num(lastObs, 1)}</b>`
        : "LIVE TOTAL <b>–</b>"}</span>
      <span class="lm-fresh">${freshTxt ? `<span class="st ${mstatus === "LIVE" ? "st-live" : mstatus === "STALE" ? "st-stale" : "st-missing"}">${esc(freshTxt)}</span>` : ""}</span>
      <span class="muted">${src}</span>
    </div>
    ${lastMeta ? `<div class="lm-sub muted">${lastMeta}</div>` : ""}
  </div>`;
}

// Observed pace trend — direction, velocity and acceleration as
// measurements of the observed scoring rate.  Descriptive only.
function paceTrendHTML(g) {
  const m = g.momentum || {};
  const dir = m.direction || "flat";
  const word = dir === "up" ? "RISING" : dir === "down" ? "FALLING" : "FLAT";
  const arrow = dir === "up" ? "↗" : dir === "down" ? "↘" : "→";
  if (m.velocity == null && m.acceleration == null) return "";
  const accelCls = m.acceleration > 0 ? "pos" : m.acceleration < 0 ? "neg" : "";
  return `<div class="pace-trend" title="Observed scoring pace trend — velocity pts/min and acceleration, measurements only">
    <span class="pt-dir ${dir}">${arrow} ${word}</span>
    <span class="pt-item">vel <b>${num(m.velocity, 2)}</b> pts/min</span>
    <span class="pt-item">accel <b class="${accelCls}">${m.acceleration > 0 ? "+" : ""}${num(m.acceleration, 2)}</b></span>
  </div>`;
}

// Analytically INVALID games: the backend quality gate excluded them from
// every headline metric.  The card marks them EXCLUDED (data-quality
// state) while keeping the descriptive panels and historical diagnostics
// (lines, scoreboard, raw history) visible.
function gatedNoteHTML(g) {
  const reason = g.quality_reason || "quality gate failed";
  return `<div class="gated-note">INVALID — EXCLUDED FROM ANALYTICS
    <span class="muted">· ${esc(reason)} · historical rows retained for diagnostics</span></div>`;
}

// Pace strip — deterministic state on LIVE cards: actual Pts/Min vs the
// pace required to reach the live total line (gap = required − actual),
// plus the most recent observed pace window and acceleration where the
// clean trajectory row has them.  Measurements only — never a
// probability or an Over/Under call.
function paceStripHTML(g) {
  const p = g.projector;
  if (!p || !g.live) return "";
  const ap = p.actual_pts_per_min, rp = p.required_pts_per_min;
  if (ap == null && rp == null) return "";
  const gap = p.pace_gap;
  const gapCls = gap > 0 ? "pos" : gap < 0 ? "neg" : "";
  const signed = (v) => v == null ? "–" : (v > 0 ? "+" : "") + num(v, 2);
  const recent = p.recent_pace_1m != null ? `${num(p.recent_pace_1m, 2)} (1m)`
    : p.recent_pace_3m != null ? `${num(p.recent_pace_3m, 2)} (3m)` : null;
  const accel = p.pace_acceleration != null ? `accel ${signed(p.pace_acceleration)}` : null;
  const extra = [recent && `recent ${recent}`, accel].filter(Boolean).join(" · ");
  return `<div class="pace-strip" title="Current scoring pace vs pace required to reach the live total line — deterministic observed state, not a probability">
    <span class="pace-item"><b>PACE ${num(ap, 2)}</b> <span class="muted">pts/min</span></span>
    <span class="pace-item muted">req ${num(rp, 2)}</span>
    <span class="pace-item">gap <b class="${gapCls}">${signed(gap)}</b></span>
    ${extra ? `<span class="pace-item muted">${extra}</span>` : ""}
  </div>`;
}

function cardHTML(g, ui) {
  const invalid = g.quality_status === "INVALID";
  const liveCls = invalid ? "chip-excluded"
    : (g.live ? "chip-live" : (g.status === "ended" ? "chip-ended" : "chip-stale"));
  const liveTxt = invalid ? "EXCLUDED" : (g.live ? "LIVE" : g.status === "ended" ? "ENDED" : "STALE");
  const score = (v) => (v == null ? "–" : v);
  const m = g.market || {};
  // section collapse state (frontend-only): CHARTS default visible,
  // DETAILS default collapsed — users control both independently.
  const detOpen = !!(ui && ui.detOpen);
  const chartsOpen = !!(ui && ui.chartsOpen);
  return `
    <div class="card-head">
      <span class="cat-badge ${esc(g.classification)}">${esc(g.classification)}</span>
      <span class="comp-name">${esc(g.competition)}</span>
      <span class="${liveCls}">${liveTxt}</span>
    </div>
    <div class="scoreboard">
      <div class="team"><div class="team-name">${esc(g.home_team)}</div>
        <div class="team-score home">${score(g.home_score)}</div></div>
      <div class="vs">vs</div>
      <div class="team away"><div class="team-name">${esc(g.away_team)}</div>
        <div class="team-score away">${score(g.away_score)}</div></div>
    </div>
    ${gameStateHTML(g)}
    ${paceStripHTML(g)}
    ${liveMarketHTML(g)}
    ${paceTrendHTML(g)}
    ${invalid ? gatedNoteHTML(g) : ""}
    <details class="card-charts" ${chartsOpen ? "open" : ""}>
      <summary data-label="CHARTS">CHARTS ${chartsOpen ? "▾" : "▸"}</summary>
      <div class="spark"><canvas></canvas></div>
    </details>
    <details class="card-details" ${detOpen ? "open" : ""}>
      <summary data-label="DETAILS">DETAILS ${detOpen ? "▾" : "▸"}</summary>
      <div class="cd-grid">
        <div class="cd-item"><span class="cd-k">Opening line</span><span class="cd-v">${num(m.opening_line, 1)}${m.opening_line_at ? ` <span class="muted">@ ${(m.opening_line_at || "").slice(11, 19)}Z</span>` : ""}</span></div>
        <div class="cd-item"><span class="cd-k">Closing line</span><span class="cd-v">${num(m.closing_line, 1)}${m.closing_line_at ? ` <span class="muted">@ ${(m.closing_line_at || "").slice(11, 19)}Z</span>` : ""}</span></div>
        <div class="cd-item"><span class="cd-k">Market source</span><span class="cd-v">${esc(m.market_source || "–")}</span></div>
        <div class="cd-item"><span class="cd-k">Quality</span><span class="cd-v">${esc(g.quality_status || "OK")}</span></div>
        <div class="cd-item"><span class="cd-k">Snapshots</span><span class="cd-v">${g.snapshot_count || 0}</span></div>
        <div class="cd-item"><span class="cd-k">Status</span><span class="cd-v">${esc(g.status || "–")}</span></div>
      </div>
    </details>
    <div class="card-foot">
      <span>${fmtTime(g.last_update)}</span>
      <span title="1 − |score total − market line| ÷ market line — observed proximity, not a model comparison">mkt proximity ${num(g.market_efficiency, 3)}</span>
      <span>id ${esc(g.game_id)}</span>
    </div>`;
}

function makeSpark(canvas, g) {
  if (!hasChart()) return null;
  const ctx = canvas.getContext("2d");
  const chart = new Chart(ctx, {
    type: "line",
    data: { labels: [], datasets: [
      { label: g.home_team, data: [], borderColor: ChartColor.home, borderWidth: 1.6, pointRadius: 0, tension: .25 },
      { label: g.away_team, data: [], borderColor: ChartColor.away, borderWidth: 1.6, pointRadius: 0, tension: .25 },
    ]},
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: {
        x: { display: false },
        y: { display: false, suggestedMin: 0 },
      },
    },
  });
  updateSpark(chart, g);
  return chart;
}

function updateSpark(chart, g) {
  if (!chart) return;
  const h = g.history || [];
  chart.data.labels = h.map((s) => fmtTime(s.t));
  chart.data.datasets[0].data = h.map((s) => s.home);
  chart.data.datasets[1].data = h.map((s) => s.away);
  chart.update("none");
}

/* bind a card's collapsible CHARTS / DETAILS sections.  Each card keeps
   its own open state across the 5s re-renders (independent per card; the
   persisted prefs only seed brand-new cards). */
function bindCardSections(el, card) {
  const det = el.querySelector("details.card-details");
  if (det) det.addEventListener("toggle", () => {
    setSectionLabel(det);
    if (card) card.detOpen = det.open;
  });
  const ch = el.querySelector("details.card-charts");
  if (ch) ch.addEventListener("toggle", () => {
    setSectionLabel(ch);
    if (card) card.chartsOpen = ch.open;
  });
  el.querySelectorAll("details").forEach(setSectionLabel);
}

function renderCards(payload) {
  const grid = $("grid");
  const games = (payload.games || []).filter(
    (g) => !state.filter || g.classification === state.filter,
  );
  // M007-M6: presentation-only filter — hide ended/stale games without
  // touching the backend dataset.  "Hide stale and ended" = status ended
  // OR not live (existing API freshness: live = latest snapshot <= 15 min).
  const visible = state.hideNonLive
    ? games.filter((g) => g.live === true && g.status !== "ended")
    : games;
  syncLiveToggle(games.length, visible.length);
  const seen = new Set();
  for (const g of visible) {
    seen.add(g.game_id);
    let card = state.cards.get(g.game_id);
    if (!card) {
      const el = document.createElement("div");
      el.className = "card";
      const ui = {
        detOpen: !prefGet(PREF.GAME_DETAILS, true),   // DETAILS collapsed by default
        chartsOpen: !prefGet(PREF.CHARTS, false),     // CHARTS visible by default
      };
      el.innerHTML = cardHTML(g, ui);
      el.addEventListener("click", (ev) => {
        // clicks inside the CHARTS / DETAILS sections only toggle the
        // section — they never open the detail modal
        if (ev.target.closest("details")) return;
        openModal(g.game_id);
      });
      grid.appendChild(el);
      const canvas = el.querySelector(".spark canvas");
      const spark = makeSpark(canvas, g);
      state.cards.set(g.game_id, { el, spark, lastScore: null, detOpen: ui.detOpen, chartsOpen: ui.chartsOpen });
      card = state.cards.get(g.game_id);
      bindCardSections(el, card);
    }
    // score change → flash
    const nowScore = `${g.home_score}|${g.away_score}`;
    if (card.lastScore && card.lastScore !== nowScore) {
      card.el.classList.remove("flash");
      void card.el.offsetWidth;
      card.el.classList.add("flash");
    }
    card.lastScore = nowScore;
    // re-render while preserving each card's CHARTS / DETAILS state
    card.el.innerHTML = cardHTML(g, { detOpen: card.detOpen, chartsOpen: card.chartsOpen });
    bindCardSections(card.el, card);
    if (card.spark) {
      const holder = card.el.querySelector(".spark");
      holder.innerHTML = "";
      holder.appendChild(card.spark.canvas);
      updateSpark(card.spark, g);
    } else {
      const canvas = card.el.querySelector(".spark canvas");
      card.spark = makeSpark(canvas, g);
    }
  }
  for (const [gid, card] of state.cards) {
    if (!seen.has(gid)) {
      card.el.remove();
      if (card.spark) card.spark.destroy();
      state.cards.delete(gid);
    }
  }
  $("empty").style.display = games.length ? "none" : "block";
}

/* ── Detail modal ────────────────────────────────────────── */

function openModal(gameId) {
  if (!$("modalBackdrop").hidden) return; // already open — keep current view
  state.modalGameId = gameId;
  state.modalDetailOk = null;
  state.modalZText = null; // fresh game — readout awaits its own fetch
  state.modalGapText = null; // fresh game — gap readout awaits its own fetch
  $("modalBackdrop").hidden = false;
  document.body.style.overflow = "hidden";
  // render immediately from the live payload (never an empty overlay)
  const live = state.games.find((g) => g.game_id === gameId);
  if (live) renderModal(live);
  else $("modalBody").innerHTML = `<div class="empty">Loading…</div>`;
  loadModalDetail(gameId);
}

function closeModal() {
  $("modalBackdrop").hidden = true;
  document.body.style.overflow = "";
  state.modalGameId = null;
  state.modalDetailOk = null;
  for (const k in state.modalCharts) {
    if (state.modalCharts[k]) state.modalCharts[k].destroy();
  }
  state.modalCharts = {};
}

function modalPanel(title, inner) {
  return `<div class="m-panel"><h4>${title}</h4>${inner}</div>`;
}

function renderModal(g) {
  const mkt = g.market || {}, p = g.projector || {};
  const invalid = g.quality_status === "INVALID";
  // score_line_gap readout cache — lives across re-renders between
  // detail polls (same contract as state.modalZText), so the header
  // readout never flickers to n/a mid-poll.  Set in renderModalCharts.
  if (state.modalGapText === undefined) state.modalGapText = null;
  const full = fullMin(g);
  const isLive = g.live === true;
  // modal section state from the persisted prefs — CHARTS visible,
  // DETAILS collapsed for new users
  const chartsOpen = !prefGet(PREF.CHARTS, false);
  const detailsOpen = !prefGet(PREF.GAME_DETAILS, true);
  // Observed market line + freshness (clean trajectory row preferred,
  // API market block as fallback).  A line is current only when the game
  // is live AND fresh; otherwise it is labeled "Last observed".
  const line = p.live_total_line != null ? p.live_total_line : mkt.total_line;
  const age = p.market_age_seconds != null ? p.market_age_seconds : mkt.total_line_age_s;
  const mstatus = p.market_status || mktStatusWord(age, line != null);
  const liveLine = isLive && mstatus === "LIVE" ? line : null;
  const lastObs = liveLine == null && line != null ? line : null;
  const statusWord = g.status === "ended" ? "ENDED" : "STALE";
  // LIVE score-vs-line gap (directive: SCORE − LIVE LINE = ±X).  Computed
  // ONLY when the shown line is genuinely current (game live + fresh);
  // a stale/last-observed line never gets a live gap — it is not current
  // and must not be presented as such.  Pure arithmetic: current combined
  // score − observed line; the API's market.score_line_gap is the same
  // simple difference (used as fallback); no model value enters this.
  const liveScore = (g.home_score != null && g.away_score != null)
    ? g.home_score + g.away_score : null;
  const gapVal = (liveLine != null && liveScore != null)
    ? Number((liveScore - liveLine).toFixed(1)) : (liveScore != null ? mkt.score_line_gap : null);
  const gapTxt = (gapVal != null && (liveLine != null || mkt.score_line_gap != null))
    ? `${gapVal > 0 ? "+" : ""}${num(gapVal, 1)}` : "–";
  const freshTxt = mstatus == null ? "" : `${mstatus} ${mstatus === "MISSING" ? "—" : fmtAgeExact(age)}`;
  const signed = (v) => v == null ? "–" : (v > 0 ? "+" : "") + num(v, 2);
  const pace = (w) => p["recent_pace_" + w + "m"] != null ? num(p["recent_pace_" + w + "m"], 2) : "–";
  // Checkpoint market history — the market line frozen at-or-before each
  // checkpoint (observed rows only; no model prediction, no edge).
  const cps = (g.checkpoints || []).map((c) => `<tr>
    <td><b>${esc(c.label)}</b><div class="muted" style="font-size:9px">${fmtTime(c.predicted_at)}</div></td>
    <td class="sc-num">${num(c.market_at_checkpoint, 1)}</td>
    <td class="muted" style="font-size:10px">${c.source_snapshot_at ? `@ ${fmtTime(c.source_snapshot_at)}` : "–"}</td>
  </tr>`).join("");
  // SETTLEMENT / TERMINAL — the game's end-state checkpoint(s), served
  // separately from the predictive table (directive).  The stored row is
  // never deleted: it stays available as settlement/audit data with
  // predictive_eligible=0, so the end state can never be mistaken for a
  // predictive checkpoint.
  const settle = (g.checkpoints_settlement || []).map((c) => `<tr>
    <td><b>${esc(c.label)}</b><div class="muted" style="font-size:9px">${fmtTime(c.predicted_at)}</div></td>
    <td class="sc-num">${num(c.market_at_checkpoint, 1)}</td>
    <td class="muted" style="font-size:10px">${c.elapsed_minutes != null ? `${num(c.elapsed_minutes, 2)} min · ${c.progress != null ? (c.progress * 100).toFixed(1) : "–"}%` : "–"}</td>
    <td class="sc-num">${num(c.actual_final, 0)}</td>
    <td><span class="pill muted">TERMINAL — SETTLEMENT ONLY</span><div class="muted" style="font-size:9px">${esc(c.exclusion_reason || "")}</div></td>
  </tr>`).join("");
  // Live Timeline — observed score/market events only.  Momentum-type
  // annotations are pace-derivative commentary and are excluded from the
  // expanded game view, whose chart section is strictly observational.
  const tl = (g.timeline || []).filter((e) => (e.type || "") !== "momentum").map((e) =>
    `<div class="tl-item"><span class="tl-time">${fmtTime(e.t)}</span><span class="tl-label ${esc(e.type)}">${esc(e.label)}</span></div>`
  ).join("") || '<div class="tl-item"><span class="muted">No events yet</span></div>';
  const rawJson = g.raw || g.latest_snapshot || null;
  const stateBits = [];
  if (p.elapsed_game_minutes != null) stateBits.push(`${num(p.elapsed_game_minutes, 1)}/${full.toFixed(0)} min elapsed`);
  if (p.remaining_game_minutes != null) stateBits.push(`${num(p.remaining_game_minutes, 1)} min remaining`);
  if (p.progress_pct != null) stateBits.push(`${(p.progress_pct * 100).toFixed(0)}% progress`);

  $("mCat").textContent = g.classification || "–";
  $("mCat").className = `cat-badge ${esc(g.classification)}`;
  $("mTitle").textContent = `${g.home_team || "–"} vs ${g.away_team || "–"}`;

  // Preserve the primary chart canvas across re-renders: re-attaching the
  // SAME canvas node into the new markup keeps the Chart.js instance alive
  // so the 5-second refresh updates the series in place instead of
  // rebuilding — the chart never blinks out of the expanded game view.
  const prevCanvas = !invalid ? document.getElementById("mcTotal") : null;
  const prevZCanvas = !invalid ? document.getElementById("mcZ") : null;
  $("modalBody").innerHTML = `
    ${g.data_quality === "LEGACY" ? `<div class="legacy-banner">LEGACY / PRE-CLEAN GAME — started before the clean-data epoch (${esc(String(g.data_epoch || "").slice(0, 19))}Z). Current state below uses post-epoch observations only; full pre-clean history is available via the audit path.</div>` : ""}
    <div class="m-hero">
      <div class="m-score">
        <div><div class="team-name">${esc(g.home_team)}</div>
          <div class="team-score home">${g.home_score ?? "–"}</div></div>
        <div style="font-size:20px;color:var(--dim)">vs</div>
        <div><div class="team-name">${esc(g.away_team)}</div>
          <div class="team-score away">${g.away_score ?? "–"}</div></div>
      </div>
      <div style="text-align:right">
        <div class="period" style="font-family:var(--mono)">${esc(g.period_label || "")} ${esc(g.clock || "")}</div>
        <div class="muted" style="margin-top:4px">${g.snapshot_count || 0} snapshots · last ${fmtAge(g.age_s)}</div>
        ${stateBits.length ? `<div class="muted" style="margin-top:2px">${stateBits.join(" · ")}</div>` : ""}
      </div>
    </div>
    ${invalid ? gatedNoteHTML(g) : ""}
    ${invalid ? "" : `<details class="m-chart-toggle" ${chartsOpen ? "open" : ""}>
      <summary data-label="CHARTS">CHARTS ${chartsOpen ? "▾" : "▸"}</summary>
      <div class="m-charts">
        <div class="m-chart m-chart-full"><div class="m-chart-head"><h4>SCORE vs LIVE LINE — MARKET MOVEMENT</h4><div class="z-readout" id="gapReadout">${esc(state.modalGapText || "SCORE − LINE = –")}</div></div><div class="chart-box"><canvas id="mcTotal"></canvas></div></div>
        <div class="m-chart m-chart-z"><div class="m-chart-head"><h4>PACE Z-SCORE</h4><div class="z-readout" id="zPanelReadout">${esc(state.modalZText || "z = n/a")}</div></div><div class="chart-box chart-box-z"><canvas id="mcZ"></canvas></div></div>
      </div>
    </details>`}
    <div class="m-panels">
      ${modalPanel("Game State", `
        <div class="m-row"><span class="k">Provider</span><span class="v">${esc(g.provider || "–")}</span></div>
        <div class="m-row"><span class="k">Competition</span><span class="v">${esc(g.competition_slug || "–")}</span></div>
        <div class="m-row"><span class="k">Score</span><span class="v">${g.home_score ?? "–"} – ${g.away_score ?? "–"}</span></div>
        <div class="m-row"><span class="k">Period</span><span class="v">${esc(g.period_label || (g.quarter ? "Q" + g.quarter : "–"))}</span></div>
        <div class="m-row"><span class="k">Clock</span><span class="v">${esc(g.clock || "–")}</span></div>
        <div class="m-row"><span class="k">Elapsed</span><span class="v">${p.elapsed_game_minutes != null ? num(p.elapsed_game_minutes, 1) + " min" : "–"}</span></div>
        <div class="m-row"><span class="k">Remaining</span><span class="v">${p.remaining_game_minutes != null ? num(p.remaining_game_minutes, 1) + " min" : "–"}</span></div>
        <div class="m-row"><span class="k">Progress</span><span class="v">${p.progress_pct != null ? (p.progress_pct * 100).toFixed(1) + "%" : "–"}</span></div>
      `)}
      ${modalPanel("Live Market", `
        <div class="m-row"><span class="k">${liveLine != null ? "Live total" : (lastObs != null ? "Last observed" : "Live total")}</span><span class="v">${num(line, 1)}${lastObs != null ? ` <span class="muted" style="font-size:10px">@ ${((mkt.total_line_at || p.market_captured_at) || "").slice(11, 19)}Z · ${statusWord}</span>` : ""}</span></div>
        <div class="m-row"><span class="k">Score − line</span><span class="v ${gapVal > 0 ? "pos" : gapVal < 0 ? "neg" : ""}">SCORE ${liveScore != null ? liveScore : "–"} − LINE ${line != null ? num(line, 1) : "–"} = ${gapTxt}</span></div>
        <div class="m-row"><span class="k">Line freshness</span><span class="v">${freshTxt ? `<span class="st ${mstatus === "LIVE" ? "st-live" : mstatus === "STALE" ? "st-stale" : "st-missing"}">${esc(freshTxt)}</span>` : "–"}</span></div>
        <div class="m-row"><span class="k">Line age</span><span class="v">${age != null ? fmtAgeExact(age) : "–"}</span></div>
        <div class="m-row"><span class="k">Market source</span><span class="v">${esc(mkt.market_source || "–")}</span></div>
      `)}
      ${modalPanel("Pace", `
        <div class="m-row"><span class="k">Current Pts/Min</span><span class="v">${num(p.actual_pts_per_min, 2)}</span></div>
        <div class="m-row"><span class="k">Required Pts/Min (vs live line)</span><span class="v">${num(p.required_pts_per_min, 2)}</span></div>
        <div class="m-row"><span class="k">Pace gap (required − actual)</span><span class="v ${(p.pace_gap ?? 0) > 0 ? "pos" : (p.pace_gap ?? 0) < 0 ? "neg" : ""}">${signed(p.pace_gap)}</span></div>
        <div class="m-row"><span class="k">Recent pace 1/2/3/5 min</span><span class="v">${pace(1)} / ${pace(2)} / ${pace(3)} / ${pace(5)}</span></div>
        <div class="m-row"><span class="k">Acceleration${p.acceleration_window ? ` (${esc(p.acceleration_window)})` : ""}</span><span class="v ${(p.pace_acceleration ?? 0) > 0 ? "pos" : (p.pace_acceleration ?? 0) < 0 ? "neg" : ""}">${signed(p.pace_acceleration)}</span></div>
        <div class="muted" style="font-size:10px;margin-top:6px">Deterministic observed pace — measurements only, not a probability and not a forecast.</div>
      `)}
    </div>
    <details class="m-details" ${detailsOpen ? "open" : ""}>
      <summary data-label="DETAILS">DETAILS ${detailsOpen ? "▾" : "▸"}</summary>
      ${cps.length ? `<div class="m-panel m-wide">
        <h4>PREDICTIVE CHECKPOINTS — 10–90% non-terminal observations</h4>
        <table class="sc-table">
          <tr><th>Check</th><th>Market @CP</th><th>Snapshot</th></tr>
          ${cps}
        </table>
        <div class="muted" style="font-size:10px;margin-top:6px">Market line frozen at-or-before each checkpoint from stored observations — later movement never rewrites these rows; missing market shown as –. The terminal end-state row is served under SETTLEMENT / TERMINAL below — never deleted, never predictive.</div>
      </div>` : ""}
      ${settle.length ? `<div class="m-panel m-wide">
        <h4>SETTLEMENT / TERMINAL</h4>
        <table class="sc-table">
          <tr><th>Check</th><th>Market @CP</th><th>Game time</th><th>Actual</th><th>State</th></tr>
          ${settle}
        </table>
        <div class="muted" style="font-size:10px;margin-top:6px">Terminal observation (the game's end state, e.g. 100.0% / 40.00/40.00) — settlement/audit only: predictive_eligible=0, absent from every predictive checkpoint table and research statistic. The stored row is preserved (never deleted).</div>
      </div>` : ""}
      <div class="timeline"><h4>Live Timeline (from stored snapshots)</h4>${tl}</div>
      <div class="m-panel m-wide">
        <h4>Game Info / Data Quality</h4>
        <div class="m-rows">
          <div class="m-row"><span class="k">Classification</span><span class="v">${esc(g.classification)}</span></div>
          <div class="m-row"><span class="k">Competition</span><span class="v">${esc(g.competition)}</span></div>
          <div class="m-row"><span class="k">Event ID</span><span class="v">${esc(g.game_id)}</span></div>
          <div class="m-row"><span class="k">Region</span><span class="v">${esc(g.region)}</span></div>
          <div class="m-row"><span class="k">Status</span><span class="v">${esc(g.status)}</span></div>
          <div class="m-row"><span class="k">Quality</span><span class="v">${esc(g.quality_status || "OK")}${g.quality_reason ? ` · ${esc(g.quality_reason)}` : ""}</span></div>
          <div class="m-row"><span class="k">W1 / W2 odds</span><span class="v">${num(mkt.w1_odds, 2)} / ${num(mkt.w2_odds, 2)}</span></div>
          <div class="m-row"><span class="k">Team totals</span><span class="v">${num(mkt.home_total_line, 1)} / ${num(mkt.away_total_line, 1)}</span></div>
          <div class="m-row"><span class="k">Source</span><span class="v">${esc(g.source)}</span></div>
          ${g.data_epoch ? `<div class="m-row"><span class="k">Clean epoch</span><span class="v">${esc(String(g.data_epoch).slice(0, 19))}Z</span></div>` : ""}
        </div>
      </div>
    </details>
    ${rawJson ? `<details class="tech-raw"><summary>Technical / Raw Data</summary><pre>${esc(typeof rawJson === "string" ? rawJson : JSON.stringify(rawJson, null, 2))}</pre></details>` : ""}`;

  if (prevCanvas) {
    const box = $("modalBody").querySelector(".chart-box");
    if (box) {
      const fresh = box.querySelector("canvas");
      if (fresh) fresh.replaceWith(prevCanvas);
    }
  }
  // Same preservation for the Z-SCORE panel canvas: the fast path updates
  // its Chart instance in place, so this node must survive re-renders too.
  if (prevZCanvas) {
    const zBox = $("modalBody").querySelector(".chart-box-z");
    if (zBox) {
      const freshZ = zBox.querySelector("canvas");
      if (freshZ) freshZ.replaceWith(prevZCanvas);
    }
  }
  bindCollapsible($("modalBody").querySelector(".m-chart-toggle"), PREF.CHARTS);
  bindCollapsible($("modalBody").querySelector(".m-details"), PREF.GAME_DETAILS);
  // Fire-and-forget is fine, but failures must surface — a silent no-chart
  // state is exactly the bug class this view must never ship again.
  renderModalCharts(invalid ? null : g).catch((err) =>
    console.error("[BLM] chart render failed:", err));
}

function baseChartOpts(yLabel) {
  return {
    responsive: true, maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { labels: { color: ChartColor.tick, font: { size: 10, family: "monospace" } } },
      tooltip: { backgroundColor: "#0d1420", borderColor: "#26374d", borderWidth: 1,
        titleColor: "#eaf2ff", bodyColor: "#d7e1f0" },
    },
    scales: {
      x: { ticks: { color: ChartColor.tick, maxTicksLimit: 8, font: { size: 9, family: "monospace" } },
        grid: { color: ChartColor.grid } },
      y: { ticks: { color: ChartColor.tick, font: { size: 9, family: "monospace" } },
        grid: { color: ChartColor.grid }, title: { display: true, text: yLabel, color: ChartColor.tick, font: { size: 9 } } },
    },
  };
}

// Draws the Z=0 reference line across the plot area — the neutral gap
// marker that makes signed Z movement (toward/away from zero) readable.
// An inline plugin (not grid ticks) so the zero line is always drawn,
// regardless of tick generation.
const zZeroLine = {
  id: "zZeroLine",
  afterDatasetsDraw(chart) {
    const s = chart.scales.y; // Z panel uses its own default y scale
    if (!s) return;
    const y = s.getPixelForValue(0);
    if (!Number.isFinite(y)) return;
    const { left, right } = chart.chartArea;
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = "rgba(167,139,250,.5)";
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(right, y);
    ctx.stroke();
    ctx.restore();
  },
};

async function renderModalCharts(g) {
  // INVALID games render no charts — gated (null) by renderModal.
  if (!g) {
    for (const k in state.modalCharts) {
      if (state.modalCharts[k]) state.modalCharts[k].destroy();
    }
    state.modalCharts = {};
    return;
  }
  if (!hasChart()) {
    document.querySelectorAll(".m-chart canvas").forEach((c) => {
      c.replaceWith(Object.assign(document.createElement("div"),
        { textContent: "Chart.js unavailable — CDN blocked" }));
    });
    return;
  }
  // ONE primary chart: SCORE vs LIVE LINE — MARKET MOVEMENT.
  // Authoritative BLM series only (deviation endpoint = stored residuals
  // + benchmark + Z; market_observations history = observed lines with
  // exact timestamps).  No frontend Z computation, no interpolation,
  // no fabricated lines: gaps stay gaps (spanGaps:false), stale/missing
  // lines never substitute for live ones.
  const h = g.history || [];
  // Score-vs-line readout computed IMMEDIATELY from the detail payload's
  // history — BEFORE the async series fetches — so the header shows the
  // gap the moment the modal opens (the /deviation series fetch can take
  // seconds under load; the readout must never wait on it).  The same
  // values are used by the chart build below.  Current combined score
  // minus the freshest observed line: pure arithmetic, no model value
  // enters this readout.
  const scoreSeries = h.map((s) => s.combined).filter((v) => v != null);
  const curScore = scoreSeries.length ? scoreSeries[scoreSeries.length - 1] : null;
  const lineSeriesVals = h.map((s) => s.total_line).filter((v) => v != null);
  const curLine = lineSeriesVals.length
    ? lineSeriesVals[lineSeriesVals.length - 1] : null;
  const gapVal = (curScore != null && curLine != null)
    ? Number((curScore - curLine).toFixed(1)) : null;
  state.modalGapText = (curScore != null && curLine != null)
    ? `SCORE ${curScore} − LINE ${num(curLine, 1)} = ${gapVal > 0 ? "+" : ""}${num(gapVal, 1)}`
    : "SCORE − LINE = –";
  const gapEl = document.getElementById("gapReadout");
  if (gapEl) gapEl.textContent = state.modalGapText;
  let pz = null, lines = null;
  // Both series are fetched in parallel — the PACE Z store (authoritative
  // pace-benchmark layer) and the observed market-line history are
  // independent authoritative sources.
  const [pzRes, linesRes] = await Promise.allSettled([
    fetch(API_GAME_PACE_Z(g.game_id)).then((r) => (r.ok ? r.json() : null)),
    fetch(API_GAME_LINES(g.game_id)).then((r) => (r.ok ? r.json() : null)),
  ]);
  if (pzRes.status === "fulfilled") pz = pzRes.value;
  if (linesRes.status === "fulfilled") lines = linesRes.value;
  // a failed fetch is not fatal — the chart renders from history alone
  if (state.modalGameId !== g.game_id) return; // modal switched games mid-fetch
  const series = (pz && pz.series) || [];
  const lineObs = (lines && lines.observations) || [];
  // epoch-ms x placement; unparseable timestamps are dropped (never guessed)
  const tMs = (iso) => { const ms = Date.parse(iso); return Number.isFinite(ms) ? ms : null; };
  // Live-line series: every valid observed line at its exact observation
  // timestamp — movement is drawn only from real observations, never
  // interpolated.  A genuine observation gap (adjacent=0) becomes an
  // explicit null-y break placed at the PREVIOUS observation's own
  // timestamp: the line stops at the last real value and restarts at the
  // next one — no timestamp and no line value is ever manufactured.
  const lineData = [];
  let prevT = null;
  for (const o of lineObs) {
    const x = tMs(o.captured_at);
    if (x == null || o.line_value == null) continue;
    // A non-adjacent observation is an explicit break.  Mid-series it is
    // placed at the PREVIOUS observation's own timestamp (the line stops
    // there and restarts at the next real value); at series start it
    // marks that no observation exists before this point.  Nothing is
    // manufactured: no timestamp and no line value is ever invented.
    if (!o.adjacent) lineData.push({ x: prevT != null ? prevT : x, y: null });
    lineData.push({ x, y: o.line_value });
    prevT = x;
  }
  // PACE Z-SCORE panel series — the authoritative STORED pace z from the
  // benchmark layer (GET /pace-z): actual pace vs the strictly-prior
  // historical population at (provider, competition, period, progress).
  // The frontend never computes z and never derives it from the live line
  // or the score-vs-line gap.  EVERY kept point is preceded by an explicit
  // null break when a previous point exists: non-adjacent stored
  // observations render as visible gaps — nothing is carried forward,
  // interpolated, or substituted.
  const zData = [];
  {
    let zPrevT = null;
    for (const s of series) {
      const x = tMs(s.captured_at);
      if (x == null || s.z == null) continue;
      if (zPrevT != null && x !== zPrevT) zData.push({ x: zPrevT, y: null });
      zData.push({ x, y: s.z });
      zPrevT = x;
    }
  }
  const lastZ = series.map((s) => s.z).filter((z) => z != null).pop();
  const zTxt = (lastZ != null
    ? `z = ${lastZ > 0 ? "+" : ""}${num(lastZ, 2)}`
    : "z = n/a");
  // benchmark provenance — read straight from the authoritative payload
  // (sample size, historical mean/σ, competition partition); never invented
  const zMeta = (pz && pz.n != null)
    ? `N=${pz.n}` +
      (pz.mean_pace != null ? ` · μ=${num(pz.mean_pace, 3)} pts/min` : "") +
      (pz.std_pace != null ? ` · σ=${num(pz.std_pace, 3)}` : "") +
      (pz.provider && pz.competition ? ` · ${pz.provider}/${pz.competition}` : "")
    : null;
  const zReadout = zMeta ? `${zTxt} · ${zMeta}` : zTxt;
  // Score-vs-line readout cache (lives across re-renders between polls,
  // like the Z cache): the plain arithmetic gap at the CURRENT state —
  // current combined score minus the observed live line.  The newest
  // (gap readout already computed synchronously pre-fetch above — the
  // history snapshot does not change mid-render)
  const opts = baseChartOpts("Points / line");
  // Wall-clock x axis WITHOUT the Chart.js time scale: no date adapter is
  // loaded (CDN core only), so a `type:"time"` scale would throw at
  // construction.  A linear epoch-ms axis with clock-formatted ticks
  // shows the exact same observation timestamps with zero extra deps.
  opts.interaction = { mode: "nearest", intersect: false };
  opts.scales.x = {
    type: "linear",
    ticks: { color: ChartColor.tick, maxTicksLimit: 8, font: { size: 9, family: "monospace" },
      callback: (v) => fmtTime(new Date(v).toISOString()) },
    grid: { color: ChartColor.grid },
  };
  const mk = (id) => document.getElementById(id);
  const scoreData = h.map((s) => { const x = tMs(s.t); return x == null ? null : { x, y: s.combined }; });
  // Z-SCORE panel options — its OWN compact chart (never a hidden dataset
  // on the score/line chart): symmetric Y around the zero reference,
  // sign-explicit ticks, bounds from the stored z values (presentation
  // range only; no z math here), same game-time axis as the primary
  // chart so the two measurements read side by side.
  const zAbs = zData.map((p) => Math.abs(p.y)).filter(Number.isFinite);
  const zBound = Math.max(1, (zAbs.length ? Math.max(...zAbs) : 1) * 1.1);
  const zOpts = {
    responsive: true, maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "nearest", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: { backgroundColor: "#0d1420", borderColor: "#26374d", borderWidth: 1,
        titleColor: "#eaf2ff", bodyColor: "#d7e1f0" },
    },
    scales: {
      x: { type: "linear",
        ticks: { color: ChartColor.tick, maxTicksLimit: 8, font: { size: 9, family: "monospace" },
          callback: (v) => fmtTime(new Date(v).toISOString()) },
        grid: { color: ChartColor.grid } },
      y: { min: -zBound, max: zBound,
        title: { display: true, text: "PACE Z", color: "#a78bfa", font: { size: 9 } },
        ticks: { color: "#a78bfa", font: { size: 9, family: "monospace" }, maxTicksLimit: 5,
          callback: (v) => (v > 0 ? "+" : "") + Number(v).toFixed(1) },
        grid: { color: ChartColor.grid },
        border: { display: false } },
    },
  };
  // Fast path — renderModal re-attaches the same canvas node, so a live
  // chart for this game is updated in place: the plotted series refresh
  // and the chart never blinks out during the fetch window.
  const prev = state.modalCharts.total;
  if (prev && prev.$gameId === g.game_id && prev.canvas && prev.canvas.isConnected) {
    prev.data.datasets[0].data = scoreData;
    prev.data.datasets[1].data = lineData;
    prev.update("none");
    const prevZ = state.modalCharts.z;
    if (prevZ && prevZ.$gameId === g.game_id && prevZ.canvas && prevZ.canvas.isConnected) {
      prevZ.data.datasets[0].data = zData;
      prevZ.options.scales.y.min = -zBound;
      prevZ.options.scales.y.max = zBound;
      prevZ.update("none");
    } else {
      // Z canvas lost (first render or node replaced) — rebuild it from
      // the already-fetched series; never leave the panel dark.
      if (prevZ) { try { prevZ.destroy(); } catch (_) {} }
      const zCanvasNow = mk("mcZ");
      if (zCanvasNow) {
        const staleZ = Chart.getChart(zCanvasNow);
        if (staleZ) staleZ.destroy();
        state.modalCharts.z = new Chart(zCanvasNow, {
          type: "line",
          plugins: [zZeroLine],
          data: { datasets: [
            { label: "PACE Z (stored benchmark)", data: zData,
              borderColor: "#a78bfa", borderWidth: 2, pointRadius: 2,
              pointHoverRadius: 4, tension: .15, spanGaps: false },
          ]},
          options: zOpts,
        });
        state.modalCharts.z.$gameId = g.game_id;
      }
    }
    const zEl = document.getElementById("zPanelReadout");
    if (zEl) zEl.textContent = zReadout;
    state.modalZText = zTxt; // cache across re-renders, like the gap readout
    prev.$devCount = series.length;
    return;
  }
  for (const k in state.modalCharts) {
    if (state.modalCharts[k]) state.modalCharts[k].destroy();
  }
  state.modalCharts = {};
  const canvas = mk("mcTotal");
  const stale = Chart.getChart(canvas); // never build over a live instance
  if (stale) stale.destroy();
  state.modalCharts.total = new Chart(canvas, {
    type: "line",
    data: { datasets: [
      // A · actual cumulative score (observed)
      { label: "Actual score (combined)", data: scoreData,
        borderColor: "#eaf2ff", backgroundColor: "rgba(234,242,255,.06)",
        fill: false, pointRadius: 0, tension: .25, spanGaps: false },
      // B · every valid observed live line, exact timestamps, movement as-is
      { label: "Live O/U line", data: lineData, borderColor: ChartColor.market,
        pointRadius: 1.5, pointHoverRadius: 3, tension: 0, spanGaps: false },
    ]},
    options: opts,
  });
  state.modalCharts.total.$gameId = g.game_id;
  // Z-SCORE panel — its own visible chart immediately below the primary
  // one: authoritative stored z observations on the same game-time axis,
  // zero reference drawn by the zZeroLine plugin, missing values as gaps.
  const zCanvas = mk("mcZ");
  const staleZ = Chart.getChart(zCanvas); // never build over a live instance
  if (staleZ) staleZ.destroy();
  state.modalCharts.z = new Chart(zCanvas, {
    type: "line",
    plugins: [zZeroLine],
    data: { datasets: [
      { label: "PACE Z (stored benchmark)", data: zData,
        borderColor: "#a78bfa", borderWidth: 2, pointRadius: 2,
        pointHoverRadius: 4, tension: .15, spanGaps: false },
    ]},
    options: zOpts,
  });
  state.modalCharts.z.$gameId = g.game_id;
  const zEl = document.getElementById("zPanelReadout");
  if (zEl) zEl.textContent = zReadout;
  state.modalZText = zReadout;
  state.modalCharts.total.$devCount = series.length;
}

async function loadModalDetail(gameId) {
  try {
    const resp = await fetch(API_GAME(gameId));
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const d = await resp.json();
    if (state.modalGameId !== gameId || $("modalBackdrop").hidden) return;
    state.modalDetailOk = true;
    renderModal(d);
  } catch (err) {
    if (state.modalGameId === gameId) state.modalDetailOk = false;
    // keep the cached live render — never show an empty overlay
    const box = $("modalBody").querySelector(".tech-raw");
    if (!box) $("modalBody").insertAdjacentHTML("beforeend",
      `<details class="tech-raw"><summary>Technical / Raw Data</summary><pre>${esc("Detail fetch failed: " + err.message)}</pre></details>`);
  }
}

/* ── Polling ─────────────────────────────────────────────── */

async function refresh() {
  try {
    const resp = await fetch(API_LIVE);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const payload = await resp.json();
    state.lastPayload = payload;
    state.games = payload.games || [];
    renderStatus(payload);
    renderSummary(payload);
    renderCards(payload);
    // The open modal's game is absent from the live payload — but that
    // list is not the authority on existence: the detail endpoint serves
    // ended/stale games too.  Keep the modal and its chart alive by
    // refreshing from the detail source; close cleanly only once the
    // detail fetch itself has failed (game truly gone).
    if (state.modalGameId && !state.games.some((g) => g.game_id === state.modalGameId)) {
      if (state.modalDetailOk === false) closeModal();
      else if (!$("modalBackdrop").hidden) loadModalDetail(state.modalGameId);
    } else if (state.modalGameId && !$("modalBackdrop").hidden) {
      loadModalDetail(state.modalGameId);
    }
  } catch (err) {
    const livePill = $("livePill");
    livePill.className = "pill live-pill bad";
    $("liveLabel").textContent = "OFFLINE";
    $("collectorPill").textContent = `api error: ${esc(err.message)}`;
    $("collectorPill").style.color = "var(--red)";
  }
}

/* ── Wiring ──────────────────────────────────────────────── */

function syncLiveToggle(total, visible) {
  const btn = $("liveToggle");
  if (!btn) return;
  const on = state.hideNonLive;
  btn.textContent = on ? "SHOWING LIVE" : "SHOW ALL";
  btn.classList.toggle("active", on);
  btn.dataset.livefilter = on ? "live" : "all";
  const cnt = $("liveCount");
  if (cnt) cnt.textContent = on ? `showing ${visible} live · hidden ${total - visible}` : "";
  const sub = $("liveHeadSub");
  if (sub) sub.textContent = on
    ? "clean post-epoch observations only · live games shown"
    : "clean post-epoch observations only · ended/stale included (SHOW ALL)";
}

$("filters").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".filter");
  if (!btn) return;
  // M007-M6 live/stale toggle (independent of classification filter)
  if (btn.dataset.livefilter !== undefined) {
    state.hideNonLive = !state.hideNonLive;
    renderCards(state.lastPayload || { games: state.games });
    return;
  }
  document.querySelectorAll(".filter").forEach((f) => f.classList.remove("active"));
  btn.classList.add("active");
  state.filter = btn.dataset.filter || "";
  renderCards(state.lastPayload || { games: state.games });
});

$("modalClose").addEventListener("click", closeModal);
$("modalBackdrop").addEventListener("click", (ev) => {
  if (ev.target === $("modalBackdrop")) closeModal();
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && !$("modalBackdrop").hidden) closeModal();
});

/* ── HISTORICAL / RESEARCH — top-level collapse ─────────────────
   One control for the whole research/audit block.  Collapsed by
   default (persisted); each inner section keeps its own independent
   toggle and default-collapsed body.  Nothing here changes data — it
   only controls visibility of research/audit material. */
function applyAuditPref() {
  const body = $("auditBody");
  const btn = $("auditToggle");
  if (!body || !btn) return;
  const collapsed = prefGet(PREF.HISTORICAL, true);
  body.hidden = collapsed;
  btn.classList.toggle("open", !collapsed);
  btn.setAttribute("aria-expanded", String(!collapsed));
}
if ($("auditToggle")) {
  $("auditToggle").addEventListener("click", () => {
    const body = $("auditBody");
    const open = body.hidden; // true → currently collapsed → expand
    body.hidden = !open;
    $("auditToggle").classList.toggle("open", open);
    $("auditToggle").setAttribute("aria-expanded", String(open));
    prefSet(PREF.HISTORICAL, !open); // stored as "collapsed"
  });
}
applyAuditPref();

refresh();
setInterval(refresh, POLL_MS);

/* ═══════════════════════════════════════════════════════════════
   GAME-LEVEL SCORECARD — progressive Market vs Fair per CLEAN
   COMPLETED game (frontend-only).

   Data: candidates come from the /api/v4/live payload already in
   state.games (a game is eligible when status=="ended" and
   quality_status!=="INVALID" — the backend game_quality gate is the
   single source of truth, never re-derived here).  Progressive rows
   come from /api/v4/game/{id}.market_vs_fair[] — the frozen
   per-checkpoint record (checkpoint_pct 10→100, live_market_line,
   blm_fair_value, signed market_vs_fair, market_status, outcome,
   actual_final_total).  Nothing is computed that changes methodology:
   Market−Fair, direction, freshness and settlement are displayed as
   the payload reports them; MISSING lines stay MISSING (never
   substituted); Under/Over outcomes remain separate directional
   results and are never compared as competing predictions.
   ═══════════════════════════════════════════════════════════════ */
let gsTimer = null;
const gs = {
  details: new Map(),   // game_id -> {detail, fetched_at}
  charts: new Map(),    // game_id -> Chart (per-game Market−Fair progression spark)
  loaded: 0,            // candidates whose detail fetch has resolved this pass
  need: 0,
};
const GS_DETAIL_TTL_ROWS = 300e3;   // 5 min — frozen rows are immutable
const GS_DETAIL_TTL_NOROWS = 120e3; // 2 min — may finalize (rows appear at finalize batch)

function gsCandidates() {
  // Clean-completed candidates: ended + not flagged INVALID by the backend gate.
  return (state.games || []).filter((g) => g.status === "ended" && g.quality_status !== "INVALID");
}

function gsNeedsFetch(gameId) {
  const d = gs.details.get(gameId);
  if (!d) return true;
  const hasRows = (d.detail.market_vs_fair || []).length > 0;
  const ttl = hasRows ? GS_DETAIL_TTL_ROWS : GS_DETAIL_TTL_NOROWS;
  return (Date.now() - d.fetched_at) > ttl;
}

async function gsFetchDetail(gameId) {
  const resp = await fetch(API_GAME(gameId));
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

function gsCache(gameId, detail) {
  gs.details.set(gameId, { detail, fetched_at: Date.now() });
}

function gsDropGame(gameId) {
  const ch = gs.charts.get(gameId);
  if (ch) { try { ch.destroy(); } catch (_) {} gs.charts.delete(gameId); }
  gs.details.delete(gameId);
  const el = document.getElementById("gs-game-" + CSS.escape(gameId));
  if (el) el.remove();
}

function gsOutcomeShort(o) {
  if (o === "UNDER_WIN") return "U WIN";
  if (o === "OVER_WIN") return "O WIN";
  if (o === "UNDER_LOSS") return "U LOSS";
  if (o === "OVER_LOSS") return "O LOSS";
  return o || "–";
}

function gsOutcomeCls(o) {
  return o && o.endsWith("_WIN") ? "win" : o && o.endsWith("_LOSS") ? "loss" : "void";
}

function gsDiffCell(r) {
  const mf = r.market_vs_fair;
  if (mf == null || r.live_market_line == null) {
    return `<td class="gs-diff zero">–<span class="gs-dir">no line</span></td>`;
  }
  const cls = mf > 0 ? "pos" : mf < 0 ? "neg" : "zero";
  const dir = mf > 0 ? "▲ MKT&gt;FAIR" : mf < 0 ? "▼ MKT&lt;FAIR" : "= FAIR";
  return `<td class="gs-diff ${cls}">${sig(mf)}<span class="gs-dir">${dir}</span></td>`;
}

function gsRowHTML(r, classification) {
  const dim = r.live_market_line == null ? " gs-row-dim" : "";
  const out = r.outcome ? `<span class="gs-out ${gsOutcomeCls(r.outcome)}">${esc(gsOutcomeShort(r.outcome))}</span>` : "–";
  // Terminal exclusion (directive): a terminal checkpoint keeps its
  // settlement outcome but is explicitly excluded from predictive
  // validation — settlement display and research eligibility stay distinct.
  const pflag = r.terminal
    ? ` <span class="st st-stale" title="${esc(r.predictive_validation || "PREDICTIVE VALIDATION: EXCLUDED")} · ${esc(r.exclusion_reason || "TERMINAL CHECKPOINT")}">SETTLEMENT ONLY</span>`
    : "";
  return `<tr class="${dim}">
    <td class="gs-pct">${obsPctLabel(r)}${cpStateHTML(r, classification)}</td>
    <td class="sc-num">${num(r.live_market_line, 1)}</td>
    <td class="sc-num">${num(r.blm_fair_value, 1)}</td>
    ${gsDiffCell(r)}
    <td>${r.market_status == null ? "–" : `<span class="st ${evStatusCls(r.market_status)}">${esc(r.market_status)}</span>`}</td>
    <td>${out}${pflag}</td>
  </tr>`;
}

function gsTableBody(rows, classification) {
  return rows.map((r) => gsRowHTML(r, classification)).join("");
}

function gsChartHTML(g, rows) {
  if (!hasChart() || rows.length < 2) return "";
  const diffs = rows.map((r) => r.market_vs_fair);
  if (diffs.every((v) => v == null)) return "";
  return `<div class="gs-chart"><canvas id="gs-spark-${CSS.escape(g.game_id)}"></canvas></div>`;
}

function gsRenderChart(gameId, rows) {
  const cv = document.getElementById("gs-spark-" + CSS.escape(gameId));
  if (!cv || !hasChart()) return;
  const old = gs.charts.get(gameId);
  if (old) { try { old.destroy(); } catch (_) {} }
  const labels = rows.map((r) => (r.checkpoint_pct != null ? r.checkpoint_pct + "%" : ""));
  const diff = rows.map((r) => r.market_vs_fair);
  const zero = rows.map(() => 0);
  const ch = new Chart(cv.getContext("2d"), {
    type: "line",
    data: { labels, datasets: [
      { label: "M−F", data: diff, borderColor: ChartColor.cyan || "#22d3ee",
        backgroundColor: "rgba(34,211,238,0.08)", fill: true, borderWidth: 1.5,
        pointRadius: 1.5, pointHoverRadius: 3, tension: 0.15, spanGaps: false },
      { label: "fair", data: zero, borderColor: "#46546b", borderWidth: 1,
        pointRadius: 0, borderDash: [3, 3], fill: false },
    ] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { enabled: true, callbacks: { title: (it) => labels[it[0].dataIndex], label: (it) => `M−F ${it.parsed.y}` } } },
      scales: { x: { display: false }, y: { display: false } },
    },
  });
  gs.charts.set(gameId, ch);
}

function gsGameHTML(g, detail) {
  const rows = (detail.market_vs_fair || []).slice();
  if (!rows.length) return "";
  const last = rows[rows.length - 1];
  const bearing = rows.filter((r) => r.live_market_line != null).length;
  const liveRows = rows.filter((r) => r.market_status === "LIVE").length;
  const mkt = detail.market || {};
  const open = (rows[0] && rows[0].opening_line) ?? mkt.opening_line;
  const close = mkt.closing_line ?? (last && last.closing_line);
  const actual = last && last.actual_final_total;
  const outcome = last && last.outcome;
  const pct = last ? obsPctLabel(last) : null;
  const endedAt = rows.length ? rows[rows.length - 1].checkpoint_timestamp : g.last_update;
  const resLine = outcome && actual != null && last.live_market_line != null
    ? `<span class="gs-res">RESULT <b>${esc(gsOutcomeShort(outcome))}</b> · actual <b>${num(actual, 0)}</b> vs market <b>${num(last.live_market_line, 1)}</b> @${pct} · M−F <b class="${last.market_vs_fair > 0 ? "pos" : last.market_vs_fair < 0 ? "neg" : ""}">${sig(last.market_vs_fair)}</b></span>`
    : `<span class="gs-res">RESULT <b>${esc(gsOutcomeShort(outcome))}</b> @${pct}</span>`;
  return `<div class="gs-head">
      <span class="cat-badge ${esc(g.classification || "UNKNOWN")}">${esc(g.classification || "UNKNOWN")}</span>
      <span class="pill gs-clean" title="ended + not flagged INVALID by the backend quality gate">CLEAN COMPLETED</span>
      <span class="gs-teams">${esc(g.home_team)} vs ${esc(g.away_team)}
        <span class="gs-gid">${esc(g.game_id)} · ended ${fmtTime(endedAt)}</span></span>
    </div>
    <div class="gs-body">
      <table class="sc-table gs-table">
        <thead><tr><th>Prog</th><th>Market</th><th>Fair</th><th>M−F</th><th>Fresh</th><th>Settlement</th></tr></thead>
        <tbody>${gsTableBody(rows, g.classification)}</tbody>
      </table>
      ${gsChartHTML(g, rows)}
      <div class="gs-foot">
        <span>line-bearing <b>${bearing}/${rows.length}</b></span>
        <span>LIVE <b>${liveRows}</b></span>
        ${open != null ? `<span>open <b>${num(open, 1)}</b></span>` : ""}
        ${close != null ? `<span>close <b>${num(close, 1)}</b></span>` : ""}
        ${resLine}
      </div>
    </div>`;
}

function gsMeta(cand, included, noRecord, pending) {
  const invalid = (state.games || []).filter((g) => g.status === "ended" && g.quality_status === "INVALID").length;
  const live = (state.games || []).filter((g) => g.status !== "ended").length;
  const parts = [];
  parts.push(`<span class="pill gs-clean">CLEAN COMPLETED · ${included}</span>`);
  parts.push(`<span class="pill muted">ended candidates ${cand.length}</span>`);
  parts.push(`<span class="pill muted">no frozen record / finalizing ${noRecord}</span>`);
  if (pending) parts.push(`<span class="pill muted">loading ${pending}…</span>`);
  parts.push(`<span class="pill muted" title="excluded by the backend quality gate (game_quality INVALID)">INVALID excluded ${invalid}</span>`);
  parts.push(`<span class="pill muted" title="not completed — excluded from this scorecard">live ${live}</span>`);
  return parts.join("");
}

function gsRender(cand) {
  const grid = $("gsGrid");
  if (!grid) return;
  const meta = $("gsMeta");
  const done = [];
  let included = 0, noRecord = 0, pending = 0;
  for (const g of cand) {
    const d = gs.details.get(g.game_id);
    if (!d) { pending++; continue; }
    const rows = (d.detail.market_vs_fair || []).filter((r) => r.checkpoint_pct != null);
    if (!rows.length) { noRecord++; continue; }
    included++;
    let el = document.getElementById("gs-game-" + CSS.escape(g.game_id));
    if (!el) {
      el = document.createElement("div");
      el.id = "gs-game-" + CSS.escape(g.game_id);
      el.className = "sc-block gs-game";
      grid.appendChild(el);
    }
    const html = gsGameHTML(g, d.detail);
    if (el.dataset.sig !== html) {
      el.dataset.sig = html;
      el.innerHTML = html;
      gsRenderChart(g.game_id, rows);
    } else {
      // chart canvas survives only if innerHTML untouched — already rendered
    }
    done.push(g);
  }
  // keep DOM order aligned with the (recency-sorted) candidate order
  for (const g of done) {
    const el = document.getElementById("gs-game-" + CSS.escape(g.game_id));
    if (el) grid.appendChild(el);
  }
  // The static "loading…" placeholder in index.html is only meaningful before the
  // first render — drop it once we own the grid (blocks append incrementally).
  const ph = grid.querySelector(":scope > .empty");
  if (ph) ph.remove();
  if (!included && !pending && !noRecord && !cand.length) {
    grid.innerHTML = `<div class="empty">No clean completed games in the current window.</div>`;
  } else if (!included && !pending && noRecord > 0) {
    grid.innerHTML = `<div class="empty">Ended games in this window are still finalizing frozen per-checkpoint records — none to show yet.</div>`;
  }
  if (meta) meta.innerHTML = gsMeta(cand, included, noRecord, pending);
}

async function gsRefresh() {
  const cand = gsCandidates();
  // prune games that left the live window (bounded to the recent set)
  const ids = new Set(cand.map((g) => g.game_id));
  for (const id of [...gs.details.keys()]) if (!ids.has(id)) gsDropGame(id);
  const need = cand.filter((g) => gsNeedsFetch(g.game_id));
  gs.need = need.length;
  gs.loaded = 0;
  gsRender(cand);
  while (need.length) {
    const batch = need.splice(0, 6);
    await Promise.all(batch.map(async (g) => {
      try {
        const detail = await gsFetchDetail(g.game_id);
        gsCache(g.game_id, detail);
      } catch (err) {
        // transient — drop nothing; next refresh retries
        console.error("game scorecard detail failed", g.game_id, err.message);
      } finally {
        gs.loaded++;
        gsRender(cand);
      }
    }));
    if (need.length) await new Promise((r) => setTimeout(r, 60));
  }
  gsRender(cand);
}

$("gsToggle").addEventListener("click", () => {
  const body = $("gsBody");
  const open = body.hidden;
  body.hidden = !open;
  $("gsToggle").setAttribute("aria-expanded", String(open));
  $("gsToggle").classList.toggle("open", open);
  if (open) {
    gsRefresh();
    if (!gsTimer) gsTimer = setInterval(gsRefresh, 60000);
  } else if (gsTimer) {
    clearInterval(gsTimer);
    gsTimer = null;
  }
});

/* ═══════════════════════════════════════════════════════════════
   RESEARCH / CALIBRATION — MARKET ↔ TRAJECTORY DEVIATION (Phase 2)

   Empirical deviation instrument, research-only (never part of the live
   predictive surface).  Charts per selected clean game:
     A  market implied total  vs  observational (pace) projected total
     B  residual = live line − projected trajectory, over game time
     C  z-score = residual standardized by its bucket's PRIOR residuals
   Maturity (EXPLORATORY / PROVISIONAL / ESTABLISHED) is an operational
   label on the bucket size N — shown but never implied as statistical
   significance.  No Over/Under/edge/probability/recommendation mapping
   exists anywhere in this surface.
   ═══════════════════════════════════════════════════════════════ */
const API_GAME_DEV = (id) => `/api/v4/game/${encodeURIComponent(id)}/deviation`;
const API_GAME_PACE_Z = (id) => `/api/v4/game/${encodeURIComponent(id)}/pace-z`;
let devTimer = null;
const devCharts = {};
let devPickedGameId = null;

function devCleanGames() {
  return (state.games || [])
    .filter((g) => g.quality_status !== "INVALID")
    .sort((a, b) => String(b.last_update || "").localeCompare(String(a.last_update || "")));
}

function devFillPicker() {
  const sel = $("devGame");
  if (!sel) return;
  const games = devCleanGames();
  const keep = devPickedGameId && [...sel.options].some((o) => o.value === devPickedGameId)
    ? devPickedGameId : null;
  const cur = keep || (games[0] && games[0].game_id) || "";
  sel.innerHTML = games.length
    ? games.map((g) => `<option value="${esc(g.game_id)}">`
        + `${esc(g.home_team)} vs ${esc(g.away_team)} · ${esc(g.game_id)} · `
        + `${g.live ? "LIVE" : g.status === "ended" ? "ENDED" : "STALE"}`
        + `</option>`).join("")
    : `<option value="">no clean games in window</option>`;
  if (cur && [...sel.options].some((o) => o.value === cur)) sel.value = cur;
  devPickedGameId = sel.value;
  return sel.value;
}

function devDestroy() {
  for (const k in devCharts) {
    if (devCharts[k]) { try { devCharts[k].destroy(); } catch (_) {} }
  }
  for (const k in devCharts) delete devCharts[k];
}

function devDraw(canvasId, labels, datasets, yLabel) {
  const cv = document.getElementById(canvasId);
  if (!cv) return;
  const old = devCharts[canvasId];
  if (old) { try { old.destroy(); } catch (_) {} }
  if (!hasChart()) {
    cv.replaceWith(Object.assign(document.createElement("div"),
      { textContent: "Chart.js unavailable — CDN blocked" }));
    return;
  }
  const options = baseChartOpts(yLabel);
  options.interaction = { mode: "index", intersect: false };
  options.scales.x.maxTicksLimit = 12;
  devCharts[canvasId] = new Chart(cv.getContext("2d"), {
    type: "line",
    data: { labels, datasets: datasets.map((d) => Object.assign({
      pointRadius: 0, pointHoverRadius: 3, tension: 0.15, spanGaps: true,
    }, d)) },
    options,
  });
}

/* ── CHECKPOINTS — ALL CLEAN OBSERVATIONS table (research) ─────
   Every deviation row rendered with its full stored field set.  The
   table body is populated even when the section is collapsed (collapse
   only hides it) so no checkpoint data is ever "removed". */
function devRenderCheckpoints(d) {
  const body = $("devCheckpointsBody");
  if (!body) return;
  const series = d.series || [];
  if (!series.length) {
    body.innerHTML = `<tr><td colspan="15" class="muted">No clean checkpoints for this game yet.</td></tr>`;
    return;
  }
  const score = (s) => {
    if (s.home_score != null && s.away_score != null) {
      return `${num(s.home_score, 0)}–${num(s.away_score, 0)}`;
    }
    return s.current_total_points != null ? `Σ ${num(s.current_total_points, 0)}` : "–";
  };
  body.innerHTML = series.map((s) => {
    const z = s.z_score != null ? num(s.z_score, 2) : "–";
    const st = s.benchmark_status || "–";
    const stCls = st === "ESTABLISHED" ? "estb" : st === "PROVISIONAL" ? "prov" : "expl";
    return `<tr>`
      + `<td>${s.elapsed_game_minutes != null ? num(s.elapsed_game_minutes, 1) + "m" : fmtTime(s.captured_at)}</td>`
      + `<td>${esc(s.clock ?? "–")}</td>`
      + `<td>${esc(s.period_label ?? "–")}</td>`
      + `<td>${esc(score(s))}</td>`
      + `<td>${s.live_total_line != null ? num(s.live_total_line, 1) : "–"}</td>`
      + `<td>${s.actual_pts_per_min != null ? num(s.actual_pts_per_min, 2) : "–"}</td>`
      + `<td>${s.required_pts_per_min != null ? num(s.required_pts_per_min, 2) : "–"}</td>`
      + `<td>${s.pace_gap != null ? sig(s.pace_gap) : "–"}</td>`
      + `<td>${s.projected_final_total != null ? num(s.projected_final_total, 1) : "–"}</td>`
      + `<td>${s.market_trajectory_residual != null ? sig(s.market_trajectory_residual) : "–"}</td>`
      + `<td>${s.benchmark_n != null ? s.benchmark_n : "–"}</td>`
      + `<td>${s.benchmark_mean != null ? num(s.benchmark_mean, 2) : "–"}</td>`
      + `<td>${s.benchmark_std != null ? num(s.benchmark_std, 2) : "–"}</td>`
      + `<td>${z}</td>`
      + `<td><span class="${stCls}">${esc(st)}</span></td>`
      + `</tr>`;
  }).join("");
}

// constant reference line (0 / ±1 / ±2 …) for residual & z plots
const devConstLine = (labels, val, color, dash, width, label) => ({
  label, data: labels.map(() => val), borderColor: color,
  borderDash: dash || [3, 3], borderWidth: width || 1, fill: false,
  pointRadius: 0, tension: 0,
});

function devRender(d) {
  const ann = $("devAnn");
  const buckets = $("devBuckets");
  const series = d.series || [];
  devRenderCheckpoints(d);
  const labels = series.map((s) =>
    s.elapsed_game_minutes != null ? num(s.elapsed_game_minutes, 1) + "m" : fmtTime(s.captured_at));
  const col = (k) => series.map((s) => (s[k] != null ? s[k] : null));
  const market = col("live_total_line");
  const traj = col("projected_final_total");
  const resid = col("market_trajectory_residual");
  const z = col("z_score");

  if (!series.length) {
    devDestroy();
    ann.innerHTML = `<span class="muted">No deviation rows yet for this game — residuals need VALID clean observations carrying a live line AND a projected trajectory.</span>`;
    if (buckets) buckets.innerHTML = "";
    return;
  }

  // A · market implied total vs observational trajectory over game time
  devDraw("devChartA", labels, [
    { label: "Market implied total", data: market, borderColor: "#fbbf24",
      borderDash: [6, 4], fill: false },
    { label: "Projected trajectory (pace)", data: traj, borderColor: "#34d399",
      backgroundColor: "rgba(52,211,153,.06)", fill: true },
  ], "Implied final total");
  // B · signed market − trajectory residual over game time
  devDraw("devChartB", labels, [
    devConstLine(labels, 0, "#46546b", [2, 2], 1, "zero"),
    { label: "Market − trajectory residual", data: resid, borderColor: "#22d3ee",
      backgroundColor: "rgba(34,211,238,.08)", fill: true },
  ], "residual = live line − projected total");
  // C · z-score over game time with ±1 / ±2 reference bands
  devDraw("devChartC", labels, [
    devConstLine(labels, 0, "#46546b", [2, 2], 1, "z=0"),
    devConstLine(labels, 1, "#6b7a90", [2, 2], 0.7, "+1σ"),
    devConstLine(labels, -1, "#6b7a90", [2, 2], 0.7, "−1σ"),
    devConstLine(labels, 2, "#8b9ab0", [4, 3], 0.7, "+2σ"),
    devConstLine(labels, -2, "#8b9ab0", [4, 3], 0.7, "−2σ"),
    { label: "Z-score", data: z, borderColor: "#eaf2ff",
      backgroundColor: "rgba(234,242,255,.08)", fill: true },
  ], "Z-score (vs prior residuals in bucket)");

  // annotation — the current observation's z with its stored bucket snapshot
  const last = series[series.length - 1];
  const zTxt = last.z_score != null
    ? `<b>z = ${num(last.z_score, 2)}</b>` : `<b>z = n/a</b>`;
  const statusCls = last.benchmark_status === "ESTABLISHED" ? "estb"
    : last.benchmark_status === "PROVISIONAL" ? "prov" : "expl";
  ann.innerHTML =
    `<span style="margin-right:14px">${zTxt}`
    + ` <span class="muted">· N = ${last.benchmark_n ?? "–"} prior residuals in bucket</span></span>`
    + `<span class="muted" style="margin-right:14px">μ = ${num(last.benchmark_mean, 2)}</span>`
    + `<span class="muted" style="margin-right:14px">σ = ${num(last.benchmark_std, 2)}</span>`
    + `<span style="margin-right:14px">STATUS = <b class="${statusCls}">${esc(last.benchmark_status || "–")}</b>`
    + ` <span class="muted" style="font-size:10px">(bucket maturity label, not significance)</span></span>`
    + `<span class="muted">· residual ${sig(last.market_trajectory_residual)} @ ${num(last.elapsed_game_minutes, 1)} min · ${fmtTime(last.captured_at)}</span>`
    + `<div class="muted" style="margin-top:4px;font-size:11px">z is descriptive — standardized residual vs the bucket's prior empirical distribution. No O/U / edge / probability mapping.</div>`;

  // benchmark maturity chips for this game's league buckets
  if (buckets) {
    buckets.innerHTML = (d.buckets || []).map((b) =>
      `<span class="pill muted" title="eligible residuals in this league+quarter bucket (N is bucket size, not total observations)">`
      + `${esc(b.benchmark_key)} · N=<b>${b.n}</b> · μ=${num(b.mean, 2)} · σ=${num(b.std, 2)} · `
      + `[${num(b.min, 1)}, ${num(b.max, 1)}] · <b>${esc(b.status)}</b></span>`
    ).join("") || `<span class="muted">no benchmark buckets yet</span>`;
  }
}

async function devLoad() {
  const sel = $("devGame");
  const gid = sel ? sel.value : "";
  const ann = $("devAnn");
  if (!gid) {
    devDestroy();
    if (ann) ann.innerHTML = `<span class="muted">Select a clean game to load its deviation research series.</span>`;
    return;
  }
  try {
    const resp = await fetch(API_GAME_DEV(gid));
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const d = await resp.json();
    devRender(d);
    $("devSub").textContent =
      `${d.classification || "—"} · ${d.total} residual rows · ${(d.buckets || []).length} benchmark buckets`;
  } catch (err) {
    devDestroy();
    if (ann) ann.innerHTML = `<span class="muted">Deviation data unavailable: ${esc(err.message)}</span>`;
  }
}

$("devToggle").addEventListener("click", () => {
  const body = $("devBody");
  const open = body.hidden;
  body.hidden = !open;
  $("devToggle").setAttribute("aria-expanded", String(open));
  $("devToggle").classList.toggle("open", open);
  if (open) {
    devFillPicker();
    devLoad();
    if (!devTimer) devTimer = setInterval(() => { devFillPicker(); devLoad(); }, 60000);
  } else if (devTimer) {
    clearInterval(devTimer);
    devTimer = null;
    devDestroy();
  }
});
if ($("devGame")) {
  $("devGame").addEventListener("change", devLoad);
}
if ($("devRefresh")) {
  $("devRefresh").addEventListener("click", () => { devFillPicker(); devLoad(); });
}
// Research subsections are collapsible, never removable: collapse only hides
// the section — every checkpoint row / chart stays in the DOM and the user's
// collapse preference persists across reloads.
bindCollapsible($("cpDetails"), PREF.CHECKPOINTS);
bindCollapsible($("mtDetails"), PREF.MARKET_TRAJ);
bindCollapsible($("dzDetails"), PREF.DEVIATION_Z);
bindCollapsible($("valDetails"), PREF.VALIDATION);
applyResearchPrefs();

function applyResearchPrefs() {
  // defaults: CHECKPOINTS collapsed (rows still rendered), the two chart
  // groups + validation open — detailed research one click away, never gone.
  const defs = [["cpDetails", PREF.CHECKPOINTS, true],
                ["mtDetails", PREF.MARKET_TRAJ, false],
                ["dzDetails", PREF.DEVIATION_Z, false],
                ["valDetails", PREF.VALIDATION, false]];
  for (const [id, key, dfltCollapsed] of defs) {
    const det = $(id);
    if (det) {
      det.open = !prefGet(key, dfltCollapsed);
      setSectionLabel(det);
    }
  }
}

/* ── VALIDATION — WHAT HAPPENED NEXT (retrospective research) ──────
   Read-only aggregate over the deviation dataset: does the market-
   vs-trajectory deviation relate to what subsequently happened?
   Research only — no predictive claim until out-of-sample validation. */
const API_VALIDATION = "/api/v4/deviation/validation";
const valCharts = {};

function valDestroy() {
  for (const k in valCharts) {
    if (valCharts[k]) { try { valCharts[k].destroy(); } catch (_) {} }
  }
  for (const k in valCharts) delete valCharts[k];
}

function valScatter(canvasId, points, xLabel, yLabel) {
  const cv = document.getElementById(canvasId);
  if (!cv) return;
  const old = valCharts[canvasId];
  if (old) { try { old.destroy(); } catch (_) {} }
  if (!hasChart()) {
    cv.replaceWith(Object.assign(document.createElement("div"),
      { textContent: "Chart.js unavailable — CDN blocked" }));
    return;
  }
  const options = baseChartOpts(yLabel);
  options.plugins.legend.display = false;
  options.scales.x.title = { display: true, text: xLabel, color: ChartColor.tick, font: { size: 9 } };
  valCharts[canvasId] = new Chart(cv.getContext("2d"), {
    type: "scatter",
    data: { datasets: [{ data: points.map((p) => ({ x: p[0], y: p[1] })),
      borderColor: "#22d3ee", backgroundColor: "rgba(34,211,238,.35)",
      pointRadius: 2, pointHoverRadius: 4, showLine: false }] },
    options,
  });
}

function valTable(title, head, rows) {
  if (!rows.length) return "";
  return `<div class="sc-block sc-wide" style="margin-bottom:10px">
    <h4>${esc(title)}</h4>
    <table class="sc-table">
      <tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>
      ${rows.join("")}
    </table>
  </div>`;
}

function valRender(d) {
  const ann = $("valAnn");
  const chips = $("valChips");
  if (!d || !d.n_gated_rows) {
    valDestroy();
    if (ann) ann.innerHTML = `<span class="muted">No gated deviation observations yet — validation appears once residuals accumulate.</span>`;
    if (chips) chips.innerHTML = "";
    $("valTables").innerHTML = "";
    return;
  }
  const cr = (c) => (c && c.n ? `r=${num(c.r, 3)} <span class="muted">(n=${c.n})</span>` : `<span class="muted">r=n/a</span>`);
  const corr = d.correlations || {};
  if (ann) {
    const tp = d.terminal_population || {};
    ann.innerHTML =
      `<b>gated rows ${d.n_gated_rows}</b> · subsequent outcome ${d.n_with_subsequent} · settlement ${d.n_with_settlement}`
      + ` · non-VALID excluded <b>${d.n_excluded_non_valid ?? 0}</b>`
      + (d.n_terminal_excluded != null
        ? ` · terminal excluded <b>${d.n_terminal_excluded}</b> · predictive-eligible <b>${d.n_predictive_eligible ?? d.n_gated_rows}</b> (clean obs: ${tp.clean_observations_total ?? "–"} total / ${tp.clean_observations_terminal ?? "–"} terminal)`
        : "")
      + `<div class="muted" style="margin-top:4px;font-size:11px">monotonicity: <b>${esc(d.monotonicity && d.monotonicity.assessment || "–")}</b> · retrospective only, not predictive · terminal checkpoints = settlement/audit only, never research evidence</div>`;
  }
  if (chips) {
    chips.innerHTML = [
      `<span class="pill muted">res→Δpace ${cr(corr.residual_vs_subsequent_pace)}</span>`,
      `<span class="pill muted">z→Δpace ${cr(corr.z_vs_subsequent_pace)}</span>`,
      `<span class="pill muted">res→settle err ${cr(corr.residual_vs_settlement)}</span>`,
      `<span class="pill muted">z→settle err ${cr(corr.z_vs_settlement)}</span>`,
      `<span class="pill muted">|res|→|Δpace| ${cr(corr.abs_residual_vs_abs_subseq_pace)}</span>`,
    ].join("");
  }
  const row = (b, ks) => `<tr>${ks.map((k) => `<td class="sc-num">${b[k] ?? "–"}</td>`).join("")}</tr>`;
  const tables = [];
  tables.push(valTable("SIGN BUCKETS — residual sign vs what happened next",
    ["Bucket", "N", "mean Δpace", "mean Δline", "mean settle err"],
    (d.sign_buckets || []).map((b) =>
      `<tr><td>${esc(b.bucket)}</td><td class="sc-num">${b.n}</td>`
      + `<td class="sc-num">${num(b.mean_subsequent_pace_change, 3)}</td>`
      + `<td class="sc-num">${num(b.mean_subsequent_live_line_change, 3)}</td>`
      + `<td class="sc-num">${num(b.mean_settlement_error, 3)}</td></tr>`)));
  tables.push(valTable("MAGNITUDE BINS — |residual| vs what happened next",
    ["Bin", "N", "mean |residual|", "mean Δpace", "mean settle err"],
    (d.magnitude_buckets || []).map((b) =>
      `<tr><td>${esc(b.bin)}</td><td class="sc-num">${b.n}</td>`
      + `<td class="sc-num">${num(b.mean_abs_residual, 2)}</td>`
      + `<td class="sc-num">${num(b.mean_subsequent_pace_change, 3)}</td>`
      + `<td class="sc-num">${num(b.mean_settlement_error, 3)}</td></tr>`)));
  tables.push(valTable("BY CONTEXT — classification | period (N≥8)",
    ["Key", "N", "r(res→Δpace)", "mean Δpace"],
    (d.by_context || []).filter((b) => !b.sample_too_small).map((b) =>
      `<tr><td>${esc(b.key)}</td><td class="sc-num">${b.n}</td>`
      + `<td class="sc-num">${num(b.r, 3)}</td>`
      + `<td class="sc-num">${num(b.mean_subsequent_pace_change, 3)}</td></tr>`)));
  tables.push(valTable("STABILITY ACROSS GAME TIME — progress buckets",
    ["Bucket", "N", "r(res→Δpace)", "mean Δpace"],
    (d.by_progress || []).map((b) =>
      `<tr><td>${esc(b.bucket)}</td><td class="sc-num">${b.n}</td>`
      + `<td class="sc-num">${num(b.r, 3)}</td>`
      + `<td class="sc-num">${num(b.mean_subsequent_pace_change, 3)}</td></tr>`)));
  $("valTables").innerHTML = tables.join("");
  valScatter("valChartA", d.scatter && d.scatter.residual_vs_subseq_pace || [],
    "market−trajectory residual", "subsequent pace change");
  valScatter("valChartB", d.scatter && d.scatter.z_vs_subseq_pace || [],
    "z-score", "subsequent pace change");
}

async function valLoad() {
  const ann = $("valAnn");
  try {
    const resp = await fetch(API_VALIDATION);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    valRender(await resp.json());
  } catch (err) {
    valDestroy();
    if (ann) ann.innerHTML = `<span class="muted">Validation unavailable: ${esc(err.message)}</span>`;
  }
}

const valDetails = document.querySelector("#valDetails");
if (valDetails) {
  valDetails.addEventListener("toggle", () => {
    const sum = valDetails.querySelector(":scope > summary");
    if (sum) {
      sum.textContent = valDetails.open
        ? "VALIDATION — WHAT HAPPENED NEXT ▾" : "VALIDATION — WHAT HAPPENED NEXT ▸";
    }
    if (valDetails.open) valLoad();
  });
}
