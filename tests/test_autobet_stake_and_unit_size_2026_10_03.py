"""Auto-Bet STAKE + UNIT SIZE + THREE MODES — automated tests (2026-10-03).

Pins the operator directives:
  * R2.00 (ZAR) is the EXACT real-money TEST stake — not the production stake.
  * PRODUCTION_AUTO_BET stakes EXACTLY the user-configured Unit Size.
  * ZERO_STAKE is R0.00 simulation.
  * Fail closed on missing/zero/negative/non-finite unit size, wrong currency,
    missing authorization.
  * "maximum" and "exact" are separately checkable.

Reference spec: the canonical module blm_v4/betting/stake.py.
"""
from __future__ import annotations

import pytest

from blm_v4.betting import stake as S


# ── the R2.00 hard invariant (REAL_MONEY_TEST) ────────────────────────────
def test_real_money_test_exact_2_00_allows_when_authorized():
    r = S.resolve_stake(S.REAL_MONEY_TEST, currency="ZAR", authorized=True)
    assert r["ok"] is True
    assert r["stake"] == 2.00
    assert r["currency"] == "ZAR"


def test_real_money_test_requires_authorization():
    r = S.resolve_stake(S.REAL_MONEY_TEST, currency="ZAR", authorized=False)
    assert r["ok"] is False and r["reason"] == S.R_AUTH
    assert r["stake"] is None


@pytest.mark.parametrize("amount,currency", [
    (1.00, "ZAR"), (2.01, "ZAR"), (5.00, "ZAR"), (10.00, "ZAR"),
    (2.00, "USD"), (2.00, None), (None, "ZAR"),
])
def test_authorized_test_stake_rejects_anything_not_exactly_2_00_zar(amount, currency):
    assert S.is_authorized_test_stake(amount, currency) is False


def test_authorized_test_stake_accepts_only_exact_2_00_zar():
    assert S.is_authorized_test_stake(2.00, "ZAR") is True
    # maximum and exact are the same value here, but asserted separately
    assert S.REAL_MONEY_TEST_STAKE == S.REAL_MONEY_TEST_MAX_STAKE == 2.00


# ── production unit size ──────────────────────────────────────────────────
def test_production_uses_configured_unit_size_not_2_00():
    r = S.resolve_stake(S.PRODUCTION_AUTO_BET, unit_size=5.00,
                        currency="ZAR", authorized=True)
    assert r["ok"] is True and r["stake"] == 5.00


def test_production_unit_size_round_trip_is_not_rounded():
    # a two-decimal unit size survives verbatim (no silent rounding)
    r = S.resolve_stake(S.PRODUCTION_AUTO_BET, unit_size=12.34,
                        currency="ZAR", authorized=True)
    assert r["stake"] == 12.34


@pytest.mark.parametrize("unit_size", [None, 0, 0.0, -1.0, -0.01,
                                       float("nan"), float("inf"),
                                       float("-inf")])
def test_production_missing_or_invalid_unit_size_fails_closed(unit_size):
    r = S.resolve_stake(S.PRODUCTION_AUTO_BET, unit_size=unit_size,
                        currency="ZAR", authorized=True)
    assert r["ok"] is False and r["stake"] is None
    assert r["reason"] in (S.R_UNIT_SIZE_MISSING, S.R_UNIT_SIZE_INVALID)


def test_production_wrong_currency_fails_closed():
    r = S.resolve_stake(S.PRODUCTION_AUTO_BET, unit_size=5.0,
                        currency="USD", authorized=True)
    assert r["ok"] is False and r["reason"] == S.R_CURRENCY


def test_production_requires_authorization():
    r = S.resolve_stake(S.PRODUCTION_AUTO_BET, unit_size=5.0,
                        currency="ZAR", authorized=False)
    assert r["ok"] is False and r["reason"] == S.R_AUTH


# ── zero stake ────────────────────────────────────────────────────────────
def test_zero_stake_is_r0_simulation():
    r = S.resolve_stake(S.ZERO_STAKE, authorized=False)
    assert r["ok"] is True and r["stake"] == 0.0 and r["currency"] == "ZAR"


# ── mode safety ───────────────────────────────────────────────────────────
def test_unknown_mode_fails_closed():
    r = S.resolve_stake("LIVE_PROD", unit_size=5.0, currency="ZAR",
                        authorized=True)
    assert r["ok"] is False and r["reason"] == S.R_MODE_INVALID


def test_r2_00_is_not_the_production_default():
    """PRODUCTION_AUTO_BET with no configured unit size must NOT fall back to
    the R2.00 test stake — it must fail closed."""
    r = S.resolve_stake(S.PRODUCTION_AUTO_BET, unit_size=None,
                        currency="ZAR", authorized=True)
    assert r["ok"] is False and r["stake"] != 2.00


# ── explicit authorization proof ──────────────────────────────────────────
def test_authorization_proof_zero_stake_needs_none():
    p = S.authorization_proof(S.ZERO_STAKE, authorized=False)
    assert p["required"] is False and p["satisfied"] is True


def test_authorization_proof_real_money_needs_actor():
    assert S.authorization_proof(S.PRODUCTION_AUTO_BET, authorized=True,
                                 authorized_by="")["satisfied"] is False
    assert S.authorization_proof(S.PRODUCTION_AUTO_BET, authorized=True,
                                 authorized_by="operator")["satisfied"] is True
