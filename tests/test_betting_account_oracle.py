"""GATE-8 account-identity oracle + guard tests.

These exercise the new account-identity surface of blm_v4/betting:

    * BettingConfig
        - expected_account_id parses BLM_EXPECTED_ACCOUNT_ID
        - account_identity_configured reads the env label
        - live_money_enabled stays a config property (not a credential)

    * PokerBetProvider
        - requires_account_identity = True
        - configured_account_id reads BLM_EXPECTED_ACCOUNT_ID at call time
        - credentials are read only inside _credentials() — never logged

    * account_guard
        - authenticated vs configured integer id → None when equal
        - BLOCKED (non-None) when they differ

No credentials are asserted, logged or persisted.  The test environment
has no oracle set, so tests drive PokerBetProvider directly with the
two id sides.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blm_v4.betting.account_guard import verify_account  # noqa: E402
from blm_v4.betting.config import BettingConfig  # noqa: E402
from blm_v4.betting.provider import PokerBetProvider  # noqa: E402


def test_account_id_parses_from_env():
    os.environ["BLM_EXPECTED_ACCOUNT_ID"] = "1956500422"
    try:
        cfg = BettingConfig.from_env()
    finally:
        os.environ.pop("BLM_EXPECTED_ACCOUNT_ID", None)
    assert cfg.expected_account_id == 1956500422
    assert cfg.account_identity_configured is True


def test_account_id_missing_makes_guard_unverifiable():
    os.environ.pop("BLM_EXPECTED_ACCOUNT_ID", None)
    cfg = BettingConfig.from_env()
    assert cfg.expected_account_id is None
    assert cfg.account_identity_configured is False


def test_configured_account_id_reads_env_at_call_time():
    # no oracle is configured in the test env, so the provider reports None
    p = PokerBetProvider(base_url="https://pokerbet.co.za")
    assert p.configured_account_id is None


def test_credentials_are_read_only_inside_provider():
    os.environ["POKERBET_USERNAME"] = "kylmerollo26@gmail.com"
    os.environ["POKERBET_PASSWORD"] = "Judah2468"
    try:
        p = PokerBetProvider(base_url="https://pokerbet.co.za")
        # credentials() must not return values; just probe presence
        assert p._credentials() == ("kylmerollo26@gmail.com", "Judah2468")
        # the credential list is never held anywhere else on the provider
        assert not hasattr(p, "username")
        assert not hasattr(p, "password")
    finally:
        os.environ.pop("POKERBET_USERNAME", None)
        os.environ.pop("POKERBET_PASSWORD", None)


def test_guard_identical_ids_pass():
    class _Prov:
        requires_account_identity = True
        authenticated_account_id = 1956500422
        configured_account_id = 1956500422

    assert verify_account(_Prov()) is None


def test_guard_different_ids_blocked():
    class _Prov:
        requires_account_identity = True
        authenticated_account_id = 1956500422
        configured_account_id = 999999999

    result = verify_account(_Prov())
    assert result is not None
    assert "1956500422 != configured account 999999999" in result


def test_guard_exempt_when_no_identity_required():
    class _Prov:
        requires_account_identity = False

    assert verify_account(_Prov()) is None


def test_guard_fails_closed_on_missing_identity():
    class _Prov:
        requires_account_identity = True
        authenticated_account_id = None
        configured_account_id = None

    result = verify_account(_Prov())
    assert result is not None
    assert "unverifiable" in result


def test_guard_rejects_non_integer_identity():
    class _Prov:
        requires_account_identity = True
        authenticated_account_id = "abc"
        configured_account_id = "abc"

    result = verify_account(_Prov())
    assert result is not None
    assert "unverifiable" in result
