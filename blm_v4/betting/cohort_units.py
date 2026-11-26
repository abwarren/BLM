"""Per-cohort unit override — shared helper for the two betting producers.

A qualification block may carry ``cohort_units`` from the strongest matching
overlay.  This helper returns a COPY of the config with that unit size applied;
the process-wide config and every risk limit are untouched.  No override →
the original config object is returned unchanged (incumbent behaviour).
"""
from __future__ import annotations

import dataclasses
import math
from typing import Any, Optional


def _sel_config_for(cfg, qualification: Optional[dict]) -> Any:
    units = None
    if isinstance(qualification, dict):
        value = qualification.get("cohort_units")
        try:
            candidate = float(value)
            if math.isfinite(candidate) and candidate > 0:
                units = candidate
        except (TypeError, ValueError):
            units = None
    if units is None:
        return cfg
    try:
        return dataclasses.replace(cfg, stake_units=units)
    except Exception:
        return cfg                       # never widen on a copy failure
