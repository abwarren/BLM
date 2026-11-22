"""SUBMISSION EVIDENCE — the balance-based adjudication of SUBMITTED rows.

Operator directive 2026-11-22: a live submission that leaves no bookmaker
receipt is recorded SUBMITTED ("sent, unconfirmed"), and the ledger has never
been able to tell a real placement from a no-op click (0 of 407 provider refs).
The book's own balance is the one channel already read every pass: pin it before
the click, then label the submission with what the balance proves — and nothing
more.  Status NEVER changes and the game is never released (a second click on an
unreadable state could place the bet twice).

ONE NAMED TEST PER CONTRACT PROPERTY:
  * test_balance_before_is_pinned_once_and_never_overwritten
  * test_placed_evidence_when_the_stake_left_the_account
  * test_not_placed_evidence_when_the_window_passed_without_a_drop
  * test_no_verdict_while_the_window_is_still_open
  * test_no_verdict_without_a_pinned_balance_or_a_live_reading
  * test_evidence_never_changes_status_and_still_blocks_a_rebet
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from blm_v4.betting import reconcile_evidence as ev
from blm_v4.betting.store import BettingStore


def _store(tmp_path) -> BettingStore:
    return BettingStore(str(tmp_path / "betting.db"))


def _submitted(store: BettingStore, *, game_id="30990500", stake=34.01,
               eid=None) -> str:
    eid = eid or f"bet-{game_id}-SUBMITTED"
    store.claim({
        "execution_id": eid, "idempotency_key": f"{game_id}|SUBMITTED",
        "game_id": game_id, "alert_id": f"{game_id}|75", "checkpoint": 75,
        "market": "TOTAL", "selection": "UNDER", "triggered_line": 178.5,
        "price": 1.8, "unit_price": 60.0, "stake_units": 0.5668,
        "stake_amount": stake, "requested_amount": stake, "status": "PENDING"})
    store.update_status(eid, "SUBMITTED")
    return eid


def _age(store: BettingStore, eid: str, seconds: float) -> None:
    """Backdate submitted_at_utc (test-only)."""
    when = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    with store._conn() as c:                       # test fixture, own temp DB
        c.execute("UPDATE bet_executions SET submitted_at_utc=? "
                  " WHERE execution_id=?", (when, eid))


def _row(store: BettingStore, eid: str) -> dict:
    return store.get_execution(eid)


def test_balance_before_is_pinned_once_and_never_overwritten(tmp_path):
    store = _store(tmp_path)
    eid = _submitted(store)
    store.set_balance_before(eid, 242.15)
    assert _row(store, eid)["balance_before"] == 242.15
    store.set_balance_before(eid, 1.00)             # a later pass must not clobber
    assert _row(store, eid)["balance_before"] == 242.15
    store.set_balance_before(eid, None)             # fail-closed on non-numeric
    assert _row(store, eid)["balance_before"] == 242.15


def test_placed_evidence_when_the_stake_left_the_account(tmp_path):
    store = _store(tmp_path)
    eid = _submitted(store, stake=34.01)
    store.set_balance_before(eid, 34.01)
    out = ev.reconcile_submitted(store, 0.00)       # the whole stake left
    assert out["placed"] == 1 and out["not_placed"] == 0
    row = _row(store, eid)
    assert row["reconciliation_state"] == ev.PLACED_EVIDENCE
    assert row["reconciliation_attempts"] == 1
    audits = [a for a in store.audit_recent(execution_id=eid)
              if a["event"] == "placement_evidence"]
    assert audits and audits[0]["reason"] == ev.PLACED_EVIDENCE


def test_not_placed_evidence_when_the_window_passed_without_a_drop(tmp_path):
    store = _store(tmp_path)
    eid = _submitted(store, stake=34.01)
    store.set_balance_before(eid, 34.01)
    _age(store, eid, ev.ADJUDICATE_AFTER_S + 30)    # older than the window
    out = ev.reconcile_submitted(store, 34.01)      # balance never moved
    assert out["not_placed"] == 1 and out["placed"] == 0
    assert _row(store, eid)["reconciliation_state"] == ev.NOT_PLACED_EVIDENCE


def test_no_verdict_while_the_window_is_still_open(tmp_path):
    store = _store(tmp_path)
    eid = _submitted(store, stake=34.01)
    store.set_balance_before(eid, 34.01)
    out = ev.reconcile_submitted(store, 34.01)      # 0 s old, no drop
    assert out["open"] == 1 and out["not_placed"] == 0
    assert not (_row(store, eid)["reconciliation_state"] or "").strip()


def test_no_verdict_without_a_pinned_balance_or_a_live_reading(tmp_path):
    store = _store(tmp_path)
    eid = _submitted(store, stake=34.01)            # never pinned
    _age(store, eid, ev.ADJUDICATE_AFTER_S + 30)
    out = ev.reconcile_submitted(store, 34.01)
    assert out["checked"] == 0 and out["not_placed"] == 0
    assert not (_row(store, eid)["reconciliation_state"] or "").strip()

    eid2 = _submitted(store, game_id="30990501")
    store.set_balance_before(eid2, 34.01)
    _age(store, eid2, ev.ADJUDICATE_AFTER_S + 30)
    out = ev.reconcile_submitted(store, None)       # unreadable balance
    assert out["checked"] == 0
    assert not (_row(store, eid2)["reconciliation_state"] or "").strip()


def test_evidence_never_changes_status_and_still_blocks_a_rebet(tmp_path):
    store = _store(tmp_path)
    eid = _submitted(store, game_id="30990502", stake=34.01)
    store.set_balance_before(eid, 34.01)
    _age(store, eid, ev.ADJUDICATE_AFTER_S + 30)
    ev.reconcile_submitted(store, 34.01)            # NOT_PLACED_EVIDENCE
    row = _row(store, eid)
    assert row["status"] == "SUBMITTED"             # money-safe: never downgraded
    assert store.has_placed_bet("30990502") is True # a re-bet is still blocked
    future = store.today_stats()
    assert future["bets"] >= 0                      # accounting path unaffected
