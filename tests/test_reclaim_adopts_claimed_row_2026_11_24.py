"""A re-claimed BLOCKED attempt must WRITE TO THE ROW IT CLAIMED.

``BettingStore.claim`` re-claims a transiently-BLOCKED key under its own
``<key>#rc<N>`` identity and returns that NEW row (fresh ``execution_id``,
status PENDING) so nothing is overwritten.  The executor used to ignore the
returned row and keep the ``execution_id`` it computed before the claim, so:

  * every status update landed on the ORIGINAL, already-BLOCKED row — its
    ``resolved_at_utc`` kept advancing as if it had been retried;
  * the ``#rc`` row that actually carried the attempt stayed PENDING forever.

Observed live 2026-11-24 on game 31678118: row 10566 BLOCKED (resolved_at
still moving), 10568 ``#rc1`` and 10570 ``#rc2`` PENDING with ``resolved_at``
NULL — 76 such orphaned PENDING rows in total, invisible to the placed-bets
view because PENDING is not a placed status.

ONE NAMED TEST PER CONTRACT PROPERTY.
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blm_v4.betting.config import BettingConfig  # noqa: E402
from blm_v4.betting.executor import evaluate, execute  # noqa: E402
from blm_v4.betting.provider import DryRunProvider  # noqa: E402
from blm_v4.betting.store import BettingStore  # noqa: E402

OBS = (190.0, 190.5, 191.0, 191.5, 192.0, 192.5, 193.0, 193.5)
GAME = "30990001"
CHECKPOINT = 75
BASE_IK = f"{GAME}|{CHECKPOINT}|{GAME}|{CHECKPOINT}"


def _cfg(tmp_path) -> BettingConfig:
    return BettingConfig(
        dry_run=True, max_stake_per_bet=100.0, max_bets_per_day=5,
        max_daily_exposure=200.0, stake_units=1.0, min_unit_price=1.0,
        max_unit_price=1000.0, alert_max_age_s=90.0,
        db_path=str(tmp_path / "betting.db"))


def _store(tmp_path) -> BettingStore:
    store = BettingStore(str(tmp_path / "betting.db"))
    store.set_config("auto_betting_enabled", "true")
    return store


def _game() -> dict:
    cap = (datetime.now(timezone.utc)
           - timedelta(seconds=5.0)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "game_id": GAME, "live": True, "live_reason": None,
        "market": {"total_line": 193.5, "market_status": "LIVE",
                   "observed_lines": list(OBS)},
        "projector": {"progress_pct": 88.0, "required_pts_per_min": 5.0,
                      "actual_pts_per_min": 4.0, "captured_at": cap,
                      "live_total_line": 193.5, "market_status": "LIVE"},
        "under_alert_eligibility": {"eligible": True, "reason": "market_live"},
        "under_alert": {"active": True, "checkpoint": CHECKPOINT,
                        "trigger_line": 193.5, "trigger_progress": 77.0,
                        "trigger_captured_at": cap,
                        "alert_id": f"{GAME}|{CHECKPOINT}"},
        "under_alert_fingerprint": {"fingerprint_count": 2,
                                    "fingerprints_fired": ["C3", "C5"]},
    }


def _stats() -> dict:
    return {"verifiable": True, "bets": 0, "amount": 0.0}


def _seed_blocked(store: BettingStore) -> str:
    """The original attempt: claimed, then BLOCKED pre-submission."""
    eid = "bet-" + hashlib.sha256(BASE_IK.encode("utf-8")).hexdigest()[:20]
    claimed, _ = store.claim({
        "execution_id": eid, "idempotency_key": BASE_IK,
        "game_id": GAME, "alert_id": f"{GAME}|{CHECKPOINT}",
        "checkpoint": CHECKPOINT, "market": "TOTAL", "selection": "UNDER",
        "triggered_line": 193.5, "price": 1.9, "unit_price": 10.0,
        "stake_units": 1.0, "stake_amount": 10.0, "requested_amount": 10.0,
        "status": "PENDING"})
    assert claimed
    store.update_status(eid, "BLOCKED", error_code="ACCOUNT_MISMATCH",
                        error_message="account identity unverifiable",
                        rejection_reason="ACCOUNT_MISMATCH")
    return eid


def _seed_failed(store: BettingStore, code: str, *,
                 submitted: bool = False) -> str:
    """A claim that FAILED before any click (the submit-control gate)."""
    eid = "bet-" + hashlib.sha256(BASE_IK.encode("utf-8")).hexdigest()[:20]
    claimed, _ = store.claim({
        "execution_id": eid, "idempotency_key": BASE_IK,
        "game_id": GAME, "alert_id": f"{GAME}|{CHECKPOINT}",
        "checkpoint": CHECKPOINT, "market": "TOTAL", "selection": "UNDER",
        "triggered_line": 193.5, "price": 1.9, "unit_price": 10.0,
        "stake_units": 1.0, "stake_amount": 10.0, "requested_amount": 10.0,
        "status": "PENDING"})
    assert claimed
    if submitted:                     # a position may exist: stamp the send
        store.update_status(eid, "SUBMITTED")
    store.update_status(eid, "FAILED", error_code=code,
                        rejection_reason=code)
    return eid


def _evaluate(store: BettingStore, cfg: BettingConfig) -> dict:
    return evaluate(_game(), cfg=cfg, store=store, enabled=True,
                    unit_price=10.0, stats=_stats(), claim=True,
                    game_enabled=True)


def test_a_reclaimed_attempt_adopts_the_claimed_identity(tmp_path):
    store, cfg = _store(tmp_path), _cfg(tmp_path)
    blocked_eid = _seed_blocked(store)

    res = _evaluate(store, cfg)
    assert res["decision"] == "WOULD_BET"          # dry run
    cand = res["candidate"]
    assert cand["idempotency_key"] == f"{BASE_IK}#rc1"
    assert cand["execution_id"] != blocked_eid

    rc = store.get_by_idempotency(f"{BASE_IK}#rc1")
    assert rc is not None
    assert rc["execution_id"] == cand["execution_id"]
    assert rc["status"] == "PENDING"


def test_the_blocked_row_is_history_and_is_never_touched_again(tmp_path):
    store, cfg = _store(tmp_path), _cfg(tmp_path)
    blocked_eid = _seed_blocked(store)
    before = store.get_by_idempotency(BASE_IK)

    res = _evaluate(store, cfg)
    execute(res["candidate"], cfg=cfg, store=store,
            provider=DryRunProvider())

    after = store.get_by_idempotency(BASE_IK)
    assert after["execution_id"] == blocked_eid
    assert after["status"] == "BLOCKED"
    assert after["error_code"] == "ACCOUNT_MISMATCH"
    assert after["resolved_at_utc"] == before["resolved_at_utc"]


def test_the_reclaimed_row_reaches_a_terminal_state(tmp_path):
    store, cfg = _store(tmp_path), _cfg(tmp_path)
    _seed_blocked(store)

    res = _evaluate(store, cfg)
    out = execute(res["candidate"], cfg=cfg, store=store,
                  provider=DryRunProvider())

    rc = store.get_by_idempotency(f"{BASE_IK}#rc1")
    assert out["status"] == "ACCEPTED"
    assert rc["status"] == "ACCEPTED"
    # DRY_RUN records the pass as WOULD_BET (executor contract, §11)
    assert rc["execution_state"] == "WOULD_BET"
    assert rc["resolved_at_utc"] is not None       # no longer an orphan


def test_the_audit_trail_follows_the_reclaimed_row(tmp_path):
    store, cfg = _store(tmp_path), _cfg(tmp_path)
    _seed_blocked(store)

    res = _evaluate(store, cfg)
    cand = res["candidate"]
    events = store.audit_recent(execution_id=cand["execution_id"], limit=20)
    assert [e["event"] for e in events].count("claimed") == 1
    assert all(e["idempotency_key"] == f"{BASE_IK}#rc1" for e in events)


def test_a_pre_submission_submit_control_failure_is_reclaimable(tmp_path):
    """The submit CONTROL gate refuses BEFORE the click: no ticket, no
    position.  Burning the checkpoint there cost real bets (2026-11-26:
    armed=1 with ``duplicate_execution`` on 118 of 123 passes)."""
    store, cfg = _store(tmp_path), _cfg(tmp_path)
    failed_eid = _seed_failed(store, "SUBMIT_CONTROL_UNAVAILABLE")

    res = _evaluate(store, cfg)
    assert res["decision"] == "WOULD_BET"
    assert res["candidate"]["idempotency_key"] == f"{BASE_IK}#rc1"
    assert res["candidate"]["execution_id"] != failed_eid
    # the failed row keeps its own history
    original = store.get_by_idempotency(BASE_IK)
    assert original["status"] == "FAILED"
    assert original["error_code"] == "SUBMIT_CONTROL_UNAVAILABLE"


def test_the_revalidation_pace_refusal_is_reclaimable(tmp_path):
    """`revalidate` refuses BEFORE `provider.submit`, so no position exists.

    Game 31695846 burned its checkpoint on this code at 2026-11-25T00:46:46Z
    and every later pass on that checkpoint became `duplicate_execution`.
    """
    store, cfg = _store(tmp_path), _cfg(tmp_path)
    _seed_failed(store, "REVALIDATION_PACE_BELOW_EXECUTION_THRESHOLD")

    res = _evaluate(store, cfg)
    assert res["decision"] == "WOULD_BET"
    assert res["candidate"]["idempotency_key"] == f"{BASE_IK}#rc1"


def test_a_gone_opportunity_is_not_reclaimed(tmp_path):
    """A stale capture means the bet is gone, not that the claim may retry."""
    store, cfg = _store(tmp_path), _cfg(tmp_path)
    _seed_failed(store, "REVALIDATION_STALE")
    assert _evaluate(store, cfg)["reason"] == "duplicate_execution"


def test_burned_checkpoints_counts_only_unretryable_rows(tmp_path):
    store = _store(tmp_path)
    assert store.burned_checkpoints_today()["count"] == 0

    _seed_failed(store, "REVALIDATION_STALE")
    burned = store.burned_checkpoints_today()
    assert burned["count"] == 1
    assert burned["by_code"] == {"REVALIDATION_STALE": 1}


def test_a_failed_row_that_may_have_reached_the_bookmaker_is_not_reclaimed(
        tmp_path):
    store, cfg = _store(tmp_path), _cfg(tmp_path)
    _seed_failed(store, "STAKE_MISMATCH")
    assert _evaluate(store, cfg)["reason"] == "duplicate_execution"

    other_dir = tmp_path / "b"
    other_dir.mkdir()
    other = _store(other_dir)
    _seed_failed(other, "SUBMIT_CONTROL_UNAVAILABLE", submitted=True)
    assert _evaluate(other, _cfg(other_dir))["reason"] == \
        "duplicate_execution"


def test_the_reclaim_count_stays_bounded(tmp_path):
    store, cfg = _store(tmp_path), _cfg(tmp_path)
    _seed_blocked(store)

    for expected in ("#rc1", "#rc2"):
        res = _evaluate(store, cfg)
        assert res["candidate"]["idempotency_key"] == f"{BASE_IK}{expected}"
        # each reclaim is blocked again, so the next pass may try once more
        store.update_status(res["candidate"]["execution_id"], "BLOCKED",
                            error_code="ACCOUNT_MISMATCH",
                            rejection_reason="ACCOUNT_MISMATCH")

    res = _evaluate(store, cfg)
    assert res["decision"] == "NO_BET"
    assert res["reason"] == "duplicate_execution"
    assert store.get_by_idempotency(f"{BASE_IK}#rc3") is None
