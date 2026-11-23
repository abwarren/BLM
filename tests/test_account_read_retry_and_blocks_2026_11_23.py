"""The identity read is retried when blind; the block count is queryable.

Context (2026-11-23): thirteen ACCOUNT_MISMATCH blocks in one morning, each
from ONE DOM read taken ~86 ms after the claim, while the browser was healthy
seconds later.  A blind read is now retried; a readable-but-different id is
not, because retrying that would turn a real account swap into a placed bet on
the wrong account.
"""
from __future__ import annotations

from blm_v4.betting.provider import PokerBetBrowserProvider as Provider
from blm_v4.betting.store import BettingStore, _day, _now


class _SeqAdapter:
    """An adapter whose read returns a scripted sequence, counting calls."""

    def __init__(self, seq):
        self.seq = list(seq)
        self.reads = 0

    def read_account_id(self):
        self.reads += 1
        return self.seq.pop(0) if self.seq else None


def _provider(seq):
    adapter = _SeqAdapter(seq)
    p = Provider(adapter=adapter)
    p._bridge = object()               # _wire() early-returns; no browser needed
    p.ACCOUNT_READ_SETTLE_S = 0.0      # no real sleeps in the test
    return p, adapter


def test_blind_read_is_retried_until_the_identity_appears(monkeypatch):
    monkeypatch.setenv(Provider.ACCOUNT_ENV, "auto")
    p, adapter = _provider([None, "", "233997789"])
    assert p.authenticated_account_id == "233997789"
    assert adapter.reads == 3


def test_a_readable_wrong_id_is_never_retried(monkeypatch):
    monkeypatch.setenv(Provider.ACCOUNT_ENV, "auto")
    p, adapter = _provider(["9999999999"])
    assert p.authenticated_account_id == "9999999999"
    assert adapter.reads == 1


def test_permanently_blind_read_gives_up_after_the_attempt_budget(monkeypatch):
    monkeypatch.setenv(Provider.ACCOUNT_ENV, "auto")
    p, adapter = _provider([None, None, None])
    assert p.authenticated_account_id is None
    assert adapter.reads == 3


def test_identity_block_count_is_todays_transient_blocks_only(tmp_path):
    store = BettingStore(str(tmp_path / "betting.db"))
    with store._conn() as c:                      # noqa: SLF001 - test seam
        def insert(status, code, day, key):
            c.execute(
                "INSERT INTO bet_executions (execution_id, idempotency_key, game_id,"
                " alert_id, checkpoint, status, error_code, requested_at_utc, day_utc)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (f"bet-{key}", key, "g1", "g1|75", "75", status, code, _now(), day))
        insert("BLOCKED", "ACCOUNT_MISMATCH", _day(), "a")
        insert("BLOCKED", "ACCOUNT_MISMATCH", _day(), "b")
        insert("BLOCKED", "GLOBAL_KILL_SWITCH", _day(), "c")
        insert("BLOCKED", "ACCOUNT_MISMATCH", "1999-01-01", "d")
        insert("FAILED", "ACCOUNT_MISMATCH", _day(), "e")
    assert store.account_mismatch_today() == 2
