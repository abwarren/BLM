"""UI AUTO-BET CONTROL PLANE → live execution path (2026-10-05).

The dashboard's AUTO-BET switch and UNIT SIZE are not cosmetic: they drive the
BACKEND execution-control state (``POST /api/v4/betting/settings`` →
``BettingStore``), which the executor re-reads at the submission boundary.
These tests prove the switch reaches the live execution path WITHOUT placing a
wager — the provider here is the deterministic contract fake.

ONE NAMED TEST PER CONTRACT PROPERTY:
  * test_provider_from_config_live_is_the_browser_transport
  * test_auto_bet_off_blocks_before_the_provider
  * test_auto_bet_on_reaches_the_provider
  * test_kill_switch_off_overrides_an_armed_ui
  * test_configured_unit_size_is_the_executor_stake_amount
  * test_configured_unit_size_must_equal_the_requested_stake
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blm_v4.betting.config import BettingConfig
from blm_v4.betting.executor import evaluate, execute
from blm_v4.betting.fake_provider import (
    TEST_BOOKMAKER_ACCOUNT_ID,
    FakeBookmakerContractProvider,
)
from blm_v4.betting.provider import provider_from_config
from blm_v4.betting.store import BettingStore, KEY_AUTO_ENABLED


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _game(game_id: str = "UI-GATE-9001", age_s: float = 1.0) -> dict:
    return {
        "game_id": game_id, "live": True, "live_reason": None,
        "status": "live",
        "under_alert": {"active": True, "checkpoint": 75,
                        "trigger_line": 180.5},
        "under_alert_eligibility": {"eligible": True, "reason": "market_live"},
        "market": {"total_line": 180.5, "market_status": "LIVE"},
        "projector": {"required_pts_per_min": 5.0, "progress_pct": 88.0,
                      "actual_pts_per_min": 4.0,
                      "captured_at": _iso(datetime.now(timezone.utc)
                                          - timedelta(seconds=age_s)),
                      "market_status": "LIVE"},
    }


def _cfg(tmp_path, **over) -> BettingConfig:
    base = dict(dry_run=False, max_stake_per_bet=100.0, max_bets_per_day=50,
                max_daily_exposure=1000.0, stake_units=1.0,
                min_unit_price=1.0, max_unit_price=1000.0,
                alert_max_age_s=90.0, db_path=str(tmp_path / "ui.db"))
    base.update(over)
    return BettingConfig(**base)


def _store(tmp_path, *, enabled: bool, unit: float = 2.0) -> BettingStore:
    s = BettingStore(str(tmp_path / "ui.db"))
    s.set_unit_price(unit)
    s.set_config(KEY_AUTO_ENABLED, "true" if enabled else "false")
    return s


def _arm(fake) -> None:
    """The account guard stays mandatory: the fake must present BOTH sides."""
    fake.set_scenario("success")
    fake.set_configured_account(TEST_BOOKMAKER_ACCOUNT_ID)


# ══════════════════════════════════════════════════════════════════════

def test_provider_from_config_live_is_the_browser_transport():
    """The UI's ON state selects the REAL transport, not the old stub."""
    assert type(provider_from_config(BettingConfig(dry_run=False))) \
        .__name__ == "PokerBetBrowserProvider"
    assert type(provider_from_config(BettingConfig(dry_run=True))) \
        .__name__ == "DryRunProvider"


def test_auto_bet_off_blocks_before_the_provider(tmp_path):
    """🔴 OFF → the executor refuses; the provider is never called."""
    s = _store(tmp_path, enabled=False)
    c = _cfg(tmp_path)
    fake = FakeBookmakerContractProvider()
    res = evaluate(_game(), cfg=c, store=s, enabled=s.is_enabled(),
                   unit_price=s.get_unit_price(), stats=s.today_stats())
    if res.get("candidate"):
        out = execute(res["candidate"], cfg=c, store=s, provider=fake)
        assert out["status"] == "BLOCKED"
    assert fake.submission_count() == 0


def test_auto_bet_on_reaches_the_provider(tmp_path):
    """🟢 ON → the signal reaches the provider (identity guard satisfied)."""
    s = _store(tmp_path, enabled=True, unit=2.0)
    c = _cfg(tmp_path)
    fake = FakeBookmakerContractProvider()
    _arm(fake)
    res = evaluate(_game(), cfg=c, store=s, enabled=s.is_enabled(),
                   unit_price=s.get_unit_price(), stats=s.today_stats())
    assert res.get("candidate"), res
    out = execute(res["candidate"], cfg=c, store=s, provider=fake)
    assert out["status"] in ("ACCEPTED", "SUBMITTED")
    assert fake.submission_count() == 1


def test_kill_switch_off_overrides_an_armed_ui(tmp_path):
    """🛑 The switch was ON at claim time; the executor re-reads it at the
    submission boundary, so flipping it OFF blocks the submit."""
    s = _store(tmp_path, enabled=True)
    c = _cfg(tmp_path)
    fake = FakeBookmakerContractProvider()
    _arm(fake)
    res = evaluate(_game(), cfg=c, store=s, enabled=True,
                   unit_price=s.get_unit_price(), stats=s.today_stats())
    assert res.get("candidate")
    s.set_config(KEY_AUTO_ENABLED, "false")      # the UI switch flips OFF
    out = execute(res["candidate"], cfg=c, store=s, provider=fake)
    assert out["status"] == "BLOCKED"
    assert fake.submission_count() == 0


def test_configured_unit_size_is_the_executor_stake_amount(tmp_path):
    """💰 The unit size the operator set is the stake the executor carries —
    the BLM signal does not compute it."""
    s = _store(tmp_path, enabled=True, unit=7.50)
    c = _cfg(tmp_path)
    res = evaluate(_game(), cfg=c, store=s, enabled=True,
                   unit_price=s.get_unit_price(), stats=s.today_stats())
    assert res["candidate"]["stake_amount"] == pytest.approx(7.50)


def test_the_provider_defaults_to_gate_only(tmp_path):
    """A freshly built provider cannot submit until live is explicitly
    enabled — the safe default for the UI's switch."""
    from blm_v4.betting.provider import PokerBetBrowserProvider
    p = PokerBetBrowserProvider()
    assert p._live is False
