"""R5 — balance below one unit → stake the whole balance (operator 2026-10-06).

ONE NAMED TEST PER CONTRACT PROPERTY:
  * test_balance_below_unit_stakes_the_whole_balance
  * test_balance_at_or_above_unit_stakes_the_unit
  * test_unknown_balance_stakes_the_unit
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blm_v4.betting.config import BettingConfig  # noqa: E402
from blm_v4.betting.executor import evaluate  # noqa: E402
from blm_v4.betting.store import BettingStore  # noqa: E402

OBS = (190.0, 190.5, 191.0, 191.5, 192.0, 192.5, 193.0, 193.5)


def _cfg(tmp_path) -> BettingConfig:
    return BettingConfig(
        dry_run=True, max_stake_per_bet=1000.0, max_bets_per_day=200,
        max_daily_exposure=10000.0, stake_units=1.0, min_unit_price=1.0,
        max_unit_price=1000.0, alert_max_age_s=90.0,
        db_path=str(tmp_path / "betting.db"))


def _store(tmp_path) -> BettingStore:
    return BettingStore(str(tmp_path / "betting.db"))


def _game(game_id="30990001", checkpoint=75) -> dict:
    cap = (datetime.now(timezone.utc)
           - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "game_id": game_id, "live": True, "live_reason": None,
        "market": {"total_line": 193.5, "market_status": "LIVE",
                   "observed_lines": list(OBS)},
        "projector": {"progress_pct": 76.0, "required_pts_per_min": 5.0,
                      "actual_pts_per_min": 4.0, "captured_at": cap,
                      "live_total_line": 193.5, "market_status": "LIVE"},
        "under_alert_eligibility": {"eligible": True, "reason": "market_live"},
        "under_alert": {"active": True, "checkpoint": checkpoint,
                        "trigger_line": 193.5, "trigger_progress": 77.0,
                        "trigger_captured_at": cap,
                        "alert_id": f"{game_id}|{checkpoint}"},
        "under_alert_fingerprint": {"fingerprint_count": 2,
                                    "fingerprints_fired": ["C3", "C5"]},
    }


def _stats() -> dict:
    return {"verifiable": True, "bets": 0, "amount": 0.0}


def _stake(tmp_path, unit, balance):
    cfg, store = _cfg(tmp_path), _store(tmp_path)
    out = evaluate(_game(), cfg=cfg, store=store, enabled=True,
                   unit_price=unit, stats=_stats(), balance=balance,
                   claim=False)
    assert out["decision"] in ("EXECUTE", "WOULD_BET"), out
    return out["candidate"]["stake_amount"]


def test_balance_below_unit_stakes_the_whole_balance(tmp_path):
    assert _stake(tmp_path, 80.0, 37.5) == 37.5


def test_balance_at_or_above_unit_stakes_the_unit(tmp_path):
    assert _stake(tmp_path, 80.0, 397.01) == 80.0
    assert _stake(tmp_path, 80.0, 80.0) == 80.0


def test_unknown_balance_stakes_the_unit(tmp_path):
    assert _stake(tmp_path, 80.0, None) == 80.0
