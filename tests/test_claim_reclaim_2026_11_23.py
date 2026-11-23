"""A transiently BLOCKED claim is re-claimable; a money-risky one never is.

A blocked claim is provably money-free, yet it used to burn the checkpoint for
the rest of the day: every later pass hit ``duplicate_execution``.  Thirteen
ACCOUNT_MISMATCH blocks in one morning cost thirteen checkpoints that could
never fire again.  Re-claiming is bounded and only for pre-submission blocks.
"""
from __future__ import annotations

import sqlite3

import pytest

from blm_v4.betting.store import BettingStore, _day


def _rec(key: str = "auto:g31660130:75:0") -> dict:
    return {"execution_id": f"bet-{key}", "idempotency_key": key,
            "game_id": "31660130", "alert_id": "31660130|75",
            "checkpoint": "75", "status": "PENDING",
            "unit_price": 40.0, "stake_units": 1.0, "stake_amount": 40.0,
            "requested_amount": 40.0}


def _block(store: BettingStore, key: str, code: str, **extra) -> None:
    """Turn the claim on ``key`` into a BLOCKED row with ``code``."""
    sets = ", ".join(f"{k}=?" for k in ("status", "error_code", *extra))
    with store._conn() as c:                      # noqa: SLF001 - test seam
        c.execute(f"UPDATE bet_executions SET {sets} WHERE idempotency_key=?",
                  ("BLOCKED", code, *extra.values(), key))


@pytest.fixture()
def store(tmp_path):
    return BettingStore(str(tmp_path / "blm_betting.db"))


def test_transient_block_is_reclaimed_under_its_own_key(store):
    rec = _rec()
    assert store.claim(rec)[0] is True
    _block(store, rec["idempotency_key"], "ACCOUNT_MISMATCH")

    claimed, row = store.claim(_rec())
    assert claimed is True
    assert row["idempotency_key"] == f"{rec['idempotency_key']}#rc1"
    assert row["execution_id"] != rec["execution_id"]      # a new attempt, not a rewrite
    assert row["status"] == "PENDING"

    # the blocked row is untouched: history is append-only
    with store._conn() as c:                      # noqa: SLF001 - test seam
        old = c.execute("SELECT status, error_code FROM bet_executions "
                        "WHERE idempotency_key=?", (rec["idempotency_key"],)).fetchone()
    assert old["status"] == "BLOCKED" and old["error_code"] == "ACCOUNT_MISMATCH"


def test_reclaim_is_bounded(store):
    rec = _rec()
    store.claim(rec)
    _block(store, rec["idempotency_key"], "ACCOUNT_MISMATCH")
    for n in (1, 2):
        claimed, row = store.claim(_rec())
        assert claimed is True and row["idempotency_key"].endswith(f"#rc{n}")
        _block(store, row["idempotency_key"], "ACCOUNT_MISMATCH")
    # third re-attempt is refused, and refused with the base row, not a crash
    claimed, row = store.claim(_rec())
    assert claimed is False
    assert row["idempotency_key"] == rec["idempotency_key"]


@pytest.mark.parametrize("code", ["GLOBAL_KILL_SWITCH", "DAILY_BET_CAP",
                                  "EXPOSURE_CAP", "AMBIGUOUS_PRIOR"])
def test_operator_and_money_blocks_are_terminal(store, code):
    rec = _rec()
    store.claim(rec)
    _block(store, rec["idempotency_key"], code)
    claimed, row = store.claim(_rec())
    assert claimed is False
    assert row["idempotency_key"] == rec["idempotency_key"]


def test_a_row_that_may_have_been_sent_is_never_reclaimed(store):
    for extra in ({"submitted_at_utc": "2026-11-23T08:00:00.000Z"},
                  {"provider_ref": "PB-99123"}):
        key = f"auto:g{abs(hash(str(extra))) % 10**6}:75:0"
        rec = _rec(key)
        store.claim(rec)
        _block(store, key, "ACCOUNT_MISMATCH", **extra)
        claimed, row = store.claim(_rec(key))
        assert claimed is False, extra
        assert row["idempotency_key"] == key


def test_an_in_flight_claim_is_still_a_duplicate(store):
    rec = _rec()
    assert store.claim(rec)[0] is True          # PENDING, not BLOCKED
    claimed, row = store.claim(_rec())
    assert claimed is False
    assert row["status"] == "PENDING"
