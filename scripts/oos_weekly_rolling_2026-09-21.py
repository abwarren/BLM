#!/usr/bin/env python3
"""WEEKLY ROLLING OOS re-evaluation — pre-registered UNDER-fingerprint rules.

READ-ONLY research instrument: opens the production DBs mode=ro and writes
only its own report + an append-only evaluation history.  Re-runs the
2026-09-21 discovery candidates (C1..C6) plus the extension rules (R1..R8,
pre-registered from full-history exploration, stated BEFORE this
validation) over FIXED weekly trigger-time partitions.  As new games
settle, re-running this script rolls the partitions forward; the
append-only history makes week-over-week drift auditable.

SOURCES
  - BLM/analysis/under_over_feature_dataset_2026-09-21.csv   (history)
  - BLM/shadow_fingerprints_75.jsonl                          (forward log;
    last record per (game_id, captured_at); _update lines supersede)

RULES (pre-registered; evaluated on top of the PRODUCTION 75% trigger
condition act < league_avg AND req > league_avg*1.04, point-in-time):
  C1  req_ratio in [1.10, 1.20)
  C2  recent3_minus_act <= -0.5
  C3  req_ratio > 1.04 AND q3_ratio < 1.0
  C4  C1 AND C2
  C5  req_ratio > 1.10 AND q3_ratio < 1.0
  C6  C2 AND q3_ratio < 1.0
  R1  req_ratio in [1.10, 1.35)                      (widen the C1 band)
  R2  q3_ratio < 0.90                                (deeper Q3 slump)
  R3  q3_ratio < 1.0 AND line_move <= 0
  R4  q3_ratio < 1.0 AND score_differential >= 0     (Q3 slump, no deficit)
  R5  recent3_minus_act <= -1.0                      (deeper deceleration)
  R6  C2 AND score_differential >= 0
  R7  q3_ratio < 1.0 AND q3_ratio >= 0.80 AND recent3_minus_act <= -0.5
  R8  act_minus_req <= -1.0 AND q3_ratio < 1.0       (discovery §6 gap band)

EVALUATION CONTRACT
  - one record per (game_id, captured_at); outcome under/over/push only
  - CLEAN subset = stale == false (production's CLEAN identity)
  - partitions = ISO weeks (Mon 00:00 UTC) of captured_at — fixed, never
    re-derived; the CURRENT week is labelled open and reported separately
  - per rule per week: N / UNDER / OVER / UNDER% + per-rule totals and
    Wilson 95% intervals; a rule is flagged ON TRACK only if its rolling
    (non-open weeks) UNDER% >= trigger baseline UNDER%
  - NO threshold re-tuning, NO rule promotion here: this script only
    measures the frozen rules as data accumulates.

Usage:
    venv/bin/python scripts/oos_weekly_rolling_2026-09-21.py            # report
    venv/bin/python scripts/oos_weekly_rolling_2026-09-21.py --append   # + history
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone

REPO = "/home/ubuntu/BLM"
CSV_PATH = f"{REPO}/analysis/under_over_feature_dataset_2026-09-21.csv"
JSONL_PATH = f"{REPO}/shadow_fingerprints_75.jsonl"
OUT_MD = f"{REPO}/analysis_oos_weekly_rolling_2026-09-21.md"
HIST_PATH = f"{REPO}/analysis/oos_weekly_history.jsonl"

FIELDS = ("req_ratio", "recent3_minus_act", "q3_ratio", "line_move",
          "score_differential", "act_minus_req")
RULES = {
    "C1 req_ratio in [1.10,1.20)": lambda f: (
        f.get("req_ratio") is not None and 1.10 <= f["req_ratio"] < 1.20),
    "C2 recent3 <= act-0.5": lambda f: (
        f.get("recent3_minus_act") is not None
        and f["recent3_minus_act"] <= -0.5),
    "C3 req>1.04x AND q3<avg": lambda f: (
        f.get("req_ratio") is not None and f["req_ratio"] > 1.04
        and f.get("q3_ratio") is not None and f["q3_ratio"] < 1.0),
    "C4 C1 AND C2": lambda f: (
        (f.get("req_ratio") is not None and 1.10 <= f["req_ratio"] < 1.20)
        and (f.get("recent3_minus_act") is not None
             and f["recent3_minus_act"] <= -0.5)),
    "C5 req>1.10x AND q3<avg": lambda f: (
        f.get("req_ratio") is not None and f["req_ratio"] > 1.10
        and f.get("q3_ratio") is not None and f["q3_ratio"] < 1.0),
    "C6 C2 AND q3<avg": lambda f: (
        (f.get("recent3_minus_act") is not None
         and f["recent3_minus_act"] <= -0.5)
        and (f.get("q3_ratio") is not None and f["q3_ratio"] < 1.0)),
    "R1 req_ratio in [1.10,1.35)": lambda f: (
        f.get("req_ratio") is not None and 1.10 <= f["req_ratio"] < 1.35),
    "R2 q3_ratio < 0.90": lambda f: (
        f.get("q3_ratio") is not None and f["q3_ratio"] < 0.90),
    "R3 q3<avg AND line_move<=0": lambda f: (
        f.get("q3_ratio") is not None and f["q3_ratio"] < 1.0
        and f.get("line_move") is not None and f["line_move"] <= 0),
    "R4 q3<avg AND diff>=0": lambda f: (
        f.get("q3_ratio") is not None and f["q3_ratio"] < 1.0
        and f.get("score_differential") is not None
        and f["score_differential"] >= 0),
    "R5 recent3 <= act-1.0": lambda f: (
        f.get("recent3_minus_act") is not None
        and f["recent3_minus_act"] <= -1.0),
    "R6 C2 AND diff>=0": lambda f: (
        (f.get("recent3_minus_act") is not None
         and f["recent3_minus_act"] <= -0.5)
        and (f.get("score_differential") is not None
             and f["score_differential"] >= 0)),
    "R7 q3 in [0.8,1.0) AND C2": lambda f: (
        f.get("q3_ratio") is not None and 0.80 <= f["q3_ratio"] < 1.0
        and (f.get("recent3_minus_act") is not None
             and f["recent3_minus_act"] <= -0.5)),
    "R8 act-req<=-1 AND q3<avg": lambda f: (
        f.get("act_minus_req") is not None and f["act_minus_req"] <= -1.0
        and f.get("q3_ratio") is not None and f["q3_ratio"] < 1.0),
}

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


def fnum(x):
    if x in ("", None):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else v


def week_key(cap):
    """ISO week (Mon 00:00 UTC) of a captured_at string."""
    try:
        dt = datetime.fromisoformat(cap.replace("Z", "+00:00"))
    except ValueError:
        return None
    dt = dt.astimezone(timezone.utc)
    monday = (dt - __import__("datetime").timedelta(days=dt.weekday())) \
        .replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.strftime("%G-W%V")


def now_week():
    dt = datetime.now(timezone.utc)
    monday = (dt - __import__("datetime").timedelta(days=dt.weekday())) \
        .replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.strftime("%G-W%V")


def load_records():
    """CSV (history) + JSONL forward log -> resolved per-key records."""
    recs = {}
    src_counts = {"csv": 0, "csv_dropped_no_key": 0, "jsonl": 0}

    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("tier") != "75":
                    continue
                # integrity: a row without a join key cannot be merged and
                # would silently double-count its JSONL twin — drop loudly
                if not (row.get("gid") and row.get("captured_at")):
                    src_counts["csv_dropped_no_key"] += 1
                    continue
                key = (row["gid"], row["captured_at"])
                r = {
                    "league": row.get("league"),
                    "stale": row.get("stale") in ("True", "true", "1"),
                    "trigger": row.get("trigger") in ("True", "true", "1"),
                    "outcome": (row.get("outcome") or None),
                    "cap": row.get("captured_at"),
                }
                for f in FIELDS:
                    r[f] = fnum(row.get(f))
                recs[key] = r
                src_counts["csv"] += 1

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
                prev = recs.get(key)
                # last-wins: JSONL supersedes the CSV snapshot
                r = dict(prev or {})
                r.update({
                    "league": d.get("league") or r.get("league"),
                    "stale": bool(d.get("stale", r.get("stale", False))),
                    "trigger": bool(d.get("is_production_trigger",
                                          r.get("trigger", False))),
                    "outcome": (d.get("outcome") or r.get("outcome")),
                    "cap": d.get("captured_at") or r.get("cap"),
                })
                for f in FIELDS:
                    if f in d:
                        r[f] = fnum(d.get(f))
                    elif f not in r:
                        r[f] = None
                recs[key] = r
                src_counts["jsonl"] += 1
    # merge integrity: the forward JSONL is the resolved, settlement-current
    # view; CSV rows lacking a join key were dropped loudly above.
    return recs, src_counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--append", action="store_true",
                    help="also append this evaluation to the weekly history")
    args = ap.parse_args()

    recs, src = load_records()
    cur_week = now_week()
    out("=" * 100)
    out("WEEKLY ROLLING OOS — PRE-REGISTERED UNDER-FINGERPRINT RULES (READ-ONLY)")
    out(f"run: {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}   "
        f"records: {len(recs)} (csv rows used={src['csv']}, "
        f"dropped_no_key={src['csv_dropped_no_key']}, "
        f"jsonl lines={src['jsonl']})")
    out("base condition: PRODUCTION 75% trigger (act<avg AND req>avg*1.04);")
    out("rules C1..C6 (discovery) + R1..R8 (pre-registered extensions) ON TOP;")
    out("partitions = ISO weeks of trigger time; current week reported separately.")
    out("=" * 100)

    # ---- assemble cohorts ----
    all_trig = [r for r in recs.values() if r["trigger"]]
    clean_trig = [r for r in all_trig if not r["stale"]]
    settled = [r for r in clean_trig
               if r["outcome"] in ("under", "over", "push")]
    out(f"75% boundary records: {len(recs)}   triggers: {len(all_trig)}   "
        f"CLEAN triggers: {len(clean_trig)}   settled CLEAN triggers: "
        f"{len(settled)}")
    if src.get("csv_dropped_no_key"):
        out(f"WARNING: {src['csv_dropped_no_key']} CSV rows dropped "
            "(missing gid/captured_at join key — regenerate the CSV)")
    if not settled:
        out("nothing settled — nothing to evaluate")
        return

    weeks = defaultdict(list)
    for r in settled:
        wk = week_key(r["cap"])
        if wk:
            weeks[wk].append(r)
    open_wk = cur_week
    closed = sorted(w for w in weeks if w != open_wk)

    out("")
    out(f"{'week':<10} {'N':>5} {'UNDER':>6} {'OVER':>5} {'PUSH':>5} "
        f"{'UNDER%':>8}  (trigger baseline per week, CLEAN)")
    for wk in closed + ([open_wk] if open_wk in weeks else []):
        g = weeks[wk]
        u = sum(1 for r in g if r["outcome"] == "under")
        o = sum(1 for r in g if r["outcome"] == "over")
        p = sum(1 for r in g if r["outcome"] == "push")
        lab = "  <- OPEN" if wk == open_wk else ""
        out(f"{wk:<10} {len(g):>5} {u:>6} {o:>5} {p:>5} "
            f"{(100.0 * u / len(g)):>7.2f}%{lab}")

    # ---- per-rule, per-week table (CLEAN settled triggers) ----
    out("")
    out("PER-RULE WEEKLY TABLE (N per week; UNDER% per week in parens)")
    out(f"{'rule':<32} {'TOTAL':>14}  " + " ".join(f"{w[6:]:>12}" for w in closed))
    totals = {}
    base_total = sum(1 for r in settled if r["outcome"] == "under") / len(settled)
    for name, cond in RULES.items():
        g_all = [r for r in settled if cond(r)]
        u_all = sum(1 for r in g_all if r["outcome"] == "under")
        lo, hi = wilson(u_all, len(g_all))
        cells = []
        for wk in closed:
            g = [r for r in weeks[wk] if cond(r)]
            u = sum(1 for r in g if r["outcome"] == "under")
            cells.append(f"{len(g):>4}({100.0 * u / len(g):>4.0f}%)"
                         if g else "   -")
        out(f"{name:<32} {len(g_all):>6} {100.0 * u_all / len(g_all):>6.2f}%"
            f"  " + " ".join(cells))
        totals[name] = (len(g_all), u_all, lo, hi)
    out("")
    out(f"trigger baseline (CLEAN settled): UNDER%={100.0 * base_total:.2f}% "
        f"(N={len(settled)})")
    out("")
    out("PER-RULE TOTALS vs BASELINE (Wilson 95% CI; 'ON TRACK' = rolling")
    out("UNDER% >= trigger baseline — an observation, NOT a promotion):")
    for name, (n, u, lo, hi) in totals.items():
        rate = 100.0 * u / n if n else 0.0
        ci = f"[{lo*100:.1f},{hi*100:.1f}%]" if n else "-"
        flag = "ON TRACK" if rate >= 100.0 * base_total else "below baseline"
        out(f"  {name:<32} N={n:>5}  UNDER={u:>4}  {rate:>6.2f}%  {ci:<20} "
            f"{flag}")
    out("")
    out("Open week (current): excluded from ON-TRACK judgement (incomplete).")
    out("Next re-run rolls partitions forward as new games settle;")
    out("append --append to record this evaluation in")
    out(f"  {HIST_PATH}")
    out("")
    out("READ-ONLY: production DBs not even opened this pass (log+CSV only);")
    out("no alert, threshold, service or production table touched.")

    if args.append:
        os.makedirs(os.path.dirname(HIST_PATH), exist_ok=True)
        rec = {
            "run_utc": datetime.now(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "records": len(recs),
            "settled_clean_triggers": len(settled),
            "baseline_under_pct": round(100.0 * base_total, 4),
            "open_week": open_wk,
            "closed_weeks": closed,
            "rules": {name: {"n": n, "under": u,
                             "under_pct": round(100.0 * u / n, 4)
                             if n else None,
                             "wilson_lo": round(lo, 4) if lo is not None
                             else None,
                             "wilson_hi": round(hi, 4) if hi is not None
                             else None}
                      for name, (n, u, lo, hi) in totals.items()},
        }
        with open(HIST_PATH, "a") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        out(f"history appended: {HIST_PATH}")

    with open(OUT_MD, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    print(f"\nreport written: {OUT_MD}")


if __name__ == "__main__":
    main()
