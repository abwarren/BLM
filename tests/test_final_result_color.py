"""EVERY ENDED GAME ONCE RESULTED — the final-result colour requirement.

Directive (2026-09-13): EVERY game that has ended and has a valid
authoritative final result MUST be coloured on the dashboard once that result
is known — whether or not it ever raised an UNDER alert.  The live-alert
state and the final-result state are SEPARATE concepts, and the first must
never gate whether the second exists.

The API serves one ``final_result`` block per game::

    {"status": "under"|"over"|"push"|None,
     "final_total": float|None,
     "line": float|None,
     "line_source": "trigger"|"closing"|None,
     "final_source": "settled"|"observation"|None,
     "authoritative": bool,
     "resolved_at": iso|None}

computed by ONE comparison — final vs line — from the SAME authorities the
settlement already uses (``under_alert_outcome.final_total`` and
``under_alert.trigger_line``, falling back to ``market.closing_line`` when no
trigger line is provable).  ``status`` is None until a final exists, so an
unfinished game is never coloured and an ended game with a valid final is
never left uncoloured.

Cases A–I below are the directive's own list.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blm_v4 import api as v4api
from blm_v4.api import router as v4_router
from blm_v4.live_analytics.under_outcome import outcome_status
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent
DASH_JS = HERE.parent / "blm_v4" / "dashboard" / "static" / "dashboard.js"
STYLES_CSS = HERE.parent / "blm_v4" / "dashboard" / "static" / "styles.css"

TRIGGER = 187.5
LATER = 189.5
OPENING = 210.5


# ══════════════════════════════════════════════════════════════════════
# fixture — a real store, real snapshots, real settled game_results rows
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
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")      # noqa: E731

    def add(gid: str, home: str, away: str):
        st.upsert_game(PokerBetGame(
            source="PokerBet", source_game_id=gid,
            competition_id="comp-b", competition_slug="betual-nba",
            competition="Betual NBA", region="Virtual Matches",
            game_family="betual", classification="BETUAL_NBA",
            sport="basketball", home_team=home, away_team=away,
            game_slug=f"{gid}-game", source_url=f"https://x/{gid}",
            status="live", first_seen_at=iso(now - timedelta(minutes=60)),
            last_seen_at=iso(now)))

        def snap(t, hs, as_, q, clock, total, status="live",
                 period_label=None):
            st.insert_snapshot(st.upsert_game(PokerBetGame(
                source="PokerBet", source_game_id=gid,
                competition_id="comp-b", competition_slug="betual-nba",
                competition="Betual NBA", region="Virtual Matches",
                game_family="betual", classification="BETUAL_NBA",
                sport="basketball", home_team=home, away_team=away,
                game_slug=f"{gid}-game", source_url=f"https://x/{gid}",
                status="live", first_seen_at=iso(now - timedelta(minutes=60)),
                last_seen_at=iso(now))),
                MarketObservation(
                    source="PokerBet", source_game_id=gid,
                    classification="BETUAL_NBA", captured_at=iso(t),
                    home_team=home, away_team=away, home_score=hs,
                    away_score=as_, period_label=period_label or f"{q}th Quarter",
                    quarter=q, clock=clock, game_status=status,
                    total_line=total, spread=None, w1_odds=None,
                    w2_odds=None, markets_json="{}"), force=True)
        return snap
    return st, add, now, iso, db


def _write_settled(db, gid, final_total, at, status="OK"):
    """The scorecard's settled game-final record (game_results) — the ONLY
    block that makes a final authoritative.  ``status='UNKNOWN'`` models a
    row that exists but is NOT a verified final."""
    import sqlite3
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE IF NOT EXISTS game_results ("
        "id INTEGER PRIMARY KEY, source_game_id TEXT NOT NULL, "
        "classification TEXT NOT NULL, final_home INTEGER, final_away INTEGER, "
        "final_total INTEGER, result_at TEXT NOT NULL, "
        "final_result_status TEXT NOT NULL DEFAULT 'UNKNOWN', "
        "UNIQUE(source_game_id))")
    con.execute(
        "INSERT INTO game_results (source_game_id, classification, final_home,"
        " final_away, final_total, result_at, final_result_status)"
        " VALUES (?, 'BETUAL_NBA', ?, ?, ?, ?, ?)"
        " ON CONFLICT(source_game_id) DO UPDATE SET"
        " final_total=excluded.final_total, result_at=excluded.result_at,"
        " final_result_status=excluded.final_result_status",
        (gid, final_total // 2, final_total - final_total // 2, final_total,
         at, status))
    con.commit()
    con.close()


def _live(gid):
    app = FastAPI()
    app.include_router(v4_router)
    body = TestClient(app).get("/api/v4/live").json()
    return next(g for g in body["games"] if g["game_id"] == gid)


def _projector(monkeypatch, progress=26.0, remaining=24.0, actual=1.5,
               required=4.69, market_status="LIVE"):
    """Serve the game's projector explicitly, so the alert condition and the
    game's checkpoint are deterministic (the trigger LINE still comes only
    from the stored snapshots)."""
    def row(source_game_id):
        return {
            "source_game_id": source_game_id, "classification": "BETUAL_NBA",
            "captured_at": datetime.now(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "period_label": "2nd Quarter", "clock": "06:00",
            "elapsed_game_minutes": 40.0 - remaining,
            "remaining_game_minutes": remaining,
            "progress_pct": progress, "current_total_points": 34,
            "live_total_line": LATER, "market_status": market_status,
            "market_age_seconds": 30, "actual_pts_per_min": actual,
            "required_pts_per_min": required, "pace_gap": actual - required,
            "status": "VALID",
        }
    monkeypatch.setattr(v4api, "_pace_projector_for", row)


def _ended(add, now, iso, gid, home, away, final_home, final_away,
           line=TRIGGER, opening=OPENING, moved=None):
    """A finished game: opening line, a line held through the checkpoints,
    optionally a moved line, then the terminal observation.  Returns the
    game's own final total."""
    snap = add(gid, home, away)
    snap(now - timedelta(minutes=55), 10, 8, 1, "05:00", opening)      # 12.5%
    snap(now - timedelta(minutes=45), 24, 20, 2, "08:00", line)        # 30%
    snap(now - timedelta(minutes=35), 40, 34, 2, "00:00", line)        # 50%
    snap(now - timedelta(minutes=25), 52, 46, 3, "06:00",
         line if moved is None else moved)                             # 60%
    snap(now - timedelta(minutes=3), final_home, final_away, 4, "00:00",
         line if moved is None else moved, status="ended",
         period_label="Finished")                                      # 100%
    return final_home + final_away


# ══════════════════════════════════════════════════════════════════════
# 2. THE RESULT IS NEVER GATED ON AN ALERT — cases A, B, C
#    (ended game, NO alert, coloured UNDER / OVER / PUSH)
# ══════════════════════════════════════════════════════════════════════

def _no_alert(monkeypatch):
    """The condition cannot hold, so the game never raises an alert — while
    the game is otherwise healthy and live.  This is the directive's
    'no alert' case, made deterministic."""
    _projector(monkeypatch, progress=60.0, actual=12.0, required=4.69)


def test_A_ended_game_with_no_alert_is_coloured_under(store, monkeypatch):
    """Game A: no alert, final UNDER the line → coloured UNDER."""
    st, add, now, iso, db = store
    _no_alert(monkeypatch)
    _ended(add, now, iso, "8101", "NoA Home", "NoA Away", 93, 91)  # 184
    _write_settled(db, "8101", 184, iso(now - timedelta(minutes=1)))

    g = _live("8101")
    assert g["under_alert"]["active"] is False, "the game never alerted"
    fr = g["final_result"]
    assert fr["status"] == "under", fr
    assert fr["final_total"] == 184.0
    assert fr["line"] == TRIGGER
    assert fr["authoritative"] is True and fr["final_source"] == "settled"


def test_B_ended_game_with_no_alert_is_coloured_over(store, monkeypatch):
    """Game B: no alert, final OVER the line → coloured OVER."""
    st, add, now, iso, db = store
    _no_alert(monkeypatch)
    _ended(add, now, iso, "8102", "NoB Home", "NoB Away", 96, 94)  # 190
    _write_settled(db, "8102", 190, iso(now - timedelta(minutes=1)))

    g = _live("8102")
    assert g["under_alert"]["active"] is False
    assert g["final_result"]["status"] == "over", g["final_result"]


def test_C_ended_game_with_no_alert_is_coloured_push(store, monkeypatch):
    """Game C: no alert, final EXACTLY the line → PUSH (neutral)."""
    st, add, now, iso, db = store
    _no_alert(monkeypatch)
    # an integer line so the push branch is reachable at all
    _ended(add, now, iso, "8103", "NoC Home", "NoC Away", 94, 94, line=188.0)
    _write_settled(db, "8103", 188, iso(now - timedelta(minutes=1)))

    g = _live("8103")
    assert g["under_alert"]["active"] is False
    fr = g["final_result"]
    assert fr["status"] == "push", fr
    assert fr["final_total"] == fr["line"] == 188.0


def test_no_alert_game_the_alert_state_does_not_gate_the_result(store,
                                                               monkeypatch):
    """The SEPARATION the directive demands: a game whose quantitative
    condition is satisfied but whose alert is suppressed (the market line is
    stale) STILL gets a result colour.  If the alert state gated colouring,
    this game would be left uncoloured."""
    st, add, now, iso, db = store
    # condition satisfied (required > avg*1.04, actual < avg) but the market
    # is STALE, so the authoritative market gate suppresses the alert
    _projector(monkeypatch, progress=60.0, actual=8.0, required=10.0,
               market_status="STALE")
    _ended(add, now, iso, "8104", "Gated Home", "Gated Away", 93, 91)
    _write_settled(db, "8104", 184, iso(now - timedelta(minutes=1)))

    g = _live("8104")
    assert g["under_alert_eligibility"]["eligible"] is False, \
        g["under_alert_eligibility"]
    assert g["under_alert_eligibility"]["reason"] == "market_stale", \
        g["under_alert_eligibility"]
    assert g["under_alert"]["active"] is False
    assert g["final_result"]["status"] == "under", g["final_result"]


# ══════════════════════════════════════════════════════════════════════
# 3. THE ALERT OUTCOME KEEPS USING THE FROZEN TRIGGER LINE — D, E, F, I
# ══════════════════════════════════════════════════════════════════════

def _alerted(store, monkeypatch, gid, final_home, final_away):
    """A game that RAISED an UNDER alert at the 50% checkpoint: the line at
    the 50% checkpoint is 160.5, the market then MOVES to 162.5, and the game
    finishes with the given score.  Returns the served game."""
    st, add, now, iso, db = store
    _projector(monkeypatch, progress=60.0, actual=8.0, required=10.0)
    snap = add(gid, f"{gid} Home", f"{gid} Away")
    snap(now - timedelta(minutes=55), 10, 8, 1, "05:00", OPENING)   # 12.5%
    snap(now - timedelta(minutes=45), 24, 20, 2, "08:00", 170.5)    # 30%
    snap(now - timedelta(minutes=35), 40, 34, 2, "00:00", 160.5)    # 50% TRIGGER
    snap(now - timedelta(minutes=25), 52, 46, 3, "06:00", 162.5)    # 60% moved
    snap(now - timedelta(minutes=3), final_home, final_away, 4, "00:00",
         162.5, status="ended", period_label="Finished")            # 100%
    return db


def test_D_alert_with_final_below_the_frozen_trigger_line_is_under(store,
                                                                   monkeypatch):
    """158 < 160.5 → UNDER, even though the market moved up to 162.5."""
    db = _alerted(store, monkeypatch, "8201", 79, 79)               # 158
    _write_settled(db, "8201", 158, datetime.now(timezone.utc)
                   .strftime("%Y-%m-%dT%H:%M:%S.%fZ"))

    g = _live("8201")
    oc = g["under_alert_outcome"]
    bc = oc["by_checkpoint"]["50"]
    assert bc["trigger_total"] == 160.5          # the FROZEN line
    assert oc["final_total"] == 158.0
    assert bc["status"] == "under", bc            # 158 < 160.5
    # the moved line (162.5) never entered the comparison
    assert outcome_status(162.5, 158.0) == "under"


def test_E_alert_with_final_above_the_frozen_trigger_line_is_over(store,
                                                                  monkeypatch):
    """162 > 160.5 → OVER."""
    db = _alerted(store, monkeypatch, "8202", 81, 81)               # 162
    _write_settled(db, "8202", 162, datetime.now(timezone.utc)
                   .strftime("%Y-%m-%dT%H:%M:%S.%fZ"))

    g = _live("8202")
    bc = g["under_alert_outcome"]["by_checkpoint"]["50"]
    assert bc["trigger_total"] == 160.5
    assert bc["status"] == "over", bc


def test_F_alert_with_final_equal_to_the_frozen_trigger_line_is_push(store,
                                                                     monkeypatch):
    """160.5 == 160.5 → PUSH."""
    db = _alerted(store, monkeypatch, "8203", 80, 80)                # 160
    _write_settled(db, "8203", 160.5, datetime.now(timezone.utc)
                   .strftime("%Y-%m-%dT%H:%M:%S.%fZ"))

    g = _live("8203")
    bc = g["under_alert_outcome"]["by_checkpoint"]["50"]
    assert bc["trigger_total"] == 160.5
    assert bc["final_total"] == 160.5
    assert bc["status"] == "push", bc


def test_I_a_later_market_move_cannot_change_the_alert_outcome(store,
                                                               monkeypatch):
    """The already-determined outcome is immovable: the SAME game re-served
    after further line movement keeps the frozen 160.5 / UNDER."""
    st, add, now, iso, db = store
    _projector(monkeypatch, progress=60.0, actual=8.0, required=10.0)
    snap = add("8204", "Move Home", "Move Away")
    snap(now - timedelta(minutes=55), 10, 8, 1, "05:00", OPENING)
    snap(now - timedelta(minutes=45), 24, 20, 2, "08:00", 170.5)
    snap(now - timedelta(minutes=35), 40, 34, 2, "00:00", 160.5)   # 50% trigger
    snap(now - timedelta(minutes=25), 52, 46, 3, "06:00", 162.5)
    snap(now - timedelta(minutes=10), 70, 64, 4, "06:00", 199.5)   # moved HARD
    snap(now - timedelta(minutes=3), 79, 79, 4, "00:00", 205.5,
         status="ended", period_label="Finished")                  # final 158
    _write_settled(db, "8204", 158, iso(now - timedelta(minutes=1)))

    g = _live("8204")
    bc = g["under_alert_outcome"]["by_checkpoint"]["50"]
    assert bc["trigger_total"] == 160.5, bc      # frozen, not 205.5
    assert bc["status"] == "under", bc
    assert g["market"]["total_line"] == 205.5    # the market DID move later
    assert g["final_result"]["status"] == "under"


# ══════════════════════════════════════════════════════════════════════
# 4. TIMING — an unproven result is never coloured, a proven one always is
#    (cases G and H)
# ══════════════════════════════════════════════════════════════════════

def test_G_a_game_without_a_final_is_never_coloured(store, monkeypatch):
    """G: while the game is still running there is no final, so no colour is
    claimed — and no verdict is invented from the live/pace numbers."""
    st, add, now, iso, db = store
    _projector(monkeypatch, progress=60.0, actual=8.0, required=10.0)
    snap = add("8301", "Live Home", "Live Away")
    snap(now - timedelta(minutes=20), 40, 34, 2, "00:00", TRIGGER)   # 50%
    snap(now - timedelta(minutes=2), 52, 46, 3, "06:00", LATER)      # 60%

    g = _live("8301")
    fr = g["final_result"]
    assert fr["status"] is None, fr
    assert fr["final_total"] is None and fr["authoritative"] is False


def test_H_once_the_final_is_settled_the_colour_is_applied(store,
                                                           monkeypatch):
    """H: the game above, once the scorecard records the verified final, is
    coloured — same payload shape, status now present."""
    st, add, now, iso, db = store
    _projector(monkeypatch, progress=60.0, actual=8.0, required=10.0)
    snap = add("8302", "Late Home", "Late Away")
    snap(now - timedelta(minutes=55), 10, 8, 1, "05:00", OPENING)
    snap(now - timedelta(minutes=35), 40, 34, 2, "00:00", TRIGGER)   # 50%
    snap(now - timedelta(minutes=2), 52, 46, 3, "06:00", LATER)      # 60%

    # no final yet → uncoloured
    assert _live("8302")["final_result"]["status"] is None

    # the authoritative final lands
    _write_settled(db, "8302", 184, iso(now - timedelta(minutes=1)))
    fr = _live("8302")["final_result"]
    assert fr["status"] == "under", fr
    assert fr["authoritative"] is True


def test_an_UNKNOWN_result_row_is_not_an_authoritative_final(store,
                                                             monkeypatch):
    """A ``game_results`` row that is not ``final_result_status='OK'`` is not
    a verified final, so it never makes a result authoritative."""
    st, add, now, iso, db = store
    _no_alert(monkeypatch)
    # a game with NO terminal observation and only an UNKNOWN result row
    snap = add("8303", "Unk Home", "Unk Away")
    snap(now - timedelta(minutes=20), 40, 34, 2, "00:00", TRIGGER)
    _write_settled(db, "8303", 184, iso(now - timedelta(minutes=1)),
                   status="UNKNOWN")

    g = _live("8303")
    assert g["final_result"]["authoritative"] is False
    assert g["final_result"]["final_source"] != "settled"


# ══════════════════════════════════════════════════════════════════════
# 5. ONE authoritative result colour — the frontend (Node)
# ══════════════════════════════════════════════════════════════════════

def _extract(js: str, name: str) -> str:
    """The source of one top-level ``function name(...) {...}`` by brace
    matching (template-literal ``${...}`` pairs are balanced, so counting
    braces is sound for these functions)."""
    i = js.index("function " + name + "(")
    k = js.index("{", i)
    depth = 0
    while k < len(js):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                return js[i:k + 1]
        k += 1
    raise AssertionError("unbalanced function body: " + name)


def _run_node(script: str):
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout)


_STUBS = """
const num1 = (x) => (x == null || !isFinite(x)) ? "\\u2013" : Number(x).toFixed(1);
const ALERT_RESULT_WORDS = { under: "UNDER", over: "OVER", push: "PUSH",
  no_final: "NO FINAL", unknown: "NO FINAL" };
"""


def test_one_colour_mapping_shared_by_every_surface():
    """There is exactly ONE status → class mapping, and the alert surfaces
    resolve through it — so the card and the alert panels can never disagree
    about what colour a verdict is."""
    js = DASH_JS.read_text(encoding="utf-8")
    assert "function resultColorClass(" in js
    # the alert wrapper delegates rather than repeating the mapping
    oc = _extract(js, "alertOutcomeClass")
    assert "resultColorClass(oc.status)" in oc, oc
    assert "al-under" not in oc and "al-over" not in oc, oc
    # and the mapping itself is exactly the project's verdict palette
    r = _run_node(_STUBS + _extract(js, "resultColorClass") + """
console.log(JSON.stringify({
  under: resultColorClass("under"), over: resultColorClass("over"),
  push: resultColorClass("push"), none: resultColorClass(null),
  nf: resultColorClass("no_final"), junk: resultColorClass("weird"),
  upper: resultColorClass("UNDER"), mixed: resultColorClass(" Under "),
  quoted: resultColorClass('"Over"') }));""")
    # RESULTED ALERTS result-coverage directive (2026-09-16): the mapping is
    # NORMALIZED (any spelling of a result classifies identically) and an
    # unknown/malformed status gets the EXPLICIT al-unknown fallback — never
    # silently uncoloured.  Only the pending family (no result yet) is null.
    assert r == {"under": "al-under", "over": "al-over", "push": "al-push",
                 "none": None, "nf": None, "junk": "al-unknown",
                 "upper": "al-under", "mixed": "al-under",
                 "quoted": "al-over"}, r


def test_the_card_renders_the_final_result_and_its_accent():
    """The game card shows the settled result — UNDER green / OVER red /
    PUSH neutral — and carries the matching accent class, using the ONE
    mapping.  An unproven result renders NOTHING (never a placeholder)."""
    js = DASH_JS.read_text(encoding="utf-8")
    r = _run_node(_STUBS + _extract(js, "resultColorClass")
                  + _extract(js, "finalResultHTML")
                  + _extract(js, "finalResultCardClass") + """
const g = (fr) => ({ final_result: fr });
console.log(JSON.stringify({
  under: finalResultHTML(g({ status: "under", final_total: 184, line: 187.5,
                             final_source: "settled", authoritative: true })),
  over: finalResultHTML(g({ status: "over", final_total: 190, line: 187.5,
                            final_source: "settled", authoritative: true })),
  push: finalResultHTML(g({ status: "push", final_total: 188, line: 188,
                            final_source: "settled", authoritative: true })),
  none: finalResultHTML(g({ status: null })),
  missing: finalResultHTML({}),
  clsUnder: finalResultCardClass(g({ status: "under" })),
  clsOver: finalResultCardClass(g({ status: "over" })),
  clsPush: finalResultCardClass(g({ status: "push" })),
  clsNone: finalResultCardClass(g({ status: null })) }));""")
    assert "card-result al-under" in r["under"] and "RESULT UNDER" in r["under"]
    assert "184.0" in r["under"] and "187.5" in r["under"]
    assert "card-result al-over" in r["over"] and "RESULT OVER" in r["over"]
    assert "card-result al-push" in r["push"] and "RESULT PUSH" in r["push"]
    # an unproven / absent result renders NOTHING — no colour, no verdict
    assert r["none"] == "" and r["missing"] == "", r
    assert r["clsUnder"] == "card-result-under"
    assert r["clsOver"] == "card-result-over"
    assert r["clsPush"] == "card-result-push"
    assert r["clsNone"] == ""


def test_the_card_wires_the_result_in_and_the_styles_exist():
    """Structural: the card renders the result block, the card element takes
    the accent class, and the stylesheet defines the three verdict colours
    (reusing the project palette)."""
    js = DASH_JS.read_text(encoding="utf-8")
    card = _extract(js, "cardHTML")
    assert "finalResultHTML(g)" in card, "the card must render the result"
    # the accent class is applied to the card ELEMENT in the render loop
    assert "finalResultCardClass(g)" in js
    assert "card-result-under" in js and "card-result-over" in js \
        and "card-result-push" in js
    css = STYLES_CSS.read_text(encoding="utf-8")
    for sel in (".card-result.al-under", ".card-result.al-over",
                ".card-result.al-push", ".card.card-result-under",
                ".card.card-result-over", ".card.card-result-push"):
        assert sel in css, sel
    # the project's own verdict colours, not new ones
    assert "#22c55e" in css and "#ef4444" in css
