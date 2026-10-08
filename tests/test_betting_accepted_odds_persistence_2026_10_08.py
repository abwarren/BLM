"""Accepted-odds persistence (operator directive 2026-10-08).

The bookmaker can re-price a selection after the click ("accept odds changes
and resubmit").  ``bet_executions.price`` must record the odds the book
ACTUALLY took — not the stale pre-click quote — otherwise the ledger diverges
from the settled slip (observed live: BLM recorded 1.80, PokerBet settled a
win at 1.90 for the same 20 R single).

Pins the propagation chain:
  provider.submit -> observed_odds / odds_accepted
  executor        -> accepted_odds derivation
  store.update_status(price=...) -> bet_executions.price
"""
from __future__ import annotations

import sqlite3

from blm_v4.betting.config import BettingConfig
from blm_v4.betting.executor import execute
from blm_v4.betting.store import BettingStore


def make_cfg(tmp_path) -> BettingConfig:
    return BettingConfig(
        dry_run=True,
        max_stake_per_bet=100.0,
        max_bets_per_day=5,
        max_daily_exposure=200.0,
        stake_units=1.0,
        min_unit_price=1.0,
        max_unit_price=1000.0,
        alert_max_age_s=90.0,
        db_path=str(tmp_path / "blm_betting.db"),
    )


class _StubProvider:
    """Provider stub with the accepted-odds fields the bridge now returns."""

    requires_account_identity = False

    def __init__(self, result: dict):
        self._result = result

    def submit(self, **kwargs) -> dict:
        return dict(self._result)


def _claim(store: BettingStore):
    store.set_config("auto_betting_enabled", "true")
    rec = {
        "execution_id": "bet-test0000000000001",
        "idempotency_key": "ik:odds",
        "game_id": "31160519",
        "alert_id": "31160519|75",
        "checkpoint": 75,
        "market": "TOTAL",
        "selection": "UNDER",
        "triggered_line": 189.5,
        "price": 1.80,
        "unit_price": 20.0,
        "stake_units": 1.0,
        "stake_amount": 20.0,
        "requested_amount": 20.0,
        "status": "PENDING",
    }
    claimed, _ = store.claim(rec)
    assert claimed is True
    return rec


def test_store_update_status_persists_price(tmp_path):
    store = BettingStore(str(tmp_path / "blm_betting.db"))
    _claim(store)
    store.update_status("bet-test0000000000001", "ACCEPTED", price=1.90)
    row = store.get_execution("bet-test0000000000001")
    assert row["price"] == 1.90


def test_execute_prefers_explicit_odds_accepted(tmp_path):
    cfg = make_cfg(tmp_path)
    store = BettingStore(cfg.db_path)
    rec = _claim(store)
    candidate = {
        "execution_id": rec["execution_id"],
        "idempotency_key": rec["idempotency_key"],
        "game_id": rec["game_id"],
        "alert_id": rec["alert_id"],
        "selection": rec["selection"],
        "market": rec["market"],
        "triggered_line": rec["triggered_line"],
        "price": rec["price"],
        "stake_amount": rec["stake_amount"],
    }
    provider = _StubProvider({
        "status": "ACCEPTED",
        "provider_ref": "PB-REF-1",
        "error_code": None,
        "error_message": None,
        "odds_accepted": [{"offered": 1.80, "accepted": 1.90,
                           "price_drift": 0.10}],
        "observed_odds": 1.88,
        "observed_line": 189.5,
    })
    execute(candidate, cfg=cfg, store=store, provider=provider)
    row = store.get_execution(rec["execution_id"])
    assert row["status"] == "ACCEPTED"
    assert row["price"] == 1.90


def test_execute_falls_back_to_observed_odds(tmp_path):
    cfg = make_cfg(tmp_path)
    store = BettingStore(cfg.db_path)
    rec = _claim(store)
    candidate = {
        "execution_id": rec["execution_id"],
        "idempotency_key": rec["idempotency_key"],
        "game_id": rec["game_id"],
        "alert_id": rec["alert_id"],
        "selection": rec["selection"],
        "market": rec["market"],
        "triggered_line": rec["triggered_line"],
        "price": rec["price"],
        "stake_amount": rec["stake_amount"],
    }
    provider = _StubProvider({
        "status": "ACCEPTED",
        "provider_ref": "PB-REF-2",
        "error_code": None,
        "error_message": None,
        "odds_accepted": [],
        "observed_odds": 1.88,
        "observed_line": 189.5,
    })
    execute(candidate, cfg=cfg, store=store, provider=provider)
    row = store.get_execution(rec["execution_id"])
    assert row["status"] == "ACCEPTED"
    assert row["price"] == 1.88
