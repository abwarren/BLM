"""TEMPORAL-FRESHNESS FIX — regression tests (audit 2026-09-16).

Covers the exact failure pattern the read-only audit proved:

  SOURCE = Q4 (WS market_observations, fresh, complete)
  DOM   = stale Q3 (or NULL-bearing degraded snapshots)
  BLM   = frozen on the last complete DOM frame

Directives under test (implementation directive §1–§10):

  1  AUTHORITATIVE LIVE STATE — validated WS state supersedes a failed /
     stale DOM state; unvalidated DOM fields never merge over WS.
  2  STALE/NULL SNAPSHOT PROTECTION — a snapshot with score=NULL or
     quarter=NULL never replaces a newer complete live state.
  3  TEMPORAL MONOTONICITY — accepted state never moves backwards
     (Q4→Q3, newer clock→older clock, current→stale).  No invented clocks.
  4  Q3→Q4 TRANSITION — a valid Q4 state immediately supersedes Q3.
  5  FRESHNESS METADATA — state_observed_at / state_age_seconds / source /
     source_observed_at are exposed per game and payload-wide.
  6  STALE ALERT PREVENTION — a game whose accepted state exceeds the
     documented ALERT_MAX_STATE_AGE_S bound is ineligible with reason
     stale_state (fail closed), with no threshold manipulation.
  7  GAME_FINISHED RECONCILIATION — the reconciler marks source-expired
     live games ended (own thread/cadence, independent of the scorecard).
  8  API FRESHNESS — /status and /live expose game-state freshness
     separately from response generation time.
  9  FRONTEND ACTIVE/ENDED RECONCILIATION — an ACTIVE alert closes when
     the payload's authoritative state says the game is over, even if a
     delayed alert verdict has not yet flipped (executed against the
     SHIPPED dashboard state machine in Node.js).
 10  NULL snapshot protection at the API surface (the /live payload).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import blm_v4.api as v4api
from blm_v4.api import (
    ALERT_MAX_STATE_AGE_S,
    WS_STATE_FALLBACK_S,
    _analyze_game,
    _authoritative_game_state,
    _clock_seconds,
    _state_is_regression,
    _ws_game_state,
)
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"
DASHBOARD_JS = DASH_STATIC / "dashboard.js"

STORE_BEGIN = "/* __ALERT_STORE_BEGIN__ */"
STORE_END = "/* __ALERT_STORE_END__ */"
PURE_BEGIN = "/* __PURE_ALERT_BEGIN__ */"
PURE_END = "/* __PURE_ALERT_END__ */"

# ── the audit's own example numbers ────────────────────────────────────
# game 30919555: SOURCE Q4 05:00 (WS 02:25:32), DOM froze Q4 01:00
# (02:29:42), dashboard showed Q3/older state for minutes.


# ══════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def store(tmp_path, monkeypatch):
    db = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    monkeypatch.setattr(v4api, "STATE_FILE", tmp_path / "collector_state.json")
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

        def snap(t: datetime, hs, as_, q, clock, total,
                 status: str = "live", period_label=None):
            obs = MarketObservation(
                source="PokerBet", source_game_id=gid,
                classification="BETUAL_NBA", captured_at=iso(t),
                home_team=home, away_team=away,
                home_score=hs, away_score=as_,
                period_label=period_label or (f"{q}th Quarter" if q else ""),
                quarter=q, clock=clock,
                game_status=status, total_line=total, spread=None,
                w1_odds=None, w2_odds=None, markets_json="{}")
            st.insert_snapshot(gid_db, obs, force=True)

        def ws(t: datetime, hs, as_, q, clock, total):
            """A WS MatchTotal frame exactly as ws_market persists it."""
            conn = st._connect()
            try:
                conn.execute(
                    """INSERT INTO market_observations
                       (game_id, source_game_id, captured_at, market_type,
                        market_name, line_value, over_price, under_price,
                        home_score, away_score, period_label, clock)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (gid_db, gid, iso(t), "MatchTotal", "Total Points",
                     total, 1.9, 1.9, hs, as_,
                     (f"{q}th Quarter" if q else None), clock))
                conn.commit()
            finally:
                conn.close()

        return snap, ws

    return st, add, now, iso


def _live(rows_by_game):
    app = FastAPI()
    app.include_router(v4_router := __import__(
        "blm_v4.api", fromlist=["router"]).router)
    client = TestClient(app)
    body = client.get("/api/v4/live").json()
    return {g["game_id"]: g for g in body["games"] if g["game_id"] in rows_by_game}


def _projector(monkeypatch, progress: float = 92.5, remaining: float = 3.0,
               actual: float = 4.4, required: float = 7.5,
               captured_at: datetime | None = None,
               period_label: str = "4th Quarter", clock: str = "01:00"):
    """Deterministic clean-metrics projector row (progress/pace authority)."""
    cap = captured_at or datetime.now(timezone.utc)

    def row(source_game_id):
        return {
            "source_game_id": source_game_id, "classification": "BETUAL_NBA",
            "captured_at": cap.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "period_label": period_label, "clock": clock,
            "elapsed_game_minutes": 40.0 - remaining,
            "remaining_game_minutes": remaining,
            "progress_pct": progress, "current_total_points": 171,
            "live_total_line": 178.5, "market_status": "LIVE",
            "market_age_seconds": 30, "actual_pts_per_min": actual,
            "required_pts_per_min": required,
            "pace_gap": actual - required,
            "status": "VALID",
        }
    monkeypatch.setattr(v4api, "_pace_projector_for", row)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ══════════════════════════════════════════════════════════════════════
# 3. Unit: monotonicity primitives
# ══════════════════════════════════════════════════════════════════════

def test_clock_seconds_parses_descending_clocks():
    assert _clock_seconds("05:00") == 300
    assert _clock_seconds("00:00") == 0
    assert _clock_seconds("12:34") == 754
    assert _clock_seconds(None) is None
    assert _clock_seconds("garbage") is None
    assert _clock_seconds("99") is None


def test_quarter_regression_rejected():
    # Q4 accepted, Q3 candidate → regression
    assert _state_is_regression(
        {"quarter": 4, "clock": "05:00"}, {"quarter": 3, "clock": "01:00"})


def test_same_quarter_clock_regression_rejected():
    # inside Q4: accepted 03:00, candidate 05:00 → clock moved backwards
    assert _state_is_regression(
        {"quarter": 4, "clock": "03:00"}, {"quarter": 4, "clock": "05:00"})


def test_clock_forward_and_period_end_are_progress():
    # 03:00 → 01:00 is the clock descending normally
    assert not _state_is_regression(
        {"quarter": 4, "clock": "03:00"}, {"quarter": 4, "clock": "01:00"})
    # 00:00 is the period-end sentinel, never a regression
    assert not _state_is_regression(
        {"quarter": 4, "clock": "03:00"}, {"quarter": 4, "clock": "00:00"})
    # Q3 → Q4 is structural progress
    assert not _state_is_regression(
        {"quarter": 3, "clock": "01:00"}, {"quarter": 4, "clock": "10:00"})


def test_incomplete_candidates_never_called_regressions():
    assert not _state_is_regression(
        {"quarter": 4, "clock": "03:00"}, {"quarter": None, "clock": "05:00"})
    assert not _state_is_regression(
        {"quarter": 4, "clock": "03:00"}, {"quarter": 4, "clock": None})


# ══════════════════════════════════════════════════════════════════════
# 1/2/3/4. Selection: WS supersedes stale DOM; NULL never wins; no
# backwards moves; the audit's exact Q4 pattern
# ══════════════════════════════════════════════════════════════════════

def _sel(rows, ws_state, now):
    state, meta = _authoritative_game_state(rows, ws_state, now)
    return state, meta


def test_valid_ws_supersedes_stale_dom():
    now = datetime.now(timezone.utc)
    stale_dom = {"captured_at": _iso(now - timedelta(seconds=400)),
                 "home_score": 84,
                 "away_score": 88, "quarter": 3, "clock": "01:00",
                 "period_label": "3rd Quarter", "total_line": 178.5}
    ws_q4 = {"captured_at": _iso(now - timedelta(seconds=10)),
             "home_score": 84, "away_score": 88,
             "quarter": 4, "clock": "05:00", "period_label": "4th Quarter",
             "line_value": 178.5}
    state, meta = _sel([stale_dom], ws_q4, now)
    assert state["quarter"] == 4 and state["clock"] == "05:00"
    assert meta["source"] == "ws"
    assert meta["state_age_seconds"] <= 15


def test_valid_ws_supersedes_degraded_null_dom():
    now = datetime.now(timezone.utc)
    # the audit's degraded-hydration frames: line carried, state NULL
    null_frames = [
        {"captured_at": _iso(now - timedelta(seconds=30)), "home_score": None, "away_score": 96,
         "quarter": None, "clock": None, "period_label": "", "total_line": 200.5},
        {"captured_at": _iso(now - timedelta(seconds=20)), "home_score": None, "away_score": None,
         "quarter": None, "clock": "03:00", "period_label": "", "total_line": 199.5},
    ]
    ws_q4 = {"captured_at": _iso(now - timedelta(seconds=10)), "home_score": 95, "away_score": 93,
             "quarter": 4, "clock": "03:00", "period_label": "4th Quarter"}
    state, meta = _sel(null_frames, ws_q4, now)
    # a NULL snapshot NEVER replaces the state: WS wins
    assert state["quarter"] == 4 and state["home_score"] == 95
    assert meta["source"] == "ws"


def test_fresh_complete_dom_still_preferred_over_ws():
    now = datetime.now(timezone.utc)
    dom = {"captured_at": _iso(now - timedelta(seconds=30)), "home_score": 86, "away_score": 73,
           "quarter": 4, "clock": "03:00", "period_label": "4th Quarter"}
    ws = {"captured_at": _iso(now - timedelta(seconds=5)), "home_score": 86, "away_score": 75,
          "quarter": 4, "clock": "02:00", "period_label": "4th Quarter"}
    state, meta = _sel([dom], ws, now)
    assert meta["source"] == "dom"          # primary while fresh
    assert state["clock"] == "03:00"


def test_ws_quarter_regression_over_stale_dom_rejected():
    """The DOM state is Q4 (accepted); a WS candidate that is OLDER game
    time (Q3, or a greater Q4 clock) must not roll it back."""
    now = datetime.now(timezone.utc)
    dom = {"captured_at": _iso(now - timedelta(seconds=400)), "home_score": 86, "away_score": 73,
           "quarter": 4, "clock": "03:00", "period_label": "4th Quarter"}
    ws_stale_q3 = {"captured_at": _iso(now - timedelta(seconds=10)), "home_score": 70,
                   "away_score": 60, "quarter": 3, "clock": "08:00",
                   "period_label": "3rd Quarter"}
    state, meta = _sel([dom], ws_stale_q3, now)
    assert meta["source"] == "dom"          # rejected → accepted state holds
    assert state["quarter"] == 4 and state["clock"] == "03:00"
    ws_clock_back = {"captured_at": _iso(now - timedelta(seconds=10)), "home_score": 86,
                     "away_score": 73, "quarter": 4, "clock": "07:00",
                     "period_label": "4th Quarter"}
    state, meta = _sel([dom], ws_clock_back, now)
    assert meta["source"] == "dom"          # 03:00 → 07:00 rejected
    assert state["clock"] == "03:00"


def test_ws_q4_immediately_supersedes_dom_q3__audit_pattern():
    """DIRECTIVE §4 — the audit's exact pattern:
    SOURCE = Q4, DOM = stale Q3, WS = current Q4 ⇒ accepted = current Q4."""
    now = datetime.now(timezone.utc)
    dom_stale_q3 = {"captured_at": _iso(now - timedelta(seconds=310)), "home_score": 70,
                    "away_score": 64, "quarter": 3, "clock": "01:00",
                    "period_label": "3rd Quarter"}
    ws_current_q4 = {"captured_at": _iso(now - timedelta(seconds=8)), "home_score": 75,
                     "away_score": 66, "quarter": 4, "clock": "10:00",
                     "period_label": "4th Quarter"}
    state, meta = _sel([dom_stale_q3], ws_current_q4, now)
    assert meta["source"] == "ws"
    assert state["quarter"] == 4 and state["clock"] == "10:00"
    assert state["home_score"] == 75 and state["away_score"] == 66


def test_stale_dom_holds_when_no_ws_state_exists():
    now = datetime.now(timezone.utc)
    dom = {"captured_at": _iso(now - timedelta(seconds=400)), "home_score": 84, "away_score": 88,
           "quarter": 4, "clock": "01:00", "period_label": "4th Quarter"}
    state, meta = _sel([dom], None, now)
    assert meta["source"] == "dom"          # nothing invented — DOM holds
    assert meta["state_age_seconds"] > WS_STATE_FALLBACK_S


# ══════════════════════════════════════════════════════════════════════
# 5/8. Freshness metadata + API freshness surfaces
# ══════════════════════════════════════════════════════════════════════

def test_payload_carries_game_state_freshness(store, monkeypatch):
    st, add, now, iso = store
    _projector(monkeypatch)
    snap, ws = add("6001", "Meta Home Virtual", "Meta Away Virtual")
    # stale complete DOM frame (>300s), fresh WS state
    snap(now - timedelta(seconds=400), 84, 88, 3, "01:00", 178.5)
    ws(now - timedelta(seconds=10), 84, 88, 4, "05:00", 178.5)
    g = _live({"6001"})["6001"]
    fm = g["game_state_freshness"]
    assert set(fm) >= {"source", "state_observed_at", "state_age_seconds",
                       "source_observed_at"}
    assert fm["source"] == "ws"
    assert fm["state_age_seconds"] <= 15
    assert fm["source_observed_at"] is not None
    # displayed state comes from the WS frame — NOT the stale DOM frame
    assert g["quarter"] == 4 and g["clock"] == "05:00"
    assert g["last_update"].startswith(fm["state_observed_at"][:19])
    # payload-level block exists and is separate from generated_at
    app = FastAPI()
    app.include_router(__import__("blm_v4.api", fromlist=["router"]).router)
    body = TestClient(app).get("/api/v4/live").json()
    assert "game_state_freshness" in body
    assert body["game_state_freshness"]["state_age_seconds"] is not None
    assert body["generated_at"] != body["game_state_freshness"]["state_observed_at"]


def test_status_endpoint_exposes_state_freshness_separately(store):
    st, add, now, iso = store
    snap, ws = add("6002", "Status Home Virtual", "Status Away Virtual")
    snap(now - timedelta(seconds=20), 10, 8, 1, "05:00", 210.5)
    ws(now - timedelta(seconds=5), 10, 8, 1, "04:00", 210.5)
    app = FastAPI()
    app.include_router(__import__("blm_v4.api", fromlist=["router"]).router)
    body = TestClient(app).get("/api/v4/status").json()
    gs = body["db"]["game_state_freshness"]
    assert set(gs) >= {"source", "state_observed_at", "state_age_seconds"}
    assert gs["state_age_seconds"] <= 30
    # distinct surfaces: db.last_snapshot_at (write recency) vs state age
    assert "last_snapshot_at" in body["db"]


# ══════════════════════════════════════════════════════════════════════
# 6. Stale alert prevention (fail closed, documented bound)
# ══════════════════════════════════════════════════════════════════════

def test_stale_state_blocks_alert_with_stale_state_reason(store, monkeypatch):
    st, add, now, iso = store
    # projector observation CURRENT (inside ALERT_MAX_OBS_AGE_S) but the
    # accepted GAME STATE is old everywhere (DOM >300s, no WS at all)
    _projector(monkeypatch, progress=80.0, actual=8.0, required=10.0,
               captured_at=now)
    snap, ws = add("6101", "Stale Home Virtual", "Stale Away Virtual")
    snap(now - timedelta(seconds=ALERT_MAX_STATE_AGE_S + 60), 55, 40, 3,
         "00:00", 187.5)
    g = _live({"6101"})["6101"]
    assert g["under_alert"]["active"] is not True
    assert g["alert"]["eligible"] is False
    assert g["alert"]["reason"] == "stale_state"
    assert g["under_alert_eligibility"]["reason"] == "stale_state"


def test_fresh_state_with_fresh_projector_alerts_normally(store, monkeypatch):
    st, add, now, iso = store
    _projector(monkeypatch, progress=80.0, actual=8.0, required=10.0,
               captured_at=now)
    snap, ws = add("6102", "Fresh Home Virtual", "Fresh Away Virtual")
    snap(now - timedelta(seconds=30), 60, 44, 4, "06:00", 187.5)
    g = _live({"6102"})["6102"]
    assert g["alert"]["eligible"] is True
    assert g["under_alert"]["active"] is True


def test_stale_state_bound_is_documented_constant():
    # §6: "The limit must be explicitly defined and documented."
    assert ALERT_MAX_STATE_AGE_S == 300.0
    assert WS_STATE_FALLBACK_S == 300.0


# ══════════════════════════════════════════════════════════════════════
# 7. game_finished reconciliation (server-side reconciler)
# ══════════════════════════════════════════════════════════════════════

def test_reconciler_marks_source_expired_games_ended(tmp_path, monkeypatch):
    """Run the reconciler body once against a fixture DB: a live-flagged
    game with no snapshot and no WS row inside the expiry window flips to
    ended; a game with fresh evidence does not."""
    import sqlite3
    import server as blm_server
    db = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    st = PokerBetStore(db)
    now = datetime.now(timezone.utc)

    def iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    stale = PokerBetGame(
        source="PokerBet", source_game_id="6201",
        competition_id="c", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA",
        sport="basketball", home_team="Gone Home", away_team="Gone Away",
        game_slug="gone-home-gone-away", source_url="https://x/6201",
        status="live", first_seen_at=iso(now - timedelta(hours=2)),
        last_seen_at=iso(now - timedelta(minutes=30)))
    fresh = PokerBetGame(
        source="PokerBet", source_game_id="6202",
        competition_id="c", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA",
        sport="basketball", home_team="Live Home", away_team="Live Away",
        game_slug="live-home-live-away", source_url="https://x/6202",
        status="live", first_seen_at=iso(now - timedelta(hours=2)),
        last_seen_at=iso(now - timedelta(seconds=10)))
    st.upsert_game(stale)
    st.upsert_game(fresh)
    # upsert_game stamps last_seen_at=now for both rows; the reconciler's
    # source-expiry predicate reads the STORED last_seen_at, so backdate the
    # stale game's row directly (this is exactly the production shape: a
    # game the collector last saw 30 minutes ago).
    conn = sqlite3.connect(f"file:{db}", uri=True, timeout=30)
    try:
        conn.execute(
            "UPDATE games SET last_seen_at=? WHERE source_game_id='6201'",
            (iso(now - timedelta(minutes=30)),))
        conn.commit()
    finally:
        conn.close()
    # fresh game keeps evidence: a WS row inside the window
    conn = st._connect()
    try:
        conn.execute(
            """INSERT INTO market_observations
               (game_id, source_game_id, captured_at, market_type,
                market_name, line_value, period_label, clock,
                home_score, away_score)
               VALUES ((SELECT id FROM games WHERE source_game_id='6202'),
                       '6202', ?, 'MatchTotal', 'Total Points', 200.5,
                       '2nd Quarter', '06:00', 30, 28)""",
            (iso(now - timedelta(seconds=5)),))
        conn.commit()
    finally:
        conn.close()

    # run ONE reconciler pass (replicates the server loop body exactly)
    expiry = 180.0
    cutoff = now - timedelta(seconds=expiry)
    cut = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    conn = sqlite3.connect(f"file:{db}", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT g.id, g.source_game_id FROM games g
           WHERE g.status = 'live' AND g.last_seen_at < ?
             AND NOT EXISTS (SELECT 1 FROM snapshots s
                             WHERE s.source_game_id = g.source_game_id
                               AND s.captured_at >= ?)
             AND NOT EXISTS (SELECT 1 FROM market_observations mo
                             WHERE mo.source_game_id = g.source_game_id
                               AND mo.captured_at >= ?)""",
        (cut, cut, cut)).fetchall()
    for r in rows:
        conn.execute("UPDATE games SET status='ended' WHERE id=?", (r["id"],))
    conn.commit()
    statuses = {r["source_game_id"]: r["status"] for r in conn.execute(
        "SELECT source_game_id, status FROM games")}
    conn.close()
    assert statuses["6201"] == "ended"      # source-expired → reconciled
    assert statuses["6202"] == "live"       # fresh evidence → untouched


# ══════════════════════════════════════════════════════════════════════
# 9. Frontend ACTIVE→ended reconciliation (SHIPPED state machine, Node)
# ══════════════════════════════════════════════════════════════════════

def _run_node_script(script: str) -> dict:
    """Run a Node.js harness that extracts the SHIPPED state machine from
    dashboard.js and returns JSON via console.log."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    runner = HERE.parent / ".pytest_node_runner.js"
    try:
        runner.write_text(script)
        with subprocess.Popen([node, str(runner)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as p:
            out, err = p.communicate(timeout=30)
    finally:
        if runner.exists():
            runner.unlink()
    if p.returncode != 0:
        raise AssertionError(f"node failed: {err}\n{out}")
    return json.loads(out)


NODE_PRELUDE = """
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync('%s', 'utf8');
const store = src.slice(src.indexOf('/* __ALERT_STORE_BEGIN__ */'),
                        src.indexOf('/* __ALERT_STORE_END__ */'));
const pure = src.slice(src.indexOf('/* __PURE_ALERT_BEGIN__ */'),
                       src.indexOf('/* __PURE_ALERT_END__ */'));
const sandbox = {
  localStorage: { _m: {}, getItem(k) { return this._m[k] ?? null; },
    setItem(k, v) { this._m[k] = v; }, removeItem(k) { delete this._m[k]; } },
  PREF: {}, ALERT_RULE_ID: 'under_rel_v1', UNDER_ALERT_MARKET: 'Total Points',
  num1: (v) => String(v), num2: (v) => String(v),
  fmtTime: () => '00:00:00', fmtDuration: () => '0s', esc: (s) => String(s),
  alertResultHTML: () => '', alertOutcomeClass: () => '',
  alertOutcomeLine: () => '', saveAlertHistory: () => {},
  Date, JSON, Map, Set, console,
};
const ctx = vm.createContext(sandbox);
vm.runInContext(store + '\\n' + pure, ctx);
ctx.__result = (o) => { ctx.__out = o; };
""" % (DASHBOARD_JS,)


def test_frontend_closes_active_alert_when_authoritative_state_ends_game():
    """§9: an ACTIVE record is closed the moment this poll's payload marks
    its game over (status ended / live false / terminal reason) — even
    though under_alert.active has not flipped yet on the same poll
    (the audit's delayed-delivery failure)."""
    harness = NODE_PRELUDE + """
vm.runInContext(`
UNDER_ALERTS.active.set('30919555|75', {
  id: '30919555|75', game_id: '30919555', checkpoint: 75,
  triggered_at: new Date(Date.now() - 131000).toISOString(),
  actual_pace: 4.38, required_pace: 7.5, league_average_pace: 4.3,
  triggered_line: 178.5, outcome: null,
});
UNDER_ALERTS.history.push({
  id: '30919555|75', game_id: '30919555', checkpoint: 75,
  triggered_at: new Date(Date.now() - 131000).toISOString(),
  triggered_line: 178.5, resolved_at: null, alert_rule: ALERT_RULE_ID,
});
reconcileUnderAlerts([
  { game_id: '30919555', status: 'ended', live: false,
    live_reason: 'game_finished',
    under_alert: { checkpoint: 75, active: true } },
], new Map());
__result({ activeCount: UNDER_ALERTS.active.size,
  reason: UNDER_ALERTS.history[0].resolved_reason });
`, ctx);
console.log(JSON.stringify(ctx.__out));
"""
    out = _run_node_script(harness)
    assert out["activeCount"] == 0
    assert out["reason"] == "game_finished"


def test_frontend_keeps_alert_active_while_game_genuinely_live():
    """The §9 pass must never suppress an alert for a game still live."""
    harness = NODE_PRELUDE + """
vm.runInContext(`
UNDER_ALERTS.active.set('7001|75', {
  id: '7001|75', game_id: '7001', checkpoint: 75,
  triggered_at: new Date(Date.now() - 60000).toISOString(),
  actual_pace: 4.38, required_pace: 7.5, league_average_pace: 4.3,
  triggered_line: 178.5, outcome: null,
});
UNDER_ALERTS.history.push({
  id: '7001|75', game_id: '7001', checkpoint: 75,
  triggered_at: new Date(Date.now() - 60000).toISOString(),
  triggered_line: 178.5, resolved_at: null, alert_rule: ALERT_RULE_ID,
});
reconcileUnderAlerts([
  { game_id: '7001', status: 'live', live: true, live_reason: null,
    under_alert: { checkpoint: 75, active: true } },
], new Map());
__result({ activeCount: UNDER_ALERTS.active.size,
  resolved: !!UNDER_ALERTS.history[0].resolved_at });
`, ctx);
console.log(JSON.stringify(ctx.__out));
"""
    out = _run_node_script(harness)
    assert out["activeCount"] == 1
    assert out["resolved"] is False


def test_frontend_closes_on_terminal_reason_even_when_status_not_yet_ended():
    """A live-flagged game whose per-frame state is terminal (Q4 clock
    sentinel) closes its alert too — the backend live_reason vocabulary
    (terminal_*) is authoritative over a lagging status string."""
    harness = NODE_PRELUDE + """
vm.runInContext(`
UNDER_ALERTS.active.set('7002|75', {
  id: '7002|75', game_id: '7002', checkpoint: 75,
  triggered_at: new Date(Date.now() - 60000).toISOString(),
  actual_pace: 4.38, required_pace: 7.5, league_average_pace: 4.3,
  triggered_line: 178.5, outcome: null,
});
UNDER_ALERTS.history.push({
  id: '7002|75', game_id: '7002', checkpoint: 75,
  triggered_at: new Date(Date.now() - 60000).toISOString(),
  triggered_line: 178.5, resolved_at: null, alert_rule: ALERT_RULE_ID,
});
reconcileUnderAlerts([
  { game_id: '7002', status: 'live', live: false,
    live_reason: 'terminal_q4_clock_sentinel',
    under_alert: { checkpoint: 75, active: true } },
], new Map());
__result({ activeCount: UNDER_ALERTS.active.size,
  reason: UNDER_ALERTS.history[0].resolved_reason });
`, ctx);
console.log(JSON.stringify(ctx.__out));
"""
    out = _run_node_script(harness)
    assert out["activeCount"] == 0
    assert out["reason"] == "game_finished"


# ══════════════════════════════════════════════════════════════════════
# 10. NULL snapshot protection at the API surface
# ══════════════════════════════════════════════════════════════════════

def test_null_snapshots_never_regress_api_surface(store, monkeypatch):
    """After a complete Q4 frame, degraded NULL-bearing frames must not
    turn the /live card back into an older/empty state."""
    st, add, now, iso = store
    _projector(monkeypatch, progress=92.5, remaining=3.0)
    snap, ws = add("6301", "Null Home Virtual", "Null Away Virtual")
    t0 = now - timedelta(minutes=10)
    snap(t0, 55, 40, 3, "00:00", 187.5)                       # Q3 complete
    snap(now - timedelta(seconds=240), 84, 88, 4, "03:00", 178.5)  # Q4 complete
    # degraded hydration burst: partial/NULL frames, fresher timestamps
    snap(now - timedelta(seconds=120), None, 96, None, None, 200.5)
    snap(now - timedelta(seconds=60), None, None, None, "03:00", 199.5)
    g = _live({"6301"})["6301"]
    # the accepted state is still the Q4 frame — NOT the NULL frames
    assert g["quarter"] == 4
    assert g["home_score"] == 84 and g["away_score"] == 88
    assert g["game_state_freshness"]["source"] == "dom"


def test_ws_game_state_skips_line_only_rows(store):
    st, add, now, iso = store
    snap, ws = add("6401", "Ws Home Virtual", "Ws Away Virtual")
    conn = st._connect()
    try:
        gid_db = conn.execute(
            "SELECT id FROM games WHERE source_game_id='6401'").fetchone()[0]
        # line-only row (newest) then a complete state row (older)
        conn.execute(
            """INSERT INTO market_observations
               (game_id, source_game_id, captured_at, market_type,
                market_name, line_value, period_label, clock)
               VALUES (?,?,?,?,?,?,NULL,NULL)""",
            (gid_db, "6401", iso(now - timedelta(seconds=5)),
             "MatchTotal", "Total Points", 210.5))
        conn.execute(
            """INSERT INTO market_observations
               (game_id, source_game_id, captured_at, market_type,
                market_name, line_value, period_label, clock,
                home_score, away_score)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (gid_db, "6401", iso(now - timedelta(seconds=10)),
             "MatchTotal", "Total Points", 210.5, "4th Quarter", "05:00",
             84, 88))
        conn.commit()
    finally:
        conn.close()
    got = _ws_game_state(conn2 := st._connect(), "6401")
    try:
        assert got is not None and got["quarter"] == 4
    finally:
        conn2.close()
