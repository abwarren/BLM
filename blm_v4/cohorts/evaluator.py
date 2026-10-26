"""The deterministic trade-cohort evaluator — ONE pure function, no I/O.

Every surface (API, ledger, rescore, dashboard badge, tests) calls
:func:`evaluate`.  Given the same inputs it returns the same answer, and the
answer names every cohort whose FULL rule holds plus the reporting primary
(``tightest_cohort``) from the frozen precedence in :mod:`blm_v4.cohorts.spec`.

Fail-closed contract (strategy doc §3.1/§3.3): a missing, non-finite, negative or
inconsistent operand is ``UNKNOWN`` with a reason code — it can never become a
passing leg.  A value that is valid but out of range is ``NOT_QUALIFIED``;
``UNKNOWN`` and ``NOT_QUALIFIED`` are distinct and must never be conflated.

No database, network, filesystem, clock or environment access lives here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from . import spec

# ── Status values (stable strings; the API and UI match on these) ──────────
STATUS_QUALIFIED = "QUALIFIED"
STATUS_NOT_QUALIFIED = "NOT_QUALIFIED"
STATUS_UNKNOWN = "UNKNOWN"

# ── Reason codes (stable; never renumbered, only appended) ────────────────
R_MISSING_PROGRESS = "missing_progress"
R_INVALID_PROGRESS = "invalid_progress"
R_PROGRESS_OUT_OF_BAND = "progress_out_of_band"
R_MISSING_POINTS = "missing_points"
R_INVALID_POINTS = "invalid_points"
R_POINTS_BELOW_MIN = "points_below_min"
R_MISSING_COMPETITION = "missing_competition"
R_EXCLUDED_COMPETITION = "excluded_competition"
R_MISSING_REQUIRED_PACE = "missing_required_pace"
R_INVALID_REQUIRED_PACE = "invalid_required_pace"
R_MISSING_ACTUAL_PACE = "missing_actual_pace"
R_INVALID_ACTUAL_PACE = "invalid_actual_pace"
R_MISSING_LEAGUE_PACE = "missing_league_pace"
R_INVALID_LEAGUE_PACE = "invalid_league_pace"
R_MISSING_PRICE = "missing_price"
R_INVALID_PRICE = "invalid_price"
R_LINE_MOVE_ENABLED = "line_move_enabled"
R_LINE_MOVE_UNKNOWN = "line_move_unknown"


def _finite(value: Any) -> Optional[float]:
    """The value as a finite float, else None.  Booleans are never numbers."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def normalize_progress(value: Any, units: str = "pct") -> Optional[float]:
    """Normalize progress ONCE, at the input boundary.

    ``units='pct'`` (default) expects 0–100 and passes it through;
    ``units='fraction'`` expects 0–1 and multiplies by 100.  Anything else, and
    any non-finite input, returns None (the caller then reports UNKNOWN) — a
    fraction and a percentage must never be silently mixed.
    """
    f = _finite(value)
    if f is None:
        return None
    if units == "pct":
        return f
    if units == "fraction":
        return f * 100.0
    return None


@dataclass(frozen=True)
class Evaluation:
    """The complete, auditable answer for one firing/checkpoint observation."""

    status: str
    rule_version: str
    qualifying_cohorts: tuple[str, ...] = ()
    tightest_cohort: Optional[str] = None
    reason_codes: tuple[str, ...] = ()
    progress_pct: Optional[float] = None
    scored_points: Optional[int] = None
    required_pts_per_min: Optional[float] = None
    actual_pts_per_min: Optional[float] = None
    league_avg_pts_per_min: Optional[float] = None
    pace_ratio: Optional[float] = None
    shortfall: Optional[float] = None
    under_price: Optional[float] = None
    competition_slug: Optional[str] = None
    line_move_enabled: Optional[bool] = None
    #: per-cohort leg verdicts, for drill-down and boundary tests
    cohort_checks: Mapping[str, Mapping[str, bool]] = field(default_factory=dict)

    @property
    def qualified(self) -> bool:
        return self.status == STATUS_QUALIFIED

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "rule_version": self.rule_version,
            "qualifying_cohorts": list(self.qualifying_cohorts),
            "tightest_cohort": self.tightest_cohort,
            "reason_codes": list(self.reason_codes),
            "progress_pct": self.progress_pct,
            "scored_points": self.scored_points,
            "required_pts_per_min": self.required_pts_per_min,
            "actual_pts_per_min": self.actual_pts_per_min,
            "league_avg_pts_per_min": self.league_avg_pts_per_min,
            "pace_ratio": self.pace_ratio,
            "shortfall": self.shortfall,
            "under_price": self.under_price,
            "competition_slug": self.competition_slug,
            "line_move_enabled": self.line_move_enabled,
            "cohort_checks": {k: dict(v) for k, v in self.cohort_checks.items()},
        }


def _unknown(code: str, **echo) -> Evaluation:
    return Evaluation(status=STATUS_UNKNOWN, rule_version=spec.RULE_VERSION,
                      reason_codes=(code,), **echo)


def evaluate(inputs: Mapping[str, Any], *, rule_version: str = spec.RULE_VERSION,
             progress_units: str = "pct") -> Evaluation:
    """Evaluate ONE observation against the frozen cohort spec.

    ``inputs`` keys (all optional — missing means UNKNOWN, never a pass):
        progress_pct        progress in percent (or ``progress`` + progress_units)
        scored_points       home_score + away_score at the observation
        competition_slug    the taxonomy slug (NOT a display name)
        required_pts_per_min / actual_pts_per_min / league_avg_pts_per_min
        under_price         decimal UNDER price at the observation
        line_move_enabled   True / False / None (None = unknown)

    An unknown ``rule_version`` raises ValueError: evaluating under a version we
    do not have would silently invent semantics.
    """
    if rule_version != spec.RULE_VERSION:
        raise ValueError(f"unknown rule_version {rule_version!r} "
                         f"(this build implements {spec.RULE_VERSION!r})")

    progress = inputs.get("progress_pct", inputs.get("progress"))
    progress = normalize_progress(progress, progress_units)
    points_raw = inputs.get("scored_points")
    points = _finite(points_raw)
    slug = inputs.get("competition_slug")
    req = _finite(inputs.get("required_pts_per_min"))
    act = _finite(inputs.get("actual_pts_per_min"))
    league = _finite(inputs.get("league_avg_pts_per_min"))
    price = _finite(inputs.get("under_price"))
    line_move = inputs.get("line_move_enabled")

    echo = dict(
        progress_pct=progress,
        scored_points=int(points) if points is not None else None,
        required_pts_per_min=req,
        actual_pts_per_min=act,
        league_avg_pts_per_min=league,
        under_price=price,
        competition_slug=slug if isinstance(slug, str) else None,
        line_move_enabled=line_move if isinstance(line_move, bool) else None,
    )

    # ── 1. structural validity: anything unprovable is UNKNOWN, fail closed ──
    if progress is None:
        return _unknown(R_MISSING_PROGRESS if inputs.get("progress_pct") is None
                        and inputs.get("progress") is None else R_INVALID_PROGRESS, **echo)
    if points is None:
        return _unknown(R_MISSING_POINTS if points_raw is None else R_INVALID_POINTS, **echo)
    if not isinstance(slug, str) or not slug.strip():
        return _unknown(R_MISSING_COMPETITION, **echo)
    slug_l = slug.strip().lower()
    if req is None:
        return _unknown(R_MISSING_REQUIRED_PACE, **echo)
    if act is None:
        return _unknown(R_MISSING_ACTUAL_PACE, **echo)
    if league is None:
        return _unknown(R_MISSING_LEAGUE_PACE, **echo)
    if price is None:
        return _unknown(R_MISSING_PRICE, **echo)
    if req < 0 or act < 0:
        return _unknown(R_INVALID_REQUIRED_PACE if req < 0 else R_INVALID_ACTUAL_PACE, **echo)
    if league <= 0:
        return _unknown(R_INVALID_LEAGUE_PACE, **echo)
    if price <= 0:
        return _unknown(R_INVALID_PRICE, **echo)
    if line_move is None:
        return _unknown(R_LINE_MOVE_UNKNOWN, **echo)
    if not isinstance(line_move, bool):
        return _unknown(R_LINE_MOVE_UNKNOWN, **echo)

    # ── 2. derived metrics at FULL stored precision (rounding is display-only) ─
    pace_ratio = req / league
    shortfall = req - act
    echo.update(pace_ratio=pace_ratio, shortfall=shortfall)

    # ── 3. common gates: valid values that simply do not pass ────────────────
    reasons: list[str] = []
    if line_move is not False or spec.LINE_MOVE_REQUIRED is not False:
        reasons.append(R_LINE_MOVE_ENABLED)
    if not (spec.MIN_PROGRESS_PCT <= progress <= spec.MAX_PROGRESS_PCT):
        reasons.append(R_PROGRESS_OUT_OF_BAND)
    if points < spec.MIN_SCORED_POINTS:
        reasons.append(R_POINTS_BELOW_MIN)
    if slug_l in spec.EXCLUDED_COMPETITIONS:
        reasons.append(R_EXCLUDED_COMPETITION)
    if reasons:
        return Evaluation(status=STATUS_NOT_QUALIFIED, rule_version=rule_version,
                          reason_codes=tuple(reasons),
                          qualifying_cohorts=(), tightest_cohort=None, **echo)

    # ── 4. every cohort's full rule (all matches kept) ───────────────────────
    checks: dict[str, Mapping[str, bool]] = {}
    qualifying: list[str] = []
    for code in spec.QUALIFYING_ORDER:
        rule = spec.COHORT_RULES[code]
        pace_ok = pace_ratio > rule.pace_ratio          # STRICT
        short_ok = shortfall >= rule.shortfall          # INCLUSIVE
        price_ok = price >= rule.min_under_price        # INCLUSIVE
        checks[code] = {"pace": pace_ok, "shortfall": short_ok, "price": price_ok}
        if pace_ok and short_ok and price_ok:
            qualifying.append(code)

    if not qualifying:
        return Evaluation(status=STATUS_NOT_QUALIFIED, rule_version=rule_version,
                          reason_codes=tuple(reasons), qualifying_cohorts=(),
                          tightest_cohort=None, cohort_checks=checks, **echo)

    # Precedence is NAMED data (spec.PRECEDENCE), never iteration/hash order.
    tightest = next(c for c in spec.PRECEDENCE if c in qualifying)
    return Evaluation(status=STATUS_QUALIFIED, rule_version=rule_version,
                      qualifying_cohorts=tuple(qualifying), tightest_cohort=tightest,
                      reason_codes=tuple(reasons), cohort_checks=checks, **echo)
