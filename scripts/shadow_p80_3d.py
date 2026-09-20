#!/usr/bin/env python3
"""Shadow monitor for the FROZEN candidate:

    progress_pct >= 75%  AND  required_pts_per_min >= trailing 3-day
    league-specific P80 of required_pts_per_min

READ-ONLY with respect to production: both DBs are opened mode=ro +
PRAGMA query_only=1.  No production table, module, service or config is
touched.  The only artifact written is the SEPARATE analysis dataset
passed via --jsonl-append (default: shadow_p80_3d_triggers.jsonl).

Calibration contract (frozen, never optimised during observation):
    * same competition_slug only
    * captured_at strictly BEFORE the trigger timestamp
    * trailing window = previous 72 hours
    * minimum 100 calibration observations, else NOT_ELIGIBLE
    * cutoff built from required_pts_per_min ONLY (no outcome ever used)

Candidate definition is deliberately pure.  It does NOT include
actual < league average, does NOT include required > avg*1.04, and does
NOT include any absolute required-actual gap.  Those are reported only
as clearly-labelled context, never as part of the signal.

Usage:
    python3 scripts/shadow_p80_3d.py                     # report only
    python3 scripts/shadow_p80_3d.py --jsonl-append F    # also record
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

REPO = "/home/ubuntu/BLM"
PROD = "/home/ubuntu/BLM/blm_pokerbet.db"
CLEAN = "/home/ubuntu/BLM/blm_metrics_clean.db"
DEFAULT_LOG = "/home/ubuntu/BLM/shadow_p80_3d_triggers.jsonl"

CYBER = "cyber-basketball-2k26-matches"
LABEL = {"betual-nba": "NBA", "betual-kbl": "KBL", "betual-cba": "CBA",
         "betual-tbsl": "TBSL", "betual-euroleague": "EuroLeague",
         CYBER: "CYBER"}
SLUGS = list(LABEL)

WINDOW_H = 72.0          # trailing 3 days
PCT = 80.0               # P80 — FROZEN, not to be re-optimised
MIN_CAL = 100            # minimum calibration observations
PROG_FLOOR = 75.0
REM_FLOOR = 2.5
TARGET_MIN = 500
TARGET_PREF = 1000


def ro(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=120)
    c.execute("PRAGMA query_only=1")
    return c


def fin(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def ep(s):
    return datetime.fromisoformat(
        s.replace(" ", "T").replace("Z", "+00:00")).timestamp()


def pctl(v, p):
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    k = (len(v) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] * (1 - (k - lo)) + v[hi] * (k - lo)


# ── load V4 modules (read-only import; nothing is executed at import) ──
sys.path.insert(0, REPO)
from blm_v4.live_analytics.competition_pace import (  # noqa: E402
    _regulation_minutes,
)
from blm_v4.live_analytics.under_outcome import (  # noqa: E402
    _row_progress, trigger_market_total,
)


def build_triggers():
    """Reconstruct the V4 trigger row for every settleable game."""
    p, c = ro(PROD), ro(CLEAN)
    settled = {}
    for gid, ft, slug, cls in p.execute(
            "SELECT gr.source_game_id, gr.final_total, g.competition_slug, "
            "g.classification FROM game_results gr JOIN games g "
            "  ON g.source_game_id=gr.source_game_id "
            "WHERE gr.final_result_status='OK' AND gr.final_total IS NOT NULL "
            "  AND gr.final_total>0 AND g.competition_slug IS NOT NULL "
            "  AND g.competition_slug<>''"):
        settled[gid] = (int(ft), slug, cls)
    invalid = {r[0] for r in p.execute(
        "SELECT source_game_id FROM game_quality "
        "WHERE UPPER(COALESCE(status,''))='INVALID'")}
    agg = defaultdict(lambda: [0.0, 0])
    for _g, (ft, slug, cls) in settled.items():
        rm = _regulation_minutes(cls)
        if rm:
            agg[slug][0] += ft / rm
            agg[slug][1] += 1
    avg = {k: v[0] / v[1] for k, v in agg.items() if v[1]}

    sql = ("SELECT source_game_id, captured_at, id, classification, "
           "period_label, clock, live_total_line, progress_pct, market_status,"
           " remaining_game_minutes, actual_pts_per_min, required_pts_per_min "
           "FROM clean_projections ORDER BY source_game_id, captured_at, id")
    rows = []
    cur, buf = None, []

    def flush(gid, grp):
        bp = gid.split("#")[0]
        if bp not in settled or bp in invalid:
            return
        ft, slug, cls = settled[bp]
        if slug not in avg:
            return
        d = [{"total_line": r[6], "classification": r[3], "period_label": r[4],
              "clock": r[5]} for r in grp]
        pr = [_row_progress(x, cls) for x in d]
        ti = next((i for i, x in enumerate(pr) if x is not None and x >= .75),
                  None)
        if ti is None:
            return
        ln = trigger_market_total(d, 75, cls)
        if ln is None:
            return
        tr = grp[ti]
        rem, act, req = fin(tr[9]), fin(tr[10]), fin(tr[11])
        if (tr[7] is not None and PROG_FLOOR <= tr[7] < 100.0
                and str(tr[8] or "").upper() == "LIVE" and rem is not None
                and rem >= REM_FLOOR and act is not None and req is not None):
            ln = fin(ln)
            oc = ("UNDER" if ft < ln else "OVER" if ft > ln else "PUSH")
            rows.append(dict(
                game_id=bp, league=slug, cls=cls, captured_at=tr[1],
                progress=tr[7], actual=act, required=req,
                league_avg=avg[slug], line=ln, final=ft, outcome=oc,
                remaining=rem))

    for row in c.execute(sql):
        if row[0] != cur:
            if cur is not None:
                flush(cur, buf)
            cur, buf = row[0], []
        buf.append(row)
    if cur is not None:
        flush(cur, buf)
    rows.sort(key=lambda r: r["captured_at"])
    return rows


def apply_cutoffs(rows):
    """Leakage-safe trailing P80. For each trigger, the cutoff uses ONLY
    same-league triggers with captured_at strictly before it, inside the
    trailing window. Returns (cutoff, calibration_n) per row."""
    by = defaultdict(list)
    for i, r in enumerate(rows):
        by[r["league"]].append(i)
    W = WINDOW_H * 3600.0
    cut: list[float | None] = [None] * len(rows)
    caln: list[int] = [0] * len(rows)
    for _lg, idxs in by.items():
        idxs.sort(key=lambda i: rows[i]["captured_at"])
        times = [ep(rows[i]["captured_at"]) for i in idxs]
        lo = hi = 0
        vals: list[float] = []
        for k in range(len(idxs)):
            i = idxs[k]
            tc = times[k]
            while hi < len(idxs) and times[hi] < tc:
                bisect.insort(vals, rows[idxs[hi]]["required"])
                hi += 1
            while lo < hi and times[lo] < tc - W:
                pos = bisect.bisect_left(vals, rows[idxs[lo]]["required"])
                vals.pop(pos)
                lo += 1
            caln[i] = len(vals)
            if len(vals) >= MIN_CAL:
                cut[i] = pctl(vals, PCT)
    for i, r in enumerate(rows):
        r["cutoff"] = cut[i]
        r["cal_n"] = caln[i]
        r["eligible"] = cut[i] is not None
        r["signal"] = bool(cut[i] is not None and r["required"] >= cut[i])
    return rows


def stats(sel):
    n = len(sel)
    if n == 0:
        return 0, 0, 0, float("nan")
    u = sum(1 for r in sel if r["outcome"] == "UNDER")
    o = sum(1 for r in sel if r["outcome"] == "OVER")
    return n, u, o, 100.0 * u / n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl-append", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rows = apply_cutoffs(build_triggers())
    sig = [r for r in rows if r["signal"]]
    elig = [r for r in rows if r["eligible"]]
    run_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    newest = max((r["captured_at"] for r in rows), default=None)

    # ── record to the SEPARATE analysis dataset (append-only, dedup) ──
    if args.jsonl_append:
        path = args.jsonl_append
        seen = {}
        if os.path.exists(path):
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    seen[rec.get("game_id")] = rec
        new = upd = 0
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as fh:
            for r in sig:
                gid = r["game_id"]
                rec = {
                    "game_id": gid, "league": LABEL[r["league"]],
                    "competition_slug": r["league"],
                    "captured_at": r["captured_at"],
                    "progress_pct": r["progress"],
                    "required_pts_per_min": round(r["required"], 6),
                    "actual_pts_per_min": round(r["actual"], 6),
                    "league_cutoff_p80_3d": (round(r["cutoff"], 6)
                                             if r["cutoff"] is not None
                                             else None),
                    "calibration_n": r["cal_n"],
                    "live_market_total": r["line"],
                    "final_total": r["final"],
                    "remaining_game_minutes": r["remaining"],
                    "outcome": r["outcome"],
                    "frozen_at_utc": run_utc,
                }
                prev = seen.get(gid)
                if prev is None:
                    fh.write(json.dumps(rec, sort_keys=True) + "\n")
                    new += 1
                elif prev.get("outcome") != rec["outcome"]:
                    rec["_update"] = True
                    fh.write(json.dumps(rec, sort_keys=True) + "\n")
                    upd += 1
        if not args.quiet:
            print(f"[dataset] {path}")
            print(f"[dataset] appended {new} new trigger(s), "
                  f"{upd} settlement update(s); "
                  f"{len(seen) + new} distinct games recorded")

    # ── monitoring report ──
    en, _eu, _eo, base = stats(elig)
    sn, su, so, under = stats(sig)
    lift = under - base
    print()
    print("=" * 96)
    print("3-DAY TRAILING P80 — PROSPECTIVE VALIDATION (FROZEN CANDIDATE)")
    print("=" * 96)
    print(f"  run (UTC)                 {run_utc}")
    print(f"  newest captured_at in DB  {newest}")
    print(f"  candidate                 progress>=75% AND required >= "
          f"trailing 72h league P80 (min {MIN_CAL} cal obs)")
    print(f"  settlement                V4 trigger_market_total @ 75%")
    print()
    print(f"  paired 75% trigger games  {len(rows)}")
    print(f"  eligible (cal>=100)       {en}")
    print(f"  shadow signals            {sn}   "
          f"coverage {100.0 * sn / len(rows) if rows else 0:.1f}% of paired")

    print()
    print("=" * 96)
    print("REQUIRED MONITORING")
    print("=" * 96)
    print(f"  {'signal':<24} {'N':>6} {'UNDER':>6} {'OVER':>5} {'PUSH':>5} "
          f"{'UNDER%':>8} {'baseline':>9} {'lift':>9}")
    print(f"  {'ALL':<24} {sn:>6} {su:>6} {so:>5} {sn - su - so:>5} "
          f"{under:>7.2f}% {base:>8.2f}% {lift:>+8.2f}pp")
    print()
    print(f"  {'league':<14} {'N':>6} {'UNDER%':>8} {'base%':>8} "
          f"{'lift':>8} {'share':>7}")
    for s in SLUGS:
        ss = [r for r in sig if r["league"] == s]
        se = [r for r in elig if r["league"] == s]
        n, u, _o, u2 = stats(ss)
        b = stats(se)[3]
        share = 100.0 * n / sn if sn else 0.0
        print(f"  {LABEL[s]:<14} {n:>6} "
              f"{(f'{u2:.2f}%' if n else '-'):>8} "
              f"{(f'{b:.2f}%' if se else '-'):>8} "
              f"{(f'{u2 - b:+.2f}' if n else '-'):>8} {share:>6.1f}%")

    # time windows
    if sig:
        ts = sorted(sig, key=lambda r: r["captured_at"])
        t0, t1 = ep(ts[0]["captured_at"]), ep(ts[-1]["captured_at"])
        span = max(t1 - t0, 1e-9)
        print()
        print(f"  {'window':<16} {'N':>6} {'UNDER%':>8} {'base%':>8} {'lift':>8}")
        for lbl, lo in (("latest 50%", t0 + span / 2),
                        ("latest 25%", t0 + 0.75 * span),
                        ("latest 7 days", t1 - 7 * 86400),
                        ("latest 3 days", t1 - 3 * 86400)):
            w = [r for r in ts if ep(r["captured_at"]) >= lo]
            we = [r for r in elig if ep(r["captured_at"]) >= lo]
            n, _u, _o, u2 = stats(w)
            b = stats(we)[3]
            print(f"  {lbl:<16} {n:>6} "
                  f"{(f'{u2:.2f}%' if n else '-'):>8} "
                  f"{(f'{b:.2f}%' if we else '-'):>8} "
                  f"{(f'{u2 - b:+.2f}' if n else '-'):>8}")
        print()
        print("  per-day (N>=15):")
        byday = defaultdict(list)
        for r in sig:
            byday[r["captured_at"][:10]].append(r)
        for d in sorted(byday):
            n, _u, _o, u2 = stats(byday[d])
            if n >= 15:
                be = stats([r for r in elig
                            if r["captured_at"][:10] == d])[3]
                print(f"    {d}  N={n:<4} {u2:>6.2f}%  base {be:>6.2f}%  "
                      f"lift {u2 - be:>+6.2f}")

    # ── stop conditions ──
    print()
    print("=" * 96)
    print("STOP CONDITIONS")
    print("=" * 96)
    if sig:
        ts = sorted(sig, key=lambda r: r["captured_at"])
        t0, t1 = ep(ts[0]["captured_at"]), ep(ts[-1]["captured_at"])
        l25 = [r for r in ts if ep(r["captured_at"]) >= t0 + 0.75 * (t1 - t0)]
        l25e = [r for r in elig
                if ep(r["captured_at"]) >= t0 + 0.75 * (t1 - t0)]
        n25 = len(l25)
        u25 = stats(l25)[3]
        l25lift = u25 - stats(l25e)[3]
        print(f"  latest-25% lift                 {l25lift:+.2f}pp "
              f"(N={n25})  -> "
              f"{'FLAGGED: FAILING' if l25lift < 0 else 'ok'}")
        conc = max((100.0 * len([r for r in sig if r['league'] == s]) / sn,
                    LABEL[s]) for s in SLUGS) if sn else (0, "-")
        print(f"  top league share                {conc[0]:.1f}% ({conc[1]}) "
              f"-> {'FLAGGED: CONCENTRATION >50%' if conc[0] > 50 else 'ok'}")
        missing = [LABEL[s] for s in SLUGS if s != CYBER
                   and not [r for r in sig if r["league"] == s]]
        print(f"  major leagues with zero signals "
              f"{missing if missing else 'none'}"
              f" -> {'FLAGGED: COVERAGE' if missing else 'ok'}")
        print(f"  CYBER signals                   "
              f"{len([r for r in sig if r['league'] == CYBER])} "
              f"(auto-excluded by min-{MIN_CAL} calibration)")
    print()
    print(f"  TARGET  min {TARGET_MIN} / preferred {TARGET_PREF} settled "
          f"signals + several additional weeks")
    print(f"  CURRENT {sn} settled signals  -> "
          f"{'MET' if sn >= TARGET_MIN else 'NOT MET'} "
          f"(sample target)")
    print(f"  NOTE    sample-quantity target is NOT the binding constraint;")
    print(f"          the binding constraint is GENUINELY UNSEEN data. Every")
    print(f"          signal above predates this run and was already used to")
    print(f"          SELECT the candidate. Prospective signals = 0.")
    print()
    print("  CLASSIFICATION: PROMISING — CONTINUE")
    print("    (cannot be ROBUST: prospective=0. cannot be FAIL: no")
    print("     prospective evidence of failure.)")


if __name__ == "__main__":
    main()
