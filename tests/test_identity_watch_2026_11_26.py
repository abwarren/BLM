"""The identity watch must make a fail-closed refusal LOUD, never quieter.

Regression for 2026-11-25: the browser stopped yielding identity reads at
11:24Z and the guard refused 53 claims over nine hours with no log line and no
auditable record — autobet looked armed and was silently placing nothing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from blm_v4.betting import identity_watch as iw
from blm_v4.betting.config import BettingConfig
from blm_v4.betting.executor import evaluate, execute
from blm_v4.betting.store import BettingStore

UNREADABLE = ("account identity unverifiable: authenticated or configured "
              "account missing")


@pytest.fixture(autouse=True)
def _clean():
    iw.reset()
    yield
    iw.reset()


def test_exempt_provider_is_not_watched():
    assert iw.observe(required=False, reason=None) is False
    assert iw.snapshot()["state"] == iw.OK
    assert iw.snapshot()["consecutive_blocks"] == 0


def test_a_healthy_match_keeps_the_watch_clear():
    assert iw.observe(required=True, reason=None) is False
    snap = iw.snapshot()
    assert snap["state"] == iw.OK and snap["consecutive_blocks"] == 0
    assert snap["last_ok_utc"]


def test_first_refusal_is_an_episode_and_warns(caplog):
    caplog.set_level(logging.WARNING, logger=iw.logger.name)
    assert iw.observe(required=True, reason=UNREADABLE) is True
    assert "identity_stall" in caplog.text
    snap = iw.snapshot()
    assert snap["state"] == iw.BLIND
    assert snap["consecutive_blocks"] == 1
    assert snap["blocked_since_utc"]


def test_repeat_refusals_do_not_mint_new_episodes():
    assert iw.observe(required=True, reason=UNREADABLE) is True
    for _ in range(5):
        assert iw.observe(required=True, reason=UNREADABLE) is False
    assert iw.snapshot()["consecutive_blocks"] == 6


def test_mismatch_and_blind_are_distinguished():
    iw.observe(required=True, reason=UNREADABLE)
    assert iw.snapshot()["state"] == iw.BLIND
    iw.reset()
    iw.observe(required=True,
               reason="authenticated account '999' is not a configured account")
    assert iw.snapshot()["state"] == iw.MISMATCH


def test_recovery_clears_the_episode_and_logs(caplog):
    caplog.set_level(logging.WARNING, logger=iw.logger.name)
    iw.observe(required=True, reason=UNREADABLE)
    iw.observe(required=True, reason=None)
    assert "identity_recovered" in caplog.text
    snap = iw.snapshot()
    assert snap["state"] == iw.OK
    assert snap["consecutive_blocks"] == 0
    assert snap["blocked_since_utc"] is None
    # a later stall is a NEW episode and is reported again
    assert iw.observe(required=True, reason=UNREADABLE) is True


class _BlindProvider:
    """An identity-requiring provider whose page cannot prove the account."""

    requires_account_identity = True
    authenticated_account_id = None
    submitted = False

    def submit(self, **kwargs):        # pragma: no cover - must never run
        type(self).submitted = True
        raise AssertionError("a blind identity must block before submit")


def _candidate(tmp_path):
    cap = (datetime.now(timezone.utc)
           - timedelta(seconds=5.0)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    game = {
        "game_id": "30990077", "live": True, "live_reason": None,
        "market": {"total_line": 193.5, "market_status": "LIVE",
                   "observed_lines": [190.0, 190.5, 191.0, 191.5, 192.0,
                                      192.5, 193.0, 193.5]},
        "projector": {"progress_pct": 88.0, "required_pts_per_min": 5.0,
                      "actual_pts_per_min": 4.0, "captured_at": cap,
                      "live_total_line": 193.5, "market_status": "LIVE"},
        "under_alert_eligibility": {"eligible": True, "reason": "market_live"},
        "under_alert": {"active": True, "checkpoint": 75,
                        "trigger_line": 193.5, "trigger_progress": 77.0,
                        "trigger_captured_at": cap,
                        "alert_id": "30990077|75"},
        "under_alert_fingerprint": {"fingerprint_count": 2,
                                    "fingerprints_fired": ["C3", "C5"]},
    }
    cfg = BettingConfig(
        dry_run=True, max_stake_per_bet=100.0, max_bets_per_day=5,
        max_daily_exposure=200.0, stake_units=1.0, min_unit_price=1.0,
        max_unit_price=1000.0, alert_max_age_s=90.0,
        db_path=str(tmp_path / "betting.db"))
    store = BettingStore(str(tmp_path / "betting.db"))
    store.set_config("auto_betting_enabled", "true")
    res = evaluate(game, cfg=cfg, store=store, enabled=True, unit_price=10.0,
                   stats={"verifiable": True, "bets": 0, "amount": 0.0},
                   claim=True, game_enabled=True)
    return store, cfg, res["candidate"]


def test_a_blind_identity_writes_one_stall_row_per_episode(tmp_path):
    store, cfg, candidate = _candidate(tmp_path)
    provider = _BlindProvider()

    execute(candidate, cfg=cfg, store=store, provider=provider)
    execute(candidate, cfg=cfg, store=store, provider=provider)

    rows = store.audit_recent(execution_id=candidate["execution_id"], limit=50)
    stalls = [r for r in rows if r["event"] == "identity_stall"]
    assert len(stalls) == 1                 # one episode, not one per bet
    assert "unverifiable" in (stalls[0]["reason"] or "")
    # the refusal itself is unchanged and fail-closed
    assert rows[0]["event"] == "result"
    assert provider.submitted is False


def test_stalled_flag_waits_for_the_threshold(monkeypatch):
    iw.observe(required=True, reason=UNREADABLE)
    assert iw.snapshot()["stalled"] is False        # one read, not a stall
    monkeypatch.setattr(iw, "STALL_AFTER_S", 0.0)
    assert iw.snapshot()["stalled"] is True
