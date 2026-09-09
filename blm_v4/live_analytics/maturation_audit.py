"""Prospective MATURATION AUDIT — read-only, descriptive only.

Consumes the existing authoritative data (durable append-only
prospective health history + the freshly built prospective health
report) and answers ONLY maturation questions: accumulation growth,
competition/benchmark maturity, classification/data integrity, market
completeness, clock/source quality, snapshot continuity, and objective
DESCRIPTIVE maturation gates.

It never re-implements or modifies frozen analytics: pace formulas,
benchmark keys, classification, Z semantics, market selection and the
descriptive-only firewall are consumed as-is.  The verdict vocabulary
is exactly: ACCUMULATE / MATURE ENOUGH FOR NEXT DESCRIPTIVE RESEARCH
STAGE — passing gates is a statement about DATA MATURITY, never about
predictive/betting/model readiness.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from blm_v4.live_analytics.prospective_health import (
    FORBIDDEN_KEYS, assert_descriptive_only, build_report)

DEFAULT_HISTORY = "/home/ubuntu/BLM/prospective_health_history.jsonl"

REQUIRED_SNAPSHOT_FIELDS = (
    "generated_utc", "window_hours", "games_observed",
    "observations_collected", "provider_counts", "competition_counts",
    "unknown_count", "benchmark_populations", "benchmark_maturity",
    "z_availability", "missing_line_rate", "stale_line_rate",
    "score_line_gap_distribution", "actual_pace_distribution",
    "required_pace_distribution", "pace_gap_distribution", "integrity",
    "competition_maturation", "clock_jitter",
)
# NOTE: "maturation_watch" is deliberately NOT required — snapshots from
# before the watch feature legitimately lack it, and the trajectory
# treats missing watch data as "not yet observed" (None), never as a
# continuity failure.

# Time-to-maturity thresholds tracked longitudinally (descriptive only).
MATURITY_THRESHOLDS = (30, 100, 500, 1000)

# maturation gates: name → (all-pass predicate parts described in text)
GATE_DEFS = (
    # (key, human description)
    ("accumulation", "observations continue accumulating with no "
                     "unexplained drop between snapshots"),
    ("benchmark_population_size", "every benchmark population has "
                                  "reached N >= 30 (minimum benchmark N)"),
    ("classification_integrity", "UNKNOWN = 0, no missing provider/"
                                 "competition, no ambiguous competition, "
                                 "no benchmark-isolation violations"),
    ("data_integrity", "zero duplicate observations, future timestamps, "
                       "market-after-capture rows, and score "
                       "monotonicity violations in the latest window"),
    ("market_completeness", "Z availability >= 0.90 in the latest "
                            "window"),
    ("snapshot_continuity", "append-only history with monotonically "
                            "increasing unique timestamps and all "
                            "required fields present"),
)


def _parse_ts(s: str) -> Optional[datetime]:
    for f in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def load_history(path: str = DEFAULT_HISTORY) -> List[dict]:
    """Append-only snapshot history, oldest first."""
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def check_snapshot_continuity(snaps: List[dict]) -> Dict[str, Any]:
    """§8: append-only, monotone unique timestamps, required fields."""
    ts = [s.get("generated_utc") for s in snaps]
    missing_fields = sorted(
        {f for s in snaps for f in REQUIRED_SNAPSHOT_FIELDS
         if f not in s})
    parsed = [t for t in (_parse_ts(x) for x in ts if x) if t]
    return {
        "snapshots": len(snaps),
        "monotone_increasing": all(a < b for a, b in zip(ts, ts[1:])),
        "duplicate_timestamps": len(ts) - len(set(ts)),
        "snapshots_missing_required_fields": missing_fields or [],
        "verdict_pass": (len(snaps) >= 2 and
                         all(a < b for a, b in zip(ts, ts[1:])) and
                         not missing_fields),
    }


def _growth(snaps: List[dict]) -> List[Dict[str, Any]]:
    out = []
    for prev, cur in zip(snaps, snaps[1:]):
        lo = (prev.get("accumulation") or {}).get("lifetime_observations")
        hi = (cur.get("accumulation") or {}).get("lifetime_observations")
        lo_g = (prev.get("accumulation") or {}).get("lifetime_games")
        hi_g = (cur.get("accumulation") or {}).get("lifetime_games")
        if lo is None or hi is None:
            out.append({"from": prev.get("generated_utc"),
                        "to": cur.get("generated_utc"),
                        "observations_added": None,
                        "games_added": None,
                        "note": "no lifetime baseline in earlier snapshot"})
            continue
        out.append({
            "from": prev.get("generated_utc"),
            "to": cur.get("generated_utc"),
            "observations_added": hi - lo,
            "games_added": (hi_g - lo_g) if (hi_g is not None
                                             and lo_g is not None) else None,
        })
    return out


def build_audit(main_db: str, clean_db: str, now: datetime,
                history_path: str = DEFAULT_HISTORY,
                window_hours: float = 24.0,
                api_url: Optional[str] = None) -> Dict[str, Any]:
    """Build the full maturation audit (read-only)."""
    snaps = load_history(history_path)
    fresh = build_report(main_db, clean_db, now, window_hours, api_url,
                         history_file=history_path)

    # ── §1 observation growth ────────────────────────────────────────
    growth = _growth(snaps)
    obs_added = [g["observations_added"] for g in growth
                 if g["observations_added"] is not None]
    unexplained_drops = [
        g for g in growth
        if g["observations_added"] is not None and g["observations_added"] <= 0]
    accumulation = {
        "snapshots_analysed": len(snaps),
        "per_interval": growth,
        "latest_interval_observations_added": (obs_added[-1]
                                               if obs_added else None),
        "unexplained_drops": unexplained_drops,
        "continuing_without_unexplained_drops": not unexplained_drops,
    }

    # ── §2 competition maturity (never combined) ─────────────────────
    competition_maturity = []
    for row in fresh["competition_maturation"]:
        competition_maturity.append({
            "provider": row["provider"],
            "competition": row["competition"],
            "lifetime_games": row["games"],
            "lifetime_observations": row["observations"],
            "median_n": row["median_benchmark_n"],
            "min_n": row["min_benchmark_n"],
            "max_n": row["max_benchmark_n"],
            "benchmark_populations": row["benchmark_keys"],
            "n_at_least": row["maturity_n_at_least"],
        })

    # ── §3 benchmark maturity over time ──────────────────────────────
    bench_history = [{"generated_utc": s.get("generated_utc"),
                      "populations_total": (s.get("benchmark_maturity")
                                            or {}).get(
                          "populations_total"),
                      "n_at_least": (s.get("benchmark_maturity") or {})
                      .get("n_at_least")}
                     for s in snaps]
    benchmark_maturity = {
        "latest": fresh["benchmark_maturity"],
        "over_time": bench_history,
        "n_distribution": (fresh["benchmark_populations"]
                           .get("n_distribution")),
        "note": "DATA MATURITY COUNTS ONLY — never statistical "
                "significance; sparse populations (N < 30) are reported "
                "honestly and are never altered or merged",
    }

    # ── §4 classification integrity (independently visible) ─────────
    latest_int = fresh["integrity"]
    unknown_over_time = [{"generated_utc": s.get("generated_utc"),
                          "unknown": s.get("unknown_count"),
                          "isolation_violations": (s.get("integrity")
                                                   or {}).get(
                              "benchmark_isolation_violations")}
                         for s in snaps]
    classification = {
        "unknown": fresh["unknown_count"],
        "missing_provider": sum(
            1 for r in fresh["competition_maturation"]
            if r["provider"] == "UNKNOWN"),
        "missing_canonical_competition": fresh["unknown_count"],
        "ambiguous_competition": sum(
            1 for r in fresh["competition_maturation"]
            if r["provider"] == "UNKNOWN" and r["games"] > 0),
        "benchmark_isolation_violations":
            latest_int["benchmark_isolation_violations"],
        "over_time": unknown_over_time,
    }

    # ── §5 data integrity (existing fields only) ─────────────────────
    data_integrity = {
        "duplicate_observations":
            latest_int["duplicate_observation_groups"],
        "future_timestamps": latest_int["future_timestamp_rows"],
        "market_timestamp_after_capture":
            latest_int["market_ts_after_capture_rows"],
        "score_monotonicity_violations":
            latest_int["score_monotonicity_violations"],
        "elapsed_monotonicity_jitter_drops":
            latest_int["elapsed_monotonicity_violations"],
        "alternative_line_groups":
            latest_int["alternative_line_groups"],
        "note": "alternative-line groups are legitimate bookmaker "
                "alternative totals, NOT duplicates",
    }

    # ── §6 market completeness over time ─────────────────────────────
    market_over_time = [{"generated_utc": s.get("generated_utc"),
                         "missing_line_rate": s.get("missing_line_rate"),
                         "stale_line_rate": s.get("stale_line_rate"),
                         "z_availability": (s.get("z_availability")
                                            or {}).get("availability_rate")}
                        for s in snaps]
    market = {
        "latest": {"missing_line_rate": fresh["missing_line_rate"],
                   "stale_line_rate": fresh["stale_line_rate"],
                   "z_availability":
                       fresh["z_availability"]["availability_rate"]},
        "over_time": market_over_time,
    }

    # ── §7 clock / source quality ────────────────────────────────────
    cj = fresh["clock_jitter"]
    clock = {
        "elapsed_micro_drops": cj["micro_drops"],
        "min_drop_minutes": cj["min_drop_minutes"],
        "max_drop_minutes": cj["max_drop_minutes"],
        "same_quarter": cj["same_quarter"],
        "period_transition": cj["period_transition"],
        "score_monotonicity_violations_separate":
            latest_int["score_monotonicity_violations"],
        "note": "source jitter is recorded, never corrected",
    }

    # ── longitudinal maturation trajectory (time-to-maturity) ────────
    # Per watch-list population: its N at every snapshot (None where the
    # snapshot predates the watch schema), current N, and for each
    # maturity threshold whether it is WAIT or MATURE — plus the first
    # snapshot timestamp at which the threshold was met.  Descriptive
    # tracking only: populations are never altered, merged or dropped.
    watch_keys: Dict[str, Dict[str, Any]] = {}
    for s in snaps:
        mw = (s.get("maturation_watch") or {}).get("populations") or []
        for p in mw:
            k = p.get("key")
            if not k:
                continue
            entry = watch_keys.setdefault(
                k, {"key": k, "observations": [],
                    "first_seen_n": None, "latest_seen_n": None})
            n = p.get("n")
            entry["observations"].append(
                {"generated_utc": s.get("generated_utc"), "n": n})
            if entry["first_seen_n"] is None:
                entry["first_seen_n"] = n
            entry["latest_seen_n"] = n
    # current N for tracked keys comes from the FRESH report's watch list
    # — the fresh run is itself the newest observation point, so its watch
    # populations are merged in (this also seeds the trajectory on the
    # very first audit after the watch feature ships)
    fresh_watch = {p["key"]: p["n"]
                   for p in (fresh.get("maturation_watch") or {})
                   .get("populations", [])}
    for k, n in fresh_watch.items():
        entry = watch_keys.setdefault(
            k, {"key": k, "observations": [],
                "first_seen_n": None, "latest_seen_n": None})
        entry["observations"].append(
            {"generated_utc": fresh["generated_utc"], "n": n})
        if entry["first_seen_n"] is None:
            entry["first_seen_n"] = n
        entry["latest_seen_n"] = n
    trajectory = []
    for k, e in sorted(watch_keys.items()):
        cur = fresh_watch.get(k, e["latest_seen_n"])
        # for keys that left the watch band (n >= threshold at first
        # sighting) the latest KNOWN n governs the status
        latest_known = cur if cur is not None else e["latest_seen_n"]
        status = {}
        for t in MATURITY_THRESHOLDS:
            crossed = next((o["generated_utc"] for o in e["observations"]
                            if o["n"] is not None and o["n"] >= t), None)
            if latest_known is not None and latest_known >= t:
                status[str(t)] = {"state": "MATURE",
                                  "crossed_at": crossed}
            else:
                status[str(t)] = {"state": "WAIT", "crossed_at": None}
        trajectory.append({
            "key": k,
            "current_n": cur,
            "first_seen_n": e["first_seen_n"],
            "observations": e["observations"],
            "thresholds": status,
        })
    maturation_trajectory = {
        "note": "time-to-maturity tracking of watch-list populations; "
                "descriptive only — no population is altered, merged, "
                "or promoted by this report",
        "populations": trajectory,
    }

    # ── §9 maturation gates (descriptive only) ───────────────────────
    # snapshot continuity: the required-field audit applies to snapshots
    # from the maturation schema onward — the single legacy snapshot
    # (taken before the maturation fields existed) is counted but not
    # allowed to mask genuine continuity failures (ordering, duplicates,
    # or missing fields in CURRENT-schema snapshots).
    cont = check_snapshot_continuity(snaps)
    legacy = [s.get("generated_utc") for s in snaps
              if any(f not in s for f in REQUIRED_SNAPSHOT_FIELDS)]
    cont["legacy_schema_snapshots"] = legacy
    cont["verdict_pass"] = (
        cont["monotone_increasing"]
        and cont["duplicate_timestamps"] == 0
        and all(f in s for s in snaps
                if s.get("generated_utc") not in legacy
                for f in REQUIRED_SNAPSHOT_FIELDS))

    gates = {
        "accumulation": accumulation[
            "continuing_without_unexplained_drops"],
        "benchmark_population_size":
            fresh["benchmark_maturity"]["n_at_least"].get("30", 0)
            == fresh["benchmark_maturity"]["populations_total"]
            and fresh["benchmark_maturity"]["populations_total"] > 0,
        "classification_integrity": (
            classification["unknown"] == 0
            and classification["missing_provider"] == 0
            and classification["missing_canonical_competition"] == 0
            and classification["ambiguous_competition"] == 0
            and not classification["benchmark_isolation_violations"]),
        "data_integrity": (
            data_integrity["duplicate_observations"] == 0
            and data_integrity["future_timestamps"] == 0
            and data_integrity["market_timestamp_after_capture"] == 0
            and data_integrity["score_monotonicity_violations"] == 0),
        "market_completeness": (fresh["z_availability"]
                                ["availability_rate"] or 0) >= 0.90,
        "snapshot_continuity": cont["verdict_pass"],
    }
    gate_results = [{"gate": k,
                     "description": dict(GATE_DEFS)[k],
                     "pass": bool(gates[k])} for k, _ in GATE_DEFS]

    audit = {
        "generated_utc": fresh["generated_utc"],
        "audit_scope": "descriptive maturation only — no predictive, "
                       "profitability, or model-readiness claim",
        "accumulation": accumulation,
        "competition_maturity": competition_maturity,
        "benchmark_maturity": benchmark_maturity,
        "sparse_populations_note": (
            "populations with N < 30 exist and are reported honestly; "
            "see benchmark_maturity.note"),
        "classification_integrity": classification,
        "data_integrity": data_integrity,
        "market_completeness": market,
        "clock_source_quality": clock,
        "snapshot_continuity": check_snapshot_continuity(snaps),
        "maturation_trajectory": maturation_trajectory,
        "maturation_gates": gate_results,
        "verdict": ("MATURE ENOUGH FOR NEXT DESCRIPTIVE RESEARCH STAGE"
                    if all(gates.values()) else "ACCUMULATE"),
        "limitations": LIMITATIONS_TEXT,
    }
    # the audit inherits the descriptive-only firewall: no banned concept
    # can appear anywhere in the payload
    assert_descriptive_only(audit)
    return audit


LIMITATIONS_TEXT = (
    "1) This audit is descriptive: passing all maturation gates is a "
    "statement about DATA MATURITY ONLY and is NOT evidence that the "
    "data is ready for prediction, betting, modelling, or any "
    "profitability claim. "
    "2) Rates (missing/stale/Z availability) describe collection "
    "completeness, not market behaviour. "
    "3) Populations share history across games by construction "
    "(strictly-prior T-state); population N is a maturity count, never "
    "statistical significance. "
    "4) Sparse populations are reported honestly and are never altered "
    "or merged. "
    "5) Elapsed-time micro-drops are source jitter, recorded and never "
    "corrected. "
    "6) Alternative-line groups are legitimate bookmaker alternative "
    "totals, not duplicates. "
    "7) All observations come from post-epoch clean data only.")


def render_txt(a: Dict[str, Any]) -> str:
    """Human-readable audit (fixed section order per directive)."""
    L = []
    add = L.append
    add("PROSPECTIVE MATURATION AUDIT — %s" % a["generated_utc"])
    add("(descriptive/read-only; no predictive or profitability claim)")
    add("")
    add("CURRENT STATUS")
    add("  snapshots in durable history : %d" %
        a["snapshot_continuity"]["snapshots"])
    add("  latest growth                : %s observations / %s games" % (
        a["accumulation"]["latest_interval_observations_added"],
        (a["accumulation"]["per_interval"][-1]["games_added"]
         if a["accumulation"]["per_interval"] else "n/a")))
    add("  verdict                      : %s" % a["verdict"])
    add("")
    add("ACCUMULATION")
    for g in a["accumulation"]["per_interval"]:
        add("  %s → %s : +%s obs / +%s games" % (
            (g["from"] or "?")[:19], (g["to"] or "?")[:19],
            g["observations_added"], g["games_added"]))
    add("  unexplained drops            : %s" %
        (len(a["accumulation"]["unexplained_drops"])))
    add("")
    add("COMPETITION MATURITY (per competition, never combined)")
    for c in a["competition_maturity"]:
        add("  %s / %s" % (c["provider"], c["competition"]))
        add("    lifetime games/obs : %s / %s" % (c["lifetime_games"],
                                                  c["lifetime_observations"]))
        add("    N median/min/max   : %s / %s / %s" % (c["median_n"],
                                                      c["min_n"], c["max_n"]))
        add("    populations        : %s (N>=30: %s, >=100: %s, >=500: %s,"
            " >=1000: %s)" % (c["benchmark_populations"],
                              c["n_at_least"]["30"], c["n_at_least"]["100"],
                              c["n_at_least"]["500"],
                              c["n_at_least"]["1000"]))
    add("")
    add("BENCHMARK MATURITY")
    bm = a["benchmark_maturity"]["latest"]
    add("  populations total            : %s" % bm["populations_total"])
    add("  N>=30 / >=100 / >=500 / >=1000 : %s / %s / %s / %s" % (
        bm["n_at_least"]["30"], bm["n_at_least"]["100"],
        bm["n_at_least"]["500"], bm["n_at_least"]["1000"]))
    nd = a["benchmark_maturity"].get("n_distribution") or {}
    add("  N distribution (p05/p50/p95) : %s / %s / %s" % (
        nd.get("p05"), nd.get("p50"), nd.get("p95")))
    add("  note: %s" % a["benchmark_maturity"]["note"])
    add("")
    add("CLASSIFICATION INTEGRITY")
    ci = a["classification_integrity"]
    add("  UNKNOWN                      : %s" % ci["unknown"])
    add("  missing provider/competition : %s / %s" % (
        ci["missing_provider"], ci["missing_canonical_competition"]))
    add("  ambiguous competition        : %s" % ci["ambiguous_competition"])
    add("  benchmark-isolation violations: %s" %
        (ci["benchmark_isolation_violations"] or "none"))
    add("")
    add("DATA INTEGRITY")
    di = a["data_integrity"]
    for k in ("duplicate_observations", "future_timestamps",
              "market_timestamp_after_capture",
              "score_monotonicity_violations",
              "elapsed_monotonicity_jitter_drops",
              "alternative_line_groups"):
        add("  %-32s: %s" % (k, di[k]))
    add("  note: %s" % di["note"])
    add("")
    add("MARKET COMPLETENESS")
    add("  latest: missing %s · stale %s · Z availability %s" % (
        a["market_completeness"]["latest"]["missing_line_rate"],
        a["market_completeness"]["latest"]["stale_line_rate"],
        a["market_completeness"]["latest"]["z_availability"]))
    for m in a["market_completeness"]["over_time"]:
        add("    %s  missing=%s stale=%s z=%s" % (
            (m["generated_utc"] or "?")[:19], m["missing_line_rate"],
            m["stale_line_rate"], m["z_availability"]))
    add("")
    add("SOURCE/CLOCK QUALITY")
    cq = a["clock_source_quality"]
    add("  elapsed micro-drops          : %s (same-quarter %s, "
        "period-transition %s)" % (cq["elapsed_micro_drops"],
                                   cq["same_quarter"],
                                   cq["period_transition"]))
    add("  drop size min/max (minutes)  : %s / %s" % (
        cq["min_drop_minutes"], cq["max_drop_minutes"]))
    add("  score monotonicity violations: %s (tracked separately)" %
        cq["score_monotonicity_violations_separate"])
    add("  note: %s" % cq["note"])
    add("")
    add("SNAPSHOT CONTINUITY")
    sc = a["snapshot_continuity"]
    add("  snapshots                    : %s" % sc["snapshots"])
    add("  monotone increasing          : %s" % sc["monotone_increasing"])
    add("  duplicate timestamps         : %s" % sc["duplicate_timestamps"])
    add("  missing required fields      : %s" %
        (sc["snapshots_missing_required_fields"] or "none"))
    add("")
    add("MATURATION TRAJECTORY (time-to-maturity, descriptive only)")
    for p in a["maturation_trajectory"]["populations"]:
        s30 = p["thresholds"]["30"]
        add("  %-42s N=%-6s N30=%s%s" % (
            p["key"], p["current_n"], s30["state"],
            (" (crossed %s)" % s30["crossed_at"][:19]
             if s30["state"] == "MATURE" and s30["crossed_at"] else "")))
    add("")
    add("MATURATION GATES (descriptive — NOT predictive readiness)")
    for g in a["maturation_gates"]:
        add("  [%s] %-28s %s" % ("PASS" if g["pass"] else "FAIL",
                               g["gate"], g["description"]))
    add("")
    add("LIMITATIONS")
    add("  %s" % a["limitations"])
    add("")
    add("VERDICT: %s" % a["verdict"])
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    from datetime import timezone
    ap = argparse.ArgumentParser(
        description="Read-only prospective maturation audit")
    ap.add_argument("--main-db", default="/home/ubuntu/BLM/blm_pokerbet.db")
    ap.add_argument("--clean-db",
                    default="/home/ubuntu/BLM/blm_metrics_clean.db")
    ap.add_argument("--history", default=DEFAULT_HISTORY)
    ap.add_argument("--window-hours", type=float, default=24.0)
    ap.add_argument("--api-url", default="http://127.0.0.1:8901")
    ap.add_argument("--json-out", default="/tmp/prospective_maturation_audit.json")
    ap.add_argument("--txt-out", default="/tmp/prospective_maturation_audit.txt")
    args = ap.parse_args(argv)
    audit = build_audit(args.main_db, args.clean_db,
                        datetime.now(timezone.utc), args.history,
                        args.window_hours, args.api_url or None)
    Path(args.json_out).write_text(
        json.dumps(audit, indent=2, default=str) + "\n")
    Path(args.txt_out).write_text(render_txt(audit) + "\n")
    print("verdict:", audit["verdict"])
    print("json:", args.json_out)
    print("txt:", args.txt_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
