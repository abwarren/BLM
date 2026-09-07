"""PREDICTIVE vs SETTLEMENT — game-detail separation (directive).

The terminal end-state observation (e.g. 40.00/40.00 = 100.0% TERMINAL)
must NOT appear as a predictive checkpoint.  Presentation/API separation
only — the terminal predicate, projection, directional logic, residual,
confirmation specification, research calculations and storage are
untouched:

  - the terminal row remains available in audit/settlement
    (``checkpoints_settlement`` / ``market_vs_fair_settlement``);
  - the terminal row is absent from the predictive checkpoint
    collections (``checkpoints`` / ``market_vs_fair`` default to
    predictive_eligible=1 / terminal=0);
  - non-terminal rows near the end (the 39.25/40.00 = 98.1% final
    snapshot) remain present and predictive-ELIGIBLE — the percentage
    bucket never decides terminality, the game-time evidence does;
  - no stored row is deleted or rewritten (both collections read the
    same immutable storage).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

import blm_v4.api as v4api
from blm_v4.api import router as v4_router
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.scorecard import Scorecard
from blm_v4.terminal_eligibility import TERMINAL_EXCLUSION_REASON
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DASH_STATIC = REPO / "blm_v4" / "dashboard" / "static"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# (offset_minutes, home, away, quarter, clock) — a full BETUAL_NBA game
# (40:00).  The FINAL frame differs per scenario via _FINAL.
_FRAMES = [
    (0, 0, 0, 1, "08:00"), (1, 5, 4, 1, "06:00"), (2, 10, 8, 1, "04:00"),
    (3, 14, 12, 1, "02:00"), (4, 17, 15, 1, "00:30"), (5, 20, 18, 2, "08:00"),
    (6, 23, 20, 2, "06:00"), (7, 26, 23, 2, "04:00"), (8, 35, 31, 2, "00:30"),
    (9, 32, 29, 3, "08:00"), (10, 36, 33, 3, "08:00"), (12, 42, 38, 3, "04:00"),
    (14, 46, 42, 3, "00:30"), (16, 50, 48, 4, "08:00"), (18, 54, 51, 4, "04:00"),
]
_FINAL_TERMINAL = (24, 60, 55, 4, "00:00")      # 40.00/40.00 = 100.0%
_FINAL_981 = (20, 57, 53, 4, "00:45")           # 39.25/40.00 = 98.1%


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Two ended games: G-SEP (terminal end state) and G-SEP98
    (non-terminal 98.1% end state)."""
    dbfile = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(dbfile))
    monkeypatch.setattr(v4api, "STATE_FILE", tmp_path / "collector_state.json")
    st = PokerBetStore(dbfile)
    base = _now() - timedelta(hours=3)

    def add_game(gid: str, final: tuple) -> None:
        game = PokerBetGame(
            source="PokerBet", source_game_id=gid,
            competition_id="comp-1", competition_slug="betual-tbsl",
            competition="Betual NBA", region="Virtual Matches",
            game_family="betual", classification="BETUAL_NBA",
            sport="basketball", home_team=f"{gid} Home Virtual",
            away_team=f"{gid} Away Virtual",
            game_slug=f"{gid.lower()}-game",
            source_url=f"https://x/{gid}", status="ended",
            first_seen_at=_iso(base), last_seen_at=_iso(base),
        )
        gid_db = st.upsert_game(game)
        for off, hs, as_, q, clock in _FRAMES + [final]:
            obs = MarketObservation(
                source="PokerBet", source_game_id=gid,
                classification="BETUAL_NBA", captured_at=_iso(base + timedelta(minutes=off)),
                home_team=f"{gid} Home Virtual", away_team=f"{gid} Away Virtual",
                home_score=hs, away_score=as_, period_label=f"{q}th Quarter",
                quarter=q, clock=clock, game_status="ended", total_line=None,
                spread=None, w1_odds=None, w2_odds=None, markets_json="{}",
            )
            st.insert_snapshot(gid_db, obs, force=True)

    add_game("G-SEP", _FINAL_TERMINAL)
    add_game("G-SEP98", _FINAL_981)
    return st, dbfile


@pytest.fixture
def sc(db):
    st, dbfile = db
    sc = Scorecard(dbfile)
    sc.record_predictions()
    sc.record_fixed_checkpoints()
    sc.capture_results()
    sc.record_checkpoint_market()
    return sc, st


@pytest.fixture
def app(db):
    application = FastAPI()
    application.include_router(v4_router)
    application.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
                      name="test_dashboard_static_sep")
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


def _detail(client, gid: str) -> dict:
    resp = client.get(f"/api/v4/game/{gid}")
    assert resp.status_code == 200
    return resp.json()


# ══════════════════════════════════════════════════════════════════════

def test_terminal_row_absent_from_predictive_checkpoints(client, sc):
    """The 40.00/40.00 (100.0% TERMINAL) end state must NOT appear as a
    predictive checkpoint: predictive rows default to terminal=0 /
    predictive_eligible=1, and the `final` prediction row is not among
    them."""
    d = _detail(client, "G-SEP")
    checks = [c["check"] for c in d["checkpoints"]]
    assert "final" not in checks
    assert all(c.get("terminal", 0) == 0 for c in d["checkpoints"])
    assert all(c.get("predictive_eligible", 1) == 1 for c in d["checkpoints"])
    # the 10–90% predictive population is intact
    assert {c["check"] for c in d["checkpoints"]} >= {
        "pct10", "pct50", "pct90", "q1"}
    # market-vs-fair predictive set: no pct100 terminal row
    assert all(r["checkpoint_pct"] != 100 for r in d["market_vs_fair"])
    assert all(r["terminal"] == 0 and r["predictive_eligible"] == 1
               for r in d["market_vs_fair"])


def test_terminal_row_preserved_in_settlement_audit(client, sc):
    """The terminal end state remains available — separately, stamped
    SETTLEMENT ONLY (terminal=1, predictive_eligible=0, explicit
    reason) — with the settlement outcome attached."""
    d = _detail(client, "G-SEP")
    settle = d["checkpoints_settlement"]
    assert settle, "terminal prediction row must remain available"
    assert all(c["terminal"] == 1 and c["predictive_eligible"] == 0
               for c in settle)
    assert all(c["exclusion_reason"] == TERMINAL_EXCLUSION_REASON
               for c in settle)
    assert any(c["check"] == "final" for c in settle)
    assert all(c["actual_final"] == 115 for c in settle)   # 60 + 55

    mvf_settle = d["market_vs_fair_settlement"]
    assert mvf_settle, "terminal market-vs-fair row must remain available"
    r100 = next(r for r in mvf_settle if r["checkpoint_pct"] == 100)
    assert r100["terminal"] == 1 and r100["predictive_eligible"] == 0
    assert r100["predictive_validation"] == "PREDICTIVE VALIDATION: EXCLUDED"
    assert r100["elapsed_minutes"] == 40.0                  # 40.00/40.00
    assert r100["progress"] == 1.0
    assert r100["actual_final_total"] == 115
    # explicit summary of the separation
    te = d["terminal_exclusion"]
    assert te["n_terminal"] >= 1
    assert te["n_predictive_eligible"] == len(d["market_vs_fair"])
    assert te["n_rows"] == te["n_terminal"] + te["n_predictive_eligible"]


def test_terminal_row_still_stored_never_deleted(client, sc):
    """Presentation/API separation only: the underlying immutable rows
    still exist in storage after the split."""
    sc_obj, _st = sc
    conn = sqlite3.connect(sc_obj._db_path)
    try:
        pred = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE source_game_id='G-SEP' "
            "AND checkpoint='final'").fetchone()[0]
        cm = conn.execute(
            "SELECT COUNT(*) FROM checkpoint_market WHERE source_game_id='G-SEP' "
            "AND checkpoint_pct=100").fetchone()[0]
    finally:
        conn.close()
    assert pred == 1, "terminal prediction row must remain in storage"
    assert cm == 1, "terminal checkpoint_market row must remain in storage"


def test_nonterminal_981_final_snapshot_remains_predictive(client, sc):
    """A final snapshot at 39.25/40.00 (98.1%) is NON-TERMINAL: its
    pct100/final rows stay in the PREDICTIVE collections and eligible —
    the bucket label never decides terminality."""
    d = _detail(client, "G-SEP98")
    # nothing about this game is terminal
    assert d["market_vs_fair_settlement"] == []
    assert d["checkpoints_settlement"] == []
    r100 = next(r for r in d["market_vs_fair"] if r["checkpoint_pct"] == 100)
    assert r100["terminal"] == 0 and r100["predictive_eligible"] == 1
    assert r100["elapsed_minutes"] == 39.25
    assert r100["progress"] == pytest.approx(0.9813, abs=1e-3)
    finals = [c for c in d["checkpoints"] if c["check"] == "final"]
    assert finals, "non-terminal final checkpoint stays predictive"


def test_dashboard_renders_predictive_and_settlement_sections(client, sc):
    """The deployed modal carries the two separate sections —
    PREDICTIVE CHECKPOINTS (10–90%) and SETTLEMENT / TERMINAL — and no
    single mixed table."""
    js = client.get("/static/dashboard.js").text
    assert "PREDICTIVE CHECKPOINTS — 10–90% non-terminal observations" in js
    assert "SETTLEMENT / TERMINAL" in js
    assert "TERMINAL — SETTLEMENT ONLY" in js
    assert "checkpoints_settlement" in js
    assert "market_vs_fair_settlement" not in js  # settlement served via checkpoints_settlement rows
