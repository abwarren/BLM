"""BLM V4 — Freshness / Stale-vs-LIVE Mechanism Audit (read-only forensic).

Question: WHY does the directional relationship exist predominantly when
the market line is classified STALE?  This module describes the
mechanism; it changes nothing:

  - projection.py:            UNTOUCHED
  - scorecard direction logic: UNTOUCHED
  - calibration.py:            UNTOUCHED (imported read-only)
  - no betting thresholds, no EV, no staking

Primary data: the SAME eligible population as the calibration layer
(load_rows): checkpoints 10-90, valid frozen live line, settled outcome,
terminal pct100 excluded, first-snapshot tautology excluded, NO_EDGE and
PUSH excluded from win/loss denominators.

CRITICAL TEMPORAL RULE (directive section 10): market age is computed
STRICTLY from contemporaneous observations (market_timestamp <=
checkpoint_timestamp, clamped at zero) — never from the closing line,
future movement, or the final score.  The final score appears in
exactly one place: the retrospective market forecast error diagnostic
(section 9), which is labelled an OUTCOME analysis and is never an
input to any classification.

Sections (directive):
  1. precise freshness: full age_seconds distribution per row
  2. fixed market-age bands (0-5 / 5-10 / 10-20 / 20-30 / 30-60 /
     60-120 / 120-300 / >300 s) — fixed a-priori, never optimized
  3. market update frequency per game (count, mean/median/max interval)
     and performance by update-frequency tier
  4. residual x age 2-D descriptive table (fixed bands, direction split)
  5. checkpoint control (per 10..90: LIVE vs STALE)
  6. chronological control (per day: LIVE vs STALE accuracy + Brier)
  7. OVER/UNDER kept completely separate throughout
  8. checkpoint-weighted AND game-weighted results
  9. market_forecast_error = final_total - live_line, BY AGE BAND
     (retrospective diagnostic only)
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from blm_v4.calibration import (MARKET_STALE_SECONDS, PRIMARY_PCTS,
                                _metrics_for, _parse_ts, brier, load_rows,
                                wilson_ci)

# Fixed market-age bands (seconds).  Directive-mandated grid, half-open
# [lo, hi); the last band is open-ended.  Never optimized.
AGE_BANDS = (
    ("0-5s", 0, 5), ("5-10s", 5, 10), ("10-20s", 10, 20),
    ("20-30s", 20, 30), ("30-60s", 30, 60), ("60-120s", 60, 120),
    ("120-300s", 120, 300), (">300s", 300, None),
)

# Fixed update-frequency tiers (median interval, seconds) — descriptive
# only, boundaries never tuned.
UPDATE_TIERS = (("fast(<60s)", 0, 60), ("normal(60-180s)", 60, 180),
                ("slow(>180s)", 180, None))


def age_band(age: Optional[float]) -> Optional[str]:
    if age is None:
        return None
    for label, lo, hi in AGE_BANDS:
        if age >= lo and (hi is None or age < hi):
            return label
    return ">300s"


def _median(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _cell(sub: list[dict]) -> dict[str, Any]:
    """Raw directional cell: N, wins, rate, Wilson CI, Brier (p=side
    base rate as the constant predictor is NOT used — Brier here is the
    walk-forward-free constant-base-rate reference, labelled)."""
    n = len(sub)
    w = sum(r["y"] for r in sub)
    lo, hi = wilson_ci(w, n)
    return {"n": n, "wins": w, "losses": n - w,
            "win_rate": round(w / n, 3) if n else None,
            "wilson_lo": lo, "wilson_hi": hi}


def _ages(rows: list[dict]) -> list[dict]:
    """Attach age_seconds to every row (strictly contemporaneous)."""
    out = []
    for r in rows:
        mt, ct = _parse_ts(r.get("market_timestamp")), _parse_ts(
            r["checkpoint_timestamp"])
        age = max(0.0, (ct - mt).total_seconds()) if (mt and ct) else None
        r2 = dict(r)
        r2["age_seconds"] = age
        out.append(r2)
    return out


# ── section 1: age distribution ────────────────────────────────────────
def age_distribution(rows: list[dict]) -> dict[str, Any]:
    ages = [r["age_seconds"] for r in rows if r["age_seconds"] is not None]
    if not ages:
        return {"n": 0}
    s = sorted(ages)
    qs = [0.05, 0.25, 0.50, 0.75, 0.95]
    quant = {f"p{int(q*100)}": round(s[min(len(s) - 1, int(q * len(s)))], 1)
             for q in qs}
    bands = defaultdict(int)
    for a in ages:
        bands[age_band(a)] += 1
    return {
        "n": len(ages),
        "min": round(s[0], 1), "max": round(s[-1], 1),
        "mean": round(sum(ages) / len(ages), 1),
        "median": round(s[len(s) // 2], 1),
        **quant,
        "bands": {label: bands.get(label, 0) for label, _, _ in AGE_BANDS},
        "live_fraction": round(
            sum(1 for a in ages if a <= MARKET_STALE_SECONDS) / len(ages), 3),
    }


# ── section 2: performance by fixed age band ───────────────────────────
def by_age_band(rows: list[dict]) -> list[dict]:
    out = []
    for label, _, _ in AGE_BANDS:
        sub = [r for r in rows if age_band(r["age_seconds"]) == label]
        cell = _cell(sub)
        over = _cell([r for r in sub if r["side"] == "OVER"])
        under = _cell([r for r in sub if r["side"] == "UNDER"])
        out.append({"band": label, "n": cell["n"],
                    "games": len({r["source_game_id"] for r in sub}),
                    "combined": cell, "over": over, "under": under})
    return out


# ── section 3: market update frequency ─────────────────────────────────
UPDATE_SQL = """
SELECT source_game_id, captured_at, line_value FROM market_observations
WHERE market_type = 'MatchTotal' ORDER BY source_game_id, captured_at
"""


def update_frequency(db_path: str, rows: list[dict]) -> dict[str, Any]:
    """Per-game market update stats from the eu-swarm MatchTotal feed,
    plus performance by update-frequency tier.

    NOTE: the WS feed is a SECOND line source alongside event-view
    snapshot lines; update intervals here describe the WS feed's cadence
    (the feed whose staleness fallback the frozen-line rule uses), not
    event-view captures.  Games with no WS observations are reported as
    no_ws_feed and excluded from tiers honestly.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        has_feed = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='market_observations'").fetchone()
        per_game: dict[str, list[float]] = defaultdict(list)
        if not has_feed:
            # schema without the WS feed table: zero coverage, honestly
            # reported (the tiers below then carry no_ws_feed only)
            stats: dict[str, dict] = {}
            intervals_all: list[float] = []
            games_seen = {r["source_game_id"] for r in rows}
            tier_rows = {label: [] for label, _, _ in UPDATE_TIERS}
            tier_rows["no_ws_feed"] = list(rows)
            tiers = {label: {**_cell(tier_rows[label]),
                             "games": len({r["source_game_id"]
                                           for r in tier_rows[label]})}
                     for label, _, _ in UPDATE_TIERS}
            tiers["no_ws_feed"] = {**_cell(rows),
                                   "games": len(games_seen)}
            return {"games_with_ws_feed": 0,
                    "games_without_ws_feed": len(games_seen),
                    "median_interval_distribution": {
                        "mean_of_medians": None, "median_of_medians": None},
                    "by_tier": tiers,
                    "note": "no market_observations table in store"}
        cur_gid, prev_ts = None, None
        for r in conn.execute(UPDATE_SQL):
            gid = r["source_game_id"]
            ts = _parse_ts(r["captured_at"])
            if gid != cur_gid:
                cur_gid, prev_ts = gid, ts
                continue
            if ts and prev_ts:
                dt = (ts - prev_ts).total_seconds()
                if 0 < dt < 3600:      # guard against feed gaps
                    per_game[gid].append(dt)
            prev_ts = ts
    finally:
        conn.close()
    stats: dict[str, dict] = {}
    for gid, intervals in per_game.items():
        stats[gid] = {
            "updates": len(intervals) + 1,
            "mean_interval": round(sum(intervals) / len(intervals), 1),
            "median_interval": round(_median(intervals), 1),
            "max_interval": round(max(intervals), 1),
        }
    # attach tier per game (from its checkpoints' game ids)
    tier_rows: dict[str, list[dict]] = defaultdict(list)
    games_seen = {r["source_game_id"] for r in rows}
    for gid in games_seen:
        s = stats.get(gid)
        if not s:
            tier_rows["no_ws_feed"].extend(
                [r for r in rows if r["source_game_id"] == gid])
            continue
        med = s["median_interval"]
        tier = next((label for label, lo, hi in UPDATE_TIERS
                     if med >= lo and (hi is None or med < hi)),
                    UPDATE_TIERS[-1][0])
        tier_rows[tier].extend([r for r in rows
                                if r["source_game_id"] == gid])
    tiers = {}
    for label, _, _ in UPDATE_TIERS:
        sub = tier_rows.get(label, [])
        tiers[label] = {**_cell(sub),
                        "games": len({r["source_game_id"] for r in sub})}
    tiers["no_ws_feed"] = {**_cell(tier_rows.get("no_ws_feed", [])),
                           "games": len({r["source_game_id"]
                                         for r in tier_rows.get("no_ws_feed",
                                                               [])})}
    intervals_all = [v for s in stats.values()
                     for v in [s["median_interval"]]]
    return {
        "games_with_ws_feed": len(stats),
        "games_without_ws_feed": len(games_seen) - len(stats),
        "median_interval_distribution": {
            "mean_of_medians": round(sum(intervals_all) / len(intervals_all), 1)
            if intervals_all else None,
            "median_of_medians": round(_median(intervals_all), 1)
            if intervals_all else None,
        },
        "by_tier": tiers,
    }


# ── section 4: residual x age interaction ──────────────────────────────
def residual_by_age(rows: list[dict]) -> dict[str, Any]:
    """2-D fixed-grid table: residual magnitude band x age band."""
    def mag_band(absraw: float) -> str:
        if absraw <= 2.5:
            return "0-2.5"
        if absraw <= 5:
            return "2.5-5"
        if absraw <= 10:
            return "5-10"
        if absraw <= 20:
            return "10-20"
        return ">20"
    mags = ("0-2.5", "2.5-5", "5-10", "10-20", ">20")
    grid: dict[str, Any] = {}
    for side in ("OVER", "UNDER"):
        side_rows = [r for r in rows if r["side"] == side]
        cols: dict[str, Any] = {}
        for label, _, _ in AGE_BANDS:
            col = []
            for m in mags:
                cell_rows = [r for r in side_rows
                             if mag_band(r["abs_raw"]) == m
                             and age_band(r["age_seconds"]) == label]
                col.append({"mag": m, **_cell(cell_rows)})
            cols[label] = col
        grid[side] = cols
    mfe_note = None
    return {"magnitudes": mags, "grid": grid}


# ── section 5: checkpoint control ──────────────────────────────────────
def by_checkpoint(rows: list[dict]) -> list[dict]:
    out = []
    for pct in PRIMARY_PCTS:
        sub = [r for r in rows if r["checkpoint_pct"] == pct]
        live = [r for r in sub if r["market_status"] == "LIVE"]
        stale = [r for r in sub if r["market_status"] == "STALE"]
        out.append({"checkpoint_pct": pct, "n": len(sub),
                    "live": _cell(live), "stale": _cell(stale),
                    "median_age": round(_median(
                        [r["age_seconds"] for r in sub
                         if r["age_seconds"] is not None]) or 0, 1)})
    return out


# ── section 6: chronological control ───────────────────────────────────
def by_block(rows: list[dict]) -> list[dict]:
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_date[r["started_date"]].append(r)
    out = []
    for d in sorted(by_date):
        sub = by_date[d]
        live = [r for r in sub if r["market_status"] == "LIVE"]
        stale = [r for r in sub if r["market_status"] == "STALE"]
        out.append({
            "date": d, "games": len({r["source_game_id"] for r in sub}),
            "live": _cell(live), "stale": _cell(stale),
            "live_brier": round(brier(
                [1.0 if r["market_status"] == "LIVE" else 0.0] * len(sub),
                [1 if r["market_status"] == "LIVE" else 0 for r in sub]), 4)
            if sub else None,
        })
    return out


# ── section 8: weighting ───────────────────────────────────────────────
def game_weighted(rows: list[dict]) -> dict[str, Any]:
    """Game-weighted LIVE vs STALE accuracy: per-game rate first, then
    the unweighted mean over games (a 9-checkpoint game counts once)."""
    def mean_rate(sub: list[dict], status: str) -> dict:
        by_game: dict[str, list[dict]] = defaultdict(list)
        for r in sub:
            if r["market_status"] == status:
                by_game[r["source_game_id"]].append(r)
        if not by_game:
            return {"games": 0, "mean_rate": None}
        rates = [sum(r["y"] for r in g) / len(g) for g in by_game.values()]
        return {"games": len(by_game),
                "mean_rate": round(sum(rates) / len(rates), 3)}
    return {"live": mean_rate(rows, "LIVE"),
            "stale": mean_rate(rows, "STALE")}


# ── section 9: market forecast error (retrospective OUTCOME analysis) ──
def market_forecast_error(rows: list[dict]) -> dict[str, Any]:
    """mfe = final_total - live_line BY AGE BAND — diagnostic only,
    never an input.  Positive mfe means the total finished above the
    checkpoint line.  Larger |mfe| variance at long ages would indicate
    stale lines simply know less."""
    out = []
    for label, _, _ in AGE_BANDS:
        sub = [r for r in rows if age_band(r["age_seconds"]) == label
               and r.get("live_market_line") is not None]
        mfes = [r["actual_final_total"] - r["live_market_line"] for r in sub]
        if not mfes:
            out.append({"band": label, "n": 0})
            continue
        abs_mfes = [abs(x) for x in mfes]
        over_n = sum(1 for x in mfes if x > 0)
        lo, hi = wilson_ci(over_n, len(mfes))
        out.append({
            "band": label, "n": len(mfes),
            "mean_mfe": round(sum(mfes) / len(mfes), 2),
            "mean_abs_mfe": round(sum(abs_mfes) / len(abs_mfes), 2),
            "median_abs_mfe": round(_median(abs_mfes), 2),
            "finished_over_line": over_n,
            "over_share": round(over_n / len(mfes), 3),
            "over_share_ci": [lo, hi],
        })
    return {"bands": out, "note": (
        "mfe = final_total - live_line_at_checkpoint; retrospective "
        "OUTCOME analysis only — never used to classify anything")}


# ── top-level report ───────────────────────────────────────────────────
def freshness_report(db_path: str,
                     classification: Optional[str] = None) -> dict[str, Any]:
    rows = _ages(load_rows(db_path, classification))
    live = [r for r in rows if r["market_status"] == "LIVE"]
    stale = [r for r in rows if r["market_status"] == "STALE"]
    over = [r for r in rows if r["side"] == "OVER"]
    under = [r for r in rows if r["side"] == "UNDER"]
    return {
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "classification_filter": classification or "ALL",
        "population": {"rows": len(rows),
                       "games": len({r["source_game_id"] for r in rows}),
                       "live": len(live), "stale": len(stale),
                       "over": len(over), "under": len(under)},
        "age_distribution": age_distribution(rows),
        "by_age_band": by_age_band(rows),
        "update_frequency": update_frequency(db_path, rows),
        "residual_by_age": residual_by_age(rows),
        "by_checkpoint": by_checkpoint(rows),
        "by_block": by_block(rows),
        "game_weighted": game_weighted(rows),
        "market_forecast_error": market_forecast_error(rows),
        "read_only": True,
        "no_betting_output": True,
    }
