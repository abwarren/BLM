"""M009 gap area 11 (CRITICAL) — LEGACY CONTAMINATION: frozen checkpoint
rows must not outlive a later quality flip.

record_checkpoint_market() checks eligibility (OK result, >= 15 snaps,
starts Q1, not quality-INVALID) at INSERT time, then freezes rows with
INSERT OR IGNORE + UNIQUE(source_game_id, checkpoint_pct).  The M007-M8
re-verification (capture_results run loop) re-checks OK results against
the CURRENT quality gate every run and writes game_quality INVALID for
games whose history is later contaminated.

DECISION (M009 follow-up): LOGICAL EXCLUSION.  Headline consumers
(market_vs_fair, /scorecard/events) filter through _CM_ELIGIBLE_SQL
(game_results final_result_status = 'OK' AND no game_quality INVALID);
the frozen historical rows REMAIN in checkpoint_market, intact and
auditable (line, timestamps, checkpoint, BLM fair, freshness
classification).  Game eligibility and market freshness are separate
dimensions — a row may stay LIVE/STALE while its game is later INVALID.

These tests pin the FINAL contract (they were xfail RED tests before
the fix; the xfail markers were removed when logical exclusion landed):

  - test_legacy_contam_reverify_invalid_keeps_feeding_headline
      full real mechanism: late-arriving score-regression snapshot ->
      capture_results re-verify -> game_quality INVALID -> rows RETAINED
      -> market_vs_fair excludes the game.
  - test_legacy_contam_direct_invalid_flag_rows_persist
      same contract via the direct quality-flag path (ANY route to
      INVALID leaves rows retained but excluded).
  - test_legacy_contam_fresh_invalid_still_excluded
      contamination present at record time is excluded — insert-time
      eligibility works.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.scorecard import Scorecard
from tests.test_m009_checkpoint_market import _LINES, _build


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def db(tmp_path, monkeypatch):
    dbfile = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(dbfile))
    return dbfile


def _rows(sc: Scorecard, gid: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{sc._db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM checkpoint_market WHERE source_game_id=? "
            "ORDER BY checkpoint_pct", (gid,))]
    finally:
        conn.close()


def _quality(db: Path, gid: str) -> dict | None:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT * FROM game_quality WHERE source_game_id=?", (gid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _result_status(db: Path, gid: str) -> str | None:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT final_result_status FROM game_results WHERE source_game_id=?",
            (gid,)).fetchone()
        return r["final_result_status"] if r else None
    finally:
        conn.close()


def _inject_regression_snapshot(db: Path, gid: str) -> None:
    """Simulate a LATE-ARRIVING contaminated snapshot: a score regression
    (home 40 -> 39) inserted between existing snapshots 5 and 6 — exactly
    the data glitch the M007-M8 re-verify was built to catch."""
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        gid_db = conn.execute(
            "SELECT id FROM games WHERE source_game_id=?", (gid,)).fetchone()["id"]
        ts5 = conn.execute(
            "SELECT captured_at FROM snapshots WHERE game_id=? "
            "ORDER BY captured_at ASC LIMIT 1 OFFSET 5",
            (gid_db,)).fetchone()["captured_at"]
        contam = (datetime.fromisoformat(ts5.replace("Z", "+00:00"))
                  + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn.execute(
            """INSERT INTO snapshots (
                game_id, source, source_game_id, classification, captured_at,
                home_team, away_team, home_score, away_score, period_label,
                quarter, clock, game_status, total_line, markets_json)
               VALUES (?, 'PokerBet', ?, 'BETUAL_NBA', ?, ?, ?, 39, 30,
                       '2nd Quarter', 2, '10:00', 'ended', ?, '{}')""",
            (gid_db, gid, contam, f"{gid} Home Virtual",
             f"{gid} Away Virtual", _LINES[5]))
        conn.commit()
    finally:
        conn.close()


def _flag_invalid_direct(db: Path, gid: str) -> None:
    """Mirror the exact end-state the M007-M8 re-verify produces (the
    capture_results INVALID branch): a game_quality INVALID row plus the
    game_results flip to INVALID — no snapshot surgery involved."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """INSERT OR IGNORE INTO game_quality
               (source_game_id, classification, status, reason, checked_at)
               VALUES (?, 'BETUAL_NBA', 'INVALID', ?, ?)""",
            (gid, "contaminated after recording", _utcnow_iso()))
        conn.execute(
            "UPDATE game_results SET final_result_status='INVALID' "
            "WHERE source_game_id=?", (gid,))
        conn.commit()
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════

def test_legacy_contam_reverify_invalid_keeps_feeding_headline(db):
    """A game recorded while clean must STOP feeding headline analytics
    after its history is LATER contaminated and the M007-M8 re-verify
    flips it INVALID — while the frozen historical rows REMAIN intact
    (logical exclusion, never destruction).  Full real mechanism (no
    shortcuts): late-arriving regression snapshot -> capture_results
    re-verify -> quality INVALID -> rows retained (auditable) -> the
    headline aggregation excludes the game."""
    _build(db, "G-CONTAM", lines=_LINES)
    s = Scorecard(db)
    s.capture_results()
    assert s.record_checkpoint_market()["recorded"] == 10
    assert len(_rows(s, "G-CONTAM")) == 10

    # Late-arriving contaminated snapshot — the NEXT run's re-verify must
    # catch it and flip the game INVALID (this part HOLDS today).
    _inject_regression_snapshot(db, "G-CONTAM")
    stats = s.capture_results()
    assert stats["invalid"] == 1
    q = _quality(db, "G-CONTAM")
    assert q is not None and q["status"] == "INVALID"
    assert _result_status(db, "G-CONTAM") == "INVALID"

    # A subsequent record run must not resurrect the game, and the
    # headline aggregation must NOT contain it — while the frozen rows
    # stay in the table, intact for audit (logical exclusion).
    s.record_checkpoint_market()
    assert len(_rows(s, "G-CONTAM")) == 10                # retained, auditable
    agg = s.market_vs_fair()
    assert "G-CONTAM" not in {g["source_game_id"] for g in agg["games"]}
    cp50 = next(c for c in agg["checkpoints"] if c["checkpoint_pct"] == 50)
    assert cp50["n"] == 0                                 # excluded from headline


def test_legacy_contam_direct_invalid_flag_rows_persist(db):
    """Same contract via the direct-flag path: ANY route to game_quality
    INVALID — re-verify, manual flag, a future stricter gate — must
    exclude the frozen rows from headline analytics while retaining
    them in checkpoint_market (quarantine, not purge)."""
    _build(db, "G-CONTAM2", lines=_LINES)
    s = Scorecard(db)
    s.capture_results()
    assert s.record_checkpoint_market()["recorded"] == 10
    _flag_invalid_direct(db, "G-CONTAM2")
    s.record_checkpoint_market()
    assert len(_rows(s, "G-CONTAM2")) == 10              # retained (quarantine)
    agg = s.market_vs_fair()
    assert "G-CONTAM2" not in {g["source_game_id"] for g in agg["games"]}
    cp50 = next(c for c in agg["checkpoints"] if c["checkpoint_pct"] == 50)
    assert cp50["n"] == 0                                # excluded from headline


def test_legacy_contam_fresh_invalid_still_excluded(db):
    """GREEN (the parts that DO hold): contamination present at record
    time is excluded — insert-time eligibility works, the re-verify flags
    it on the first run, and the headline never sees it.  The defect is
    strictly the LATER-contamination path (rows already frozen)."""
    _build(db, "G-CLEAN", lines=_LINES)
    _build(db, "G-BAD", lines=_LINES, dip=True)          # regression at snap 6
    s = Scorecard(db)
    stats = s.capture_results()
    assert stats["invalid"] == 1                         # G-BAD flagged 1st run
    q_bad = _quality(db, "G-BAD")
    assert q_bad is not None and q_bad["status"] == "INVALID"
    s.record_checkpoint_market()
    assert len(_rows(s, "G-CLEAN")) == 10
    assert _rows(s, "G-BAD") == []
    agg = s.market_vs_fair()
    assert {g["source_game_id"] for g in agg["games"]} == {"G-CLEAN"}
    cp50 = next(c for c in agg["checkpoints"] if c["checkpoint_pct"] == 50)
    assert cp50["n"] == 1                                # G-BAD never counted
