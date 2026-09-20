"""ACTIVE UNDER ALERTS — triggered line + result (directive 2026-09-13).

The ACTIVE UNDER ALERTS view must show BOTH halves of every alert:

  1. THE TRIGGERED LIVE LINE — the exact live market line that caused the
     alert to trigger, captured at creation and IMMUTABLE thereafter;
  2. THE RESULT — PENDING (neutral) until the game settles, then UNDER
     (green) or OVER (red) against that same triggered line; an exact-line
     hit keeps the existing PUSH/VOID semantics (neutral).

The triggered line is NEVER the opening line, the closing line, the
current/live line after the alert, a calculated/reference line, a league
average or a required-pace value.  The browser never re-selects it from a
line series of its own and never infers a result from a live line: it
consumes the backend's per-checkpoint block
(`under_alert_outcome.by_checkpoint[checkpoint]`), which carries the
immutable `trigger_total` (the line in force when the checkpoint was
reached — the SAME value settlement compares the final total against) and
the settled `status`.

Everything here runs against the SHIPPED API and the SHIPPED dashboard
state machine (extracted between the __PURE_ALERT__ / __ALERT_STORE__
markers and executed in Node.js), never a copy.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

import blm_v4.api as v4api
from blm_v4.api import router as v4_router
from blm_v4.live_analytics.under_outcome import outcome_status
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"
STYLES_CSS = DASH_STATIC / "styles.css"

PURE_BEGIN = "/* __PURE_ALERT_BEGIN__ */"
PURE_END = "/* __PURE_ALERT_END__ */"
STORE_BEGIN = "/* __ALERT_STORE_BEGIN__ */"
STORE_END = "/* __ALERT_STORE_END__ */"

# ── the directive's own example numbers ────────────────────────────────
TRIGGER = 187.5          # live line at the moment the alert triggered
LATER = 189.5            # where the live line moved afterwards
OPENING = 210.5          # the event's first observed line


# ══════════════════════════════════════════════════════════════════════
# 1. API — the immutable triggering line + the settled result
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def store(tmp_path, monkeypatch):
    db = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    monkeypatch.setattr(v4api, "STATE_FILE", tmp_path / "collector_state.json")
    # the league reference is a DB aggregate; served explicitly so the
    # quantitative condition is deterministic in this fixture (the trigger
    # line itself is independent of it)
    monkeypatch.setattr(v4api, "_pace_reference",
                        lambda conn: {"betual-nba": {"avg_pace": 9.0,
                                                     "games": 100}})
    st = PokerBetStore(db)
    now = datetime.now(timezone.utc)

    def iso(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def add(gid: str, home: str, away: str):
        game = PokerBetGame(
            source="PokerBet", source_game_id=gid,
            competition_id="comp-b", competition_slug="betual-nba",
            competition="Betual NBA", region="Virtual Matches",
            game_family="betual", classification="BETUAL_NBA",
            sport="basketball", home_team=home, away_team=away,
            game_slug=f"{home.lower().replace(' ', '-')}-{away.lower().replace(' ', '-')}",
            source_url=f"https://x/{gid}",
            status="live", first_seen_at=iso(now - timedelta(minutes=60)),
            last_seen_at=iso(now))
        gid_db = st.upsert_game(game)

        def snap(t: datetime, hs: int, as_: int, q: int, clock: str,
                 total: float | None, status: str = "live",
                 period_label: str | None = None):
            obs = MarketObservation(
                source="PokerBet", source_game_id=gid,
                classification="BETUAL_NBA", captured_at=iso(t),
                home_team=home, away_team=away,
                home_score=hs, away_score=as_,
                period_label=period_label or f"{q}th Quarter",
                quarter=q, clock=clock,
                game_status=status, total_line=total, spread=None,
                w1_odds=None, w2_odds=None, markets_json="{}")
            st.insert_snapshot(gid_db, obs, force=True)
        return snap
    return st, add, now, iso


def _live(rows_by_game):
    app = FastAPI()
    app.include_router(v4_router)
    client = TestClient(app)
    body = client.get("/api/v4/live").json()
    return {g["game_id"]: g for g in body["games"] if g["game_id"] in rows_by_game}


def _projector(monkeypatch, progress: float = 26.0, remaining: float = 24.0,
               actual: float = 1.5, required: float = 4.69):
    """Serve the game's clean-metrics projector row explicitly (the trigger
    line itself is independent of it): the projection is the authority for
    progress/pace, so this makes the quantitative condition deterministic
    and the alert genuinely ACTIVE."""
    def row(source_game_id):
        return {
            "source_game_id": source_game_id, "classification": "BETUAL_NBA",
            "captured_at": datetime.now(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "period_label": "2nd Quarter", "clock": "06:00",
            "elapsed_game_minutes": 40.0 - remaining,
            "remaining_game_minutes": remaining,
            "progress_pct": progress, "current_total_points": 34,
            "live_total_line": LATER, "market_status": "LIVE",
            "market_age_seconds": 30, "actual_pts_per_min": actual,
            "required_pts_per_min": required, "pace_gap": actual - required,
            "status": "VALID",
        }
    monkeypatch.setattr(v4api, "_pace_projector_for", row)


def test_api_exposes_the_immutable_triggered_line_for_an_active_alert(store,
                                                                     monkeypatch):
    """An ACTIVE alert's payload carries the line it fired against
    (187.5) — never the opening line (210.5) and never the live line that
    came later (189.5).  The alert itself is still PENDING.  It fires in
    THE production tier (directive 2026-09-14): nothing below 75% progress
    is active, and there is no actual<average leg to satisfy."""
    st, add, now, iso = store
    # the projector puts the game at 80% progress with REQUIRED above the
    # 9.0 * 1.04 margin -> ACTIVE (actual is irrelevant under this rule)
    _projector(monkeypatch, progress=80.0, actual=8.0, required=10.0)
    snap = add("5001", "Line Home Virtual", "Line Away Virtual")
    snap(now - timedelta(minutes=52), 10, 8, 1, "05:00", OPENING)    # 12.5%
    snap(now - timedelta(minutes=8), 30, 25, 2, "08:00", TRIGGER)    # 30%
    snap(now - timedelta(minutes=5), 42, 30, 2, "00:00", TRIGGER)    # 50%
    snap(now - timedelta(minutes=4), 55, 40, 3, "00:00", TRIGGER)    # 75% boundary
    snap(now - timedelta(minutes=2), 60, 44, 4, "06:00", LATER)      # 85%, line moved

    g = _live({"5001"})["5001"]
    cp = g["under_alert"]["checkpoint"]
    assert cp == 75, cp
    assert g["live"] is True and g["under_alert"]["active"] is True

    # the triggering line — the line in force when 75% was reached
    oc = g["under_alert_outcome"]
    assert oc["by_checkpoint"]["75"]["trigger_total"] == TRIGGER
    # ...and it is NOT any of the substituted values
    assert g["market"]["opening_line"] == OPENING != TRIGGER
    assert g["market"]["total_line"] == LATER != TRIGGER
    # the result is still open — the alert must render neutral
    assert oc["status"] == "pending"
    assert oc["by_checkpoint"]["75"]["status"] is None
    assert oc["final_total"] is None
    # the line that arrived LATER leaks into NO checkpoint's frozen line
    assert LATER not in {oc["by_checkpoint"][k]["trigger_total"]
                         for k in oc["by_checkpoint"]}


def test_api_triggered_line_never_moves_when_the_market_does(store,
                                                             monkeypatch):
    """187.5 at the trigger; the live line subsequently moves to 189.5 and
    then 192.5 — the 25% trigger line stays 187.5 through every poll."""
    st, add, now, iso = store
    _projector(monkeypatch)
    snap = add("5002", "Moves Home Virtual", "Moves Away Virtual")
    snap(now - timedelta(minutes=52), 10, 8, 1, "05:00", OPENING)   # 12.5%
    snap(now - timedelta(minutes=8), 30, 25, 2, "08:00", TRIGGER)   # 30% -> 25%
    snap(now - timedelta(minutes=6), 31, 26, 2, "07:00", LATER)     # 40%

    first = _live({"5002"})["5002"]
    assert first["under_alert_outcome"]["by_checkpoint"]["25"]["trigger_total"] \
        == TRIGGER
    assert first["market"]["total_line"] == LATER

    # a later market capture arrives (55%, and a new line)
    snap(now - timedelta(minutes=1), 40, 34, 3, "08:00", 192.5)
    second = _live({"5002"})["5002"]
    bc = second["under_alert_outcome"]["by_checkpoint"]
    assert second["market"]["total_line"] == 192.5
    assert bc["25"]["trigger_total"] == TRIGGER          # NOT 189.5, NOT 192.5
    # the 50% checkpoint is its OWN identity with its OWN line (192.5 at the
    # boundary) — and the 25% line is still untouched by it
    _projector(monkeypatch, progress=55.0, remaining=18.0)
    third = _live({"5002"})["5002"]
    bc = third["under_alert_outcome"]["by_checkpoint"]
    assert third["under_alert"]["checkpoint"] == 50
    assert bc["25"]["trigger_total"] == TRIGGER
    assert bc["50"]["trigger_total"] == 192.5


def test_api_settles_under_over_push_against_the_trigger_line(store):
    """Final total vs the TRIGGER line: 186 -> UNDER, 190 -> OVER, exact
    -> PUSH.  The opening / closing / later lines never enter the
    comparison."""
    st, add, now, iso = store
    # UNDER: trigger 187.5, final 186 (93 + 93)
    u = add("5101", "Under Home Virtual", "Under Away Virtual")
    u(now - timedelta(minutes=52), 10, 8, 1, "05:00", OPENING)
    u(now - timedelta(minutes=30), 40, 38, 2, "08:00", TRIGGER)
    u(now - timedelta(minutes=2), 93, 93, 4, "00:00", LATER,
      status="ended", period_label="Finished")
    # OVER: trigger 187.5, final 190 (95 + 95)
    o = add("5102", "Over Home Virtual", "Over Away Virtual")
    o(now - timedelta(minutes=52), 10, 8, 1, "05:00", OPENING)
    o(now - timedelta(minutes=30), 40, 38, 2, "08:00", TRIGGER)
    o(now - timedelta(minutes=2), 95, 95, 4, "00:00", LATER,
      status="ended", period_label="Finished")
    # PUSH: integer line, exact final (94 + 94 = 188)
    p = add("5103", "Push Home Virtual", "Push Away Virtual")
    p(now - timedelta(minutes=52), 10, 8, 1, "05:00", OPENING)
    p(now - timedelta(minutes=30), 40, 38, 2, "08:00", 188.0)
    p(now - timedelta(minutes=2), 94, 94, 4, "00:00", 190.5,
      status="ended", period_label="Finished")

    got = _live({"5101", "5102", "5103"})
    for gid, status, trig, final, latest in (
            ("5101", "under", TRIGGER, 186.0, LATER),
            ("5102", "over", TRIGGER, 190.0, LATER),
            ("5103", "push", 188.0, 188.0, 190.5)):
        g = got[gid]
        oc = g["under_alert_outcome"]
        bc = oc["by_checkpoint"]["25"]
        assert oc["status"] == "resolved", gid
        assert bc["trigger_total"] == trig, gid
        assert oc["final_total"] == final, gid
        assert bc["status"] == status, gid
        # the settled verdict is a function of the TRIGGER line only
        assert outcome_status(bc["trigger_total"], oc["final_total"]) == status
        # the opening and latest lines are DIFFERENT values and settled
        # nothing; the closing line (only defined once the game's status is
        # terminal) is never the trigger line either
        assert g["market"]["opening_line"] == OPENING != trig
        assert g["market"]["total_line"] == latest != trig
        assert g["market"]["closing_line"] != trig


def test_trigger_line_is_exposed_on_the_live_alert_and_frozen(store,
                                                              monkeypatch):
    """Directive (2026-09-13): an ACTIVE alert carries the FROZEN market total
    in force when its checkpoint was reached, ON THE ALERT ITSELF
    (under_alert.trigger_line) — never the current/opening line — and it is
    the SAME value the settlement compares the final against."""
    st, add, now, iso = store
    _projector(monkeypatch, progress=80.0, remaining=8.0, actual=8.0,
               required=10.0)
    snap = add("7001", "TL Home", "TL Away")
    snap(now - timedelta(minutes=52), 10, 8, 1, "05:00", OPENING)    # 12.5%
    snap(now - timedelta(minutes=10), 30, 25, 2, "00:00", TRIGGER)   # 50%
    snap(now - timedelta(minutes=5), 42, 34, 3, "00:00", TRIGGER)    # 75% trigger
    snap(now - timedelta(minutes=2), 48, 40, 4, "06:00", LATER)      # 85% moved

    g = _live({"7001"})["7001"]
    ua = g["under_alert"]
    assert ua["active"] is True and ua["checkpoint"] == 75
    # the frozen trigger line rides ON the alert block
    assert ua["trigger_line"] == TRIGGER
    assert ua["trigger_progress"] == 0.75
    assert ua["trigger_captured_at"]
    # identical to the settlement's line for the same checkpoint (one authority)
    bc = g["under_alert_outcome"]["by_checkpoint"]["75"]
    assert ua["trigger_line"] == bc["trigger_total"] == TRIGGER
    # the market moved AFTER the trigger: the current line is the later value,
    # a DIFFERENT field, never the trigger line
    assert g["market"]["total_line"] == LATER != ua["trigger_line"]
    assert g["market"]["opening_line"] == OPENING != ua["trigger_line"]


def test_trigger_line_on_the_live_alert_for_the_75_tier(store, monkeypatch):
    """The LATE (>=75%) tier: the alert drops the actual<average leg but
    still carries the frozen trigger line of the 75% checkpoint."""
    st, add, now, iso = store
    _projector(monkeypatch, progress=85.0, remaining=6.0, actual=8.0,
               required=10.0)
    snap = add("7002", "Late Home", "Late Away")
    snap(now - timedelta(minutes=52), 10, 8, 1, "05:00", OPENING)    # 12.5%
    snap(now - timedelta(minutes=10), 30, 25, 2, "00:00", TRIGGER)   # 50%
    snap(now - timedelta(minutes=5), 42, 34, 3, "00:00", LATER)      # 75% trigger
    snap(now - timedelta(minutes=2), 48, 40, 4, "06:00", 191.5)      # 85%

    g = _live({"7002"})["7002"]
    ua = g["under_alert"]
    assert ua["checkpoint"] == 75 and ua["active"] is True
    assert ua["trigger_line"] == LATER          # line in force when 75% was hit
    assert ua["trigger_progress"] == 0.75
    bc = g["under_alert_outcome"]["by_checkpoint"]["75"]
    assert ua["trigger_line"] == bc["trigger_total"] == LATER
    assert g["market"]["total_line"] == 191.5 != ua["trigger_line"]


def test_settlement_uses_the_frozen_trigger_line_not_the_moved_line(
        store, monkeypatch):
    """The verdict is final_total vs the FROZEN trigger line — a market that
    moved afterwards cannot change it.  Here the trigger line (187.5) and the
    moved line (185.5) straddle the final (186), so the two rules DISAGREE:
    the UNDER verdict proves the frozen trigger line was the one used."""
    st, add, now, iso = store
    _projector(monkeypatch, progress=60.0, remaining=16.0, actual=8.0,
               required=10.0)
    snap = add("7003", "Set Home", "Set Away")
    snap(now - timedelta(minutes=52), 10, 8, 1, "05:00", OPENING)    # 12.5%
    snap(now - timedelta(minutes=30), 40, 38, 2, "08:00", TRIGGER)   # 30%
    snap(now - timedelta(minutes=5), 42, 30, 2, "00:00", TRIGGER)    # 50% = 187.5
    # the market moves DOWN to 185.5 after the trigger, then the game ends 186
    snap(now - timedelta(minutes=2), 93, 93, 4, "00:00", 185.5,
         status="ended", period_label="Finished")

    g = _live({"7003"})["7003"]
    oc = g["under_alert_outcome"]
    assert oc["final_total"] == 186.0
    assert g["market"]["total_line"] == 185.5                     # the moved line
    assert oc["by_checkpoint"]["50"]["trigger_total"] == TRIGGER  # frozen
    assert oc["by_checkpoint"]["50"]["status"] == "under"         # 186 < 187.5
    # the moved line would have said OVER — proving it was NOT used
    assert outcome_status(185.5, 186.0) == "over"


def test_no_live_alert_below_the_50_progress_boundary(store, monkeypatch):
    """REGRESSION (directive 2026-09-13): nothing below 50% progress may ever
    become an active trading alert — even numbers that fully satisfy the
    quantitative condition — while every non-progress gate still passes."""
    st, add, now, iso = store
    # progress 30%: required 10.0 clears 9.0 * 1.04 and actual 8.0 < 9.0 — the
    # superseded below-both rule WOULD have alerted here.  The tiered rule
    # must NOT, purely because the game has not reached 50%.
    _projector(monkeypatch, progress=30.0, remaining=28.0, actual=8.0,
               required=10.0)
    snap = add("7004", "Early Home", "Early Away")
    snap(now - timedelta(minutes=2), 30, 25, 2, "08:00", TRIGGER)   # 30%, live

    g = _live({"7004"})["7004"]
    ua = g["under_alert"]
    assert ua["checkpoint"] == 25
    assert ua["active"] is False                 # < 50% -> no alert, ever
    # the suppression is the PROGRESS tier, not the market gate: eligibility
    # still passes, and the quantitative numbers are still served
    assert g["under_alert_eligibility"]["eligible"] is True
    assert ua["actual_pace"] == 8.0 and ua["required_pace"] == 10.0


def test_outcome_rule_directive_examples():
    """The exact directive examples, on the pure rule the API settles with."""
    assert outcome_status(TRIGGER, 186) == "under"     # GREEN
    assert outcome_status(TRIGGER, 190) == "over"      # RED
    assert outcome_status(188.0, 188.0) == "push"      # PUSH / VOID
    # an unresolved side never produces a verdict
    assert outcome_status(TRIGGER, None) is None
    assert outcome_status(None, 186) is None


# ══════════════════════════════════════════════════════════════════════
# 2. the served frontend (Node) — the SHIPPED state machine
# ══════════════════════════════════════════════════════════════════════

node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")

STUBS = """
let __T = Date.parse("2026-09-12T20:00:00Z");
Date.now = () => __T;
const __LS = {};
const localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(__LS, k) ? __LS[k] : null),
  setItem: (k, v) => { __LS[k] = String(v); },
};
const __EL = {};
function $(id) { return __EL[id] || (__EL[id] = { innerHTML: "", textContent: "" }); }
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtTime = (iso) => !iso ? "--"
  : new Date(iso).toLocaleTimeString("en-GB", { hour12: false });
function isActuallyLive(g) { return !!(g && g.live === true); }
"""

EXPORTS = """
module.exports = { UNDER_ALERTS, reconcileUnderAlerts, renderUnderAlerts,
  activeAlertsHTML, historyAlertsHTML, alertResultHTML, alertOutcomeClass,
  triggeredLineFrom, sealTriggeredLine };
"""

# a qualifying live game (server verdict supplied explicitly — the store is
# a pure consumer of it) plus the outcome-block builders used below.
FIXTURE = """
const LABELS = { "betual-nba": "NBA" };
const base = (o) => Object.assign({
  game_id: "G-A", competition_slug: "betual-nba", live: true,
  live_reason: null, alert: { eligible: true },
  under_alert_eligibility: { eligible: true, reason: "market_live" },
  home_team: "Under Home", away_team: "Under Away",
  projector: { progress_pct: 26, market_status: "LIVE", live_total_line: 189.5 },
  market: { opening_line: 210.5, closing_line: null, total_line: 189.5 },
  under_alert: { active: true, checkpoint: 25, actual_pace: 1.5,
                 required_pace: 4.69, league_average_pace: 9.0,
                 league_reference_games: 100, pace_gap: 1.5 - 4.69 },
}, o || {});
const blk = (status, trig, fin) => ({ status: status, trigger_total: trig,
  final_total: fin, resolved_at: fin == null ? null : "2026-09-12T21:00:00Z" });
const outcome = (cp, status, trig, fin) => ({
  status: fin == null ? "pending" : "resolved",
  final_total: fin, resolved_at: fin == null ? null : "2026-09-12T21:00:00Z",
  by_checkpoint: Object.assign(blk(null, null, null),
    { [cp]: blk(status, trig, fin) }) });
// the payload as the API serves it: outcome block keyed by checkpoint
const game = (cp, status, trig, fin, o) => base(Object.assign({
  under_alert_outcome: outcome(cp, status, trig, fin),
}, o || {}));
"""


def _js() -> str:
    app = FastAPI()
    app.include_router(v4_router)
    app.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
              name="triggered_line_static")

    @app.get("/", include_in_schema=False)
    async def operator_dashboard():
        return FileResponse(str(DASH_STATIC / "index.html"))
    return TestClient(app).get("/static/dashboard.js").text


def _block(js: str, begin: str, end: str) -> str:
    i = js.index(begin) + len(begin)
    return js[i:js.index(end, i)]


def _run(js: str, tmp_path: Path, script: str):
    mod = tmp_path / "triggered_line_store.js"
    mod.write_text(STUBS + _block(js, PURE_BEGIN, PURE_END)
                   + _block(js, STORE_BEGIN, STORE_END) + EXPORTS,
                   encoding="utf-8")
    code = (f"const m = require({json.dumps(str(mod))});\n"
            f"const OUT = {{}};\nconst G = {{}};\n{FIXTURE}\n{script}\n"
            "console.log(JSON.stringify(Object.assign({"
            "activeSize: m.UNDER_ALERTS.active.size,"
            "history: m.UNDER_ALERTS.history,"
            "actHTML: m.activeAlertsHTML(),"
            "histHTML: m.historyAlertsHTML()}, OUT)));")
    out = subprocess.run(["node", "-e", code], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout)


@node
def test_trigger_line_is_read_from_the_live_alert_block(tmp_path):
    """The alert's OWN frozen field (under_alert.trigger_line) is the
    authority the ACTIVE row shows — the directive's example: Trigger Line
    160.5 while the moved market reads 162.5 as the separate Current Line.
    The settlement block's line is deliberately absent here, so 160.5 can
    only have come from the live alert itself."""
    js = _js()
    r = _run(js, tmp_path, """
      m.reconcileUnderAlerts([game(50, null, null, null, {
        market: { opening_line: 210.5, closing_line: null, total_line: 162.5 },
        under_alert: Object.assign({}, base().under_alert, {
          checkpoint: 50, trigger_line: 160.5, trigger_progress: 0.5,
          trigger_captured_at: "2026-09-13T10:00:00Z" }) })], LABELS);
      G.act = m.UNDER_ALERTS.active.get("G-A|50");
      OUT.line = G.act.triggered_line;
      OUT.recLine = m.UNDER_ALERTS.history[0].triggered_line;
      OUT.cur = G.act.current_line;
      OUT.html = m.activeAlertsHTML();
    """)
    assert r["line"] == 160.5, r          # the frozen trigger line, from the alert
    assert r["recLine"] == 160.5, r       # sealed onto the record too
    assert r["cur"] == 162.5, r           # the moved market, shown separately
    assert ('Triggered Line: <span class="al-trigger-line">160.5</span>'
            in r["html"]), r["html"]
    assert 'Current Line: <span class="al-num">162.5</span>' in r["html"], r["html"]


@node
def test_active_view_shows_the_triggered_line_not_the_later_line(tmp_path):
    """The alert triggers at 187.5 while the live market already reads
    189.5: the ACTIVE row's Triggered Line stays 187.5 (frozen) and the
    later live line is shown separately, ONLY as the Current Line — never
    substituted for the trigger line.  The opening line appears nowhere."""
    js = _js()
    r = _run(js, tmp_path, """
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      G.act = m.UNDER_ALERTS.active.get("G-A|25");
      OUT.line = G.act.triggered_line;
      OUT.recLine = m.UNDER_ALERTS.history[0].triggered_line;
      OUT.hasTriggeredLabel = m.activeAlertsHTML().indexOf("Triggered Line:") !== -1;
      OUT.triggerRow = m.activeAlertsHTML()
        .indexOf('Triggered Line: <span class="al-trigger-line">187.5</span>') !== -1;
      OUT.currentRow = m.activeAlertsHTML()
        .indexOf('Current Line: <span class="al-num">189.5</span>') !== -1;
      OUT.shows210 = m.activeAlertsHTML().indexOf("210.5") !== -1;
    """)
    assert r["line"] == 187.5, r
    assert r["recLine"] == 187.5, r          # sealed onto the record too
    assert r["hasTriggeredLabel"] is True, r
    assert r["triggerRow"] is True, r        # the trigger line is 187.5
    assert r["currentRow"] is True, r        # 189.5 shows ONLY as Current Line
    assert r["shows210"] is False, r         # nor the opening line
    assert "Triggered Line:" in r["actHTML"]


@node
def test_triggered_line_is_immutable_under_later_market_movement(tmp_path):
    """Directive data-integrity test: 187.5 -> 189.5 changes nothing.  Even
    when a later payload reports a different line for the same checkpoint,
    the record keeps the ORIGINAL triggering line."""
    js = _js()
    r = _run(js, tmp_path, """
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      // the market moves: live line 191.5, and the payload now (wrongly)
      // reports 189.5 for the same checkpoint
      const moved = game(25, null, 189.5, null, {
        market: { opening_line: 210.5, closing_line: null, total_line: 191.5 },
        projector: { progress_pct: 30, market_status: "LIVE",
                     live_total_line: 191.5 } });
      m.reconcileUnderAlerts([moved], LABELS);
      m.reconcileUnderAlerts([moved], LABELS);
      G.act = m.UNDER_ALERTS.active.get("G-A|25");
      OUT.line = G.act.triggered_line;
      OUT.recLine = m.UNDER_ALERTS.history[0].triggered_line;
      OUT.html = m.activeAlertsHTML();
      OUT.activeSize = m.UNDER_ALERTS.active.size;
      OUT.historySize = m.UNDER_ALERTS.history.length;
    """)
    assert r["line"] == 187.5, r
    assert r["recLine"] == 187.5, r
    # the FROZEN trigger line is untouched by the move...
    assert ('Triggered Line: <span class="al-trigger-line">187.5</span>'
            in r["html"]), r["html"]
    # ...the wrongly-reported checkpoint line (189.5) never appears at all...
    assert "189.5" not in r["html"], r["html"]
    # ...and the moved live market shows ONLY as the separate Current Line
    assert 'Current Line: <span class="al-num">191.5</span>' in r["html"], r["html"]
    # one record per identity, EVER — no duplicate from the refresh
    assert r["historySize"] == 1 and r["activeSize"] == 1, r


@node
def test_pending_alert_is_neutral_never_coloured(tmp_path):
    """An unresolved alert shows PENDING, carries no colour class, and is
    never labelled with a verdict."""
    js = _js()
    r = _run(js, tmp_path, """
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      const a = m.activeAlertsHTML();
      OUT.html = a;
      OUT.orClass = m.alertOutcomeClass(m.UNDER_ALERTS.history[0].outcome);
      OUT.pending = m.alertResultHTML(m.UNDER_ALERTS.history[0]);
    """)
    assert "PENDING" in r["html"], r["html"]
    assert "al-pending" in r["html"], r["html"]
    for banned in ("al-under", "al-over", "al-push"):
        assert banned not in r["html"], banned
    assert r["orClass"] is None, r
    assert "al-pending" in r["pending"] and "al-outcome" not in r["pending"], r


@node
def test_settled_result_renders_under_green_and_over_red(tmp_path):
    """Final total vs the TRIGGER line: 186 -> UNDER (green, al-under),
    190 -> OVER (red, al-over).  The value is shown against the sealed
    triggered line, not against any live line."""
    js = _js()
    r = _run(js, tmp_path, """
      m.reconcileUnderAlerts([
        game(25, "under", 187.5, 186),
        game(25, "over", 192.5, 190, { game_id: "G-B" }),
      ], LABELS);
      const underRec = m.UNDER_ALERTS.active.get("G-A|25");
      OUT.both = m.activeAlertsHTML();
      OUT.underRecLine = underRec.triggered_line;
      OUT.underRecOutcome = underRec.outcome.status;
    """)
    html = r["both"]
    # the settled UNDER row: green class, the word, the final total and the
    # value it was measured against (its own triggered line)
    assert 'class="al-row al-under"' in html, html
    assert ">UNDER<" in html and "al-under" in html, html
    assert "186.0" in html and "187.5" in html, html
    # the settled OVER row: red class, the word, its own line
    assert 'class="al-row al-over"' in html, html
    assert ">OVER<" in html and "al-over" in html, html
    assert "190.0" in html and "192.5" in html, html
    assert r["underRecLine"] == 187.5 and r["underRecOutcome"] == "under", r


@node
def test_exact_line_settles_push_neutral(tmp_path):
    """final == triggered line keeps the existing PUSH/VOID semantics: the
    neutral class, the PUSH word, and no UNDER/OVER colour at all."""
    js = _js()
    r = _run(js, tmp_path, """
      m.reconcileUnderAlerts([game(25, "push", 188, 188)], LABELS);
      OUT.html = m.activeAlertsHTML();
      OUT.cls = m.alertOutcomeClass(m.UNDER_ALERTS.active.get("G-A|25").outcome);
    """)
    assert r["cls"] == "al-push", r
    assert "al-push" in r["html"] and "PUSH" in r["html"], r["html"]
    assert "al-under" not in r["html"] and "al-over" not in r["html"], r["html"]
    assert "188.0" in r["html"], r["html"]


@node
def test_each_alert_keeps_its_own_triggered_line(tmp_path):
    """Multiple alerts (two games, two checkpoints on one game) each keep
    and render their OWN triggering line — never a shared/latest value."""
    js = _js()
    r = _run(js, tmp_path, """
      m.reconcileUnderAlerts([
        game(25, null, 187.5, null),
        game(50, null, 192.5, null, { game_id: "G-B",
          projector: { progress_pct: 55, market_status: "LIVE",
                       live_total_line: 195.5 },
          under_alert: Object.assign({}, base().under_alert,
            { checkpoint: 50 }) }),
      ], LABELS);
      OUT.recs = m.UNDER_ALERTS.history.map((x) => [x.id, x.triggered_line]);
      OUT.act = m.activeAlertsHTML();
    """)
    assert sorted(r["recs"]) == [["G-A|25", 187.5], ["G-B|50", 192.5]], r["recs"]
    assert "187.5" in r["act"] and "192.5" in r["act"], r["act"]
    assert "195.5" not in r["act"], r["act"]     # each row its own line only
    assert r["act"].count("Triggered Line:") == 2, r["act"]


@node
def test_line_is_filled_only_when_provable_and_never_replaced(tmp_path):
    """A record whose line the backend cannot yet prove shows "–" (never a
    fabricated value); once provable it is filled ONCE and never rewritten."""
    js = _js()
    r = _run(js, tmp_path, """
      m.reconcileUnderAlerts([game(25, null, null, null, {
        under_alert_outcome: { status: "pending" },
      })], LABELS);
      OUT.blank = m.activeAlertsHTML();
      OUT.blankLine = m.UNDER_ALERTS.active.get("G-A|25").triggered_line;
      // the backend proves it on a later poll
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      OUT.filled = m.UNDER_ALERTS.active.get("G-A|25").triggered_line;
      // ...and can never change it afterwards
      m.reconcileUnderAlerts([game(25, null, 189.5, null)], LABELS);
      OUT.final = m.UNDER_ALERTS.active.get("G-A|25").triggered_line;
      OUT.recFinal = m.UNDER_ALERTS.history[0].triggered_line;
    """)
    assert r["blankLine"] is None, r
    assert "Triggered Line:" in r["blank"] and "–" in r["blank"], r["blank"]
    assert r["filled"] == 187.5 and r["final"] == 187.5, r
    assert r["recFinal"] == 187.5, r


@node
def test_triggered_line_travels_with_the_resolved_record(tmp_path):
    """Resolution seals the order: the trigger line is not overwritten by
    resolution values, and the settled record keeps it for history."""
    js = _js()
    r = _run(js, tmp_path, """
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      m.reconcileUnderAlerts([game(25, "under", 187.5, 186,
        { live: false, live_reason: "game_finished", status: "ended",
          under_alert: Object.assign({}, base().under_alert,
            { active: false }) })], LABELS);
      OUT.rec = m.UNDER_ALERTS.history[0];
      OUT.activeSize = m.UNDER_ALERTS.active.size;
      OUT.histHTML = m.historyAlertsHTML();
    """)
    assert r["activeSize"] == 0, r
    assert r["rec"]["triggered_line"] == 187.5, r["rec"]
    assert r["rec"]["outcome"]["status"] == "under", r["rec"]
    assert "al-under" in r["histHTML"] and "UNDER" in r["histHTML"], r


# ══════════════════════════════════════════════════════════════════════
# 3. the sealed-outcome guard (applyFinalOutcomes) — regression surface
# ══════════════════════════════════════════════════════════════════════

@node
def test_settled_verdict_is_never_overwritten_by_later_polls(tmp_path):
    """The guard's PRIMARY purpose: once an outcome is SETTLED it is
    immutable — a later payload carrying a different verdict cannot rewrite
    it, the trigger line cannot be restated, and the ACTIVE row (which reads
    the sealed record) cannot disagree with the HISTORY row."""
    js = _js()
    r = _run(js, tmp_path, """
      m.reconcileUnderAlerts([game(25, "under", 187.5, 186)], LABELS);
      const settled = m.UNDER_ALERTS.history[0].outcome.status;
      // a later poll claims the opposite verdict and a different line
      m.reconcileUnderAlerts([game(25, "over", 189.5, 190)], LABELS);
      const after = m.UNDER_ALERTS.history[0];
      OUT.settled = settled;
      OUT.status = after.outcome.status;
      OUT.final = after.outcome.final_total;
      OUT.line = after.triggered_line;
      OUT.histSize = m.UNDER_ALERTS.history.length;
      OUT.act = m.activeAlertsHTML();
      OUT.actOutcome = m.UNDER_ALERTS.active.get("G-A|25").outcome.status;
    """)
    assert r["settled"] == "under" and r["status"] == "under", r
    assert r["final"] == 186.0, r            # the settled final is sealed
    assert r["line"] == 187.5, r             # ...and so is the line
    assert r["histSize"] == 1, r             # no duplicate record
    # the ACTIVE row reads the SEALED record — it cannot show the later
    # payload's contradicting verdict
    assert r["actOutcome"] == "under", r
    assert "al-under" in r["act"] and "al-over" not in r["act"], r["act"]
    # the trigger line stays the SEALED 187.5 — the later payload's 189.5 can
    # only ever surface as the moving Current Line, never as the trigger line
    assert ('Triggered Line: <span class="al-trigger-line">187.5</span>'
            in r["act"]), r["act"]
    assert 'Current Line: <span class="al-num">189.5</span>' in r["act"], r["act"]


@node
def test_an_alert_that_closes_before_its_game_ends_still_reaches_a_verdict(
        tmp_path):
    """The guard must NOT strand a record: an alert that leaves the active
    store while the game is still running seals an UNSETTLED block, and it
    is settled from the payload once the backend proves a final.  Without
    the guard's status-based test this record would stay colourless for
    ever."""
    js = _js()
    r = _run(js, tmp_path, """
      // true at the 25% checkpoint; the alert triggers, then the condition
      // goes false while the game is still live (nothing settled yet)
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      m.reconcileUnderAlerts([game(25, null, 187.5, null,
        { under_alert: Object.assign({}, base().under_alert,
            { active: false }) })], LABELS);
      OUT.midStatus = m.UNDER_ALERTS.history[0].outcome
        && m.UNDER_ALERTS.history[0].outcome.status;
      OUT.midActive = m.UNDER_ALERTS.active.size;
      // the game finishes: the backend now settles the same checkpoint
      m.reconcileUnderAlerts([game(25, "under", 187.5, 186,
        { live: false, live_reason: "game_finished", status: "ended",
          under_alert: Object.assign({}, base().under_alert,
            { active: false }) })], LABELS);
      OUT.rec = m.UNDER_ALERTS.history[0];
      OUT.histHTML = m.historyAlertsHTML();
    """)
    # resolved before the game ended, with no verdict yet — honest, neutral
    assert r["midActive"] == 0, r
    assert r["midStatus"] is None, r
    # ...and the verdict arrives afterwards, sealed onto the same record
    assert r["rec"]["outcome"]["status"] == "under", r["rec"]
    assert r["rec"]["outcome"]["final_total"] == 186.0, r["rec"]
    assert r["rec"]["triggered_line"] == 187.5, r["rec"]
    assert "al-under" in r["histHTML"] and "UNDER" in r["histHTML"], r


@node
def test_settling_one_record_cannot_suppress_another_checkpoint_or_game(
        tmp_path):
    """The guard is per-RECORD (game_id|checkpoint) and only ever writes the
    outcome/line: a settled 25% record must not prevent the 50% checkpoint
    opening, nor another game alerting, and must not remove any active
    alert."""
    js = _js()
    r = _run(js, tmp_path, """
      // game A: 25% settles as UNDER
      m.reconcileUnderAlerts([game(25, "under", 187.5, 186)], LABELS);
      // game A advances into the 50% phase — the server reports the highest
      // checkpoint reached, so the payload now carries cp=50 only
      m.reconcileUnderAlerts([game(50, null, 192.5, null, {
        under_alert: Object.assign({}, base().under_alert,
          { checkpoint: 50 }) })], LABELS);
      // a second game alerts (its own identity), both in one poll
      m.reconcileUnderAlerts([
        game(50, null, 192.5, null, {
          under_alert: Object.assign({}, base().under_alert,
            { checkpoint: 50 }) }),
        game(25, null, 210.5, null, { game_id: "G-B" }),
      ], LABELS);
      OUT.ids = m.UNDER_ALERTS.history.map((x) => x.id).sort();
      OUT.actIds = Array.from(m.UNDER_ALERTS.active.keys()).sort();
      OUT.act = m.activeAlertsHTML();
      OUT.a25 = m.UNDER_ALERTS.history.find((x) => x.id === "G-A|25").outcome.status;
      OUT.a50 = m.UNDER_ALERTS.history.find((x) => x.id === "G-A|50")
        .outcome.status;
    """)
    # the new checkpoint and the new game BOTH exist and are ACTIVE
    assert r["ids"] == ["G-A|25", "G-A|50", "G-B|25"], r["ids"]
    assert r["actIds"] == ["G-A|50", "G-B|25"], r["actIds"]
    assert "al-under" not in r["act"], r["act"]   # settled 25% is not active
    assert "al-pending" in r["act"], r["act"]
    assert r["act"].count("Triggered Line:") == 2, r["act"]
    assert "192.5" in r["act"] and "210.5" in r["act"], r["act"]
    # each record keeps its own settlement state and its own line
    assert r["a25"] == "under" and r["a50"] is None, r


@node
def test_steady_state_polling_keeps_the_alert_and_never_duplicates(tmp_path):
    """Repeated identical polls (the reconciliation loop) must keep exactly
    one record and one active alert — no stale drop, no duplicate."""
    js = _js()
    r = _run(js, tmp_path, """
      for (let i = 0; i < 6; i += 1) {
        m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      }
      OUT.histSize = m.UNDER_ALERTS.history.length;
      OUT.actSize = m.UNDER_ALERTS.active.size;
      OUT.line = m.UNDER_ALERTS.history[0].triggered_line;
      OUT.activeStill = m.UNDER_ALERTS.active.has("G-A|25");
    """)
    assert r["histSize"] == 1 and r["actSize"] == 1, r
    assert r["line"] == 187.5, r
    assert r["activeStill"] is True, r


@node
def test_live_to_ended_transition_closes_active_and_keeps_the_settlement(
        tmp_path):
    """LIVE → ENDED: the active alert leaves the ACTIVE surface, history is
    retained and settled, and the settled record is not re-coloured by a
    further poll."""
    js = _js()
    r = _run(js, tmp_path, """
      m.reconcileUnderAlerts([game(25, null, 187.5, null)], LABELS);
      const fin = game(25, "over", 187.5, 190, {
        live: false, live_reason: "game_finished", status: "ended",
        under_alert: Object.assign({}, base().under_alert,
          { active: false }) });
      m.reconcileUnderAlerts([fin], LABELS);
      OUT.afterEnd = { act: m.UNDER_ALERTS.active.size,
                       hist: m.UNDER_ALERTS.history.length,
                       status: m.UNDER_ALERTS.history[0].outcome.status,
                       line: m.UNDER_ALERTS.history[0].triggered_line };
      // the game is gone from the payload entirely — history must persist
      m.reconcileUnderAlerts([], LABELS);
      OUT.afterGone = { act: m.UNDER_ALERTS.active.size,
                        hist: m.UNDER_ALERTS.history.length,
                        status: m.UNDER_ALERTS.history[0].outcome.status,
                        histHTML: m.historyAlertsHTML() };
    """)
    assert r["afterEnd"]["act"] == 0 and r["afterEnd"]["hist"] == 1, r
    assert r["afterEnd"]["status"] == "over", r
    assert r["afterEnd"]["line"] == 187.5, r
    assert r["afterGone"]["hist"] == 1, r
    assert r["afterGone"]["status"] == "over", r
    assert "al-over" in r["afterGone"]["histHTML"], r


def test_outcome_delivery_never_touches_the_active_store():
    """Source-level: the sealed-outcome guard lives in applyFinalOutcomes,
    which only writes outcome/triggered_line onto history records.  It has
    no access to the active store, so it can neither drop a live alert nor
    keep a stale one standing — the alert lifecycle stays owned by
    reconcileUnderAlerts' server-verdict gate."""
    js = _js()
    i = js.index("function applyFinalOutcomes(games) {")
    depth, end = 0, None
    for k in range(js.index("{", i), len(js)):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                end = k + 1
                break
    body = js[i:end]
    # the immutability rule is the ONE sealOutcome, not an inline test that
    # could drift from the other call sites
    assert "sealOutcome(r, merged)" in body
    assert "function sealOutcome(rec, candidate)" in js
    assert "if (rec.outcome && rec.outcome.status != null) return false;" in js
    for banned in (".active", "UNDER_ALERTS.active", "active.delete",
                   "active.set", "closeUnderAlert"):
        assert banned not in body, banned
    # and the reconciliation gate itself is unchanged: the alert is the
    # server's authoritative verdict and NOTHING else — never the outcome
    # block, never a second eligibility decision
    # AMENDMENT (audit 2026-09-16 §9): the gate may also refuse to
    # resurrect an ACTIVE record whose game the payload's authoritative
    # state marks over (gameOverIds is built from g.status / g.live /
    # g.live_reason only — never from an outcome block, never a
    # re-derived condition). It can only ever CLOSE, never open.  This is
    # lifecycle reconciliation, distinct from the eligibility second-gate
    # requirement N forbids.
    assert "const ok = cp != null && ua.active === true && !gameOver;" in js
    assert "const gameOver = gameOverIds.has(g.game_id);" in js
    # ONE authority for the sealed values: every poll ends by re-publishing
    # the RECORD's line + verdict onto the active rows, so the two surfaces
    # cannot disagree about the same alert
    assert "function syncActiveSealed()" in js
    assert "act.outcome = rec.outcome || null;" in js
    assert "syncActiveSealed();" in js


# ══════════════════════════════════════════════════════════════════════
# 4. source-level guards — the one definition, and no reconstruction
# ══════════════════════════════════════════════════════════════════════

def test_frontend_reads_the_canonical_per_checkpoint_line():
    """The browser takes the line from the backend's own immutable
    per-checkpoint block — it never re-selects it from a line series."""
    js = _js()
    store = _block(js, STORE_BEGIN, STORE_END)
    for needed in ("triggeredLineOf", "triggeredLineFrom", "sealTriggeredLine",
                   "triggered_line", "under_alert_outcome", "by_checkpoint",
                   "trigger_total"):
        assert needed in store, needed
    # no second definition of the line is invented browser-side
    for banned in ("opening_line", "closing_line", "market.total_line",
                   "live_total_line", "league_average_pace:",
                   "required_pace:"):
        assert f"triggered_line = {banned}" not in store, banned


def test_frontend_never_infers_the_result_from_a_line_comparison():
    """The result is consumed from the settled status — the browser never
    compares lines and never derives a verdict itself."""
    js = _js()
    store = _block(js, STORE_BEGIN, STORE_END)
    for banned in ("final_total <", "final_total >", "trigger_total <",
                   "trigger_total >", "triggered_line <", "triggered_line >",
                   "actual < required"):
        assert banned not in store, banned
    assert "ALERT_RESULT_WORDS" in store and "function alertResultHTML" in js
    # the verdict word comes from ONE map, shared with the history line
    assert store.count("under: \"UNDER\"") == 1


def test_active_surface_consumes_the_result_renderer():
    """The ACTIVE component renders the shared result renderer and the
    triggered line; its own region reconstructs nothing."""
    js = _js()
    active = js[js.index("activeAlertsHTML"):js.index("historyAlertsHTML")]
    assert "alertResultHTML(a)" in active
    assert "Triggered Line:" in active
    assert "triggered_line" in active
    assert "PENDING" not in active.split("RESULTED-PANEL FILTERS")[0]
        # the word lives in the renderer (and the filter bar's option list)
    assert "alertOutcomeClass(a.outcome)" in active


def test_result_state_styles_are_green_red_and_neutral():
    css = STYLES_CSS.read_text(encoding="utf-8")
    assert ".al-row.al-under .al-result { color: #22c55e; }" in css     # GREEN
    assert ".al-row.al-over .al-result { color: #ef4444; }" in css      # RED
    assert ".al-line .al-pending" in css                                # NEUTRAL
    assert ".al-trigger-line" in css                                    # prominent
