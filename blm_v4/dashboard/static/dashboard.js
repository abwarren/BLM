/* ═══════════════════════════════════════════════════════════════
   BLM LIVE ANALYTICS — operator dashboard
   Polls /api/v4/live every 5s; renders game cards + detail charts.
   No manual refresh required.  Analytics first — raw JSON is only
   available via the API endpoints or "Technical / Raw Data" in detail.
   ═══════════════════════════════════════════════════════════════ */
"use strict";

const POLL_MS = 5000;
const API_LIVE = "/api/v4/live";
const API_GAME = (id) => `/api/v4/game/${encodeURIComponent(id)}`;

const state = {
  filter: "",
  games: [],
  cards: new Map(),        // game_id -> {el, spark}
  modalGameId: null,
  modalCharts: {},
};

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
const num = (v, d = 1) => (v == null ? "–" : Number(v).toFixed(d));
const pct = (v, d = 0) => (v == null ? "–" : `${(v * 100).toFixed(d)}%`);
const hasChart = () => typeof Chart !== "undefined";
const ChartColor = {
  home: "rgba(34,211,238,1)", away: "rgba(251,191,36,1)",
  model: "rgba(96,165,250,1)", market: "rgba(251,191,36,1)",
  momentum: "rgba(52,211,153,1)", confidence: "rgba(96,165,250,1)",
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
  // current model performance
  const v = { model_version: d.model_version || "v4-pace-1", ...ver };
  html.push(`<div class="sc-block">
    <h4>MODEL ${esc(v.model_version || "?")}</h4>
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
  // model vs market
  html.push(`<div class="sc-block">
    <h4>MODEL VS MARKET</h4>
    <table class="sc-table">
      <tr><td>Comparisons (market existed)</td><td class="sc-num">${mc.n ?? 0}</td></tr>
      <tr><td>Model MAE</td><td class="sc-num">${mc.model_mae ?? "–"}</td></tr>
      <tr><td>Market MAE</td><td class="sc-num">${mc.market_mae ?? "–"}</td></tr>
      <tr><td>Model beat market</td><td class="sc-num">${mc.model_beat_market_rate != null ? (mc.model_beat_market_rate * 100).toFixed(1) + "%" : "–"}</td></tr>
      <tr><td>O/U hit rate</td><td class="sc-num">${mc.ou_hit_rate != null ? (mc.ou_hit_rate * 100).toFixed(1) + "%" : "–"}</td></tr>
      <tr><td>Over / Under / Push</td><td class="sc-num">${mc.over ?? 0} / ${mc.under ?? 0} / ${mc.push ?? 0}</td></tr>
    </table>
  </div>`);
  // data quality — RECORDED vs COMPLETED vs VALID vs EXCLUDED (never conflated)
  html.push(`<div class="sc-block">
    <h4>DATA QUALITY</h4>
    <table class="sc-table">
      <tr><td>Recorded predictions</td><td class="sc-num">${q.recorded_predictions ?? "–"}</td></tr>
      <tr><td>Completed games (OK result)</td><td class="sc-num">${q.completed_games ?? "–"}</td></tr>
      <tr><td>Valid scored games</td><td class="sc-num">${q.valid_scored_games ?? "–"}</td></tr>
      <tr><td>Invalid / excluded</td><td class="sc-num">${q.invalid ?? "–"}</td></tr>
      <tr><td>Excluded games total</td><td class="sc-num">${q.excluded_games ?? "–"}</td></tr>
      <tr><td>Reasons</td><td>${Object.entries(q.excluded_reasons || {}).map(([k, n]) => `${esc(k)}: ${n}`).join(", ") || "–"}</td></tr>
    </table>
  </div>`);
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
    <h4>TIME-OF-DAY (start hour, local)</h4>
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

/* ── Game cards ──────────────────────────────────────────── */

function winprobHTML(g) {
  const wp = g.model ? g.model.win_probability : 0.5;
  const h = Math.round(wp * 100), a = 100 - h;
  return `
    <div class="winprob">
      <div class="winprob-bar">
        <div class="home" style="width:${h}%"><span>${h}%</span></div>
        <div class="away" style="width:${a}%"><span>${a}%</span></div>
      </div>
      <div class="winprob-legend">
        <span>${esc(g.home_team)} <b>${h}%</b></span>
        <span><b>${a}%</b> ${esc(g.away_team)}</span>
      </div>
    </div>`;
}

function divergenceHTML(g) {
  const m = g.market || {}, mdl = g.model || {};
  const mkt = m.total_line, mod = mdl.expected_total;
  const hasT = mkt != null && mod != null;
  const tEdge = hasT ? +(mod - mkt).toFixed(1) : null;
  const mSpr = m.spread, modSpr = mdl.expected_margin;
  const hasS = mSpr != null && modSpr != null;
  const sEdge = hasS ? +(modSpr - mSpr).toFixed(1) : null;
  const edgeCls = (v) => (v == null ? "flat" : v > 0.05 ? "pos" : v < -0.05 ? "neg" : "flat");
  const edgeSym = (v) => (v == null ? "–" : (v > 0 ? "+" : "") + v);
  return `
    <div class="divergence">
      <div class="divergence-title">Market vs Model</div>
      <div class="div-row">
        <div><div class="lab">Mkt Total</div><div class="val">${num(mkt, 1)}${m.total_line_age_s != null ? `<span class="muted" style="font-size:10px"> ${m.total_line_age_s > 300 ? "· stale" : "· live"}</span>` : ""}</div></div>
        <div><div class="lab">Model Total</div><div class="val">${num(mod, 1)}</div></div>
        <div><div class="lab">Edge</div><div class="edge ${edgeCls(tEdge)}">${edgeSym(tEdge)}</div></div>
      </div>
      ${hasS ? `<div class="div-row" style="margin-top:6px">
        <div><div class="lab">Mkt Spread</div><div class="val">${num(mSpr, 1)}</div></div>
        <div><div class="lab">Model Margin</div><div class="val">${num(modSpr, 1)}</div></div>
        <div><div class="lab">Edge</div><div class="edge ${edgeCls(sEdge)}">${edgeSym(sEdge)}</div></div>
      </div>` : ""}
    </div>`;
}

function momentumHTML(g) {
  const m = g.momentum || {};
  const dir = m.direction || "flat";
  const word = dir === "up" ? "RISING" : dir === "down" ? "FALLING" : "FLAT";
  const arrow = dir === "up" ? "↗" : dir === "down" ? "↘" : "→";
  const conf = g.model ? Math.round((g.model.confidence || 0) * 100) : 0;
  return `
    <div class="gauges">
      <div class="gauge">
        <div class="gauge-label">Momentum</div>
        <div class="gauge-value mom-value ${dir}">${arrow}<span class="mom-word">${word}</span></div>
        <div class="gauge-sub">${esc(m.strength_label || "—")} · ${num(m.velocity, 1)} pts/min · acc ${num(m.acceleration, 1)}</div>
        <div class="bar-track"><div class="bar-fill ${dir === "up" ? "green" : dir === "down" ? "red" : ""}"
          style="width:${Math.min(100, Math.abs(m.score - 50) * 2 + 5)}%"></div></div>
      </div>
      <div class="gauge">
        <div class="gauge-label">Model Confidence</div>
        <div class="gauge-value" style="font-size:22px">${conf}%</div>
        <div class="gauge-sub">pace ${num(g.model ? g.model.pace : null, 1)} · exp total ${num(g.model ? g.model.expected_total : null, 1)}</div>
        <div class="bar-track"><div class="bar-fill ${conf >= 70 ? "green" : conf >= 50 ? "amber" : "red"}"
          style="width:${conf}%"></div></div>
      </div>
    </div>`;
}

function signalsHTML(g) {
  const s = g.signals || {};
  const names = [
    ["bull_trap", "Bull"], ["bear_trap", "Bear"], ["reverse_bull_trap", "Rev Bull"],
    ["dead_market", "Dead"], ["false_momentum", "False Mom"], ["late_trap", "Late"],
    ["sharp_trap", "Sharp"],
  ];
  const anyActive = (s.active || []).length;
  if (!anyActive) return `<div class="signals"><span class="sig-none">No active signals</span></div>`;
  return `<div class="signals">${names.map(([k, label]) => {
    const v = s[k] || {};
    if (!v.active) return "";
    return `<span class="sig active" title="${esc(label)} trap · conf ${Math.round((v.confidence || 0) * 100)}%">● ${label} ${Math.round((v.confidence || 0) * 100)}%</span>`;
  }).join("")}</div>`;
}

function projHTML(g) {
  const mdl = g.model || {};
  const hp = mdl.home_projection, ap = mdl.away_projection;
  const total = (hp != null && ap != null) ? +(hp + ap).toFixed(1) : null;
  const max = Math.max(hp || 0, ap || 0, 1);
  return `
    <div class="projs">
      <div class="winprob-label">Team Projections</div>
      <div class="proj-row"><span class="pname">${esc(g.home_team)}</span>
        <span class="ptrack"><span class="pfill" style="width:${Math.round((hp || 0) / max * 100)}%;background:var(--cyan)"></span></span>
        <span class="pval">${num(hp, 1)}</span></div>
      <div class="proj-row"><span class="pname">${esc(g.away_team)}</span>
        <span class="ptrack"><span class="pfill" style="width:${Math.round((ap || 0) / max * 100)}%;background:var(--amber)"></span></span>
        <span class="pval">${num(ap, 1)}</span></div>
      ${total != null ? `<div class="proj-total"><span>PROJECTED TOTAL</span><b>${num(total, 1)}</b></div>` : ""}
    </div>`;
}

function cardHTML(g) {
  const liveCls = g.live ? "chip-live" : (g.status === "ended" ? "chip-ended" : "chip-stale");
  const liveTxt = g.live ? "LIVE" : g.status === "ended" ? "ENDED" : "STALE";
  const score = (v) => (v == null ? "–" : v);
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
    <div class="game-meta">
      <span class="period">${esc(g.period_label || (g.quarter ? "Q" + g.quarter : "–"))}</span>
      <span>${esc(g.clock || "–")}</span>
      <span>${g.snapshot_count} snaps</span>
    </div>
    ${winprobHTML(g)}
    ${divergenceHTML(g)}
    ${momentumHTML(g)}
    ${signalsHTML(g)}
    ${projHTML(g)}
    <div class="spark"><canvas></canvas></div>
    <div class="card-foot">
      <span>${fmtTime(g.last_update)}</span>
      <span>eff ${num(g.market_efficiency, 3)}</span>
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

function renderCards(payload) {
  const grid = $("grid");
  const games = (payload.games || []).filter(
    (g) => !state.filter || g.classification === state.filter,
  );
  const seen = new Set();
  for (const g of games) {
    seen.add(g.game_id);
    let card = state.cards.get(g.game_id);
    if (!card) {
      const el = document.createElement("div");
      el.className = "card";
      el.innerHTML = cardHTML(g);
      el.addEventListener("click", () => openModal(g.game_id));
      grid.appendChild(el);
      const canvas = el.querySelector(".spark canvas");
      const spark = makeSpark(canvas, g);
      state.cards.set(g.game_id, { el, spark, lastScore: null });
      card = state.cards.get(g.game_id);
    }
    // score change → flash
    const nowScore = `${g.home_score}|${g.away_score}`;
    if (card.lastScore && card.lastScore !== nowScore) {
      card.el.classList.remove("flash");
      void card.el.offsetWidth;
      card.el.classList.add("flash");
    }
    card.lastScore = nowScore;
    card.el.innerHTML = cardHTML(g);
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
  for (const k in state.modalCharts) {
    if (state.modalCharts[k]) state.modalCharts[k].destroy();
  }
  state.modalCharts = {};
}

function modalPanel(title, inner) {
  return `<div class="m-panel"><h4>${title}</h4>${inner}</div>`;
}

function renderModal(g) {
  const mdl = g.model || {}, mkt = g.market || {}, mom = g.momentum || {},
        sig = g.signals || {};
  const confPct = Math.round((mdl.confidence || 0) * 100);
  const wpPct = Math.round((mdl.win_probability || 0) * 100);
  const tEdge = mkt.total_line != null && mdl.expected_total != null
    ? (mdl.expected_total - mkt.total_line).toFixed(1) : null;
  const sEdge = mkt.spread != null && mdl.expected_margin != null
    ? (mdl.expected_margin - mkt.spread).toFixed(1) : null;
  const activeSigs = (sig.active || []).map((k) =>
    `<span class="sig active">● ${esc(k)} ${Math.round((sig[k]?.confidence || 0) * 100)}%</span>`).join("");
  const tl = (g.timeline || []).map((e) =>
    `<div class="tl-item"><span class="tl-time">${fmtTime(e.t)}</span><span class="tl-label ${esc(e.type)}">${esc(e.label)}</span></div>`
  ).join("") || '<div class="tl-item"><span class="muted">No events yet</span></div>';
  const rawJson = g.raw || g.latest_snapshot || null;

  $("mCat").textContent = g.classification || "–";
  $("mCat").className = `cat-badge ${esc(g.classification)}`;
  $("mTitle").textContent = `${g.home_team || "–"} vs ${g.away_team || "–"}`;

  $("modalBody").innerHTML = `
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
      </div>
    </div>
    <div class="m-charts">
      <div class="m-chart"><h4>Score Progression</h4><canvas id="mcScore"></canvas></div>
      <div class="m-chart"><h4>Actual vs Market vs Model Total</h4><canvas id="mcTotal"></canvas></div>
      <div class="m-chart"><h4>Model History — Win Probability &amp; Confidence</h4><canvas id="mcModel"></canvas></div>
      <div class="m-chart"><h4>Momentum History</h4><canvas id="mcMomentum"></canvas></div>
    </div>
    <div class="m-panels">
      ${modalPanel("Market vs Model", `
        <div class="m-rows">
          <div class="m-row"><span class="k">Market total</span><span class="v">${num(mkt.total_line, 1)}</span></div>
          <div class="m-row"><span class="k">Model total</span><span class="v">${num(mdl.expected_total, 1)}</span></div>
          <div class="m-row"><span class="k">Total edge</span><span class="v ${tEdge > 0 ? "pos" : tEdge < 0 ? "neg" : ""}">${tEdge != null ? (tEdge > 0 ? "+" : "") + tEdge : "–"}</span></div>
          <div class="m-row"><span class="k">Market spread</span><span class="v">${num(mkt.spread, 1)}</span></div>
          <div class="m-row"><span class="k">Model margin</span><span class="v">${num(mdl.expected_margin, 1)}</span></div>
          <div class="m-row"><span class="k">Spread edge</span><span class="v ${sEdge > 0 ? "pos" : sEdge < 0 ? "neg" : ""}">${sEdge != null ? (sEdge > 0 ? "+" : "") + sEdge : "–"}</span></div>
          <div class="m-row"><span class="k">Market efficiency</span><span class="v">${num(g.market_efficiency, 3)}</span></div>
          <div class="m-row"><span class="k">Market momentum</span><span class="v">${num(g.market_momentum, 2)}</span></div>
        </div>`)}
      ${modalPanel("Model", `
        <div class="m-row"><span class="k">Win probability (home)</span><span class="v">${wpPct}%</span></div>
        <div class="winprob-bar" style="margin:6px 0"><div class="home" style="width:${wpPct}%"><span>${wpPct}%</span></div><div class="away" style="width:${100 - wpPct}%"><span>${100 - wpPct}%</span></div></div>
        <div class="m-row"><span class="k">Confidence</span><span class="v">${confPct}%</span></div>
        <div class="bar-track"><div class="bar-fill ${confPct >= 70 ? "green" : confPct >= 50 ? "amber" : "red"}" style="width:${confPct}%"></div></div>
        <div class="m-row" style="margin-top:8px"><span class="k">Home projection</span><span class="v">${num(mdl.home_projection, 1)}</span></div>
        <div class="m-row"><span class="k">Away projection</span><span class="v">${num(mdl.away_projection, 1)}</span></div>
        <div class="m-row"><span class="k">Pace</span><span class="v">${num(mdl.pace, 1)}</span></div>
        <div class="m-row"><span class="k">Expected total</span><span class="v">${num(mdl.expected_total, 1)}</span></div>
      `)}
      ${modalPanel("Momentum", `
        <div class="big ${esc(mom.direction)}">${mom.direction === "up" ? "↗" : mom.direction === "down" ? "↘" : "→"} ${esc((mom.direction || "flat").toUpperCase())}</div>
        <div class="m-rows" style="margin-top:8px">
          <div class="m-row"><span class="k">Score</span><span class="v">${num(mom.score, 0)} / 100</span></div>
          <div class="m-row"><span class="k">Strength</span><span class="v">${esc(mom.strength_label)}</span></div>
          <div class="m-row"><span class="k">Velocity</span><span class="v ${mom.velocity >= 0 ? "pos" : "neg"}">${mom.velocity >= 0 ? "+" : ""}${num(mom.velocity, 2)} pts/min</span></div>
          <div class="m-row"><span class="k">Acceleration</span><span class="v ${mom.acceleration >= 0 ? "pos" : "neg"}">${mom.acceleration >= 0 ? "+" : ""}${num(mom.acceleration, 2)}</span></div>
        </div>
        <div class="bar-track" style="margin-top:10px"><div class="bar-fill ${mom.direction === "up" ? "green" : mom.direction === "down" ? "red" : ""}" style="width:${Math.min(100, Math.abs((mom.score || 50) - 50) * 2 + 5)}%"></div></div>
      `)}
      ${modalPanel("Signals / Traps", `
        <div class="m-row"><span class="k">Trap meter</span><span class="v">${num(sig.trap_meter, 1)} / 100 (${esc(sig.trap_meter_level)})</span></div>
        <div class="bar-track"><div class="bar-fill ${sig.trap_meter >= 60 ? "red" : sig.trap_meter >= 30 ? "amber" : "green"}" style="width:${Math.min(100, sig.trap_meter || 0)}%"></div></div>
        <div class="signals" style="margin-top:8px">${activeSigs || '<span class="sig-none">No active signals</span>'}</div>
        ${["bull_trap", "bear_trap", "reverse_bull_trap", "dead_market", "false_momentum", "late_trap", "sharp_trap"]
          .map((k) => `<div class="m-row" style="margin-top:4px"><span class="k">${esc(k)}</span><span class="v">${sig[k]?.active ? "ACTIVE" : "no"} · conf ${Math.round((sig[k]?.confidence || 0) * 100)}%</span></div>`).join("")}
      `)}
      ${modalPanel("Game Info", `
        <div class="m-row"><span class="k">Classification</span><span class="v">${esc(g.classification)}</span></div>
        <div class="m-row"><span class="k">Competition</span><span class="v">${esc(g.competition)}</span></div>
        <div class="m-row"><span class="k">Event ID</span><span class="v">${esc(g.game_id)}</span></div>
        <div class="m-row"><span class="k">Region</span><span class="v">${esc(g.region)}</span></div>
        <div class="m-row"><span class="k">Status</span><span class="v">${esc(g.status)}</span></div>
        <div class="m-row"><span class="k">W1 / W2 odds</span><span class="v">${num(mkt.w1_odds, 2)} / ${num(mkt.w2_odds, 2)}</span></div>
        <div class="m-row"><span class="k">Team totals</span><span class="v">${num(mkt.home_total_line, 1)} / ${num(mkt.away_total_line, 1)}</span></div>
        <div class="m-row"><span class="k">Source</span><span class="v">${esc(g.source)}</span></div>
      `)}
    </div>
    <div class="timeline"><h4>Live Timeline (from stored snapshots)</h4>${tl}</div>
    ${rawJson ? `<details class="tech-raw"><summary>Technical / Raw Data</summary><pre>${esc(typeof rawJson === "string" ? rawJson : JSON.stringify(rawJson, null, 2))}</pre></details>` : ""}`;

  renderModalCharts(g);
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

function renderModalCharts(g) {
  if (!hasChart()) {
    document.querySelectorAll(".m-chart canvas").forEach((c) => {
      c.replaceWith(Object.assign(document.createElement("div"),
        { textContent: "Chart.js unavailable — CDN blocked" }));
    });
    return;
  }
  const h = g.history || [];
  const labels = h.map((s) => fmtTime(s.t));
  const mk = (id) => document.getElementById(id);
  for (const k in state.modalCharts) {
    if (state.modalCharts[k]) state.modalCharts[k].destroy();
  }
  state.modalCharts = {};

  state.modalCharts.score = new Chart(mk("mcScore"), {
    type: "line",
    data: { labels, datasets: [
      { label: g.home_team, data: h.map((s) => s.home), borderColor: ChartColor.home,
        backgroundColor: "rgba(34,211,238,.08)", fill: true, pointRadius: 0, tension: .25 },
      { label: g.away_team, data: h.map((s) => s.away), borderColor: ChartColor.away,
        backgroundColor: "rgba(251,191,36,.08)", fill: true, pointRadius: 0, tension: .25 },
    ]},
    options: baseChartOpts("Points"),
  });

  state.modalCharts.total = new Chart(mk("mcTotal"), {
    type: "line",
    data: { labels, datasets: [
      { label: "Actual (combined)", data: h.map((s) => s.combined), borderColor: "#eaf2ff", pointRadius: 0, tension: .25 },
      { label: "Market total", data: h.map((s) => s.total_line), borderColor: ChartColor.market, borderDash: [6, 4], pointRadius: 0 },
      { label: "Model total", data: h.map((s) => s.expected_total), borderColor: ChartColor.model, borderDash: [2, 3], pointRadius: 0 },
    ]},
    options: baseChartOpts("Points"),
  });

  state.modalCharts.model = new Chart(mk("mcModel"), {
    type: "line",
    data: { labels, datasets: [
      { label: "Win prob %", data: h.map((s) => (s.win_prob ?? null) != null ? s.win_prob * 100 : null), borderColor: ChartColor.home, pointRadius: 0, tension: .25 },
      { label: "Confidence %", data: h.map((s) => (s.confidence ?? null) != null ? s.confidence * 100 : null), borderColor: ChartColor.confidence, borderDash: [4, 3], pointRadius: 0, tension: .25 },
    ]},
    options: baseChartOpts("%"),
  });

  state.modalCharts.momentum = new Chart(mk("mcMomentum"), {
    type: "line",
    data: { labels, datasets: [
      { label: "Momentum", data: h.map((s) => s.momentum_score), borderColor: ChartColor.momentum,
        backgroundColor: "rgba(52,211,153,.08)", fill: true, pointRadius: 0, tension: .25 },
    ]},
    options: baseChartOpts("Score 0–100"),
  });
}

async function loadModalDetail(gameId) {
  try {
    const resp = await fetch(API_GAME(gameId));
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const d = await resp.json();
    if (state.modalGameId !== gameId || $("modalBackdrop").hidden) return;
    renderModal(d);
  } catch (err) {
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
    // game vanished while its detail was open → close cleanly
    if (state.modalGameId && !state.games.some((g) => g.game_id === state.modalGameId)) {
      closeModal();
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

$("filters").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".filter");
  if (!btn) return;
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

refresh();
setInterval(refresh, POLL_MS);
