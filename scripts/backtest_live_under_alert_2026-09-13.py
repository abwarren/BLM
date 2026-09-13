#!/usr/bin/env python3
"""READ-ONLY backtest of the CURRENT BLM V4 LIVE UNDER ALERT condition.

Condition (blm_v4/live_analytics/under_alert.py — the ONLY definition):

    active = actual_pts_per_min < required_pts_per_min
             AND actual_pts_per_min < league_average_pace

league_average_pace = competition_pace_reference(competition_slug).avg_pace
                      = mean(final_total / regulation_minutes) over settled
                        games of that competition (competition_pace.py).

Reproduced at the 25% / 50% / 75% progress checkpoints.  Databases opened
READ-ONLY (uri mode=ro).  Nothing is written anywhere but the report file.

V4 field mappings confirmed against the live code:
  actual_pts_per_min   = current_total_points / elapsed_game_minutes
  required_pts_per_min = (live_total_line - current_total_points)
                         / remaining_game_minutes
  (api.py projects these; live rounds both to 2 dp)
"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict

PROD = "/home/ubuntu/BLM/blm_pokerbet.db"
CLEAN = "/home/ubuntu/BLM/blm_metrics_clean.db"
OUT = "/home/ubuntu/BLM/analysis_live_under_alert_backtest_2026-09-13.txt"

REGMIN = {"BETUAL_NBA": 40.0, "CYBER_2K26": 48.0}
LEAGUE_SHORT = {
    "betual-nba": "NBA", "betual-kbl": "KBL", "betual-cba": "CBA",
    "betual-tbsl": "TBSL", "betual-euroleague": "EUROLEAGUE",
    "cyber-basketball-2k26-matches": "CYBER(2K26)",
}
COMP_ORDER = ["betual-nba", "betual-kbl", "betual-cba", "betual-tbsl",
              "betual-euroleague", "cyber-basketball-2k26-matches"]
CHECKPOINTS = [(25, 15.0, 35.0), (50, 40.0, 60.0), (75, 65.0, 85.0)]

REPORT: list[str] = []


def out(s: str = "") -> None:
    REPORT.append(s)
    print(s)


def ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)


def fin(x):
    """Finite float or None (excludes None, bool, nan, inf)."""
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
    con_p, con_c = ro(PROD), ro(CLEAN)

    # ── settled games: authoritative final + league ──
    settled = {}
    for gid, ft, slug, cls in con_p.execute(
        "SELECT gr.source_game_id, gr.final_total, g.competition_slug, "
        "       g.classification "
        "FROM game_results gr JOIN games g ON g.source_game_id=gr.source_game_id "
        "WHERE gr.final_result_status='OK' AND gr.final_total IS NOT NULL "
        "  AND gr.final_total>0 AND g.competition_slug IS NOT NULL "
        "  AND g.competition_slug<>''"
    ):
        settled[gid] = (int(ft), slug, cls)

    # ── league-specific pace reference (competition_pace_reference) ──
    agg = defaultdict(lambda: [0.0, 0])
    for _g, (ft, slug, cls) in settled.items():
        rm = REGMIN.get(cls)
        if rm and ft > 0:
            agg[slug][0] += ft / rm
            agg[slug][1] += 1
    league_avg = {k: v[0] / v[1] for k, v in agg.items() if v[1]}

    # ── checkpoint observations (closest valid to target, per game) ──
    def load(target, lo, hi):
        best = {}
        for gid, prog, tot, el, rem, line, act_s, req_s in con_c.execute(
            "SELECT source_game_id, progress_pct, current_total_points, "
            "       elapsed_game_minutes, remaining_game_minutes, "
            "       live_total_line, actual_pts_per_min, required_pts_per_min "
            "FROM clean_projections "
            "WHERE progress_pct>=? AND progress_pct<=? AND progress_pct<100 "
            "  AND (terminal IS NULL OR terminal=0)",
            (lo, hi),
        ):
            base = gid.split("#")[0]
            if base not in settled:
                continue
            el, rem, line, act_s, req_s = (fin(el), fin(rem), fin(line),
                                           fin(act_s), fin(req_s))
            tot = fin(tot)
            # enforce valid elapsed/remaining/line; exclude non-finite
            if (el is None or el <= 0 or rem is None or rem <= 0
                    or line is None or tot is None):
                continue
            d = abs(prog - target)
            cur = best.get(base)
            if cur is None or d < cur["d"]:
                best[base] = {"d": d, "gid": base, "prog": prog, "tot": tot,
                              "el": el, "rem": rem, "line": line,
                              "act_s": act_s, "req_s": req_s}
        return best

    # ── build records with LIVE-faithful rounded fields ──
    def build(target, lo, hi):
        recs = []
        for base, o in load(target, lo, hi).items():
            ft, slug, cls = settled[base]
            avg = league_avg.get(slug)
            if avg is None:                     # no league reference -> ineligible
                continue
            # recompute exactly as api.py does, rounded to 2 dp (live semantics)
            act = round(o["tot"] / o["el"], 2)
            req = round((o["line"] - o["tot"]) / o["rem"], 2)
            outcome = ("UNDER" if ft < o["line"]
                       else "OVER" if ft > o["line"] else "PUSH")
            recs.append({
                "gid": base, "league": slug, "cp": target, "prog": o["prog"],
                "score": int(o["tot"]), "line": o["line"], "el": o["el"],
                "rem": o["rem"], "act": act, "req": req, "avg": avg,
                "final": ft, "outcome": outcome,
                "alert": act < req and act < avg,
                "req_gt_avg": req > avg,
                "act_lt_req": act < req,
                "act_lt_avg": act < avg,
                "act_s": o["act_s"], "req_s": o["req_s"],
            })
        return recs

    R = {}
    for cp, lo, hi in CHECKPOINTS:
        R[cp] = build(cp, lo, hi)

    def cell(rs):
        u = sum(1 for r in rs if r["outcome"] == "UNDER")
        o = sum(1 for r in rs if r["outcome"] == "OVER")
        p = sum(1 for r in rs if r["outcome"] == "PUSH")
        return len(rs), u, o, p

    # ── header ──
    out("=" * 96)
    out("BLM V4 — LIVE UNDER ALERT BACKTEST (READ-ONLY), 2026-09-13")
    out("  condition: actual_pts_per_min < required_pts_per_min "
        "AND actual_pts_per_min < league_average_pace")
    out("=" * 96)
    out(f"source: {PROD} + {CLEAN} (both mode=ro)")
    out(f"settled games: {len(settled)}   leagues with a pace reference: "
        f"{len(league_avg)}")
    out("")
    out("LEAGUE-SPECIFIC AVERAGE PACE (competition_pace_reference):")
    for slug in COMP_ORDER:
        if slug in league_avg:
            out(f"  {LEAGUE_SHORT[slug]:<12} {league_avg[slug]:.4f} pts/min "
                f"  (n={agg[slug][1]})")
    out("")

    # ── checkpoint selection transparency ──
    out("CHECKPOINT OBSERVATIONS (closest valid to target, per game):")
    for cp, lo, hi in CHECKPOINTS:
        prog = sorted(r["prog"] for r in R[cp])
        exact = sum(1 for r in R[cp] if abs(r["prog"] - cp) < 1e-9)
        out(f"  {cp}%: n={len(R[cp])}  window[{lo:.0f},{hi:.0f}]  "
            f"exact={exact}  median progress={prog[len(prog)//2]:.2f}")
    out("")

    # ── 1. alert population per checkpoint ──
    out("=" * 96)
    out("1. LIVE ALERT POPULATION PER CHECKPOINT")
    out("=" * 96)
    out(f"  {'cp':>4} {'alert_N':>8} {'UNDER':>7} {'OVER':>6} {'PUSH':>5} "
        f"{'UNDER%':>8}")
    tot = [0, 0, 0, 0]
    for cp, lo, hi in CHECKPOINTS:
        al = [r for r in R[cp] if r["alert"]]
        n, u, o, p = cell(al)
        for i, v in enumerate((n, u, o, p)):
            tot[i] += v
        out(f"  {cp:>4} {n:>8} {u:>7} {o:>6} {p:>5} {pct(u,n):>7.2f}%")
    n, u, o, p = tot
    out(f"  {'ALL':>4} {n:>8} {u:>7} {o:>6} {p:>5} {pct(u,n):>7.2f}%")
    out("  (ALL CHECKPOINTS COMBINED — observation-level, as the live engine "
        "evaluates it)")

    # also equal-game (each game once across checkpoints is impossible; show
    # per-checkpoint already game-level).  Combined distinct games:
    games_all = {r["gid"] for cp, _, _ in CHECKPOINTS for r in R[cp]
                 if r["alert"]}
    ug = set()
    for cp, _, _ in CHECKPOINTS:
        for r in R[cp]:
            if r["alert"] and r["outcome"] == "UNDER":
                ug.add(r["gid"])
    out(f"  distinct games appearing in any alert cohort: {len(games_all)}")
    out("")

    # ── 2. per league ──
    out("=" * 96)
    out("2. PER-LEAGUE LIVE ALERT (all checkpoints combined)")
    out("=" * 96)
    out(f"  {'League':<12} {'alert_N':>8} {'UNDER':>7} {'OVER':>6} {'PUSH':>5} "
        f"{'UNDER%':>8}")
    allal = [r for cp, _, _ in CHECKPOINTS for r in R[cp] if r["alert"]]
    for slug in COMP_ORDER:
        al = [r for r in allal if r["league"] == slug]
        n, u, o, p = cell(al)
        if n == 0:
            out(f"  {LEAGUE_SHORT[slug]:<12} {n:>8}  (no qualifying alerts)")
            continue
        out(f"  {LEAGUE_SHORT[slug]:<12} {n:>8} {u:>7} {o:>6} {p:>5} "
            f"{pct(u,n):>7.2f}%")
    out("")

    # ── 4. baseline comparison ──
    out("=" * 96)
    out("4. BASELINE COMPARISON (alert vs non-alert, same checkpoints)")
    out("=" * 96)
    out(f"  {'cp':>4} {'alertUNDER%':>12} {'nonalertUNDER%':>15} "
        f"{'lift_pp':>9} {'rel_lift':>10} {'alert_N':>8}")
    for cp, lo, hi in CHECKPOINTS:
        al = [r for r in R[cp] if r["alert"]]
        na = [r for r in R[cp] if not r["alert"]]
        an, au, _, _ = cell(al)
        nn, nu, _, _ = cell(na)
        a_up, n_up = pct(au, an), pct(nu, nn)
        lift = a_up - n_up
        rel = (lift / n_up * 100) if n_up else float("nan")
        out(f"  {cp:>4} {a_up:>11.2f}% {n_up:>14.2f}% {lift:>+8.2f} {rel:>+9.1f}% "
            f"{an:>8}")
    out("")

    # ── 5. REQUIRED > AVERAGE (separate research cohort) ──
    out("=" * 96)
    out("5. REQUIRED > AVERAGE cohort (SEPARATE from the live alert)")
    out("=" * 96)
    out(f"  {'cp':>4} {'N':>8} {'UNDER':>7} {'OVER':>6} {'PUSH':>5} {'UNDER%':>8}")
    tot2 = [0, 0, 0, 0]
    for cp, lo, hi in CHECKPOINTS:
        c = [r for r in R[cp] if r["req_gt_avg"]]
        n, u, o, p = cell(c)
        for i, v in enumerate((n, u, o, p)):
            tot2[i] += v
        out(f"  {cp:>4} {n:>8} {u:>7} {o:>6} {p:>5} {pct(u,n):>7.2f}%")
    n, u, o, p = tot2
    out(f"  {'ALL':>4} {n:>8} {u:>7} {o:>6} {p:>5} {pct(u,n):>7.2f}%")
    out("")

    # ── 6. intersection analysis ──
    out("=" * 96)
    out("6. INTERSECTION ANALYSIS (per checkpoint)")
    out("=" * 96)
    for cp, lo, hi in CHECKPOINTS:
        out(f"  ── {cp}% ──")
        sets = [
            ("A required>avg only",       lambda r: r["req_gt_avg"]),
            ("B actual<req only",         lambda r: r["act_lt_req"]),
            ("C actual<avg only",         lambda r: r["act_lt_avg"]),
            ("D actual<req AND actual<avg [LIVE ALERT]",
             lambda r: r["alert"]),
            ("E required>avg AND live alert",
             lambda r: r["req_gt_avg"] and r["alert"]),
        ]
        for label, f in sets:
            rs = [r for r in R[cp] if f(r)]
            n, u, o, p = cell(rs)
            out(f"    {label:<44} N={n:>6}  UNDER={u:>5}  "
                f"UNDER%={pct(u,n):>6.2f}")
    out("")

    # ── 7. league x checkpoint detail ──
    out("=" * 96)
    out("7. LEAGUE x CHECKPOINT DETAIL")
    out("=" * 96)
    out(f"  {'league':<12} {'cp':>4} {'alert_N':>8} {'UNDER':>6} {'OVER':>5} "
        f"{'PUSH':>5} {'UNDER%':>7} {'base_N':>7} {'baseU%':>7} {'lift_pp':>8}")
    for slug in COMP_ORDER:
        for cp, lo, hi in CHECKPOINTS:
            al = [r for r in R[cp] if r["league"] == slug and r["alert"]]
            na = [r for r in R[cp] if r["league"] == slug and not r["alert"]]
            n, u, o, p = cell(al)
            nn, nu, _, _ = cell(na)
            a_up, n_up = pct(u, n), pct(nu, nn)
            out(f"  {LEAGUE_SHORT[slug]:<12} {cp:>4} {n:>8} {u:>6} {o:>5} {p:>5} "
                f"{a_up:>6.2f}% {nn:>7} {n_up:>6.2f}% {a_up-n_up:>+7.2f}")
    out("")

    # ── 8. raw validation rows ──
    out("=" * 96)
    out("8. RAW VALIDATION ROWS — LIVE ALERT cohort (>=10, all checkpoints)")
    out("=" * 96)
    al = [r for cp, _, _ in CHECKPOINTS for r in R[cp] if r["alert"]]
    al.sort(key=lambda r: (r["cp"], r["gid"]))
    # spread the sample across checkpoints
    sample = []
    for cp, _, _ in CHECKPOINTS:
        sample += [r for r in al if r["cp"] == cp][:4]
    hdr = (f"  {'game_id':<10} {'league':<11} {'cp':>3} {'prog%':>6} "
           f"{'score':>5} {'line':>6} {'el':>6} {'rem':>6} {'act':>6} {'req':>6} "
           f"{'avg':>6} {'final':>5} {'out':>5}")
    out(hdr)
    for r in sample:
        out(f"  {r['gid']:<10} {LEAGUE_SHORT[r['league']]:<11} {r['cp']:>3} "
            f"{r['prog']:>6.2f} {r['score']:>5} {r['line']:>6.1f} {r['el']:>6.2f} "
            f"{r['rem']:>6.2f} {r['act']:>6.2f} {r['req']:>6.2f} "
            f"{r['avg']:>6.2f} {r['final']:>5} {r['outcome']:>5}")
    out("")

    # ── 9. formula validation ──
    out("=" * 96)
    out("9. FORMULA VALIDATION (recomputed vs stored projection fields)")
    out("=" * 96)
    bad_a = bad_r = chk = 0
    max_a = max_r = 0.0
    for cp, _, _ in CHECKPOINTS:
        for r in R[cp]:
            chk += 1
            da = abs(r["act"] - r["act_s"])
            dr = abs(r["req"] - r["req_s"])
            max_a = max(max_a, da)
            max_r = max(max_r, dr)
            if da > 0.02:
                bad_a += 1
            if dr > 0.02:
                bad_r += 1
    out(f"  rows checked: {chk}")
    out(f"  actual = round(total/elapsed,2) vs stored actual_pts_per_min:")
    out(f"    mismatches(>0.02): {bad_a}   max|diff|={max_a:.4f}")
    out(f"  required = round((line-total)/remaining,2) vs stored "
        f"required_pts_per_min:")
    out(f"    mismatches(>0.02): {bad_r}   max|diff|={max_r:.4f}")
    out("  (live api.py rounds both to 2 dp; stored clean_projections fields "
        "are 4 dp — the tiny deltas are rounding only)")
    out("")

    # ── 10. critical question ──
    out("=" * 96)
    out("10. CRITICAL ANSWER — % of live-alert games that finished UNDER")
    out("=" * 96)
    for cp, lo, hi in CHECKPOINTS:
        al = [r for r in R[cp] if r["alert"]]
        n, u, o, p = cell(al)
        out(f"  {cp}% : {u}/{n} = {pct(u,n):.2f}% UNDER   "
            f"(OVER {o}, PUSH {p})")
    n, u, o, p = tot
    out(f"  ALL CHECKPOINTS: {u}/{n} = {pct(u,n):.2f}% UNDER")
    out("")
    out("  Per league (all checkpoints combined):")
    for slug in COMP_ORDER:
        al = [r for r in allal if r["league"] == slug]
        n, u, o, p = cell(al)
        if n:
            out(f"    {LEAGUE_SHORT[slug]:<12} {u}/{n} = {pct(u,n):.2f}%")
        else:
            out(f"    {LEAGUE_SHORT[slug]:<12} 0 alerts")
    out("")
    out("  SEPARATION (alert UNDER% minus non-alert UNDER%):")
    for cp, lo, hi in CHECKPOINTS:
        al = [r for r in R[cp] if r["alert"]]
        na = [r for r in R[cp] if not r["alert"]]
        an, au, _, _ = cell(al)
        nn, nu, _, _ = cell(na)
        out(f"    {cp}%: {pct(au,an):.2f}% - {pct(nu,nn):.2f}% = "
            f"{pct(au,an)-pct(nu,nn):+.2f}pp")

    with open(OUT, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    out("")
    out(f"[report written to {OUT}]")


if __name__ == "__main__":
    main()
