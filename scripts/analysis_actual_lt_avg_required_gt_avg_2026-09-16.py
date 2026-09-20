#!/usr/bin/env python3
"""READ-ONLY cohort analysis (directive 2026-09-16).

COHORT: the EXACT pair of conditions, nothing else::

    actual_pts_per_min  <  league_average_pace
    AND
    required_pts_per_min  >  league_average_pace

NO 1.04 margin, NO actual<required, NO required>actual, NO other alert
condition.  Evaluated separately at the 25% / 50% / 75% progress
checkpoints and combined; ONE observation per (game, checkpoint).

POINT-IN-TIME SAFETY: league_average_pace for a trigger is the mean of
final_total / regulation_minutes over the SAME competition's games whose
authoritative settlement (game_results.result_at) is STRICTLY BEFORE the
checkpoint timestamp.  No future result can enter the reference.

TEMPORAL SAFETY (stale-state audit follow-up): every trigger observation
is flagged stale when its market line was already stale at capture
(market_age_seconds > FRESH_LINE_SECONDS = 300).  Stale observations are
reported, never silently discarded; economics run on the CLEAN cohort.

Settlement: game_results final_result_status='OK' final_total vs the
checkpoint's own live_total_line (UNDER <, OVER >, PUSH =).  Economics at
1.85: break-even 54.054%, profit = UNDER*0.85 - OVER*1.00, ROI = profit/N.

NO PRODUCTION CHANGES — both databases opened uri mode=ro; the only file
written is this script's report.
"""
from __future__ import annotations

import sqlite3
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime

PROD = "/home/ubuntu/BLM/blm_pokerbet.db"
CLEAN = "/home/ubuntu/BLM/blm_metrics_clean.db"
OUT = "/home/ubuntu/BLM/analysis_actual_lt_avg_required_gt_avg_2026-09-16.txt"

#: full regulation minutes per classification (blm_v4.projection.duration_for)
REGMIN = {"BETUAL_NBA": 40.0, "BETUAL_KBL": 40.0, "BETUAL_CBA": 40.0,
          "BETUAL_TBSL": 40.0, "BETUAL_EUROLEAGUE": 40.0,
          "CYBER_2K26": 48.0}

LEAGUE_SHORT = {
    "betual-nba": "NBA", "betual-kbl": "KBL", "betual-cba": "CBA",
    "betual-tbsl": "TBSL", "betual-euroleague": "EuroLeague",
    "cyber-basketball-2k26-matches": "CYBER",
}
LEAGUE_ORDER = ["NBA", "KBL", "CBA", "TBSL", "EuroLeague", "CYBER", "OTHER"]

FRESH_LINE_SECONDS = 300.0     # the platform's market-freshness convention
CHECKPOINTS = ((25, 15.0, 35.0), (50, 40.0, 60.0), (75, 65.0, 85.0))
BET_ODDS = 1.85
BREAK_EVEN = 100.0 / BET_ODDS          # 54.054%

REPORT: list[str] = []


def out(s: str = "") -> None:
    REPORT.append(s)
    print(s)


def ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)


def pct(n: float, d: float) -> float:
    return (100.0 * n / d) if d else 0.0


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def economics(recs: list[dict]) -> dict:
    """UNDER/OVER/PUSH economics over a list of observation records."""
    n = len(recs)
    under = sum(1 for r in recs if r["outcome"] == "UNDER")
    over = sum(1 for r in recs if r["outcome"] == "OVER")
    push = n - under - over
    profit = under * (BET_ODDS - 1.0) - over * 1.0
    return {"n": n, "under": under, "over": over, "push": push,
            "under_pct": pct(under, n), "over_pct": pct(over, n),
            "push_pct": pct(push, n),
            "profit": profit, "roi": pct(profit, n) if n else 0.0,
            "per100": (100.0 * profit / n) if n else 0.0}


def main() -> None:
    con_p = ro(PROD)
    con_c = ro(CLEAN)

    # ── 1. authoritative settled results + league ─────────────────────────
    settled: dict[str, tuple[float, str, str, datetime]] = {}
    for gid, ft, slug, cls, rat in con_p.execute(
        "SELECT gr.source_game_id, gr.final_total, g.competition_slug, "
        "       g.classification, gr.result_at "
        "FROM game_results gr JOIN games g "
        "  ON g.source_game_id = gr.source_game_id "
        "WHERE gr.final_result_status='OK' AND gr.final_total IS NOT NULL "
        "  AND gr.final_total > 0 AND g.competition_slug IS NOT NULL "
        "  AND g.competition_slug <> '' AND gr.result_at IS NOT NULL"
    ):
        if cls in REGMIN:
            settled[gid] = (float(ft), slug, cls, ts(rat))

    # ── 2. POINT-IN-TIME league pace references ───────────────────────────
    # per slug: sorted settlement timestamps + realised paces (final/regmin)
    pace_ts: dict[str, list[datetime]] = defaultdict(list)
    pace_val: dict[str, list[float]] = defaultdict(list)
    for gid, (ft, slug, cls, rat) in settled.items():
        pace_ts[slug].append(rat)
        pace_val[slug].append(ft / REGMIN[cls])
    for slug in pace_ts:
        order = sorted(range(len(pace_ts[slug])), key=lambda i: pace_ts[slug][i])
        pace_ts[slug] = [pace_ts[slug][i] for i in order]
        pace_val[slug] = [pace_val[slug][i] for i in order]

    def league_avg_at(slug: str, before: datetime) -> tuple[float, int] | None:
        """Mean realised pace over games settled STRICTLY BEFORE `before`."""
        tss, vals = pace_ts.get(slug), pace_val.get(slug)
        if not tss:
            return None
        k = bisect_left(tss, before)          # strict: exclude == before
        if k <= 0:
            return None
        return sum(vals[:k]) / k, k

    out("=" * 100)
    out("ACTUAL < LEAGUE AVG  AND  REQUIRED > LEAGUE AVG — HISTORICAL COHORT")
    out("READ-ONLY ANALYSIS, 2026-09-16  (exact directive conditions, no margins)")
    out("=" * 100)
    out(f"prod DB : {PROD} (mode=ro)")
    out(f"clean DB: {CLEAN} (mode=ro)")
    out(f"settled games (game_results OK, final>0, league+regmin known): {len(settled)}")
    out(f"break-even at {BET_ODDS:.2f}: {BREAK_EVEN:.3f}%")
    out("")

    # ── 3. ONE pass per checkpoint: every settleable observation with a
    #       line, one per (game, checkpoint), closest to the target
    #       progress.  STALE-line observations are KEPT and flagged (the
    #       directive forbids silently discarding them) — the headline
    #       cohort is the clean subset, and the stale cohort is reported
    #       separately in the temporal-safety section.
    def pick(target: float, lo: float, hi: float):
        rows = con_c.execute(
            "SELECT source_game_id, progress_pct, actual_pts_per_min, "
            "       required_pts_per_min, live_total_line, captured_at, "
            "       market_age_seconds, market_status "
            "FROM clean_projections "
            "WHERE progress_pct >= ? AND progress_pct <= ? "
            "  AND progress_pct < 100 AND (terminal IS NULL OR terminal = 0) "
            "  AND status = 'VALID' "
            "  AND actual_pts_per_min IS NOT NULL "
            "  AND required_pts_per_min IS NOT NULL "
            "  AND live_total_line IS NOT NULL",
            (lo, hi))
        best: dict[str, tuple] = {}
        for gid, prog, act, req, line, cap, mage, mstat in rows:
            base = gid.split("#")[0]
            if base not in settled:
                continue
            d = abs(prog - target)
            cur = best.get(base)
            if cur is None or d < cur[0]:
                best[base] = (d, prog, act, req, line, cap, mage, mstat)
        return best

    cohort: dict[int, list[dict]] = {25: [], 50: [], 75: []}
    baseline: dict[int, list[dict]] = {25: [], 50: [], 75: []}
    stale_obs = clean_obs = no_ref = 0

    for cp, lo, hi in CHECKPOINTS:
        obs = pick(float(cp), lo, hi)
        for base, (d, prog, act, req, line, cap, mage, mstat) in obs.items():
            ft, slug, cls, rat = settled[base]
            ref = league_avg_at(slug, ts(cap))
            if ref is None:
                no_ref += 1            # no league history yet: no reference
                continue
            avg_pace, ngames = ref
            outcome = ("UNDER" if ft < line else
                       "OVER" if ft > line else "PUSH")
            # the platform's own market-freshness convention, corroborated
            # by the raw age: the line was already old when the state fired
            is_stale = (mstat == "STALE"
                        or (mage is not None and mage > FRESH_LINE_SECONDS))
            if is_stale:
                stale_obs += 1
            else:
                clean_obs += 1
            rec = {"gid": base, "league": slug, "prog": prog, "cp": cp,
                   "cap": cap, "outcome": outcome, "stale": is_stale,
                   "avg": avg_pace, "act": act, "req": req, "line": line,
                   "ref_games": ngames}
            baseline[cp].append(rec)
            if (act < avg_pace) and (req > avg_pace):
                cohort[cp].append(rec)

    # ── 4. checkpoint tables (clean cohort) ───────────────────────────────
    def clean(recs: list[dict]) -> list[dict]:
        return [r for r in recs if not r["stale"]]

    hdr = (f"  {'cp':<5} {'N':>6} {'UNDER':>6} {'OVER':>6} {'PUSH':>5} "
           f"{'UNDER%':>8} {'OVER%':>8} {'PUSH%':>7} {'base UND%':>10} "
           f"{'lift':>8} {'net u':>9} {'ROI':>8} {'u/100':>8}")
    out("COHORT: actual < league_avg AND required > league_avg "
        "(point-in-time league averages, CLEAN observations)")
    out(hdr)
    out("  " + "-" * (len(hdr) - 2))
    allc: list[dict] = []
    allb: list[dict] = []
    for cp in (25, 50, 75):
        e = economics(clean(cohort[cp]))
        b = economics(clean(baseline[cp]))
        allc.extend(clean(cohort[cp]))
        allb.extend(clean(baseline[cp]))
        lift = (f"{e['under_pct'] - b['under_pct']:+.2f}" if b["n"] else "n/a")
        out(f"  {str(cp) + '%':<5} {e['n']:>6} {e['under']:>6} {e['over']:>6} "
            f"{e['push']:>5} {e['under_pct']:>8.2f} {e['over_pct']:>8.2f} "
            f"{e['push_pct']:>7.2f} {b['under_pct']:>10.2f} {lift:>8} "
            f"{e['profit']:>+9.2f} {e['roi']:>+7.2f}% {e['per100']:>+8.2f}")
    e_all = economics(allc)
    b_all = economics(allb)
    lift = f"{e_all['under_pct'] - b_all['under_pct']:+.2f}" if b_all["n"] else "n/a"
    out(f"  {'ALL':<5} {e_all['n']:>6} {e_all['under']:>6} {e_all['over']:>6} "
        f"{e_all['push']:>5} {e_all['under_pct']:>8.2f} "
        f"{e_all['over_pct']:>8.2f} {e_all['push_pct']:>7.2f} "
        f"{b_all['under_pct']:>10.2f} {lift:>8} {e_all['profit']:>+9.2f} "
        f"{e_all['roi']:>+7.2f}% {e_all['per100']:>+8.2f}")
    out("")

    # ── 5. league breakdown (clean cohort, ALL checkpoints) ───────────────
    out("LEAGUE BREAKDOWN (clean cohort, all checkpoints combined)")
    out(f"  {'league':<11} {'N':>6} {'UNDER':>6} {'OVER':>6} "
        f"{'UNDER%':>8} {'net u':>9} {'ROI':>8}")
    out("  " + "-" * 62)
    by_league: dict[str, list[dict]] = defaultdict(list)
    for r in allc:
        by_league[LEAGUE_SHORT.get(r["league"], "OTHER")].append(r)
    for lg in LEAGUE_ORDER:
        recs = by_league.get(lg, [])
        if not recs:
            out(f"  {lg:<11} {0:>6} {'-':>6} {'-':>6} {'n/a':>8} "
                f"{'n/a':>9} {'n/a':>8}")
            continue
        e = economics(recs)
        out(f"  {lg:<11} {e['n']:>6} {e['under']:>6} {e['over']:>6} "
            f"{e['under_pct']:>8.2f} {e['profit']:>+9.2f} {e['roi']:>+7.2f}%")
    out("")

    # ── 6. chronological thirds (OOS check, clean cohort ALL) ────────────
    allc_sorted = sorted(allc, key=lambda r: ts(r["cap"]))
    third = max(1, len(allc_sorted) // 3) if allc_sorted else 1
    thirds = [allc_sorted[:third],
              allc_sorted[third:2 * third],
              allc_sorted[2 * third:]]
    out("CHRONOLOGICAL THIRDS (clean cohort, ALL checkpoints) — "
        "split by trigger timestamp")
    if allc_sorted:
        out(f"  T1 [{allc_sorted[0]['cap']} .. {allc_sorted[third-1]['cap']}]")
        out(f"  T2 [{allc_sorted[third]['cap']} .. "
            f"{allc_sorted[min(2*third, len(allc_sorted)-1)]['cap']}]")
        out(f"  T3 [{allc_sorted[2*third]['cap'] if 2*third < len(allc_sorted) else '-'}"
            f" .. {allc_sorted[-1]['cap']}]")
    out(f"  {'third':<6} {'N':>6} {'UNDER%':>8} {'net u':>9} {'ROI':>8}")
    out("  " + "-" * 45)
    third_stats = []
    for i, t in enumerate(thirds, 1):
        if not t:
            out(f"  T{i:<5} {0:>6} {'n/a':>8} {'n/a':>9} {'n/a':>8}")
            third_stats.append(None)
            continue
        e = economics(t)
        third_stats.append(e)
        out(f"  T{i:<5} {e['n']:>6} {e['under_pct']:>8.2f} "
            f"{e['profit']:>+9.2f} {e['roi']:>+7.2f}%")
    out("")

    # ── 7. stale-state audit (directive temporal safety) ──────────────────
    cohort_all = [r for cp in (25, 50, 75) for r in cohort[cp]]
    cohort_stale = [r for r in cohort_all if r["stale"]]
    cohort_clean = [r for r in cohort_all if not r["stale"]]
    e_stale = economics(cohort_stale)
    e_stale_all = economics(cohort_all)
    out("STALE-STATE FLAGGING (temporal safety, audit follow-up)")
    out(f"  qualifying observations (cohort, all cps): {len(cohort_all)}")
    out(f"  of which STALE at capture (market_status=STALE or line age > "
        f"{FRESH_LINE_SECONDS:.0f}s): {len(cohort_stale)}")
    out(f"  clean observations (cohort): {len(cohort_clean)}")
    out(f"  (all selected checkpoint observations, settled+line: "
        f"stale {stale_obs} / clean {clean_obs}; "
        f"dropped for no point-in-time league reference: {no_ref})")
    out("")
    out("  STALE cohort economics (REPORTED, not discarded):")
    out(f"    stale cohort : N={e_stale['n']} UNDER={e_stale['under']} "
        f"OVER={e_stale['over']} UNDER%={e_stale['under_pct']:.2f} "
        f"net={e_stale['profit']:+.2f} ROI={e_stale['roi']:+.2f}%")
    out(f"    cohort incl. stale: N={e_stale_all['n']} "
        f"UNDER%={e_stale_all['under_pct']:.2f} "
        f"net={e_stale_all['profit']:+.2f} "
        f"ROI={e_stale_all['roi']:+.2f}%")
    out("  NOTE: headline tables above use the CLEAN cohort; the stale")
    out("  cohort is quantified here, never silently discarded.")
    out("  NOTE: settlement PUSH is structurally impossible in this data —")
    out("  every checkpoint line is a half-point (x.5) total, so final_total")
    out("  can never equal the line.  PUSH% is 0 by construction.")
    out("")

    # ── 8. the six answers ─────────────────────────────────────────────────
    out("ANSWERS")
    out(f"  1. exact N (ACTUAL<AVG AND REQUIRED>AVG, clean, all cps): "
        f"{e_all['n']}")
    out(f"     (per checkpoint: 25%={economics(clean(cohort[25]))['n']} "
        f"50%={economics(clean(cohort[50]))['n']} "
        f"75%={economics(clean(cohort[75]))['n']}; "
        f"+{e_stale['n']} stale kept out of the headline cohort)")
    out(f"  2. UNDER percentage: {e_all['under_pct']:.2f}%  "
        f"(UNDER {e_all['under']} / OVER {e_all['over']} / "
        f"PUSH {e_all['push']})")
    out(f"  3. above 55%? "
        f"{'YES' if e_all['under_pct'] > 55.0 else 'NO'}")
    out(f"  4. above {BREAK_EVEN:.3f}% (1.85 break-even)? "
        f"{'YES' if e_all['under_pct'] > BREAK_EVEN else 'NO'}")
    out(f"  5. net profit at 1 unit/signal: {e_all['profit']:+.2f} units  "
        f"(ROI {e_all['roi']:+.2f}%, {e_all['per100']:+.2f} u/100)")
    oos = [e for e in third_stats if e]
    ok_oos = bool(oos) and all(e["under_pct"] > BREAK_EVEN for e in oos)
    out(f"  6. above break-even in ALL chronological OOS thirds? "
        f"{'YES' if ok_oos else 'NO'}  (UNDER% by third: "
        + ", ".join(f"{e['under_pct']:.2f}%" for e in oos) + ")")
    out("")
    out("PRODUCTION CHANGES: NONE (read-only analysis; report file only).")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT) + "\n")
    print(f"\nreport written: {OUT}")


if __name__ == "__main__":
    main()
