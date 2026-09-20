#!/usr/bin/env python3
"""READ-ONLY backtest of the Q3 BREAK UNDER ALERT condition (Phase 0,
directive 2026-09-18 — IMPLEMENT_Q3_BREAK_UNDER_ALERT_2026-09-18.md §8).

CONDITION UNDER TEST (blm_v4/live_analytics/under_alert.py, 2026-09-18):

    The Q3/Q4 break sits at exactly 75.0% progress (3 of 4 regulation
    quarters).  At the FIRST observation with progress >= 75.0:

        required_pts_per_min = (triggered_line - score_at_trigger) / q_min
        active = required_pts_per_min > league_average_pace * 1.04  (STRICT)

    where triggered_line is the live O/U line in force at the boundary
    (last line observed AT OR BEFORE the boundary observation — the
    `trigger_observation` authority), score_at_trigger is the boundary
    observation's own combined score, q_min is the game's classification
    quarter length (10 BETUAL_NBA / 12 CYBER_2K26), and league_average_pace
    is the game's OWN competition reference (settled OK finals only).

    A game "reaches the boundary" when ANY eligible observation has
    progress >= 75.0; the alert cohort is games whose boundary observation
    satisfies the condition with a provable line.  Baseline: games that
    reach the boundary, are eligible, but do NOT satisfy the condition.

    Settled against the SAME frozen boundary line the live alert would
    carry (final_total vs trigger line — under / over / push).

    The production alert condition (progress >= 75 AND projector required
    pace > league_avg * 1.04) is REPORTED beside it as context only — it
    is NOT this directive's condition and is not modified by it.

METHOD NOTES
------------
The two production backtests (2026-09-13) established the conventions:
import the REAL V4 functions where possible, use the clean-metrics
projection rows the projector actually serves, gate on predictive
eligibility + LIVE market + clean-data epoch + OK-settled finals, and
settle from the SAME snapshot series under_outcome reads.

The Q3_BREAK geometry differs from the projector condition in ONE input:
required pace is computed FROM THE LINE AND SCORE at the boundary
((line - score) / q_min), not from the projector's remaining-minutes
row.  The boundary score and line are read from the PRODUCTION
authorities (`score_at_observation`, `trigger_observation`) over the
game's snapshot series — never re-derived here.

DATABASES (both opened `mode=ro`; nothing is written to either):
  blm_metrics_clean.db.clean_projections — the rows PaceProjector serves
  blm_pokerbet.db   — games.competition_slug, game_results (OK-only),
                      game_quality (INVALID exclusion), snapshots (line +
                      score at the boundary, settlement)

The only file written is the report:
  analysis_q3_break_under_alert_2026-09-18.txt
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/home/ubuntu/BLM")
from blm_v4.live_analytics.under_outcome import (  # noqa: E402
    score_at_observation,
    trigger_observation,
)

# Overridable so the harness can be pointed at FROZEN COPIES of the
# databases (determinism); defaults are production.  Read-only either way.
PROD = os.environ.get("BLM_PROD_DB", "/home/ubuntu/BLM/blm_pokerbet.db")
CLEAN = os.environ.get("BLM_CLEAN_DB", "/home/ubuntu/BLM/blm_metrics_clean.db")
OUT = os.environ.get(
    "BLM_BACKTEST_OUT",
    "/home/ubuntu/BLM/analysis_q3_break_under_alert_2026-09-18.txt")

CLEAN_DATA_EPOCH = "2026-09-05T05:40:41.782315Z"
# projection.duration_for(), confirmed by import
REGMIN = {"BETUAL_NBA": 40.0, "CYBER_2K26": 48.0}
QMIN = {"BETUAL_NBA": 10.0, "CYBER_2K26": 12.0}
ORDER = ["betual-nba", "betual-kbl", "betual-cba", "betual-tbsl",
         "betual-euroleague", "cyber-basketball-2k26-matches"]
MARGIN = 1.04           # under_alert.REQUIRED_MARGIN — the directed margin
BOUNDARY = 75.0         # under_alert.Q3_BREAK_PROGRESS — the Q3/Q4 break

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


def settle(final, line):
    f, l = fin(final), fin(line)
    if f is None or l is None:
        return None
    return "UNDER" if f < l else "OVER" if f > l else "PUSH"


def pct(n, d):
    return f"{(100.0 * n / d):.2f}%" if d else "  n/a"


def main() -> None:
    con_p, con_c = ro(PROD), ro(CLEAN)
    con_p.row_factory = sqlite3.Row
    con_c.row_factory = sqlite3.Row

    out("BLM V4 — Q3 BREAK UNDER ALERT BACKTEST (READ-ONLY), 2026-09-18")
    out("Phase 0 of IMPLEMENT_Q3_BREAK_UNDER_ALERT_2026-09-18.md §8")
    out()
    out(f"PROD DB : {PROD}")
    out(f"CLEAN DB: {CLEAN}")
    out()
    out("CONDITION (directive 2026-09-18):")
    out("  boundary  = FIRST eligible observation with progress >= 75.0")
    out("              (the Q3/Q4 break: 30/40 NBA, 36/48 CYBER)")
    out("  required  = (triggered_line - boundary_score) / q_min")
    out("  alert     = required > league_avg * 1.04   (STRICT, RELATIVE)")
    out("  line      = last live O/U line OBSERVED AT OR BEFORE the")
    out("              boundary (trigger_observation — never opening,")
    out("              later, closing or reconstructed)")
    out("  league    = game's OWN competition reference, OK finals only")
    out("  settle    = final_total vs triggered_line (under/over/push)")
    out()

    # ── league reference: competition_pace_reference() population ────────
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

    out("LEAGUE REFERENCE (competition_pace population):")
    for slug in ORDER:
        if slug in league:
            out(f"  {slug:<38} avg={league[slug]['avg']:7.4f}  "
                f"n={league[slug]['n']}")
    out()

    # ── identity / settled final / quality ───────────────────────────────
    meta = {gid: {"slug": s, "cls": c, "first": fs}
            for gid, s, c, fs in con_p.execute(
                "SELECT source_game_id, competition_slug, classification, "
                "first_seen_at FROM games")}
    final = {gid: fin(ft) for gid, ft, st in con_p.execute(
        "SELECT source_game_id, final_total, final_result_status "
        "FROM game_results") if st == "OK"}
    invalid = {r[0] for r in con_p.execute(
        "SELECT source_game_id FROM game_quality WHERE status='INVALID'")}

    # snapshot series per game — the SAME rows under_outcome reads (the
    # boundary line + score come from the production authorities over
    # THESE rows; settlement uses the same series)
    snaps = defaultdict(list)
    for r in con_p.execute(
            "SELECT source_game_id, captured_at, total_line, quarter, "
            "clock, period_label, game_status, home_score, away_score "
            "FROM snapshots ORDER BY source_game_id, captured_at, id"):
        snaps[r["source_game_id"]].append(dict(r))

    # ── eligible observations (V4 gates, as far as data permits) ─────────
    rows = con_c.execute("""
        SELECT p.source_game_id, p.captured_at, p.progress_pct,
               p.market_status, p.actual_pts_per_min,
               p.required_pts_per_min
        FROM clean_projections p
        WHERE p.predictive_eligible = 1 AND p.market_status = 'LIVE'
          AND p.progress_pct IS NOT NULL
          AND p.actual_pts_per_min IS NOT NULL
          AND p.required_pts_per_min IS NOT NULL
        ORDER BY p.source_game_id, p.captured_at, p.id""").fetchall()

    stats = Counter()
    per_cp = defaultdict(Counter)   # slug -> alert/base counters
    cp_line_missing = Counter()
    prod_ctx = Counter()            # production-condition context only

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
        cls = m["cls"]
        qm = QMIN.get(cls)
        if not qm:
            stats["drop_unknown_classification"] += 1
            continue
        stats["games_with_eligible_rows"] += 1

        prog = fin(r["progress_pct"])
        if prog is None or prog < BOUNDARY:
            continue
        # the game's FIRST eligible row at/after the boundary — the same
        # first-crossing identity the live evaluation uses
        stats["games_reaching_boundary"] += 1

        srows = snaps.get(gid) or []
        if not srows:
            stats["drop_no_snapshots"] += 1
            continue
        trig = trigger_observation(srows, 75, cls)
        line = fin(trig.get("total_line"))
        score = fin(score_at_observation(srows, 75, cls))
        if line is None or score is None:
            # the live rule fails closed here too — reported, never guessed
            cp_line_missing[slug] += 1
            stats["boundary_line_or_score_unprovable"] += 1
            continue

        req = (line - score) / qm
        avg = league[slug]["avg"]
        is_alert = req > avg * MARGIN

        # production-condition context (NOT this directive's condition)
        preq = fin(r["required_pts_per_min"])
        pact = fin(r["actual_pts_per_min"])
        if preq is not None and preq > avg * MARGIN:
            prod_ctx["prod_true_at_boundary"] += 1
            if pact is not None:
                prod_ctx["prod_rows"] += 1

        st = settle(ft, line)
        if st is None:
            stats["settle_unprovable"] += 1
            continue
        bucket = per_cp[slug]
        if is_alert:
            bucket["alert_n"] += 1
            bucket[f"alert_{st.lower()}"] += 1
        else:
            bucket["base_n"] += 1
            bucket[f"base_{st.lower()}"] += 1

    # ── the report ────────────────────────────────────────────────────────
    out("=" * 78)
    out("Q3 BREAK cohort — alert vs baseline (settled vs the FROZEN")
    out("boundary line; UNDER% = UNDER / (UNDER + OVER + PUSH))")
    out("=" * 78)
    hdr = (f"{'competition':<38} {'N':>6} {'UNDER%':>8} {'baseN':>7} "
           f"{'base%':>8} {'lift pp':>8}")
    out(hdr)
    tot = Counter()
    for slug in ORDER:
        c = per_cp.get(slug)
        if not c:
            continue
        a_n, b_n = c["alert_n"], c["base_n"]
        a_u = c["alert_under"]
        b_u = c["base_under"]
        a_pct = (100.0 * a_u / a_n) if a_n else None
        b_pct = (100.0 * b_u / b_n) if b_n else None
        lift = (a_pct - b_pct) if (a_pct is not None
                                   and b_pct is not None) else None
        out(f"{slug:<38} {a_n:>6} {pct(a_u, a_n):>8} {b_n:>7} "
            f"{pct(b_u, b_n):>8} "
            f"{('%+.2f' % lift) if lift is not None else 'n/a':>8}")
        for k in ("alert_n", "alert_under", "alert_over", "alert_push",
                  "base_n", "base_under", "base_over", "base_push"):
            tot[k] += c[k]
    out("-" * 78)
    a_n, b_n = tot["alert_n"], tot["base_n"]
    a_pct = (100.0 * tot["alert_under"] / a_n) if a_n else None
    b_pct = (100.0 * tot["base_under"] / b_n) if b_n else None
    lift = (a_pct - b_pct) if (a_pct is not None
                               and b_pct is not None) else None
    out(f"{'ALL':<38} {a_n:>6} {pct(tot['alert_under'], a_n):>8} {b_n:>7} "
        f"{pct(tot['base_under'], b_n):>8} "
        f"{('%+.2f' % lift) if lift is not None else 'n/a':>8}")
    out()
    out("PUSH counts (half-point lines make pushes arithmetically")
    out(f"unreachable; reported honestly): alert_push={tot['alert_push']} "
        f"base_push={tot['base_push']}")
    out()
    out("Coverage of the boundary:")
    out(f"  games with eligible rows           : "
        f"{stats['games_with_eligible_rows']}")
    out(f"  games reaching the boundary (>=75%): "
        f"{stats['games_reaching_boundary']}")
    out(f"  boundary line/score unprovable     : "
        f"{stats['boundary_line_or_score_unprovable']} "
        f"(the live rule fails closed on these — no line, no alert)")
    for slug in ORDER:
        if cp_line_missing.get(slug):
            out(f"    {slug:<38} {cp_line_missing[slug]}")
    out(f"  settlement unprovable (no OK final): "
        f"{stats['settle_unprovable']}")
    out()
    out("PRODUCTION-CONDITION CONTEXT (directive 2026-09-14 rule at the")
    out("boundary observation — projector required pace; NOT this")
    out(f"directive's condition): prod_true_at_boundary="
        f"{prod_ctx['prod_true_at_boundary']}")
    out()
    out("=" * 78)
    out("DIRECTIVE §8 READ-THROUGH")
    out("=" * 78)
    out("The production condition ships with the directed 1.04 margin.")
    if a_n and a_pct is not None and a_n >= 300 and a_pct < 55.0:
        out("*** FLAG: the Q3_BREAK cohort measures BELOW 55% UNDER with")
        out(f"*** N >= 300 (N={a_n}, UNDER%={a_pct:.2f}).  Per directive")
        out("*** §8 this is flagged prominently as a RECOMMENDATION to")
        out("*** re-examine the margin.  The agent must NOT change the")
        out("*** margin, the boundary, or the 75% trigger on its own")
        out("*** authority.")
    else:
        out("No §8 flag condition met (flag requires N >= 300 AND")
        out("UNDER% < 55).  Report honestly; no silent tuning either way.")
    out()
    out("READ-ONLY: no database was written.  The only file produced is")
    out(f"this report: {OUT}")

    con_p.close()
    con_c.close()

    with open(OUT, "w") as f:
        f.write("\n".join(REPORT) + "\n")


if __name__ == "__main__":
    main()
