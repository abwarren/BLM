"""Named trade cohorts (TC-*) — frozen spec, deterministic evaluator, scorecards.

Public surface:
    spec       — the frozen, versioned cohort rules + precedence (data only)
    evaluate   — the ONE pure decision function (never I/O)
    metrics    — Wilson interval, break-even, outcome counting, UNDER settlement

This package is analytic only: it does not read the database, change alerts,
touch Auto-Bet, or perform network calls.
"""
from __future__ import annotations

from .evaluator import (Evaluation, STATUS_QUALIFIED, STATUS_NOT_QUALIFIED,
                        STATUS_UNKNOWN, evaluate, normalize_progress)
from .spec import (COHORT_RULES, PRECEDENCE, RULE_EFFECTIVE_AT, RULE_VERSION,
                   spec_manifest)

__all__ = [
    "Evaluation", "evaluate", "normalize_progress",
    "STATUS_QUALIFIED", "STATUS_NOT_QUALIFIED", "STATUS_UNKNOWN",
    "COHORT_RULES", "PRECEDENCE", "RULE_VERSION", "RULE_EFFECTIVE_AT",
    "spec_manifest",
]
