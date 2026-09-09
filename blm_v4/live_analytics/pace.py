"""Pace formulas — the descriptive core of the live analytics architecture.

Pure arithmetic on OBSERVED values only.  No fair value, no projection,
no probability, no edge, no betting semantics.  Every function returns
None when an input is missing or a division is undefined — a boundary is
an honest None, never an invented number.
"""
from __future__ import annotations

import re

from typing import Optional


def _num(x) -> Optional[float]:
    return float(x) if x is not None else None


def actual_pace(actual_score: Optional[float],
                elapsed_minutes: Optional[float]) -> Optional[float]:
    """Points per minute actually produced so far: score / elapsed_minutes.

    elapsed = 0 → None (nothing has happened yet; the pace is undefined,
    not zero and not infinite).
    """
    score = _num(actual_score)
    elapsed = _num(elapsed_minutes)
    if score is None or elapsed is None or elapsed <= 0:
        return None
    return round(score / elapsed, 4)


def remaining_minutes(total_minutes: Optional[float],
                      elapsed_minutes: Optional[float]) -> Optional[float]:
    """Game minutes left, floored at 0 (clock can over-run regulation)."""
    total = _num(total_minutes)
    elapsed = _num(elapsed_minutes)
    if total is None or elapsed is None:
        return None
    return max(0.0, round(total - elapsed, 4))


def required_pace(live_line: Optional[float], actual_score: Optional[float],
                  remaining_min: Optional[float]) -> Optional[float]:
    """Points/minute needed from now on to reach the live line:
    (live_line − actual_score) / remaining_minutes.

    remaining = 0 → None (no time left to score; the raw line − score
    difference is the honest remaining-state measure instead).
    A negative result is VALID — the score is already above the line.
    """
    line = _num(live_line)
    score = _num(actual_score)
    rem = _num(remaining_min)
    if line is None or score is None or rem is None or rem <= 0:
        return None
    return round((line - score) / rem, 4)


def pace_gap(required: Optional[float], actual: Optional[float]) -> Optional[float]:
    """required − actual.  Positive → the game must speed up to reach the
    line; negative → the game is outpacing the line.  Descriptive only."""
    req = _num(required)
    act = _num(actual)
    if req is None or act is None:
        return None
    return round(req - act, 4)


def line_score_gap(live_line: Optional[float],
                   actual_score: Optional[float]) -> Optional[float]:
    """LIVE LINE − ACTUAL SCORE: points the market is pricing above the
    score already on the board (the directive's primary relationship)."""
    line = _num(live_line)
    score = _num(actual_score)
    if line is None or score is None:
        return None
    return round(line - score, 1)


def score_line_gap(actual_score: Optional[float],
                   live_line: Optional[float]) -> Optional[float]:
    """ACTUAL SCORE − LIVE LINE (the existing signed readout, kept in sync:
    score_line_gap == −line_score_gap whenever both are defined)."""
    line = _num(live_line)
    score = _num(actual_score)
    if line is None or score is None:
        return None
    return round(score - line, 1)


def period_bucket(period_label: Optional[str]) -> Optional[str]:
    """Canonical period bucket from the observation's period label.
    '1st Quarter' → 'Q1' … '4th Quarter' → 'Q4'.  Anything else
    (Half End, OT, unknown) → None: the population stays clean rather
    than silently lumping odd states into a quarter bucket."""
    if not period_label:
        return None
    m = re.match(r"^([1-4])", period_label.strip())
    return ("Q" + m.group(1)) if m else None


def benchmark_key(provider: Optional[str],
                  competition: Optional[str],
                  progress_pct: Optional[float],
                  period_label: Optional[str] = None) -> Optional[str]:
    """Historical population key — MANDATORY partition dimensions:
    (provider, competition, period, progress/state bucket).  Example:
    ('BETUAL', 'betual-nba', 62.0, '2nd Quarter')
        → 'BETUAL|betual-nba|Q2|P060'.

    BETUAL|betual-nba|Q2|P040 and BETUAL|betual-tbsl|Q2|P040 are two
    completely separate populations: NBA and TSBL never mix even though
    both come through the BETUAL provider.  Any unresolvable dimension →
    None (ineligible)."""
    if provider is None or competition is None or progress_pct is None:
        return None
    p = float(progress_pct)
    if p < 0 or p > 100:
        return None
    if period_label is None:
        return None
    qb = period_bucket(period_label)
    if qb is None:
        return None
    lo = int(p // 5) * 5
    return "%s|%s|%s|P%03d" % (str(provider), str(competition), qb, lo)
