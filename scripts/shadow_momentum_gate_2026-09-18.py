#!/usr/bin/env python3
"""PROSPECTIVE SHADOW VALIDATION — momentum-gated 75% UNDER trigger.

Answers the standing question: can BLM CONSISTENTLY hold ~70-75% UNDER accuracy
at low alert frequency on UNSEEN games?

Rules of engagement (frozen at spec creation, never edited afterwards):
  * PRODUCTION IS UNTOUCHED. This script only READS the DBs (mode=ro) and
    writes to its own files: spec JSON, decisions JSONL, report TXT.
  * The trigger definition is the production mirror from
    audit_alert_improvement_2026-09-18 (unchanged gates: progress>=75,
    actual<league_avg, required>avg*1.04, fresh line <=300s, OK settlement).
  * The momentum gates are PRE-REGISTERED round numbers discovered in-sample
    (backtest 2026-09-18): m = recent_pace_3m - actual_pts_per_min,
    gate A: m < 0, gate B: m < -0.5.  NO threshold tuning is permitted on
    shadow data; a change requires a NEW spec file with a NEW start time.
  * UNSEEN ONLY: a game enters the shadow set only if its FIRST >=75%
    observation is captured AFTER shadow_start_utc.  A decision row is
    written BEFORE the game settles (decision is genuinely prospective).
  * Settlement joins game_results final_result_status='OK' only, and a
    trigger counts only when captured_at < result_at.

Usage:
  python3 scripts/shadow_momentum_gate_2026-09-18.py record    # decisions now
  python3 scripts/shadow_momentum_gate_2026-09-18.py report    # accuracy so far
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/BLM")

_spec = importlib.util.spec_from_file_location(
    "audit_mod", "/home/ubuntu/BLM/scripts/audit_alert_improvement_2026-09-18.py")
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

BASE = Path("/home/ubuntu/BLM")
SPEC_PATH = BASE / "shadow_momentum_spec_2026-09-18.json"
DECISIONS_PATH = BASE / "shadow_momentum_decisions_2026-09-18.jsonl"
REPORT_PATH = BASE / "analysis_shadow_momentum_report_2026-09-18.txt"

ODDS = 1.85
BE = 100.0 / ODDS
BE_P = 1.0 / ODDS
GATES = {"gateA_m_lt_0": 0.0, "gateB_m_lt_-0.5": -0.5}
PRIMARY_MIN_N = 60


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_of(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def wilson(wins, n, z=1.96):
    if not n:
        return (None, None)
    p = wins / n
    d = 1.0 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _lbp(k, n, p):
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
            + k * math.log(p) + (n - k) * math.log1p(-p))


def binom_tail_ge(k, n, p):
    """Exact P(X >= k), X ~ Binomial(n, p), log-space stable."""
    if n == 0 or k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if k > n * p:                      # upper tail is the small side
        return _tail(k, n, p)
    lower = _tail(0, k, p, upper=False)
    return max(0.0, 1.0 - lower)


def _tail(k0, n, p, upper=True):
    m = None
    logs = []
    rng = range(k0, n + 1) if upper else range(0, k0)
    for i in rng:
        logs.append(_lbp(i, n, p))
    m = max(logs)
    return math.exp(m) * math.fsum(math.exp(x - m) for x in logs)


def ensure_spec() -> dict:
    if SPEC_PATH.exists():
        return json.loads(SPEC_PATH.read_text())
    spec = {
        "created_utc": now_iso(),
        "shadow_start_utc": now_iso(),
        "production_trigger": ("progress>=75% & actual<league_avg & "
                               "required>league_avg*1.04 (strict) & "
                               "line freshness<=300s & OK settlement"),
        "league_reference": "point-in-time mean realised pace, result_at STRICTLY before trigger",
        "momentum_feature": "m = recent_pace_3m - actual_pts_per_min at trigger observation",
        "gates": {k: {"m_lt": v} for k, v in GATES.items()},
        "unseen_rule": "game enters shadow set only if FIRST >=75% observation is after shadow_start_utc",
        "decision_timing": "recorded before settlement; settled only vs final_result_status='OK' with captured_at < result_at",
        "primary_success_rule": f"cumulative Wilson95 lower bound > {BE:.3f}% at n>={PRIMARY_MIN_N} and point estimate >= 65%",
        "warning_rule": "any rolling-50 window below 54.054%",
        "threshold_change_policy": "forbidden on shadow data; new thresholds require a new spec + new start time",
        "odds_assumption": ODDS,
    }
    SPEC_PATH.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"spec created (START={spec['shadow_start_utc']}): {SPEC_PATH}")
    return spec


def record() -> None:
    spec = ensure_spec()
    start_t = epoch_of(spec["shadow_start_utc"])
    con_p, con_c = audit.ro(audit.PROD), audit.ro(audit.CLEAN)
    settled, invalid, league = audit.load_universe(con_p, con_c)

    done = set()
    if DECISIONS_PATH.exists():
        for line in DECISIONS_PATH.read_text().splitlines():
            if line.strip():
                done.add(json.loads(line)["game_id"])

    rows = con_c.execute(
        """SELECT source_game_id, progress_pct, actual_pts_per_min,
                  required_pts_per_min, live_total_line, captured_at,
                  market_age_seconds, market_status, recent_pace_3m
             FROM clean_projections
            WHERE progress_pct >= 75 AND progress_pct < 100
              AND (terminal IS NULL OR terminal = 0) AND status='VALID'
              AND actual_pts_per_min IS NOT NULL AND required_pts_per_min IS NOT NULL
              AND live_total_line IS NOT NULL
              AND captured_at > ?
            ORDER BY source_game_id, captured_at""",
        (spec["shadow_start_utc"],)).fetchall()

    new_rows = 0
    with DECISIONS_PATH.open("a") as fh:
        for r in rows:
            gid = r["source_game_id"].split("#")[0]
            if gid in done:
                continue
            s = settled.get(gid)
            if s is None or gid in invalid:
                continue
            t = epoch_of(r["captured_at"])
            ra = epoch_of(s["ra"])
            # this should be the FIRST >=75% row after start for the game
            avg = league[s["slug"]].avg_before(t)
            rec = {
                "game_id": gid, "league": audit.SLUG_SHORT.get(s["slug"], s["slug"]),
                "captured_at": r["captured_at"], "recorded_at_utc": now_iso(),
                "line": r["live_total_line"], "act": r["actual_pts_per_min"],
                "req": r["required_pts_per_min"], "avg": avg,
                "m": (r["recent_pace_3m"] - r["actual_pts_per_min"])
                     if r["recent_pace_3m"] is not None else None,
                "stale": (r["market_status"] == "STALE"
                          or (r["market_age_seconds"] is not None
                              and r["market_age_seconds"] > 300.0)),
                "post_settlement": bool(ra is not None and t is not None and t >= ra),
                "fire_production": None, "gateA_m_lt_0": None, "gateB_m_lt_-0.5": None,
                "spec_start": spec["shadow_start_utc"],
            }
            if rec["avg"] is not None and not rec["post_settlement"]:
                fire = (r["actual_pts_per_min"] < rec["avg"]) and \
                       (r["required_pts_per_min"] > rec["avg"] * 1.04) and not rec["stale"]
                rec["fire_production"] = fire
                for gname, thr in GATES.items():
                    rec[gname] = bool(fire and rec["m"] is not None and rec["m"] < thr)
            fh.write(json.dumps(rec) + "\n")
            done.add(gid)
            new_rows += 1
    con_p.close()
    con_c.close()
    print(f"record: {new_rows} new decision row(s); total {len(done)} games tracked")


def report() -> None:
    spec = ensure_spec()
    con_p = audit.ro(audit.PROD)
    results = {}
    for r in con_p.execute(
            """SELECT gr.source_game_id AS gid, gr.final_total AS ft, gr.result_at AS ra
                 FROM game_results gr
                WHERE gr.final_result_status='OK' AND gr.final_total IS NOT NULL"""):
        results[r["gid"]] = (float(r["ft"]), r["ra"])
    con_p.close()

    recs = []
    if DECISIONS_PATH.exists():
        for line in DECISIONS_PATH.read_text().splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            res = results.get(d["game_id"])
            if not res:
                continue
            ft, ra = res
            t, t_ra = epoch_of(d["captured_at"]), epoch_of(ra)
            if t is None or t_ra is None or not (t < t_ra):
                continue
            d["outcome"] = "under" if ft < d["line"] else "over" if ft > d["line"] else "push"
            recs.append(d)

    L = [f"SHADOW MOMENTUM GATE — PROSPECTIVE REPORT  generated {now_iso()}",
         f"shadow start {spec['shadow_start_utc']} (unseen games only; decisions pre-registered)",
         f"decision rows: {sum(1 for _ in open(DECISIONS_PATH)) if DECISIONS_PATH.exists() else 0}"
         f"  settled&valid: {len(recs)}", ""]

    def cohort_row(label, rows_):
        n = len(rows_)
        u = sum(1 for d in rows_ if d["outcome"] == "under")
        o = sum(1 for d in rows_ if d["outcome"] == "over")
        p = sum(1 for d in rows_ if d["outcome"] == "push")
        lo, hi = wilson(u, n) if n else (None, None)
        pval = None
        if n:
            if u > n * BE_P:
                pval = _tail(u, n, BE_P)
            else:
                pval = max(0.0, 1.0 - _tail(0, u, BE_P, upper=False))
        fire_n = sum(1 for d in rows_ if d.get("fire_production"))
        L.append(f"  {label:<24} N={n:>4} (prod-fire {fire_n:>4})  UNDER={u:>3} OVER={o:>3} "
                 f"PUSH={p:>2}  hit={audit.pct(u, n):>6.2f}%  "
                 f"CI95=[{100*lo if lo is not None else 0:>5.1f},{100*hi if hi is not None else 0:>5.1f}]  "
                 f"p={pval if pval is not None else 1:.4f}")

    L.append("COHORTS (settled so far)")
    L.append("-" * 96)
    prod = [d for d in recs if d.get("fire_production")]
    cohort_row("production mirror", prod)
    for gname in GATES:
        cohort_row(gname, [d for d in recs if d.get(gname)])
    L.append("")

    L.append("SAMPLE-SIZE REQUIREMENT for the 70-75% question")
    L.append("-" * 96)
    L.append("  minimum n for Wilson95 LOWER BOUND to clear break-even 54.054%:")
    for p_hat in (0.60, 0.65, 0.70, 0.75):
        need = None
        for n in range(10, 2000):
            lo, _ = wilson(round(p_hat * n), n)
            if lo is not None and 100.0 * lo > BE:
                need = n
                break
        L.append(f"    true hit ~{p_hat:.0%}:  n >= {need}")
    L.append("")
    L.append("  alert-frequency reality (audit E3, current gates): ~6.4 production triggers/day,")
    L.append("  gateA keeps ~48% -> ~3.1 gated alerts/day  ->  n=60 in ~19 days, n=100 in ~32 days.")
    L.append("")

    if recs:
        L.append("ROLLING-50 STABILITY (gateA, chronological)")
        L.append("-" * 96)
        seq = sorted([d for d in recs if d.get("gateA_m_lt_0")], key=lambda d: d["captured_at"])
        hits = [1 if d["outcome"] == "under" else 0 for d in seq]
        if len(hits) >= 50:
            rates = [100.0 * sum(hits[i:i + 50]) / 50 for i in range(len(hits) - 49)]
            L.append(f"  windows={len(rates)} min={min(rates):.2f}% max={max(rates):.2f}% latest={rates[-1]:.2f}%")
            warn = any(r < BE for r in rates)
            L.append(f"  warning rule (any rolling-50 < {BE:.3f}%): {'TRIGGERED' if warn else 'not triggered'}")
        else:
            L.append(f"  insufficient settled gateA rows ({len(hits)} < 50)")
    else:
        L.append("No settled shadow rows yet — collection starts now; report again after games settle.")

    Path(REPORT_PATH).write_text("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"\nwritten: {REPORT_PATH}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "record"
    if mode == "record":
        record()
    elif mode == "report":
        report()
    else:
        sys.exit("usage: shadow_momentum_gate_2026-09-18.py [record|report]")
