/* ═══════════════════════════════════════════════════════════════
   BLM LIVE ANALYTICS — operator dashboard
   Polls /api/v4/live every 5s; renders game cards + detail charts.
   No manual refresh required.
   ═══════════════════════════════════════════════════════════════ */
"use strict";

const POLL_MS = 5000;
const API_LIVE = "/api/v4/live";
const API_STATUS = "/api/v4/status";
const API_GAME = (id) => `/api/v4/game/${encodeURIComponent(id)}`;

const state = {
  filter: "",
  games: [],
  cards: new Map(),        // game_id -> {el, spark}
  modalGameId: null,
  modalCharts: {},
  rawOn: false,
  lastPayload: null,
  conn: null,
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
const num = (v, d = 1) => (v == null ? "--" : Number(v).toFixed(d));
const hasChart = () => typeof Chart !== "undefined";
const ChartColor = {
  home: "rgba(34,211,238,1)", away: "rgba(251,191,36,1)",
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
  let snaps = 0;
  for (const g of games) {
    if (per[g.classification]) {
      per[g.classification].n++;
      if (g.live) per[g.classification].live++;
    }
    snaps += g.snapshot_count || 0;
  }
  $("sumCyber").textContent = per.CYBER_2K26.n || 0;
  $("sumCyber").className = "sum-value cyber";
  $("sumCyberSub").textContent = `${per.CYBER_2K26.live} live`;
  $("sumBetual").textContent = per.BETUAL_NBA.n || 0;
  $("sumBetual").className = "sum-value betual";
  $("sumBetualSub").textContent = `${per.BETUAL_NBA.live} live`;
  $("sumSnaps").textContent = snaps;
  const db = payload.collector ? null : null;
  // age of freshest snapshot across games
  let newest = null;
  for (const g of games) {
    const t = g.last_update ? new Date(g.last_update).getTime() : 0;
    if (t > (newest || 0)) newest = t;
  }
  $("sumAge").textContent = newest ? fmtAge((Date.now() - newest) / 1000) : "--";
  $("sumAgeSub").textContent = newest ? fmtTime(new Date(newest).toISOString()) : "--";
}

/* ── Game cards ──────────────────────────────────────────── */

function winprobHTML(g) {
  const wp = g.model ? g.model.win_probability : 0.5;
  const h = Math.round(wp * 100), a = 100 - h;
  return `
    <div class="winprob">
      <div class="winprob-label">Win Probability</div>
      <div class="winprob-bar">
        <div class="home" style="width:${h}%"></div>
        <div class="away" style="width:${a}%"></div>
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
        <div><div class="lab">Mkt Total</div><div class="val">${num(mkt, 1)}</div></div>
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
  const arrow = dir === "up" ? "▲" : dir === "down" ? "▼" : "◆";
  const conf = g.model ? Math.round((g.model.confidence || 0) * 100) : 0;
  return `
    <div class="gauges">
      <div class="gauge">
        <div class="gauge-label">Momentum</div>
        <div class="gauge-value">${num(m.score, 0)}
          <span class="mom-arrow ${dir}">${arrow}</span>
          <span style="font-size:11px;color:var(--muted)">${esc(m.strength_label || "")}</span>
        </div>
        <div class="gauge-sub">${num(m.velocity, 1)} pts/min · acc ${num(m.acceleration, 1)}</div>
        <div class="bar-track"><div class="bar-fill ${dir === "up" ? "green" : dir === "down" ? "red" : ""}"
          style="width:${Math.min(100, Math.abs(m.score - 50) * 2 + 5)}%"></div></div>
      </div>
      <div class="gauge">
        <div class="gauge-label">Model Confidence</div>
        <div class="gauge-value" style="font-size:20px">${conf}%</div>
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
  return `<div class="signals">${names.map(([k, label]) => {
    const v = s[k] || {};
    const on = v.active;
    return `<span class="sig ${on ? "active" : ""}" title="${esc(label)} trap${on ? ` · conf ${Math.round((v.confidence || 0) * 100)}%` : ""}">${on ? "●" : "○"} ${label}${on ? ` ${Math.round((v.confidence || 0) * 100)}%` : ""}</span>`;
  }).join("")}</div>`;
}

function projHTML(g) {
  const mdl = g.model || {};
  const hp = mdl.home_projection, ap = mdl.away_projection;
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
      <button class="expand-btn" data-expand="${esc(g.game_id)}">⤢</button>
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
    // rebuild card body (charts survive: chart canvas is re-attached below)
    card.el.innerHTML = cardHTML(g);
    if (card.spark) {
      const holder = card.el.querySelector(".spark");
      holder.innerHTML = ""; // drop the template canvas
      holder.appendChild(card.spark.canvas);
      updateSpark(card.spark, g);
    } else {
      const canvas = card.el.querySelector(".spark canvas");
      card.spark = makeSpark(canvas, g);
    }
  }
  // remove cards whose games vanished from payload
  for (const [gid, card] of state.cards) {
    if (!seen.has(gid)) {
      card.el.remove();
      if (card.spark) card.spark.destroy();
      state.cards.delete(gid);
    }
  }
  if (!games.length) {
    $("empty").style.display = "block";
  } else {
    $("empty").style.display = "none";
  }
}

/* ── Detail modal ────────────────────────────────────────── */

function openModal(gameId) {
  state.modalGameId = gameId;
  $("modalBackdrop").hidden = false;
  document.body.style.overflow = "hidden";
  loadModal(gameId);
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

function renderModal(d) {
  const g = d;
  const mdl = g.model || {}, mkt = g.market || {}, mom = g.momentum || {},
        sig = g.signals || {};
  const body = $("modalBody");
  const confPct = Math.round((mdl.confidence || 0) * 100);
  const wpPct = Math.round((mdl.win_probability || 0) * 100);
  const tEdge = mkt.total_line != null && mdl.expected_total != null
    ? (mdl.expected_total - mkt.total_line).toFixed(1) : null;
  const sEdge = mkt.spread != null && mdl.expected_margin != null
    ? (mdl.expected_margin - mkt.spread).toFixed(1) : null;
  const activeSigs = (sig.active || []).map((k) =>
    `<span class="sig active">● ${esc(k)} ${Math.round((sig[k]?.confidence || 0) * 100)}%</span>`).join("");
  const tl = (d.timeline || []).map((e) =>
    `<div class="tl-item"><span class="tl-time">${fmtTime(e.t)}</span><span class="tl-label ${esc(e.type)}">${esc(e.label)}</span></div>`
  ).join("") || '<div class="tl-item"><span class="muted">No events yet</span></div>';

  body.innerHTML = `
    <div class="m-hero">
      <div class="m-score">
        <div><div class="team-name">${esc(g.home_team)}</div>
          <div class="team-score home">${g.home_score ?? "–"}</div></div>
        <div style="font-size:20px;color:var(--dim)">vs</div>
        <div><div class="team-name">${esc(g.away_team)}</div>
          <div class="team-score away">${g.away_score ?? "–"}</div></div>
      </div>
      <div style="text-align:right">
        <div><span class="cat-badge ${esc(g.classification)}">${esc(g.classification)}</span></div>
        <div class="muted" style="margin-top:6px">${esc(g.period_label || "")} ${esc(g.clock || "")}</div>
        <div class="muted">${g.snapshot_count} snapshots · last ${fmtAge(g.age_s)}</div>
      </div>
    </div>
    <div class="m-charts">
      <div class="m-chart"><h4>Score Progression (stored snapshots)</h4><canvas id="mcScore"></canvas></div>
      <div class="m-chart"><h4>Actual vs Market vs Projected Total</h4><canvas id="mcTotal"></canvas></div>
      <div class="m-chart full"><h4>Market &amp; Pace History</h4><canvas id="mcLine"></canvas></div>
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
        <div class="winprob-bar" style="margin:6px 0"><div class="home" style="width:${wpPct}%"></div><div class="away" style="width:${100 - wpPct}%"></div></div>
        <div class="m-row"><span class="k">Confidence</span><span class="v">${confPct}%</span></div>
        <div class="bar-track"><div class="bar-fill ${confPct >= 70 ? "green" : confPct >= 50 ? "amber" : "red"}" style="width:${confPct}%"></div></div>
        <div class="m-row" style="margin-top:8px"><span class="k">Home projection</span><span class="v">${num(mdl.home_projection, 1)}</span></div>
        <div class="m-row"><span class="k">Away projection</span><span class="v">${num(mdl.away_projection, 1)}</span></div>
        <div class="m-row"><span class="k">Pace</span><span class="v">${num(mdl.pace, 1)}</span></div>
        <div class="m-row"><span class="k">Expected total</span><span class="v">${num(mdl.expected_total, 1)}</span></div>
      `)}
      ${modalPanel("Momentum", `
        <div class="big">${num(mom.score, 0)} <span class="mom-arrow ${esc(mom.direction)}">${mom.direction === "up" ? "▲" : mom.direction === "down" ? "▼" : "◆"}</span></div>
        <div class="m-rows" style="margin-top:8px">
          <div class="m-row"><span class="k">Direction</span><span class="v">${esc(mom.direction)}</span></div>
          <div class="m-row"><span class="k">Strength</span><span class="v">${esc(mom.strength_label)}</span></div>
          <div class="m-row"><span class="k">Velocity</span><span class="v ${mom.velocity >= 0 ? "pos" : "neg"}">${mom.velocity >= 0 ? "+" : ""}${num(mom.velocity, 2)} pts/min</span></div>
          <div class="m-row"><span class="k">Acceleration</span><span class="v ${mom.acceleration >= 0 ? "pos" : "neg"}">${mom.acceleration >= 0 ? "+" : ""}${num(mom.acceleration, 2)}</span></div>
        </div>
        <div class="bar-track" style="margin-top:10px"><div class="bar-fill ${mom.direction === "up" ? "green" : mom.direction === "down" ? "red" : ""}" style="width:${Math.min(100, Math.abs(mom.score - 50) * 2 + 5)}%"></div></div>
      `)}
      ${modalPanel("Signals / Traps", `
        <div class="m-row"><span class="k">Trap meter</span><span class="v">${num(sig.trap_meter, 1)} / 100 (${esc(sig.trap_meter_level)})</span></div>
        <div class="bar-track"><div class="bar-fill ${sig.trap_meter >= 60 ? "red" : sig.trap_meter >= 30 ? "amber" : "green"}" style="width:${Math.min(100, sig.trap_meter || 0)}%"></div></div>
        <div class="signals" style="margin-top:8px">${activeSigs || '<span class="sig">○ no active signals</span>'}</div>
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
    <div class="timeline"><h4>Live Timeline (from stored snapshots)</h4>${tl}</div>`;

  // ── charts ──
  const h = g.history || [];
  if (hasChart()) {
    const mk = (id) => document.getElementById(id);
    if (state.modalCharts.score) state.modalCharts.score.destroy();
    if (state.modalCharts.total) state.modalCharts.total.destroy();
    if (state.modalCharts.line) state.modalCharts.line.destroy();

    state.modalCharts.score = new Chart(mk("mcScore"), {
      type: "line",
      data: {
        labels: h.map((s) => fmtTime(s.t)),
        datasets: [
          { label: g.home_team, data: h.map((s) => s.home), borderColor: ChartColor.home, backgroundColor: "rgba(34,211,238,.08)", fill: true, pointRadius: 0, tension: .25 },
          { label: g.away_team, data: h.map((s) => s.away), borderColor: ChartColor.away, backgroundColor: "rgba(251,191,36,.08)", fill: true, pointRadius: 0, tension: .25 },
        ],
      },
      options: baseChartOpts("Points"),
    });

    const combined = h.map((s) => (s.home != null && s.away != null ? s.home + s.away : null));
    const mktLine = h.map((s) => s.total_line);
    const projLine = h.map(() => mdl.expected_total);
    state.modalCharts.total = new Chart(mk("mcTotal"), {
      type: "line",
      data: {
        labels: h.map((s) => fmtTime(s.t)),
        datasets: [
          { label: "Actual (combined)", data: combined, borderColor: "#eaf2ff", pointRadius: 0, tension: .25 },
          { label: "Market total", data: mktLine, borderColor: ChartColor.amber, borderDash: [6, 4], pointRadius: 0 },
          { label: "Projected total", data: projLine, borderColor: ChartColor.cyan, borderDash: [2, 3], pointRadius: 0 },
        ],
      },
      options: baseChartOpts("Points"),
    });

    const lineVals = h.map((s) => s.total_line);
    state.modalCharts.line = new Chart(mk("mcLine"), {
      type: "line",
      data: {
        labels: h.map((s) => fmtTime(s.t)),
        datasets: [
          { label: "Total line", data: lineVals, borderColor: ChartColor.amber, pointRadius: 0, tension: .25 },
          { label: "Spread", data: h.map((s) => s.spread), borderColor: ChartColor.cyan, pointRadius: 0, tension: .25 },
        ],
      },
      options: baseChartOpts("Line value"),
    });
  } else {
    document.querySelectorAll(".m-chart canvas").forEach((c) => {
      c.replaceWith(Object.assign(document.createElement("div"), { textContent: "Chart.js unavailable — CDN blocked" }));
    });
  }
  if ($("mRawToggle").classList.contains("on")) toggleModalRaw(true, d);
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

async function loadModal(gameId) {
  try {
    const resp = await fetch(API_GAME(gameId));
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const d = await resp.json();
    $("mCat").textContent = d.classification || "–";
    $("mCat").className = `cat-badge ${esc(d.classification)}`;
    $("mTitle").textContent = `${d.home_team} vs ${d.away_team}`;
    renderModal(d);
    $("mRawToggle").dataset.gameId = gameId;
  } catch (err) {
    $("modalBody").innerHTML = `<div class="empty">Failed to load game: ${esc(err.message)}</div>`;
  }
}

/* ── Raw drawer ──────────────────────────────────────────── */

function setRaw(on) {
  state.rawOn = on;
  $("rawDrawer").hidden = !on;
  $("rawToggle").classList.toggle("on", on);
  if (on && state.lastPayload) {
    $("rawJson").textContent = JSON.stringify(state.lastPayload, null, 2);
  }
}

function toggleModalRaw(on, d) {
  $("mRawToggle").classList.toggle("on", on);
  if (on) {
    const raw = d ? JSON.stringify(d, null, 2)
      : state.lastPayload?.games?.find((g) => g.game_id === state.modalGameId);
    $("modalBody").insertAdjacentHTML("beforeend",
      `<details class="m-panel" id="mRawBox" style="margin-top:14px"><summary style="cursor:pointer;font-family:var(--mono);font-size:12px;color:var(--amber)">RAW JSON</summary>
       <pre class="raw-json" style="max-height:360px">${esc(typeof raw === "string" ? raw : JSON.stringify(raw, null, 2))}</pre></details>`);
  } else {
    document.getElementById("mRawBox")?.remove();
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
    if (state.rawOn) $("rawJson").textContent = JSON.stringify(payload, null, 2);
    if (state.modalGameId && !$("modalBackdrop").hidden) loadModal(state.modalGameId);
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

$("rawToggle").addEventListener("click", () => setRaw(!state.rawOn));
$("rawClose").addEventListener("click", () => setRaw(false));
$("modalClose").addEventListener("click", closeModal);
$("modalBackdrop").addEventListener("click", (ev) => {
  if (ev.target === $("modalBackdrop")) closeModal();
});
$("mRawToggle").addEventListener("click", (ev) => {
  const on = !ev.target.classList.contains("on");
  toggleModalRaw(on, null);
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") {
    if (!$("rawDrawer").hidden) setRaw(false);
    else if (!$("modalBackdrop").hidden) closeModal();
  }
});

refresh();
setInterval(refresh, POLL_MS);
