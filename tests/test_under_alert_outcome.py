"""UNDER ALERT final outcome — trigger-total settlement + history coloring.

Directive "UNDER ALERT HISTORY — FINAL OUTCOME COLORING" (2026-09-12).

Proves, against the SHIPPED backend and the SHIPPED dashboard state machine
(extracted between the __ALERT_STORE__ markers and run in Node.js):

  * outcome rule: final < trigger -> UNDER (green), final > trigger -> OVER
    (red), final == trigger -> PUSH (neutral)
  * the comparison uses ONLY the market total captured at the checkpoint the
    alert fired at — opening / closing / later lines can never leak in
  * later market movement never rewrites a settled record
  * multiple checkpoints on one game stay independent
  * still-active alerts carry no outcome (no coloring)
  * finished games settle their remaining records; history survives the
    game leaving /api/v4/live
  * the frontend renders the backend verdict verbatim — it never
    reconstructs the quantitative condition
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
from blm_v4.live_analytics.under_outcome import (
    final_total_for, outcome_status, resolved_at_for, trigger_market_total,
    under_alert_outcome,
)
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"

PURE_BEGIN = "/* __PURE_ALERT_BEGIN__ */"
PURE_END = "/* __PURE_ALERT_END__ */"
STORE_BEGIN = "/* __ALERT_STORE_BEGIN__ */"
STORE_END = "/* __ALERT_STORE_END__ */"

node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")


# ══════════════════════════════════════════════════════════════════════
# Backend pure rules
# ══════════════════════════════════════════════════════════════════════

def test_outcome_rule_under_over_push():
    # 180.5 trigger vs the three finals from the directive examples
    assert outcome_status(180.5, 175.0) == "under"    # GREEN
    assert outcome_status(180.5, 185.0) == "over"     # RED
    assert outcome_status(180.5, 180.5) == "push"     # neutral
    # half-point lines can never push against integer finals and vice versa
    assert outcome_status(180.5, 180.0) == "under"
    assert outcome_status(180.0, 180.5) == "over"


def test_outcome_rule_unprovable_inputs_are_none():
    assert outcome_status(None, 180.0) is None
    assert outcome_status(180.5, None) is None
    assert outcome_status("x", 180.0) is None


def _row(minutes_elapsed: float, line: float | None, hs: int, as_: int,
         captured_at: str, q: int = 1, clock: str = "05:00",
         status: str = "live", classification: str = "BETUAL_NBA") -> dict:
    """A snapshot row shaped like the store's, with BETUAL 40' timing."""
    remaining = 40.0 - minutes_elapsed
    return {
        "classification": classification,
        "captured_at": captured_at,
        "quarter": q, "clock": clock, "period_label": f"{q}th Quarter",
        "game_status": status,
        "total_line": line, "home_score": hs, "away_score": as_,
        # fields row_elapsed_minutes derives its own way; supply precomputed
        # elapsed via the clock it parses: Q1 05:00 of a 10' quarter.
        "_elapsed": minutes_elapsed,
        "_remaining": remaining,
    }


def _rows_for_timeline():
    """A BETUAL 40-minute game: trigger lines at 25/50/75 + a final row.

    Line history: 180.5 until 25%, 176.5 at 50%, 174.5 at 75%, final 176.
    """
    t0 = datetime(2026, 9, 12, 19, 0, tzinfo=timezone.utc)

    def ts(minute: float) -> str:
        return (t0 + timedelta(minutes=minute)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")

    def clock_for(elapsed: float, quarter_minutes: float = 10.0) -> tuple:
        # full time is carried by the Q4 00:00 sentinel row (quarter 5
        # would be unparseable by row_elapsed_minutes — by design)
        q = min(4, int(elapsed // quarter_minutes) + 1)
        rem_in_q = quarter_minutes - (elapsed % quarter_minutes)
        if elapsed >= 40.0:
            return 4, "00:00"
        return q, f"{int(rem_in_q):02d}:00"

    rows = []
    # elapsed minutes -> (line, home, away)
    plan = [(2.0, 180.5, 4, 3), (9.5, 180.5, 18, 15),      # Q1
            (10.5, 180.5, 24, 20),                          # 25%+
            (20.5, 176.5, 45, 40),                          # 50%+
            (30.5, 174.5, 66, 58),                          # 75%+
            (40.0, 174.5, 88, 88)]                          # final (176)
    for elapsed, line, hs, as_ in plan:
        q, clock = clock_for(elapsed)
        rows.append({
            "classification": "BETUAL_NBA",
            "captured_at": ts(elapsed),
            "quarter": q, "clock": clock,
            "period_label": f"{q}th Quarter",
            "game_status": "ended" if elapsed >= 40.0 else "live",
            "total_line": line, "home_score": hs, "away_score": as_,
        })
    return rows


def test_trigger_total_is_the_line_at_the_checkpoint():
    rows = _rows_for_timeline()
    assert trigger_market_total(rows, 25) == 180.5   # line live at 25%
    assert trigger_market_total(rows, 50) == 176.5   # NOT 180.5, NOT 174.5
    assert trigger_market_total(rows, 75) == 174.5


def test_trigger_total_carries_the_line_in_force_at_the_boundary():
    """WS/list rows carry no line; the bookmaker line persists between
    captures.  The trigger is the line in force at the boundary — the
    boundary row's own capture if present, else the most recent line
    OBSERVED AT OR BEFORE it.  Nothing captured AFTER the boundary can
    ever leak in."""
    rows = _rows_for_timeline()
    # line observed only up to 25%+ boundary, then a capture gap until the
    # terminal row: the 50% boundary (first reached during the gap) keeps
    # the line in force there — the pre-gap capture 180.5, NOT the closing
    # 174.5 which was captured AFTER the boundary.
    rows_gap = [dict(r, total_line=None) for r in rows[3:]]
    rows_gap = rows[:3] + rows_gap
    assert trigger_market_total(rows_gap, 25) == 180.5   # own capture
    assert trigger_market_total(rows_gap, 50) == 180.5   # carried, not 174.5
    assert trigger_market_total(rows_gap, 75) == 180.5   # carried, not 174.5


def test_trigger_total_stays_none_before_any_market_capture():
    """No line observed at-or-before the boundary -> unprovable (None):
    neither the later (opening-relative) capture nor any other line
    substitutes."""
    rows = _rows_for_timeline()
    # lines exist only from the 50% boundary onward: the 25% boundary is
    # first reached at a row with no line, with no earlier line either
    rows_late = [dict(r, total_line=None) for r in rows[:3]] + rows[3:]
    assert trigger_market_total(rows_late, 25) is None
    assert trigger_market_total(rows_late, 50) == 176.5  # own capture works


def test_final_total_only_from_terminal_rows():
    rows = _rows_for_timeline()
    live_rows = [r for r in rows if r["game_status"] == "live"]
    assert final_total_for(live_rows) is None          # game still live
    assert final_total_for(rows) == 176.0              # 88 + 88


def test_settlement_block_per_checkpoint_independent():
    rows = _rows_for_timeline()
    oc = under_alert_outcome(rows)
    assert oc["final_total"] == 176.0
    assert oc["status"] == "resolved"
    bc = oc["by_checkpoint"]
    # 25% trigger 180.5 vs final 176 -> UNDER (green)
    assert bc[25] == {"status": "under", "trigger_total": 180.5,
                      "final_total": 176.0, "resolved_at": bc[25]["resolved_at"]}
    # 50% trigger 176.5 vs final 176 -> UNDER (green)
    assert bc[50]["status"] == "under"
    assert bc[50]["trigger_total"] == 176.5
    # 75% trigger 174.5 vs final 176 -> OVER (red) — INDEPENDENT of the others
    assert bc[75]["status"] == "over"
    assert bc[75]["trigger_total"] == 174.5
    # resolved_at is the terminal observation's captured_at
    assert bc[75]["resolved_at"] == rows[-1]["captured_at"]


def test_pending_game_has_no_outcome():
    rows = [r for r in _rows_for_timeline() if r["game_status"] == "live"]
    oc = under_alert_outcome(rows)
    assert oc["status"] == "pending"
    assert oc["final_total"] is None
    for cp in (25, 50, 75):
        assert oc["by_checkpoint"][cp]["status"] is None


def test_settled_result_is_the_authoritative_final():
    """A settled game_results record (backend game-final data) takes
    precedence; without one the terminal observation still settles."""
    rows = _rows_for_timeline()
    # settled OK row wins even though observations also carry a terminal
    assert final_total_for(rows, settled=(177.0, "2026-09-12T21:00:00Z")) == 177.0
    assert resolved_at_for(rows, settled=(177.0, "2026-09-12T21:00:00Z")) \
        == "2026-09-12T21:00:00Z"
    # fallback unchanged: no settled record -> terminal observation
    assert final_total_for(rows) == 176.0
    # a game with NO terminal row settles from the settled record alone
    live_rows = [r for r in rows if r["game_status"] == "live"]
    assert final_total_for(live_rows) is None
    assert final_total_for(live_rows, settled=(176.0, "2026-09-12T21:05:00Z")) \
        == 176.0
    # non-OK settled data is never passed in by the caller (status filtered
    # upstream) and a non-numeric final_total stays honest None
    assert final_total_for(live_rows, settled=(None, "x")) is None


def test_outcome_block_settled_record_settles_unprovable_finals():
    """A finished game whose snapshots lack a terminal row still settles
    every checkpoint once the backend's settled final exists — status,
    final_total and resolved_at all come from the authoritative record."""
    rows = [r for r in _rows_for_timeline() if r["game_status"] == "live"]
    oc = under_alert_outcome(rows, settled=(175.0, "2026-09-12T21:00:00Z"))
    assert oc["status"] == "resolved"
    assert oc["final_total"] == 175.0
    assert oc["resolved_at"] == "2026-09-12T21:00:00Z"
    bc = oc["by_checkpoint"]
    assert bc[25]["status"] == "under"     # 180.5 -> 175
    assert bc[50]["status"] == "under"     # 176.5 -> 175
    assert bc[75]["status"] == "over"      # 174.5 -> 175
    assert bc[75]["trigger_total"] == 174.5  # trigger source unchanged


# ══════════════════════════════════════════════════════════════════════
# API integration — the payload carries the sibling outcome block
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def store(tmp_path, monkeypatch):
    db = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    monkeypatch.setattr(v4api, "STATE_FILE", tmp_path / "collector_state.json")
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
                # the collector's real finished representation is the
                # finished period label — "ended" status alone with live
                # game-time fields is deliberately NOT terminal evidence
                period_label=period_label or f"{q}th Quarter",
                quarter=q, clock=clock,
                game_status=status, total_line=total, spread=None,
                w1_odds=None, w2_odds=None, markets_json="{}")
            st.insert_snapshot(gid_db, obs, force=True)
        return snap
    return st, add, now, iso


def test_api_serves_outcome_block(store):
    st, add, now, iso = store
    snap = add("4001", "Outcome Home Virtual", "Outcome Away Virtual")
    # trigger 180.5 at 25%+ (elapsed > 10 of 40), line moves, final 175 UNDER
    snap(now - timedelta(minutes=48), 20, 18, 2, "07:00", 180.5)
    snap(now - timedelta(minutes=30), 40, 38, 3, "08:00", 176.5)
    snap(now - timedelta(minutes=2), 88, 87, 4, "00:00", 174.5,
         status="ended", period_label="Finished")

    app = FastAPI()
    app.include_router(v4_router)
    client = TestClient(app)
    body = client.get("/api/v4/live").json()
    g = next(g for g in body["games"] if g["game_id"] == "4001")
    oc = g["under_alert_outcome"]
    assert oc["status"] == "resolved"
    assert oc["final_total"] == 175.0
    bc = oc["by_checkpoint"]
    assert bc["25"]["status"] == "under"
    assert bc["25"]["trigger_total"] == 180.5
    assert bc["50"]["trigger_total"] == 176.5
    assert bc["75"]["trigger_total"] == 174.5
    assert bc["75"]["status"] == "over"
    # the seven-field under_alert contract is untouched
    assert set(g["under_alert"].keys()) >= {"active", "checkpoint",
                                            "actual_pace", "required_pace",
                                            "league_average_pace", "pace_gap"}


def test_active_game_has_pending_outcome(store):
    st, add, now, iso = store
    snap = add("4002", "Pending Home Virtual", "Pending Away Virtual")
    snap(now - timedelta(minutes=6), 10, 8, 2, "08:00", 180.5)
    app = FastAPI()
    app.include_router(v4_router)
    client = TestClient(app)
    g = next(g for g in client.get("/api/v4/live").json()["games"]
             if g["game_id"] == "4002")
    assert g["under_alert_outcome"]["status"] == "pending"
    assert g["under_alert_outcome"]["final_total"] is None


def test_api_settled_final_settles_finished_game_without_terminal_row(store):
    """Backend-authoritative settlement end-to-end: a genuinely finished
    game whose snapshots carry no terminal row still resolves every
    checkpoint once the scorecard's settled OK result exists."""
    import sqlite3
    st, add, now, iso = store
    snap = add("4003", "Settled Home Virtual", "Settled Away Virtual")
    # live snapshots only — the final arrived via the settled store
    snap(now - timedelta(minutes=48), 20, 18, 2, "07:00", 180.5)
    snap(now - timedelta(minutes=30), 40, 38, 3, "08:00", 176.5)
    snap(now - timedelta(minutes=25), 58, 52, 4, "08:00", 174.5)
    db_path = Path(st.path if hasattr(st, "path") else
                   v4api.os.environ["BLM_POKERBET_DB"])
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS game_results ("
        "id INTEGER PRIMARY KEY, source_game_id TEXT NOT NULL, "
        "classification TEXT NOT NULL, final_home INTEGER, "
        "final_away INTEGER, final_total INTEGER, result_at TEXT NOT NULL, "
        "final_result_status TEXT NOT NULL DEFAULT 'UNKNOWN')")
    con.execute(
        "INSERT INTO game_results (source_game_id, classification, "
        "final_home, final_away, final_total, result_at, "
        "final_result_status) VALUES (?, 'BETUAL_NBA', 90, 85, 175, ?, 'OK')",
        ("4003", iso(now - timedelta(minutes=1))))
    con.commit()
    con.close()

    app = FastAPI()
    app.include_router(v4_router)
    client = TestClient(app)
    g = next(g for g in client.get("/api/v4/live").json()["games"]
             if g["game_id"] == "4003")
    oc = g["under_alert_outcome"]
    assert oc["status"] == "resolved"
    assert oc["final_total"] == 175.0
    bc = oc["by_checkpoint"]
    assert bc["25"]["status"] == "under"
    assert bc["25"]["trigger_total"] == 180.5
    assert bc["75"]["trigger_total"] == 174.5
    assert oc["resolved_at"] is not None


# ══════════════════════════════════════════════════════════════════════
# Shipped dashboard state machine (Node) — coloring + lifecycle
# ══════════════════════════════════════════════════════════════════════

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
const esc = (s) => String(s == null ? "" : s).replace(/[&<>\"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtTime = (iso) => !iso ? "--"
  : new Date(iso).toLocaleTimeString("en-GB", { hour12: false });
function isActuallyLive(g) { return !!(g && g.live === true); }
"""

EXPORTS = """
module.exports = { UNDER_ALERTS, reconcileUnderAlerts, renderUnderAlerts,
  activeAlertsHTML, historyAlertsHTML, alertOutcomeClass, loadAlertHistory,
  __setT: (t) => { __T = t; }, __getT: () => __T };
"""


def _module(js: str, tmp_path: Path) -> Path:
    mod = tmp_path / "outcome_store.js"
    mod.write_text(STUBS + _block(js, PURE_BEGIN, PURE_END)
                   + _block(js, STORE_BEGIN, STORE_END) + EXPORTS,
                   encoding="utf-8")
    return mod


def _block(js: str, begin: str, end: str) -> str:
    i = js.index(begin) + len(begin)
    return js[i:js.index(end, i)]


def _run(js: str, tmp_path: Path, script: str):
    mod = _module(js, tmp_path)
    game_js = "const GAME = " + json.dumps(GAME) + ";\n"
    code = (f"const m = require({json.dumps(str(mod))});\n"
            f"{game_js}{script}\nconsole.log(JSON.stringify({{"
            f"history: m.UNDER_ALERTS.history,"
            f" activeSize: m.UNDER_ALERTS.active.size,"
            f" histHTML: m.historyAlertsHTML(),"
            f" actHTML: m.activeAlertsHTML()}}));")
    out = subprocess.run(["node", "-e", code], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout)


GAME = {
    "game_id": "G1", "competition_slug": "betual-nba", "live": True,
    "live_reason": None, "alert": {"eligible": True},
    "under_alert_eligibility": {"eligible": True, "reason": "market_live"},
    "projector": {"progress_pct": 30},
    "under_alert": {"active": True, "checkpoint": 25, "actual_pace": 3.2,
                    "required_pace": 4.1, "league_average_pace": 3.9,
                    "pace_gap": 0.9, "league_reference_games": 120},
}


def _client():
    app = FastAPI()
    app.include_router(v4_router)
    app.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
              name="outcome_test_static")

    @app.get("/", include_in_schema=False)
    async def operator_dashboard():
        return FileResponse(str(DASH_STATIC / "index.html"))
    return TestClient(app)


def test_frontend_colors_outcome_verbatim(tmp_path):
    """UNDER -> green class, OVER -> red class, PUSH -> neutral class."""
    client = _client()
    js = client.get("/static/dashboard.js").text

    def run(script):
        return _run(js, tmp_path, script)

    # alert opens (25%), then the game finishes between polls: the next
    # payload carries the backend's per-checkpoint outcome
    r = run("""
      m.reconcileUnderAlerts([Object.assign({}, GAME)]);
      const fin = Object.assign({}, GAME, { live: false,
        live_reason: "game_finished", status: "ended",
        under_alert: Object.assign({}, GAME.under_alert, { active: false }),
        under_alert_outcome: { status: "resolved", final_total: 175,
          resolved_at: "2026-09-12T19:59:00Z",
          by_checkpoint: { 25: { status: "under", trigger_total: 180.5,
            final_total: 175, resolved_at: "2026-09-12T19:59:00Z" } } } });
      m.reconcileUnderAlerts([fin]);
      null;
    """)
    rec = r["history"][0]
    assert rec["outcome"]["status"] == "under"
    assert rec["outcome"]["trigger_total"] == 180.5
    assert "al-under" in r["histHTML"]
    assert "al-outcome" in r["histHTML"]
    assert "UNDER" in r["histHTML"]

    # same machine, OVER and PUSH verdicts
    def verdict_case(status, final, trig=180.5):
        return run(f"""
          m.reconcileUnderAlerts([Object.assign({{}}, GAME, {{
            game_id: "G-{status}" }})]);
          const fin = Object.assign({{}}, GAME, {{
            game_id: "G-{status}", live: false, status: "ended",
            live_reason: "game_finished",
            under_alert: Object.assign({{}}, GAME.under_alert, {{ active: false }}),
            under_alert_outcome: {{ status: "resolved", final_total: {final},
              by_checkpoint: {{ 25: {{ status: "{status}",
                trigger_total: {trig}, final_total: {final} }} }} }} }});
          m.reconcileUnderAlerts([fin]);
          null;
        """)
    over = verdict_case("over", 185)
    assert "al-over" in over["histHTML"] and "OVER" in over["histHTML"]
    push = verdict_case("push", 180.5)
    assert "al-push" in push["histHTML"] and "PUSH" in push["histHTML"]


def test_frontend_does_not_color_active_alerts(tmp_path):
    client = _client()
    js = client.get("/static/dashboard.js").text
    r = _run(js, tmp_path, "m.reconcileUnderAlerts([Object.assign({}, GAME)]);")
    assert r["activeSize"] == 1
    rec = r["history"][0]
    assert rec["resolved_at"] is None
    assert rec["outcome"] is None or rec["outcome"].get("status") is None
    assert "al-under" not in r["histHTML"]
    assert "al-over" not in r["histHTML"]
    assert "al-push" not in r["histHTML"]


def test_outcome_survives_and_never_changes(tmp_path):
    """Settled verdict is immutable; history survives feed disappearance."""
    client = _client()
    js = client.get("/static/dashboard.js").text
    r = _run(js, tmp_path, """
      m.reconcileUnderAlerts([Object.assign({}, GAME)]);
      const fin = Object.assign({}, GAME, { live: false, status: "ended",
        live_reason: "game_finished",
        under_alert: Object.assign({}, GAME.under_alert, { active: false }),
        under_alert_outcome: { status: "resolved", final_total: 175,
          by_checkpoint: { 25: { status: "under", trigger_total: 180.5,
            final_total: 175 } } } });
      m.reconcileUnderAlerts([fin]);
      // later market movement must NOT rewrite the settled record
      const fin2 = Object.assign({}, fin,
        { under_alert_outcome: { status: "resolved", final_total: 185,
          by_checkpoint: { 25: { status: "over", trigger_total: 180.5,
            final_total: 185 } } } });
      m.reconcileUnderAlerts([fin2]);
      // game gone from /api/v4/live entirely — history persists
      m.reconcileUnderAlerts([]);
      null;
    """)
    assert len(r["history"]) == 1
    rec = r["history"][0]
    assert rec["outcome"]["status"] == "under"
    assert rec["outcome"]["trigger_total"] == 180.5
    assert rec["outcome"]["final_total"] == 175
    assert rec["resolved_at"] is not None
    assert "al-under" in r["histHTML"]


def test_frontend_has_no_quantitative_reconstruction():
    """The browser never re-derives the condition or the outcome rule."""
    client = _client()
    js = client.get("/static/dashboard.js").text
    store = _block(js, STORE_BEGIN, STORE_END)
    assert "final_total" in store or "under_alert_outcome" in store
    # no comparison of final vs trigger exists browser-side
    for banned in ("final_total <", "final_total >", "final_total <",
                   "trigger_total <", "trigger_total >"):
        assert banned not in store, banned
    # the outcome arrives from the payload and is only mapped to a class
    assert "function alertOutcomeClass" in js
    assert "g.under_alert_outcome" in js


def test_history_survives_reload_with_outcome(tmp_path):
    """localStorage restore keeps the colored, settled records."""
    client = _client()
    js = client.get("/static/dashboard.js").text
    r = _run(js, tmp_path, """
      m.reconcileUnderAlerts([Object.assign({}, GAME)]);
      const fin = Object.assign({}, GAME, { live: false, status: "ended",
        live_reason: "game_finished",
        under_alert: Object.assign({}, GAME.under_alert, { active: false }),
        under_alert_outcome: { status: "resolved", final_total: 175,
          by_checkpoint: { 25: { status: "under", trigger_total: 180.5,
            final_total: 175 } } } });
      m.reconcileUnderAlerts([fin]);
      m.loadAlertHistory();
      null;
    """)
    rec = r["history"][0]
    assert rec["outcome"]["status"] == "under"
    assert "al-under" in r["histHTML"]


# ══════════════════════════════════════════════════════════════════════
# Team-name persistence — history records are SELF-CONTAINED
# ══════════════════════════════════════════════════════════════════════

def _names_run(js, tmp_path, script, game):
    """_run with GAME pre-bound to a specific game object."""
    global GAME
    old = dict(GAME)
    GAME.clear()
    GAME.update(game)
    try:
        return _run(js, tmp_path, script)
    finally:
        GAME.clear()
        GAME.update(old)


def test_team_name_persistence_lifecycle(tmp_path):
    """The full directive matrix, against the SHIPPED store."""
    client = _client()
    js = client.get("/static/dashboard.js").text
    G = dict(GAME, game_id="30874038", live=True,
             home_team="Korfez Basket", away_team="Petkim Spor KB")

    # 1+2: names stored at alert creation, canonical (no Virtual suffix)
    r = _names_run(js, tmp_path,
                   "m.reconcileUnderAlerts([Object.assign({}, GAME)]);", G)
    rec = r["history"][0]
    assert rec["home_team"] == "Korfez Basket"
    assert rec["away_team"] == "Petkim Spor KB"
    assert "Korfez Basket vs Petkim Spor KB" in r["histHTML"]
    assert "Virtual" not in r["histHTML"]

    # 3+4: TRUE → TRUE must NOT mutate the frozen trigger snapshot names;
    # a later poll carrying different (e.g. raw) names cannot leak in
    r2 = _names_run(js, tmp_path, """
      m.reconcileUnderAlerts([Object.assign({}, GAME)]);
      m.reconcileUnderAlerts([Object.assign({}, GAME,
        { home_team: "Korfez Basket Virtual", away_team: "Petkim Spor KB Virtual" })]);
      null;
    """, G)
    rec2 = r2["history"][0]
    assert rec2["home_team"] == "Korfez Basket"
    assert rec2["away_team"] == "Petkim Spor KB"

    # 5: TRUE → FALSE (resolution) retains the names
    fin = dict(G, live=False, live_reason="game_finished", status="ended",
               under_alert=dict(GAME["under_alert"], active=False),
               under_alert_outcome={"status": "resolved", "final_total": 175,
                 "by_checkpoint": {25: {"status": "under",
                   "trigger_total": 180.5, "final_total": 175}}})
    r3 = _names_run(js, tmp_path, """
      m.reconcileUnderAlerts([Object.assign({}, GAME)]);
      const fin = Object.assign({}, GAME, { live: false,
        live_reason: "game_finished", status: "ended",
        under_alert: Object.assign({}, GAME.under_alert, { active: false }),
        under_alert_outcome: { status: "resolved", final_total: 175,
          by_checkpoint: { 25: { status: "under", trigger_total: 180.5,
            final_total: 175 } } } });
      m.reconcileUnderAlerts([fin]);
      null;
    """, G)
    rec3 = r3["history"][0]
    assert rec3["resolved_at"] is not None
    assert rec3["home_team"] == "Korfez Basket"
    assert rec3["away_team"] == "Petkim Spor KB"

    # 6: game REMOVED from /live entirely — history still self-contained
    r4 = _names_run(js, tmp_path, """
      m.reconcileUnderAlerts([Object.assign({}, GAME)]);
      m.reconcileUnderAlerts([]);
      null;
    """, G)
    assert r4["history"][0]["home_team"] == "Korfez Basket"
    assert "Korfez Basket vs Petkim Spor KB" in r4["histHTML"]

    # 7: reload (localStorage restoration) keeps the names
    r5 = _names_run(js, tmp_path, """
      m.reconcileUnderAlerts([Object.assign({}, GAME)]);
      m.loadAlertHistory();
      null;
    """, G)
    assert r5["history"][0]["home_team"] == "Korfez Basket"

    # 8: multiple checkpoints each carry their own names — no lookups
    G25 = dict(G, game_id="M1", under_alert=dict(GAME["under_alert"],
                                                checkpoint=25))
    G50 = dict(G, game_id="M1", under_alert=dict(GAME["under_alert"],
                                                 checkpoint=50))
    r6 = _names_run(js, tmp_path, """
      m.reconcileUnderAlerts([Object.assign({}, GAME, { game_id: "M1",
        under_alert: Object.assign({}, GAME.under_alert, { checkpoint: 25 }) })]);
      m.reconcileUnderAlerts([Object.assign({}, GAME, { game_id: "M1",
        under_alert: Object.assign({}, GAME.under_alert, { checkpoint: 50 }) })]);
      null;
    """, G)
    recs = {rec["checkpoint"]: rec for rec in r6["history"]}
    assert recs[25]["home_team"] == "Korfez Basket"
    assert recs[50]["home_team"] == "Korfez Basket"
    assert recs[25]["id"] != recs[50]["id"]


def test_legacy_records_backfilled_once_never_fabricated(tmp_path):
    """Old records without names are filled exactly once from the backend's
    canonical identity for the exact game_id; a game the backend cannot
    resolve keeps no names (Game <id> fallback); stored names are never
    overwritten; and no Virtual-stripping hack exists in the store."""
    client = _client()
    js = client.get("/static/dashboard.js").text
    G = dict(GAME, game_id="30887006", live=True,
             home_team="Beijing Royal Fighters", away_team="Tianjin Pioneers")

    # backfill fills the nameless legacy record
    r = _names_run(js, tmp_path, """
      m.UNDER_ALERTS.history.push({ id: "30887006#25", game_id: "30887006",
        checkpoint: 25, triggered_at: "2026-09-12T21:00:00Z",
        resolved_at: "2026-09-12T22:00:00Z", legacy: true });
      m.reconcileUnderAlerts([Object.assign({}, GAME)]);
      null;
    """, G)
    rec = r["history"][0]
    assert rec["home_team"] == "Beijing Royal Fighters"
    assert "Beijing Royal Fighters vs Tianjin Pioneers" in r["histHTML"]

    # backfilled names are then frozen: a later poll cannot overwrite them
    r2 = _names_run(js, tmp_path, """
      m.UNDER_ALERTS.history.push({ id: "30887006#25", game_id: "30887006",
        checkpoint: 25, home_team: "Beijing Royal Fighters",
        away_team: "Tianjin Pioneers", triggered_at: "2026-09-12T21:00:00Z" });
      m.reconcileUnderAlerts([Object.assign({}, GAME,
        { home_team: "Wrong Team", away_team: "Other Team" })]);
      null;
    """, G)
    assert r2["history"][0]["home_team"] == "Beijing Royal Fighters"

    # a game the backend cannot resolve (absent payload) -> no fabrication
    r3 = _names_run(js, tmp_path, """
      m.UNDER_ALERTS.history.push({ id: "999#25", game_id: "999",
        checkpoint: 25, triggered_at: "2026-09-12T21:00:00Z" });
      m.reconcileUnderAlerts([]);
      null;
    """, G)
    assert r3["history"][0].get("home_team") in (None, "")
    assert "Game 999" in r3["histHTML"]

    # no frontend Virtual-stripping hack anywhere in the shipped store
    store = _block(js, STORE_BEGIN, STORE_END)
    for banned in ('replace("Virtual"', "replace('Virtual'",
                   "/Virtual/g", "replaceAll('Virtual'",
                   'replaceAll("Virtual"'):
        assert banned not in store, banned


def test_frontend_renders_names_from_history_record_only(tmp_path):
    """The renderer reads names from the HISTORY record itself — with the
    live game object long gone, HOME vs AWAY still renders."""
    client = _client()
    js = client.get("/static/dashboard.js").text
    G = dict(GAME, game_id="30887004", live=True,
             home_team="Guangdong Southern Tigers",
             away_team="Nanjing Tongxi Monkey King")
    r = _names_run(js, tmp_path, """
      m.reconcileUnderAlerts([Object.assign({}, GAME)]);
      // resolve AND remove the game in the same poll: the record must
      // already be self-contained
      m.reconcileUnderAlerts([]);
      null;
    """, G)
    rec = r["history"][0]
    assert rec["home_team"] == "Guangdong Southern Tigers"
    assert rec["away_team"] == "Nanjing Tongxi Monkey King"
    assert rec["resolved_at"] is not None
    assert "Guangdong Southern Tigers vs Nanjing Tongxi Monkey King" \
        in r["histHTML"]
    # the names line renders from the record — the ident line (league | Game
    # <id>) is the existing header; the fallback "Game <id>" WITHOUT a teams
    # line applies only to nameless legacy records (covered above)
    assert "al-teams" in r["histHTML"]
