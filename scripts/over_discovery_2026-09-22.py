#!/usr/bin/env python3
"""OVER-SIDE INDICATOR DISCOVERY — read-only research (2026-09-22).

Mirrors the UNDER fingerprint methodology EXACTLY (read-only research
instrument; writes only its own report):

  SOURCES (same as scripts/oos_weekly_rolling_2026-09-21.py)
    - BLM/analysis/under_over_feature_dataset_2026-09-21.csv  (discovery)
    - BLM/shadow_fingerprints_75.jsonl                        (forward log;
      last record per (game_id, captured_at); JSONL supersedes CSV)

  COHORT
    - one record per (gid, captured_at); tier "75"
    - CLEAN  = stale == false
    - triggers = production 75% condition (act < avg AND req > avg*1.04)
    - settled  = outcome in (under, over, push)
    - point-in-time features ONLY: req_ratio, recent3_minus_act, q3_ratio,
      line_move, act_minus_req, score_differential (the SAME six fields the
      OOS contract froze — no final, closing line, Q4 or future data).

  TASK
    Do NOT invert the UNDER rules and call them OVER rules.  SCAN both
    sides of every feature distribution to find where OVER% actually
    rises, then report each candidate with N / OVER% / UNDER% / baseline
    OVER% / lift / Wilson 95% CI / size class, the forward (OOS) subset,
    and the symmetry verdict against its UNDER counterpart.

Usage:
    python3 scripts/over_discovery_2026-09-22.py
"""
from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

REPO = "/home/ubuntu/BLM"
CSV_PATH = f"{REPO}/analysis/under_over_feature_dataset_2026-09-21.csv"
JSONL_PATH = f"{REPO}/shadow_fingerprints_75.jsonl"
OUT_MD = f"{REPO}/analysis_over_discovery_2026-09-22.md"

# The SAME point-in-time feature set the UNDER OOS contract froze.
# CSV column name -> canonical feature name ("diff" is the CSV spelling of
# score_differential; the JSONL may carry either spelling).
FIELDS = ("req_ratio", "recent3_minus_act", "q3_ratio", "line_move",
          "act_minus_req")
DIFF_KEYS = ("score_differential", "diff")

REPORT: list[str] = []


def out(s: str = "") -> None:
    REPORT.append(s)
    print(s)


def fnum(x):
    if x in ("", None):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def wilson(wins, n, z=1.96):
    if not n:
        return (None, None)
    p = wins / n
    d = 1.0 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def size_class(n: int) -> str:
    if n < 20:
        return "tiny"
    if n < 50:
        return "small"
    if n < 100:
        return "moderate"
    return "substantial"


def week_key(cap):
    try:
        dt = datetime.fromisoformat(cap.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    dt = dt.astimezone(timezone.utc)
    monday = (dt - timedelta(days=dt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return monday.strftime("%G-W%V")


def load_records():
    """CSV (history) + JSONL forward log -> resolved per-key records.
    Identical merge to oos_weekly_rolling (JSONL last-wins)."""
    recs = {}
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("tier") != "75":
                    continue
                if not (row.get("gid") and row.get("captured_at")):
                    continue
                key = (row["gid"], row["captured_at"])
                r = {
                    "league": row.get("league"),
                    "stale": row.get("stale") in ("True", "true", "1"),
                    "trigger": row.get("trigger") in ("True", "true", "1"),
                    "outcome": (row.get("outcome") or None),
                    "cap": row.get("captured_at"),
                    "src": "csv",
                }
                for f in FIELDS:
                    r[f] = fnum(row.get(f))
                for dk in DIFF_KEYS:
                    if row.get(dk) not in ("", None):
                        r["score_differential"] = fnum(row.get(dk))
                        break
                else:
                    r["score_differential"] = None
                recs[key] = r
    if os.path.exists(JSONL_PATH):
        with open(JSONL_PATH) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (d.get("game_id"), d.get("captured_at"))
                prev = recs.get(key) or {}
                r = dict(prev)
                r.update({
                    "league": d.get("league") or prev.get("league"),
                    "stale": bool(d.get("stale", prev.get("stale", False))),
                    "trigger": bool(d.get("is_production_trigger",
                                          prev.get("trigger", False))),
                    "outcome": (d.get("outcome") or prev.get("outcome")),
                    "cap": d.get("captured_at") or prev.get("cap"),
                    "src": "jsonl",
                })
                for f in FIELDS:
                    if f in d:
                        r[f] = fnum(d.get(f))
                    elif f not in r:
                        r[f] = None
                sd = None
                for dk in DIFF_KEYS:
                    if d.get(dk) not in (None, ""):
                        sd = fnum(d.get(dk))
                        break
                if sd is None:
                    sd = prev.get("score_differential")
                r["score_differential"] = sd
                recs[key] = r
    return recs


# ── candidate conditions (discovery scans + mirror rules) ─────────────

def _between(v, lo, hi):
    return v is not None and lo <= v < hi


def _gt(v, t):
    return v is not None and v > t


def _ge(v, t):
    return v is not None and v >= t


def _lt(v, t):
    return v is not None and v < t


def _le(v, t):
    return v is not None and v <= t


SCANS: list[tuple[str, object]] = [
    # --- req_ratio, both sides of 1.0 ---
    ("req_ratio in [0.80,0.90)",        lambda r: _between(r["req_ratio"], 0.80, 0.90)),
    ("req_ratio in [0.90,1.00)",        lambda r: _between(r["req_ratio"], 0.90, 1.00)),
    ("req_ratio in [1.00,1.04)",        lambda r: _between(r["req_ratio"], 1.00, 1.04)),
    ("req_ratio in [1.04,1.10)",        lambda r: _between(r["req_ratio"], 1.04, 1.10)),
    ("req_ratio in [1.10,1.20) [C1]",   lambda r: _between(r["req_ratio"], 1.10, 1.20)),
    ("req_ratio >= 1.20",               lambda r: _ge(r["req_ratio"], 1.20)),
    # --- q3_ratio, both sides of 1.0 ---
    ("q3_ratio < 0.90 [R2]",            lambda r: _lt(r["q3_ratio"], 0.90)),
    ("q3_ratio in [0.90,1.00)",         lambda r: _between(r["q3_ratio"], 0.90, 1.00)),
    ("q3_ratio in [1.00,1.10)",         lambda r: _between(r["q3_ratio"], 1.00, 1.10)),
    ("q3_ratio >= 1.10",                lambda r: _ge(r["q3_ratio"], 1.10)),
    # --- recent3_minus_act (deceleration <-> acceleration) ---
    ("recent3-act <= -1.0",             lambda r: _le(r["recent3_minus_act"], -1.0)),
    ("recent3-act in [-1.0,-0.5)",      lambda r: _between(r["recent3_minus_act"], -1.0, -0.5)),
    ("recent3-act in [-0.5,0.0)",       lambda r: _between(r["recent3_minus_act"], -0.5, 0.0)),
    ("recent3-act in [0.0,0.5)",        lambda r: _between(r["recent3_minus_act"], 0.0, 0.5)),
    ("recent3-act in [0.5,1.0)",        lambda r: _between(r["recent3_minus_act"], 0.5, 1.0)),
    ("recent3-act >= 1.0",              lambda r: _ge(r["recent3_minus_act"], 1.0)),
    # --- act_minus_req (actual vs required) ---
    ("act-req <= -1.0",                 lambda r: _le(r["act_minus_req"], -1.0)),
    ("act-req in [-1.0,0.0)",           lambda r: _between(r["act_minus_req"], -1.0, 0.0)),
    ("act-req >= 0.0",                  lambda r: _ge(r["act_minus_req"], 0.0)),
    # --- line_move ---
    ("line_move < 0 (drifting down)",   lambda r: _lt(r["line_move"], 0)),
    ("line_move == 0",                  lambda r: r["line_move"] == 0),
    ("line_move > 0 (drifting up)",     lambda r: _gt(r["line_move"], 0)),
    # --- score differential ---
    ("diff < 0 (trailing)",             lambda r: _lt(r["score_differential"], 0)),
    ("diff >= 0 (level/ahead)",         lambda r: _ge(r["score_differential"], 0)),
]

MIRRORS: list[tuple[str, object, str]] = [
    # (name, condition, UNDER counterpart it mirrors)
    ("M-C1a req_ratio in [0.80,0.90)",
     lambda r: _between(r["req_ratio"], 0.80, 0.90), "C1 [1.10,1.20)"),
    ("M-C1b req_ratio in [0.90,1.00)",
     lambda r: _between(r["req_ratio"], 0.90, 1.00), "C1 [1.10,1.20)"),
    ("M-C2 recent3-act >= +0.5",
     lambda r: _ge(r["recent3_minus_act"], 0.5), "C2 <= -0.5"),
    ("M-C3 req>1.04x AND q3>avg",
     lambda r: _gt(r["req_ratio"], 1.04) and _gt(r["q3_ratio"], 1.0), "C3"),
    ("M-C5 req>1.10x AND q3>avg",
     lambda r: _gt(r["req_ratio"], 1.10) and _gt(r["q3_ratio"], 1.0), "C5"),
    ("M-C6 accel>=0.5 AND q3>avg",
     lambda r: _ge(r["recent3_minus_act"], 0.5) and _gt(r["q3_ratio"], 1.0),
     "C6"),
    ("M-R2 q3_ratio > 1.10",
     lambda r: _ge(r["q3_ratio"], 1.10), "R2 < 0.90"),
    ("M-C4 [0.90,1.00) AND accel>=0.5",
     lambda r: _between(r["req_ratio"], 0.90, 1.00)
     and _ge(r["recent3_minus_act"], 0.5), "C4"),
]

COMBOS: list[tuple[str, object]] = [
    ("q3>avg AND line_move>0",
     lambda r: _gt(r["q3_ratio"], 1.0) and _gt(r["line_move"], 0)),
    ("q3>avg AND accel>=0",
     lambda r: _gt(r["q3_ratio"], 1.0) and _ge(r["recent3_minus_act"], 0.0)),
    ("req<1.00 AND q3>avg",
     lambda r: _lt(r["req_ratio"], 1.00) and _gt(r["q3_ratio"], 1.0)),
    ("q3>avg AND diff>=0",
     lambda r: _gt(r["q3_ratio"], 1.0) and _ge(r["score_differential"], 0)),
    ("q3>avg AND act-req>=0",
     lambda r: _gt(r["q3_ratio"], 1.0) and _ge(r["act_minus_req"], 0.0)),
    ("accel>=0.5 AND line_move>=0",
     lambda r: _ge(r["recent3_minus_act"], 0.5) and _ge(r["line_move"], 0)),
    ("M-C2 AND M-R2 (accel + hot Q3)",
     lambda r: _ge(r["recent3_minus_act"], 0.5) and _ge(r["q3_ratio"], 1.10)),
    ("q3>avg NOT trigger-style (req<=1.10)",
     lambda r: _gt(r["q3_ratio"], 1.0) and _le(r["req_ratio"], 1.10)),
]


def evaluate(cohort, name, cond, baseline_over):
    g = [r for r in cohort if cond(r)]
    n = len(g)
    o = sum(1 for r in g if r["outcome"] == "over")
    u = sum(1 for r in g if r["outcome"] == "under")
    p = sum(1 for r in g if r["outcome"] == "push")
    ov = (100.0 * o / n) if n else None
    uv = (100.0 * u / n) if n else None
    lift = (ov - baseline_over) if ov is not None else None
    lo, hi = wilson(o, n)
    return {"name": name, "n": n, "over": o, "under": u, "push": p,
            "over_pct": ov, "under_pct": uv, "lift": lift,
            "lo": (100 * lo if lo is not None else None),
            "hi": (100 * hi if hi is not None else None),
            "size": size_class(n)}


def main():
    recs = load_records()
    cohort = [r for r in recs.values()
              if r["trigger"] and not r["stale"]
              and r["outcome"] in ("under", "over", "push")]
    n_all = len(cohort)
    o_all = sum(1 for r in cohort if r["outcome"] == "over")
    u_all = sum(1 for r in cohort if r["outcome"] == "under")
    p_all = sum(1 for r in cohort if r["outcome"] == "push")
    base_over = 100.0 * o_all / n_all if n_all else 0.0
    base_under = 100.0 * u_all / n_all if n_all else 0.0

    out("# OVER-SIDE INDICATOR DISCOVERY — 2026-09-22 (READ-ONLY)")
    out()
    out("Methodology: IDENTICAL to the UNDER fingerprint analysis")
    out(f"(oos_weekly_rolling_2026-09-21): cohort = discovery CSV + forward")
    out(f"shadow JSONL merged last-wins on (game_id, captured_at); CLEAN")
    out(f"(stale=false) production 75% triggers, settled; point-in-time")
    out(f"features only (req_ratio, recent3_minus_act, q3_ratio, line_move,")
    out(f"act_minus_req, score_differential). No inversion of UNDER rules:")
    out(f"both sides of every distribution were scanned for where OVER%")
    out(f"actually rises. No production code, DB, alert, fingerprint or")
    out(f"betting change — this script writes only this report.")
    out()
    out("## 1. BASELINE (CLEAN settled 75% triggers)")
    out()
    out(f"- cohort N = {n_all}  (UNDER {u_all} = {base_under:.2f}%, "
        f"OVER {o_all} = {base_over:.2f}%, PUSH {p_all} = "
        f"{100.0 * p_all / n_all:.2f}%)")
    lo, hi = wilson(o_all, n_all)
    out(f"- baseline OVER% = {base_over:.2f}%  "
        f"(Wilson 95% CI {100*lo:.2f}%–{100*hi:.2f}%)")
    out(f"- baseline UNDER% = {base_under:.2f}%  (matches the UNDER analysis)")

    # supplementary read-only DB context (mode=ro + query_only)
    try:
        conn = sqlite3.connect(
            f"file:{REPO}/blm_pokerbet.db?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=1")
        ok = conn.execute(
            "SELECT COUNT(*) FROM game_results "
            "WHERE final_result_status='OK'").fetchone()[0]
        conn.close()
        out(f"- production context: {ok} settled (OK) game_results rows in "
            "blm_pokerbet.db (read-only, query_only) — the 75% cohort is a "
            "production-trigger subset of this")
    except Exception as e:  # pragma: no cover
        out(f"- (DB context unavailable read-only: {e})")

    # forward (OOS) subset — the discovery CSV was built 2026-09-05
    # (ISO week 36); everything captured from Mon 2026-09-07 onward is
    # out-of-sample by construction (the shadow forward log's window).
    IS_CUTOFF = "2026-09-07"
    is_ = [r for r in cohort if (r.get("cap") or "") < IS_CUTOFF]
    oos = [r for r in cohort if (r.get("cap") or "") >= IS_CUTOFF]
    oos_n = len(oos)
    oos_o = sum(1 for r in oos if r["outcome"] == "over")
    out()
    out(f"IS/OOS split: discovery(csv) N={len(is_)} "
        f"(OVER {100.0*sum(1 for r in is_ if r['outcome']=='over')/max(1,len(is_)):.2f}%)"
        f"  |  forward(jsonl) N={oos_n} "
        f"(OVER {100.0*oos_o/max(1,oos_n):.2f}%)")
    out()
    out("## 2. DISTRIBUTION SCANS (both sides; OVER% vs baseline "
        f"{base_over:.2f}%)")
    out()
    out(f"{'feature window':<28} {'N':>5} {'OVER':>5} {'UNDER':>6} {'PUSH':>5} "
        f"{'OVER%':>7} {'lift':>7} {'95% CI':>15} {'size':>12}")
    rows = [evaluate(cohort, name, cond, base_over) for name, cond in SCANS]
    for e in rows:
        ci = (f"[{e['lo']:.1f},{e['hi']:.1f}]"
              if e["lo"] is not None else "—")
        lift = f"{e['lift']:+.1f}" if e["lift"] is not None else "—"
        ov = f"{e['over_pct']:.2f}" if e["over_pct"] is not None else "—"
        out(f"{e['name']:<28} {e['n']:>5} {e['over']:>5} {e['under']:>6} "
            f"{e['push']:>5} {ov:>7} {lift:>7} {ci:>15} {e['size']:>12}")
    out()
    out("## 3. MIRROR CANDIDATES (structure-opposite to each UNDER rule)")
    out()
    out(f"{'candidate':<34} {'mirror of':<14} {'N':>5} {'OVER':>5} {'OVER%':>7} "
        f"{'lift':>7} {'size':>12} {'OOS N':>6} {'OOS OVER%':>10}")
    for name, cond, mirror in MIRRORS:
        e = evaluate(cohort, name, cond, base_over)
        eo = evaluate(oos, name, cond, base_over)
        ov = f"{e['over_pct']:.2f}" if e["over_pct"] is not None else "—"
        lift = f"{e['lift']:+.1f}" if e["lift"] is not None else "—"
        oov = (f"{eo['over_pct']:.2f}" if eo["over_pct"] is not None else "—")
        out(f"{name:<34} {mirror:<14} {e['n']:>5} {e['over']:>5} {ov:>7} "
            f"{lift:>7} {e['size']:>12} {eo['n']:>6} {oov:>10}")
    out()
    out("## 4. COMBINATION / INTERACTION SCANS")
    out()
    out(f"{'combination':<34} {'N':>5} {'OVER':>5} {'OVER%':>7} {'lift':>7} "
        f"{'95% CI':>15} {'size':>12} {'OOS N':>6} {'OOS OVER%':>10}")
    for name, cond in COMBOS:
        e = evaluate(cohort, name, cond, base_over)
        eo = evaluate(oos, name, cond, base_over)
        ci = (f"[{e['lo']:.1f},{e['hi']:.1f}]"
              if e["lo"] is not None else "—")
        ov = f"{e['over_pct']:.2f}" if e["over_pct"] is not None else "—"
        lift = f"{e['lift']:+.1f}" if e["lift"] is not None else "—"
        oov = (f"{eo['over_pct']:.2f}" if eo["over_pct"] is not None else "—")
        out(f"{name:<34} {e['n']:>5} {e['over']:>5} {ov:>7} {lift:>7} "
            f"{ci:>15} {e['size']:>12} {eo['n']:>6} {oov:>10}")
    out()
    out("## 5. WEEKLY STABILITY OF TOP CANDIDATES (ISO weeks, OVER%)")
    out()
    weeks = defaultdict(list)
    for r in cohort:
        wk = week_key(r["cap"])
        if wk:
            weeks[wk].append(r)
    order = sorted(weeks)
    out(f"{'week':<10} {'N':>5} {'OVER%':>7}   " +
        "  ".join(f"{w[6:]:>6}" for w in order))
    top = [(m[0], m[1]) for m in MIRRORS] \
        + [(c[0], c[1]) for c in COMBOS] \
        + [("baseline(all)", lambda r: True)]
    for name, cond in top:
        cells = []
        for w in order:
            g = [r for r in weeks[w] if cond(r)]
            cells.append(
                f"{100.0*sum(1 for r in g if r['outcome']=='over')/len(g):>6.1f}"
                if g else f"{'—':>6}")
        g_all = [r for r in cohort if cond(r)]
        out(f"{name[:9]:<10} {len(g_all):>5} "
            f"{100.0*sum(1 for r in g_all if r['outcome']=='over')/max(1,len(g_all)):>7.1f}   "
            + "  ".join(cells))
    out()
    out("## 6. VERDICT SUMMARY")
    out()
    out("(completed below in the analysis narrative — see")
    out("analysis_over_discovery_2026-09-22.md as written by this script)")

    with open(OUT_MD, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    print(f"\nreport written: {OUT_MD}")


if __name__ == "__main__":
    main()
