"""The NAMED trade cohorts (TC-*) — one registry, one resolver.

Why this module: the trade gate's legs live in ``blm_v4.trade_window`` (which keeps
a frozen prefix and may only be appended to), so the NAMES live here instead — a
thin, dependency-free layer over the same constants.  Every consumer (the served
payload, the dashboard badge, the cohort pulls, the tests) resolves a game's cohort
through :func:`resolve_cohort`, so a name can never drift from the legs it claims.

A game belongs to EVERY cohort whose legs hold.  ``code`` reports the ONE it is
classified as — the MAXIMUM first: of the cohorts it clears, the one demanding
the highest thresholds (pace, then shortfall, then price) wins.  ``codes`` lists
all of them in that same order, strictest first.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional

from .trade_window import (TRADE_EXCLUDED_COMPETITIONS, TRADE_MAX_PROGRESS_PCT,
                           TRADE_MIN_PROGRESS_PCT, TRADE_MIN_SCORED_POINTS)

#: Ordered by STRICTNESS, derived from the legs themselves — never hand-written.
#: ``code`` = the first cohort here whose legs all hold, i.e. the MAXIMUM: the
#: cohort demanding the highest pace, then the biggest shortfall, then the
#: highest price.  TC-D and TC-C+ are side routes (shortfall / price) rather than
#: rungs on the pace ladder, so a hand-written order ranked them below cohorts
#: they are strictly stricter than (a price-1.85, shortfall-0.30 game came back
#: as TC-C instead of TC-C+).
COHORTS: dict[str, dict[str, Any]] = {
    "TC-W": dict(name="WIDE", pace=1.00, shortfall=-0.50, price=1.60,
                 blurb="the live gate — every game the engine will take"),
    "TC-C": dict(name="CORE", pace=1.04, shortfall=-0.50, price=1.60,
                 blurb="the old alert-era pace bar"),
    "TC-S": dict(name="SHARP", pace=1.08, shortfall=-0.50, price=1.60,
                 blurb="game needs 8% more than league-average pace"),
    "TC-E": dict(name="EDGE", pace=1.12, shortfall=-0.50, price=1.60,
                 blurb="12% over league pace"),
    "TC-X": dict(name="ELITE", pace=1.16, shortfall=-0.50, price=1.60,
                 blurb="16% over league pace — tightest pace rung"),
    "TC-D": dict(name="DEEP", pace=1.00, shortfall=0.50, price=1.60,
                 blurb="shortfall route: game is half a point/min under its required rate"),
    "TC-C+": dict(name="CORE+", pace=1.04, shortfall=0.25, price=1.80,
                  blurb="CORE pace plus shortfall plus a price floor"),
}

#: The CLASSIFICATION order: strictest first, derived from the legs so it can
#: never drift from them.  ``code`` = first entry whose legs all hold = MAXIMUM.
COHORT_ORDER = tuple(sorted(
    COHORTS,
    key=lambda c: (-COHORTS[c]["pace"], -COHORTS[c]["shortfall"],
                   -COHORTS[c]["price"]),
))


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def cohort_legs(code: str) -> dict[str, Any]:
    """The full leg set of one cohort (shared band + points floor included)."""
    c = COHORTS[code]
    return dict(progress_min=TRADE_MIN_PROGRESS_PCT, progress_max=TRADE_MAX_PROGRESS_PCT,
                total_points_min=TRADE_MIN_SCORED_POINTS, pace_ratio=c["pace"],
                shortfall=c["shortfall"], under_price=c["price"],
                name=c["name"], blurb=c["blurb"])


def resolve_cohort(*, progress_pct, total_points, required_pace, avg_pace, actual_pace,
                   under_price, competition_slug=None) -> dict[str, Any]:
    """Which named cohort(s) this game satisfies.

    Fails CLOSED: an unprovable input (missing / non-finite) refuses that leg, so a
    game with no pace can never be named.  An absent competition slug is NOT a
    refusal (the exclusion leg is fail-OPEN, exactly like ``trade_competition_reason``).

    Returns ``{"code": tightest-or-None, "name", "codes": [...], "excluded": bool}``.
    """
    excluded = bool(competition_slug) and str(competition_slug) in TRADE_EXCLUDED_COMPETITIONS
    prog, pts = _num(progress_pct), _num(total_points)
    req, ap, act, price = _num(required_pace), _num(avg_pace), _num(actual_pace), _num(under_price)
    out = {"code": None, "name": None, "codes": [], "excluded": excluded}
    if excluded or prog is None or pts is None or req is None or act is None or price is None:
        return out
    if not (TRADE_MIN_PROGRESS_PCT <= prog <= TRADE_MAX_PROGRESS_PCT):
        return out
    if pts < TRADE_MIN_SCORED_POINTS:
        return out
    ratio = (req / ap) if (ap is not None and ap > 0) else None
    for code in COHORT_ORDER:
        c = COHORTS[code]
        if ratio is None or ratio <= c["pace"]:
            continue
        if (req - act) < c["shortfall"]:
            continue
        if price < c["price"]:
            continue
        out["codes"].append(code)
    if out["codes"]:
        out["code"] = out["codes"][0]
        out["name"] = COHORTS[out["code"]]["name"]
    return out


def label(code: Optional[str]) -> str:
    """``'TC-X · ELITE'`` for a known code, ``'—'`` otherwise (UI helper)."""
    if not code or code not in COHORTS:
        return "\u2014"
    return f"{code} \u00b7 {COHORTS[code]['name']}"
