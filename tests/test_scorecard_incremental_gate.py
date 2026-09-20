"""Incremental scorecard pass (2026-09-20) — the settled-game gate.

PROBLEM: ``Scorecard.run()`` is a monolithic minutes-long pass (observed
6,446s ≈ 1h47m) that re-derives EVERY row of the whole history on every
loop iteration.  Its writes are idempotent and derived only from stored
inputs, so a game whose inputs have not moved since it was last written
cannot produce a different row — re-deriving it is pure cost.  Measured
on the live population before this change:

    stage 3 capture_results     18,615 ended games re-verified
                                (12,916 OK + 4,946 UNKNOWN settled)
    stage 5 market_history      11,570 settled rows re-upserted
                                (1,182,449 of 1,611,678 snapshots re-read)
    stage 6 checkpoint_market   11,567 settled games re-derived
    genuine new work            ~1,346 games (≈10% of the population)

CONTRACT UNDER TEST

1. A SETTLED game is skipped: its stored rows are left byte-identical
   (a deliberately poisoned value is NOT repaired — proof the skip is
   real, not an accidental rewrite).
2. Settled means "the stored derivation's inputs have not moved":
     stage 3  result_at == the game's own last snapshot (the exact input
              list the derivation reads) and quiet >= grace
     stage 5  no snapshot / market_observation / prediction row newer
              than the stored recorded_at, analytics_tz unchanged, quiet
     stage 6  the game has its pct100 row and no snapshot newer than that
              terminal checkpoint_timestamp, quiet
3. Any input newer than the stored write (a late snapshot, a late WS
   market observation, a result re-settled from a sibling chain) makes
   the game NOT settled — it is reprocessed exactly as before.
4. A game whose newest input is younger than the grace window is NOT
   settled (the collector stamps captured_at at insert time, but the WS /
   event-view paths can insert a moment after the read the gate performs).
5. The gate is revision-guarded: while the stored SCORECARD_LOGIC_REVISION
   differs from the code's constant, every pass is FULL — this preserves
   the documented rule that an OK recorded under an older, laxer quality
   gate must not survive a now-failing history.  Only a pass that
   completes end-to-end stores the revision.
6. The gate never over-skips: games with no derived rows (new, or row
   missing) are still processed.
7. On identical input, a GATED pass leaves market_history +
   checkpoint_market in the same state as a FULL pass (content columns,
   excluding the write timestamp of an idempotent re-upsert).
8. BLM_SCORECARD_INCREMENTAL=0 is a kill switch back to the full sweep.
9. The split is reported: run() carries the gate state, the revision and
   the per-stage skipped_settled counters.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4 import scorecard as sc_mod
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.scorecard import SCORECARD_LOGIC_REVISION, Scorecard
from blm_v4.storage import PokerBetStore

# 20 snapshots, one every 3 wall-clock minutes, Q1 10:00 -> Q4 00:00.
_HOME = [0, 8, 16, 24, 32, 40, 44, 47, 50, 53, 56, 59, 62, 65, 68, 71, 74, 76, 78, 80]
_AWAY = [0, 6, 12, 18, 24, 30, 33, 36, 39, 42, 45, 48, 51, 53, 55, 57, 59, 61, 62, 63]
_CLOCKS = ["10:00", "08:00", "06:00", "04:00", "02:00"]
_LINES = [170 + i for i in range(20)]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _build(db: Path, gid: str, *, status: str = "ended",
           start: datetime | None = None, nsnaps: int = 20,
           lines: list | None = None) -> int:
    """Insert a synthetic game + snapshots.  Returns the games.id."""
    st = PokerBetStore(db)
    base = start or (_now() - timedelta(hours=2))
    game = PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="comp-1", competition_slug="betual-tbsl",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA",
        sport="basketball", home_team=f"{gid} Home Virtual",
        away_team=f"{gid} Away Virtual", game_slug=f"{gid.lower()}-game",
        source_url=f"https://x/{gid}", status=status,
        first_seen_at=_iso(base),
        last_seen_at=_iso(base + timedelta(minutes=3 * (nsnaps - 1))),
    )
    gid_db = st.upsert_game(game)
    ln = lines if lines is not None else _LINES
    for i in range(nsnaps):
        t = base + timedelta(minutes=3 * i)
        q = i // 5 + 1
        clock = "00:00" if (i == nsnaps - 1 and q >= 4) else _CLOCKS[i % 5]
        obs = MarketObservation(
            source="PokerBet", source_game_id=gid, classification="BETUAL_NBA",
            captured_at=_iso(t),
            home_team=f"{gid} Home Virtual", away_team=f"{gid} Away Virtual",
            home_score=_HOME[i], away_score=_AWAY[i],
            period_label=f"{q}th Quarter", quarter=q, clock=clock,
            game_status=status, total_line=ln[i], spread=None,
            w1_odds=None, w2_odds=None,
            markets_json=json.dumps({"total": {"first_line": ln[i]}}),
        )
        st.insert_snapshot(gid_db, obs, force=True)
    return gid_db


def _add_snapshot(db: Path, gid_db: int, gid: str, *, at: datetime,
                  line: float, hs: int, as_: int, quarter: int = 4,
                  clock: str = "00:00") -> None:
    """Append ONE late snapshot at an explicit time (post-write input)."""
    st = PokerBetStore(db)
    st.insert_snapshot(gid_db, MarketObservation(
        source="PokerBet", source_game_id=gid, classification="BETUAL_NBA",
        captured_at=_iso(at),
        home_team=f"{gid} Home Virtual", away_team=f"{gid} Away Virtual",
        home_score=hs, away_score=as_,
        period_label=f"{quarter}th Quarter", quarter=quarter, clock=clock,
        game_status="ended", total_line=line, spread=None,
        w1_odds=None, w2_odds=None,
        markets_json=json.dumps({"total": {"first_line": line}}),
    ), force=True)


def _q(db: Path, sql: str, *args):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _one(db: Path, sql: str, *args):
    rows = _q(db, sql, *args)
    return rows[0][0] if rows else None


def _poison_mh(db: Path, gid: str) -> None:
    """Corrupt an idempotently-derived value; only a real re-derivation
    can repair it."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE market_history SET closing_total = -999.0 "
                     "WHERE source_game_id = ?", (gid,))
        conn.commit()
    finally:
        conn.close()


def _poison_cm(db: Path, gid: str) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE checkpoint_market SET blm_fair_value = -999.0 "
                     "WHERE source_game_id = ? AND checkpoint_pct = 50", (gid,))
        conn.commit()
    finally:
        conn.close()


def _set_iso(db: Path, sql: str, iso: str, *args) -> None:
    """Run an UPDATE whose FIRST placeholder is an ISO-Z timestamp — the
    format every writer in this codebase uses.  SQLite's datetime('now')
    emits a SPACE-separated stamp that sorts before every ISO-Z stamp,
    which would silently change what a string comparison in the gate means.
    """
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sql, (iso, *args))
        conn.commit()
    finally:
        conn.close()


def _checkpoint_db(db: Path) -> None:
    """Flush the WAL so a plain file copy is a consistent snapshot."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _copy_db(src: Path) -> Path:
    _checkpoint_db(src)
    dst = src.with_name(src.name + ".copy")
    shutil.copyfile(src, dst)
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.exists():
            shutil.copyfile(side, Path(str(dst) + suffix))
    return dst


def _content(db: Path, table: str) -> list[tuple]:
    """All columns except the write timestamp of an idempotent upsert."""
    cols = [r["name"] for r in _q(db, f"PRAGMA table_info({table})")]
    keep = [c for c in cols if c != "recorded_at"]
    sql = f'SELECT {", ".join(keep)} FROM {table} ORDER BY 1, 2' \
        if table == "checkpoint_market" else \
        f'SELECT {", ".join(keep)} FROM {table} ORDER BY 1'
    return [tuple(r) for r in _q(db, sql)]


@pytest.fixture
def game_db(tmp_path):
    """One settled game + one brand-new game, one full pass already run."""
    db = tmp_path / "blm_gate.db"
    gid_a = "30800001"
    _build(db, gid_a)
    _build(db, "30800002")
    Scorecard(db).run()
    return db, gid_a


def _one_game(tmp_path, gid: str = "30800001") -> tuple[Path, str]:
    """A single-game DB with one full pass behind it — so the per-stage
    skipped_settled counters can be asserted exactly."""
    db = tmp_path / "blm_gate.db"
    _build(db, gid)
    Scorecard(db).run()
    return db, gid


# ── 1. settled game is skipped, not repaired ─────────────────────────


def test_settled_market_row_is_not_rewritten(game_db):
    db, gid = game_db
    _poison_mh(db, gid)
    stats = Scorecard(db).run()
    assert _one(db, "SELECT closing_total FROM market_history "
                    "WHERE source_game_id = ?", gid) == -999.0, \
        "settled row was re-derived — the gate did not skip"
    assert stats["market"]["skipped_settled"] >= 1


def test_settled_checkpoint_rows_are_not_rewritten(game_db):
    db, gid = game_db
    _poison_cm(db, gid)
    stats = Scorecard(db).run()
    assert _one(db, "SELECT blm_fair_value FROM checkpoint_market "
                    "WHERE source_game_id = ? AND checkpoint_pct = 50", gid) == -999.0
    assert _one(db, "SELECT COUNT(*) FROM checkpoint_market "
                    "WHERE source_game_id = ? AND blm_fair_value = -999.0", gid) == 1
    assert stats["checkpoint_market"]["skipped_settled"] >= 1


def test_incomplete_checkpoint_set_stays_in_the_work_set(game_db):
    """The stage-6 criterion is the full pct DOMAIN, not staleness: a game
    missing a checkpoint row can still have one added (a pct that fell
    outside the ±5pp tolerance at the time it was written), so it is never
    skipped."""
    db, gid = game_db
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM checkpoint_market "
                 "WHERE source_game_id = ? AND checkpoint_pct = 30", (gid,))
    conn.commit()
    conn.close()
    stats = Scorecard(db).run()
    assert stats["checkpoint_market"]["skipped_settled"] == 1   # the other game
    # the sweep was given the chance to fill the hole back in — and did,
    # which is exactly the capability a staleness-keyed gate would have
    # thrown away for every complete-looking game
    assert _one(db, "SELECT COUNT(*) FROM checkpoint_market "
                    "WHERE source_game_id = ?", gid) == 10
    assert stats["checkpoint_market"]["checked"] >= 1


def test_complete_checkpoint_set_is_settled_even_with_new_snapshots(game_db):
    """Immutability makes staleness irrelevant for stage 6: UNIQUE(game,
    pct) over a fixed 10-value domain means a complete set can neither grow
    nor change, so a later snapshot cannot make the write anything but a
    no-op."""
    db, gid = game_db
    gid_db = _one(db, "SELECT id FROM games WHERE source_game_id = ?", gid)
    _add_snapshot(db, gid_db, gid, at=_now() - timedelta(minutes=30),
                  line=201.0, hs=86, as_=68)
    stats = Scorecard(db).run()
    assert stats["checkpoint_market"]["skipped_settled"] >= 1


# ── 2. new work is still done (no over-skip) ─────────────────────────


def test_new_game_is_still_processed(tmp_path):
    db = tmp_path / "blm_gate.db"
    _build(db, "30800001")
    Scorecard(db).run()                      # stores the revision
    _build(db, "30800003")                   # arrives after the first pass
    stats = Scorecard(db).run()
    assert _one(db, "SELECT COUNT(*) FROM market_history "
                    "WHERE source_game_id = '30800003'") == 1
    assert _one(db, "SELECT COUNT(*) FROM checkpoint_market "
                    "WHERE source_game_id = '30800003'") > 0
    assert stats["market"]["skipped_settled"] >= 1     # the old game still skipped


def test_missing_derived_row_is_not_treated_as_settled(tmp_path):
    db = tmp_path / "blm_gate.db"
    _build(db, "30800001")
    _build(db, "30800004")
    Scorecard(db).run()
    # delete the derived rows: the game must be re-derived, not skipped
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM market_history WHERE source_game_id='30800004'")
    conn.commit()
    conn.close()
    Scorecard(db).run()
    assert _one(db, "SELECT COUNT(*) FROM market_history "
                    "WHERE source_game_id = '30800004'") == 1


# ── 3. any newer input voids the settle ──────────────────────────────


def test_late_snapshot_forces_reprocess(tmp_path):
    """A snapshot landing AFTER the derived row was written means the
    derivation read a smaller input set than the one present now."""
    db, gid = _one_game(tmp_path)
    _poison_mh(db, gid)
    wrote_at = _one(db, "SELECT recorded_at FROM market_history "
                        "WHERE source_game_id = ?", gid)
    gid_db = _one(db, "SELECT id FROM games WHERE source_game_id = ?", gid)
    # a snapshot that arrives AFTER the derived row was written: the
    # derivation's input list has grown, so the row cannot be trusted
    late = datetime.strptime(wrote_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc) + timedelta(minutes=1)
    _add_snapshot(db, gid_db, gid, at=late, line=195.0, hs=84, as_=66)
    stats = Scorecard(db).run()
    assert _one(db, "SELECT closing_total FROM market_history "
                    "WHERE source_game_id = ?", gid) != -999.0, \
        "a late snapshot must force a re-derivation"
    assert stats["market"]["skipped_settled"] == 0


def test_late_market_observation_forces_reprocess(tmp_path):
    """OLV/CLV can come from the WS MatchTotal feed: a late observation is
    an input move just like a late snapshot."""
    db, gid = _one_game(tmp_path)
    _poison_mh(db, gid)
    wrote_at = _one(db, "SELECT recorded_at FROM market_history "
                        "WHERE source_game_id = ?", gid)
    gid_db = _one(db, "SELECT id FROM games WHERE source_game_id = ?", gid)
    late = datetime.strptime(wrote_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc) + timedelta(minutes=1)
    PokerBetStore(db).upsert_market_observation({
        "game_id": gid_db, "source_game_id": gid, "captured_at": _iso(late),
        "market_type": "MatchTotal", "market_name": "Match Total",
        "line_value": 199.5, "over_price": 1.9, "under_price": 1.9,
        "home_score": 0, "away_score": 0, "period_label": "", "clock": "",
        "raw_json": "{}",
    })
    stats = Scorecard(db).run()
    assert _one(db, "SELECT closing_total FROM market_history "
                    "WHERE source_game_id = ?", gid) != -999.0
    assert stats["market"]["skipped_settled"] == 0


def test_chained_verdict_is_reverified_not_skipped(tmp_path):
    """A verdict re-settled from a sibling `base#iN` chain carries a
    result_at that is NOT the base game's own last snapshot.  Stage 3 must
    never skip it: it would re-derive UNKNOWN from the base history alone,
    and that existing behaviour is deliberately preserved."""
    db, gid = _one_game(tmp_path)
    last_snap = datetime.strptime(
        _one(db, "SELECT MAX(captured_at) FROM snapshots "
                 "WHERE source_game_id = ?", gid),
        "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    # result_at now points past the base game's own history: a sibling's row
    _set_iso(db, "UPDATE game_results SET result_at = ? "
                 "WHERE source_game_id = ?",
             _iso(last_snap + timedelta(minutes=5)), gid)
    stats = Scorecard(db).run()
    assert stats["results"]["skipped_settled"] == 0, \
        "a chained verdict must be re-verified, never skipped"
    assert stats["results"]["checked"] == 1


def test_result_revised_after_the_write_forces_reprocess(tmp_path):
    """Defence in depth for stage 5, tested on the stage directly: the
    stage is independently callable (scripts and calibration call it), and
    game_results IS one of its inputs — the row stores final_home/away/
    total — so a result whose result_at moved past the market_history write
    voids the settle.

    Under a full run() this ordering is not reachable: capture_results runs
    first and re-derives result_at from the game's own last snapshot, so
    the revised stamp is normalised away before this stage reads it.
    """
    db, gid = _one_game(tmp_path)
    _poison_mh(db, gid)
    wrote_at = _one(db, "SELECT recorded_at FROM market_history "
                        "WHERE source_game_id = ?", gid)
    rec = datetime.strptime(wrote_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc)
    _set_iso(db, "UPDATE game_results SET result_at = ? "
                 "WHERE source_game_id = ?",
             _iso(rec + timedelta(minutes=1)), gid)
    stats = Scorecard(db).record_market_history()
    assert _one(db, "SELECT closing_total FROM market_history "
                    "WHERE source_game_id = ?", gid) != -999.0
    assert stats["skipped_settled"] == 0


def test_fresh_game_is_not_settled(tmp_path, monkeypatch):
    """Newest input younger than the grace window -> not settled: the WS /
    event-view writer can insert a row just after the gate's read."""
    monkeypatch.setenv("BLM_SCORECARD_SETTLE_GRACE_S", "600")
    db = tmp_path / "blm_gate.db"
    gid = "30800005"
    # game ended ~2 minutes ago: inputs are inside a 600s grace window
    _build(db, gid, start=_now() - timedelta(minutes=59))
    Scorecard(db).run()
    _poison_mh(db, gid)
    stats = Scorecard(db).run()
    assert _one(db, "SELECT closing_total FROM market_history "
                    "WHERE source_game_id = ?", gid) != -999.0
    assert stats["market"]["skipped_settled"] == 0


def test_unknown_result_with_no_new_snapshot_is_settled(tmp_path):
    """UNKNOWN rows are re-verified each run *because* the history may have
    grown — with no growth the re-verification is provably a no-op."""
    db = tmp_path / "blm_gate.db"
    _build(db, "30800006", nsnaps=4)        # too few snapshots -> UNKNOWN
    Scorecard(db).run()
    assert _one(db, "SELECT final_result_status FROM game_results "
                    "WHERE source_game_id='30800006'") == "UNKNOWN"
    stats = Scorecard(db).run()
    assert stats["results"]["skipped_settled"] == 1
    assert stats["results"]["checked"] == 0


# ── 4. revision guard ────────────────────────────────────────────────


def test_revision_mismatch_forces_a_full_pass(game_db):
    db, gid = game_db
    _poison_mh(db, gid)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO scorecard_state (key, value) "
                 "VALUES ('logic_revision', 'stale') "
                 "ON CONFLICT(key) DO UPDATE SET value='stale'")
    conn.commit()
    conn.close()
    stats = Scorecard(db).run()
    assert _one(db, "SELECT closing_total FROM market_history "
                    "WHERE source_game_id = ?", gid) != -999.0, \
        "a stale revision must run the FULL sweep"
    assert stats["incremental"] is False
    # and the pass re-stamps the revision so the next pass may gate again
    assert _one(db, "SELECT value FROM scorecard_state "
                    "WHERE key='logic_revision'") == SCORECARD_LOGIC_REVISION


def test_first_ever_pass_is_full_and_stores_revision(tmp_path):
    db = tmp_path / "blm_gate.db"
    _build(db, "30800001")
    stats = Scorecard(db).run()
    assert stats["incremental"] is False
    assert _one(db, "SELECT value FROM scorecard_state "
                    "WHERE key='logic_revision'") == SCORECARD_LOGIC_REVISION


def test_failed_pass_does_not_stamp_the_revision(tmp_path, monkeypatch):
    db = tmp_path / "blm_gate.db"
    _build(db, "30800001")
    def boom(self):
        raise RuntimeError("stage exploded")
    monkeypatch.setattr(Scorecard, "record_checkpoint_market", boom)
    with pytest.raises(RuntimeError):
        Scorecard(db).run()
    assert _one(db, "SELECT COUNT(*) FROM scorecard_state "
                    "WHERE key='logic_revision'") == 0


# ── 5. kill switch ───────────────────────────────────────────────────


def test_kill_switch_disables_the_gate(game_db, monkeypatch):
    db, gid = game_db
    monkeypatch.setenv("BLM_SCORECARD_INCREMENTAL", "0")
    _poison_mh(db, gid)
    stats = Scorecard(db).run()
    assert _one(db, "SELECT closing_total FROM market_history "
                    "WHERE source_game_id = ?", gid) != -999.0
    assert stats["incremental"] is False
    assert stats["market"]["skipped_settled"] == 0


def test_forced_full_pass_argument(game_db):
    db, gid = game_db
    _poison_mh(db, gid)
    stats = Scorecard(db).run(incremental=False)
    assert _one(db, "SELECT closing_total FROM market_history "
                    "WHERE source_game_id = ?", gid) != -999.0
    assert stats["incremental"] is False


# ── 6. gated pass == full pass on identical input ────────────────────


def test_gated_pass_matches_full_pass(tmp_path):
    """The headline proof: the gate is a pure cost optimisation.  On the
    SAME stored state — one settled game, one game whose snapshot history
    grew, one chained-result game, one never-processed game — a gated pass
    and a full pass leave identical rows (every column except the write
    timestamp of the idempotent market_history upsert)."""
    db = tmp_path / "blm_eq.db"
    for gid in ("30800001", "30800002", "30800003", "30800004"):
        _build(db, gid)
    Scorecard(db).run()                       # full: stores the revision

    # grow game 2's history and chain-settle game 4 AFTER that pass
    g2 = _one(db, "SELECT id FROM games WHERE source_game_id='30800002'")
    _add_snapshot(db, g2, "30800002", at=_now() - timedelta(minutes=30),
                  line=205.0, hs=88, as_=70)
    g4_last = datetime.strptime(
        _one(db, "SELECT MAX(captured_at) FROM snapshots "
                 "WHERE source_game_id='30800004'"),
        "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    _set_iso(db, "UPDATE game_results SET result_at = ? "
                 "WHERE source_game_id='30800004'",
             _iso(g4_last + timedelta(minutes=5)))
    _build(db, "30800009")                    # brand-new game

    gated = _copy_db(db)
    full = _copy_db(db)

    s_gated = Scorecard(gated).run()                   # gate active
    s_full = Scorecard(full).run(incremental=False)    # forced full sweep

    assert s_gated["incremental"] is True
    assert s_gated["market"]["skipped_settled"] >= 1, \
        "the gated pass skipped nothing — the comparison is vacuous"
    assert s_full["market"]["skipped_settled"] == 0

    for table in ("market_history", "checkpoint_market"):
        assert _content(gated, table) == _content(full, table), \
            f"{table} diverged between the gated and full passes"


def test_gated_pass_reports_the_split(game_db):
    db, gid = game_db
    stats = Scorecard(db).run()
    assert stats["incremental"] is True
    assert stats["revision"] == SCORECARD_LOGIC_REVISION
    for stage in ("results", "market", "checkpoint_market"):
        assert "skipped_settled" in stats[stage]
