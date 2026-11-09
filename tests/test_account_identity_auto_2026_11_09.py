"""BLM_EXPECTED_ACCOUNT_ID=auto — the signed-in account is the authorized one.

Operator: "the account that is loaded in Opera GX is the account that I want
loaded."  ``auto`` pins the first live identity the browser proves, so every
later submission is still compared against it: switching accounts blocks the
bet instead of silently following the swap.
"""
from __future__ import annotations

from blm_v4.betting.account_guard import verify_account
from blm_v4.betting.provider import PokerBetBrowserProvider


class _Adapter:
    def __init__(self, account):
        self.account = account

    def read_account_id(self):
        return self.account


def _provider(account):
    p = PokerBetBrowserProvider(adapter=_Adapter(account))
    p._bridge = object()          # _wire() early-returns; no browser needed
    return p


def test_auto_pins_the_first_live_account(monkeypatch):
    monkeypatch.setenv(PokerBetBrowserProvider.ACCOUNT_ENV, "auto")
    p = _provider("233997789")

    # identity is required even before the first read pinned anything
    assert p.requires_account_identity is True
    assert p.configured_account_ids == frozenset()

    assert p.authenticated_account_id == "233997789"
    assert p.configured_account_ids == frozenset({"233997789"})
    assert verify_account(p) is None


def test_auto_blocks_a_later_account_swap(monkeypatch):
    monkeypatch.setenv(PokerBetBrowserProvider.ACCOUNT_ENV, "auto")
    p = _provider("233997789")
    assert verify_account(p) is None

    p._adapter = _Adapter("9999999999")
    reason = verify_account(p)
    assert reason and "not a configured account" in reason


def test_auto_with_unreadable_session_fails_closed(monkeypatch):
    monkeypatch.setenv(PokerBetBrowserProvider.ACCOUNT_ENV, "auto")
    p = _provider(None)
    reason = verify_account(p)
    assert reason and "unverifiable" in reason


def test_auto_is_explicit_not_a_missing_value(monkeypatch):
    monkeypatch.delenv(PokerBetBrowserProvider.ACCOUNT_ENV, raising=False)
    p = _provider("233997789")
    assert p.requires_account_identity is False      # unchanged old contract
