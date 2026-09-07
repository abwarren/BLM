"""BLM V4 — Deviation validation / empirical relationship analysis.

READ-ONLY, retrospective, research-only.  This layer answers:

    "When the live market was far from the observed score/pace
     trajectory at time T, what subsequently happened?"

It never produces a betting rule: no O/U calls, no signals, no
probabilities, no edges, no recommendations, and no predictive scoring.
Nothing here may be called "predictive" until out-of-sample validation
(performed elsewhere, in a later phase) demonstrates that it is.

Temporal separation is mandatory and structural:

  * T-state variables (market_trajectory_residual, z_score, pace_gap,
    classification, period, elapsed minutes, progress, actual pace)
    come ONLY from deviation_residuals rows, which are written once at
    T and never rewritten.
  * Hindsight outcome variables (subsequent trajectory / subsequent
    pace change / subsequent live line change / final settled total)
    come ONLY from the STORED outcome columns on the trajectory row
    (clean_projections.subsequent_* / final_settled_total) — the same
    frozen, immutably-recorded values the pace projector wrote when the
    outcome was observed.  Nothing is recomputed from later data, so
    later observations can never leak backward into a T-state or into
    the outcome attribution of an earlier observation.

Sources:
  * "gated" population  = deviation_residuals (VALID clean observations
    that passed replay / regression / snapshot-quality gating).
  * "ungated comparison" = a direct read of clean_observations (ALL
    statuses: VALID, REPLAY, STALE, INVALID_MARKET, …) that still carry
    a live line + a projection — built ONLY to show what the numbers
    would look like if replay/fake-pace frames were NOT excluded.  The
    gated-vs-ungated delta demonstrates that the relationship survives
    replay exclusion and clean-observation gating.

All outputs are deterministic functions of the stored database state:
the same database always yields the same report (stable ordering via
(captured_at, id); scatter points are the first N rows in that order,
never a random sample).
"""

from __future__ import annotations

import math
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

MODEL_VERSION = "v4-deviation-validation-1"

# Terminal classifier for trajectory rows (directive §5 — legacy-schema
# aware): the EXPLICIT stamp when present, otherwise DERIVED from the
# row's own game-time fields (full classification duration reached or
# progress 100%).  A missing stamp column is never evidence of zero
# terminal rows.
_PROJ_TERMINAL_EXPR = ("""COALESCE(p.terminal, CASE WHEN
    COALESCE(p.progress_pct, 0) >= 100.0
    OR (p.elapsed_game_minutes IS NOT NULL
        AND p.elapsed_game_minutes >= CASE COALESCE(p.classification, '')
            WHEN 'CYBER_2K26' THEN 48.0 ELSE 40.0 END)
    THEN 1 ELSE 0 END)""")

# Status of a clean observation that is allowed into the statistical
# population (mirror of clean_metrics.VALID).
VALID = "VALID"

# fixed, documented magnitude bins for |residual| (points)
_MAGNITUDE_BINS = (0.0, 2.5, 5.0, 10.0, 20.0)
_MAGNITUDE_LABELS = ("0–2.5", "2.5–5", "5–10", "10–20", "20+")
_PROGRESS_BINS = ((0.0, 25.0), (25.0, 50.0), (50.0, 75.0), (75.0, 100.01))
_PROGRESS_LABELS = ("0–25%", "25–50%", "50–75%", "75–100%")
_MIN_CONTEXT_N = 8          # contexts below this are omitted (sample too small)
_SCATTER_CAP = 1500         # deterministic cap on scatter points

_VALIDATION_NOTE = (
    "Retrospective research only — WHAT HAPPENED NEXT.  T-state variables "
    "are frozen at their observation time; subsequent pace / line movement "
    "and the final settled total are used ONLY as hindsight outcome "
    "variables (stored outcome fields, never recomputed).  Correlations and "
    "bucket means describe the historical relationship; they are NOT "
    "predictive until out-of-sample validation shows otherwise.  No O/U, "
    "edge, probability or recommendation is produced.  N is the count of "
    "eligible gated observations (deviation residuals), never total "
    "observations."
)


def _r(x: Optional[float], n: int = 4) -> Optional[float]:
    return None if x is None else round(float(x), n)


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson r.  None when fewer than 2 pairs or zero variance."""
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    return sxy / math.sqrt(sxx * syy)


def _mean(xs: list[float]) -> Optional[float]:
    return (sum(xs) / len(xs)) if xs else None


def _magnitude_bin(x: float) -> int:
    idx = 0
    for i, b in enumerate(_MAGNITUDE_BINS):
        if x >= b:
            idx = i
    return min(idx, len(_MAGNITUDE_LABELS) - 1)


class DeviationAnalysis:
    """Read-only retrospective validation over the deviation dataset."""

    def __init__(self, clean_db_path: Optional[Path] = None):
        self.db_path = Path(clean_db_path) if clean_db_path else \
            Path(__file__).resolve().parent.parent / "blm_metrics_clean.db"
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # ── gated pairs: residual rows joined to STORED outcome fields ────

    def pairs(self) -> list[dict[str, Any]]:
        """Every deviation residual row + its stored trajectory outcome
        fields (subsequent pace/line change, final settled total, and the
        T-state pace_gap from the same observation).  Ordered
        deterministically by (captured_at, id).

        TERMINAL EXCLUSION (directive): terminal rows are SETTLEMENT/AUDIT
        ONLY — they are excluded from this research dataset at the source
        (DeviationEngine never admits them) AND here at the read boundary
        (COALESCE(p.terminal, 0) = 0), so no aggregate below can ever see
        one even if a legacy residual row existed."""
        with self._lock:
            conn = self._connect()
            try:
                return [dict(r) for r in conn.execute(
                    """SELECT r.id AS residual_id,
                              r.observation_id,
                              r.source_game_id,
                              r.classification,
                              r.period,
                              r.captured_at,
                              r.elapsed_game_minutes,
                              r.progress_pct,
                              r.actual_pts_per_min,
                              r.live_total_line,
                              r.projected_final_total,
                              r.market_trajectory_residual,
                              r.z_score,
                              p.pace_gap,
                              p.subsequent_actual_pace,
                              p.subsequent_pace_change,
                              p.subsequent_live_line,
                              p.subsequent_live_line_change,
                              p.final_settled_total,
                              p.trajectory_state
                       FROM deviation_residuals r
                       JOIN clean_projections p
                         ON p.observation_id = r.observation_id
                       WHERE {_PROJ_TERMINAL_EXPR} = 0
                       ORDER BY r.captured_at, r.id""".format(
                           _PROJ_TERMINAL_EXPR=_PROJ_TERMINAL_EXPR)
                ).fetchall()]
            finally:
                conn.close()

    def terminal_population(self) -> dict[str, int]:
        """Audit counters for the terminal-exclusion boundary: how many
        residual rows exist in total vs how many are non-terminal
        (predictive-eligible).  Under the directive the terminal count in
        the research dataset MUST be 0 — the DeviationEngine gate and the
        read boundary above both exclude them."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    f"""SELECT COUNT(*) AS total,
                              COALESCE(SUM(CASE WHEN {_PROJ_TERMINAL_EXPR} = 1
                                           THEN 1 ELSE 0 END), 0) AS terminal
                       FROM deviation_residuals r
                       JOIN clean_projections p
                         ON p.observation_id = r.observation_id""").fetchone()
                total = int(row["total"])
                terminal = int(row["terminal"])
                obs = conn.execute(
                    """SELECT COUNT(*) AS total,
                              COALESCE(SUM(terminal), 0) AS terminal,
                              COALESCE(SUM(CASE WHEN status = 'VALID'
                                           THEN 1 ELSE 0 END), 0) AS valid
                       FROM clean_observations""").fetchone()
                return {
                    "clean_observations_total": int(obs["total"]),
                    "clean_observations_terminal": int(obs["terminal"]),
                    "clean_observations_valid": int(obs["valid"]),
                    "deviation_residual_rows": total,
                    "deviation_residual_terminal": terminal,
                    "deviation_residual_predictive_eligible": total - terminal,
                }
            finally:
                conn.close()

    # ── ungated comparison: all clean_observations (incl. replays) ────

    def ungated_rows(self) -> list[dict[str, Any]]:
        """Direct clean_observations read: every row with a live line AND a
        projection (ALL statuses).  Used ONLY to quantify what replay /
        stale / regressed frames would add if they were NOT gated out.
        The terminal flag is carried per row (audit display only)."""
        with self._lock:
            conn = self._connect()
            try:
                rows = [dict(r) for r in conn.execute(
                    """SELECT o.source_game_id, o.captured_at, o.classification,
                              o.status,
                              COALESCE(o.terminal, 0) AS terminal,
                              o.live_total_line,
                              o.projected_final_total,
                              g.final_total
                       FROM clean_observations o
                       LEFT JOIN clean_games g ON g.source_game_id = o.source_game_id
                       WHERE o.live_total_line IS NOT NULL
                         AND o.projected_final_total IS NOT NULL
                       ORDER BY o.captured_at, o.id""").fetchall()]
            finally:
                conn.close()
        for r in rows:
            r["market_trajectory_residual"] = (
                round(float(r["live_total_line"])
                      - float(r["projected_final_total"]), 2)
                if r["live_total_line"] is not None
                and r["projected_final_total"] is not None else None)
        return rows

    # ── aggregates ───────────────────────────────────────────────────

    @staticmethod
    def _corr(pairs: list[dict[str, Any]], xk: str, yk: str) -> dict[str, Any]:
        xs = [p[xk] for p in pairs if p.get(xk) is not None and p.get(yk) is not None]
        ys = [p[yk] for p in pairs if p.get(xk) is not None and p.get(yk) is not None]
        r = pearson(xs, ys)
        return {"r": _r(r) if r is not None else None, "n": len(xs)}

    @staticmethod
    def _corr_settle(pairs: list[dict[str, Any]], xk: str) -> dict[str, Any]:
        """Correlation of ``xk`` with the SETTLEMENT ERROR
        (final settled total - projected final total).  The raw settled
        total itself is a constant per game and carries no information;
        the error against the T-state projection is the outcome."""
        xs = [p[xk] for p in pairs
              if p.get(xk) is not None and p.get("final_settled_total") is not None
              and p.get("projected_final_total") is not None]
        ys = [p["final_settled_total"] - p["projected_final_total"]
              for p in pairs
              if p.get(xk) is not None and p.get("final_settled_total") is not None
              and p.get("projected_final_total") is not None]
        r = pearson(xs, ys)
        return {"r": _r(r) if r is not None else None, "n": len(xs)}

    def validation(self) -> dict[str, Any]:
        pairs = self.pairs()
        gated = [p for p in pairs]

        def has(p: dict, k: str) -> bool:
            return p.get(k) is not None

        with_subseq = [p for p in gated if has(p, "subsequent_pace_change")]
        with_settle = [p for p in gated if has(p, "final_settled_total")]

        # ── sign buckets: does residual SIGN relate to what happened? ──
        sign_buckets = []
        for label, sel in (("negative", lambda p: p["market_trajectory_residual"] < 0),
                           ("zero", lambda p: p["market_trajectory_residual"] == 0),
                           ("positive", lambda p: p["market_trajectory_residual"] > 0)):
            sel_pairs = [p for p in gated if sel(p)]
            sub = [p for p in sel_pairs if has(p, "subsequent_pace_change")]
            line = [p for p in sel_pairs if has(p, "subsequent_live_line_change")]
            settle = [p for p in sel_pairs if has(p, "final_settled_total")]
            sign_buckets.append({
                "bucket": label,
                "n": len(sel_pairs),
                "mean_subsequent_pace_change": _r(_mean(
                    [p["subsequent_pace_change"] for p in sub]), 4),
                "mean_subsequent_live_line_change": _r(_mean(
                    [p["subsequent_live_line_change"] for p in line]), 4),
                "mean_settlement_error": _r(_mean(
                    [p["final_settled_total"] - p["projected_final_total"]
                     for p in settle]), 4),
            })

        # ── magnitude bins: does |residual| relate to movement? ──────
        mag_buckets = []
        for idx, label in enumerate(_MAGNITUDE_LABELS):
            sel_pairs = [p for p in gated
                         if _magnitude_bin(abs(p["market_trajectory_residual"])) == idx]
            sub = [p for p in sel_pairs if has(p, "subsequent_pace_change")]
            settle = [p for p in sel_pairs if has(p, "final_settled_total")]
            mag_buckets.append({
                "bin": label,
                "n": len(sel_pairs),
                "mean_abs_residual": _r(_mean(
                    [abs(p["market_trajectory_residual"]) for p in sel_pairs]), 2),
                "mean_subsequent_pace_change": _r(_mean(
                    [p["subsequent_pace_change"] for p in sub]), 4),
                "mean_settlement_error": _r(_mean(
                    [p["final_settled_total"] - p["projected_final_total"]
                     for p in settle]), 4),
            })

        # ── monotonicity of |residual| vs subsequent pace change ─────
        means = [b["mean_subsequent_pace_change"] for b in mag_buckets
                 if b["n"] >= 10 and b["mean_subsequent_pace_change"] is not None]
        monotonic = "insufficient data"
        if len(means) >= 2:
            diffs = [means[i + 1] - means[i] for i in range(len(means) - 1)]
            non_zero = [d for d in diffs if abs(d) > 1e-12]
            if not non_zero:
                monotonic = "flat (no relationship)"
            elif all(d > 0 for d in non_zero):
                monotonic = "monotonic increasing"
            elif all(d < 0 for d in non_zero):
                monotonic = "monotonic decreasing"
            else:
                monotonic = "non-monotonic"

        # ── correlations (residual vs z, each vs outcomes) ───────────
        correlations = {
            "residual_vs_subsequent_pace": self._corr(
                gated, "market_trajectory_residual", "subsequent_pace_change"),
            "residual_vs_settlement": self._corr_settle(
                gated, "market_trajectory_residual"),
            "z_vs_subsequent_pace": self._corr(
                gated, "z_score", "subsequent_pace_change"),
            "z_vs_settlement": self._corr_settle(gated, "z_score"),
            "abs_residual_vs_abs_subseq_pace": _abs_corr(gated),
        }

        # ── by league/period context ─────────────────────────────────
        ctx: dict[str, list[dict]] = {}
        for p in gated:
            key = f"{p['classification']}|{p['period'] or 'UNK'}"
            ctx.setdefault(key, []).append(p)
        by_context = []
        for key in sorted(ctx):
            grp = ctx[key]
            sub = [p for p in grp if has(p, "subsequent_pace_change")]
            by_context.append({
                "key": key,
                "n": len(grp),
                **self._corr(grp, "market_trajectory_residual", "subsequent_pace_change"),
                "mean_subsequent_pace_change": _r(_mean(
                    [p["subsequent_pace_change"] for p in sub]), 4),
                "sample_too_small": len(sub) < _MIN_CONTEXT_N,
            })

        # ── stability across elapsed-game-time buckets ───────────────
        by_progress = []
        for (lo, hi), label in zip(_PROGRESS_BINS, _PROGRESS_LABELS):
            grp = [p for p in gated
                   if p.get("progress_pct") is not None
                   and lo <= p["progress_pct"] < hi]
            by_progress.append({
                "bucket": label,
                "n": len(grp),
                **self._corr(grp, "market_trajectory_residual", "subsequent_pace_change"),
                "mean_subsequent_pace_change": _r(_mean(
                    [p["subsequent_pace_change"] for p in grp
                     if has(p, "subsequent_pace_change")]), 4),
            })

        # ── ungated comparison (what replay/stale frames WOULD add) ──
        ungated = self.ungated_rows()
        by_status: dict[str, int] = {}
        for r in ungated:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        n_excluded = sum(v for k, v in by_status.items() if k != VALID)
        # terminal population counters (directive section 6): the research
        # dataset must contain 0 terminal rows; terminal rows are reported
        # separately as settlement/audit population.
        term = self.terminal_population()
        ung_settle = [r for r in ungated if r.get("final_total") is not None
                      and r.get("market_trajectory_residual") is not None]
        xs = [r["market_trajectory_residual"] for r in ung_settle]
        ys = [r["final_total"] - r["projected_final_total"] for r in ung_settle]
        r_u = pearson(xs, ys)

        # ── deterministic scatter points (first N in stable order) ───
        def scatter(pairs: list[dict], xk: str, yk: str) -> list[list[float]]:
            pts = [[p[xk], p[yk]] for p in pairs
                   if p.get(xk) is not None and p.get(yk) is not None]
            return pts[:_SCATTER_CAP]

        return {
            "section": "deviation_validation",
            "model_version": MODEL_VERSION,
            "note": _VALIDATION_NOTE,
            "n_gated_rows": len(gated),
            "n_with_subsequent": len(with_subseq),
            "n_with_settlement": len(with_settle),
            "n_terminal_excluded": term["deviation_residual_terminal"],
            "n_predictive_eligible": len(gated),
            "terminal_population": term,
            "n_ungated_rows": len(ungated),
            "n_ungated_valid": by_status.get(VALID, 0),
            "n_excluded_non_valid": n_excluded,
            "excluded_by_status": by_status,
            "sign_buckets": sign_buckets,
            "magnitude_buckets": mag_buckets,
            "monotonicity": {
                "assessment": monotonic,
                "magnitude_bucket_means": [b["mean_subsequent_pace_change"]
                                           for b in mag_buckets],
            },
            "correlations": correlations,
            "by_context": by_context,
            "by_progress": by_progress,
            "ungated_comparison": {
                "rows": len(ungated),
                "by_status": by_status,
                "corr_residual_vs_settlement": {
                    "r": _r(r_u) if r_u is not None else None,
                    "n": len(ung_settle),
                },
            },
            "scatter": {
                "residual_vs_subseq_pace": scatter(
                    gated, "market_trajectory_residual", "subsequent_pace_change"),
                "z_vs_subseq_pace": scatter(gated, "z_score", "subsequent_pace_change"),
            },
        }


def _abs_corr(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """|residual| vs |subsequent pace change| correlation."""
    xs = [abs(p["market_trajectory_residual"]) for p in pairs
          if p.get("market_trajectory_residual") is not None
          and p.get("subsequent_pace_change") is not None]
    ys = [abs(p["subsequent_pace_change"]) for p in pairs
          if p.get("market_trajectory_residual") is not None
          and p.get("subsequent_pace_change") is not None]
    r = pearson(xs, ys)
    return {"r": _r(r) if r is not None else None, "n": len(xs)}