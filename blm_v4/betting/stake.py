"""Auto-Bet STAKE authority — the THREE execution modes + unit size + R2.00.

Operator directives 2026-10-03.  There are exactly THREE execution modes and
each has exactly ONE permitted stake:

    ZERO_STAKE          -> R0.00 (simulation only; never real money)
    REAL_MONEY_TEST     -> EXACTLY R2.00 ZAR   (the ONLY authorized real-money
                           test stake; a separate, explicitly-authorized gate)
    PRODUCTION_AUTO_BET -> EXACTLY the user-configured BLM Unit Size

R2.00 is NOT the production stake.  The production stake is the user's
configured Unit Size, persisted and shown in the BLM UI.  Neither is derived
from bankroll; neither is a hidden default.

FAIL CLOSED: no mode, no currency, no configured unit size, a non-positive or
non-finite unit size, or a real-money mode without explicit authorization all
resolve to NO BET — never a substituted or rounded stake.

Pure functions only — no DB, no network.  Persistence lives in the store; this
module is the single authority for WHAT stake a mode permits.
"""
from __future__ import annotations

import math
from typing import Any, Optional

# ── modes ─────────────────────────────────────────────────────────────────
ZERO_STAKE = "ZERO_STAKE"
REAL_MONEY_TEST = "REAL_MONEY_TEST"
PRODUCTION_AUTO_BET = "PRODUCTION_AUTO_BET"
MODES = (ZERO_STAKE, REAL_MONEY_TEST, PRODUCTION_AUTO_BET)

CURRENCY = "ZAR"

#: The ONLY authorized real-money TEST stake (exact).  NOT a production value.
REAL_MONEY_TEST_STAKE = 2.00
#: The maximum real-money TEST stake — a second, independent statement of the
#: same invariant so "exact" and "maximum" are separately checkable.
REAL_MONEY_TEST_MAX_STAKE = 2.00

# refusal reasons (stable, auditable)
R_MODE_INVALID = "stake_mode_invalid"
R_CURRENCY = "stake_currency_invalid"
R_UNIT_SIZE_MISSING = "unit_size_missing"
R_UNIT_SIZE_INVALID = "unit_size_invalid"      # zero / negative / non-finite
R_TEST_STAKE = "real_money_test_stake_must_equal_2_00"
R_AUTH = "real_money_not_authorized"


def _f(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def is_authorized_test_stake(amount: Any, currency: Any) -> bool:
    """True ONLY for exactly R2.00 in ZAR (the authorized test stake).

    Separate from the mode check so "maximum" and "exact" can each be asserted:
    an amount above the maximum is rejected, and an amount below it is ALSO
    rejected (the test stake is exact, not a ceiling).
    """
    a, c = _f(amount), (str(currency).upper() if currency is not None else None)
    return (a is not None and c == CURRENCY
            and a == REAL_MONEY_TEST_STAKE
            and a <= REAL_MONEY_TEST_MAX_STAKE)


def validate_unit_size(unit_size: Any, currency: Any = CURRENCY) -> dict:
    """A production unit size is valid only when finite, > 0, and in ZAR.

    No rounding is applied here — a value the operator saved is used verbatim;
    the directive forbids silently rounding the configured unit size.
    """
    c = (str(currency).upper() if currency is not None else None)
    if c != CURRENCY:
        return {"valid": False, "reason": R_CURRENCY, "unit_size": None}
    v = _f(unit_size)
    if v is None:
        return {"valid": False, "reason": R_UNIT_SIZE_INVALID,
                "unit_size": None}
    if v <= 0:
        return {"valid": False, "reason": R_UNIT_SIZE_INVALID,
                "unit_size": None}
    return {"valid": True, "reason": None, "unit_size": v}


def resolve_stake(mode: Any, *, unit_size: Any = None,
                  currency: Any = CURRENCY,
                  authorized: bool = False) -> dict:
    """The ONE authority for the stake a mode permits.

    Returns ``{"ok": bool, "mode", "stake", "currency", "reason"}``.

      * ZERO_STAKE           -> ok, stake 0.0 (simulation; no real money)
      * REAL_MONEY_TEST      -> ok only when ``authorized`` and the test stake
                                equals the exact R2.00 ZAR invariant
      * PRODUCTION_AUTO_BET  -> ok only with a valid configured unit size
      * anything else        -> REJECT (fail closed)
    """
    if mode not in MODES:
        return {"ok": False, "mode": mode, "stake": None, "currency": currency,
                "reason": R_MODE_INVALID}
    if mode == ZERO_STAKE:
        return {"ok": True, "mode": mode, "stake": 0.0, "currency": CURRENCY,
                "reason": None}
    if mode == REAL_MONEY_TEST:
        if not authorized:
            return {"ok": False, "mode": mode, "stake": None,
                    "currency": currency, "reason": R_AUTH}
        if not is_authorized_test_stake(REAL_MONEY_TEST_STAKE, currency):
            return {"ok": False, "mode": mode, "stake": None,
                    "currency": currency, "reason": R_TEST_STAKE}
        return {"ok": True, "mode": mode, "stake": REAL_MONEY_TEST_STAKE,
                "currency": CURRENCY, "reason": None}
    # PRODUCTION_AUTO_BET — the user-configured unit size, never a default
    if not authorized:
        return {"ok": False, "mode": mode, "stake": None,
                "currency": currency, "reason": R_AUTH}
    check = validate_unit_size(unit_size, currency)
    if not check["valid"]:
        return {"ok": False, "mode": mode, "stake": None,
                "currency": currency,
                "reason": (R_UNIT_SIZE_MISSING if unit_size is None
                           else check["reason"])}
    return {"ok": True, "mode": mode, "stake": check["unit_size"],
            "currency": CURRENCY, "reason": None}


def authorization_proof(mode: Any, *, authorized: bool,
                        authorized_by: Optional[str] = None) -> dict:
    """The explicit authorization proof for a real-money mode.

    A command carries this proof so an audit can reconstruct WHY it was allowed.
    ZERO_STAKE needs no authorization; the two real-money modes require
    ``authorized is True`` AND a non-empty ``authorized_by`` actor.
    """
    if mode == ZERO_STAKE:
        return {"required": False, "satisfied": True, "reason": None}
    actor = (authorized_by or "").strip()
    satisfied = bool(authorized) and bool(actor)
    return {"required": True, "satisfied": satisfied,
            "authorized_by": actor or None,
            "reason": None if satisfied else R_AUTH}
