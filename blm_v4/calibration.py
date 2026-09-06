"""BLM V4 — Calibration Layer (CALIBRATION SLICE, read-only research).

Maps the EXISTING directional model's raw score to an empirical
probability of being right, WITHOUT changing the model:

  - projection.py formula:      UNTOUCHED
  - scorecard direction logic:  UNTOUCHED
  - checkpoint timestamps:      UNTOUCHED
  - clean-observation gating:   UNTOUCHED (same eligibility as
    record_checkpoint_market / _CM_ELIGIBLE_SQL)
  - deviation / Z infrastructure: UNTOUCHED
  - no new predictive variables

Raw score (directive section 3) — from EXISTING model information only:

    raw = signed market-vs-fair residual = blm_fair_value - live_market_line

    raw > 0  -> model says OVER   (P(OVER win) increases with raw)
    raw < 0  -> model says UNDER  (P(UNDER win) increases with -raw)
    raw == 0 -> NO_EDGE (no-bet; excluded from calibration denominators)

The direction label is IDENTICAL to ``scorecard._checkpoint_outcome`` /
``_market_vs_fair_signal`` semantics.  The raw score is NOT called a
probability; calibration maps it to one.  Settlement uses the actual
final total vs the frozen live line; a landing exactly on the line is a
PUSH and is excluded (identical to the scorecard taxonomy).  Neither
final_total (beyond settlement), closing_line, CLV, nor OLV->CLV
movement ever enters the score.

Calibration population (directive section 2): non-terminal checkpoints
10..90 ONLY.  Terminal pct100 rows are excluded from the PRIMARY
calibration because the live-score floor creates a mechanical
relationship between terminal fair value and the final score; they stay
available as a separately-labelled diagnostic.  A first-snapshot
tautology guard additionally excludes any 10-90 row frozen at the game's
first observed instant (empty-history fair ~= market by construction).

Walk-forward (directive section 6): rows are ordered by (game start,
game id, checkpoint).  A row may only be evaluated by a calibrator fit
on rows from STRICTLY EARLIER COMPLETED GAMES — the same game's later
checkpoints and final outcome never enter the calibrator used for it,
and no future observation enters the calibration state.  Logistic
calibration is fit by IRLS on the prior rows; isotonic by PAV.  Both
methods are always evaluated and reported side by side; no method is
selected on in-sample accuracy (directive section 5).

OVER and UNDER are calibrated SEPARATELY (directive section 5) and never
mixed: each side has its own calibrators, metrics, reliability bins and
blocks.

Metrics (directive section 7): Brier score, log loss, reliability bins
(fixed probability bands), calibration intercept + slope (logistic
regression of the outcome on the logit of the predicted probability),
predicted-vs-actual per bin, N, and per-chronological-block performance.

Weighting (directive section 11): every side/method report carries the
checkpoint-weighted metrics AND a game-weighted view (per-game Brier /
log loss computed first, then averaged over games), so a 9-checkpoint
game can never count 9x in the game-level view.

Baselines (directive section 8): always-UNDER, always-OVER, the raw
model direction at its base rate, and the fade-model complement.  Note:
on this dataset a "market direction" baseline degenerates to the
fade-model complement (both derive from the same M-F sign); a genuine
line-move-direction baseline would require post-checkpoint movement
information and is therefore prohibited here.

NO betting thresholds, stake sizing, EV signals or automated wagers are
produced (directive section 12).  Read-only: opens the SQLite store in
``mode=ro``; never writes.
"""

from __future__ import annotations

import math
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

# The frozen market line is LIVE at-or-before this age, STALE after —
# the EXISTING M009-M3 definition (scorecard.MARKET_STALE_SECONDS),
# re-declared here read-only so the calibration layer never imports or
# perturbs scorecard state.
MARKET_STALE_SECONDS = int(os.environ.get("BLM_MARKET_STALE_SECONDS", "300"))

PRIMARY_PCTS = (10, 20, 30, 40, 50, 60, 70, 80, 90)

# Fixed reliability bins (probability space).  Boundaries are analytical
# constants — never tuned, never optimized (directive section 12).
RELIABILITY_BINS = (
    (0.00, 0.50), (0.50, 0.55), (0.55, 0.60), (0.60, 0.65),
    (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 0.90),
    (0.90, 1.00),
)

# Raw-score descriptive buckets (|residual| in points).  Analytical
# buckets only (directive section 4) — not betting thresholds.
RESIDUAL_BANDS = ("<=2", "2-5", "5-10", ">10")

MIN_ROWS_FOR_SLOPE = 8   # minimum rows for the slope/intercept regression
MIN_HISTORY_DEFAULT = 30  # walk-forward prior-row gate per side

# Residual (fair - market) buckets — FIXED analytical grid, never tuned
# against outcomes (slice-1 directive; label = "from, inclusive", to
# exclusive; the last band is open-ended).  Boundaries are documented
# constants, not optimized cutoffs.
RESIDUAL_BUCKET_EDGES = [
    ("< -20", None, -20.0), ("-20 to -15", -20.0, -15.0),
    ("-15 to -10", -15.0, -10.0), ("-10 to -7.5", -10.0, -7.5),
    ("-7.5 to -5", -7.5, -5.0), ("-5 to -2.5", -5.0, -2.5),
    ("-2.5 to 0", -2.5, 0.0), ("0 to +2.5", 0.0, 2.5),
    ("+2.5 to +5", 2.5, 5.0), ("+5 to +7.5", 5.0, 7.5),
    ("+7.5 to +10", 7.5, 10.0), ("+10 to +15", 10.0, 15.0),
    ("+15 to +20", 15.0, 20.0), ("> +20", 20.0, None),
]
MIN_BUCKET_N = 30          # merge rule: below this, merge with the
BUCKET_MERGE_DIRECTION = 0  # adjacent bucket closer to zero (documented)

EPS = 1e-9


# ── numeric helpers (no numpy dependency) ──────────────────────────────
def _logit(p: float) -> float:
    p = min(max(p, EPS), 1.0 - EPS)
    return math.log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _clip01(p: float) -> float:
    return min(max(p, 0.0), 1.0)


# ── calibrators ────────────────────────────────────────────────────────
def fit_logistic(xs: list[float], ys: list[int]) -> dict[str, float]:
    """Logistic regression y ~ 1 + x by IRLS (Newton-Raphson).

    Falls back to the intercept-only model when x carries no usable
    variance or the sample is tiny (degenerate prior windows) — an
    honest base-rate map, flagged via ``intercept_only``.
    """
    n = len(ys)
    if n == 0:
        return {"b0": 0.0, "b1": 0.0, "intercept_only": 1.0}
    ybar = sum(ys) / n
    b0 = _logit((n * ybar + 0.5) / (n + 1.0))  # smoothed start
    b1 = 0.0
    mean_x = sum(xs) / n
    var = sum((x - mean_x) ** 2 for x in xs) / n
    use_x = n >= MIN_ROWS_FOR_SLOPE and var > 1e-9
    for _ in range(50):
        g00 = g01 = g11 = 0.0
        d0 = d1 = 0.0
        for x, y in zip(xs, ys):
            p = min(max(_sigmoid(b0 + b1 * x), EPS), 1.0 - EPS)
            w = p * (1.0 - p)
            r = y - p
            g00 += w
            g01 += w * x
            g11 += w * x * x
            d0 += r
            d1 += r * x
        det = g00 * g11 - g01 * g01
        if abs(det) < 1e-12:
            break
        step0 = (g11 * d0 - g01 * d1) / det
        step1 = (g00 * d1 - g01 * d0) / det
        b0 += step0
        if use_x:
            b1 += step1
        if abs(step0) < 1e-8 and abs(step1) < 1e-8:
            break
    return {"b0": b0, "b1": b1, "intercept_only": 0.0 if use_x else 1.0}


def predict_logistic(model: dict[str, float], x: float) -> float:
    return _clip01(_sigmoid(model["b0"] + model["b1"] * x))


def fit_isotonic(xs: list[float], ys: list[int]) -> list[tuple[float, float]]:
    """PAV — pool-adjacent-violators (tie-safe).

    Returns (x, pooled_mean) knots sorted by x.  Identical x values are
    merged to one knot so the mapping stays a function of x.
    """
    if not xs:
        return []
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    # block = [sum_of_y, count, member_indices]
    blocks: list[list[Any]] = []
    for i in order:
        blocks.append([float(ys[i]), 1, [i]])
        while (len(blocks) >= 2
               and blocks[-2][0] / blocks[-2][1]
               > blocks[-1][0] / blocks[-1][1] + 1e-12):
            s2, n2, idx2 = blocks[-2]
            s1, n1, idx1 = blocks[-1]
            blocks[-2] = [s2 + s1, n2 + n1, idx2 + idx1]
            blocks.pop()
    knots: list[tuple[float, float]] = []
    for s, n, idxs in blocks:
        mean = s / n
        for i in idxs:
            knots.append((xs[i], mean))
    knots.sort(key=lambda t: t[0])
    merged: list[tuple[float, float]] = []
    for x, m in knots:
        if merged and merged[-1][0] == x:
            merged[-1] = (x, (merged[-1][1] + m) / 2.0)
        else:
            merged.append((x, m))
    return merged


def predict_isotonic(knots: list[tuple[float, float]], x: float) -> float:
    if not knots:
        return 0.5
    if x <= knots[0][0]:
        return _clip01(knots[0][1])
    if x >= knots[-1][0]:
        return _clip01(knots[-1][1])
    for i in range(1, len(knots)):
        x0, m0 = knots[i - 1]
        x1, m1 = knots[i]
        if x <= x1:
            if x1 == x0:
                return _clip01(m1)
            t = (x - x0) / (x1 - x0)
            return _clip01(m0 + t * (m1 - m0))
    return _clip01(knots[-1][1])


# ── metrics ────────────────────────────────────────────────────────────
def wilson_ci(wins: int, n: int,
              z: float = 1.959963984540054) -> tuple[float, float]:
    """Wilson score interval (no scipy dependency).

    Returns (lower, upper) bounds for a binomial proportion; n == 0 ->
    (None, None) honestly.
    """
    if n <= 0:
        return (None, None)
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    return (round(max(0.0, center - half), 3),
            round(min(1.0, center + half), 3))


def brier(ps: list[float], ys: list[int]) -> Optional[float]:
    if not ps:
        return None
    return round(sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps), 4)


def log_loss(ps: list[float], ys: list[int]) -> Optional[float]:
    if not ps:
        return None
    total = 0.0
    for p, y in zip(ps, ys):
        p = min(max(p, EPS), 1.0 - EPS)
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return round(total / len(ps), 4)


def calibration_slope_intercept(ps: list[float], ys: list[int]) -> dict:
    """Standard calibration regression: outcome ~ logit(p).

    slope=1, intercept=0 is perfect probabilistic calibration.
    Reported descriptively; insufficient-variance populations return
    None honestly.
    """
    if len(ps) < MIN_ROWS_FOR_SLOPE or len(set(ys)) < 2:
        return {"slope": None, "intercept": None, "n": len(ps),
                "note": "insufficient variance for the calibration fit"}
    m = fit_logistic([_logit(p) for p in ps], ys)
    return {"slope": round(m["b1"], 3), "intercept": round(m["b0"], 3),
            "n": len(ps)}


def reliability_bins(ps: list[float], ys: list[int]) -> list[dict]:
    out = []
    for lo, hi in RELIABILITY_BINS:
        sel = [(p, y) for p, y in zip(ps, ys)
               if lo <= p < hi or (hi == 1.0 and p == 1.0)]
        n = len(sel)
        out.append({
            "bin": f"{lo:.2f}-{hi:.2f}",
            "n": n,
            "mean_predicted": round(sum(p for p, _ in sel) / n, 3) if n else None,
            "actual_frequency": round(sum(y for _, y in sel) / n, 3) if n else None,
        })
    return out


def _metrics_for(pairs: list[tuple[float, int]]) -> dict[str, Any]:
    """Checkpoint-weighted metrics (each row one vote)."""
    if not pairs:
        return {"n": 0}
    ps = [p for p, _ in pairs]
    ys = [y for _, y in pairs]
    return {
        "n": len(pairs),
        "brier": brier(ps, ys),
        "log_loss": log_loss(ps, ys),
        "base_rate": round(sum(ys) / len(ys), 3),
        "reliability_bins": reliability_bins(ps, ys),
        "calibration": calibration_slope_intercept(ps, ys),
    }


# ── dataset construction (read-only) ───────────────────────────────────
LOAD_SQL = """
SELECT cm.source_game_id, cm.classification, cm.checkpoint_pct,
       cm.checkpoint_timestamp, cm.blm_fair_value, cm.live_market_line,
       cm.market_timestamp, cm.market_vs_fair, cm.actual_final_total,
       g.home_team, g.away_team
FROM checkpoint_market cm
JOIN game_results r ON r.source_game_id = cm.source_game_id
JOIN games g ON g.source_game_id = cm.source_game_id
WHERE r.final_result_status = 'OK'
  AND NOT EXISTS (SELECT 1 FROM game_quality q
                  WHERE q.source_game_id = cm.source_game_id
                    AND q.status = 'INVALID')
"""


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _market_status(market_ts: Optional[str], checkpoint_ts: Optional[str]) -> str:
    if market_ts is None:
        return "MISSING"
    mt, ct = _parse_ts(market_ts), _parse_ts(checkpoint_ts)
    if mt is None or ct is None:
        return "MISSING"
    age = max(0.0, (ct - mt).total_seconds())
    return "LIVE" if age <= MARKET_STALE_SECONDS else "STALE"


def load_rows(db_path: str, classification: Optional[str] = None) -> list[dict]:
    """Load eligible checkpoint rows and build the calibration dataset.

    Exclusions (directive sections 2/3): terminal pct100 rows leave the
    primary population (diagnostic only); first-snapshot tautology rows
    are excluded; NO_EDGE (raw == 0) rows and PUSH settlements carry no
    direction to calibrate and are excluded from denominators; rows
    without a frozen market line or a settled final total are excluded.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql, params = LOAD_SQL, []
        if classification:
            sql += " AND cm.classification = ?"
            params.append(classification)
        rows = [dict(r) for r in conn.execute(sql, params)]
        starts = dict(conn.execute(
            """SELECT g.source_game_id, MIN(s.captured_at)
               FROM games g JOIN snapshots s ON s.game_id = g.id
               GROUP BY g.source_game_id""").fetchall())
    finally:
        conn.close()
    out = []
    for r in rows:
        start = starts.get(r["source_game_id"])
        fair, line = r["blm_fair_value"], r["live_market_line"]
        actual = r["actual_final_total"]
        if fair is None or line is None or actual is None:
            continue
        if r["checkpoint_pct"] not in PRIMARY_PCTS:
            continue                          # terminal pct100 -> diagnostic only
        if start and str(r["checkpoint_timestamp"])[:16] <= str(start)[:16]:
            continue                          # first-snapshot tautology guard
        raw = round(fair - line, 2)           # signed market-vs-fair residual
        if raw == 0:
            continue                          # NO_EDGE — no direction
        outcome_dir = ("OVER" if actual > line else
                       "UNDER" if actual < line else "PUSH")
        if outcome_dir == "PUSH":
            continue                          # genuine push — no win/loss
        side = "OVER" if raw > 0 else "UNDER"
        # y = 1 when the model's side matched the settlement direction
        y = 1 if side == outcome_dir else 0
        r2 = dict(r)
        r2.update({
            "raw": raw, "abs_raw": abs(raw), "side": side,
            "y": y, "market_status": _market_status(
                r.get("market_timestamp"), r["checkpoint_timestamp"]),
            "game_start": start or "",
            "started_date": (start or "")[:10],
            "order_key": ((start or ""), r["source_game_id"],
                          r["checkpoint_pct"]),
        })
        out.append(r2)
    out.sort(key=lambda r: r["order_key"])
    return out


def band_of(absraw: float) -> str:
    if absraw <= 2:
        return "<=2"
    if absraw <= 5:
        return "2-5"
    if absraw <= 10:
        return "5-10"
    return ">10"


def residual_bucket(raw: float) -> str:
    """Bucket label for a signed residual on the fixed 14-bucket grid."""
    for label, lo, hi in RESIDUAL_BUCKET_EDGES:
        if (lo is None or raw >= lo) and (hi is None or raw < hi):
            return label
    return "> +20"


def merge_small_buckets(buckets: list[dict],
                        min_n: int = MIN_BUCKET_N) -> tuple[list[dict], str]:
    """DOCUMENTED merge rule (size-only — never outcome-aware).

    A bucket below ``min_n`` is pooled into its neighbouring bucket
    CLOSER TO THE GRID CENTRE (merging always pulls the sparse bucket
    toward the populated centre; the sparsest bucket merges first, ties
    break toward the innermost).  Returns the merged list plus the rule
    text for the report.
    """
    rule = (f"buckets with n < {min_n} are pooled into the adjacent bucket "
            f"closer to the grid centre (size-only rule, sparsest-first; "
            f"never outcome-aware)")
    out = [dict(b) for b in buckets]
    while len(out) > 1:
        centre = (len(out) - 1) / 2.0
        idx = min(range(len(out)),
                  key=lambda i: (out[i]["n"], abs(i - centre)))
        if out[idx]["n"] >= min_n:
            break
        candidates = [j for j in (idx - 1, idx + 1) if 0 <= j < len(out)]
        if not candidates:
            break
        target = min(candidates, key=lambda j: abs(j - centre))
        lo, hi = min(idx, target), max(idx, target) + 1
        merged = dict(out[lo])
        for k in range(lo + 1, hi):
            m = out[k]
            merged = {
                "bucket": f"{merged['bucket']}+{m['bucket']}",
                "n": merged["n"] + m["n"],
                "wins": merged["wins"] + m["wins"],
                "losses": merged["losses"] + m["losses"],
            }
        out[lo:hi] = [merged]
    return out, rule


def empirical_bucket_table(rows: list[dict],
                           merge: bool = True) -> dict[str, Any]:
    """STEP 1 — outcome rates per SIGNED residual bucket with Wilson CIs.

    Direction is the model side (raw > 0 -> OVER, raw < 0 -> UNDER);
    win = side matched settlement.  Merging uses the documented
    size-only rule; the rule text is returned with the table.
    """
    per: dict[str, dict[str, int]] = {}
    for r in rows:
        b = residual_bucket(r["raw"])
        d = per.setdefault(b, {"n": 0, "wins": 0, "losses": 0})
        d["n"] += 1
        if r["y"] == 1:
            d["wins"] += 1
        else:
            d["losses"] += 1
    raw_buckets = []
    for label, _, _ in RESIDUAL_BUCKET_EDGES:
        d = per.get(label, {"n": 0, "wins": 0, "losses": 0})
        lo_w, hi_w = wilson_ci(d["wins"], d["n"])
        raw_buckets.append({"bucket": label, **d, "win_rate":
                            round(d["wins"] / d["n"], 3) if d["n"] else None,
                            "wilson_lo": lo_w, "wilson_hi": hi_w})
    if merge:
        merged, rule = merge_small_buckets(raw_buckets)
        for b in merged:
            lo_w, hi_w = wilson_ci(b["wins"], b["n"])
            b["win_rate"] = round(b["wins"] / b["n"], 3) if b["n"] else None
            b["wilson_lo"] = lo_w
            b["wilson_hi"] = hi_w
        return {"buckets": merged, "merge_rule": rule}
    return {"buckets": raw_buckets, "merge_rule": None}


def empirical_checkpoint_dependence(rows: list[dict]) -> dict[str, Any]:
    """STEP 2 — residual-bucket outcome rates PER CHECKPOINT 10-90.

    The bucket grid is fixed; small samples stay unmerged here (the
    per-cell N is reported honestly).  Material change across game
    progress is MEASURED, never assumed away.
    """
    out: dict[str, Any] = {}
    for pct in PRIMARY_PCTS:
        sub = [r for r in rows if r["checkpoint_pct"] == pct]
        t = empirical_bucket_table(sub, merge=False)
        out[pct] = {"n": len(sub), "buckets": t["buckets"]}
    return out


def empirical_direction_freshness(rows: list[dict]) -> dict[str, Any]:
    """STEPS 3/4 — direction and LIVE/STALE empirical tables.

    OVER and UNDER are computed from disjoint row sets and never mixed;
    LIVE and STALE likewise.  Terminal pct100 rows are absent by
    construction (load_rows).  Each cell carries Wilson CIs.
    """
    def cell(sub: list[dict]) -> dict:
        n = len(sub)
        w = sum(r["y"] for r in sub)
        lo, hi = wilson_ci(w, n)
        return {"n": n, "wins": w, "losses": n - w,
                "win_rate": round(w / n, 3) if n else None,
                "wilson_lo": lo, "wilson_hi": hi}

    out: dict[str, Any] = {"by_direction": {}, "by_freshness": {}}
    for side in ("OVER", "UNDER"):
        sub = [r for r in rows if r["side"] == side]
        out["by_direction"][side] = {
            **cell(sub),
            "by_abs_band": {b: cell([r for r in sub
                                     if band_of(r["abs_raw"]) == b])
                            for b in RESIDUAL_BANDS},
            "live": cell([r for r in sub if r["market_status"] == "LIVE"]),
            "stale": cell([r for r in sub if r["market_status"] == "STALE"]),
        }
    for st in ("LIVE", "STALE"):
        sub = [r for r in rows if r["market_status"] == st]
        out["by_freshness"][st] = {
            **cell(sub),
            "over": cell([r for r in sub if r["side"] == "OVER"]),
            "under": cell([r for r in sub if r["side"] == "UNDER"]),
            "by_bucket": empirical_bucket_table(sub, merge=False)["buckets"],
        }
    return out


def block_summaries(rows: list[dict]) -> list[dict]:
    """STEP 5 — per-chronological-block empirical summaries.

    Raw directional accuracy, OVER, UNDER, LIVE and STALE accuracy per
    block (each with n + Wilson CI); blocks use the same chronological
    ordering as the walk-forward, so a game's outcome can never
    influence its own block's earlier rows (barrier enforced upstream).
    """
    def cell(sub: list[dict]) -> dict:
        n = len(sub)
        w = sum(r["y"] for r in sub)
        lo, hi = wilson_ci(w, n)
        return {"n": n, "win_rate": round(w / n, 3) if n else None,
                "wilson_lo": lo, "wilson_hi": hi}

    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_date[r["started_date"]].append(r)
    out = []
    for d in sorted(by_date):
        sub = by_date[d]
        out.append({
            "date": d,
            "games": len({r["source_game_id"] for r in sub}),
            "raw_direction": cell(sub),
            "over": cell([r for r in sub if r["side"] == "OVER"]),
            "under": cell([r for r in sub if r["side"] == "UNDER"]),
            "live": cell([r for r in sub if r["market_status"] == "LIVE"]),
            "stale": cell([r for r in sub if r["market_status"] == "STALE"]),
        })
    return out


def terminal_sanity(db_path: str,
                    classification: Optional[str] = None) -> dict[str, Any]:
    """STEP 9 — terminal sanity check (diagnostic, separately labelled).

    Recomputes pct100 rows from the same store and contrasts the
    10-90 population with the terminal one.  The pct100 population is
    MECHANICALLY inflated: the live-score floor forces fair >= live
    total, and at the terminal snapshot the live total IS the final
    total, so terminal OVER positions settle toward ~100%.  Never
    mixed into primary calibration (kept out at load_rows).
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql, params = LOAD_SQL, []
        if classification:
            sql += " AND cm.classification = ?"
            params.append(classification)
        rows = [dict(r) for r in conn.execute(
            sql + " AND cm.checkpoint_pct = 100", params)]
    finally:
        conn.close()
    diag: dict[str, Any] = {"n_rows": 0, "n_decided": 0, "wins": 0,
                            "losses": 0, "win_rate": None,
                            "wilson_lo": None, "wilson_hi": None,
                            "over_win": 0, "over_loss": 0,
                            "under_win": 0, "under_loss": 0,
                            "mechanical_note": (
        "terminal fair is floored at the live total, which at pct100 IS "
        "the final total -> terminal OVER positions are mechanically "
        "near-certain; this is the tautology, not skill")}
    decided = 0
    wins = 0
    for r in rows:
        fair, line, actual = (r["blm_fair_value"], r["live_market_line"],
                              r["actual_final_total"])
        if fair is None or line is None or actual is None:
            continue
        diag["n_rows"] += 1
        if fair == line:
            continue                      # NO_EDGE — excluded like primary
        outcome_dir = ("OVER" if actual > line else
                       "UNDER" if actual < line else "PUSH")
        if outcome_dir == "PUSH":
            continue
        side = "OVER" if fair > line else "UNDER"
        y = 1 if side == outcome_dir else 0
        decided += 1
        wins += y
        if side == "OVER":
            diag["over_win" if y else "over_loss"] += 1
        else:
            diag["under_win" if y else "under_loss"] += 1
    diag["n_decided"] = decided
    diag["wins"], diag["losses"] = wins, decided - wins
    lo, hi = wilson_ci(wins, decided)
    diag["win_rate"] = round(wins / decided, 3) if decided else None
    diag["wilson_lo"], diag["wilson_hi"] = lo, hi
    return {"terminal": diag}


def _metrics_for(pairs: list[tuple[float, int]]) -> dict[str, Any]:
    """Checkpoint-weighted metrics (each row one vote)."""
    if not pairs:
        return {"n": 0}
    ps = [p for p, _ in pairs]
    ys = [y for _, y in pairs]
    return {
        "n": len(pairs),
        "brier": brier(ps, ys),
        "log_loss": log_loss(ps, ys),
        "base_rate": round(sum(ys) / len(ys), 3),
        "reliability_bins": reliability_bins(ps, ys),
        "calibration": calibration_slope_intercept(ps, ys),
    }


# ── walk-forward engine ────────────────────────────────────────────────
def walk_forward(rows: list[dict],
                 min_history: int = MIN_HISTORY_DEFAULT,
                 refit_every: int = 1) -> dict[str, Any]:
    """Chronological walk-forward calibration (directive section 6).

    Rows arrive in (game_start, game_id, checkpoint) order.  A row is
    evaluated only when >= ``min_history`` PRIOR rows of its side exist
    from strictly-earlier completed games; the current game is evaluated
    ENTIRELY against that prior state, and only AFTER the whole game is
    scored may its rows join the calibration pool.  Consequences (all
    required): the current game's final outcome never enters its own
    calibration; its later checkpoints never recalibrate its earlier
    checkpoints; no future observation enters the calibration state.

    ``refit_every`` is a COMPUTE parameter only (default 1 = refit for
    every evaluated row): the calibrators are re-fit at most once per
    ``refit_every`` evaluated rows per side.  Every fit still uses ONLY
    strictly-prior completed games, so no information barrier is
    affected; between refits the most recent prior-data fit is reused.
    """
    games: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        games[r["source_game_id"]].append(r)
    ordered_gids = list(dict.fromkeys(r["source_game_id"] for r in rows))

    results: dict[str, Any] = {
        "evaluated": 0, "skipped_no_history": 0,
        "refits": 0, "refit_every": refit_every,
        "rows": [],
    }
    prior_by_side: dict[str, list[dict]] = {"OVER": [], "UNDER": []}
    since_fit: dict[str, int] = {"OVER": 0, "UNDER": 0}
    cached: dict[str, tuple] = {"OVER": None, "UNDER": None}

    def _fit(side: str) -> tuple:
        fit_rows = prior_by_side[side]
        xs = [t["raw"] for t in fit_rows]
        ys = [t["y"] for t in fit_rows]
        results["refits"] += 1
        return (fit_logistic(xs, ys), fit_isotonic(xs, ys), len(xs))

    for gid in ordered_gids:
        grows = games[gid]
        # evaluate the CURRENT game against the strictly-prior state FIRST
        for r in grows:
            side = r["side"]
            rec = {"game": gid, "date": r["started_date"],
                   "checkpoint_pct": r["checkpoint_pct"], "side": side,
                   "raw": r["raw"], "y": r["y"],
                   "market_status": r["market_status"]}
            if len(prior_by_side[side]) >= min_history:
                results["evaluated"] += 1
                if cached[side] is None or since_fit[side] >= refit_every:
                    cached[side] = _fit(side)
                    since_fit[side] = 0
                lg, iso, n_fit = cached[side]
                rec["p_logistic"] = predict_logistic(lg, r["raw"])
                rec["p_isotonic"] = predict_isotonic(iso, r["raw"])
                rec["logistic_intercept_only"] = lg["intercept_only"]
                rec["fit_rows"] = n_fit
                since_fit[side] += 1
            else:
                results["skipped_no_history"] += 1
                rec["p_logistic"] = None
                rec["p_isotonic"] = None
            results["rows"].append(rec)
        # only AFTER the whole game is evaluated may it join the fit pool
        for r in grows:
            prior_by_side[r["side"]].append(r)
    return results


# ── reporting ──────────────────────────────────────────────────────────
def side_report(ev_rows: list[dict], method: str) -> dict[str, Any]:
    """Full report for one side and one calibrator method.

    ``ev_rows`` are walk-forward-evaluated rows of ONE side carrying
    ``p_<method>`` predictions.
    """
    ok = [r for r in ev_rows if r.get(f"p_{method}") is not None]
    out = _metrics_for([(r[f"p_{method}"], r["y"]) for r in ok])
    if not ok:
        return out
    # per checkpoint (directive section 9)
    out["by_checkpoint"] = {
        pct: _metrics_for([(r[f"p_{method}"], r["y"])
                           for r in ok if r["checkpoint_pct"] == pct])
        for pct in PRIMARY_PCTS
        if any(r["checkpoint_pct"] == pct for r in ok)}
    # live / stale / combined (directive section 10) — MISSING cannot
    # occur: a row without a frozen line never enters the dataset.
    out["by_freshness"] = {
        st: _metrics_for([(r[f"p_{method}"], r["y"])
                          for r in ok if r["market_status"] == st])
        for st in ("LIVE", "STALE")
        if any(r["market_status"] == st for r in ok)}
    # chronological blocks (directive section 6)
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        by_date[r["date"]].append(r)
    out["by_block"] = {
        d: _metrics_for([(r[f"p_{method}"], r["y"]) for r in rr])
        for d, rr in sorted(by_date.items())}
    # game-weighted view (directive section 11): per-game metric first,
    # then the unweighted mean over games — a 9-checkpoint game counts
    # exactly once.
    by_game: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        by_game[r["game"]].append(r)
    gb = [b for b in (brier([r[f"p_{method}"] for r in g],
                            [r["y"] for r in g])
                      for g in by_game.values()) if b is not None]
    gl = [l for l in (log_loss([r[f"p_{method}"] for r in g],
                               [r["y"] for r in g])
                      for g in by_game.values()) if l is not None]
    out["game_weighted"] = {
        "games": len(by_game),
        "brier_mean_over_games": round(sum(gb) / len(gb), 4) if gb else None,
        "log_loss_mean_over_games": round(sum(gl) / len(gl), 4) if gl else None,
    }
    return out


def descriptive(rows: list[dict]) -> dict[str, Any]:
    """Outcome rates by bucket — BEFORE any calibrator (section 4).

    Monotonicity of directional success in |residual| is MEASURED, never
    assumed.
    """

    def rate(sel: list[dict]) -> dict:
        n = len(sel)
        return {"n": n,
                "win_rate": round(sum(r["y"] for r in sel) / n, 3) if n else None,
                "over_n": sum(1 for r in sel if r["side"] == "OVER"),
                "under_n": sum(1 for r in sel if r["side"] == "UNDER")}

    out: dict[str, Any] = {}
    out["by_abs_residual_band"] = {
        b: rate([r for r in rows if band_of(r["abs_raw"]) == b])
        for b in RESIDUAL_BANDS}
    out["by_signed_bucket"] = {
        ("positive_favors_over" if b == "pos" else "negative_favors_under"):
        rate([r for r in rows if (r["raw"] > 0) == (b == "pos")])
        for b in ("pos", "neg")}
    out["by_checkpoint"] = {
        pct: rate([r for r in rows if r["checkpoint_pct"] == pct])
        for pct in PRIMARY_PCTS}
    out["by_direction"] = {
        s: {**rate([r for r in rows if r["side"] == s]),
            "live_n": sum(1 for r in rows if r["side"] == s
                          and r["market_status"] == "LIVE"),
            "stale_n": sum(1 for r in rows if r["side"] == s
                           and r["market_status"] == "STALE")}
        for s in ("OVER", "UNDER")}
    by_cls: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cls[r["classification"]].append(r)
    out["by_classification"] = {
        c: rate(v) for c, v in sorted(by_cls.items())}
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_date[r["started_date"]].append(r)
    out["by_block"] = {d: rate(v) for d, v in sorted(by_date.items())}
    bands = [out["by_abs_residual_band"][b] for b in RESIDUAL_BANDS]
    rates = [b["win_rate"] for b in bands if b["n"] >= 10]
    out["monotonic_increase_with_abs_residual"] = (
        all(rates[i] <= rates[i + 1] for i in range(len(rates) - 1))
        if len(rates) >= 2 else None)
    return out


def baselines(ev_rows: list[dict]) -> dict[str, Any]:
    """Baselines on the exact walk-forward-evaluated rows (section 8)."""
    if not ev_rows:
        return {"n": 0}
    n = len(ev_rows)
    ys = [r["y"] for r in ev_rows]
    base = round(sum(ys) / n, 3)
    # outcome == OVER  iff  (side == OVER) == (y == 1)
    over_outcomes = sum(1 for r in ev_rows
                        if (r["side"] == "OVER") == (r["y"] == 1))
    return {
        "n": n,
        "raw_model_direction_hit_rate": base,
        "always_over_hit_rate": round(over_outcomes / n, 3),
        "always_under_hit_rate": round((n - over_outcomes) / n, 3),
        "constant_base_rate_brier": round(base * (1 - base), 4),
        "fade_model_hit_rate": round(1 - base, 3),
        "market_direction_note": (
            "A genuine market-direction baseline needs line-move "
            "information after the checkpoint (prohibited); on this "
            "dataset 'fade model' is its degenerate complement"),
    }


def calibration_report(db_path: str,
                       classification: Optional[str] = None) -> dict[str, Any]:
    """Top-level entry (read-only).  Returns the full verdict payload."""
    rows = load_rows(db_path, classification)
    # refit_every=25 keeps each fit strictly-prior while bounding the
    # per-row IRLS/PAV cost on large populations (compute-only; the
    # information barrier is unaffected).  The per-row history gate and
    # chronological ordering are untouched.
    wf = walk_forward(rows, refit_every=25)
    ev_rows = [r for r in wf["rows"] if r.get("p_logistic") is not None]
    over_rows = [r for r in ev_rows if r["side"] == "OVER"]
    under_rows = [r for r in ev_rows if r["side"] == "UNDER"]
    return {
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "classification_filter": classification or "ALL",
        "population": {
            "primary_rows": len(rows),
            "games": len({r["source_game_id"] for r in rows}),
            "evaluated_walk_forward": wf["evaluated"],
            "skipped_no_history": wf["skipped_no_history"],
            "over_evaluated": len(over_rows),
            "under_evaluated": len(under_rows),
        },
        "descriptive": descriptive(rows),
        "empirical": {
            "residual_buckets": empirical_bucket_table(rows),
            "by_checkpoint": empirical_checkpoint_dependence(rows),
            "direction_freshness": empirical_direction_freshness(rows),
            "blocks": block_summaries(rows),
        },
        "terminal_sanity": terminal_sanity(db_path, classification),
        "baselines": baselines(ev_rows),
        "over": {
            "logistic": side_report(over_rows, "logistic"),
            "isotonic": side_report(over_rows, "isotonic"),
        },
        "under": {
            "logistic": side_report(under_rows, "logistic"),
            "isotonic": side_report(under_rows, "isotonic"),
        },
        "read_only": True,
        "no_betting_output": True,
    }


def calibration_status(report: dict[str, Any]) -> dict[str, str]:
    """Verdict strings — CALIBRATION SLICE 1 format (descriptive).

    PASS requires: walk-forward-evaluated rows on BOTH sides (>= 30),
    logistic Brier strictly below the constant-base-rate Brier on both
    sides, and >= 1 reliability bin with n >= 20 on each side.
    """
    def side_verdict(side: dict[str, Any]) -> tuple[bool, str]:
        lg = side.get("logistic", {})
        if lg.get("n", 0) < 30 or lg.get("brier") is None:
            return False, "INSUFFICIENT EVALUATED ROWS (n>=30 required)"
        base_brier = round(lg["base_rate"] * (1 - lg["base_rate"]), 4)
        ok = lg["brier"] < base_brier
        big = any(b["n"] >= 20 for b in lg.get("reliability_bins", []))
        cal_ = lg.get("calibration", {})
        return (ok and big), (
            f"brier {lg['brier']} vs constant-base-rate {base_brier}; "
            f"slope {cal_.get('slope')}; intercept {cal_.get('intercept')}"
            + ("" if big else "; no reliability bin with n>=20"))

    over_ok, over_txt = side_verdict(report["over"])
    under_ok, under_txt = side_verdict(report["under"])
    pop = report["population"]
    emp = report.get("empirical", {})
    mono = (report.get("descriptive", {})
            .get("monotonic_increase_with_abs_residual"))
    o_lg = report["over"]["logistic"]
    u_lg = report["under"]["logistic"]
    o_iso = report["over"]["isotonic"]
    u_iso = report["under"]["isotonic"]
    o_fresh = o_lg.get("by_freshness", {})
    u_fresh = u_lg.get("by_freshness", {})
    gw = (f"checkpoint-weighted brier over {o_lg.get('brier')} / "
          f"under {u_lg.get('brier')}; game-weighted brier over "
          f"{o_lg.get('game_weighted', {}).get('brier_mean_over_games')} / "
          f"under {u_lg.get('game_weighted', {}).get('brier_mean_over_games')} "
          f"({o_lg.get('game_weighted', {}).get('games', 0)}+"
          f"{u_lg.get('game_weighted', {}).get('games', 0)} games)")
    term = report.get("terminal_sanity", {}).get("terminal", {})
    status = "PASS" if (over_ok and under_ok) else "FAIL"
    return {
        "CALIBRATION_STATUS": status,
        "EMPIRICAL_RELATIONSHIP": (
            f"residual buckets: {len(emp.get('residual_buckets', {}).get('buckets', []))} "
            f"(rule: {emp.get('residual_buckets', {}).get('merge_rule')}); "
            f"monotonic_increase_with_abs_residual={mono}"),
        "OVER_CALIBRATION": over_txt,
        "UNDER_CALIBRATION": under_txt,
        "LIVE_CALIBRATION": (
            f"OVER n={o_fresh.get('LIVE', {}).get('n', 0)} "
            f"brier={o_fresh.get('LIVE', {}).get('brier')}; "
            f"UNDER n={u_fresh.get('LIVE', {}).get('n', 0)} "
            f"brier={u_fresh.get('LIVE', {}).get('brier')}"),
        "STALE_CALIBRATION": (
            f"OVER n={o_fresh.get('STALE', {}).get('n', 0)} "
            f"brier={o_fresh.get('STALE', {}).get('brier')}; "
            f"UNDER n={u_fresh.get('STALE', {}).get('n', 0)} "
            f"brier={u_fresh.get('STALE', {}).get('brier')}"),
        "LOGISTIC": (
            f"over n={o_lg.get('n', 0)} brier={o_lg.get('brier')} "
            f"logloss={o_lg.get('log_loss')} slope="
            f"{o_lg.get('calibration', {}).get('slope')} intercept="
            f"{o_lg.get('calibration', {}).get('intercept')}; "
            f"under n={u_lg.get('n', 0)} brier={u_lg.get('brier')} "
            f"logloss={u_lg.get('log_loss')} slope="
            f"{u_lg.get('calibration', {}).get('slope')} intercept="
            f"{u_lg.get('calibration', {}).get('intercept')}"),
        "ISOTONIC": (
            f"over n={o_iso.get('n', 0)} brier={o_iso.get('brier')} "
            f"logloss={o_iso.get('log_loss')}; "
            f"under n={u_iso.get('n', 0)} brier={u_iso.get('brier')} "
            f"logloss={u_iso.get('log_loss')}"),
        "WALK-FORWARD": (
            f"evaluated {pop['evaluated_walk_forward']} rows "
            f"({pop['over_evaluated']} OVER / {pop['under_evaluated']} UNDER), "
            f"skipped {pop['skipped_no_history']} (no prior history; "
            f"per-side gate = {MIN_HISTORY_DEFAULT} prior rows)"),
        "GAME-LEVEL": gw,
        "TERMINAL EXCLUSION": (
            f"pct100 excluded from primary (n_rows={term.get('n_rows', 0)}, "
            f"decided={term.get('n_decided', 0)}, "
            f"win_rate={term.get('win_rate')}) — mechanical tautology, "
            f"reported separately only"),
        "VERDICT": "Read-only calibration research; raw signal -> "
                   "probability only; no model changes; no betting signal "
                   "(directive sections 10/11).",
    }
