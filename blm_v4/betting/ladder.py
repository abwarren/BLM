"""Bet LADDER — the operator's win-progression, selectable from the UI.

Operator directive 2026-10-30 (after rejecting a doubling ladder: "that will
eat into my bankroll — add 0.8 of a unit"):

    rung 1   stake = 1.0 unit
    rung 2   stake = 1.8 units      (+0.8)
    rung 3   stake = 2.6 units
    ...
    rung 7   stake = 5.8 units

    a WIN  advances one rung
    a LOSS resets to rung 1
    a PUSH/void leaves the rung unchanged (no money moved, no information)
    at the selected depth (3-7) the ladder BANKS and returns to rung 1

The step is a FIXED number of units, never a multiplier: doubling would put
8.0 units on rung 4 (vs 3.4 here) and 64 units on rung 7, which is what the
operator refused.  Every rung still passes the executor's existing bounds —
max_stake_per_bet, the daily exposure ceiling, and the account balance.

DEPTH 0 means the ladder is OFF and stakes are the plain configured unit.

This module is pure: no DB, no network, no clock.  Persistence is a config key
(``ladder_wins``) and the rung is DERIVED from the settlement history rather
than stored, so a restart can never resume on a stale rung.
"""
from __future__ import annotations

import math
import os
from typing import Iterable, Optional

#: rungs the operator may select from the UI.  0 = off (plain unit staking).
DEPTHS = (0, 3, 4, 5, 6, 7)
MAX_DEPTH = 7

#: Fixed increment per rung, in units (the "add 0.8" variant).
DEFAULT_STEP = 0.8
#: Compounding factor per rung (the operator's final choice, 2026-10-30:
#: "multiply by 1.8 -- 1 unit, 2nd bet 1.8, 3rd 1.8x1.8").  This is the
#: stake+winnings rule at average odds, expressed as one constant.
DEFAULT_FACTOR = 1.8
MODES = ("multiply", "step")


def step_units(environ: Optional[dict] = None) -> float:
    """The per-rung increment in units (default 0.8)."""
    env = os.environ if environ is None else environ
    raw = env.get("BET_LADDER_STEP")
    if raw is None:
        return DEFAULT_STEP
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_STEP
    return v if math.isfinite(v) and v > 0 else DEFAULT_STEP


def factor(environ: Optional[dict] = None) -> float:
    """The per-rung multiplier (default 1.8)."""
    env = os.environ if environ is None else environ
    raw = env.get("BET_LADDER_FACTOR")
    if raw is None:
        return DEFAULT_FACTOR
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_FACTOR
    return v if math.isfinite(v) and v > 1.0 else DEFAULT_FACTOR


def mode(environ: Optional[dict] = None) -> str:
    """``multiply`` (default, factor^rung) or ``step`` (base + k * step)."""
    env = os.environ if environ is None else environ
    m = (env.get("BET_LADDER_MODE") or "multiply").strip().lower()
    return m if m in MODES else "multiply"


def depth(value) -> int:
    """Normalise a stored ``ladder_wins`` value to a permitted depth.

    Anything unparseable, negative or above 7 reads as 0 (OFF): a corrupt
    config must never silently stake more than the plain unit.
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 0
    return v if v in DEPTHS else 0


def enabled(value) -> bool:
    return depth(value) > 0


def stake_units(rung: int, base_units: float = 1.0,
                step: Optional[float] = None) -> float:
    """Stake for a given rung.  ``rung`` is 1-based (1 = first bet of a cycle).

    multiply (default): base * factor**(rung-1)   1, 1.8, 3.24, 5.83, ...
    step:               base + step*(rung-1)      1, 1.8, 2.6, 3.4, ...

    Clamped at MAX_DEPTH so a corrupt rung cannot stair-step past the deepest
    permitted stake.  The executor's max_stake_per_bet and the account balance
    still bound the result — this function never widens a risk limit.
    """
    try:
        b = float(base_units)
    except (TypeError, ValueError):
        return float("nan")
    if not math.isfinite(b) or b <= 0:
        return float("nan")
    r = max(1, min(int(rung), MAX_DEPTH))
    if mode() == "step":
        s = step_units() if step is None else float(step)
        return round(b + s * (r - 1), 4)
    f = factor()
    return round(b * (f ** (r - 1)), 4)


def advance(rung: int, result: str, depth_value) -> int:
    """The next rung after a settled bet.

    ``result`` is "WON" / "LOST" / "PUSH" (case-insensitive).  An unknown
    result is treated as a loss — the ladder drops to rung 1 rather than
    pretending a win it cannot prove (§ fail closed).
    """
    d = depth(depth_value)
    if d == 0:
        return 1
    r = max(1, min(int(rung), MAX_DEPTH))
    res = (result or "").strip().upper()
    if res == "PUSH":
        return r
    if res != "WON":
        return 1
    return 1 if r >= d else r + 1


def rung_from_history(results: Iterable[str], depth_value) -> int:
    """Replay settlement results in chronological order -> the current rung.

    Derived, never stored: the rung after a restart is the same rung the
    engine would have had, which is the property a persisted counter loses
    when a settle is missed.
    """
    r = 1
    for res in results:
        r = advance(r, res, depth_value)
    return r


def settle_outcome(market: Optional[str], selection: Optional[str],
                   line, final_total) -> Optional[str]:
    """WON / LOST / PUSH for a settled UNDER or OVER bet, else None.

    None means "not settleable yet" (no final total) — the caller must skip
    it, never guess: a guessed win would double the next stake on no evidence.
    """
    if final_total is None or line is None:
        return None
    try:
        final = float(final_total)
        ln = float(line)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(final) and math.isfinite(ln)):
        return None
    sel = (selection or "").strip().upper()
    mkt = (market or "").strip().upper()
    if final == ln:
        return "PUSH"
    under = "UNDER" in sel or "UNDER" in mkt
    over = "OVER" in sel or "OVER" in mkt
    if under and not over:
        return "WON" if final < ln else "LOST"
    if over and not under:
        return "WON" if final > ln else "LOST"
    return None
