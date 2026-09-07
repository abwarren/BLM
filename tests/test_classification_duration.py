"""Classification-specific regulation duration — BETUAL 4×10=40, CYBER 4×12=48.

CYBER_2K26 is confirmed 4 × 12-minute quarters (source virtual clock reads
12:00 at every quarter start); BETUAL_NBA is 4 × 10 (source clock reads
10:00 at quarter start).  Every elapsed-time / progress / pace / checkpoint
calculation must use the game's own regulation duration — never one shared
40-minute assumption.

Covers:
  - duration_for(): BETUAL (10,40), CYBER (12,48), unknown -> default (10,40)
  - clock_minutes(): classification-aware quarter length
  - progress: CYBER 24 elapsed = 50%, CYBER 20 elapsed = 41.6667%,
    BETUAL 20 elapsed = 50%
  - pace: 48-minute scale for CYBER, 40-minute for BETUAL
  - checkpoint_market rows for a CYBER game freeze the CORRECT 48-minute
    basis (pct50 at elapsed 24, progress 0.5)
  - the /game/{id} reader exposes point-in-time checkpoint state from the
    immutable snapshot join (score/clock/period/elapsed/remaining/pace) —
    never the terminal state for an early checkpoint
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.projection import (clock_minutes, duration_for,
                               pace_from_snapshots, project)
from blm_v4.scorecard import Scorecard
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ── Pure-function tests ──────────────────────────────────────────────


def test_duration_for_classifications():
    assert duration_for("BETUAL_NBA") == (10.0, 40.0)
    assert duration_for("CYBER_2K26") == (12.0, 48.0)
    # unknown / missing -> BETUAL default (historical basis preserved)
    assert duration_for(None) == (10.0, 40.0)
    assert duration_for("") == (10.0, 40.0)
    assert duration_for("SOME_OTHER") == (10.0, 40.0)


def test_clock_minutes_classification_aware():
    # Q2 with 6:00 remaining: BETUAL 10+4=14, CYBER 12+6=18
    assert clock_minutes(2, "06:00", 10.0) == 14.0
    assert clock_minutes(2, "06:00", 12.0) == 18.0
    # CYBER Q3 start sentinel (clock 12:00) = exactly 24 elapsed
    assert clock_minutes(3, "12:00", 12.0) == 24.0
    # default arg keeps the historical 10-minute basis
    assert clock_minutes(2, "06:00") == 14.0


def _rows(cls: str, el_min: int, n: int = 2):
    """n synthetic scored snapshot dicts ending at elapsed el_min."""
    base = _now() - timedelta(hours=1)
    out = []
    for i in range(n):
        el = max(0, el_min - (n - 1 - i) * 4)
        q = min(el // 12 + 1, 4) if cls == "CYBER_2K26" else min(el // 10 + 1, 4)
        qlen = 12.0 if cls == "CYBER_2K26" else 10.0
        mod = el % int(qlen)
        clock = "00:00" if (q == 4 and el >= 40) else \
            (f"{int(qlen):02d}:00" if mod == 0 else f"{int(qlen - mod):02d}:00")
        out.append({
            "classification": cls, "captured_at": _iso(base + timedelta(minutes=2 * i)),
            "quarter": q, "clock": clock, "period_label": f"{q}th Quarter",
            "home_score": el // 2, "away_score": el // 2,
            "total_line": 100.0,
        })
    return out


def test_progress_cyber_24min_is_half():
    p = project(_rows("CYBER_2K26", 24, 3))
    assert p["elapsed_minutes"] == 24.0
    assert p["progress"] == 0.5


def test_progress_cyber_20min_is_4167():
    p = project(_rows("CYBER_2K26", 20, 3))
    assert p["elapsed_minutes"] == 20.0
    assert p["progress"] == pytest.approx(0.4166667, abs=1e-4)


def test_progress_betual_20min_is_half():
    p = project(_rows("BETUAL_NBA", 20, 3))
    assert p["elapsed_minutes"] == 20.0
    assert p["progress"] == 0.5


def test_pace_scales_by_classification_duration():
    # same 9 pts over the same wall span -> 40-min vs 48-min scale
    base = _now() - timedelta(hours=1)
    def pair(cls: str):
        return [
            {"classification": cls, "captured_at": _iso(base),
             "quarter": 1, "clock": "08:00", "home_score": 3, "away_score": 3},
            {"classification": cls, "captured_at": _iso(base + timedelta(minutes=3)),
             "quarter": 1, "clock": "04:00", "home_score": 7, "away_score": 8},
        ]
    bet = pace_from_snapshots(pair("BETUAL_NBA"))
    cyb = pace_from_snapshots(pair("CYBER_2K26"))
    assert bet == pytest.approx(9 / 3 * 40.0)   # 120.0
    assert cyb == pytest.approx(9 / 3 * 48.0)   # 144.0


# ── End-to-end: a CYBER game's checkpoint_market rows + reader ───────

# 21 snapshots at 2 regulation-minutes apart: el 0..32 step 2, then 36/40/44/48.
_CYBER_ELS = list(range(0, 34, 2)) + [36, 40, 44, 48]


def _build_cyber(db: Path, gid: str = "C-CYBER") -> None:
    st = PokerBetStore(db)
    base = _now() - timedelta(hours=2)
    game = PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="comp-cyber", competition_slug="cyber-basketball-2k26",
        competition="Cyber Basketball 2K26", region="Virtual Matches",
        game_family="cyber", classification="CYBER_2K26",
        sport="basketball", home_team=f"{gid} Home Cyber",
        away_team=f"{gid} Away Cyber",
        game_slug=f"{gid.lower()}-game",
        source_url=f"https://x/{gid}", status="ended",
        first_seen_at=_iso(base),
        last_seen_at=_iso(base + timedelta(minutes=2 * (len(_CYBER_ELS) - 1))),
    )
    gid_db = st.upsert_game(game)
    for i, el in enumerate(_CYBER_ELS):
        q = min(el // 12 + 1, 4)
        qlen = 12.0
        if el == 48:
            clock, label, q = "00:00", "Finished", 4
        else:
            mod = el % int(qlen)
            clock = f"{int(qlen):02d}:00" if mod == 0 else f"{int(qlen - mod):02d}:00"
            label = f"{q}th Quarter"
        obs = MarketObservation(
            source="PokerBet", source_game_id=gid, classification="CYBER_2K26",
            captured_at=_iso(base + timedelta(minutes=2 * i)),
            home_team=f"{gid} Home Cyber", away_team=f"{gid} Away Cyber",
            home_score=el, away_score=el,   # total = 2*el (0 .. 96)
            period_label=label, quarter=q, clock=clock,
            game_status="ended", total_line=200.0,
            spread=None, w1_odds=None, w2_odds=None,
            markets_json="{}",
        )
        st.insert_snapshot(gid_db, obs, force=True)


@pytest.fixture
def cyb(tmp_path, monkeypatch):
    dbfile = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(dbfile))
    _build_cyber(dbfile)
    s = Scorecard(dbfile)
    s.capture_results()
    s.record_checkpoint_market()
    return dbfile


def _cm_rows(dbfile: Path, gid: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{dbfile}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM checkpoint_market WHERE source_game_id=? "
            "ORDER BY checkpoint_pct", (gid,))]
    finally:
        conn.close()


def test_cyber_checkpoint_rows_freeze_48min_basis(cyb):
    """CYBER pct50 sits at elapsed 24 (progress 0.5), not elapsed 20."""
    rows = {r["checkpoint_pct"]: r for r in _cm_rows(cyb, "C-CYBER")}
    assert rows[50]["progress"] == 0.5
    assert rows[50]["elapsed_minutes"] == 24.0
    # pct100 = terminal (48 elapsed)
    assert rows[100]["progress"] == 1.0
    assert rows[100]["elapsed_minutes"] == 48.0


def test_cyber_reader_exposes_point_in_time_state(cyb):
    """The /game/{id} reader joins checkpoint_timestamp to the immutable
    snapshot: score/clock/period at the checkpoint, NOT the terminal state."""
    from blm_v4.api import _game_checkpoint_market
    conn = sqlite3.connect(f"file:{cyb}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = {r["checkpoint_pct"]: r
                for r in _game_checkpoint_market(conn, "C-CYBER")}
    finally:
        conn.close()
    r50 = rows[50]
    # point-in-time state at the 50% checkpoint (elapsed 24, 24-24 score)
    assert r50["home_score_at_checkpoint"] == 24
    assert r50["away_score_at_checkpoint"] == 24
    assert r50["current_total"] == 48
    assert r50["clock_at_checkpoint"] == "12:00"
    assert r50["quarter"] == 3
    assert r50["elapsed_minutes"] == 24.0
    assert r50["progress"] == 0.5
    assert r50["remaining_minutes"] == 24.0
    # ANTI-LEAKAGE: terminal state (final 96 total, Finished) never leaks in
    assert r50["current_total"] != r50["actual_final_total"] == 96
    assert r50["period_label_at_checkpoint"] != "Finished"
    assert r50["current_pace"] == pytest.approx(48.0 / 24.0, abs=1e-9)
    assert r50["required_pace"] == 6.33  # round((200-48)/24, 2)
    # Fair stays the single existing definition
    assert r50["blm_fair_value"] is not None
    assert "projected_final" not in r50  # no duplicate Fair definition
    # terminal row's snapshot IS the final state (by definition)
    r100 = rows[100]
    assert r100["home_score_at_checkpoint"] == 48
    assert r100["clock_at_checkpoint"] == "00:00"
    assert r100["period_label_at_checkpoint"] == "Finished"
    assert r100["remaining_minutes"] == 0.0
