"""The frozen trade-cohort specification (TC-*) — ONE source of truth.

Why this module exists: the legs of a cohort were previously spread across the
live trade gate (``blm_v4.trade_window``), ad-hoc research scripts and pull tools,
so a "TC-X" number on one surface could not be proved to mean the same thing on
another.  This package freezes the rules as DATA with a version tag, and the
evaluator (``blm_v4.cohorts.evaluator``) is the only code allowed to decide
membership.

Rules are analytics-only: nothing here reads or writes a database, changes an
alert, touches Auto-Bet, or reaches the network.  The live gate keeps its own
constants; this spec is what the dashboards, rescores and ledgers report.

Spec version history
--------------------
tc-1.0.0  2026-10-26  initial freeze of the seven named cohorts defined in
                      docs/TRADE_COHORT_REFACTOR_IMPLEMENTATION_STRATEGY.md §3.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

#: Bump ONLY with a new effective date + a new entry above. Every evaluation and
#: every derived row carries it, so historical results can never be silently
#: relabelled under a newer rule set.
RULE_VERSION = "tc-1.0.0"
RULE_EFFECTIVE_AT = "2026-10-26T00:00:00Z"

#: Inclusive progress band (percent, 0–100).
MIN_PROGRESS_PCT = 75.0
MAX_PROGRESS_PCT = 92.0

#: Inclusive scored-points floor at the firing row (home + away).
MIN_SCORED_POINTS = 70

#: Competitions excluded from every cohort — Cyber 2K26 is a simulated feed and a
#: different statistical population.  A slug (never a display name) so a rename
#: of the display label cannot silently re-admit it.
EXCLUDED_COMPETITIONS = ("cyber-basketball-2k26-matches",)

#: The line-movement leg is OFF for this cohort model.  The evaluator requires the
#: caller to state it explicitly: False passes, True fails, None is UNKNOWN.
LINE_MOVE_REQUIRED = False


@dataclass(frozen=True)
class CohortRule:
    """One named cohort. All three legs are ANDed with the common gates."""

    code: str
    name: str
    pace_ratio: float          # STRICT  > this value
    shortfall: float           # INCLUSIVE >= this value
    min_under_price: float     # INCLUSIVE >= this value
    blurb: str = ""


#: The seven cohorts, exactly as frozen in the strategy doc §3.2.
COHORT_RULES: Mapping[str, CohortRule] = {
    rule.code: rule
    for rule in (
        CohortRule("TC-X", "ELITE", 1.16, -0.50, 1.60, "16% over league pace — tightest pace rung"),
        CohortRule("TC-E", "EDGE", 1.12, -0.50, 1.60, "12% over league pace"),
        CohortRule("TC-S", "SHARP", 1.08, -0.50, 1.60, "8% over league pace"),
        CohortRule("TC-C", "CORE", 1.04, -0.50, 1.60, "4% over league pace"),
        CohortRule("TC-W", "WIDE", 1.00, -0.50, 1.60, "pace above league average — the live gate"),
        CohortRule("TC-D", "DEEP", 1.00, 0.50, 1.60, "shortfall route: half a point/min under its required rate"),
        CohortRule("TC-C+", "CORE+", 1.04, 0.25, 1.80, "CORE pace plus shortfall plus a higher price floor"),
    )
}

#: Reporting precedence for ``tightest_cohort`` — a NAMED, VERSIONED policy, not
#: iteration order.  Rationale: walk the pace ladder first (X→E→S→C), then the
#: two THINNER side routes (C+ adds a price floor and a shortfall floor on top of
#: CORE's pace; D swaps the pace leg for a large shortfall), then WIDE.
#:
#: This is explicitly a REPORTING precedence: it is not a claim that every earlier
#: entry mathematically dominates every later one (C+ and D are partially
#: incomparable with each other and with C).  ``qualifying_cohorts`` — which keeps
#: every match — remains the authoritative answer.
PRECEDENCE: tuple[str, ...] = ("TC-X", "TC-E", "TC-S", "TC-C+", "TC-C", "TC-D", "TC-W")

#: Stable, documented order for ``qualifying_cohorts`` (same as precedence; kept
#: separate so a future precedence change cannot silently reorder ledger output).
QUALIFYING_ORDER: tuple[str, ...] = PRECEDENCE


def cohort(code: str) -> CohortRule:
    """The rule for ``code`` (KeyError on an unknown code — never a default)."""
    return COHORT_RULES[code]


def all_codes() -> tuple[str, ...]:
    return tuple(COHORT_RULES)


def spec_manifest() -> dict:
    """Machine-readable manifest of the frozen spec (served by the API)."""
    return {
        "rule_version": RULE_VERSION,
        "rule_effective_at": RULE_EFFECTIVE_AT,
        "common_gates": {
            "min_progress_pct": MIN_PROGRESS_PCT,
            "max_progress_pct": MAX_PROGRESS_PCT,
            "min_scored_points": MIN_SCORED_POINTS,
            "excluded_competitions": list(EXCLUDED_COMPETITIONS),
            "line_move_required": LINE_MOVE_REQUIRED,
        },
        "cohorts": [
            {
                "code": r.code, "name": r.name,
                "pace_ratio_gt": r.pace_ratio,
                "shortfall_gte": r.shortfall,
                "min_under_price_gte": r.min_under_price,
                "blurb": r.blurb,
            }
            for r in (COHORT_RULES[c] for c in QUALIFYING_ORDER)
        ],
        "precedence": list(PRECEDENCE),
    }
