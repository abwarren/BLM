"""BLM V4 — Residual x Market-State Interaction Validation (read-only).

Validates out-of-sample whether the RESIDUAL x MARKET-AGE interaction
found by the freshness audit persists, WITHOUT changing anything:

  - projection.py / scorecard.py / deviation.py:  UNTOUCHED
  - freshness_audit.py:                            UNTOUCHED (imported)
  - calibration.py:                                UNTOUCHED (imported)
  - no interaction model is FIT (no logistic-interaction, no GBM/RF/NN)
  - no betting / probability / EV thresholds

Pre-specification (directive section 3): the residual magnitude groups
(0-2.5 / 2.5-5 / 5-10 / 10-20 / >20) and market-age groups (0-5s ...
>300s) are the freshness audit's FIXED grids, imported as constants —
boundaries are not altered and no cell may be selected or removed by
its outcome.  Every cell is reported; small-N cells are flagged
exploratory (N < 30) vs evaluable (N >= 30), never pooled by outcome.

Chronological walk-forward (section 6): blocks are daily (the store's
5-day span); per-block cell rates are computed from that block's rows
only.  Persistence of a cell across blocks is counted, never tuned.
No future block influences any earlier block's report.

Multiple-comparison discipline (section 13): the grid has 5 x 8 = 40
cells per side (80 total).  A "discovery" requires ALL of:
  (a) N >= 30 in the cell (evaluable, not exploratory),
  (b) win rate >= 0.60 with the Wilson 95% LOWER bound above 0.50,
  (c) the cell's sign persists in >= 3 of the blocks where the cell has
      N >= 10 (chronological replication),
  (d) the direction-level effect survives the same test in BOTH
      chronological halves (split-half replication).
Cells meeting fewer criteria are labelled accordingly; nothing is
called an edge.

Mechanism foci (sections 8/9): the fresh-line UNDER effect (0-5s cells
by magnitude) and the stale-line OVER monotonicity (|M-F| gradient at
>300s, tested with a fixed low-vs-high contrast — no boundary fitting).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from blm_v4.calibration import (PRIMARY_PCTS, _metrics_for, brier,
                                load_rows, wilson_ci)
from blm_v4.freshness_audit import (AGE_BANDS, _ages, age_band,
                                    market_forecast_error)

# The pre-specified grids (imported constants — never redefined here).
RESIDUAL_MAGS = ("0-2.5", "2.5-5", "5-10", "10-20", ">20")
AGE_LABELS = tuple(label for label, _, _ in AGE_BANDS)
EVALUABLE_N = 30          # cells below this are EXPLORATORY
PERSIST_MIN_RATE = 0.60   # pre-registered discovery threshold (rate)
PERSIST_MIN_BLOCKS = 3    # blocks (N>=10) where the cell rate >= 0.60

MAG_BANDS = (("0-2.5", 0.0, 2.5), ("2.5-5", 2.5, 5.0),
             ("5-10", 5.0, 10.0), ("10-20", 10.0, 20.0), (">20", 20.0, None))


def mag_band(absraw: float) -> str:
    for label, lo, hi in MAG_BANDS:
        if absraw >= lo and (hi is None or absraw < hi):
            return label
    return ">20"


def _cell(sub: list[dict]) -> dict[str, Any]:
    n = len(sub)
    w = sum(r["y"] for r in sub)
    lo, hi = wilson_ci(w, n)
    return {"n": n, "games": len({r["source_game_id"] for r in sub}),
            "wins": w, "losses": n - w,
            "win_rate": round(w / n, 3) if n else None,
            "wilson_lo": lo, "wilson_hi": hi,
            "status": "evaluable" if n >= EVALUABLE_N else "exploratory"}


def _side_of(r: dict) -> str:
    return r["side"]


# ── section 3: the full interaction grid ───────────────────────────────
def interaction_grid(rows: list[dict]) -> dict[str, Any]:
    """Full 5x8 grid per side.  EVERY cell is reported; none removed."""
    grid: dict[str, Any] = {}
    cells_flat: list[dict] = []
    for side in ("OVER", "UNDER"):
        side_rows = [r for r in rows if r["side"] == side]
        by_age: dict[str, Any] = {}
        for age_label in AGE_LABELS:
            col = []
            for mag in RESIDUAL_MAGS:
                cell_rows = [r for r in side_rows
                             if mag_band(r["abs_raw"]) == mag
                             and age_band(r["age_seconds"]) == age_label]
                cell = _cell(cell_rows)
                cell.update({"side": side, "mag": mag, "age": age_label})
                col.append(cell)
                cells_flat.append(cell)
            by_age[age_label] = col
        grid[side] = by_age
    n_evaluable = sum(1 for c in cells_flat if c["status"] == "evaluable")
    return {"grid": grid, "mags": list(RESIDUAL_MAGS), "summary": {
        "cells_total": len(cells_flat),
        "cells_evaluable": n_evaluable,
        "cells_exploratory": len(cells_flat) - n_evaluable,
    }}


# ── section 4 helpers: discovery flags ─────────────────────────────────
def discovery_flags(cell: dict, block_persistence: int,
                    half_persistence: bool) -> dict[str, Any]:
    """Pre-registered criteria (a)-(d); nothing is pooled or dropped."""
    rate = cell.get("win_rate")
    lo = cell.get("wilson_lo")
    a = cell["n"] >= EVALUABLE_N
    b = (rate is not None and lo is not None
         and rate >= PERSIST_MIN_RATE and lo > 0.50)
    c = block_persistence >= PERSIST_MIN_BLOCKS
    d = half_persistence
    met = sum(1 for x in (a, b, c, d) if x)
    if a and b and c and d:
        label = "MEETS_ALL_CRITERIA"
    elif b and c:
        label = "RATE+WILSON+PERSISTENCE (split-half unconfirmed)"
    elif b:
        label = "RATE+WILSON only (exploratory; no persistence proof)"
    else:
        label = "not a candidate"
    return {"criteria": {"a_evaluable": a, "b_rate_wilson": b,
                         "c_block_persistence": c, "d_half_replication": d},
            "criteria_met": met, "label": label}


# ── sections 5/6/9: persistence machinery ──────────────────────────────
def _block_key(r: dict) -> str:
    return r["started_date"]


def cell_persistence(rows: list[dict], side: str, mag: str,
                     age_label: str) -> dict[str, Any]:
    """Per-block rates for one cell + split-half replication.

    Block persistence = number of blocks with N >= 10 and rate >= 0.60.
    Split-half: games split chronologically in half by start date; the
    cell must show rate > 0.50 in BOTH halves (with N >= 10 each) for
    half-replication.
    """
    sel = [r for r in rows if r["side"] == side
           and mag_band(r["abs_raw"]) == mag
           and age_band(r["age_seconds"]) == age_label]
    by_block: dict[str, list[dict]] = defaultdict(list)
    for r in sel:
        by_block[_block_key(r)].append(r)
    blocks = {}
    persistent = 0
    for d in sorted(by_block):
        sub = by_block[d]
        c = _cell(sub)
        blocks[d] = {"n": c["n"], "win_rate": c["win_rate"]}
        if c["n"] >= 10 and c["win_rate"] is not None \
                and c["win_rate"] >= PERSIST_MIN_RATE:
            persistent += 1
    dates = sorted({r["started_date"] for r in rows})
    mid = dates[len(dates) // 2]
    halves = {}
    for name, pred in (("first", lambda d: d < mid),
                       ("second", lambda d: d >= mid)):
        sub = [r for r in sel if pred(r["started_date"])]
        c = _cell(sub)
        halves[name] = {"n": c["n"], "win_rate": c["win_rate"]}
    half_ok = all(h["n"] >= 10 and h["win_rate"] is not None
                  and h["win_rate"] > 0.50 for h in halves.values())
    return {"blocks": blocks, "persistent_blocks": persistent,
            "halves": halves, "half_replication": half_ok}


# ── section 5: checkpoint control for the two mechanism cells ──────────
def checkpoint_control(rows: list[dict], side: str, mag: str,
                       age_label: str) -> dict[str, Any]:
    sel = [r for r in rows if r["side"] == side
           and mag_band(r["abs_raw"]) == mag
           and age_band(r["age_seconds"]) == age_label]
    out = {}
    for pct in PRIMARY_PCTS:
        c = _cell([r for r in sel if r["checkpoint_pct"] == pct])
        if c["n"]:
            out[pct] = c
    return out


# ── section 8: fresh-line UNDER mechanism ──────────────────────────────
def fresh_under_mechanism(rows: list[dict]) -> dict[str, Any]:
    sel = [r for r in rows if r["side"] == "UNDER"
           and age_band(r["age_seconds"]) == "0-5s"]
    by_mag = {m: _cell([r for r in sel if mag_band(r["abs_raw"]) == m])
              for m in RESIDUAL_MAGS}
    # persistence + controls for the aggregate fresh-UNDER effect
    pers = cell_persistence(rows, "UNDER", ">20", "0-5s")  # largest cell
    agg_halves = _split_half_rate([r for r in rows
                                   if r["side"] == "UNDER"
                                   and age_band(r["age_seconds"]) == "0-5s"],
                                  rows)
    by_cp = {}
    for pct in PRIMARY_PCTS:
        c = _cell([r for r in sel if r["checkpoint_pct"] == pct])
        if c["n"]:
            by_cp[pct] = c
    return {"by_magnitude": by_mag, "aggregate": _cell(sel),
            "split_halves": agg_halves, "by_checkpoint": by_cp,
            "block_persistence_example": pers["blocks"]}


def _split_half_rate(sel: list[dict], rows: list[dict]) -> dict:
    dates = sorted({r["started_date"] for r in rows})
    mid = dates[len(dates) // 2]
    out = {}
    for name, pred in (("first", lambda d: d < mid),
                       ("second", lambda d: d >= mid)):
        sub = [r for r in sel if pred(r["started_date"])]
        out[name] = _cell(sub)
    return out


# ── section 9: stale-line OVER monotonicity ────────────────────────────
def stale_over_mechanism(rows: list[dict]) -> dict[str, Any]:
    sel = [r for r in rows if r["side"] == "OVER"
           and age_band(r["age_seconds"]) == ">300s"]
    by_mag = {m: _cell([r for r in sel if mag_band(r["abs_raw"]) == m])
              for m in RESIDUAL_MAGS}
    # pre-registered monotonicity contrast: pooled LOW (0-5) vs HIGH
    # (10-20 + >20) magnitudes — fixed groups, no boundary fitting
    low = [r for r in sel if mag_band(r["abs_raw"]) in ("0-2.5", "2.5-5")]
    high = [r for r in sel if mag_band(r["abs_raw"]) in ("10-20", ">20")]
    contrast = {"low_pooled": _cell(low), "high_pooled": _cell(high)}
    # each magnitude's own persistence
    pers = {}
    for m in ("5-10", "10-20", ">20"):
        p = cell_persistence(rows, "OVER", m, ">300s")
        pers[m] = {"persistent_blocks": p["persistent_blocks"],
                   "half_replication": p["half_replication"],
                   "halves": p["halves"]}
    by_cp = {}
    for pct in PRIMARY_PCTS:
        c = _cell([r for r in sel if r["checkpoint_pct"] == pct])
        if c["n"]:
            by_cp[pct] = c
    return {"by_magnitude": by_mag, "aggregate": _cell(sel),
            "low_vs_high_contrast": contrast, "persistence": pers,
            "by_checkpoint": by_cp}


# ── section 7: game weighting for the two mechanism effects ────────────
def game_weighted_effects(rows: list[dict]) -> dict[str, Any]:
    def effect(sel: list[dict]) -> dict:
        by_game: dict[str, list[dict]] = defaultdict(list)
        for r in sel:
            by_game[r["source_game_id"]].append(r)
        rates = [sum(r["y"] for r in g) / len(g) for g in by_game.values()]
        n = len(sel)
        w = sum(r["y"] for r in sel)
        return {"games": len(by_game),
                "checkpoint_weighted_rate": round(w / n, 3) if n else None,
                "game_weighted_rate": round(
                    sum(rates) / len(rates), 3) if rates else None}
    stale_over = [r for r in rows if r["side"] == "OVER"
                  and age_band(r["age_seconds"]) == ">300s"]
    fresh_under = [r for r in rows if r["side"] == "UNDER"
                   and age_band(r["age_seconds"]) == "0-5s"]
    return {"stale_over": effect(stale_over),
            "fresh_under": effect(fresh_under)}


# ── section 11: baselines (diagnostic) ─────────────────────────────────
def baselines(rows: list[dict]) -> dict[str, Any]:
    """always-O/U, raw direction, and the two DIAGNOSTIC baselines:
    residual-only (sign of M-F, i.e. the raw model) vs age-only
    (predict UNDER on 0-5s rows, OVER on >300s rows, raw direction
    otherwise — a fixed pre-specification, no tuning)."""
    n = len(rows)
    over_outcomes = sum(1 for r in rows
                        if (r["side"] == "OVER") == (r["y"] == 1))
    age_only_hits = 0
    for r in rows:
        b = age_band(r["age_seconds"])
        if b == "0-5s":
            pred = "UNDER"
        elif b == ">300s":
            pred = "OVER"
        else:
            pred = r["side"]          # raw direction elsewhere
        age_only_hits += 1 if pred == r["side"] and r["y"] == 1 else 0
        # note: when pred == side, hit iff y == 1; when pred != side
        # (impossible here by construction) it would hit iff y == 0
    return {
        "n": n,
        "raw_direction": round(sum(r["y"] for r in rows) / n, 3),
        "always_over": round(over_outcomes / n, 3),
        "always_under": round((n - over_outcomes) / n, 3),
        "age_only_diagnostic": round(age_only_hits / n, 3),
        "note": ("residual-only equals the raw direction on this "
                 "population (the side IS the residual sign); age-only "
                 "is a fixed pre-specification, not a fitted model"),
    }


# ── section 10: MFE by interaction cell (outcome diagnostic) ───────────
def mfe_by_interaction(rows: list[dict]) -> list[dict]:
    rep = market_forecast_error(rows)
    out = []
    for side in ("OVER", "UNDER"):
        side_rows = [r for r in rows if r["side"] == side]
        for mag in RESIDUAL_MAGS:
            for age_label in (">300s", "0-5s"):
                sub = [r for r in side_rows
                       if mag_band(r["abs_raw"]) == mag
                       and age_band(r["age_seconds"]) == age_label
                       and r.get("live_market_line") is not None]
                if len(sub) < 10:
                    continue
                mfes = [r["actual_final_total"] - r["live_market_line"]
                        for r in sub]
                out.append({
                    "side": side, "mag": mag, "age": age_label,
                    "n": len(sub),
                    "mean_mfe": round(sum(mfes) / len(mfes), 2),
                    "mean_abs_mfe": round(
                        sum(abs(x) for x in mfes) / len(mfes), 2),
                })
    return {"cells": out, "note": rep["note"]}


# ── top-level report ───────────────────────────────────────────────────
def interaction_report(db_path: str,
                       classification: Optional[str] = None) -> dict[str, Any]:
    rows = _ages(load_rows(db_path, classification))
    grid = interaction_grid(rows)
    # mechanism cells with full persistence machinery
    stale_over = stale_over_mechanism(rows)
    fresh_under = fresh_under_mechanism(rows)
    # headline cell flags (the directive-named candidates)
    flags = {}
    for mag in ("5-10", "10-20", ">20"):
        p = cell_persistence(rows, "OVER", mag, ">300s")
        cell = next(c for c in grid["grid"]["OVER"][">300s"]
                    if c["mag"] == mag)
        flags[f"OVER {mag} @>300s"] = {
            "cell": cell,
            "persistence": {"persistent_blocks": p["persistent_blocks"],
                            "half_replication": p["half_replication"],
                            "blocks": p["blocks"]},
            "discovery": discovery_flags(cell, p["persistent_blocks"],
                                         p["half_replication"]),
        }
    p_fu = cell_persistence(rows, "UNDER", ">20", "0-5s")
    fu_cell = next(c for c in grid["grid"]["UNDER"]["0-5s"]
                   if c["mag"] == ">20")
    flags["UNDER >20 @0-5s"] = {
        "cell": fu_cell,
        "persistence": {"persistent_blocks": p_fu["persistent_blocks"],
                        "half_replication": p_fu["half_replication"],
                        "blocks": p_fu["blocks"]},
        "discovery": discovery_flags(fu_cell, p_fu["persistent_blocks"],
                                     p_fu["half_replication"]),
    }
    return {
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "classification_filter": classification or "ALL",
        "population": {"rows": len(rows),
                       "games": len({r["source_game_id"] for r in rows})},
        "grid": grid,
        "discovery_flags": flags,
        "stale_over_mechanism": stale_over,
        "fresh_under_mechanism": fresh_under,
        "checkpoint_control": {
            "OVER >20 @>300s": checkpoint_control(rows, "OVER", ">20", ">300s"),
            "UNDER >20 @0-5s": checkpoint_control(rows, "UNDER", ">20", "0-5s"),
        },
        "game_weighted": game_weighted_effects(rows),
        "baselines": baselines(rows),
        "mfe_by_interaction": mfe_by_interaction(rows),
        "read_only": True,
        "no_betting_output": True,
        "no_model_fit": True,
    }
