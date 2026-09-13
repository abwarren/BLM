#!/usr/bin/env python3
"""READ-ONLY threshold sweep for a progress-dependent UNDER alert policy.

Answers the question: instead of choosing one relaxed rule from one result,
sweep the REQUIRED-vs-league-average margin and measure, at 50% and 75%,
by league:

    N | UNDER | UNDER% | lift vs checkpoint baseline | false-alert rate

for    required_pts_per_min > league_average_pace + margin
margins 0.00 .. 0.50.

Two league references are computed side by side:
  GLOBAL  — mean(final/min) over ALL settled games (what production ships)
  PIT     — mean over same-league games settled STRICTLY BEFORE this
            checkpoint's captured_at (result_at < T, n>=30) — the
            look-ahead-free forward-test reference

Databases opened READ-ONLY.  No writes except the report file.
"""
from __future__ import annotations

import bisect
import math
import sqlite3
from collections import defaultdict

PROD = "/home/ubuntu/BLM/blm_pokerbet.db"
CLEAN = "/home/ubuntu/BLM/blm_metrics_clean.db"
OUT = "/home/ubuntu/BLM/analysis_alert_threshold_sweep_2026-09-13.txt"

REGMIN = {"BETUAL_NBA": 40.0, "CYBER_2K26": 48.0}
SHORT = {"betual-nba": "NBA", "betual-kbl": "KBL", "betual-cba": "CBA",
         "betual-tbsl": "TBSL", "betual-euroleague": "EUROLEAGUE",
         "cyber-basketball-2k26-matches": "CYBER"}
ORDER = ["betual-nba", "betual-kbl", "betual-cba", "betual-tbsl",
         "betual-euroleague", "cyber-basketball-2k26-matches"]
MARGINS = [0.00, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
RELMARGINS = [0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20]
CHECKPOINTS = [(50, 40.0, 60.0), (75, 65.0, 85.0)]
MIN_PIT_N = 30

REPORT: list[str] = []


def out(s: str = "") -> None:
    REPORT.append(s)
    print(s)


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=60)


def fin(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def pct(n, d):
    return (100.0 * n / d) if d else float("nan")


def main() -> None:
    p, c = ro(PROD), ro(CLEAN)

    # settled games incl. result_at for the PIT reference
    settled = {}
    for gid, ft, slug, cls, rat in p.execute(
        "SELECT gr.source_game_id, gr.final_total, g.competition_slug, "
        "       g.classification, gr.result_at "
        "FROM game_results gr JOIN games g ON g.source_game_id=gr.source_game_id "
        "WHERE gr.final_result_status='OK' AND gr.final_total IS NOT NULL "
        "  AND gr.final_total>0 AND g.competition_slug IS NOT NULL "
        "  AND g.competition_slug<>''"
    ):
        settled[gid] = (int(ft), slug, cls, rat)

    # ── GLOBAL reference ──
    agg = defaultdict(lambda: [0.0, 0])
    for _g, (ft, slug, cls, _r) in settled.items():
        rm = REGMIN.get(cls)
        if rm and ft > 0:
            agg[slug][0] += ft / rm
            agg[slug][1] += 1
    avg_global = {k: v[0] / v[1] for k, v in agg.items() if v[1]}

    # ── PIT reference: per league, sorted result_at + prefix sums ──
    pit = defaultdict(list)          # slug -> [(result_at, pace)]
    for _g, (ft, slug, cls, rat) in settled.items():
        rm = REGMIN.get(cls)
        if rm and ft > 0 and rat:
            pit[slug].append((rat, ft / rm))
    pit_index = {}
    for slug, rows in pit.items():
        rows.sort()
        caps = [r[0] for r in rows]
        pref = [0.0]
        for _t, v in rows:
            pref.append(pref[-1] + v)
        pit_index[slug] = (caps, pref)

    def pit_avg(slug, t):
        idx = pit_index.get(slug)
        if not idx or not t:
            return None
        caps, pref = idx
        i = bisect.bisect_left(caps, t)      # strictly before t
        if i < MIN_PIT_N:
            return None
        return (pref[i] - pref[0]) / i

    # ── checkpoint observations ──
    def load(target, lo, hi):
        best = {}
        for gid, prog, tot, el, rem, line, ts in c.execute(
            "SELECT source_game_id, progress_pct, current_total_points, "
            "       elapsed_game_minutes, remaining_game_minutes, "
            "       live_total_line, captured_at "
            "FROM clean_projections "
            "WHERE progress_pct>=? AND progress_pct<=? AND progress_pct<100 "
            "  AND (terminal IS NULL OR terminal=0)",
            (lo, hi),
        ):
            b = gid.split("#")[0]
            if b not in settled:
                continue
            tot, el, rem, line = (fin(tot), fin(el), fin(rem), fin(line))
            if (tot is None or el is None or rem is None or line is None
                    or el <= 0 or rem <= 0):
                continue
            d = abs(prog - target)
            if b not in best or d < best[b][0]:
                best[b] = (d, prog, tot, el, rem, line, ts)
        recs = []
        for b, (d, prog, tot, el, rem, line, ts) in best.items():
            ft, slug, cls, _r = settled[b]
            ag = avg_global.get(slug)
            if ag is None:
                continue
            recs.append({
                "gid": b, "league": slug, "prog": prog, "tot": tot,
                "el": el, "rem": rem, "line": line, "ts": ts,
                "act": round(tot / el, 2), "req": round((line - tot) / rem, 2),
                "avg": ag, "avg_pit": pit_avg(slug, ts), "final": ft,
                "outcome": ("UNDER" if ft < line
                            else "OVER" if ft > line else "PUSH"),
            })
        return recs

    R = {cp: load(cp, lo, hi) for cp, lo, hi in CHECKPOINTS}

    def stats(rs):
        n = len(rs)
        u = sum(1 for r in rs if r["outcome"] == "UNDER")
        o = sum(1 for r in rs if r["outcome"] == "OVER")
        return n, u, o, pct(u, n), pct(o, n)

    out("=" * 100)
    out("THRESHOLD SWEEP — required_pts_per_min > league_average_pace + margin")
    out("READ-ONLY, 2026-09-13.  Lifts are vs the checkpoint's own overall "
        "UNDER baseline.")
    out("=" * 100)
    out("")

    # ── checkpoint baselines ──
    for cp, _, _ in CHECKPOINTS:
        n, u, o, up, _ = stats(R[cp])
        out(f"  {cp}% checkpoint baseline: N={n}  UNDER={u}  UNDER%={up:.2f}")
    out("")

    for refname, key in (("GLOBAL reference (production)", "avg"),
                         ("POINT-IN-TIME reference (look-ahead free)", "avg_pit")):
        out("=" * 100)
        out(f"SWEEP — {refname}")
        out("=" * 100)
        for cp, _, _ in CHECKPOINTS:
            base_n, base_u, _, base_up, _ = stats(R[cp])
            out("")
            out(f"  ── {cp}% checkpoint (baseline UNDER%={base_up:.2f}) ──")
            out(f"  {'margin':>6} {'N':>6} {'UNDER':>6} {'UNDER%':>7} "
                f"{'lift_pp':>8} {'false%':>7}   per-league UNDER% "
                f"(N)")
            for m in MARGINS:
                coh = [r for r in R[cp]
                       if r[key] is not None and r["req"] > r[key] + m]
                n, u, o, up, fp = stats(coh)
                lift = up - base_up if n else float("nan")
                per = []
                for slug in ORDER:
                    rs = [r for r in coh if r["league"] == slug]
                    if rs:
                        nn, uu, _, uu_p, _ = stats(rs)
                        per.append(f"{SHORT[slug]} {uu_p:.0f}%({nn})")
                out(f"  {m:>6.2f} {n:>6} {u:>6} {up:>6.2f}% {lift:>+7.2f} "
                    f"{fp:>6.2f}%   " + " ".join(per))
        out("")

    # ── RELATIVE margin sweep (league-fair: a fixed pts/min margin is a
    #    different relative move in each league) ──
    out("=" * 100)
    out("RELATIVE-MARGIN SWEEP — required > league_average_pace * (1 + m)")
    out("=" * 100)
    for cp, _, _ in CHECKPOINTS:
        base_n, base_u, _, base_up, _ = stats(R[cp])
        out("")
        out(f"  ── {cp}% checkpoint (baseline UNDER%={base_up:.2f}) ──")
        out(f"  {'rel_m':>6} {'N':>6} {'UNDER':>6} {'UNDER%':>7} {'lift_pp':>8} "
            f"{'false%':>7}   per-league UNDER% (N)")
        for m in RELMARGINS:
            coh = [r for r in R[cp] if r["req"] > r["avg"] * (1 + m)]
            n, u, o, up, fp = stats(coh)
            lift = up - base_up if n else float("nan")
            per = []
            for slug in ORDER:
                rs = [r for r in coh if r["league"] == slug]
                if rs:
                    nn, uu, _, uu_p, _ = stats(rs)
                    per.append(f"{SHORT[slug]} {uu_p:.0f}%({nn})")
            out(f"  {m:>6.2f} {n:>6} {u:>6} {up:>6.2f}% {lift:>+7.2f} "
                f"{fp:>6.2f}%   " + " ".join(per))
    out("")

    # ── league × checkpoint × margin detail (GLOBAL) ──
    out("=" * 100)
    out("LEAGUE BREAKDOWN (GLOBAL reference)")
    out("=" * 100)
    for cp, _, _ in CHECKPOINTS:
        out("")
        out(f"  ── {cp}% ──")
        out(f"  {'league':<11} {'margin':>6} {'N':>6} {'UNDER':>6} {'UNDER%':>7} "
            f"{'false%':>7}")
        for slug in ORDER:
            league_rs = [r for r in R[cp] if r["league"] == slug]
            base_n, base_u, _, base_up, _ = stats(league_rs)
            for m in MARGINS:
                coh = [r for r in league_rs if r["req"] > r["avg"] + m]
                n, u, o, up, fp = stats(coh)
                if n == 0:
                    out(f"  {SHORT[slug]:<11} {m:>6.2f} {n:>6}      -       -"
                        f"       -")
                    continue
                out(f"  {SHORT[slug]:<11} {m:>6.2f} {n:>6} {u:>6} {up:>6.2f}% "
                    f"{fp:>6.2f}%")
    out("")

    # ── current alert vs relaxed (GLOBAL), for the policy decision ──
    out("=" * 100)
    out("CURRENT ALERT vs RELAXED, BY CHECKPOINT (GLOBAL reference)")
    out("=" * 100)
    out(f"  {'cp':>4} {'rule':<38} {'N':>6} {'UNDER':>6} {'UNDER%':>7} "
        f"{'lift':>7} {'false%':>7}")
    for cp, _, _ in CHECKPOINTS:
        base_n, base_u, _, base_up, _ = stats(R[cp])
        rules = [
            ("current alert (act<req AND act<avg)",
             lambda r: r["act"] < r["req"] and r["act"] < r["avg"]),
            ("required>avg",
             lambda r: r["req"] > r["avg"]),
            ("required>avg+0.10",
             lambda r: r["req"] > r["avg"] + 0.10),
            ("required>avg+0.20",
             lambda r: r["req"] > r["avg"] + 0.20),
            ("required>avg AND current alert",
             lambda r: r["req"] > r["avg"]
             and r["act"] < r["req"] and r["act"] < r["avg"]),
        ]
        for label, f in rules:
            coh = [r for r in R[cp] if f(r)]
            n, u, o, up, fp = stats(coh)
            out(f"  {cp:>4} {label:<38} {n:>6} {u:>6} {up:>6.2f}% "
                f"{up-base_up:>+6.2f} {fp:>6.2f}%")
    out("")

    # ── PIT-safe headline for the key margins ──
    out("=" * 100)
    out("POINT-IN-TIME SANITY (does the edge survive with no look-ahead?)")
    out("=" * 100)
    out(f"  {'cp':>4} {'margin':>6} {'N':>6} {'UNDER':>6} {'UNDER%':>7}")
    for cp, _, _ in CHECKPOINTS:
        for m in (0.00, 0.10, 0.20):
            coh = [r for r in R[cp]
                   if r["avg_pit"] is not None and r["req"] > r["avg_pit"] + m]
            n, u, o, up, _ = stats(coh)
            out(f"  {cp:>4} {m:>6.2f} {n:>6} {u:>6} {up:>6.2f}%")
    # how many obs lost the PIT reference to the n>=30 gate
    for cp, _, _ in CHECKPOINTS:
        miss = sum(1 for r in R[cp] if r["avg_pit"] is None)
        out(f"  ({cp}%: {miss}/{len(R[cp])} observations lacked a mature "
            f"point-in-time reference n>={MIN_PIT_N})")
    out("")

    # ── NBA 75% focus ──
    out("=" * 100)
    out("NBA @ 75% — the flagged high-priority cell, across margins")
    out("=" * 100)
    nba75 = [r for r in R[75] if r["league"] == "betual-nba"]
    nb, nu, _, nbup, _ = stats(nba75)
    out(f"  baseline NBA@75%: N={nb} UNDER={nu} UNDER%={nbup:.2f}")
    out(f"  {'margin':>6} {'N':>6} {'UNDER':>6} {'UNDER%':>7} {'false%':>7}")
    for m in MARGINS:
        coh = [r for r in nba75 if r["req"] > r["avg"] + m]
        n, u, o, up, fp = stats(coh)
        if n:
            out(f"  {m:>6.2f} {n:>6} {u:>6} {up:>6.2f}% {fp:>6.2f}%")
        else:
            out(f"  {m:>6.2f} {n:>6}      -       -       -")

    with open(OUT, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    out("")
    out(f"[report written to {OUT}]")


if __name__ == "__main__":
    main()
