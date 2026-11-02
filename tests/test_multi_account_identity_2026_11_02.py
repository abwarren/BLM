"""The account identity guard may permit SEVERAL accounts (2026-11-02).

Operator: "cant i use different accounts?"  Yes — the browser may be signed into
any ONE of a declared set.  Everything the guard did before is unchanged: an
account outside the set, an unreadable authenticated id, or an empty set on an
identity-requiring provider still BLOCKS the bet before any submission.
"""
from __future__ import annotations

import importlib

import pytest

from blm_v4.betting.account_guard import verify_account

prov_mod = importlib.import_module("blm_v4.betting.provider")


def _provider_class():
    for name in dir(prov_mod):
        obj = getattr(prov_mod, name)
        if isinstance(obj, type) and hasattr(obj, "ACCOUNT_ENV"):
            return obj
    raise AssertionError("no provider class exposes ACCOUNT_ENV")


class Single:
    """The pre-existing contract: one configured id."""
    requires_account_identity = True

    def __init__(self, auth, configured):
        self.authenticated_account_id = auth
        self.configured_account_id = configured


class Multi:
    """The new contract: a permitted set."""
    requires_account_identity = True

    def __init__(self, auth, ids):
        self.authenticated_account_id = auth
        self.configured_account_ids = frozenset(ids)


def test_single_account_still_matches():
    assert verify_account(Single("1956500422", "1956500422")) is None


def test_single_account_still_blocks_another():
    reason = verify_account(Single("1956500422", "233997789"))
    assert reason and "not a configured account" in reason


@pytest.mark.parametrize("acct", ["1956500422", "233997789"])
def test_any_permitted_account_is_accepted(acct):
    assert verify_account(Multi(acct, {"1956500422", "233997789"})) is None


def test_an_unlisted_account_is_blocked():
    reason = verify_account(Multi("9999999999", {"1956500422"}))
    assert reason and "9999999999" in reason


def test_unreadable_authenticated_id_fails_closed():
    assert verify_account(Multi(None, {"1956500422"}))
    assert verify_account(Multi("", {"1956500422"}))


def test_empty_permitted_set_fails_closed():
    assert verify_account(Multi("1956500422", set()))


def test_env_accepts_a_list(monkeypatch):
    cls = _provider_class()
    inst = cls.__new__(cls)
    for raw, expected in (
        ("1956500422", {"1956500422"}),
        ("1956500422,233997789", {"1956500422", "233997789"}),
        ("1956500422; 233997789 ", {"1956500422", "233997789"}),
        ("", set()),
    ):
        monkeypatch.setenv(cls.ACCOUNT_ENV, raw)
        assert set(inst.configured_account_ids) == expected
        assert inst.configured_account_id == (sorted(expected)[0] if expected else None)


def test_a_non_identity_provider_is_exempt():
    class Dry:
        requires_account_identity = False
    assert verify_account(Dry()) is None
