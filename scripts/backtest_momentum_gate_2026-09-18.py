#!/usr/bin/env python3
"""READ-ONLY BACKTEST — momentum-gated variants of the production 75% UNDER trigger.

Cohort: EXACTLY the audit cohort (audit_alert_improvement_2026-09-18):
  first clean_projections row per game with progress>=75% (<100%), non-terminal,
  VALID, line present, ACTUAL < point-in-time league avg AND REQUIRED > avg*1.04,
  fresh line (<=300s), game OK-settled, trigger strictly before own settlement.
Momentum feature (present at the trigger observation, point-in-time safe):
  m = recent_pace_3m - actual_pts_per_min   (negative = game cooling)

Honesty notes, declared up front:
  * The momentum FEATURE was discovered on this same cohort (audit §3i) — every
    number below is IN-SAMPLE. Round-number gates only; no threshold optimised.
  * Wilson 95% CI + exact binomial tail vs break-even (54.054% at 1.85) on every cell.
  * Chronological thirds shown for stability; nothing is cherry-picked.
No production changes, no writes to any DB.
"""

from __future__ import annotations

import importlib.util
import math
import sys

sys.path.insert(0, "/home/ubuntu/BLM")

_spec = importlib.util.spec_from_file_location(
    "audit_mod", "/home/ubuntu/BLM/scripts/audit_alert_improvement_2026-09-18.py")
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

OUT = "/home/ubuntu/BLM/analysis_momentum_gate_backtest_2026-09-18.txt"
ODDS = 1.85
BREAK_EVEN = 100.0 / ODDS           # 54.054%
BE_P = 1.0 / ODDS

REPORT: list[str] = []


def out(s: str = "") -> None:
    REPORT.append(s)
    print(s)


def wilson(wins, n, z=1.96):
    if not n:
        return (None, None)
    p = wins / n
    d = 1.0 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _log_binom_pmf(k, n, p):
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
            + k * math.log(p) + (n - k) * math.log1p(-p))


def _sum_logs(logs):
    m = max(logs)
    return math.exp(m) * math.fsum(math.exp(x - m) for x in logs)


def binom_tail_ge(k, n, p):
    """Exact P(X >= k), X ~ Binomial(n, p), log-space stable."""
    if n == 0 or k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if k > n * p:
        return _sum_logs([_log_binom_pmf(i, n, p) for i in range(k, n + 1)])
    lower = _sum_logs([_log_binom_pmf(i, n, p) for i in range(0, k)])
    return max(0.0, 1.0 - lower)


def row(label, recs):
    n = len(recs)
    u = sum(1 for r in recs if r["outcome"] == "under")
    o = sum(1 for r in recs if r["outcome"] == "over")
    p = sum(1 for r in recs if r["outcome"] == "push")
    hit = audit.pct(u, n)
    lo, hi = wilson(u, n) if n else (None, None)
    profit = u * (ODDS - 1.0) - o * 1.0
    roi = audit.pct(profit, n) if n else 0.0
    pval = binom_tail_ge(u, n, BE_P) if n else None
    flag = ""
    if n and lo is not None:
        flag = "  *LB>BE*" if 100.0 * lo > BREAK_EVEN else ""
    out(f"  {label:<34} N={n:>4}  UNDER={u:>3} OVER={o:>3} PUSH={p:>2}  "
        f"hit={hit:>6.2f}%  CI95=[{100*lo:>5.1f},{100*hi:>5.1f}]  "
        f"p(binom)={pval:.4f}  ROI={roi:+6.2f}%  net={profit:+7.1f}u{flag}")
    return {"n": n, "under": u, "hit": hit}


def main():
    con_p, con_c = audit.ro(audit.PROD), audit.ro(audit.CLEAN)
    settled, invalid, league = audit.load_universe(con_p, con_c)
    sigs = audit.load_signals(con_p, con_c, settled, invalid, league)
    con_p.close()
    con_c.close()

    for s in sigs:
        s["trigger"] = (s["act"] < s["avg"]) and (s["req"] > s["avg"] * audit.REQUIRED_MARGIN)
        s["m"] = (s["recent3"] - s["act"]) if s["recent3"] is not None else None

    cohort = [s for s in sigs if s["tier"] == 75 and s["trigger"] and not s["stale"]]
    missing_m = sum(1 for s in cohort if s["m"] is None)
    have_m = [s for s in cohort if s["m"] is not None]

    out("=" * 96)
    out("READ-ONLY BACKTEST — MOMENTUM-GATED 75% UNDER TRIGGER  (2026-09-18)")
    out(f"cohort = audit production mirror: 75% CLEAN triggers, N={len(cohort)} "
        f"(recent3 missing on {missing_m} -> momentum analyses on N={len(have_m)})")
    out("m = recent_pace_3m - actual_pts_per_min at the trigger observation (negative = cooling)")
    out("ODDS=1.85, break-even 54.054%.  IN-SAMPLE: feature discovered on this cohort (audit §3i).")
    out("=" * 96)

    out("")
    out("§1 GATE SWEEP (round-number gates only, no optimisation)")
    out("-" * 96)
    row("V0 production (no gate)", cohort)
    gates = [
        ("m < +0.5  (exclude heating)", lambda m: m < 0.5),
        ("m < 0     (any cooling)", lambda m: m < 0.0),
        ("m < -0.25", lambda m: m < -0.25),
        ("m < -0.5  (cooling audit bucket)", lambda m: m < -0.5),
        ("m < -1.0  (strong cooling)", lambda m: m < -1.0),
        ("m >= +0.5 (heating only — contrast)", lambda m: m >= 0.5),
    ]
    kept_rows = {}
    for label, fn in gates:
        kept_rows[label] = row(label, [s for s in have_m if fn(s["m"])])

    out("")
    out("§2 COUNTERFACTUAL vs PRODUCTION (what the gate adds/drops)")
    out("-" * 96)
    for label, fn in gates[:5]:
        kept = [s for s in have_m if fn(s["m"])]
        dropped = [s for s in have_m if not fn(s["m"])]
        ku = sum(1 for s in kept if s["outcome"] == "under")
        du = sum(1 for s in dropped if s["outcome"] == "under")
        out(f"  {label:<34} kept {len(kept):>4}/{len(have_m)}  hits kept {ku:>3}  "
            f"dropped {len(dropped):>4} (hits dropped {du:>3})  "
            f"drop-accuracy={audit.pct(du, len(dropped)):>6.2f}%")

    out("")
    out("§3 STABILITY — chronological thirds (same order as trigger time)")
    out("-" * 96)
    for label, fn in [("V0 production", None), ("m < 0", lambda m: m < 0.0),
                      ("m < -0.5", lambda m: m < -0.5)]:
        base = have_m if fn is None else [s for s in have_m if fn(s["m"])]
        base = sorted(base, key=lambda s: s["t"])
        third = max(1, len(base) // 3)
        parts = [base[:third], base[third:2 * third], base[2 * third:]]
        cells = "   ".join(
            f"T{i}: N={len(p):>3} hit={audit.pct(sum(1 for s in p if s['outcome']=='under'), len(p)):>6.2f}%"
            for i, p in enumerate(parts, 1))
        out(f"  {label:<18} {cells}")

    out("")
    out("§4 LEAGUE MIX under the strictest declared gate (m < -0.5)")
    out("-" * 96)
    for lg in audit.LEAGUE_ORDER:
        recs = [s for s in have_m if s["m"] < -0.5 and s["league"] == lg]
        if recs:
            row(f"m<-0.5 & {lg}", recs)

    out("")
    out("§5 INTERACTION with the required-margin size (is momentum just a proxy for extremity?)")
    out("-" * 96)
    for lo_, hi_, lab in [(1.04, 1.20, "req/avg 1.04-1.20"), (1.20, 99, "req/avg >= 1.20")]:
        for lab2, fn in [("m < -0.5", lambda m: m < -0.5), ("m >= -0.5", lambda m: m >= -0.5)]:
            g = [s for s in have_m if lo_ <= s["req"] / s["avg"] < hi_ and fn(s["m"])]
            row(f"{lab} & {lab2}", g)

    out("")
    out("§6 VERDICT (filled in analysis report)")
    out("-" * 96)


if __name__ == "__main__":
    main()
    with open(OUT, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    print(f"\nwritten: {OUT}")
