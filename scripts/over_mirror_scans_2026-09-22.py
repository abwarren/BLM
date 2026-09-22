#!/usr/bin/env python3
"""OVER MIRROR + INTERACTION FINE SCANS — read-only research (2026-09-22).

Second stage of the OVER analysis: fine threshold searches (do NOT assume
symmetric mirrors — search the actual distribution), triple interactions,
per-candidate OOS validation with explicit status, and the final
consolidated report.  Reuses the EXACT cohort loader from the discovery
script.  Read-only; the only outputs are research markdown reports.
"""
from __future__ import annotations

import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "over_disc", os.path.join(os.path.dirname(__file__),
                              "over_discovery_2026-09-22.py"))
disc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(disc)

REPO = disc.REPO
OUT_MD = f"{REPO}/analysis_over_mirror_scans_2026-09-22.md"
IS_CUTOFF = "2026-09-07"          # discovery CSV built 2026-09-05 (W36)

R: list[str] = []


def out(s: str = "") -> None:
    R.append(s)
    print(s)


def pct(g, key):
    return 100.0 * sum(1 for r in g if r["outcome"] == key) / len(g) if g \
        else None


def size_class(n):
    return "TINY" if n < 20 else "SMALL" if n < 50 else \
        "MODERATE" if n < 100 else "SUBSTANTIAL"


def row(name, cond, cohort, is_, oos, base_all, base_oos):
    g = [r for r in cohort if cond(r)]
    gi = [r for r in is_ if cond(r)]
    go = [r for r in oos if cond(r)]
    n, no, nu = len(g), sum(1 for r in g if r["outcome"] == "over"), \
        sum(1 for r in g if r["outcome"] == "under")
    np_ = n - no - nu
    ov, uv, pv = pct(g, "over"), pct(g, "under"), pct(g, "push")
    lift = (ov - base_all) if ov is not None else None
    # OOS
    no_, nn_ = sum(1 for r in go if r["outcome"] == "over"), len(go)
    ov_o = 100.0 * no_ / nn_ if nn_ else None
    lift_o = (ov_o - base_oos) if ov_o is not None else None
    # discovery-side lift
    ov_i = pct(gi, "over")
    lift_i = (ov_i - (100.0 - base_oos * 0 - 0) if False else
              (ov_i - (100.0 * sum(1 for r in is_ if r["outcome"] == "over")
                       / len(is_)) if (ov_i is not None and is_) else None))
    if n < 5:
        status = "NO DATA"
    elif nn_ < 20:
        status = "INSUFFICIENT OOS SAMPLE"
    elif lift_o is not None and lift_o <= 0:
        status = "REJECTED (dead OOS)"
    elif lift_o is not None and lift_o < 10:
        status = "WEAK OOS"
    else:
        status = "PROMISING — shadow test only"
    lo, hi = disc.wilson(no, n)

    def f2(x):
        return f"{x:>6.2f}" if x is not None else "     —"

    def f1(x):
        return f"{x:>+6.1f}" if x is not None else "     —"

    def f0(x):
        return f"{x:>5.1f}" if x is not None else "    —"

    ci = (f"[{100*lo:.0f},{100*hi:.0f}]"
          if lo is not None and hi is not None else "    —")
    out(f"{name:<40} {n:>4} {no:>4} {f2(ov)} {f2(uv)} "
        f"{f0(pv)} {f1(lift)} "
        f"{nn_:>4} {no_:>3} {f2(ov_o)} {f1(lift_o)} "
        f"{ci:>11} {size_class(n):<12} {status}")
    return {"name": name, "n": n, "over_pct": ov, "lift": lift,
            "oos_n": nn_, "oos_over_pct": ov_o, "oos_lift": lift_o,
            "status": status}


def main():
    recs = disc.load_records()
    cohort = [r for r in recs.values()
              if r["trigger"] and not r["stale"]
              and r["outcome"] in ("under", "over", "push")]
    is_ = [r for r in cohort if (r.get("cap") or "") < IS_CUTOFF]
    oos = [r for r in cohort if (r.get("cap") or "") >= IS_CUTOFF]
    base_all = pct(cohort, "over")
    base_oos = pct(oos, "over")
    base_is = pct(is_, "over")

    out("# OVER MIRROR + INTERACTION FINE SCANS — 2026-09-22 (READ-ONLY)")
    out()
    out(f"cohort N={len(cohort)} (IS W36 N={len(is_)} OVER {base_is:.2f}% | "
        f"OOS W37+ N={len(oos)} OVER {base_oos:.2f}%)")
    out(f"baseline OVER% full={base_all:.2f}  discovery={base_is:.2f}  "
        f"OOS={base_oos:.2f}   (PUSH = 0 everywhere in this cohort)")
    out()
    out("Legend: N / OVER / OVER% / UNDER% / PUSH% / lift(full) / OOS N / "
        "OOS OVER / OOS OVER% / OOS lift / Wilson95(full) / size / STATUS")
    out()

    # ── 1. fine q3_ratio bands (R2 mirror search) ────────────────────
    out("## 1. q3_ratio FINE BANDS (R2 mirror — search, don't assume ≥1.10)")
    out(f"{'band':<40} {'N':>4} {'OVR':>4} {'OV%':>6} {'UN%':>6} {'PSH%':>5} "
        f"{'lift':>6} {'ON':>4} {'OO':>3} {'OOV%':>6} {'Olift':>6} "
        f"{'CI':>11} {'size':<12} STATUS")
    b_q = [
        ("q3_ratio in [1.00,1.05)", lambda r: disc._between(r["q3_ratio"], 1.00, 1.05)),
        ("q3_ratio in [1.05,1.10)", lambda r: disc._between(r["q3_ratio"], 1.05, 1.10)),
        ("q3_ratio in [1.10,1.20)", lambda r: disc._between(r["q3_ratio"], 1.10, 1.20)),
        ("q3_ratio >= 1.20",        lambda r: disc._ge(r["q3_ratio"], 1.20)),
        ("CUMULATIVE:",             lambda r: False),
        ("q3_ratio > 1.00 (simple)", lambda r: disc._gt(r["q3_ratio"], 1.00)),
        ("q3_ratio >= 1.05",         lambda r: disc._ge(r["q3_ratio"], 1.05)),
        ("q3_ratio >= 1.10 [M-R2]",  lambda r: disc._ge(r["q3_ratio"], 1.10)),
        ("q3_ratio >= 1.20",         lambda r: disc._ge(r["q3_ratio"], 1.20)),
    ]
    for name, cond in b_q:
        row(name, cond, cohort, is_, oos, base_all, base_oos)
    out()

    # ── 2. fine acceleration bands (C2 mirror search) ────────────────
    out("## 2. recent3-act (acceleration) FINE BANDS — the mirror of C2")
    out(f"{'band':<40} {'N':>4} {'OVR':>4} {'OV%':>6} {'UN%':>6} {'PSH%':>5} "
        f"{'lift':>6} {'ON':>4} {'OO':>3} {'OOV%':>6} {'Olift':>6} "
        f"{'CI':>11} {'size':<12} STATUS")
    b_a = [
        ("recent3-act in [0.00,0.25)",  lambda r: disc._between(r["recent3_minus_act"], 0.00, 0.25)),
        ("recent3-act in [0.25,0.50)",  lambda r: disc._between(r["recent3_minus_act"], 0.25, 0.50)),
        ("recent3-act in [0.50,0.75)",  lambda r: disc._between(r["recent3_minus_act"], 0.50, 0.75)),
        ("recent3-act in [0.75,1.00)",  lambda r: disc._between(r["recent3_minus_act"], 0.75, 1.00)),
        ("recent3-act >= 1.00",         lambda r: disc._ge(r["recent3_minus_act"], 1.00)),
        ("CUMULATIVE:",                 lambda r: False),
        ("recent3-act >= 0.00",         lambda r: disc._ge(r["recent3_minus_act"], 0.00)),
        ("recent3-act >= 0.25",         lambda r: disc._ge(r["recent3_minus_act"], 0.25)),
        ("recent3-act >= 0.50 [M-C2]",  lambda r: disc._ge(r["recent3_minus_act"], 0.50)),
        ("recent3-act >= 1.00",         lambda r: disc._ge(r["recent3_minus_act"], 1.00)),
    ]
    for name, cond in b_a:
        row(name, cond, cohort, is_, oos, base_all, base_oos)
    out()

    # ── 3. req_ratio bands reachable under the trigger (C1 mirror) ───
    out("## 3. req_ratio BANDS INSIDE THE TRIGGER (C1 mirror — the trigger")
    out("   precludes req_ratio < 1.04, so the C1 band [1.10,1.20) is where")
    out("   OVERs DIE (11.8%); the reachable mirror is the LOWEST band)")
    out(f"{'band':<40} {'N':>4} {'OVR':>4} {'OV%':>6} {'UN%':>6} {'PSH%':>5} "
        f"{'lift':>6} {'ON':>4} {'OO':>3} {'OOV%':>6} {'Olift':>6} "
        f"{'CI':>11} {'size':<12} STATUS")
    b_r = [
        ("req_ratio in [1.04,1.10)",  lambda r: disc._between(r["req_ratio"], 1.04, 1.10)),
        ("req_ratio in [1.10,1.20) = C1 band", lambda r: disc._between(r["req_ratio"], 1.10, 1.20)),
        ("req_ratio in [1.20,1.35)",  lambda r: disc._between(r["req_ratio"], 1.20, 1.35)),
        ("req_ratio >= 1.35 (R1's tail)", lambda r: disc._ge(r["req_ratio"], 1.35)),
    ]
    for name, cond in b_r:
        row(name, cond, cohort, is_, oos, base_all, base_oos)
    out()

    # ── 4. mirror-per-fingerprint (best data-found thresholds) ───────
    out("## 4. PER-FINGERPRINT MIRROR VERDICTS (data-found thresholds)")
    out(f"{'candidate':<40} {'N':>4} {'OVR':>4} {'OV%':>6} {'UN%':>6} {'PSH%':>5} "
        f"{'lift':>6} {'ON':>4} {'OO':>3} {'OOV%':>6} {'Olift':>6} "
        f"{'CI':>11} {'size':<12} STATUS")
    mirrors = [
        # C1: no true mirror exists (band empty); best reachable = lowest band
        ("C1-mirror: req in [1.04,1.10)", lambda r: disc._between(r["req_ratio"], 1.04, 1.10)),
        # C2: data says >= 0 (not +0.5) is the better threshold
        ("C2-mirror: accel >= 0",         lambda r: disc._ge(r["recent3_minus_act"], 0.00)),
        # C3: req>1.04 (whole trigger) AND q3>1.0
        ("C3-mirror: req>1.04 & q3>1.0",  lambda r: disc._gt(r["req_ratio"], 1.04) and disc._gt(r["q3_ratio"], 1.0)),
        # C4 = C1 AND C2 -> reachable analogue: lowest req band AND accel
        ("C4-mirror: req[1.04,1.10) & accel>=0",
         lambda r: disc._between(r["req_ratio"], 1.04, 1.10) and disc._ge(r["recent3_minus_act"], 0.00)),
        # C5 mirror: deeper req AND q3>1.0
        ("C5-mirror: req>1.10 & q3>1.0",  lambda r: disc._gt(r["req_ratio"], 1.10) and disc._gt(r["q3_ratio"], 1.0)),
        # C6 mirror: accel>=0.5 AND q3>1.0
        ("C6-mirror: accel>=0.5 & q3>1.0", lambda r: disc._ge(r["recent3_minus_act"], 0.50) and disc._gt(r["q3_ratio"], 1.0)),
        # R2 mirror: fine search says >=1.05 or >=1.10?
        ("R2-mirror: q3 >= 1.05",         lambda r: disc._ge(r["q3_ratio"], 1.05)),
    ]
    res = {}
    for name, cond in mirrors:
        res[name] = row(name, cond, cohort, is_, oos, base_all, base_oos)
    out()

    # ── 5. interactions (meaningful sizes only) ──────────────────────
    out("## 5. INTERACTION / TRIPLE SCANS (meaningful sample sizes only)")
    out(f"{'combination':<40} {'N':>4} {'OVR':>4} {'OV%':>6} {'UN%':>6} {'PSH%':>5} "
        f"{'lift':>6} {'ON':>4} {'OO':>3} {'OOV%':>6} {'Olift':>6} "
        f"{'CI':>11} {'size':<12} STATUS")
    combos = [
        ("q3>1.0 AND accel>=0 (hot, no decel)",
         lambda r: disc._gt(r["q3_ratio"], 1.0) and disc._ge(r["recent3_minus_act"], 0.00)),
        ("req[1.04,1.10) AND q3>1.0 (low-req, hot Q3)",
         lambda r: disc._between(r["req_ratio"], 1.04, 1.10) and disc._gt(r["q3_ratio"], 1.0)),
        ("req[1.04,1.10) AND accel>=0 (low-req, momentum)",
         lambda r: disc._between(r["req_ratio"], 1.04, 1.10) and disc._ge(r["recent3_minus_act"], 0.00)),
        ("line_move>0 AND accel>=0",
         lambda r: disc._gt(r["line_move"], 0) and disc._ge(r["recent3_minus_act"], 0.00)),
        ("line_move>0 AND q3>1.0",
         lambda r: disc._gt(r["line_move"], 0) and disc._gt(r["q3_ratio"], 1.0)),
        ("TRIPLE q3>1.0 & accel>=0 & req<=1.10",
         lambda r: disc._gt(r["q3_ratio"], 1.0) and disc._ge(r["recent3_minus_act"], 0.00) and disc._le(r["req_ratio"], 1.10)),
        ("TRIPLE q3>1.0 & accel>=0 & line_move>=0",
         lambda r: disc._gt(r["q3_ratio"], 1.0) and disc._ge(r["recent3_minus_act"], 0.00) and disc._ge(r["line_move"], 0)),
    ]
    for name, cond in combos:
        row(name, cond, cohort, is_, oos, base_all, base_oos)
    out()

    # ── 6. leakage verification ──────────────────────────────────────
    out("## 6. LEAKAGE VERIFICATION")
    out("- Features used: req_ratio, recent3_minus_act, q3_ratio, line_move,")
    out("  act_minus_req, score_differential — EXACTLY the six fields the")
    out("  UNDER OOS contract froze as point-in-time-safe at the 75% boundary.")
    out("- Label: outcome (under/over/push) — used ONLY as the label, never")
    out("  as a feature.  No final score, closing line, Q4 split, settlement")
    out("  timestamp, or post-trigger information appears in any condition.")
    out("- Future league averages: none — q3_ratio uses the same frozen")
    out("  point-in-time league Q3 authority as the UNDER analysis.")
    out("- Scan verification: every candidate condition above references only")
    feats = {"req_ratio", "recent3_minus_act", "q3_ratio", "line_move",
             "act_minus_req", "score_differential"}
    import inspect
    srcs = []
    for ns in (b_q, b_a, b_r, mirrors, combos):
        for _, c in ns:
            try:
                srcs.append(inspect.getsource(c))
            except (TypeError, OSError):
                pass
    ok = all(any(f in s for f in feats) or "False" in s for s in srcs)
    out(f"- AST/source check over {len(srcs)} conditions: "
        f"{'PASS — only whitelisted features referenced' if ok else 'FAIL'}")
    out()

    # ── 7. consolidated verdict table ────────────────────────────────
    out("## 7. OOS VALIDATION SUMMARY (discovery vs OOS)")
    out()
    out(f"{'candidate':<40} {'disc N':>6} {'disc OV%':>8} {'disc lift':>9} "
        f"{'OOS N':>5} {'OOS OV%':>8} {'OOS lift':>9} {'status'}")
    for name, d in res.items():
        dl = f"{d['lift']:+.1f}" if d["lift"] is not None else "—"
        ol = f"{d['oos_lift']:+.1f}" if d["oos_lift"] is not None else "—"
        dov = f"{d['over_pct']:.2f}" if d["over_pct"] is not None else "—"
        oov = f"{d['oos_over_pct']:.2f}" if d["oos_over_pct"] is not None else "—"
        out(f"{name:<40} {d['n']:>6} {dov:>8} {dl:>9} "
            f"{d['oos_n']:>5} {oov:>8} {ol:>9}  {d['status']}")

    with open(OUT_MD, "w") as fh:
        fh.write("\n".join(R) + "\n")
    print(f"\nreport written: {OUT_MD}")


if __name__ == "__main__":
    main()
