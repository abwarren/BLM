"""The bet LADDER: 1 unit, then 1.8x per consecutive win (operator 2026-10-30).

One test per numbered requirement of the directive, plus the two failure modes
that matter with real money: an unsettleable bet must not move the ladder, and
the ladder must never widen an existing risk limit.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from blm_v4.betting import ladder  # noqa: E402
from blm_v4.betting import settlement  # noqa: E402


@pytest.fixture(autouse=True)
def _factor_env(monkeypatch):
    """Pin the deployment's constants so an operator env cannot bend the test."""
    monkeypatch.delenv("BET_LADDER_FACTOR", raising=False)
    monkeypatch.delenv("BET_LADDER_MODE", raising=False)
    monkeypatch.delenv("BET_LADDER_STEP", raising=False)


# 1 ── the ladder is OFF unless selected
def test_ladder_is_off_by_default():
    assert ladder.depth("0") == 0
    assert ladder.depth(None) == 0
    assert ladder.enabled("0") is False
    assert ladder.enabled("4") is True


# 2 ── only the permitted depths are selectable, anything else reads OFF
def test_depth_is_validated():
    for good in (3, 4, 5, 6, 7):
        assert ladder.depth(good) == good
        assert ladder.depth(str(good)) == good
    for bad in (1, 2, 8, 99, -3, "x", None, True):
        assert ladder.depth(bad) == 0


# 3 ── rung 1 stakes exactly the configured unit
def test_rung_one_stakes_the_configured_unit():
    assert ladder.stake_units(1, 1.0) == 1.0
    assert ladder.stake_units(1, 0.5) == 0.5


# 4 ── each consecutive win multiplies the stake by 1.8
def test_each_win_multiplies_by_one_point_eight():
    assert [ladder.stake_units(r, 1.0) for r in range(1, 8)] == [
        1.0, 1.8, 3.24, 5.832, 10.4976, 18.8957, 34.0122]


# 5 ── a loss terminates the progression
def test_a_loss_resets_to_rung_one():
    for rung in (1, 3, 7):
        assert ladder.advance(rung, "LOST", 7) == 1


# 6 ── at the selected depth the win banks and the next bet is rung 1
def test_banking_at_depth_returns_to_rung_one():
    assert ladder.advance(1, "WON", 4) == 2
    assert ladder.advance(3, "WON", 4) == 4
    assert ladder.advance(4, "WON", 4) == 1        # banked
    assert ladder.advance(7, "WON", 3) == 1


# 7 ── a push moves no money, so it moves no rung
def test_push_holds_the_rung():
    assert ladder.advance(3, "PUSH", 4) == 3


# 8 ── an unprovable result is treated as a loss, never as a win
def test_unknown_result_fails_closed():
    assert ladder.advance(5, "", 7) == 1
    assert ladder.advance(5, "VOID", 7) == 1


# 9 ── the rung is DERIVED from history, so a restart resumes correctly
def test_rung_is_derived_from_history():
    assert ladder.rung_from_history([], 4) == 1
    assert ladder.rung_from_history(["WON"], 4) == 2
    assert ladder.rung_from_history(["WON", "WON"], 4) == 3
    assert ladder.rung_from_history(["WON", "WON", "LOST"], 4) == 1
    assert ladder.rung_from_history(["WON", "WON", "WON", "WON"], 4) == 1  # banked
    assert ladder.rung_from_history(["WON", "PUSH", "WON"], 4) == 3


# 10 ── settlement: UNDER wins below the line, loses above, pushes level
def test_settlement_outcome_under():
    assert ladder.settle_outcome("TOTAL", "TOTAL UNDER", 180.5, 170) == "WON"
    assert ladder.settle_outcome("TOTAL", "TOTAL UNDER", 180.5, 190) == "LOST"
    assert ladder.settle_outcome("TOTAL", "TOTAL UNDER", 180.5, 180.5) == "PUSH"


# 11 ── no final total is not a result: the caller must skip, not guess
def test_unsettleable_bet_has_no_outcome():
    assert ladder.settle_outcome("TOTAL", "TOTAL UNDER", 180.5, None) is None
    assert ladder.settle_outcome("TOTAL", "TOTAL UNDER", None, 190) is None


# 12 ── the ladder never widens a limit: the stake is bounded by max_depth
def test_stake_is_clamped_to_max_depth():
    assert ladder.stake_units(99, 1.0) == ladder.stake_units(7, 1.0)
    assert ladder.stake_units(0, 1.0) == 1.0


# 13 ── a corrupt base stake yields NaN (the executor refuses), never 0 or a guess
def test_invalid_base_returns_nan():
    assert ladder.stake_units(1, 0) != ladder.stake_units(1, 0)      # NaN
    assert ladder.stake_units(1, -1) != ladder.stake_units(1, -1)


# 14 ── settlement feed: only OK renders count, and only placed bets
def test_settled_outcomes_skips_unsettled_and_failed(tmp_path):
    betting = tmp_path / "bet.db"
    prod = tmp_path / "prod.db"
    b = sqlite3.connect(betting)
    b.execute("""CREATE TABLE bet_executions (
        id INTEGER PRIMARY KEY, game_id TEXT, market TEXT, selection TEXT,
        triggered_line REAL, status TEXT, submitted_at_utc TEXT,
        requested_at_utc TEXT)""")
    rows = [
        (1, "g1", "TOTAL", "TOTAL UNDER", 180.5, "SUBMITTED", "2026-10-01T00:00:00Z", None),
        (2, "g2", "TOTAL", "TOTAL UNDER", 180.5, "FAILED",    "2026-10-01T01:00:00Z", None),
        (3, "g3", "TOTAL", "TOTAL UNDER", 180.5, "SUBMITTED", "2026-10-01T02:00:00Z", None),
        (4, "g4", "TOTAL", "TOTAL UNDER", 180.5, "SUBMITTED", "2026-10-01T03:00:00Z", None),
    ]
    b.executemany("INSERT INTO bet_executions VALUES (?,?,?,?,?,?,?,?)", rows)
    b.commit()
    b.close()
    p = sqlite3.connect(prod)
    p.execute("""CREATE TABLE game_results (
        source_game_id TEXT, final_total INTEGER, final_result_status TEXT)""")
    p.executemany("INSERT INTO game_results VALUES (?,?,?)", [
        ("g1", 170, "OK"),            # WON
        ("g3", None, "NEEDS_RECONCILIATION"),   # not settled -> skipped
        ("g4", 195, "OK"),            # LOST
    ])
    p.commit()
    p.close()

    got = settlement.settled_outcomes(str(betting), str(prod))
    assert got == ["WON", "LOST"]      # FAILED and unsettled rows excluded


# 15 ── the executor's rung for a live ledger matches the replayed history
def test_current_rung_reads_the_ledger(tmp_path):
    betting = tmp_path / "bet.db"
    prod = tmp_path / "prod.db"
    b = sqlite3.connect(betting)
    b.execute("""CREATE TABLE bet_executions (
        id INTEGER PRIMARY KEY, game_id TEXT, market TEXT, selection TEXT,
        triggered_line REAL, status TEXT, submitted_at_utc TEXT,
        requested_at_utc TEXT)""")
    b.executemany("INSERT INTO bet_executions VALUES (?,?,?,?,?,?,?,?)", [
        (1, "g1", "TOTAL", "TOTAL UNDER", 180.5, "SUBMITTED", "2026-10-01T00:00:00Z", None),
        (2, "g2", "TOTAL", "TOTAL UNDER", 180.5, "SUBMITTED", "2026-10-01T01:00:00Z", None),
    ])
    b.commit()
    b.close()
    p = sqlite3.connect(prod)
    p.execute("""CREATE TABLE game_results (
        source_game_id TEXT, final_total INTEGER, final_result_status TEXT)""")
    p.executemany("INSERT INTO game_results VALUES (?,?,?)",
                  [("g1", 170, "OK"), ("g2", 165, "OK")])
    p.commit()
    p.close()

    # two wins at depth 4 -> the next bet is rung 3 (3.24u at a 1u base)
    assert settlement.current_rung(str(betting), "4", str(prod)) == 3
    # depth 0 is off: always rung 1 regardless of history
    assert settlement.current_rung(str(betting), "0", str(prod)) == 1


# ── executor integration: the stake the engine actually carries ───────────
sys.path.insert(0, str(ROOT / "tests"))
from test_betting_execution import make_cfg, make_store, qualifying_game  # noqa: E402


def _evaluate(tmp_path, monkeypatch, ladder_wins, rung, unit=10.0, **cfg_over):
    from blm_v4.betting.executor import evaluate
    store = make_store(tmp_path)
    store.set_config("ladder_wins", str(ladder_wins))
    cfg = make_cfg(tmp_path, **cfg_over)
    monkeypatch.setattr(settlement, "current_rung", lambda *a, **k: rung)
    game = qualifying_game()
    res = evaluate(game, cfg=cfg, store=store, enabled=True, unit_price=unit,
                   stats={"verifiable": True, "bets": 0, "amount": 0.0},
                   claim=True, game_enabled=True, balance=10_000.0)
    return res


# 16 ── OFF stakes the plain unit (today's contract, unchanged)
def test_executor_off_stakes_the_plain_unit(tmp_path, monkeypatch):
    res = _evaluate(tmp_path, monkeypatch, 0, 1)
    assert res["decision"] in ("WOULD_BET", "EXECUTE")
    assert res["candidate"]["stake_amount"] == 10.0


# 17 ── rung 3 carries 3.24 units = 1.8^2
def test_executor_multiplies_the_stake_by_the_rung(tmp_path, monkeypatch):
    res = _evaluate(tmp_path, monkeypatch, 4, 3)
    assert res["candidate"]["stake_units"] == pytest.approx(3.24, abs=1e-6)
    assert res["candidate"]["stake_amount"] == pytest.approx(32.40, abs=0.01)


# 18 ── a rung whose stake breaches the per-bet cap is REFUSED, never trimmed
def test_executor_refuses_a_rung_over_the_per_bet_cap(tmp_path, monkeypatch):
    res = _evaluate(tmp_path, monkeypatch, 7, 7, unit=10.0, max_stake_per_bet=100.0)
    assert res["decision"] == "NO_BET"
    assert res["reason"] == "max_stake_exceeded"


# 19 ── a corrupt ladder config reads OFF, so the one-unit contract returns
def test_executor_treats_a_corrupt_depth_as_off(tmp_path, monkeypatch):
    res = _evaluate(tmp_path, monkeypatch, 2, 3)      # 2 is not a permitted depth
    assert res["candidate"]["stake_amount"] == 10.0
