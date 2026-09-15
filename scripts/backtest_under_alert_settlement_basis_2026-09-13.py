#!/usr/bin/env python3
"""READ-ONLY backtest of the 2026-09-13 BLM V4 LIVE UNDER ALERT condition,
with the SETTLEMENT-BASIS analysis.

CONDITION UNDER TEST (blm_v4/live_analytics/under_alert.py, 2026-09-13 rev —
`backtest_live_under_alert_2026-09-13.py` covers the same condition; this
script is the independent companion focused on WHERE THE OUTCOME IS SETTLED):

    active = eligible AND actual_pts_per_min < required_pts_per_min
                        AND actual_pts_per_min < league_average_pace

WHAT IS NEW HERE vs the sibling script
--------------------------------------
An alert's outcome is `final_total` vs *some* market line.  Which line you
pick changes the headline materially, so this script settles every alert
TWICE and reports both:

  BOARD  trigger_boundary : V4's OWN sealed "triggered line" — the value
        `blm_v4.live_analytics.under_outcome.trigger_market_total` returns
        (last line observed at-or-before the checkpoint boundary).  This is
        the line the dashboard displays and the line V4 settles against.
  TRADE  trigger_row      : the live line in force on the FIRST observation
        at which the condition became TRUE (the price actually available to
        a trader).  Every alert has one by construction, so this basis has
        full coverage.

Using the REAL V4 function (imported, never reimplemented) on the REAL
snapshot rows V4 itself passes it is the point: earlier hand-rolled
approximations of the boundary line are what produce the wrong answer.

DATABASES (both opened `mode=ro`; nothing is written to either):
  blm_metrics_clean.db.clean_projections — the rows PaceProjector serves
  blm_pokerbet.db   — games.competition_slug, game_results (OK-only),
                      game_quality (INVALID exclusion), snapshots (settlement)

The only file written is the report: analysis_under_alert_settlement_basis_2026-09-13.txt
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/home/ubuntu/BLM")
from blm_v4.live_analytics.under_outcome import trigger_market_total  # noqa: E402

# Overridable so the harness can be pointed at a FROZEN COPY of the databases,
# which is the only way to test determinism: the production DBs are live and a
# collector writes them continuously (blm-collector.service), so the population
# grows mid-run and raw cell counts legitimately move.  Defaults are production.
PROD = os.environ.get("BLM_PROD_DB", "/home/ubuntu/BLM/blm_pokerbet.db")
CLEAN = os.environ.get("BLM_CLEAN_DB", "/home/ubuntu/BLM/blm_metrics_clean.db")
OUT = os.environ.get(
    "BLM_BACKTEST_OUT",
    "/home/ubuntu/BLM/analysis_under_alert_settlement_basis_2026-09-13.txt")

CLEAN_DATA_EPOCH = "2026-09-05T05:40:41.782315Z"
# projection.duration_for(), confirmed by import
REGMIN = {"BETUAL_NBA": 40.0, "CYBER_2K26": 48.0}
SHORT = {"betual-nba": "NBA", "betual-kbl": "KBL", "betual-cba": "CBA",
         "betual-tbsl": "TBSL", "betual-euroleague": "EUROLEAGUE",
         "cyber-basketball-2k26-matches": "CYBER"}
ORDER = ["betual-nba", "betual-kbl", "betual-cba", "betual-tbsl",
         "betual-euroleague", "cyber-basketball-2k26-matches"]
CHECKPOINTS = (25, 50, 75)
BAND_HI = {25: 50, 50: 75, 75: None}   # V4 checkpoint band = [cp, next cp)
MIN_REMAINING = 2.5                    # api.ALERT_MIN_REMAINING_MINUTES

REPORT: list[str] = []


def out(s: str = "") -> None:
    REPORT.append(s)
    print(s)


def ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)


def fin(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def checkpoint_for(p) -> int | None:
    """Verbatim under_alert.checkpoint_for(): highest cp <= progress."""
    p = fin(p)
    if p is None:
        return None
    cp = None
    for c in CHECKPOINTS:
        if p >= c:
            cp = c
    return cp


def settle(final, line):
    f, l = fin(final), fin(line)
    if f is None or l is None:
        return None
    return "UNDER" if f < l else "OVER" if f > l else "PUSH"


def main() -> None:
    con_p, con_c = ro(PROD), ro(CLEAN)
    con_c.row_factory = sqlite3.Row
    con_p.row_factory = sqlite3.Row

    # ── league reference: competition_pace_reference(), OK-only ──────────
    agg = defaultdict(list)
    for slug, cls, ft in con_p.execute(
            "SELECT g.competition_slug, g.classification, gr.final_total "
            "FROM game_results gr JOIN games g "
            "  ON g.source_game_id = gr.source_game_id "
            "WHERE gr.final_result_status = 'OK' "
            "  AND gr.final_total IS NOT NULL AND gr.final_total > 0 "
            "  AND g.competition_slug IS NOT NULL "
            "  AND g.competition_slug <> ''"):
        rm = REGMIN.get(cls, 40.0)
        if slug and rm > 0 and ft is not None:
            agg[slug].append(float(ft) / rm)
    league = {s: {"avg": sum(v) / len(v), "n": len(v)}
              for s, v in agg.items() if v}

    # ── identity / settled final / quality ──────────────────────────────
    meta = {gid: {"slug": s, "cls": c, "first": fs}
            for gid, s, c, fs in con_p.execute(
                "SELECT source_game_id, competition_slug, classification, "
                "first_seen_at FROM games")}
    final = {gid: fin(ft) for gid, ft, st in con_p.execute(
        "SELECT source_game_id, final_total, final_result_status "
        "FROM game_results") if st == "OK"}
    invalid = {r[0] for r in con_p.execute(
        "SELECT source_game_id FROM game_quality WHERE status='INVALID'")}

    stats = Counter()
    stats["games_total"] = len(meta)
    stats["games_settled_ok"] = len(final)
    stats["games_quality_invalid"] = len(invalid)

    # ── eligible observations (V4 gates, as far as data permits) ─────────
    rows = con_c.execute("""
        SELECT p.source_game_id, p.captured_at, p.progress_pct,
               p.current_total_points, p.elapsed_game_minutes,
               p.remaining_game_minutes, p.actual_pts_per_min,
               p.required_pts_per_min, p.live_total_line,
               o.home_score, o.away_score
        FROM clean_projections p
        LEFT JOIN clean_observations o ON o.id = p.observation_id
        WHERE p.predictive_eligible = 1 AND p.market_status = 'LIVE'
          AND p.live_total_line IS NOT NULL
          AND p.actual_pts_per_min IS NOT NULL
          AND p.required_pts_per_min IS NOT NULL
          AND p.elapsed_game_minutes IS NOT NULL
          AND p.remaining_game_minutes IS NOT NULL
          AND p.progress_pct IS NOT NULL
        ORDER BY p.source_game_id, p.captured_at, p.id""").fetchall()
    stats["rows_prefilter"] = len(rows)

    # snapshot series per game — the SAME rows under_outcome settles from
    snaps = defaultdict(list)
    for r in con_p.execute(
            "SELECT source_game_id, captured_at, total_line, quarter, clock, "
            "period_label, game_status, home_score, away_score "
            "FROM snapshots ORDER BY source_game_id, captured_at, id"):
        snaps[r["source_game_id"]].append(dict(r))

    groups: dict = {}
    formula_bad = 0
    formula_max = 0.0
    for r in rows:
        gid = r["source_game_id"]
        m = meta.get(gid)
        if not m:
            stats["drop_no_game_row"] += 1
            continue
        if gid in invalid:
            stats["drop_quality_invalid"] += 1
            continue
        if not m["first"] or m["first"] < CLEAN_DATA_EPOCH:
            stats["drop_pre_epoch"] += 1
            continue
        ft = final.get(gid)
        if ft is None:
            stats["drop_no_ok_final"] += 1
            continue
        slug = m["slug"]
        if not slug or slug not in league:
            stats["drop_no_league_ref"] += 1
            continue

        act, req = fin(r["actual_pts_per_min"]), fin(r["required_pts_per_min"])
        rem, el = fin(r["remaining_game_minutes"]), fin(
            r["elapsed_game_minutes"])
        prog, line = fin(r["progress_pct"]), fin(r["live_total_line"])
        tot = fin(r["current_total_points"])
        if None in (act, req, rem, el, prog, line):
            stats["drop_non_finite"] += 1
            continue
        if el <= 0:
            stats["drop_elapsed_le0"] += 1
            continue
        if rem < MIN_REMAINING:
            stats["drop_below_min_remaining"] += 1
            continue
        cp = checkpoint_for(prog)
        if cp is None:
            stats["drop_progress_lt25"] += 1
            continue

        if tot is not None:
            da, dr = abs(tot / el - act), abs((line - tot) / rem - req)
            formula_max = max(formula_max, da, dr)
            if da > 0.011 or dr > 0.011:
                formula_bad += 1

        stats["rows_usable"] += 1
        avg = league[slug]["avg"]
        key = (gid, cp)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "gid": gid, "cp": cp, "slug": slug, "avg": avg, "final": ft,
                "A": False, "B": False, "C": False, "alert": False,
                "trade": None, "first": r}
        if g["trade"] is None:
            g["trade"] = line            # first obs in the band
        if req > avg:
            g["A"] = True
        if act < req:
            g["B"] = True
        if act < avg:
            g["C"] = True
        if act < req and act < avg:
            g["alert"] = True
            if g["first"] is None or not g.get("_hit"):
                g["_hit"] = True
                g["first_hit"] = r

    for g in groups.values():
        rowset = snaps.get(g["gid"])
        g["board"] = (trigger_market_total(rowset, g["cp"],
                                          meta[g["gid"]]["cls"])
                      if rowset else None)
        fh = g.get("first_hit")
        # the tradeable line is the first row where the condition is TRUE
        g["trade_hit"] = fin(fh["live_total_line"]) if fh else g["trade"]

    stats["pairings"] = len(groups)
    allr = list(groups.values())
    ALERT = [g for g in allr if g["alert"]]
    BASE = [g for g in allr if not g["alert"]]

    def tally(rs, key):
        c = Counter(settle(g["final"], g[key]) for g in rs)
        u, o, p = c["UNDER"], c["OVER"], c["PUSH"]
        n = u + o + p
        return {"n": n, "u": u, "o": o, "p": p, "un": c[None],
                "pct": (100 * u / n) if n else None}

    def block(title, rs):
        out(title)
        out(f'  {"cp":<6}{"N":>7}{"UNDER":>8}{"OVER":>7}{"PUSH":>6}'
            f'{"unprov":>8}{"UNDER %":>10}')
        for cp in CHECKPOINTS:
            t = tally([g for g in rs if g["cp"] == cp], "board")
            p = "   n/a" if t["pct"] is None else f'{t["pct"]:.2f}'
            out(f'  {str(cp)+"%":<6}{t["n"]:>7}{t["u"]:>8}{t["o"]:>7}'
                f'{t["p"]:>6}{t["un"]:>8}{p:>10}')
        t = tally(rs, "board")
        p = "   n/a" if t["pct"] is None else f'{t["pct"]:.2f}'
        out(f'  {"ALL":<6}{t["n"]:>7}{t["u"]:>8}{t["o"]:>7}{t["p"]:>6}'
            f'{t["un"]:>8}{p:>10}')

    out("=" * 96)
    out("BLM V4 — UNDER ALERT BACKTEST: SETTLEMENT-BASIS ANALYSIS "
        "(READ-ONLY), 2026-09-13 condition")
    out("  condition: actual_pts_per_min < required_pts_per_min "
        "AND actual_pts_per_min < league_average_pace")
    out("=" * 96)
    out(f"source: {PROD} + {CLEAN} (both mode=ro)")
    out(f"games {stats['games_total']}  settled OK {stats['games_settled_ok']}"
        f"  quality-INVALID {stats['games_quality_invalid']}")
    out("")
    out("LEAGUE REFERENCE (competition_pace_reference, OK-only):")
    for slug in ORDER:
        if slug in league:
            out(f"  {SHORT[slug]:<12} {league[slug]['avg']:.4f} pts/min "
                f"(n={league[slug]['n']})")
    out("")
    out("POPULATION FUNNEL (observations):")
    for k in ("rows_prefilter", "drop_no_game_row", "drop_quality_invalid",
              "drop_pre_epoch", "drop_no_ok_final", "drop_no_league_ref",
              "drop_non_finite", "drop_elapsed_le0",
              "drop_below_min_remaining", "drop_progress_lt25", "rows_usable"):
        out(f"  {k:<26}{stats[k]}")
    out(f"  -> (game,checkpoint) pairings  {stats['pairings']}"
        f"   ALERT {len(ALERT)}   BASELINE {len(BASE)}")
    out(f"  distinct alert games: {len({g['gid'] for g in ALERT})}")
    out("")

    out("=" * 96)
    out("PRIMARY — settle vs V4's OWN sealed trigger line "
        "(under_outcome.trigger_market_total)")
    out("=" * 96)
    block("ALERT cohort", ALERT)
    out("")
    block("BASELINE (non-alert) cohort", BASE)

    out("")
    out("=" * 96)
    out("CROSS-CHECK — settle vs the TRADEABLE line at the first TRUE "
        "observation (full coverage)")
    out("=" * 96)
    out(f'  {"cp":<6}{"N":>7}{"UNDER":>8}{"OVER":>7}{"PUSH":>6}'
        f'{"UNDER %":>10}')
    for cp in CHECKPOINTS:
        t = tally([g for g in ALERT if g["cp"] == cp], "trade_hit")
        out(f'  {str(cp)+"%":<6}{t["n"]:>7}{t["u"]:>8}{t["o"]:>7}'
            f'{t["p"]:>6}{(t["pct"] or 0):>10.2f}')
    t = tally(ALERT, "trade_hit")
    out(f'  {"ALL":<6}{t["n"]:>7}{t["u"]:>8}{t["o"]:>7}{t["p"]:>6}'
        f'{(t["pct"] or 0):>10.2f}')

    out("")
    out("=" * 96)
    out("LIFT — alert vs baseline, BOTH on V4's sealed trigger line")
    out("=" * 96)
    out(f'  {"cp":<6}{"alertN":>8}{"alert%":>9}{"baseN":>8}{"base%":>9}'
        f'{"lift pp":>10}{"lift rel":>11}')
    for cp in CHECKPOINTS:
        a = tally([g for g in ALERT if g["cp"] == cp], "board")
        b = tally([g for g in BASE if g["cp"] == cp], "board")
        if a["pct"] is None or b["pct"] is None:
            continue
        out(f'  {str(cp)+"%":<6}{a["n"]:>8}{a["pct"]:>9.2f}{b["n"]:>8}'
            f'{b["pct"]:>9.2f}{a["pct"]-b["pct"]:>+10.2f}'
            f'{(a["pct"]/b["pct"]-1)*100:>+10.1f}%')

    out("")
    out("=" * 96)
    out("SEPARATE COHORT — required_pts_per_min > league_average_pace")
    out("=" * 96)
    block("required > avg", [g for g in allr if g["A"]])

    out("")
    out("=" * 96)
    out("INTERSECTION ANALYSIS")
    out("=" * 96)
    out(f'  {"cohort":<34}{"N":>7}{"UNDER":>8}{"OVER":>7}{"UNDER %":>10}')
    for nm, rs in [("A  required > league avg", [g for g in allr if g["A"]]),
                   ("B  actual < required", [g for g in allr if g["B"]]),
                   ("C  actual < league avg", [g for g in allr if g["C"]]),
                   ("D  CURRENT ALERT (B and C)", ALERT),
                   ("E  A and D",
                    [g for g in allr if g["A"] and g["alert"]])]:
        t = tally(rs, "board")
        p = "n/a" if t["pct"] is None else f'{t["pct"]:.2f}'
        out(f'  {nm:<34}{t["n"]:>7}{t["u"]:>8}{t["o"]:>7}{p:>10}')

    out("")
    out("=" * 96)
    out("PER LEAGUE x CHECKPOINT (V4 sealed trigger line)")
    out("=" * 96)
    out(f'  {"league":<11}{"cp":<5}{"aN":>6}{"UND":>5}{"OV":>5}{"PUSH":>5}'
        f'{"a%":>8}{"bN":>6}{"b%":>8}{"liftpp":>9}')
    for slug in ORDER:
        for cp in CHECKPOINTS + ("ALL",):
            def sel(g):
                return g["slug"] == slug and (cp == "ALL" or g["cp"] == cp)
            a = tally([g for g in ALERT if sel(g)], "board")
            b = tally([g for g in BASE if sel(g)], "board")
            if a["n"] == 0 and b["n"] == 0:
                continue
            ap = "   n/a" if a["pct"] is None else f'{a["pct"]:.2f}'
            bp = "   n/a" if b["pct"] is None else f'{b["pct"]:.2f}'
            lp = ("   n/a" if a["pct"] is None or b["pct"] is None
                  else f'{a["pct"]-b["pct"]:+.2f}')
            out(f'  {SHORT[slug]:<11}{str(cp):<5}{a["n"]:>6}{a["u"]:>5}'
                f'{a["o"]:>5}{a["p"]:>5}{ap:>8}{b["n"]:>6}{bp:>8}{lp:>9}')
        out("")

    out("=" * 96)
    out("SETTLEMENT COVERAGE — alerts with NO provable trigger line")
    out("=" * 96)
    for cp in CHECKPOINTS:
        al = [g for g in ALERT if g["cp"] == cp]
        miss = sum(1 for g in al if g["board"] is None)
        out(f'  cp {cp}%: {miss}/{len(al)} alerts have no line at the '
            f'boundary ({100*miss/len(al):.1f}%)')
    out("  (V4 forward-fills the line only from observed snapshots; early-game "
        "lines are sparse.)")

    out("")
    out("=" * 96)
    out("RAW VALIDATION ROWS — alert cohort, spread across leagues")
    out("=" * 96)
    out(f'  {"game_id":<10}{"league":<11}{"cp":>3}{"prog%":>8}{"score":>8}'
        f'{"line":>7}{"el":>6}{"rem":>6}{"actual":>8}{"required":>9}'
        f'{"lg_avg":>8}{"board":>7}{"final":>6}{"res":>6}')
    seen_leagues = set()
    picks = []
    for cp in CHECKPOINTS:
        for g in sorted([x for x in ALERT if x["cp"] == cp],
                        key=lambda x: x["gid"]):
            if g["board"] is None:
                continue
            if g["slug"] in seen_leagues and len(picks) >= 8:
                continue
            seen_leagues.add(g["slug"])
            picks.append(g)
            if len(picks) >= 12:
                break
        if len(picks) >= 12:
            break
    for g in picks:
        r = g["first_hit"] if g.get("first_hit") else g["first"]
        sc = (f'{r["home_score"]}-{r["away_score"]}'
              if r["home_score"] is not None else "-")
        out(f'  {g["gid"]:<10}{SHORT[g["slug"]]:<11}{g["cp"]:>3}'
            f'{fin(r["progress_pct"]):>8.2f}{sc:>8}'
            f'{fin(r["live_total_line"]):>7}{fin(r["elapsed_game_minutes"]):>6.1f}'
            f'{fin(r["remaining_game_minutes"]):>6.1f}'
            f'{fin(r["actual_pts_per_min"]):>8.4f}'
            f'{fin(r["required_pts_per_min"]):>9.4f}{g["avg"]:>8.4f}'
            f'{g["board"]:>7}{g["final"]:>6}'
            f'{settle(g["final"], g["board"]):>6}')

    out("")
    out("=" * 96)
    out("FORMULA VALIDATION")
    out("=" * 96)
    out("  actual_pts_per_min   = current_total_points / elapsed_game_minutes")
    out("  required_pts_per_min = (live_total_line - current_total_points) "
        "/ remaining_game_minutes")
    out(f"  rows checked            : {stats['rows_usable']}")
    out(f"  rows disagreeing (>0.011): {formula_bad}")
    out(f"  max |stored-recomputed| : {formula_max:.6f}")

    out("")
    out("=" * 96)
    out("CRITICAL ANSWER — % of alert games that finished UNDER")
    out("=" * 96)
    for key, lbl in (("board", "vs V4 sealed trigger line"),
                     ("trade_hit", "vs tradeable line at first TRUE obs")):
        out(f"  -- {lbl} --")
        for cp in CHECKPOINTS:
            t = tally([g for g in ALERT if g["cp"] == cp], key)
            out(f'     {cp}%: {t["u"]}/{t["n"]} = '
                f'{(t["pct"] or 0):.2f}%   (OVER {t["o"]}, PUSH {t["p"]})')
        t = tally(ALERT, key)
        out(f'     ALL: {t["u"]}/{t["n"]} = {(t["pct"] or 0):.2f}%')
    out("")
    out("  Per league (all checkpoints, V4 sealed trigger line):")
    for slug in ORDER:
        t = tally([g for g in ALERT if g["slug"] == slug], "board")
        if t["n"]:
            out(f'     {SHORT[slug]:<12} {t["u"]}/{t["n"]} = {t["pct"]:.2f}%')

    with open(OUT, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    print(f"\n[report written to {OUT}]")


if __name__ == "__main__":
    main()
