"""Transient score-regression tolerance in the quality gate.

The audit found 62 games marked INVALID by a single 1-4pt score dip on an
EVENT-VIEW row (the slow event page rendering a stale scoreboard 1-4 pts
behind the live state for a row or two) — never identity contamination.
Every one of the 64 poisoning dips was 1-4 pts; none was a >50pt jump, an
identity change, or a classification change.

The fix: in _snapshot_history_quality, a regression of <= 4 points is
treated as a transient glitch — the offending snapshot is SKIPPED and the
monotonicity scan continues from the last valid pre-glitch state, PROVIDED
the scores recover to >= the pre-glitch level within 5 valid rows.
Genuine contamination (large regression, >50pt impossible jump, sustained
non-recovery) still invalidates.

Covers:
  1. transient V-shaped 1-4pt regression (57-49 -> 55-49 -> 57-49) -> VALID
  2. stale-clock / event-view small regression (recovers) -> VALID
  3. >50pt impossible jump in <90s -> INVALID
  4. sustained / non-recovering regression -> INVALID
  5. final INVALID remains final (no automatic rescore)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.scorecard import Scorecard, _snapshot_history_quality

HERE = Path(__file__).resolve().parent
BASE = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row(h: int, a: int, t_min: float, gid: str = "G-TRANSIENT",
         period: str = "1st Quarter", clock: str = "10:00",
         classification: str = "BETUAL_NBA") -> dict:
    return {
        "captured_at": _iso(BASE + timedelta(minutes=t_min)),
        "source_game_id": gid, "classification": classification,
        "home_score": h, "away_score": a,
        "period_label": period, "clock": clock,
    }


def _rows(*states) -> list[dict]:
    """Build rows 10s apart from (h, a) states; returns with a common gid."""
    gid = "G-TRANSIENT"
    out = []
    for i, (h, a) in enumerate(states):
        out.append(_row(h, a, i / 6.0, gid=gid))
    return out


# ── 1. transient V-shaped 1-4pt regression -> VALID ──────────────────

def test_vshaped_small_regression_is_valid():
    """57-49 -> 55-49 (stale dip) -> 57-49 (recovery): a transient event-view
    render glitch — the game must stay VALID (was INVALID before the fix)."""
    rows = _rows(
        (50, 45), (53, 47), (57, 49), (55, 49),   # dip -2 home
        (57, 49), (60, 50), (63, 52), (66, 53),   # recovered
    )
    status, reason = _snapshot_history_quality(rows)
    assert status == "OK", reason


def test_small_away_dip_recovers_is_valid():
    """74-58 -> 74-57 (stale -1 away) -> 76-57: recovers, VALID."""
    rows = _rows(
        (70, 55), (72, 56), (74, 58), (74, 57),   # dip -1 away
        (76, 57), (78, 58), (80, 59),
    )
    status, reason = _snapshot_history_quality(rows)
    assert status == "OK", reason


def test_max_tolerance_dip_four_recovers_is_valid():
    """A 4-point dip (the tolerance boundary) that recovers is VALID."""
    rows = _rows(
        (60, 60), (64, 62), (68, 64), (64, 64),   # dip -4 home
        (66, 64), (68, 64), (70, 65), (72, 66),   # recovery to >= 68
    )
    status, reason = _snapshot_history_quality(rows)
    assert status == "OK", reason


# ── 2. stale-clock / event-view small regression -> VALID ────────────

def test_stale_clock_event_view_regression_is_valid():
    """30798922 pattern: list row 75-59 @ 00:30, then a stale event-view row
    75-57 @ 00:45 (older game state), then 78-59 — VALID."""
    rows = _rows(
        (70, 55), (72, 57), (75, 59), (75, 57),   # stale EV read -2 away
        (78, 59), (80, 61), (83, 63), (85, 64),   # continues from truth
    )
    status, reason = _snapshot_history_quality(rows)
    assert status == "OK", reason


# ── 3. >50pt impossible jump -> INVALID ──────────────────────────────

def test_impossible_large_jump_is_invalid():
    """A 55pt home jump in <90s (the lobby-attribution contamination the
    gate exists to catch) must remain INVALID."""
    rows = _rows(
        (10, 10), (15, 12), (20, 14), (25, 16),
        (80, 18),   # +55 home in 10s — impossible
        (82, 20), (85, 22),
    )
    status, reason = _snapshot_history_quality(rows)
    assert status == "INVALID"
    assert "impossible" in reason or "regression" in reason


def test_large_regression_over_tolerance_is_invalid():
    """A >4pt score drop is a genuine regression (replay reset / wrong
    event) — INVALID regardless of later recovery."""
    rows = _rows(
        (50, 50), (60, 55), (70, 60), (20, 20),   # -50 drop = new instance
        (22, 21), (24, 22),
    )
    status, reason = _snapshot_history_quality(rows)
    assert status == "INVALID"
    assert "regression" in reason


# ── 4. sustained / non-recovering regression -> INVALID ──────────────

def test_sustained_small_regression_is_invalid():
    """A small dip that never recovers to the pre-glitch level (the score
    stays down) is a sustained regression -> INVALID."""
    rows = _rows(
        (50, 45), (55, 48), (58, 50), (58, 47),   # dip -3 away
        (60, 47), (62, 47), (64, 47), (66, 47),   # never back to 50
        (68, 47), (70, 47),
    )
    status, reason = _snapshot_history_quality(rows)
    assert status == "INVALID"
    assert "sustained" in reason


def test_repeated_small_dips_exhaust_budget_is_invalid():
    """Repeated small dips (score bounces down and stays below baseline
    across the recovery window) -> INVALID."""
    rows = _rows(
        (50, 50), (55, 52), (58, 54), (56, 54),   # dip
        (57, 54), (55, 55), (56, 55),             # keeps dipping, no recovery
        (57, 56), (58, 56), (60, 57),
    )
    status, reason = _snapshot_history_quality(rows)
    assert status == "INVALID"


# ── 5. existing final INVALID remains final (no automatic rescore) ───

def _build_game(db: Path, gid: str, scores: list[tuple[int, int]],
                gap_min: float = 3.0) -> None:
    """Insert a finished 20-snapshot game (Q1 10:00 -> Q4 00:00) with the
    given score trajectory at `gap_min` wall-minutes between snapshots."""
    from blm_v4.storage import PokerBetStore
    st = PokerBetStore(db)
    base = BASE - timedelta(hours=2)
    game = PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="comp-1", competition_slug="betual-tbsl",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA",
        sport="basketball", home_team=f"{gid} Home Virtual",
        away_team=f"{gid} Away Virtual",
        game_slug=f"{gid.lower()}-game",
        source_url=f"https://x/{gid}", status="ended",
        first_seen_at=_iso(base),
        last_seen_at=_iso(base + timedelta(minutes=gap_min * (len(scores) - 1))),
    )
    gid_db = st.upsert_game(game)
    n = len(scores)
    for i, (h, a) in enumerate(scores):
        q = min(i // 5 + 1, 4)
        clock = "00:00" if (i == n - 1 and q >= 4) else "10:00"
        obs = MarketObservation(
            source="PokerBet", source_game_id=gid, classification="BETUAL_NBA",
            captured_at=_iso(base + timedelta(minutes=gap_min * i)),
            home_team=f"{gid} Home Virtual", away_team=f"{gid} Away Virtual",
            home_score=h, away_score=a,
            period_label=f"{q}th Quarter", quarter=q, clock=clock,
            game_status="ended", total_line=None,
            spread=None, w1_odds=None, w2_odds=None, markets_json="{}",
        )
        st.insert_snapshot(gid_db, obs, force=True)


@pytest.fixture
def db(tmp_path, monkeypatch):
    dbfile = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(dbfile))
    # clean game WITH a transient V-dip -> should now record OK
    scores = [(0, 0), (8, 6), (16, 12), (24, 18), (30, 22),
              (36, 26), (44, 30), (50, 34), (55, 37), (57, 40),
              (56, 42), (58, 44), (63, 46), (68, 48), (74, 50),
              (80, 52), (86, 54), (92, 56), (96, 58), (100, 60)]
    _build_game(dbfile, "G-VDIP", scores)
    # game with a genuine >50pt impossible jump in <90s -> INVALID.
    # Built with a 1-second gap so the jump lands inside the 90s window.
    bad = [(0, 0), (8, 6), (16, 12), (24, 18), (30, 22),
           (36, 26), (90, 28), (94, 30), (98, 32), (102, 34),
           (106, 36), (110, 38), (114, 40), (118, 42), (122, 44),
           (126, 46), (130, 48), (134, 50), (138, 52), (142, 54)]
    _build_game(dbfile, "G-BAD", bad, gap_min=1 / 60.0)  # 1s gaps
    return dbfile


def _result_status(dbfile: Path, gid: str) -> str | None:
    conn = sqlite3.connect(f"file:{dbfile}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT final_result_status FROM game_results "
                         "WHERE source_game_id=?", (gid,)).fetchone()
        return r["final_result_status"] if r else None
    finally:
        conn.close()


def test_transient_dip_game_records_ok(db):
    """A game whose only flaw is a transient V-dip now records OK (was
    INVALID before the fix)."""
    s = Scorecard(db)
    s.capture_results()
    assert _result_status(db, "G-VDIP") == "OK"


def test_genuine_contamination_still_invalid(db):
    s = Scorecard(db)
    s.capture_results()
    assert _result_status(db, "G-BAD") == "INVALID"


def test_final_invalid_remains_final_no_rescore(db):
    """Re-running capture_results must NOT flip an INVALID game to OK —
    the final-status guard (scorecard.py ~931) keeps it INVALID forever."""
    s = Scorecard(db)
    s.capture_results()
    assert _result_status(db, "G-BAD") == "INVALID"
    # second run — still INVALID, never rescored
    s.capture_results()
    assert _result_status(db, "G-BAD") == "INVALID"
