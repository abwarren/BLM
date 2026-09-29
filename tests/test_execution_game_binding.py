"""BLM EXECUTION — GAME-ID BINDING + PLACEMENT-BOUNDARY ACTIVITY GATES
(directive 2026-09-23: "verify game ID / teams / market / line / odds /
market still active" + "record execution: game ID").

Three gaps closed against the shipped phase-① engine:

  1. GAME ID VERIFICATION — the Selection carries the canonical BLM
     game_id; the betslip verifier rejects a slip entry that names the
     right teams but exposes a different game id (virtuals reuse team
     names across consecutive fixtures — name similarity is not
     identity).
  2. MARKET STILL ACTIVE — immediately before placement the executor
     re-resolves every leg and refuses (MARKET_NOT_ACTIVE, terminal)
     when any market is suspended or can no longer be resolved.
  3. EXECUTION RECORD — the ledger carries the game id on leg
     attempts, jobs and order submissions, so every recorded execution
     joins back to the game's history.

Everything here runs in DRY_RUN: nothing is placed, no real-money path
is exercised.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_browser_adapter import (  # noqa: E402
    FakeBrowserAdapter,
)

from blm_v4.execution.betslip_verifier import (  # noqa: E402
    BETSLIP_NOT_UPDATED,
    VERIFIED,
    verify_full_betslip,
    verify_leg_in_betslip,
)
from blm_v4.execution.config import ExecutionConfig  # noqa: E402
from blm_v4.execution.selection_model import (  # noqa: E402
    MODE_DRY_RUN,
    ParlayJob,
    Selection,
    STATE_DRY_RUN_COMPLETE,
    STATE_NON_RECOVERABLE,
)
from blm_v4.execution.store import ExecutionStore  # noqa: E402
from blm_v4.execution.total_executor import TotalExecutor  # noqa: E402

GAME_A = "Team A vs Team B"
GID_A = "30840226"


def make_cfg(tmp_path, **over) -> ExecutionConfig:
    defaults = dict(
        dry_run=True, max_selection_retries=3, retry_delay_ms=0,
        settle_ms=0, slip_wait_ms=0, verify_attempts=3,
        leg_timeout_s=45.0, job_timeout_s=600.0,
        max_parlays_per_run=100, db_path=str(tmp_path / "blm_execution.db"))
    defaults.update(over)
    return ExecutionConfig(**defaults)


def under(event: str, **kw) -> Selection:
    return Selection(event=event, market="TOTAL", position="UNDER", **kw)


# ══════════════════════════════════════════════════════════════════════
# 1. GAME ID — selection model + verifier identity
# ══════════════════════════════════════════════════════════════════════

def test_game_id_is_not_part_of_identity_but_round_trips():
    """The matrix contract stays intact: same event/market/position is
    the SAME selection whether or not a game id is attached; the id
    still survives to_dict/from_dict for verification and the ledger."""
    a = under(GAME_A, game_id=GID_A)
    b = under(GAME_A)
    assert a == b and hash(a) == hash(b)
    assert Selection.from_dict(a.to_dict()).game_id == GID_A
    assert Selection.from_dict(b.to_dict()).game_id is None


def test_verifier_rejects_right_teams_wrong_game_id():
    """A slip entry naming the right teams but carrying a DIFFERENT game
    id is NOT the requested leg — the classic same-teams-next-fixture
    contamination is rejected, never verified."""
    adapter = FakeBrowserAdapter()
    adapter.set_market(GAME_A, line=164.5, over=1.95, under=1.90)
    sel = under(GAME_A, game_id=GID_A)
    adapter.click_selection(adapter.find_position(
        GAME_A, "TOTAL", "UNDER"))
    # the slip entry carries a different game's id:
    adapter.slip[0]["game_id"] = "99999999"
    check = verify_leg_in_betslip(adapter, sel, 164.5, 1.90)
    assert check.outcome == BETSLIP_NOT_UPDATED
    # ...and the full-slip gate reports the leg as missing (never ok)
    full = verify_full_betslip(adapter, [sel], [164.5], [1.90])
    assert full["ok"] is False and full["missing"]


def test_verifier_accepts_matching_game_id():
    adapter = FakeBrowserAdapter()
    adapter.set_market(GAME_A, line=164.5, over=1.95, under=1.90)
    sel = under(GAME_A, game_id=GID_A)
    adapter.click_selection(adapter.find_position(
        GAME_A, "TOTAL", "UNDER"))
    adapter.slip[0]["game_id"] = GID_A
    check = verify_leg_in_betslip(adapter, sel, 164.5, 1.90)
    assert check.outcome == VERIFIED


def test_verifier_degrades_to_name_identity_without_ids():
    """No game id on either side → the older, id-less contract still
    verifies (the id-less bookmaker rendering must not break)."""
    adapter = FakeBrowserAdapter()
    adapter.set_market(GAME_A, line=164.5, over=1.95, under=1.90)
    sel = under(GAME_A)
    adapter.click_selection(adapter.find_position(
        GAME_A, "TOTAL", "UNDER"))
    assert verify_leg_in_betslip(
        adapter, sel, 164.5, 1.90).outcome == VERIFIED
    # id on the selection only (bookmaker exposes none) → same
    sel2 = under(GAME_A, game_id=GID_A)
    assert verify_leg_in_betslip(
        adapter, sel2, 164.5, 1.90).outcome == VERIFIED


# ══════════════════════════════════════════════════════════════════════
# 2. MARKET STILL ACTIVE — the placement-boundary re-check
# ══════════════════════════════════════════════════════════════════════

def test_suspension_after_confirmation_blocks_placement(tmp_path):
    """The leg confirmed fine, the slip is correct — but the bookmaker
    suspended the market BEFORE placement.  The job stops with
    MARKET_NOT_ACTIVE (terminal, nothing placed, honest record)."""
    adapter = FakeBrowserAdapter()
    adapter.set_market(GAME_A, line=164.5, over=1.95, under=1.90)
    sel = under(GAME_A, game_id=GID_A)
    executor = TotalExecutor(adapter, make_cfg(tmp_path))
    job = ParlayJob(legs=[sel], fold_size=1, stake_amount=10.0)

    def suspend_at_boundary():
        # fires on the executor's placement-boundary re-resolution
        adapter.suspended_positions.add(f"{GAME_A}|UNDER")

    # the leg resolves+clicks+verifies normally; the re-check inside the
    # pre-placement gate sees the suspension
    orig_find = adapter.find_position

    def find_then_suspend(event, market, position):
        # the bookmaker suspends the market at the placement boundary:
        # flag it BEFORE the observation is built so the re-check sees
        # it (the gate re-resolves after the leg confirmed)
        if adapter._click_no >= 1:
            suspend_at_boundary()
        return orig_find(event, market, position)

    adapter.find_position = find_then_suspend
    result = executor.run_job("exec-suspend", job, MODE_DRY_RUN)
    assert result.status == STATE_NON_RECOVERABLE
    assert "MARKET_NOT_ACTIVE" in (result.last_error or "")
    assert adapter.placements == []          # nothing placed


def test_active_market_passes_the_boundary_check(tmp_path):
    adapter = FakeBrowserAdapter()
    adapter.set_market(GAME_A, line=164.5, over=1.95, under=1.90)
    sel = under(GAME_A, game_id=GID_A)
    store = ExecutionStore(str(tmp_path / "blm_execution.db"))
    executor = TotalExecutor(adapter, make_cfg(tmp_path), store=store)
    job = ParlayJob(legs=[sel], fold_size=1, stake_amount=10.0)
    result = executor.run_job("exec-ok", job, MODE_DRY_RUN)
    assert result.status == STATE_DRY_RUN_COMPLETE
    assert adapter.placements == []          # DRY_RUN never places


# ══════════════════════════════════════════════════════════════════════
# 3. EXECUTION RECORD — the ledger carries the game id
# ══════════════════════════════════════════════════════════════════════

def test_ledger_records_game_id_end_to_end(tmp_path):
    """The directive's record fields: timestamp, game ID, selection,
    line, odds, stake — with the game id present on leg attempts, the
    job row and (idempotently) the order-submission ledger."""
    import sqlite3
    adapter = FakeBrowserAdapter()
    adapter.set_market(GAME_A, line=164.5, over=1.95, under=1.90)
    sel = under(GAME_A, game_id=GID_A, snapshot_line=164.5,
                snapshot_price=1.90)
    store = ExecutionStore(str(tmp_path / "blm_execution.db"))
    executor = TotalExecutor(adapter, make_cfg(tmp_path), store=store)
    job = ParlayJob(legs=[sel], fold_size=1, stake_amount=10.0)
    result = executor.run_job("exec-ledger", job, MODE_DRY_RUN)
    assert result.status == STATE_DRY_RUN_COMPLETE

    attempts = store.jobs_for_run("exec-ledger")
    assert attempts and json.loads(attempts[0]["game_ids_json"]) == [GID_A]
    conn = store._conn()
    try:
        rows = conn.execute(
            "SELECT game_id, discovered_line, discovered_price, outcome"
            " FROM leg_attempts WHERE parlay_id=?",
            (job.parlay_id,)).fetchall()
        assert any(
            r["game_id"] == GID_A and r["discovered_line"] == 164.5
            and r["discovered_price"] == 1.90
            and r["outcome"] == "LEG_CONFIRMED" for r in rows)
    finally:
        conn.close()


def test_pre_game_id_ledger_file_migrates_in_place(tmp_path):
    """An existing ledger created before the game-id columns opens,
    migrates (idempotent ALTERs), and keeps working — historical data
    untouched, new writes carry the game id."""
    db = str(tmp_path / "old_ledger.db")
    import sqlite3
    conn = sqlite3.connect(db)
    conn.executescript(
        """CREATE TABLE execution_jobs (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               execution_id TEXT NOT NULL, parlay_id TEXT NOT NULL,
               combination_id TEXT NOT NULL, fold_size INTEGER NOT NULL,
               mode TEXT NOT NULL, stake_amount REAL, status TEXT NOT NULL,
               current_leg INTEGER DEFAULT 0, retry_count INTEGER DEFAULT 0,
               legs_json TEXT NOT NULL, last_error TEXT,
               started_at_utc TEXT, completed_at_utc TEXT,
               created_at TEXT NOT NULL DEFAULT 'now',
               UNIQUE(execution_id, parlay_id));
           CREATE TABLE leg_attempts (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               execution_id TEXT NOT NULL, parlay_id TEXT NOT NULL,
               leg_index INTEGER NOT NULL, attempt_no INTEGER NOT NULL,
               event TEXT NOT NULL, market TEXT NOT NULL,
               position TEXT NOT NULL,
               discovered_line REAL, discovered_price REAL,
               clicked_line REAL, clicked_price REAL,
               outcome TEXT NOT NULL, verified_line REAL,
               verified_price REAL, error_code TEXT, at_utc TEXT);
        """)
    conn.execute(
        "INSERT INTO execution_jobs (execution_id, parlay_id,"
        " combination_id, fold_size, mode, status, legs_json)"
        " VALUES ('old-run','P-old','c',1,'DRY_RUN','COMPLETE','[]')")
    conn.commit()
    conn.close()

    store = ExecutionStore(db)               # migration runs here
    adapter = FakeBrowserAdapter()
    adapter.set_market(GAME_A, line=164.5, over=1.95, under=1.90)
    sel = under(GAME_A, game_id=GID_A)
    executor = TotalExecutor(adapter, make_cfg(tmp_path, db_path=db),
                             store=store)
    job = ParlayJob(legs=[sel], fold_size=1, stake_amount=10.0)
    result = executor.run_job("exec-migrated", job, MODE_DRY_RUN)
    assert result.status == STATE_DRY_RUN_COMPLETE

    conn = sqlite3.connect(db)
    try:
        old = conn.execute(
            "SELECT status FROM execution_jobs WHERE parlay_id='P-old'"
        ).fetchone()
        new = conn.execute(
            "SELECT game_ids_json FROM execution_jobs WHERE parlay_id=?",
            (job.parlay_id,)).fetchone()
        gid = conn.execute(
            "SELECT game_id FROM leg_attempts WHERE execution_id="
            "'exec-migrated' AND outcome='LEG_CONFIRMED'").fetchone()
        assert old[0] == "COMPLETE"          # history preserved
        assert json.loads(new[0]) == [GID_A]  # new rows carry the id
        assert gid[0] == GID_A
    finally:
        conn.close()


def test_job_without_game_binding_still_executes(tmp_path):
    """Backward compatibility: legs created without a game id (the
    existing UI contract) run unchanged; the ledger stores null."""
    adapter = FakeBrowserAdapter()
    adapter.set_market(GAME_A, line=164.5, over=1.95, under=1.90)
    sel = under(GAME_A)
    store = ExecutionStore(str(tmp_path / "blm_execution.db"))
    executor = TotalExecutor(adapter, make_cfg(tmp_path), store=store)
    job = ParlayJob(legs=[sel], fold_size=1, stake_amount=10.0)
    result = executor.run_job("exec-nogid", job, MODE_DRY_RUN)
    assert result.status == STATE_DRY_RUN_COMPLETE
    conn = store._conn()
    try:
        gid = conn.execute(
            "SELECT game_id FROM leg_attempts WHERE parlay_id=?"
            " AND outcome='LEG_CONFIRMED'", (job.parlay_id,)).fetchone()
        assert gid[0] is None
    finally:
        conn.close()
