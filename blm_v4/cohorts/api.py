"""Read-only cohort API (strategy doc §6, §7).

Serves the derived rescore DB (``cohort_rescore.db``) plus the frozen spec.  Every
handler opens the database ``mode=ro``; nothing here writes, mutates betting state,
or touches the live alert path.  Aggregation happens in Python because the ledger
is small by construction (one row per firing/checkpoint, not per snapshot) and the
denominator rules are easier to audit in one place.

Endpoints (all under /api/v4/cohorts):
    GET /spec          — the frozen cohort manifest (rules, version, precedence)
    GET /summary       — per-cohort scorecards with denominators + Wilson CI
    GET /ledger        — paginated ledger (firing-time evidence)
    GET /comparisons   — grouped summaries by competition/price/pace/shortfall/progress
    GET /quality       — data-quality diagnostics (unknowns, coverage, exclusions)
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from blm_v4.cohorts import metrics, spec

router = APIRouter(prefix="/api/v4/cohorts", tags=["cohorts"])

DEFAULT_DB = os.environ.get(
    "BLM_COHORT_DB",
    str(Path(__file__).resolve().parents[2] / "data" / "derived" / "cohort_rescore.db"),
)

MAX_LIMIT = 500


def _db_path() -> str:
    return os.environ.get("BLM_COHORT_DB", DEFAULT_DB)


def _connect() -> sqlite3.Connection:
    path = _db_path()
    if not os.path.exists(path):
        raise HTTPException(status_code=503, detail="cohort read-model not built")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _rows(where: str = "", params: tuple = ()) -> list[dict]:
    con = _connect()
    try:
        q = "SELECT * FROM cohort_ledger"
        if where:
            q += f" WHERE {where}"
        q += " ORDER BY observed_at, source_game_id"
        out = [dict(r) for r in con.execute(q, params)]
    finally:
        con.close()
    for r in out:
        r["qualifying_cohorts"] = json.loads(r["qualifying_cohorts"])
        r["reason_codes"] = json.loads(r["reason_codes"])
    return out


def _run_meta() -> dict:
    con = _connect()
    try:
        row = con.execute("SELECT * FROM rescore_runs ORDER BY run_at DESC LIMIT 1").fetchone()
    finally:
        con.close()
    if not row:
        return {}
    d = dict(row)
    d["reconciliation"] = json.loads(d.get("reconciliation") or "{}")
    return d


def _cohort_stats(rows: list[dict]) -> list[dict]:
    """Per-cohort scorecard. Denominators: unique games (first firing per game).

    Rules (fixed, shared with the UI):
      * a game counts once per cohort, using its FIRST reconstructed firing;
      * UNDER/OVER form the settled denominator; PUSH/VOID are reported apart;
      * pending (no authoritative final) is never a loss;
      * market coverage = rows carrying BOTH price and frozen line / rows for that cohort.
    """
    first_by_game: dict[tuple[str, str], dict] = {}
    coverage = defaultdict(lambda: [0, 0])   # code -> [with market, total]
    unknown = Counter()

    for r in rows:
        if r["status"] == "UNKNOWN":
            for code in r["reason_codes"]:
                unknown[code] += 1
        if r["source_type"] != "reconstructed_firing":
            for code in r["qualifying_cohorts"]:
                tot = coverage[code]
                tot[1] += 1
                if r["under_price"] is not None and r["trigger_line"] is not None:
                    tot[0] += 1
            continue
        for code in r["qualifying_cohorts"]:
            tot = coverage[code]
            tot[1] += 1
            if r["under_price"] is not None and r["trigger_line"] is not None:
                tot[0] += 1
            key = (r["source_game_id"], code)
            cur = first_by_game.get(key)
            if cur is None or r["observed_at"] < cur["observed_at"]:
                first_by_game[key] = r

    out = []
    for code in spec.QUALIFYING_ORDER:
        rule = spec.COHORT_RULES[code]
        games = [r for (gid, c), r in first_by_game.items() if c == code]
        outcomes = [(r["outcome"] or "UNKNOWN") for r in games]
        counts = metrics.outcome_counts(outcomes)
        cov_with, cov_total = coverage.get(code, [0, 0])
        prices = [r["under_price"] for r in games
                  if r["outcome"] in ("UNDER", "OVER") and r["under_price"]]
        out.append({
            "code": code,
            "name": rule.name,
            "blurb": rule.blurb,
            "rule": {"pace_ratio_gt": rule.pace_ratio,
                     "shortfall_gte": rule.shortfall,
                     "min_under_price_gte": rule.min_under_price},
            "unique_games": counts["total"],
            "firings": sum(1 for r in games),
            "under": counts["UNDER"], "over": counts["OVER"],
            "push": counts["PUSH"], "void": counts["VOID"],
            "pending": counts["UNKNOWN"],
            "settled": counts["settled"],
            "under_rate": counts["under_rate"],
            "wilson95": counts["wilson95"],
            "market_coverage": (cov_with / cov_total) if cov_total else None,
            "market_coverage_pct": round(100.0 * cov_with / cov_total, 1) if cov_total else None,
            "avg_trigger_price": (sum(prices) / len(prices)) if prices else None,
            "break_even_rate_at_avg_price": (metrics.break_even_rate(
                sum(prices) / len(prices)) if prices else None),
            "unknown_rows": sum(1 for r in rows
                                if r["status"] == "UNKNOWN"
                                and code in r["qualifying_cohorts"]),
        })
    return out


@router.get("/spec")
def cohort_spec():
    return spec.spec_manifest()


@router.get("/summary")
def cohort_summary(league_source: Optional[str] = Query(None, pattern="^(prior_only|full_sample)$"),
                   source_type: Optional[str] = Query(None, pattern="^(reconstructed_firing|checkpoint)$"),
                   rule_version: Optional[str] = Query(None)):
    where, params = [], []
    if league_source:
        where.append("league_source = ?"); params.append(league_source)
    if source_type:
        where.append("source_type = ?"); params.append(source_type)
    if rule_version:
        where.append("rule_version = ?"); params.append(rule_version)
    rows = _rows(" AND ".join(where), tuple(params))
    meta = _run_meta()
    return {
        "rule_version": spec.RULE_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "read_model": {"db": os.path.basename(_db_path()),
                       "run": meta.get("run_at"), "window_start": meta.get("window_start"),
                       "window_end": meta.get("window_end"),
                       "league_reference": meta.get("league_reference"),
                       "reconciliation": meta.get("reconciliation")},
        "cohorts": _cohort_stats(rows),
        "totals": {
            "ledger_rows": len(rows),
            "firings": sum(1 for r in rows if r["source_type"] == "reconstructed_firing"),
            "checkpoints": sum(1 for r in rows if r["source_type"] == "checkpoint"),
            "unknown": sum(1 for r in rows if r["status"] == "UNKNOWN"),
        },
    }


@router.get("/ledger")
def cohort_ledger(cohort: Optional[str] = Query(None),
                  source_type: Optional[str] = Query(None, pattern="^(reconstructed_firing|checkpoint)$"),
                  status: Optional[str] = Query(None, pattern="^(QUALIFIED|NOT_QUALIFIED|UNKNOWN)$"),
                  competition: Optional[str] = Query(None),
                  limit: int = Query(50, ge=1, le=MAX_LIMIT),
                  offset: int = Query(0, ge=0, le=100_000)):
    where, params = [], []
    if cohort:
        if cohort not in spec.COHORT_RULES:
            raise HTTPException(status_code=422, detail=f"unknown cohort {cohort!r}")
        where.append("qualifying_cohorts LIKE ?"); params.append(f'%"{cohort}"%')
    if source_type:
        where.append("source_type = ?"); params.append(source_type)
    if status:
        where.append("status = ?"); params.append(status)
    if competition:
        where.append("competition_slug = ?"); params.append(competition)
    rows = _rows(" AND ".join(where), tuple(params))
    total = len(rows)
    page = rows[offset:offset + limit]
    return {
        "rule_version": spec.RULE_VERSION,
        "total": total, "limit": limit, "offset": offset,
        "ordering": "observed_at ASC, source_game_id ASC",
        "rows": [{
            "source_game_id": r["source_game_id"],
            "observed_at": r["observed_at"],
            "source_type": r["source_type"],
            "status": r["status"],
            "competition_slug": r["competition_slug"],
            "classification": r["classification"],
            "tightest_cohort": r["tightest_cohort"],
            "qualifying_cohorts": r["qualifying_cohorts"],
            "progress_pct": r["progress_pct"], "scored_points": r["scored_points"],
            "required_ppm": r["required_ppm"], "actual_ppm": r["actual_ppm"],
            "league_ppm": r["league_ppm"], "pace_ratio": r["pace_ratio"],
            "shortfall": r["shortfall"], "under_price": r["under_price"],
            "trigger_line": r["trigger_line"], "trigger_observed_at": r["trigger_observed_at"],
            "outcome": r["outcome"], "final_total": r["final_total"],
            "reason_codes": r["reason_codes"], "league_source": r["league_source"],
            "rule_version": r["rule_version"],
        } for r in page],
    }


def _band(value: Optional[float], edges: list[float]) -> str:
    if value is None:
        return "unknown"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= value < hi:
            return f"{lo:g}-{hi:g}"
    return f">={edges[-1]:g}"


@router.get("/comparisons")
def cohort_comparisons(by: str = Query("competition",
                                       pattern="^(competition|price|pace_ratio|shortfall|progress)$"),
                       source_type: Optional[str] = Query("reconstructed_firing",
                                                          pattern="^(reconstructed_firing|checkpoint)$")):
    where = ["source_type = ?"]
    params = [source_type or "reconstructed_firing"]
    rows = _rows(" AND ".join(where), tuple(params))
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if not r["qualifying_cohorts"]:
            continue
        if by == "competition":
            key = r["competition_slug"] or "unknown"
        elif by == "price":
            key = _band(r["under_price"], [0, 1.6, 1.8, 2.0, 2.5, 99])
        elif by == "pace_ratio":
            key = _band(r["pace_ratio"], [0, 1.0, 1.04, 1.08, 1.12, 1.16, 99])
        elif by == "shortfall":
            key = _band(r["shortfall"], [-99, -0.5, 0, 0.25, 0.5, 99])
        else:
            key = _band(r["progress_pct"], [0, 75, 80, 85, 90, 92, 100])
        groups[key].append(r)
    out = []
    for key, grp in sorted(groups.items()):
        counts = metrics.outcome_counts([(r["outcome"] or "UNKNOWN") for r in grp])
        prices = [r["under_price"] for r in grp if r["under_price"]]
        out.append({"bucket": key, "rows": len(grp), "under": counts["UNDER"],
                    "over": counts["OVER"], "push": counts["PUSH"],
                    "pending": counts["UNKNOWN"], "settled": counts["settled"],
                    "under_rate": counts["under_rate"], "wilson95": counts["wilson95"],
                    "avg_price": (sum(prices) / len(prices)) if prices else None})
    return {"rule_version": spec.RULE_VERSION, "by": by, "source_type": source_type,
            "buckets": out}


@router.get("/quality")
def cohort_quality():
    rows = _rows()
    reasons = Counter()
    for r in rows:
        if r["status"] == "UNKNOWN":
            reasons.update(r["reason_codes"])
    bands = Counter()
    for r in rows:
        if r["progress_pct"] is not None and 75 <= r["progress_pct"] <= 92:
            bands["in_band"] += 1
        else:
            bands["out_of_band"] += 1
    meta = _run_meta()
    return {
        "rule_version": spec.RULE_VERSION,
        "ledger_rows": len(rows),
        "unknown_rows": sum(1 for r in rows if r["status"] == "UNKNOWN"),
        "unknown_reasons": dict(reasons.most_common()),
        "progress_band": dict(bands),
        "sources": dict(Counter(r["source_type"] for r in rows)),
        "league_sources": dict(Counter(r["league_source"] for r in rows)),
        "competitions": dict(Counter(r["competition_slug"] for r in rows)),
        "market_rows_with_price_and_line": sum(
            1 for r in rows if r["under_price"] is not None and r["trigger_line"] is not None),
        "run": {"run_at": meta.get("run_at"), "window_start": meta.get("window_start"),
                "window_end": meta.get("window_end"),
                "reconciliation": meta.get("reconciliation")},
        "caveats": [
            "Firings are RECONSTRUCTED from observations — this system persists no alert log.",
            "Historical rescore is descriptive, not prospective confirmation.",
            "PUSH/VOID are excluded from the settled UNDER-vs-OVER denominator.",
        ],
    }
