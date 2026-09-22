"""Quarter-score chronological-consistency validation (directive
2026-09-22 §4, DATA COLLECTION ONLY).

Every check FLAGS an anomaly; NOTHING here rewrites, repairs or drops an
observation (§4: flag anomalies rather than silently correcting them; §5:
never overwrite valid historical observations).  The caller records the
anomaly and keeps the original row exactly as the source presented it.

All checks are pure functions of one observation plus optional prior
state — no database access, no production decision input, no alert
surface.  A missing value NEVER produces an anomaly: NULL is a legal
state (§5: NULL is preferable to an invented value), so every comparison
runs only over quarters the source actually exposed.
"""
from __future__ import annotations

from typing import Optional

# quarter-index helpers: (home, away) pairs per quarter index 1..4
_PAIR_KEYS = tuple(
    (f"q{i}_home_score", f"q{i}_away_score") for i in (1, 2, 3, 4)
)


def _pairs(obs: dict) -> dict[int, tuple[int, int]]:
    """Quarters present in the observation (both legs non-NULL)."""
    out: dict[int, tuple[int, int]] = {}
    for i, (hk, ak) in enumerate(_PAIR_KEYS, start=1):
        h, a = obs.get(hk), obs.get(ak)
        if h is not None and a is not None:
            out[i] = (int(h), int(a))
    return out


def check_quarter_nonnegative(obs: dict) -> list[dict]:
    """A quarter score cannot be negative when the source exposes it."""
    out = []
    for q, (h, a) in sorted(_pairs(obs).items()):
        if h < 0 or a < 0:
            out.append({
                "check": "quarter_nonnegative",
                "detail": f"Q{q} score is negative ({h}-{a})",
                "quarter": q,
            })
    return out


def check_cumulative_monotone(obs: dict) -> list[dict]:
    """Q(k+1) per-quarter points must be >= 0 — i.e. the cumulative score
    implied by consecutive quarters must never move backwards (§4:
    'quarter scores must not move backwards').  With per-quarter values
    this is the negativity of the per-quarter delta itself; the stricter
    cross-observation regression is checked by check_quarter_regression.
    """
    return []  # per-quarter values cannot regress against themselves


def check_quarter_within_total(obs: dict) -> list[dict]:
    """The full-game score must be >= the cumulative score of the
    quarters the source exposed (Q4/final >= Q1+Q2+Q3 cumulative, etc.).
    Only runs when BOTH the full-game score and the quarter are present.
    """
    out = []
    fh, fa = obs.get("home_score"), obs.get("away_score")
    if fh is None or fa is None:
        return out
    cum_h = cum_a = 0
    seen = False
    for q, (h, a) in sorted(_pairs(obs).items()):
        cum_h += h
        cum_a += a
        seen = True
    if not seen:
        return out
    # The full score must be at least the cumulative of the exposed
    # quarters ONLY when the current game state has reached them; an
    # observation made DURING Q3 legitimately shows a full score below
    # the final cumulative.  The defensible invariant: the last exposed
    # quarter's cumulative can never EXCEED the displayed full score
    # (you cannot have scored more quarters than the total shows).
    if cum_h > int(fh) or cum_a > int(fa):
        out.append({
            "check": "quarter_within_total",
            "detail": (
                f"cumulative exposed quarters ({cum_h}-{cum_a}) exceed "
                f"full-game score ({fh}-{fa})"),
        })
    return out


def check_quarter_regression(
    obs: dict, prev: Optional[dict],
) -> list[dict]:
    """A completed quarter cannot change after it completed (§4).

    ``prev`` is the game's previous VALIDATED observation (same game,
    earlier captured_at) with its own quarter pairs.  If a quarter was
    exposed before with value X and is now exposed with value Y != X,
    that is a regression — flagged, not corrected.
    """
    if not prev:
        return []
    out = []
    prev_pairs = _pairs(prev)
    cur_pairs = _pairs(obs)
    for q, (h, a) in sorted(cur_pairs.items()):
        old = prev_pairs.get(q)
        if old and old != (h, a):
            out.append({
                "check": "quarter_regression",
                "detail": (
                    f"Q{q} changed after completion: was {old[0]}-{old[1]}, "
                    f"now {h}-{a}"),
                "quarter": q,
            })
    return out


def validate_quarter_scores(
    obs: dict, prev: Optional[dict] = None,
) -> list[dict]:
    """Run all checks; return the anomaly list (possibly empty).

    A game's LAST recorded observation is the natural ``prev`` — the
    caller may keep a small per-game cache or read its most recent
    quarter_score_observations row.  Passing prev=None simply skips the
    regression check (e.g. first observation of a game after restart).
    """
    anomalies: list[dict] = []
    anomalies += check_quarter_nonnegative(obs)
    anomalies += check_cumulative_monotone(obs)
    anomalies += check_quarter_within_total(obs)
    anomalies += check_quarter_regression(obs, prev)
    return anomalies
