"""BLM V4 — Prospective Confirmation Harness (read-only, frozen spec).

Audits the "walk-forward" terminology and enforces it:

  A chronological split within the EXISTING historical dataset is NOT
  prospective confirmation.  An observation is PROSPECTIVE (population C)
  only if BOTH:
    - its own checkpoint_timestamp  > CONFIRMATION_FREEZE_TIMESTAMP, and
    - its game has NO checkpoint at or before the freeze.
  Rows already present in the database at the freeze instant are
  HISTORICAL forever, regardless of which chronological block they fall
  in.  Only C can contribute to the confirmation verdict.

The specification (nominated cells, grids, criteria, verdict labels,
forbidden actions) lives in confirmation_spec.json, frozen at
CONFIRMATION_FREEZE_TIMESTAMP.  This module pins it by sha256
(FROZEN_SPEC_SHA256); any later edit to the spec file refuses to
evaluate — the hypothesis must not be altered while waiting.

No model / projection / scorecard / calibration / freshness code is
touched; load_rows and _ages are imported unchanged.  No thresholds are
optimized: every criterion is a frozen constant of the spec.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from blm_v4.calibration import load_rows, wilson_ci
from blm_v4.freshness_audit import _ages, age_band
from blm_v4.interaction_validation import (  # frozen grids
    RESIDUAL_MAGS, mag_band)

SPEC_PATH = Path(__file__).with_name("confirmation_spec.json")

# sha256 of confirmation_spec.json recorded AT FREEZE TIME.  If the file
# on disk ever hashes differently, the spec has been altered after the
# hypothesis was frozen and the harness must refuse to evaluate.
FROZEN_SPEC_SHA256 = ("e884faa7cd013f9ce6b72d4e963d97a6ed16236fb"
                      "c0be5fb47efb03aa67cc32f")

NOMINATED_CELLS = (
    {"side": "OVER", "mag": "5-10", "age": ">300s"},
    {"side": "OVER", "mag": "10-20", "age": ">300s"},
    {"side": "OVER", "mag": ">20", "age": ">300s"},
    {"side": "UNDER", "mag": "10-20", "age": "0-5s"},
)
STALE_OVER_CELLS = NOMINATED_CELLS[:3]
FRESH_UNDER_CELLS = NOMINATED_CELLS[3:]

# Frozen decision criteria (spec: confirmation_criteria_frozen).
CELL_MIN_N = 30
CELL_MIN_RATE = 0.60
CELL_MIN_WILSON_LO = 0.50   # strict lower bound of the Wilson 95% CI
PRIMARY_CLASSIFICATION = "BETUAL_NBA"


# ── spec integrity ──────────────────────────────────────────────────────
def spec_sha256(path: Path = SPEC_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    digest = spec_sha256(path)
    if digest != FROZEN_SPEC_SHA256:
        raise RuntimeError(
            "confirmation_spec.json hash mismatch: the frozen hypothesis "
            f"was altered after freeze (expected {FROZEN_SPEC_SHA256}, "
            f"found {digest}).  Evaluation refused.")
    return json.loads(path.read_text())


def freeze_ts(spec: Optional[dict[str, Any]] = None) -> datetime:
    spec = spec or load_spec()
    return datetime.fromisoformat(
        spec["CONFIRMATION_FREEZE_TIMESTAMP"].replace("Z", "+00:00"))


# ── A / B / C temporal partition ────────────────────────────────────────
def partition(rows: list[dict], cutoff: datetime) -> dict[str, list[dict]]:
    """Split eligible rows into A/B/C strictly by timestamps.

    C requires the row's checkpoint_timestamp > cutoff AND no checkpoint
    of the same game at or before cutoff (game-level gate: a game partly
    observed before the freeze is NOT prospective — its identity, line
    context and pace were already exposed).
    """
    first_cp: dict[str, datetime] = {}
    for r in rows:
        gid = r["source_game_id"]
        ts = _parse(r["checkpoint_timestamp"])
        if gid not in first_cp or ts < first_cp[gid]:
            first_cp[gid] = ts
    A: list[dict] = []
    C: list[dict] = []
    for r in rows:
        ts = _parse(r["checkpoint_timestamp"])
        if ts > cutoff and first_cp[r["source_game_id"]] > cutoff:
            C.append(r)
        else:
            A.append(r)
    return {"A": A, "B": A, "C": C}   # B re-scores the SAME historical data


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ── cell evaluation under the frozen criteria ───────────────────────────
def _cell_stats(rows: list[dict], side: str, mag: str, age: str,
                cutoff: Optional[datetime] = None) -> dict[str, Any]:
    # identical cell semantics to the frozen interaction grid: bands are
    # derived from the same frozen functions, never redefined here
    sel = [r for r in rows
           if r["side"] == side and mag_band(r["abs_raw"]) == mag
           and age_band(r["age_seconds"]) == age]
    if cutoff is not None:   # defence in depth: C rows only
        sel = [r for r in sel if _parse(r["checkpoint_timestamp"]) > cutoff]
    n = len(sel)
    wins = sum(1 for r in sel if r["y"] == 1)
    lo, hi = wilson_ci(wins, n)
    rate = (wins / n) if n else None
    return {"side": side, "mag": mag, "age": age,
            "n": n, "wins": wins, "losses": n - wins,
            "win_rate": round(rate, 3) if rate is not None else None,
            "wilson_lo": lo, "wilson_hi": hi,
            "games": len({r["source_game_id"] for r in sel})}


def _cell_pass(stats: dict[str, Any]) -> dict[str, Any]:
    n, rate, lo = stats["n"], stats["win_rate"], stats["wilson_lo"]
    checks = {"n_ge_30": n >= CELL_MIN_N,
              "rate_ge_060": rate is not None and rate >= CELL_MIN_RATE,
              "wilson_lo_gt_050": lo is not None and lo > CELL_MIN_WILSON_LO}
    return {"checks": checks, "passed": all(checks.values())}


# ── chronological revalidation of B (explicitly NOT prospective) ────────
def _b_blocks(rows: list[dict]) -> list[dict[str, Any]]:
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(_parse(r["checkpoint_timestamp"]).date().isoformat(),
                          []).append(r)
    out = []
    for day in sorted(by_day):
        rs = by_day[day]
        out.append({"block": day, "n": len(rs),
                    "games": len({r["source_game_id"] for r in rs}),
                    "raw_accuracy": round(
                        sum(1 for r in rs if r["y"] == 1) / len(rs), 3)})
    return out


# ── report ──────────────────────────────────────────────────────────────
def confirmation_report(db_path: str,
                        classification: Optional[str] = PRIMARY_CLASSIFICATION
                        ) -> dict[str, Any]:
    spec = load_spec()
    cutoff = freeze_ts(spec)
    rows = _ages(load_rows(db_path, classification))
    part = partition(rows, cutoff)

    a_cells = [_cell_stats(part["A"], **cell) | {"cell": dict(cell)}
               for cell in NOMINATED_CELLS]
    c_cells = [_cell_stats(part["C"], **cell) | {"cell": dict(cell)}
               for cell in NOMINATED_CELLS]
    for st in c_cells:
        st["pass_evaluation"] = _cell_pass(st)

    c_rows = len(part["C"])
    any_n30 = any(st["n"] >= CELL_MIN_N for st in c_cells)
    if c_rows == 0:
        status = "AWAITING_PROSPECTIVE_DATA"
    elif not any_n30:
        status = "INSUFFICIENT_PROSPECTIVE_N"
    else:
        so = all(st["pass_evaluation"]["passed"] for st in c_cells[:3])
        fu = any(st["pass_evaluation"]["passed"] for st in c_cells[3:])
        status = "CONFIRMED" if (so and fu) else "NOT_CONFIRMED"

    return {
        "spec_sha256": FROZEN_SPEC_SHA256,
        "spec_intact": True,
        "CONFIRMATION_FREEZE_TIMESTAMP": spec["CONFIRMATION_FREEZE_TIMESTAMP"],
        "classification_filter": classification,
        "terminology_audit": {
            "chronological_split_is_prospective": False,
            "note": ("Population B re-scores the SAME historical rows "
                     "chronologically; it is labelled HISTORICAL and can "
                     "never contribute to the confirmation verdict. Only "
                     "post-freeze population C is prospective."),
        },
        "A_discovery": {"n_rows": len(part["A"]),
                        "n_games": len({r["source_game_id"]
                                        for r in part["A"]}),
                        "nominated_cells_in_A": a_cells},
        "B_historical_validation": {
            "is_prospective": False,
            "n_rows": len(part["B"]),
            "chronological_blocks": _b_blocks(part["B"]),
        },
        "C_prospective": {"is_prospective": True,
                          "n_rows": c_rows,
                          "n_games": len({r["source_game_id"]
                                          for r in part["C"]}),
                          "nominated_cells": c_cells},
        "status": status,
        "read_only": True,
        "no_betting_output": True,
        "no_model_fit": True,
        "spec_frozen_while_waiting": True,
    }


def confirmation_status(rep: dict[str, Any]) -> str:
    """Render the required verdict block (no result manufacturing)."""
    c = rep["C_prospective"]
    lines = [
        "TERMINOLOGY AUDIT: chronological split within historical data is "
        "NOT prospective; only post-freeze rows count",
        f"FREEZE TIMESTAMP: {rep['CONFIRMATION_FREEZE_TIMESTAMP']}",
        f"SPEC INTEGRITY: sha256 {rep['spec_sha256'][:16]}… intact="
        f"{rep['spec_intact']}",
        f"A HISTORICAL/DISCOVERY: {rep['A_discovery']['n_rows']} rows / "
        f"{rep['A_discovery']['n_games']} games",
        f"B HISTORICAL WALK-FORWARD: {rep['B_historical_validation']['n_rows']}"
        " rows (NOT prospective; excluded from verdict)",
        f"C PROSPECTIVE POST-FREEZE: {c['n_rows']} rows / {c['n_games']}"
        " games",
    ]
    for st in c["nominated_cells"]:
        pe = st["pass_evaluation"]
        lines.append(f"  C cell {st['side']} {st['mag']} @{st['age']}: "
                     f"n={st['n']} rate={st['win_rate']} "
                     f"passed={pe['passed']}")
    lines.append(f"STATUS: {rep['status']}")
    if rep["status"] == "AWAITING_PROSPECTIVE_DATA":
        lines.append("No post-freeze observations exist yet. The frozen "
                     "specification and harness await prospective data. "
                     "No result manufactured; spec untouched while waiting.")
    return "\n".join(lines)
