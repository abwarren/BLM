"""Scorecard statistics for cohort reporting — pure functions.

Wilson 95 % interval for a binomial proportion (never the normal approximation:
cohort n's are small and rates sit near 0.8), plus the price-derived break-even
rate so a hit rate is never presented without its economic bar.

The denominator convention is fixed here and shared by every surface: PUSH and
VOID are EXCLUDED from the binary UNDER-vs-OVER rate and reported separately;
UNKNOWN/unsettled rows are never counted as losses.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

Z_95 = 1.959963984540054


def wilson_interval(successes: int, trials: int, z: float = Z_95) -> Optional[tuple[float, float]]:
    """Two-sided Wilson score interval for successes/trials (None if trials == 0)."""
    if trials <= 0:
        return None
    if successes < 0 or successes > trials:
        raise ValueError("successes must satisfy 0 <= successes <= trials")
    p = successes / trials
    denom = 1.0 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def break_even_rate(decimal_price: float) -> Optional[float]:
    """Win rate required just to break even at ``decimal_price`` (1/price)."""
    try:
        p = float(decimal_price)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p) or p <= 1.0:
        return None
    return 1.0 / p


def outcome_counts(outcomes: Iterable[str]) -> dict:
    """Count UNDER/OVER/PUSH/VOID/UNKNOWN with the fixed denominator rules.

    ``settled_denominator`` = UNDER + OVER (PUSH/VOID deliberately excluded).
    """
    counts = {"UNDER": 0, "OVER": 0, "PUSH": 0, "VOID": 0, "UNKNOWN": 0}
    for o in outcomes:
        key = (o or "UNKNOWN").upper()
        if key not in counts:
            key = "UNKNOWN"
        counts[key] += 1
    counts["settled"] = counts["UNDER"] + counts["OVER"]
    counts["total"] = sum(counts[k] for k in ("UNDER", "OVER", "PUSH", "VOID", "UNKNOWN"))
    rate = (counts["UNDER"] / counts["settled"]) if counts["settled"] else None
    counts["under_rate"] = rate
    counts["wilson95"] = wilson_interval(counts["UNDER"], counts["settled"])
    return counts


def settle_under(trigger_line: Optional[float], final_total: Optional[float]) -> str:
    """UNDER/OVER/PUSH for an UNDER bet, or UNKNOWN when either operand is missing.

    Equality is a PUSH (the line is a half-point grid in this market, so a push is
    rare but must be represented, not folded into a win).
    """
    try:
        line = float(trigger_line) if trigger_line is not None else None
        final = float(final_total) if final_total is not None else None
    except (TypeError, ValueError):
        return "UNKNOWN"
    if line is None or final is None or not math.isfinite(line) or not math.isfinite(final):
        return "UNKNOWN"
    if final < line:
        return "UNDER"
    if final > line:
        return "OVER"
    return "PUSH"
