/* ═══════════════════════════════════════════════════════════════
   LIVE ANALYTICS — descriptive pace-Z operator dashboard.
   Polls /api/v4/live every 5s; renders game cards + a detail modal.

   SCOPE: LIVE + CLEAN + DESCRIPTIVE.  Observation surface for clean
   post-epoch data ONLY.  Cards and the modal present measured state:
   game state (provider/competition/period/clock/score), market state
   (observed live O/U line, age, SCORE − LINE gap), pace state
   (actual/required pace, pace gap), and the PACE Z-SCORE — the
   current actual pace compared with the strictly-prior historical
   actual-pace distribution at the same (provider, competition,
   period, progress) key.  z = (x − μ) / σ is consumed verbatim from
   the authoritative /pace-z endpoint; it is never computed here and
   it is descriptive only — it states how unusual the current pace is
   versus history, never what happens next.

   The expanded game view shows exactly ONE primary chart — SCORE vs
   LIVE LINE — MARKET MOVEMENT (actual cumulative score + every valid
   observed live O/U line, gaps kept as gaps) — plus the PACE Z panel
   chart below it on the same game-time axis.  The view carries only
   observed series and the descriptive pace-Z measurement; nothing
   derived from any other analytical source is presented here.

   A read-only HISTORICAL UNDER-CONDITION layer sits on top of this
   surface: when the current live state matches fixed findings from
   settled games (pace gap > +1.5, late Q4 progress, stored pace-z),
   a badge/panel shows the matching archive statistics and marks the
   market freshness.  Presentation of past observations only — the
   frozen descriptive model stays authoritative.
   ═══════════════════════════════════════════════════════════════ */
"use strict";

const POLL_MS = 5000;
const API_LIVE = "/api/v4/live";
const API_GAME = (id) => `/api/v4/game/${encodeURIComponent(id)}`;
// READ-ONLY additive exposure: the observed live-line history (every
// valid observed line at its exact observation time — the same
// market_observations rows the collector writes; no fabrication).
const API_GAME_LINES = (id) => `/api/v4/game/${encodeURIComponent(id)}/market-lines`;
const API_GAME_PACE_Z = (id) => `/api/v4/game/${encodeURIComponent(id)}/pace-z`;
const API_GAME_HIST_CTX = (id) => `/api/v4/game/${encodeURIComponent(id)}/historical-context`;

const state = {
  filter: "",
  games: [],
  cards: new Map(),        // game_id -> {el, spark, detOpen, chartsOpen}
  modalGameId: null,
  modalDetailOk: null,     // detail-endpoint liveness for the open modal (null = pending)
  modalCharts: {},
  modalZText: null,        // cached PACE Z readout across header re-renders
  modalGapText: null,      // cached SCORE − LINE readout across header re-renders
  modalZMeta: null,        // authoritative /pace-z payload block for the Z panel
  hideNonLive: true,       // default view: LIVE games only
  lastPayload: null,
};

/* ── UI section preferences — persisted in localStorage ─────
   Frontend presentation state only (no backend involvement).
     pz.gameDetailsCollapsed  — DETAILS in game cards / detail modal
     pz.chartsCollapsed       — CHARTS (cards' sparkline / modal)
   Defaults: game details collapsed; charts visible.
   "1" = collapsed, "0" = expanded.  New users get the defaults. */
const PREF = {
  GAME_DETAILS: "pz.gameDetailsCollapsed",
  CHARTS: "pz.chartsCollapsed",
  ALERT_AUDIO: "pz.alertAudioEnabled",
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

/* ── LIVE-state gate — the single predicate every live surface uses ────
   The BACKEND owns live-state (api._live_state): `live` is true only for a
   game whose status is an explicitly supported in-progress state, whose own
   latest frame proves it is not at/past the end of the game, AND whose
   latest observation is fresh.  Freshness alone is never sufficient.

   A game is NOT live merely because it appears in /api/v4/live, has a
   recent observation, a score, a market line, an eligible alert, or
   historical context — every one of those can be true of a finished game.
   The supported-status list mirrors api.LIVE_STATUSES and is applied on top
   of the backend flag (it can only ever REMOVE a game), so a stale cached
   payload cannot leak a non-live card back in. */
const LIVE_STATUSES = ["live", "halftime", "in_progress", "in-play", "inplay"];
function isActuallyLive(g) {
  if (!g || g.live !== true) return false;
  const s = String(g.status || "").trim().toLowerCase();
  return LIVE_STATUSES.indexOf(s) !== -1;
}

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
// exact age for market freshness: "18s", "4m 21s", "1h 05m" — never
// fabricated; null renders the en dash.
const fmtAgeExact = (s) => {
  if (s == null) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${Math.round(s % 60)}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
};
// market freshness status at the 300s threshold — LIVE/STALE/MISSING,
// exactly the backend classification; never re-derived differently.
const mktStatusWord = (age, hasLine) => {
  if (!hasLine) return "MISSING";
  if (age == null) return null;
  return age <= 300 ? "LIVE" : "STALE";
};
const num = (v, d = 1) => (v == null ? "–" : Number(v).toFixed(d));
const sig = (x) => x == null ? "–" : (x > 0 ? "+" : "") + x;

/* ── UNDER-condition layer (READ-ONLY PRESENTATION) ──
   TWO descriptive states, both against fixed archive findings:

   PRIMARY (pace-state, frozen forensic audit 2026-09-11 archive): the
   league/state-relative relationship — actual pace BELOW the observation's
   own PROVIDER|COMPETITION|PERIOD|PROGRESS historical average AND required
   pace AT/ABOVE that same average, with remaining_game_minutes >= 2.5
   (2.50 included) and a mature benchmark (N>=30).  Historical rate:
   69.75% observation UNDER / 73.02% equal-game / 12,444 obs / 1,515 games.
   All inputs come from the authoritative /historical-context payload —
   never recomputed in the browser.

   LEGACY (pace-gap tiers, retained as historical research context):
     base   pace_gap > +1.5              → 72.65% / 87.31% / 1,489
     strong + Q4 + progress >= 90        → 78.01% / 87.71% / 1,446
     high   + pace_z < -1                → 85.74% / 89.27% /   363

   The pace-state condition drives the alert; the pace-gap tier still
   renders when it matches but the pace-state condition outranks it.
   UNDER only — no opposite-direction alert and no forward claim. */
/* __PURE_ALERT_BEGIN__ */
// pace-state level: fires from the authoritative /historical-context
// payload only (under_state + eligible + benchmark_n >= 30).  z is
// secondary context and never part of the primary condition.
function paceStateLevel(ctx) {
  if (!ctx || ctx.status !== "matched" || !ctx.eligible) return null;
  if (!(ctx.benchmark_n >= 30)) return null;
  return ctx.under_state ? "pace-state" : null;
}
// UNDER level selection — consumes ONLY pre-extracted authoritative
// state: { hasLine, periodQ, progressPct, paceGap, z }  (z optional: the
// live card list carries no z, so the card path cannot reach the
// z-dependent levels until the modal's /pace-z payload lands).
function histAlertLevel(s) {
  if (!s || !s.hasLine) return null;               // missing line → no alert
  if (s.paceGap == null || !(s.paceGap > 1.5)) return null;
  const late = s.periodQ === "Q4" && s.progressPct != null && s.progressPct >= 90;
  if (late && s.z != null && s.z < -1) return "high";
  if (late) return "strong";
  return "base";
}
// pulse decision — the attention treatment fires ONLY when the level
// RISES (entry none→Lx or an escalation Lx→Ly with rank Ly>Lx).  Steady
// state and downgrades re-render without any pulse; returns the new level
// when it should pulse, else null.  pace-state outranks the legacy tiers.
const ALERT_RANK = { base: 1, strong: 2, high: 3, "pace-state": 4 };
function alertEscalation(prevLvl, nextLvl) {
  if (!nextLvl) return null;
  const rPrev = prevLvl ? (ALERT_RANK[prevLvl] || 0) : 0;
  return (ALERT_RANK[nextLvl] || 0) > rPrev ? nextLvl : null;
}
// ALERT ELIGIBILITY — the authoritative gate, decided by the BACKEND
// (api._alert_gate) and consumed here verbatim.  A game may alert ONLY
// when its latest observation is concurrently live, non-terminal, fresh
// (within the backend freshness bound) and has >= 2.5 minutes remaining.
// The browser never re-derives this, so a finished game or a stale stored
// observation can never raise an alert however well its OLD state happens
// to match the condition.  Every alert path routes through here.
function alertEligible(g) {
  return !!(g && g.alert && g.alert.eligible === true);
}
// ── fixed-progress UNDER condition (25% / 50% / 75%) ──
// One INDEPENDENT record per game per checkpoint, identified by
// game_id + checkpoint, so a 25% record never suppresses a later 50% or
// 75% one.  Phase-based attribution: the server evaluates the condition on
// the current observation and reports the highest checkpoint the game's
// progress has reached (g.under_alert.checkpoint); advancing into the next
// phase closes the previous checkpoint's record and lets the next one open
// on its own.
//
// The CONDITION ITSELF is not defined here.  It is evaluated once by the
// backend (blm_v4/live_analytics/under_alert.py) and arrives as
// g.under_alert.active, so the browser cannot reconstruct it — and no two
// surfaces can disagree about the same opportunity.
function underAlertId(gameId, checkpoint) {
  return `${gameId}|${checkpoint}`;
}
function fmtDuration(ms) {
  if (ms == null || !isFinite(ms) || ms < 0) return "—";
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}m ${s % 60}s` : `${s}s`;
}
/* __PURE_ALERT_END__ */
// fixed archive statistics bound to each UNDER level (display values only)
const HIST_ALERTS = {
  "pace-state": { label: "UNDER CONDITION — PACE STATE",
    obs: "69.75%", game: "73.02%", n: "1,515" },
  base: { label: "UNDER CONDITION",
    obs: "72.65%", game: "87.31%", n: "1,489" },
  strong: { label: "UNDER CONDITION — STRONG",
    obs: "78.01%", game: "87.71%", n: "1,446" },
  high: { label: "UNDER CONDITION — VERY STRONG",
    obs: "85.74%", game: "89.27%", n: "363" },
};
// CYBER archive result — shown as READ-ONLY context with its thin sample
// flagged; it is deliberately NOT an alert level (no CYBER tier exists).
const CYBER_HIST = { obs: "59.3%", game: "56.5%", n: "84" };

/* __ALERT_STORE_BEGIN__ */
/* ── UNDER ALERTS: ACTIVE vs HISTORY — two SEPARATE stores ──────────
   `active` holds ONLY the records whose condition is TRUE on the
   current poll; `history` holds every record that has ever triggered,
   resolved ones included.  Two distinct containers, never one array
   doing both jobs, and no poll ever rebuilds one from the other: a
   polling cycle cannot resurrect a resolved record as active, and it
   cannot erase history.

   Identity = game_id + checkpoint, so a single game carries up to three
   independent records across its life (25%, 50%, 75%) and an earlier
   checkpoint never suppresses a later one.

   Lifecycle:  INACTIVE → ACTIVE → RESOLVED
     FALSE→TRUE   open a record in `history`, add it to `active`
     TRUE→TRUE    refresh the ACTIVE display values only — the history
                  record keeps its original trigger snapshot untouched
     TRUE→FALSE   drop it from `active`, mark the record RESOLVED in
                  `history` (never deleted)

   The league reference is NOT carried in this file.  Each game arrives
   with its own server-evaluated `under_alert` block (active, checkpoint,
   actual/required/league-average pace), built from the mean settled final
   total / regulation minutes of the game's OWN competition — league-
   specific by construction, never one global number.  The browser holds no
   competition identifier of its own, so it can neither merge two leagues
   nor invent one; a competition with no reference gets active:false rather
   than borrowing another league's rate.

   Display labels are read from the filter buttons that already carry the
   canonical slug → label mapping in the page. */
const LEAGUE_LABELS = {};
function leagueLabelsFrom(root) {
  const map = {};
  const nodes = (root && root.querySelectorAll)
    ? root.querySelectorAll("#filters .filter[data-filter]") : [];
  for (const b of nodes) {
    const slug = b.dataset ? b.dataset.filter : "";
    const label = (b.textContent || "").trim();
    if (slug && label) map[slug] = label;
  }
  return map;
}
const UNDER_ALERTS = { active: new Map(), history: [] };
const ALERT_HISTORY_MAX = 300;
const ALERT_HISTORY_KEY = "pz.underAlertHistory";

const num2 = (x) => (x == null || !isFinite(x)) ? "–" : x.toFixed(2);
const num1 = (x) => (x == null || !isFinite(x)) ? "–" : x.toFixed(1);

/* ── FINAL OUTCOME — coloring only, never recomputation ──────────
   The outcome arrives per alert record in the game's authoritative
   `under_alert_outcome` block (backend-computed from the game's final
   total vs the IMMUTABLE trigger market total — never the opening,
   closing or any later line).  The browser maps status → class/word
   and renders it; it does not compare lines itself.
     under → GREEN   over → RED   push → neutral
     no_final/unknown → no coloring and no fake verdict
     null/absent  → still-active or pre-outcome record: existing look */
function alertOutcomeClass(oc) {
  if (!oc || oc.status == null) return null;
  return oc.status === "under" ? "al-under"
    : oc.status === "over" ? "al-over"
    : oc.status === "push" ? "al-push"
    : null;
}
function alertOutcomeLine(rec) {
  const oc = rec.outcome;
  if (!oc || oc.status == null) return "";
  const words = { under: "UNDER", over: "OVER", push: "PUSH",
    no_final: "NO FINAL", unknown: "NO FINAL" };
  const t = (v) => (v == null || !isFinite(v)) ? "–" : v.toFixed(1);
  const word = words[oc.status] || String(oc.status).toUpperCase();
  const trigger = `Trigger Total: <span class="al-num">${t(oc.trigger_total)}</span>`;
  const fin = (oc.final_total != null)
    ? ` · Final: <span class="al-num">${t(oc.final_total)}</span>` : "";
  return `<div class="al-line"><span class="al-outcome">${word}</span>`
    + ` · ${trigger}${fin}</div>`;
}

function loadAlertHistory() {
  try {
    const arr = JSON.parse(localStorage.getItem(ALERT_HISTORY_KEY) || "[]");
    UNDER_ALERTS.history = Array.isArray(arr) ? arr : [];
  } catch (_) { UNDER_ALERTS.history = []; }
}
function saveAlertHistory() {
  try {
    localStorage.setItem(ALERT_HISTORY_KEY,
      JSON.stringify(UNDER_ALERTS.history.slice(-ALERT_HISTORY_MAX)));
  } catch (_) { /* storage unavailable — history stays in memory */ }
}

// Every value comes from the authoritative /live payload — the game's own
// `under_alert` block, evaluated server-side.  Nothing is scraped back out
// of the rendered page, nothing is recalculated here, and the condition is
// never re-derived: `pace_gap` in particular is the server's own field, so
// the gap on screen and the verdict behind it come from one computation.
function underAlertValues(g, ua, labels) {
  const actual = ua.actual_pace;
  const required = ua.required_pace;
  const avg = ua.league_average_pace;
  return {
    checkpoint: ua.checkpoint,
    league: (labels && labels[g.competition_slug]) || g.competition_slug || "—",
    competition_slug: g.competition_slug || null,
    // canonical team names (already normalized upstream — never touched here)
    home_team: g.home_team || "",
    away_team: g.away_team || "",
    actual_pace: actual,
    required_pace: required,
    league_average_pace: avg,
    league_reference_games: ua.league_reference_games,
    pace_gap: ua.pace_gap,
    required_vs_league_avg_pct: avg ? (required - avg) / avg * 100 : null,
    actual_vs_required_pct: required ? (actual - required) / required * 100 : null,
    progress_pct: (g.projector || {}).progress_pct,
    // per-checkpoint final-outcome block, consumed verbatim (see
    // alertOutcomeClass) — null while the game has no final result
    outcome: (g.under_alert_outcome || {}).by_checkpoint
      ? (g.under_alert_outcome.by_checkpoint[ua.checkpoint] || null)
      : (g.under_alert_outcome || null),
  };
}

function reconcileUnderAlerts(games, labels) {
  const now = Date.now();
  const held = new Map();     // game_id -> this poll's live/phase verdict
  const trueNow = new Set();  // identities whose condition is TRUE now

  for (const g of games || []) {
    // the server's verdict for this poll, consumed verbatim
    const ua = g.under_alert || {};
    const cp = ua.checkpoint;
    // A record survives only while the game is genuinely live AND passes
    // the existing backend alert gate — a finished, stale, cancelled or
    // postponed game resolves its records (their history remains).  A null
    // checkpoint means the game has not reached its first checkpoint yet
    // (or has no resolvable progress), so there is no phase to attribute.
    // Defence-in-depth: the server's market gate, consumed verbatim.  It
    // can only REMOVE a record — never create one — so the API stays the
    // single authority on whether an alert is eligible at all.  An absent
    // block (synthetic payloads only) means "no opinion", not "ineligible".
    const mktEligible = !g.under_alert_eligibility
      || g.under_alert_eligibility.eligible === true;
    const live = isActuallyLive(g) && alertEligible(g) && mktEligible;
    const ok = live && cp != null && ua.active === true;
    held.set(g.game_id, {
      live,
      // Why the record is not standing RIGHT NOW — the server's own
      // vocabulary, in precedence order: the live gate's reason, the
      // alert gate's reason, the market gate's reason (a stale or missing
      // line is never silently dropped), else not_live.
      reason: live ? null
        : (g.live_reason || (g.alert && g.alert.reason)
           || (g.under_alert_eligibility && g.under_alert_eligibility.reason)
           || "not_live"),
      checkpoint: cp,
      ok,
    });
    if (!ok) continue;

    const id = underAlertId(g.game_id, cp);
    trueNow.add(id);
    const vals = underAlertValues(g, ua, labels);
    const act = UNDER_ALERTS.active.get(id);
    if (act) {
      // TRUE → TRUE — refresh what the ACTIVE panel shows.  The history
      // record keeps its original trigger snapshot untouched.
      Object.assign(act, vals);
      act.updated_at = new Date(now).toISOString();
      continue;
    }
    // FALSE → TRUE — resume an unresolved record for this identity (a
    // reload, or a poll that was missed), otherwise open a new one.
    let rec = UNDER_ALERTS.history.find((r) => r.id === id && !r.resolved_at);
    if (!rec) {
      rec = Object.assign({
        id, game_id: g.game_id,
        triggered_at: new Date(now).toISOString(),
        resolved_at: null, duration_ms: null, resolved_reason: null,
      }, vals);
      UNDER_ALERTS.history.push(rec);
      saveAlertHistory();
    }
    UNDER_ALERTS.active.set(id, Object.assign({
      id, game_id: g.game_id, triggered_at: rec.triggered_at,
      updated_at: new Date(now).toISOString(),
    }, vals));
  }

  // TRUE → FALSE — anything whose condition is not TRUE on THIS poll
  // leaves the active store; its history record is marked RESOLVED and
  // is never removed.
  for (const [id, act] of Array.from(UNDER_ALERTS.active)) {
    if (trueNow.has(id)) continue;
    closeUnderAlert(id, act, held.get(act.game_id), now);
  }
  backfillTeamNames(games);
  applyFinalOutcomes(games);
}

/* LEGACY records — records created before team names were captured at
   trigger time carry no home/away.  Names are FROZEN in the trigger
   snapshot for every record that has them; a record WITHOUT them may be
   filled exactly once from the backend's canonical identity for that
   EXACT game_id (the same canonical data the live payload serves, already
   Betual-normalized upstream).  Never guessed, never overwritten, never
   re-derived later: once set, the record is as self-contained as any new
   one.  A record the backend cannot safely resolve keeps no names and
   renders the Game <id> fallback. */
function backfillTeamNames(games) {
  let changed = false;
  for (const g of games || []) {
    if (!g || !g.game_id) continue;
    const h = g.home_team || "", a = g.away_team || "";
    if (!h && !a) continue;
    for (const r of UNDER_ALERTS.history) {
      if (r.game_id !== g.game_id) continue;
      if (r.home_team || r.away_team) continue;   // frozen — never touched
      r.home_team = h;
      r.away_team = a;
      changed = true;
    }
  }
  if (changed) saveAlertHistory();
}

/* FINAL OUTCOME delivery — backend-computed, trigger snapshot untouched.
   A game's alert can resolve (condition false) while the game continues;
   the final result only exists once the game finishes, so the outcome is
   sealed onto the matching history record whenever the payload first
   carries it.  Only records WITHOUT an outcome are filled — a sealed
   verdict is never recomputed or overwritten, and the trigger snapshot
   fields (actual/required/pace/line values) are never touched. */
function applyFinalOutcomes(games) {
  let changed = false;
  for (const g of games || []) {
    const ocBlock = g.under_alert_outcome;
    if (!ocBlock) continue;
    const per = ocBlock.by_checkpoint || null;
    for (const r of UNDER_ALERTS.history) {
      if (r.game_id !== g.game_id || r.outcome) continue;
      const one = per ? per[r.checkpoint] : ocBlock;
      if (!one || one.status == null) continue;
      r.outcome = Object.assign({}, one,
        (r.duration_ms != null && one.duration_ms == null)
          ? { duration_ms: r.duration_ms } : {});
      changed = true;
    }
  }
  if (changed) saveAlertHistory();
}

function closeUnderAlert(id, act, game, now) {
  UNDER_ALERTS.active.delete(id);
  const rec = UNDER_ALERTS.history.find((r) => r.id === id && !r.resolved_at);
  if (!rec) return;
  let reason = "condition_false";
  if (!game) reason = "no_longer_monitored";
  else if (!game.live) reason = game.reason || "not_live";
  else if (game.checkpoint != null && game.checkpoint > act.checkpoint) {
    reason = "checkpoint_passed";
  }
  rec.resolved_at = new Date(now).toISOString();
  rec.duration_ms = now - Date.parse(act.triggered_at);
  rec.resolved_reason = reason;
  // optional resolution values — the trigger snapshot above is never
  // overwritten by them.
  rec.final_actual_pace = act.actual_pace;
  rec.final_required_pace = act.required_pace;
  rec.final_pace_gap = act.pace_gap;
  // FINAL OUTCOME — sealed once at resolution, never recomputed.  If the
  // backend already computed the final verdict (game finished between
  // polls) it is kept verbatim and only the local duration is filled in;
  // a trigger-only block (game vanished before a final existed) is
  // stamped with the local resolution time.  A null status renders as
  // the legacy look, never as a colored outcome.
  let oc = act.outcome || null;
  if (oc) {
    if (oc.final_total == null && oc.status != null) {
      oc = Object.assign({}, oc, {
        resolved_at: rec.resolved_at, duration_ms: rec.duration_ms });
    } else if (oc.duration_ms == null) {
      oc = Object.assign({}, oc, { duration_ms: rec.duration_ms });
    }
  }
  rec.outcome = oc;
  saveAlertHistory();
}

const alertIdent = (r) => `${esc(r.league)} | Game ${esc(r.game_id)}`;

function activeAlertsHTML() {
  const rows = Array.from(UNDER_ALERTS.active.values())
    .sort((a, b) => b.checkpoint - a.checkpoint);
  if (!rows.length) {
    return `<div class="alerts-empty">No active UNDER alerts</div>`;
  }
  return `<ul>` + rows.map((a) => `
    <li class="al-row" data-alert-id="${esc(a.id)}">
      <div class="al-headline">🔥 UNDER ALERT — ${a.checkpoint}%</div>
      <div class="al-ident">${a.home_team || a.away_team
        ? `${esc(a.home_team)} vs ${esc(a.away_team)}` : alertIdent(a)}</div>
      <div class="al-line">Actual: <span class="al-num">${num2(a.actual_pace)}</span> | Required: <span class="al-num">${num2(a.required_pace)}</span> | League Avg: <span class="al-num">${num2(a.league_average_pace)}</span></div>
      <div class="al-line">Gap: <span class="al-neg">${num2(a.pace_gap)}</span> <span class="muted">(${num1(a.actual_vs_required_pct)}% vs required · ${num1(a.required_vs_league_avg_pct)}% vs avg)</span></div>
    </li>`).join("") + `</ul>`;
}

function historyRowHTML(rec) {
  const running = !rec.resolved_at;
  const dur = running ? (Date.now() - Date.parse(rec.triggered_at)) : rec.duration_ms;
  // final values are shown only when they actually moved after the trigger
  const moved = !running && (rec.final_actual_pace !== rec.actual_pace
    || rec.final_required_pace !== rec.required_pace);
  const ocClass = alertOutcomeClass(rec.outcome);
  return `
    <li class="al-row${running ? "" : " al-resolved"}${ocClass ? " " + ocClass : ""}">
      <div class="al-ident">[${rec.checkpoint}%] ${esc(rec.league)} | Game ${esc(rec.game_id)}</div>
      ${(rec.home_team || rec.away_team)
        ? `<div class="al-ident al-teams">${esc(rec.home_team)} vs ${esc(rec.away_team)}</div>` : ""}
      <div class="al-times">
        <span>Triggered: ${fmtTime(rec.triggered_at)}</span>
        <span>Ended: ${running ? "—" : fmtTime(rec.resolved_at)}</span>
        <span>Duration: ${fmtDuration(dur)}${running ? ` <span class="al-running">(still active)</span>` : ""}</span>
      </div>
      <div class="al-line">Actual: <span class="al-num">${num2(rec.actual_pace)}</span> | Required: <span class="al-num">${num2(rec.required_pace)}</span> | Avg: <span class="al-num">${num2(rec.league_average_pace)}</span></div>
      ${moved ? `<div class="al-line">Final: <span class="al-num">${num2(rec.final_actual_pace)}</span> | <span class="al-num">${num2(rec.final_required_pace)}</span></div>` : ""}
      ${alertOutcomeLine(rec)}
    </li>`;
}

function historyAlertsHTML() {
  if (!UNDER_ALERTS.history.length) {
    return `<div class="alerts-empty">No alerts triggered</div>`;
  }
  const rows = UNDER_ALERTS.history.slice().reverse();
  return `<ul>` + rows.map(historyRowHTML).join("") + `</ul>`;
}

function paintAlerts(elId, html) {
  const el = $(elId);
  if (el && el.innerHTML !== html) el.innerHTML = html;
}

// Single entry point, called once per poll from refresh() with the SAME
// payload the cards were rendered from.
function renderUnderAlerts(games, labels) {
  reconcileUnderAlerts(games, labels || LEAGUE_LABELS);
  paintAlerts("activeAlerts", activeAlertsHTML());
  paintAlerts("alertHistory", historyAlertsHTML());
  const ac = $("activeAlertsCount");
  if (ac) ac.textContent = UNDER_ALERTS.active.size
    ? `${UNDER_ALERTS.active.size} active` : "";
  const hc = $("alertHistoryCount");
  if (hc) {
    const open = UNDER_ALERTS.history.filter((r) => !r.resolved_at).length;
    hc.textContent = UNDER_ALERTS.history.length
      ? `${UNDER_ALERTS.history.length} triggered · ${open} still active` : "";
  }
}
/* __ALERT_STORE_END__ */

/* ── AUDIO alerts — sound ONLY on an alert ENTRY or ESCALATION ──
   A short cue sounds exactly when the UNDER condition RISES: the
   transition the existing alertEscalation() already defines (none→Lx
   entry, or Lx→Ly escalation with a higher rank).  A steady condition,
   a repeat of an unchanged level and every downgrade stay SILENT, so
   the cue can never fire on a 5-second poll or a plain re-render.

   Cues are synthesised with the Web Audio API — no audio files, no CDN,
   no network request, no asset that can go missing.  Three clearly
   distinguishable cues: base = one tone, strong = two, high = three.

   Browser autoplay policy is respected, never bypassed: the shared
   AudioContext is built lazily on the dashboard's first user gesture
   (or when AUDIO is switched on) and is never retried on a poll.  AUDIO
   defaults to OFF and only plays once explicitly enabled; the visual
   alert is completely independent of it. */
/* __PURE_AUDIO_BEGIN__ */
const ALERT_CUES = {
  "pace-state": [523.25, 659.25, 783.99, 1046.5],  // four tones, top priority
  base:   [523.25],                       // one tone
  strong: [659.25, 880],                  // two tones
  high:   [783.99, 987.77, 1318.51],      // three tones, highest priority
};
// One poll can enter/escalate several games at once.  Sound at most one
// cue per coalescing window — the first rise, plus any higher rise inside
// it — then hold silence for the rest of the window, so a burst of
// simultaneous entries can never stack into a continuous alarm while
// every genuine rise still sounds.
const AUDIO_COALESCE_MS = 400;
// pure decision: (window, level, now, rank) → { play, win }
//   play = level to sound (or null to stay silent)
//   win  = the coalescing window to remember for the next call
function audioWindowUpdate(win, level, now, rank) {
  const r = rank[level] || 0;
  if (!r) return { play: null, win };
  if (win && now < win.until && r <= win.best) return { play: null, win };
  return { play: level, win: { until: now + AUDIO_COALESCE_MS, best: r } };
}
/* __PURE_AUDIO_END__ */

let audioCtx = null;
let audioReady = false;      // context running — unlocked by a user gesture
let audioWindow = null;      // coalescing window state

// shared AudioContext, built lazily: a context created before any gesture
// stays suspended anyway, so it is never constructed at page load
function audioContext() {
  if (audioCtx) return audioCtx;
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return null;
  try { audioCtx = new AC(); } catch (_) { audioCtx = null; }
  return audioCtx;
}
// resume the context — only ever called from a user gesture, never a poll
function unlockAudio() {
  const ctx = audioContext();
  if (!ctx) { audioReady = false; return; }
  if (ctx.state === "running") { audioReady = true; return; }
  ctx.resume().then(
    () => { audioReady = audioCtx.state === "running"; syncAudioButton(); },
    () => { audioReady = false; syncAudioButton(); });
}
function audioEnabled() {
  try { return localStorage.getItem(PREF.ALERT_AUDIO) === "1"; } catch (_) { return false; }
}
// synthesise one cue — short tones with a click-free gain envelope
function playAlertCue(level) {
  const ctx = audioContext();
  if (!ctx || ctx.state !== "running") return;   // locked → silent, no retry
  const tones = ALERT_CUES[level];
  if (!tones) return;
  let t = ctx.currentTime + 0.02;
  for (const freq of tones) {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "triangle";
    osc.frequency.setValueAtTime(freq, t);
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.2, t + 0.012);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.16);
    osc.connect(gain).connect(ctx.destination);
    osc.start(t);
    osc.stop(t + 0.18);
    t += 0.2;
  }
}
// the SINGLE entry point, called wherever alertEscalation() returns a
// level (card badge and modal panel) — the transition source of truth
function alertAudio(level) {
  if (!level || !audioEnabled()) return;
  const upd = audioWindowUpdate(audioWindow, level, Date.now(), ALERT_RANK);
  audioWindow = upd.win;
  if (upd.play) playAlertCue(upd.play);
}
function syncAudioButton() {
  const btn = $("audioToggle");
  if (!btn) return;
  const on = audioEnabled();
  const locked = on && !audioReady;
  btn.classList.toggle("on", on);
  btn.classList.toggle("locked", locked);
  btn.dataset.audio = on ? "on" : "off";
  btn.textContent = on ? "AUDIO ON" : "AUDIO OFF";
  btn.setAttribute("aria-pressed", on ? "true" : "false");
  btn.title = !on ? "Alert audio off — click to enable"
    : locked ? "Alert audio on — click anywhere to activate sound"
    : "Alert audio on — click to silence";
}

const hasChart = () => typeof Chart !== "undefined";
const ChartColor = {
  home: "rgba(34,211,238,1)", away: "rgba(251,191,36,1)",
  market: "rgba(251,191,36,1)", grid: "rgba(30,42,58,.6)", tick: "#6b7a90",
};
const Z_COLOR = "#a78bfa";

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
  // live count comes from the SAME predicate the cards use, so the header
  // can never advertise a live game the grid refuses to render (or vice
  // versa) — the backend `totals.live` is derived identically.
  const liveNow = (payload.games || []).filter(isActuallyLive).length;
  $("gamesMonitoredPill").textContent =
    `${liveNow} live / ${(payload.games || []).length} games`;
}

function renderSummary(payload) {
  const games = payload.games || [];
  const per = { CYBER_2K26: { n: 0, live: 0 }, BETUAL_NBA: { n: 0, live: 0 } };
  let snaps = 0, live = 0;
  for (const g of games) {
    if (per[g.classification]) {
      per[g.classification].n++;
      if (isActuallyLive(g)) per[g.classification].live++;
    }
    if (isActuallyLive(g)) live++;
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

/* ── Game cards ──────────────────────────────────────────── */

// Regulation duration for elapsed/remaining/progress display
// (CYBER 2K26 = 48 min, BETUAL NBA = 40 min).
const fullMin = (g) => g.classification === "CYBER_2K26" ? 48 : 40;

// GAME STATE — observed score, period, clock, elapsed / remaining /
// progress minutes.  Measurements only.
function gameStateHTML(g) {
  const p = g.projector || {};
  const full = fullMin(g);
  const bits = [];
  if (p.elapsed_game_minutes != null) bits.push(`${num(p.elapsed_game_minutes, 1)}/${full.toFixed(0)} min`);
  if (p.remaining_game_minutes != null) bits.push(`${num(p.remaining_game_minutes, 1)} left`);
  if (p.progress_pct != null) bits.push(`${num(p.progress_pct, 0)}%`);
  const extra = bits.length ? `<span class="muted">· ${bits.join(" · ")}</span>` : "";
  return `<div class="game-meta">
    <span class="period">${esc(g.period_label || (g.quarter ? "Q" + g.quarter : "–"))}</span>
    <span>${esc(g.clock || "–")}</span>
    <span>${g.snapshot_count} snaps</span>
    ${extra}
  </div>`;
}

// LIVE MARKET — the observed market total line with its freshness state
// (LIVE/STALE/MISSING at the 300s threshold, exact age).  A line is
// presented as CURRENT only when the game is live AND the observation is
// fresh; otherwise the last observed line is labeled as such (ENDED/STALE)
// and is never presented as current.  Observed market state only.
function liveMarketHTML(g) {
  const m = g.market || {};
  const p = g.projector || {};
  const isLive = isActuallyLive(g);
  const line = p.live_total_line != null ? p.live_total_line : m.total_line;
  const age = p.market_age_seconds != null ? p.market_age_seconds : m.total_line_age_s;
  const mstatus = p.market_status || mktStatusWord(age, line != null);
  const liveLine = isLive && mstatus === "LIVE" ? line : null;
  const lastObs = liveLine == null && line != null ? line : null;
  const statusWord = g.status === "ended" ? "ENDED" : "STALE";
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

// PACE STATE — deterministic state on LIVE cards: actual Pts/Min vs the
// pace required to reach the live total line (gap = required − actual).
// Measurements only — observed scoring rate against the observed line.
function paceStripHTML(g) {
  const p = g.projector;
  if (!p || !isActuallyLive(g)) return "";
  const ap = p.actual_pts_per_min, rp = p.required_pts_per_min;
  if (ap == null && rp == null) return "";
  const gap = p.pace_gap;
  const gapCls = gap > 0 ? "pos" : gap < 0 ? "neg" : "";
  const signed = (v) => v == null ? "–" : (v > 0 ? "+" : "") + num(v, 2);
  return `<div class="pace-strip" title="Current scoring pace vs pace required to reach the live total line — observed state">
    <span class="pace-item"><b>PACE ${num(ap, 2)}</b> <span class="muted">pts/min</span></span>
    <span class="pace-item muted">req ${num(rp, 2)}</span>
    <span class="pace-item">gap <b class="${gapCls}">${signed(gap)}</b></span>
  </div>`;
}

// Analytically INVALID games: the backend quality gate excluded them from
// every aggregate.  The card marks them EXCLUDED (data-quality state).
function gatedNoteHTML(g) {
  const reason = g.quality_reason || "quality gate failed";
  return `<div class="gated-note">INVALID — EXCLUDED FROM ANALYTICS
    <span class="muted">· ${esc(reason)} · historical rows retained for diagnostics</span></div>`;
}

function cardHTML(g, ui, alertEnter) {
  const invalid = g.quality_status === "INVALID";
  const liveNow = isActuallyLive(g);
  const liveCls = invalid ? "chip-excluded"
    : (liveNow ? "chip-live" : (g.status === "ended" ? "chip-ended" : "chip-stale"));
  const liveTxt = invalid ? "EXCLUDED" : (liveNow ? "LIVE" : g.status === "ended" ? "ENDED" : "STALE");
  const score = (v) => (v == null ? "–" : v);
  const m = g.market || {};
  const detOpen = !!(ui && ui.detOpen);
  const chartsOpen = !!(ui && ui.chartsOpen);
  return `
    <div class="card-head">
      <span class="cat-badge ${esc(g.classification)}">${esc(g.classification)}</span>
      <span class="comp-name">${esc(g.competition_slug || g.competition || "")}</span>
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
    ${histBadgeHTML(g, alertEnter || null)}
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
  // Competition filter operates on the CANONICAL competition identifier
  // carried by each game record (competition_slug from the authoritative
  // source metadata), never a display alias.  The button's data-filter
  // value is that identifier; empty = ALL (no filter).  No competition
  // slug is hard-coded here — the comparison is generic.
  const games = (payload.games || []).filter(
    (g) => !state.filter || g.competition_slug === state.filter,
  );
  const visible = state.hideNonLive
    ? games.filter(isActuallyLive)
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
    // historical-condition entry tracking: pulse ONLY on level entry /
    // escalation (null→base→strong), never on steady 5s refreshes and
    // never on downgrades (strong→base renders the weaker badge silently).
    // The pace-state level (primary condition) outranks the legacy tiers;
    // it consumes the authoritative /historical-context payload only.
    const legacyLvl = liveAlertOf(g);
    const ctxLvl = paceStateLevel(g.historical_context);
    // backend alert gate: an ineligible game yields NO level, so its pulse
    // and cue state reset to null and it cannot fire from a stale state.
    const alLvl = alertEligible(g) ? (ctxLvl || legacyLvl) : null;
    const entered = alertEscalation(card.prevAlert, alLvl);
    card.prevAlert = alLvl;
    if (entered) alertAudio(entered);
    // re-render while preserving each card's CHARTS / DETAILS state
    card.el.innerHTML = cardHTML(g, { detOpen: card.detOpen, chartsOpen: card.chartsOpen }, entered);
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
  state.modalZMeta = null; // fresh game — authoritative Z payload awaits its fetch
  state.modalAlertLevel = null; // fresh game — alert pulse state resets
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
  state.modalAlertLevel = null;
  for (const k in state.modalCharts) {
    if (state.modalCharts[k]) state.modalCharts[k].destroy();
  }
  state.modalCharts = {};
}

function modalPanel(title, inner) {
  return `<div class="m-panel"><h4>${title}</h4>${inner}</div>`;
}

// authoritative PACE Z display: the value is the API z formatted to 2dp
// (sign-explicit).  Formatting only — never recomputed in the browser.
function zDisplay(z) {
  return z == null ? "n/a" : (z > 0 ? "+" : "") + num(z, 2);
}

/* ── Historical under-condition presentation ──────────────────
   Builders below compare CURRENT authoritative state (game payload
   projector fields + the /pace-z payload) against the FIXED archive
   findings and render badges/panels.  Pure state→level selection
   lives in histAlertLevel() above; these functions only format. */

// canonical period Q1..Q4 from the stored period label ("4th Quarter"),
// numeric quarter fallback; null for Half End / non-quarter states
function periodQOf(g) {
  const pl = String(g.period_label || "");
  const m = /^\s*([1-4])/.exec(pl);
  if (m) return "Q" + m[1];
  if (g.quarter != null && g.quarter >= 1 && g.quarter <= 4) return "Q" + g.quarter;
  return null;
}

function isCyberGame(g) {
  return /^CYBER/i.test(String(g.classification || ""))
    || g.provider === "CYBER"
    || /cyber/i.test(String(g.competition_slug || ""));
}

// observed market line + freshness classification (LIVE/STALE/MISSING
// at the 300s backend threshold) — never re-derived differently
function liveLineState(g) {
  const p = g.projector || {}, m = g.market || {};
  const line = p.live_total_line != null ? p.live_total_line : m.total_line;
  const age = p.market_age_seconds != null ? p.market_age_seconds : m.total_line_age_s;
  const mstatus = p.market_status || mktStatusWord(age, line != null);
  return { line, age, mstatus };
}

function signedNum(v, d = 2) {
  return v == null ? "–" : (v > 0 ? "+" : "") + Number(v).toFixed(d);
}

// card-level condition level — the /live list carries no z, so this
// never escalates past "strong"; the modal re-evaluates with the
// authoritative stored z from the /pace-z payload.
function liveAlertOf(g) {
  if (!g || !isActuallyLive(g) || g.quality_status === "INVALID") return null;
  const p = g.projector || {};
  const st = liveLineState(g);
  if (st.line == null) return null;
  return histAlertLevel({ hasLine: true, periodQ: periodQOf(g),
    progressPct: p.progress_pct, paceGap: p.pace_gap, z: null });
}

function marketChipHTML(mstatus) {
  if (mstatus === "STALE") return '<span class="al-chip al-stale">MARKET STALE</span>';
  if (mstatus === "LIVE") return '<span class="al-chip al-live">MARKET: LIVE</span>';
  return "";
}

function cyberNoteHTML(compact) {
  const note = `CYBER HISTORICAL CONTEXT — observation UNDER ${CYBER_HIST.obs} · `
    + `equal-game ${CYBER_HIST.game} · settled games ${CYBER_HIST.n}`;
  return compact
    ? `${note} · <span class="al-limited">LIMITED SAMPLE</span>`
    : `${note}<br><span class="al-limited">LIMITED SAMPLE — ${CYBER_HIST.n} SETTLED GAMES</span>`;
}

// RELATIVE PACE — HISTORICAL STATE.  The operator reads, explicitly: the
// benchmark IDENTITY, the HISTORICAL N of the relative-pace benchmark, the
// HISTORICAL MEAN, both pace values with their direction against that mean,
// and each relationship as its own literal TRUE/FALSE.
//
// EVERY value comes from the authoritative /historical-context payload.  The
// browser recomputes NOTHING: the directions are server-evaluated labels and
// the verdicts are payload booleans.
//
// The N shown here is the RELATIVE-PACE benchmark N (state_mean_n) — a
// SEPARATE calculation from the Z-score benchmark N, which is rendered only
// in the PACE Z-SCORE panel.  The two are never presented as one population.
function relPaceHTML(ctx) {
  if (!ctx || ctx.status !== "matched") return "";
  const ident = (k, v) =>
    `<div class="rp-ident"><span class="k">${k}</span>`
    + `<span class="v">${esc(v == null ? "–" : v)}</span></div>`;
  const pace = (k, v, dir) =>
    `<div class="rp-pace">`
    + `<div class="al-row"><span class="k">${k}</span>`
    + `<span class="v">${num(v, 3)} <span class="u">pts/min</span></span></div>`
    + `<div class="al-row rp-cmp"><span class="k">vs mean</span>`
    + `<span class="v">${esc(dir == null ? "–" : dir)}</span></div></div>`;
  const mu = ctx.state_mean_pace == null ? "–"
    : Number(ctx.state_mean_pace).toFixed(3) + " pts/min";
  return `<div class="rp-block">`
    + `<div class="rp-title">RELATIVE PACE — HISTORICAL STATE</div>`
    + ident("Provider", ctx.state_mean_provider)
    + ident("Competition", ctx.state_mean_competition)
    + ident("Period", ctx.state_mean_period)
    + ident("State", ctx.state_mean_state)
    + ident("Historical N", ctx.state_mean_n != null
        ? Number(ctx.state_mean_n).toLocaleString("en-US") : "–")
    + ident("Historical μ", mu)
    + pace("ACTUAL PACE", ctx.actual_pace, ctx.actual_vs_state_mean)
    + pace("REQUIRED PACE", ctx.required_pace, ctx.required_vs_state_mean)
    + paceStateHTML(ctx)
    + `</div>`;
}

// The three relationships, each its OWN literal TRUE/FALSE — no quadrant of
// the 2x2 can be hidden.  The verdicts are authoritative payload booleans
// rendered verbatim; the operator never infers a relationship from the z
// value, the pace gap, a colour or the raw numbers.
function paceStateHTML(ctx) {
  if (!ctx || ctx.status !== "matched") return "";
  const line = (label, ok) =>
    `<div class="rp-state-line ${ok ? "rp-true" : "rp-false"}">`
    + `${label} — TRUE/FALSE → <b>${ok ? "TRUE" : "FALSE"}</b></div>`;
  const actualBelow = ctx.actual_below_state_mean === true;
  const reqAtOrAbove = ctx.required_ge_state_mean === true;
  const both = ctx.both_conditions_true === true;
  return `<div class="rp-state">`
    + line("ACTUAL &lt; MEAN", actualBelow)
    + line("REQUIRED &ge; MEAN", reqAtOrAbove)
    + line("BOTH CONDITIONS TRUE", both)
    + `</div>`;
}

// HISTORICAL CONTEXT block — shown when BOTH conditions are TRUE: the
// frozen whole-archive QUALIFYING-cell rates (the primary-cell population
// the forensic audit froze on 2026-09-11), with the archive baseline
// alongside.  Server-served display constants, never browser arithmetic,
// and explicitly labelled historical / descriptive: these are NOT the
// current game's rate and NOT a forward claim.
function histContextHTML(ctx) {
  if (!ctx || ctx.status !== "matched" || !ctx.both_conditions_true) return "";
  const fmt = (v, d = 2) => (v == null ? "–" : Number(v).toFixed(d) + "%");
  const ni = (v) => (v == null ? "–" : Number(v).toLocaleString("en-US"));
  return `<div class="hc-block">`
    + `<div class="rp-title">HISTORICAL CONTEXT</div>`
    + `<span class="al-line"><b>${fmt(ctx.qualifying_under_pct)} UNDER</b> — observation rate</span>`
    + `<span class="al-line"><b>${fmt(ctx.qualifying_equal_game_under_pct)} UNDER</b> — equal-game rate</span>`
    + `<span class="al-line"><b>${ni(ctx.qualifying_games)}</b> qualifying games</span>`
    + `<span class="al-line"><b>${ni(ctx.qualifying_observations)}</b> qualifying observations</span>`
    + `<span class="al-line"><b>${fmt(ctx.archive_baseline_under_pct)}</b> archive baseline</span>`
    + `<span class="al-line muted">Frozen whole-archive figures `
    + `(${esc(ctx.frozen_audit_utc || "2026-09-11")} audit) — historical / `
    + `descriptive only, not this game's rate and not a forward claim.</span>`
    + `</div>`;
}

// compact card badge — shown while the condition holds, removed the
// moment it ceases; z is unknown here so only base/strong can appear.
// The pace-state (primary) level outranks the legacy pace-gap tiers.
function histBadgeHTML(g, entered) {
  if (!g || !isActuallyLive(g) || g.quality_status === "INVALID") return "";
  const p = g.projector || {};
  const st = liveLineState(g);
  if (st.line == null) return "";                     // missing line → no alert
  const lvl = alertEligible(g)
    ? (paceStateLevel(g.historical_context) || liveAlertOf(g)) : null;
  const cyber = isCyberGame(g);
  if (!lvl && !cyber) return "";
  const meta = lvl ? HIST_ALERTS[lvl] : null;
  const flash = entered ? " al-in" : "";
  const head = meta
    ? `<span class="hb-title al-${lvl}">${meta.label}</span>`
    : `<span class="hb-title hb-title-ctx">CYBER HISTORICAL CONTEXT</span>`;
  const stateLine = [];
  const ctx = g.historical_context;
  if (lvl === "pace-state" && ctx && ctx.status === "matched") {
    // primary condition: the two league/state-relative relationships shown
    // as explicit TRUE/FALSE chips (never inferred from the raw numbers)
    stateLine.push(`actual &lt; mean: ${ctx.actual_below_state_mean === true ? "TRUE" : "FALSE"}`);
    stateLine.push(`required &ge; mean: ${ctx.required_ge_state_mean === true ? "TRUE" : "FALSE"}`);
  } else {
    if (p.pace_gap != null) stateLine.push(`pace gap ${signedNum(p.pace_gap, 2)}`);
  }
  const q = periodQOf(g);
  if (q && p.progress_pct != null) stateLine.push(`${q} ${num(p.progress_pct, 0)}%`);
  const histLine = meta
    ? `<span class="hb-hist">${meta.obs} obs UNDER · ${meta.game} equal-game · N=${meta.n}</span>`
    : "";
  const cy = cyber ? `<div class="hb-cyber">${cyberNoteHTML(true)}</div>` : "";
  return `<div class="hist-badge${lvl ? " al-" + lvl : " al-ctx"}${flash}">`
    + `<div class="hb-head">${head}${marketChipHTML(st.mstatus)}</div>`
    + (stateLine.length ? `<div class="hb-state">${stateLine.join(" · ")}</div>` : "")
    + (histLine ? `<div class="hb-hist">${histLine}</div>` : "")
    + cy
    + `</div>`;
}

// full modal UNDER panel — the prominent, unmistakable presentation:
// every CURRENT authoritative live indicator (pace gap, pace Z, period,
// progress), the canonical competition, the matched level's fixed archive
// statistics and the market freshness marker.  UNDER only.
function histPanelHTML(g) {
  if (!g || !isActuallyLive(g) || g.quality_status === "INVALID") return "";
  const p = g.projector || {};
  const st = liveLineState(g);
  if (st.line == null) return "";                     // missing line → no alert
  const zm = state.modalZMeta || {};
  const ctx = g.historical_context;
  const lvl = alertEligible(g)
    ? (paceStateLevel(ctx)
       || histAlertLevel({ hasLine: true, periodQ: periodQOf(g),
         progressPct: p.progress_pct, paceGap: p.pace_gap, z: zm.z }))
    : null;
  const meta = lvl ? HIST_ALERTS[lvl] : null;
  const cyber = isCyberGame(g);
  const matched = ctx && ctx.status === "matched";
  if (!meta && !cyber && !matched) return "";
  const q = periodQOf(g);
  const score = (g.home_score != null && g.away_score != null)
    ? g.home_score + g.away_score : null;
  const slg = (score != null && st.line != null)
    ? Number((score - st.line).toFixed(1)) : null;
  // every current indicator, from authoritative values only (never recomputed)
  const rows = [];
  rows.push(`<div class="al-row"><span class="k">Live line</span><span class="v">${num(st.line, 1)}</span></div>`);
  rows.push(`<div class="al-row"><span class="k">Score</span><span class="v">${score != null ? score : "–"}</span></div>`);
  rows.push(`<div class="al-row"><span class="k">Score − line</span><span class="v">${signedNum(slg, 1)}</span></div>`);
  rows.push(`<div class="al-row"><span class="k">Actual pace</span><span class="v">${num(p.actual_pts_per_min, 2)} <span class="u">pts/min</span></span></div>`);
  if (ctx && ctx.status !== "matched") {
    rows.push(`<div class="al-row"><span class="k">Historical context</span><span class="v">NO MATURE HISTORICAL CONTEXT</span></div>`);
  }
  rows.push(`<div class="al-row"><span class="k">Required pace</span><span class="v">${num(p.required_pts_per_min, 2)} <span class="u">pts/min</span></span></div>`);
  rows.push(`<div class="al-row"><span class="k">Pace gap</span><span class="v">${signedNum(p.pace_gap, 2)} <span class="u">pts/min</span></span></div>`);
  rows.push(`<div class="al-row"><span class="k">Pace Z</span><span class="v">${zDisplay(zm.z)}</span></div>`);
  rows.push(`<div class="al-row"><span class="k">Period</span><span class="v">${esc(q || g.period_label || "–")}</span></div>`);
  rows.push(`<div class="al-row"><span class="k">Q4</span><span class="v">${q === "Q4" ? "YES" : "NO"}</span></div>`);
  rows.push(`<div class="al-row"><span class="k">Progress</span><span class="v">${p.progress_pct != null ? num(p.progress_pct, 1) + "%" : "–"}</span></div>`);
  rows.push(`<div class="al-row"><span class="k">Competition</span><span class="v">${esc(g.competition_slug || g.competition || "–")}</span></div>`);
  const head = meta
    ? `<span class="al-title">${meta.label}</span>`
    : '<span class="al-title al-title-ctx">RELATIVE-PACE STATE</span>';
  // RELATIVE-PACE CONTEXT (four values + the two evaluated comparisons +
  // benchmark identity) and the prominent state indicator — the primary
  // presentation.  When BOTH conditions are TRUE, the frozen whole-archive
  // qualifying rates follow (API-served constants, never browser arithmetic).
  const rp = relPaceHTML(ctx);
  const hc = histContextHTML(ctx);
  // legacy pace-gap tier statistics keep their fixed-constant block; the
  // pace-state level presents its rates through hc above (one headline).
  const legacyHist = meta && lvl !== "pace-state"
    ? `<div class="al-hist"><span class="al-big">HISTORICAL UNDER CONDITION</span>`
      + `<span class="al-line"><b>${meta.obs}</b> observation UNDER rate</span>`
      + `<span class="al-line"><b>${meta.game}</b> mean per-game UNDER rate</span>`
      + `<span class="al-line"><b>${meta.n}</b> settled games</span></div>`
    : "";
  // matched-key settled-population frequencies — explicitly labelled
  // MATCHED-KEY HINDSIGHT (whole settled population of this benchmark key,
  // including this game); always secondary, never a substitute for the
  // frozen qualifying-cell rates above
  const kh = (matched && ctx.key_hindsight_under_pct != null)
    ? `<div class="al-note muted">MATCHED-KEY HINDSIGHT — settled population `
      + `of this provider · competition · period · progress key: `
      + `${num(ctx.key_hindsight_under_pct, 2)}% UNDER observations · `
      + `${num(ctx.key_hindsight_equal_game_under_pct, 2)}% equal-game · `
      + `${ctx.key_hindsight_obs != null
          ? Number(ctx.key_hindsight_obs).toLocaleString("en-US") : "–"} obs</div>`
    : "";
  const cy = cyber ? `<div class="al-cyber">${cyberNoteHTML(false)}</div>` : "";
  const note = '<div class="al-note muted">Historical outcome = final total below this live line in the archive. Observed association only — no forward claim. Benchmark identity is this game\'s own provider · competition · period · progress state — never a league-wide average.</div>';
  return `<div class="hist-panel hist-alert${lvl ? " al-" + lvl : " al-ctx"}">`
    + `<div class="al-head">${head}${marketChipHTML(st.mstatus)}</div>`
    + (rows.length ? `<div class="al-rows">${rows.join("")}</div>` : "")
    + rp + hc + legacyHist + kh + cy + note
    + `</div>`;
}

// idempotent render into the single modal container; one-shot pulse on
// level entry/escalation only (never on steady-state refreshes)
function refreshHistAlert(g) {
  const box = document.getElementById("histAlertBox");
  if (!box || !g) return;
  const html = histPanelHTML(g);
  const zm = state.modalZMeta || {};
  const p = g.projector || {};
  const ctxLvl = paceStateLevel(g.historical_context);
  const legacyLvl = histAlertLevel({ hasLine: liveLineState(g).line != null,
    periodQ: periodQOf(g), progressPct: p.progress_pct,
    paceGap: p.pace_gap, z: zm.z });
  const lvl = alertEligible(g) ? (ctxLvl || legacyLvl) : null;
  const priorLevel = state.modalAlertLevel;
  state.modalAlertLevel = lvl;
  if (box.innerHTML === html) return;                 // stable during refresh
  box.innerHTML = html;
  const panel = box.firstElementChild;
  const rose = alertEscalation(priorLevel, lvl);
  if (panel && rose) {
    panel.classList.remove("al-in");
    void panel.offsetWidth;                            // restart the one-shot pulse
    panel.classList.add("al-in");
  }
  if (rose) alertAudio(rose);                          // same transition → same cue
}

// explicit no-context panel — shown when the live state is eligible but
// no mature own-league/state population exists (never a fallback, never
// a hidden game).  Pure presentation of the authoritative payload.
function noContextPanelHTML(g) {
  if (!g || !isActuallyLive(g) || g.quality_status === "INVALID") return "";
  const ctx = g.historical_context;
  if (!ctx || ctx.status !== "no_mature_historical_context") return "";
  if (ctx.reason === "below_analytical_eligibility") return ""; // <2.5 min: quiet, excluded
  const reason = {
    no_clean_observation: "no clean observation yet",
    competition_unresolved: "competition identity unresolved",
    insufficient_mature_population: `benchmark N=${ctx.benchmark_n} < 30 at ${ctx.benchmark_key || "own key"}`,
  }[ctx.reason] || ctx.reason || "no matching mature population";
  return `<div class="hist-panel hist-alert al-ctx">`
    + `<div class="al-head"><span class="al-title al-title-ctx">NO MATURE HISTORICAL CONTEXT</span></div>`
    + `<div class="al-note muted">No mature historical population (N &lt; 30) at this game's own ` 
    + `provider · competition · period · progress state. ${esc(reason)}. `
    + `No fallback average is ever substituted.</div>`
    + `</div>`;
}

function renderModal(g) {
  const mkt = g.market || {}, p = g.projector || {};
  const invalid = g.quality_status === "INVALID";
  if (state.modalGapText === undefined) state.modalGapText = null;
  const full = fullMin(g);
  const isLive = isActuallyLive(g);
  const chartsOpen = !prefGet(PREF.CHARTS, false);
  const detailsOpen = !prefGet(PREF.GAME_DETAILS, true);
  // Observed market line + freshness (clean metrics row preferred,
  // API market block as fallback).  A line is current only when the game
  // is live AND fresh; otherwise it is labeled "Last observed".
  const line = p.live_total_line != null ? p.live_total_line : mkt.total_line;
  const age = p.market_age_seconds != null ? p.market_age_seconds : mkt.total_line_age_s;
  const mstatus = p.market_status || mktStatusWord(age, line != null);
  const liveLine = isLive && mstatus === "LIVE" ? line : null;
  const lastObs = liveLine == null && line != null ? line : null;
  const statusWord = g.status === "ended" ? "ENDED" : "STALE";
  // LIVE score-vs-line gap: current combined score − observed live line.
  // Pure arithmetic on two OBSERVED values; the API's market.score_line_gap
  // is the same simple difference (used as fallback).
  const liveScore = (g.home_score != null && g.away_score != null)
    ? g.home_score + g.away_score : null;
  const gapVal = (liveLine != null && liveScore != null)
    ? Number((liveScore - liveLine).toFixed(1)) : (liveScore != null ? mkt.score_line_gap : null);
  const gapTxt = (gapVal != null && (liveLine != null || mkt.score_line_gap != null))
    ? `${gapVal > 0 ? "+" : ""}${num(gapVal, 1)}` : "–";
  const freshTxt = mstatus == null ? "" : `${mstatus} ${mstatus === "MISSING" ? "—" : fmtAgeExact(age)}`;
  const signed = (v) => v == null ? "–" : (v > 0 ? "+" : "") + num(v, 2);
  // Z panel content — authoritative /pace-z payload, refreshed by
  // renderModalCharts each poll; placeholders until the first fetch lands.
  const zm = state.modalZMeta || {};
  const zVal = zm.z;
  const stateBits = [];
  if (p.elapsed_game_minutes != null) stateBits.push(`${num(p.elapsed_game_minutes, 1)}/${full.toFixed(0)} min elapsed`);
  if (p.remaining_game_minutes != null) stateBits.push(`${num(p.remaining_game_minutes, 1)} min remaining`);
  if (p.progress_pct != null) stateBits.push(`${num(p.progress_pct, 1)}% progress`);

  $("mCat").textContent = g.classification || "–";
  $("mCat").className = `cat-badge ${esc(g.classification)}`;
  $("mTitle").textContent = `${g.home_team || "–"} vs ${g.away_team || "–"}`;

  // Preserve chart canvases across re-renders: re-attaching the SAME canvas
  // node keeps the Chart.js instances alive so the 5-second refresh updates
  // the series in place instead of rebuilding.
  const prevCanvas = !invalid ? document.getElementById("mcTotal") : null;
  const prevZCanvas = !invalid ? document.getElementById("mcZ") : null;
  $("modalBody").innerHTML = `
    ${g.data_quality === "LEGACY" ? `<div class="legacy-banner">LEGACY / PRE-CLEAN GAME — started before the clean-data epoch (${esc(String(g.data_epoch || "").slice(0, 19))}Z). Current state below uses post-epoch observations only.</div>` : ""}
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
    <div class="m-panels">
      ${modalPanel("Game State", `
        <div class="m-row"><span class="k">Provider</span><span class="v">${esc(g.provider || "–")}</span></div>
        <div class="m-row"><span class="k">Competition</span><span class="v">${esc(g.competition_slug || "–")}</span></div>
        <div class="m-row"><span class="k">Score</span><span class="v">${g.home_score ?? "–"} – ${g.away_score ?? "–"}</span></div>
        <div class="m-row"><span class="k">Period</span><span class="v">${esc(g.period_label || (g.quarter ? "Q" + g.quarter : "–"))}</span></div>
        <div class="m-row"><span class="k">Clock</span><span class="v">${esc(g.clock || "–")}</span></div>
        <div class="m-row"><span class="k">Elapsed</span><span class="v">${p.elapsed_game_minutes != null ? num(p.elapsed_game_minutes, 1) + " min" : "–"}</span></div>
        <div class="m-row"><span class="k">Remaining</span><span class="v">${p.remaining_game_minutes != null ? num(p.remaining_game_minutes, 1) + " min" : "–"}</span></div>
        <div class="m-row"><span class="k">Progress</span><span class="v">${p.progress_pct != null ? num(p.progress_pct, 1) + "%" : "–"}</span></div>
      `)}
      ${modalPanel("Market State", `
        <div class="m-row"><span class="k">${liveLine != null ? "Live total" : (lastObs != null ? "Last observed" : "Live total")}</span><span class="v">${num(line, 1)}${lastObs != null ? ` <span class="muted" style="font-size:10px">@ ${((mkt.total_line_at || p.market_captured_at) || "").slice(11, 19)}Z · ${statusWord}</span>` : ""}</span></div>
        <div class="m-row"><span class="k">Score − line</span><span class="v ${gapVal > 0 ? "pos" : gapVal < 0 ? "neg" : ""}">SCORE ${liveScore != null ? liveScore : "–"} − LINE ${line != null ? num(line, 1) : "–"} = ${gapTxt}</span></div>
        <div class="m-row"><span class="k">Line freshness</span><span class="v">${freshTxt ? `<span class="st ${mstatus === "LIVE" ? "st-live" : mstatus === "STALE" ? "st-stale" : "st-missing"}">${esc(freshTxt)}</span>` : "–"}</span></div>
        <div class="m-row"><span class="k">Line age</span><span class="v">${age != null ? fmtAgeExact(age) : "–"}</span></div>
        <div class="m-row"><span class="k">Market source</span><span class="v">${esc(mkt.market_source || "–")}</span></div>
      `)}
      ${modalPanel("Pace State", `
        <div class="m-row"><span class="k">Actual Pts/Min</span><span class="v">${num(p.actual_pts_per_min, 2)}</span></div>
        <div class="m-row"><span class="k">Required Pts/Min (vs live line)</span><span class="v">${num(p.required_pts_per_min, 2)}</span></div>
        <div class="m-row"><span class="k">Pace gap (required − actual)</span><span class="v ${(p.pace_gap ?? 0) > 0 ? "pos" : (p.pace_gap ?? 0) < 0 ? "neg" : ""}">${signed(p.pace_gap)}</span></div>
        <div class="muted" style="font-size:10px;margin-top:6px">Observed scoring rate vs the rate the live line implies — descriptive state only.</div>
      `)}
    </div>
    <div class="m-panel zpanel">
      <div class="zpanel-head">
        <h4>PACE Z-SCORE DEVIATION</h4>
        <div class="z-big" id="zPanelReadout">${esc(state.modalZText || "z = n/a")}</div>
      </div>
      <div class="z-grid">
        <div class="z-cell"><span class="z-k">CURRENT ACTUAL PACE</span><span class="z-v" id="zStatPace">${num(zm.actual_pace != null ? zm.actual_pace : p.actual_pts_per_min, 3)}</span><span class="z-u">pts/min</span></div>
        <div class="z-cell"><span class="z-k">Z-SCORE HISTORICAL N</span><span class="z-v" id="zStatN">${zm.n != null ? zm.n : "–"}</span><span class="z-u">prior observations</span></div>
        <div class="z-cell"><span class="z-k">HISTORICAL MEAN PACE</span><span class="z-v" id="zStatMu">${num(zm.mean_pace, 3)}</span><span class="z-u">μ pts/min</span></div>
        <div class="z-cell"><span class="z-k">HISTORICAL STD DEV</span><span class="z-v" id="zStatSigma">${num(zm.std_pace, 3)}</span><span class="z-u">σ</span></div>
      </div>
      <div class="z-key muted" id="zStatKey">${zm.benchmark_key ? esc(zm.benchmark_key) : "benchmark: provider|competition|period|progress — prior observations only"}${zm.status ? ` · ${esc(zm.status)}` : ""}</div>
      <div class="z-note muted">z = (actual pace − historical mean) ÷ historical σ — how the current scoring pace compares with the prior actual-pace distribution at the same provider · competition · period · progress state. Descriptive only; the observation never defines its own benchmark. <b>Z-SCORE HISTORICAL N is the Z-score benchmark population — a SEPARATE calculation from the RELATIVE PACE — HISTORICAL STATE N</b>, which uses its own cutoff observation and may legitimately differ.</div>
    </div>
    <div id="histAlertBox" class="hist-alert-box"></div>
    <div id="histNoCtxBox" class="hist-alert-box">${noContextPanelHTML(g)}</div>
    ${invalid ? "" : `<details class="m-chart-toggle" ${chartsOpen ? "open" : ""}>
      <summary data-label="CHARTS">CHARTS ${chartsOpen ? "▾" : "▸"}</summary>
      <div class="m-charts">
        <div class="m-chart m-chart-full"><div class="m-chart-head"><h4>SCORE vs LIVE LINE — MARKET MOVEMENT</h4><div class="z-readout" id="gapReadout">${esc(state.modalGapText || "SCORE − LINE = –")}</div></div><div class="chart-box"><canvas id="mcTotal"></canvas></div></div>
        <div class="m-chart m-chart-z"><div class="m-chart-head"><h4>PACE Z-SCORE</h4><div class="z-readout" id="zPanelChartReadout">${esc(state.modalZText || "z = n/a")}</div></div><div class="chart-box chart-box-z"><canvas id="mcZ"></canvas></div></div>
      </div>
    </details>`}
    <details class="m-details" ${detailsOpen ? "open" : ""}>
      <summary data-label="DETAILS">DETAILS ${detailsOpen ? "▾" : "▸"}</summary>
      <div class="m-panel m-wide">
        <h4>Game Info / Data Quality</h4>
        <div class="m-rows">
          <div class="m-row"><span class="k">Classification</span><span class="v">${esc(g.classification)}</span></div>
          <div class="m-row"><span class="k">Competition</span><span class="v">${esc(g.competition_slug || g.competition || "–")}</span></div>
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
    ${g.raw ? `<details class="tech-raw"><summary>Technical / Raw Data</summary><pre>${esc(typeof g.raw === "string" ? g.raw : JSON.stringify(g.raw, null, 2))}</pre></details>` : ""}`;

  if (prevCanvas) {
    const box = $("modalBody").querySelector(".chart-box");
    if (box) {
      const fresh = box.querySelector("canvas");
      if (fresh) fresh.replaceWith(prevCanvas);
    }
  }
  if (prevZCanvas) {
    const zBox = $("modalBody").querySelector(".chart-box-z");
    if (zBox) {
      const freshZ = zBox.querySelector("canvas");
      if (freshZ) freshZ.replaceWith(prevZCanvas);
    }
  }
  bindCollapsible($("modalBody").querySelector(".m-chart-toggle"), PREF.CHARTS);
  bindCollapsible($("modalBody").querySelector(".m-details"), PREF.GAME_DETAILS);
  // Historical-condition panel: immediate render from cached state, then
  // again once the authoritative /pace-z payload lands (charts fetch it).
  refreshHistAlert(g);
  // Fire-and-forget is fine, but failures must surface — a silent no-chart
  // state is exactly the bug class this view must never ship again.
  renderModalCharts(invalid ? null : g)
    .then(() => {
      if (state.modalGameId === g.game_id) refreshHistAlert(g);
    })
    .catch((err) =>
      console.error("[PZ] chart render failed:", err));
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

// Draws the Z=0 reference line across the plot area — the neutral marker
// that makes signed Z movement (toward/away from zero) readable.  An
// inline plugin (not grid ticks) so the zero line is always drawn,
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
  // Two series only — the actual cumulative score and the observed live
  // O/U line history.  No frontend Z computation, no interpolation, no
  // fabricated lines: gaps stay gaps (spanGaps:false), stale/missing
  // lines never substitute for live ones.
  const h = g.history || [];
  // Score-vs-line readout computed IMMEDIATELY from the detail payload's
  // history — BEFORE the async series fetches — so the header shows the
  // gap the moment the modal opens.  Current combined score minus the
  // freshest observed line: pure arithmetic, no derived value enters it.
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
  let pz = null, lines = null, hctx = null;
  // All three series are fetched in parallel — the PACE Z store, the
  // observed market-line history, and the league/state historical context
  // are independent authoritative sources.
  const [pzRes, linesRes, ctxRes] = await Promise.allSettled([
    fetch(API_GAME_PACE_Z(g.game_id)).then((r) => (r.ok ? r.json() : null)),
    fetch(API_GAME_LINES(g.game_id)).then((r) => (r.ok ? r.json() : null)),
    fetch(API_GAME_HIST_CTX(g.game_id)).then((r) => (r.ok ? r.json() : null)),
  ]);
  if (pzRes.status === "fulfilled") pz = pzRes.value;
  if (linesRes.status === "fulfilled") lines = linesRes.value;
  if (ctxRes.status === "fulfilled") hctx = ctxRes.value;
  if (hctx) {
    g.historical_context = hctx;              // authoritative payload, verbatim
    const nc = document.getElementById("histNoCtxBox");
    if (nc) {
      const nch = noContextPanelHTML(g);
      if (nc.innerHTML !== nch) nc.innerHTML = nch;   // idempotent
    }
  }
  // a failed fetch is not fatal — the chart renders from history alone
  if (state.modalGameId !== g.game_id) return; // modal switched games mid-fetch
  const series = (pz && pz.series) || [];
  const lineObs = (lines && lines.observations) || [];
  // epoch-ms x placement; unparseable timestamps are dropped (never guessed)
  const tMs = (iso) => { const ms = Date.parse(iso); return Number.isFinite(ms) ? ms : null; };
  // Live-line series: every valid observed line at its exact observation
  // timestamp — movement is drawn only from real observations, never
  // interpolated.  A genuine observation gap becomes an explicit null-y
  // break placed at the PREVIOUS observation's own timestamp.
  const lineData = [];
  let prevT = null;
  for (const o of lineObs) {
    const x = tMs(o.captured_at);
    if (x == null || o.line_value == null) continue;
    if (!o.adjacent) lineData.push({ x: prevT != null ? prevT : x, y: null });
    lineData.push({ x, y: o.line_value });
    prevT = x;
  }
  // PACE Z-SCORE panel series — the authoritative STORED pace z from the
  // benchmark layer (GET /pace-z): actual pace vs the strictly-prior
  // historical population at (provider, competition, period, progress).
  // The frontend never computes z.  EVERY kept point is preceded by an
  // explicit null break when a previous point exists: non-adjacent stored
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
  // (sample size, historical mean/σ, provider/competition partition)
  const zMeta = (pz && pz.n != null)
    ? `Z-SCORE N=${pz.n}` +
      (pz.mean_pace != null ? ` · μ=${num(pz.mean_pace, 3)} pts/min` : "") +
      (pz.std_pace != null ? ` · σ=${num(pz.std_pace, 3)}` : "") +
      (pz.provider && pz.competition ? ` · ${pz.provider}/${pz.competition}` : "")
    : null;
  const zReadout = zMeta ? `${zTxt} · ${zMeta}` : zTxt;
  // cache the authoritative block for the Z panel (re-renders between polls)
  state.modalZMeta = {
    z: (pz ? pz.z : null),
    actual_pace: (pz && pz.actual_pace != null ? pz.actual_pace
                  : (pz && pz.series && pz.series.length
                     ? pz.series[pz.series.length - 1].actual_pace : null)),
    n: (pz ? pz.n : null),
    mean_pace: (pz ? pz.mean_pace : null),
    std_pace: (pz ? pz.std_pace : null),
    benchmark_key: (pz ? pz.benchmark_key : null),
    status: (pz ? pz.benchmark_status : null),
  };
  const zEl = document.getElementById("zPanelReadout");
  if (zEl) zEl.textContent = zReadout;
  const zcEl = document.getElementById("zPanelChartReadout");
  if (zcEl) zcEl.textContent = zReadout;
  state.modalZText = zReadout;
  // live-update the Z panel stat cells without waiting for the next poll
  const setCell = (id, txt) => {
    const el = document.getElementById(id);
    if (el) el.textContent = txt;
  };
  const mz = state.modalZMeta;
  setCell("zStatPace", mz.actual_pace != null ? num(mz.actual_pace, 3) : "–");
  setCell("zStatN", mz.n != null ? String(mz.n) : "–");
  setCell("zStatMu", mz.mean_pace != null ? num(mz.mean_pace, 3) : "–");
  setCell("zStatSigma", mz.std_pace != null ? num(mz.std_pace, 3) : "–");
  setCell("zStatKey", mz.benchmark_key
    ? `${mz.benchmark_key} · ${mz.status || "prior observations only"}`
    : "benchmark: provider|competition|period|progress — prior observations only");
  const opts = baseChartOpts("Points / line");
  // Wall-clock x axis WITHOUT the Chart.js time scale: no date adapter is
  // loaded (CDN core only), so a type:"time" scale would throw.  A linear
  // epoch-ms axis with clock-formatted ticks shows the same timestamps.
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
  // range only; no z math here), same game-time axis as the primary chart.
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
        title: { display: true, text: "PACE Z", color: Z_COLOR, font: { size: 9 } },
        ticks: { color: Z_COLOR, font: { size: 9, family: "monospace" }, maxTicksLimit: 5,
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
              borderColor: Z_COLOR, borderWidth: 2, pointRadius: 2,
              pointHoverRadius: 4, tension: .15, spanGaps: false },
          ]},
          options: zOpts,
        });
        state.modalCharts.z.$gameId = g.game_id;
      }
    }
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
        borderColor: Z_COLOR, borderWidth: 2, pointRadius: 2,
        pointHoverRadius: 4, tension: .15, spanGaps: false },
    ]},
    options: zOpts,
  });
  state.modalCharts.z.$gameId = g.game_id;
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
    // active-vs-history alert reconciliation runs on the SAME payload, once
    // per poll — it never rebuilds one store from the other.
    renderUnderAlerts(state.games);
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

/* AUDIO control — OFF by default.  Switching it on is the explicit user
   gesture that also unlocks the shared AudioContext.  The context is armed
   from a user gesture so a later alert can sound; it is never created or
   resumed from the polling loop. */
$("audioToggle").addEventListener("click", () => {
  const on = !audioEnabled();
  try { localStorage.setItem(PREF.ALERT_AUDIO, on ? "1" : "0"); } catch (_) {}
  audioWindow = null;                     // fresh coalescing window per switch
  if (on) unlockAudio();
  syncAudioButton();
});
syncAudioButton();

// Arm the context from a user gesture.  A single attempt must not strand a
// context the browser chose to leave suspended, so each gesture retries
// until it is running — and only a gesture, never the poll, can do this.
const armAudio = () => { if (!audioReady) unlockAudio(); };
["pointerdown", "keydown", "touchstart"].forEach((ev) =>
  window.addEventListener(ev, armAudio, { passive: true }));

// Alert history is restored from local storage; the ACTIVE store is never
// restored from it — it is always re-derived from live observations, so a
// reload can never promote a resolved record back to active.  Competition
// display labels come from the filter buttons already in the page.
loadAlertHistory();
Object.assign(LEAGUE_LABELS, leagueLabelsFrom(document));
refresh();
setInterval(refresh, POLL_MS);
