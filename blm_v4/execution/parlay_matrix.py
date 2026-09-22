"""BLM EXECUTION — PARLAY MATRIX ENGINE.

Generates every mathematical combination of the requested fold sizes
from the user's selections.  Pure combinatorics — no DOM, no I/O.

Duplicate legs are refused and conflicting positions on the same
event+market are flagged BEFORE anything is generated: the matrix
must be clean before execution can be requested.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from blm_v4.execution.selection_model import Selection

FOLD_LABELS = {
    1: "Singles", 2: "Doubles", 3: "Trebles",
    4: "4-Folds", 5: "5-Folds", 6: "6-Folds", 7: "7-Folds",
    8: "8-Folds",
}


@dataclass
class MatrixReport:
    """The generated matrix plus any validation findings."""

    combos: list[list[Selection]] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    fold_sizes: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.duplicates and not self.conflicts

    @property
    def combo_count(self) -> int:
        return len(self.combos)


def _counts(legs: list[Selection]) -> dict:
    out: dict[tuple, int] = {}
    for leg in legs:
        key = (leg.event, leg.market, leg.position)
        out[key] = out.get(key, 0) + 1
    return out


def build_matrix(legs: list[Selection],
                 fold_sizes: list[int]) -> MatrixReport:
    """Validate ``legs`` and generate all combinations of the requested
    fold sizes (a set — the same fold twice does not double-generate).

    Raises nothing for duplicates/conflicts: both are REPORTED so the UI
    can show them next to the offending rows; generation still proceeds
    from the unique leg set, but ``report.ok`` gates execution.
    """
    report = MatrixReport(fold_sizes=sorted(set(fold_sizes)))

    counts = _counts(legs)
    seen: set[tuple] = set()
    for leg in legs:
        key = (leg.event, leg.market, leg.position)
        if key in seen:
            report.duplicates.append(leg.to_dict())
        seen.add(key)
    # Conflicts: same event+market taken in BOTH directions.
    events: dict[tuple, set[str]] = {}
    for leg in legs:
        events.setdefault((leg.event, leg.market), set()).add(leg.position)
    for (event, market), positions in events.items():
        if len(positions) > 1:
            report.conflicts.append({
                "event": event, "market": market,
                "positions": sorted(positions)})

    unique = []
    seen.clear()
    for leg in legs:
        key = (leg.event, leg.market, leg.position)
        if key not in seen:
            seen.add(key)
            unique.append(leg)

    n = len(unique)
    for k in report.fold_sizes:
        if k < 1 or k > n:
            continue
        report.combos.extend(
            list(c) for c in combinations(unique, k))
    return report


def describe_combo(combo: list[Selection]) -> str:
    """Human-readable combination description for previews/logs."""
    return " + ".join(
        f"{leg.event} {leg.market}→{leg.position}" for leg in combo)
