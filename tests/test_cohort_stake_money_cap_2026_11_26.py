"""COHORT STAKE under the money cap (operator ruling 2026-11-26).

"1 unit" is the VARIABLE amount (unit_price), not a ceiling.  A cohort overlay
may stake 2-3 units; the binding bound is cfg.max_stake_per_bet (R2 000), the
daily limits and the balance.  One test per property, no live DB, no network.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_betting_execution import make_cfg, make_store, qualifying_game  # noqa: E402

from blm_v4.betting.executor import evaluate  # noqa: E402


def _run(tmp_path, cfg):
    store = make_store(tmp_path)
    store.set_config("ladder_wins", "0")
    return evaluate(qualifying_game(), cfg=cfg, store=store, enabled=True,
                    unit_price=10.0,
                    stats={"verifiable": True, "bets": 0, "amount": 0.0},
                    claim=True, game_enabled=True, balance=10_000.0)


def test_two_units_are_staked_under_the_money_cap(tmp_path):
    cfg = make_cfg(tmp_path, stake_units=2.0, max_stake_per_bet=2000.0)
    res = _run(tmp_path, cfg)
    assert res["decision"] in ("WOULD_BET", "EXECUTE")
    assert res["candidate"]["stake_units"] == 2.0
    assert res["candidate"]["stake_amount"] == 20.0


def test_three_units_over_the_money_cap_is_refused(tmp_path):
    cfg = make_cfg(tmp_path, stake_units=3.0, max_stake_per_bet=25.0)
    res = _run(tmp_path, cfg)
    assert (res["decision"], res["reason"]) == ("NO_BET", "max_stake_exceeded")


def test_cohort_units_come_from_the_copied_config_not_the_process_one(tmp_path):
    cfg = make_cfg(tmp_path, stake_units=1.0, max_stake_per_bet=2000.0)
    res = _run(tmp_path, replace(cfg, stake_units=3.0))
    assert res["decision"] in ("WOULD_BET", "EXECUTE")
    assert res["candidate"]["stake_units"] == 3.0
    assert res["candidate"]["stake_amount"] == 30.0
    assert cfg.stake_units == 1.0          # the original object is untouched
