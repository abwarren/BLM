"""BLM V4 — Historical market & time-of-day trend analytics.

Pure read-only aggregations over market_history + prediction_scores.

These are OBSERVATIONS, never model rules: the hourly and grouped
buckets are analytical views only.  Every percentage is reported WITH
its sample size; a line that is missing simply excludes the game from
that metric — market data is never substituted from the model.

The analytical timezone is explicit and configurable (BLM_ANALYTICS_TZ,
default Africa/Johannesburg); grouped periods are configurable via
BLM_TREND_GROUPS='[[1,5],[5,9],[10,13],[13,17],[18,23]]'.
"""

from __future__ import annotations

import json
import os
import sqlite3
from statistics import median
from typing import Any, Optional

DEFAULT_ANALYTICS_TZ = "Africa/Johannesburg"

# (start_hour, end_hour) inclusive, local time — analytical views ONLY.
DEFAULT_GROUPS: list[tuple[int, int]] = [
    (1, 5), (5, 9), (10, 13), (13, 17), (18, 23),
]


def analytics_tz() -> str:
    return os.environ.get("BLM_ANALYTICS_TZ", DEFAULT_ANALYTICS_TZ)


def grouped_periods() -> list[tuple[int, int]]:
    raw = os.environ.get("BLM_TREND_GROUPS")
    if raw:
        try:
            return [(int(a), int(b)) for a, b in json.loads(raw)]
        except Exception:
            pass
    return list(DEFAULT_GROUPS)


def _pct(part: int, n: int) -> Optional[float]:
    return round(part / n * 100, 1) if n else None


def _side_stats(items: list[tuple[Optional[str], Optional[float]]]) -> dict[str, Any]:
    """(outcome, edge) pairs -> counts + percentages WITH sample size."""
    n = len(items)
    over = sum(1 for o, _ in items if o == "OVER")
    under = sum(1 for o, _ in items if o == "UNDER")
    push = sum(1 for o, _ in items if o == "PUSH")
    edges = [e for _, e in items if e is not None]
    return {
        "n": n,
        "over": over, "under": under, "push": push,
        "over_pct": _pct(over, n), "under_pct": _pct(under, n),
        "push_pct": _pct(push, n),
        "avg_edge": round(sum(edges) / len(edges), 2) if edges else None,
        "median_edge": round(median(edges), 2) if edges else None,
    }


def _bucket_report(rows: list[sqlite3.Row]) -> dict[str, Any]:
    """One time bucket: counts vs OLVC and vs CLV, percentages with
    sample sizes, avg/median deltas, closing-line MAE."""
    olvc = [(r["outcome_olvc"], r["opening_total_edge"]) for r in rows
            if r["outcome_olvc"] is not None]
    clv = [(r["outcome_clv"], r["closing_total_edge"]) for r in rows
           if r["outcome_clv"] is not None]
    deltas_clv = [e for _, e in clv if e is not None]
    mae = round(sum(abs(e) for e in deltas_clv) / len(deltas_clv), 2) \
        if deltas_clv else None
    o = _side_stats(olvc)
    c = _side_stats(clv)
    return {
        "games": len(rows),
        "olvc_n": o["n"], "clv_n": c["n"],
        "over_olvc": o["over"], "under_olvc": o["under"], "push_olvc": o["push"],
        "over_clv": c["over"], "under_clv": c["under"], "push_clv": c["push"],
        "over_pct_clv": c["over_pct"], "under_pct_clv": c["under_pct"],
        "push_pct_clv": c["push_pct"],
        "avg_delta_olvc": o["avg_edge"], "avg_delta_clv": c["avg_edge"],
        "median_delta_clv": c["median_edge"], "mae_clv": mae,
        # soft confidence proxy: sample size relative to a 50-game target
        "confidence": round(min(1.0, c["n"] / 50.0), 2),
    }


def market_performance(conn: sqlite3.Connection) -> dict[str, Any]:
    """OVER/UNDER/PUSH vs OLVC and vs CLV across all clean games."""
    rows = conn.execute(
        """SELECT outcome_olvc, outcome_clv,
                  opening_total_edge, closing_total_edge
           FROM market_history
           WHERE opening_total IS NOT NULL OR closing_total IS NOT NULL"""
    ).fetchall()
    return {
        "olvc": _side_stats(
            [(r["outcome_olvc"], r["opening_total_edge"]) for r in rows
             if r["outcome_olvc"] is not None]),
        "clv": _side_stats(
            [(r["outcome_clv"], r["closing_total_edge"]) for r in rows
             if r["outcome_clv"] is not None]),
    }


def time_of_day(conn: sqlite3.Connection) -> dict[str, Any]:
    """Hourly + grouped time-of-day buckets (local analytics timezone)."""
    rows = conn.execute(
        """SELECT started_hour, outcome_olvc, outcome_clv,
                  opening_total_edge, closing_total_edge
           FROM market_history"""
    ).fetchall()
    hourly = []
    for h in range(24):
        bucket = [r for r in rows if r["started_hour"] == h]
        hourly.append({
            "hour": f"{h:02d}-{(h + 1) % 24:02d}",
            **_bucket_report(bucket),
        })
    grouped = []
    for a, b in grouped_periods():
        bucket = [r for r in rows if a <= r["started_hour"] <= b]
        grouped.append({
            "period": f"{a:02d}-{b:02d}",
            **_bucket_report(bucket),
        })
    return {"hourly": hourly, "grouped": grouped}


def market_movement(conn: sqlite3.Connection) -> dict[str, Any]:
    """Opening->closing line movement and final results by direction."""
    rows = conn.execute(
        """SELECT total_line_move, market_move, outcome_clv
           FROM market_history
           WHERE opening_total IS NOT NULL AND closing_total IS NOT NULL"""
    ).fetchall()
    moves = [r["total_line_move"] for r in rows
             if r["total_line_move"] is not None]
    by_dir: dict[str, Any] = {}
    for d in ("UP", "DOWN", "UNCHANGED"):
        sub = [r for r in rows if r["market_move"] == d]
        by_dir[d] = {
            "n": len(sub),
            "over_clv": sum(1 for r in sub if r["outcome_clv"] == "OVER"),
            "under_clv": sum(1 for r in sub if r["outcome_clv"] == "UNDER"),
            "push_clv": sum(1 for r in sub if r["outcome_clv"] == "PUSH"),
        }
    return {
        "n": len(rows),
        "avg_move": round(sum(moves) / len(moves), 2) if moves else None,
        "median_move": round(median(moves), 2) if moves else None,
        "up": by_dir["UP"]["n"], "down": by_dir["DOWN"]["n"],
        "unchanged": by_dir["UNCHANGED"]["n"],
        "by_direction": by_dir,
    }


def model_vs_market(conn: sqlite3.Connection) -> dict[str, Any]:
    """Model edge + directional hit rate vs the market, clean games only,
    split by model version and checkpoint."""
    rows = conn.execute(
        """SELECT ps.model_version, p.checkpoint, ps.model_total,
                  ps.market_total, ps.ou_prediction, ps.ou_correct,
                  ps.model_beat_market
           FROM prediction_scores ps
           JOIN predictions p ON p.id = ps.prediction_id
           JOIN market_history mh ON mh.source_game_id = ps.source_game_id
           WHERE ps.market_total IS NOT NULL"""
    ).fetchall()
    versions: dict[str, Any] = {}
    for r in rows:
        v = versions.setdefault(r["model_version"], {
            "n": 0, "_edges": [], "model_over": 0, "model_under": 0,
            "model_push": 0, "dir_hits": 0, "dir_valid": 0,
            "beat_market": 0, "beat_valid": 0, "by_checkpoint": {},
        })
        v["n"] += 1
        if r["model_total"] is not None and r["market_total"] is not None:
            v["_edges"].append(round(r["model_total"] - r["market_total"], 2))
        if r["ou_prediction"] == 1:
            v["model_over"] += 1
        elif r["ou_prediction"] == -1:
            v["model_under"] += 1
        else:
            v["model_push"] += 1
        if r["ou_correct"] is not None:
            v["dir_hits"] += r["ou_correct"]
            v["dir_valid"] += 1
        if r["model_beat_market"] is not None:
            v["beat_market"] += r["model_beat_market"]
            v["beat_valid"] += 1
        cp = v["by_checkpoint"].setdefault(r["checkpoint"], {
            "n": 0, "dir_hits": 0, "dir_valid": 0})
        cp["n"] += 1
        if r["ou_correct"] is not None:
            cp["dir_hits"] += r["ou_correct"]
            cp["dir_valid"] += 1
    for v in versions.values():
        edges = v.pop("_edges")
        v["avg_model_edge"] = round(sum(edges) / len(edges), 2) if edges else None
        v["model_over_pct"] = _pct(v["model_over"], v["n"])
        v["model_under_pct"] = _pct(v["model_under"], v["n"])
        v["dir_hit_rate"] = _pct(v["dir_hits"], v["dir_valid"])
        v["beat_market_rate"] = _pct(v["beat_market"], v["beat_valid"])
        for cp in v["by_checkpoint"].values():
            cp["dir_hit_rate"] = _pct(cp["dir_hits"], cp["dir_valid"])
    return {"by_version": versions}
