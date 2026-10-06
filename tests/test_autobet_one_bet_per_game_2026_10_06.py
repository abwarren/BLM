"""R4 — ONE auto-bet per game (operator directive 2026-10-06).

Once a REAL bet has been SUBMITTED/ACCEPTED for a game, the executor must NEVER
auto-bet that game again — at any checkpoint.  No real browser, no money.

ONE NAMED TEST PER CONTRACT PROPERTY:
  * test_one_bet_per_game_blocks_a_second_bet_on_the_same_game
  * test_one_bet_per_game_still_allows_a_different_game
  * test_one_bet_per_game_ignores_failed_attempts
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
        dry_run=True, max_stake_per_bet=100.0, max_bets_per_day=5,
        max_daily_exposure=200.0, stake_units=1.0, min_unit_price=1.0,
        max_unit_price=1000.0, alert_max_age_s=90.0,
        db_path=str(tmp_path / "betting.db"))


def _store(tmp_path) -> BettingStore:
    return BettingStore(str(tmp_path / "betting.db"))


def _game(game_id="30990001", checkpoint=75, age_s=5.0, trigger=193.5,
          cur=193.5) -> dict:
    cap = (datetime.now(timezone.utc)
           - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "game_id": game_id, "live": True, "live_reason": None,
        "market": {"total_line": cur, "market_status": "LIVE",
                   "observed_lines": list(OBS)},
        "projector": {"progress_pct": 76.0, "required_pts_per_min": 5.0,
                      "actual_pts_per_min": 4.0, "captured_at": cap,
                      "live_total_line": cur, "market_status": "LIVE"},
        "under_alert_eligibility": {"eligible": True, "reason": "market_live"},
        "under_alert": {"active": True, "checkpoint": checkpoint,
                        "trigger_line": trigger, "trigger_progress": 77.0,
                        "trigger_captured_at": cap,
                        "alert_id": f"{game_id}|{checkpoint}"},
        "under_alert_fingerprint": {"fingerprint_count": 2,
                                    "fingerprints_fired": ["C3", "C5"]},
    }


def _stats() -> dict:
    return {"verifiable": True, "bets": 0, "amount": 0.0}


def _place(store: BettingStore, game_id: str, status: str) -> None:
    """Record a completed execution row for ``game_id`` with ``status``."""
    eid = f"bet-{game_id}-{status}"
    store.claim({
        "execution_id": eid, "idempotency_key": f"{game_id}|{status}",
        "game_id": game_id, "alert_id": f"{game_id}|75", "checkpoint": 75,
        "market": "TOTAL", "selection": "UNDER", "triggered_line": 193.5,
        "price": 1.9, "unit_price": 10.0, "stake_units": 1.0,
        "stake_amount": 10.0, "requested_amount": 10.0, "status": "PENDING"})
    store.update_status(eid, status)


def _ev(cfg, store, g):
    return evaluate(g, cfg=cfg, store=store, enabled=True, unit_price=10.0,
                    stats=_stats(), claim=True, game_enabled=True)


def test_one_bet_per_game_blocks_a_second_bet_on_the_same_game(tmp_path):
    cfg, store = _cfg(tmp_path), _store(tmp_path)
    g = _game("30990001")
    assert _ev(cfg, store, g)["decision"] in ("EXECUTE", "WOULD_BET")  # first
    _place(store, "30990001", "SUBMITTED")                            # placed
    out = _ev(cfg, store, _game("30990001", checkpoint=100))          # re-fires
    assert out["decision"] == "NO_BET"
    assert out["reason"] == "one_bet_per_game"


def test_one_bet_per_game_still_allows_a_different_game(tmp_path):
    cfg, store = _cfg(tmp_path), _store(tmp_path)
    _place(store, "30990001", "SUBMITTED")
    out = _ev(cfg, store, _game("30990002"))
    assert out["decision"] in ("EXECUTE", "WOULD_BET")


def test_one_bet_per_game_ignores_failed_attempts(tmp_path):
    cfg, store = _cfg(tmp_path), _store(tmp_path)
    _place(store, "30990001", "FAILED")          # not a placed bet
    out = _ev(cfg, store, _game("30990001"))
    assert out["decision"] in ("EXECUTE", "WOULD_BET")
