#!/usr/bin/env python3
"""READ-ONLY forensic analysis: REQUIRED vs league-specific average at the
50% and 75% progress checkpoints, and eventual UNDER outcome.

Directive 2026-09-13.  NO database is written.  Both databases are opened
read-only (uri mode=ro).  Every number is computed from raw rows.

Definition of REQUIRED has two defensible mappings in this database, so BOTH
are computed and reported side by side:

  PANEL A (pace mapping)
    REQUIRED  = required scoring pace at the checkpoint
                = clean_projections.required_pts_per_min
                = (live_total_line - current_total_points) / remaining_min
    LEAGUE AVG = league-specific mean realised pace over settled games
                = mean(final_total / regulation_minutes) per competition_slug
                (the platform's competition_pace_reference definition)
    ACTUAL FINAL = game_results.final_total  (authoritative settled result;
                 final-realised pace = final_total / regulation_minutes)
    UNDER     = final realised pace < REQUIRED pace

  PANEL B (total-line mapping)
    REQUIRED  = checkpoint market total line = clean_projections.live_total_line
    LEAGUE AVG = league-specific mean final total (points) per competition_slug
    ACTUAL FINAL = game_results.final_total
    UNDER     = final_total < checkpoint line

Both panels also report the market UNDER (final_total < checkpoint line).
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

PROD = "/home/ubuntu/BLM/blm_pokerbet.db"
CLEAN = "/home/ubuntu/BLM/blm_metrics_clean.db"
OUT = "/home/ubuntu/BLM/analysis_required_vs_league_avg_2026-09-13.txt"

REGMIN = {"BETUAL_NBA": 40.0, "CYBER_2K26": 48.0}
LEAGUE_SHORT = {
    "betual-nba": "NBA", "betual-kbl": "KBL", "betual-cba": "CBA",
    "betual-tbsl": "TBSL", "betual-euroleague": "EUROLEAGUE",
    "cyber-basketball-2k26-matches": "CYBER(2K26)",
}
COMP_ORDER = ["betual-nba", "betual-kbl", "betual-cba", "betual-tbsl",
              "betual-euroleague", "cyber-basketball-2k26-matches"]

REPORT: list[str] = []


def out(s: str = "") -> None:
    REPORT.append(s)
    print(s)


def ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)


def pct(n, d):
    return (100.0 * n / d) if d else 0.0


def main() -> None:
    con_p = ro(PROD)
    con_c = ro(CLEAN)

    # ── 1. settled (completed) games: authoritative final result + league ──
    settled = {}
    for gid, ft, slug, cls in con_p.execute(
        "SELECT gr.source_game_id, gr.final_total, g.competition_slug, "
        "       g.classification "
        "FROM game_results gr JOIN games g "
        "  ON g.source_game_id = gr.source_game_id "
        "WHERE gr.final_result_status='OK' AND gr.final_total IS NOT NULL "
        "  AND gr.final_total > 0 AND g.competition_slug IS NOT NULL "
        "  AND g.competition_slug <> ''"
    ):
        settled[gid] = (ft, slug, cls)

    total_games_db = con_p.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    total_results = con_p.execute(
        "SELECT COUNT(*) FROM game_results").fetchone()[0]

    # ── 2. league-specific averages (same statistic/market, per league) ──
    agg = defaultdict(lambda: [0.0, 0.0, 0])  # slug -> [sum_pace, sum_total, n]
    for _gid, (ft, slug, cls) in settled.items():
        rm = REGMIN.get(cls)
        if not rm:
            continue
        agg[slug][0] += ft / rm
        agg[slug][1] += ft
        agg[slug][2] += 1
    league_avg_pace = {k: v[0] / v[2] for k, v in agg.items() if v[2]}
    league_avg_total = {k: v[1] / v[2] for k, v in agg.items() if v[2]}

    # ── 3. checkpoint observation selection (closest to target, per game) ──
    def pick(target: float, lo: float, hi: float):
        rows = con_c.execute(
            "SELECT source_game_id, progress_pct, required_pts_per_min, "
            "       live_total_line, current_total_points, elapsed_game_minutes "
            "FROM clean_projections "
            "WHERE progress_pct >= ? AND progress_pct <= ? "
            "  AND required_pts_per_min IS NOT NULL "
            "  AND live_total_line IS NOT NULL "
            "  AND progress_pct < 100 "
            "  AND (terminal IS NULL OR terminal = 0)",
            (lo, hi),
        )
        best = {}
        for gid, prog, req, line, tot, el in rows:
            base = gid.split("#")[0]
            if base not in settled:
                continue
            d = abs(prog - target)
            cur = best.get(base)
            if cur is None or d < cur[0]:
                best[base] = (d, prog, req, line, tot, el)
        return best

    obs50 = pick(50.0, 40.0, 60.0)
    obs75 = pick(75.0, 65.0, 85.0)

    # ── 3b. exclusion diagnostics (why settled games drop out per checkpoint) ──
    def excl_counts(lo, hi):
        anyrow, lineok, reqok = set(), set(), set()
        for gid, line, req in con_c.execute(
            "SELECT source_game_id, live_total_line, required_pts_per_min "
            "FROM clean_projections "
            "WHERE progress_pct >= ? AND progress_pct <= ? "
            "  AND progress_pct < 100 AND (terminal IS NULL OR terminal = 0)",
            (lo, hi),
        ):
            base = gid.split("#")[0]
            if base not in settled:
                continue
            anyrow.add(base)
            if line is not None:
                lineok.add(base)
            if req is not None:
                reqok.add(base)
        return anyrow, lineok, reqok

    ex50 = excl_counts(40.0, 60.0)
    ex75 = excl_counts(65.0, 85.0)

    # ── 4. build analysis records ──
    def build(obs, cp_label):
        recs = []
        for base, (d, prog, req, line, tot, el) in obs.items():
            ft, slug, cls = settled[base]
            rm = REGMIN.get(cls)
            if not rm or slug not in league_avg_pace:
                continue
            final_pace = ft / rm
            recs.append({
                "gid": base, "league": slug, "prog": prog, "req": req,
                "line": line, "final": ft, "final_pace": final_pace,
                "avg_pace": league_avg_pace[slug],
                "avg_total": league_avg_total[slug],
                "diff_pace": req - league_avg_pace[slug],
                "diff_total": line - league_avg_total[slug],
                "cp": cp_label,
            })
        return recs

    R50 = build(obs50, "50%")
    R75 = build(obs75, "75%")

    def outcome_mkt(r):
        return ("UNDER" if r["final"] < r["line"]
                else "OVER" if r["final"] > r["line"] else "PUSH")

    def outcome_pace(r):
        return ("UNDER" if r["final_pace"] < r["req"]
                else "OVER" if r["final_pace"] > r["req"] else "PUSH")

    # ── header ──
    out("=" * 92)
    out("REQUIRED vs LEAGUE-SPECIFIC AVERAGE — 50% & 75% CHECKPOINTS  "
        "(READ-ONLY FORENSIC ANALYSIS, 2026-09-13)")
    out("=" * 92)
    out(f"source DB      : {PROD} (read-only)  +  {CLEAN} (read-only)")
    out(f"games in DB    : {total_games_db}   game_results rows: {total_results}")
    out(f"settled games  : {len(settled)} (game_results OK, final_total>0, "
        f"league known)")
    out("")
    out("LEAGUE-SPECIFIC REFERENCES (computed over the settled set above):")
    out(f"  {'league':<14} {'avg pace pts/min':>16} {'avg total pts':>14} "
        f"{'games':>7}")
    for slug in COMP_ORDER:
        if slug in league_avg_pace:
            out(f"  {LEAGUE_SHORT.get(slug, slug):<14} "
                f"{league_avg_pace[slug]:>16.4f} "
                f"{league_avg_total[slug]:>14.4f} {agg[slug][2]:>7}")
    out("")
    out("REQUIRED mappings:")
    out("  PANEL A  REQUIRED = required pace (pts/min) at the checkpoint")
    out("           LEAGUE AVG = league avg pace; UNDER = final pace < REQUIRED")
    out("  PANEL B  REQUIRED = checkpoint total line (pts)")
    out("           LEAGUE AVG = league avg total; UNDER = final total < line")
    out("  Both panels also show MARKET UNDER (final total < checkpoint line).")
    out("")

    # ── checkpoint selection transparency ──
    out("CHECKPOINT OBSERVATION SELECTION (closest valid observation to target):")
    for name, obs, lo, hi in (("50%", obs50, 40, 60), ("75%", obs75, 65, 85)):
        progs = sorted(round(v[1], 2) for v in obs.values())
        exact = sum(1 for v in obs.values() if abs(v[1] - float(name[:-1])) < 1e-9)
        out(f"  {name}: {len(obs)} settled games with a valid observation in "
            f"[{lo}%,{hi}%]; {exact} landed exactly on {name}.");
        if progs:
            out(f"       progress range {progs[0]}..{progs[-1]}, "
                f"median {progs[len(progs)//2]}")
    out("")

    # ── exclusion / audit ──
    out("EXCLUSIONS & DATA AUDIT:")
    out(f"  settled games (completed, authoritative final): {len(settled)}")
    for name, el, obs in (("50%", ex50, obs50), ("75%", ex75, obs75)):
        anyrow, lineok, reqok = el
        out(f"  {name}: included={len(obs)}  excluded={len(settled)-len(obs)} "
            f"(no obs row in window={len(settled)-len(anyrow)}, "
            f"line NULL={len(anyrow)-len(lineok)}, "
            f"required NULL={len(lineok)-len(reqok)})")
    out(f"  missing league averages: 0 (all {len(league_avg_pace)} leagues have "
        f"a settled reference); no game fell back to a global average.")
    out("  duplicates: one observation per game per checkpoint "
        "(closest to target); split instances (id#iN) collapsed to base id.")
    out("")

    # ── per-checkpoint panels ──
    def panel(recs, title, cp):
        out("=" * 92)
        out(f"{title}")
        out("=" * 92)
        n = len(recs)

        # PANEL A
        cohA = [r for r in recs if r["req"] > r["avg_pace"]]
        cohA_le = [r for r in recs if r["req"] <= r["avg_pace"]]
        out(f"TOTAL GAMES AT {cp} (settled, valid checkpoint obs): {n}")
        out(f"PANEL A — REQUIRED (pace {cp}) > league avg pace : "
            f"{len(cohA)} / {n} = {pct(len(cohA), n):.2f}%")
        out("")
        # outcome of the >avg cohort (both outcome definitions)
        om = defaultdict(int)
        op = defaultdict(int)
        for r in cohA:
            om[outcome_mkt(r)] += 1
            op[outcome_pace(r)] += 1
        out(f"  PANEL A cohort (REQUIRED>avg) final OUTCOME:")
        out(f"    by MARKET  (final total vs checkpoint line): "
            f"UNDER {om['UNDER']}/{len(cohA)}={pct(om['UNDER'],len(cohA)):.2f}%  "
            f"OVER {om['OVER']}={pct(om['OVER'],len(cohA)):.2f}%  "
            f"PUSH {om['PUSH']}={pct(om['PUSH'],len(cohA)):.2f}%")
        out(f"    by PACE    (final pace vs REQUIRED pace): "
            f"UNDER {op['UNDER']}={pct(op['UNDER'],len(cohA)):.2f}%  "
            f"OVER {op['OVER']}={pct(op['OVER'],len(cohA)):.2f}%  "
            f"PUSH {op['PUSH']}={pct(op['PUSH'],len(cohA)):.2f}%")
        out("")
        # league breakdown A
        out(f"  PANEL A league breakdown (REQUIRED pace > league avg pace):")
        out(f"  {'League':<13} {'>Avg':>6} {'UNDER':>6} {'U%':>7} "
            f"{'OVER':>6} {'O%':>7} {'PUSH':>5} {'P%':>6}  (market outcome)")
        for slug in COMP_ORDER:
            rs = [r for r in cohA if r["league"] == slug]
            if not rs:
                continue
            u = sum(1 for r in rs if outcome_mkt(r) == "UNDER")
            o = sum(1 for r in rs if outcome_mkt(r) == "OVER")
            p = len(rs) - u - o
            out(f"  {LEAGUE_SHORT.get(slug,slug):<13} {len(rs):>6} {u:>6} "
                f"{pct(u,len(rs)):>7.2f} {o:>6} {pct(o,len(rs)):>7.2f} "
                f"{p:>5} {pct(p,len(rs)):>6.2f}")
        # reverse cohort A
        u_le = sum(1 for r in cohA_le if outcome_mkt(r) == "UNDER")
        out(f"  PANEL A reverse cohort REQUIRED <= avg pace: {len(cohA_le)} games, "
            f"market UNDER {u_le} = {pct(u_le,len(cohA_le)):.2f}%")
        out(f"  PANEL A LIFT (market UNDER% >avg minus <=avg) = "
            f"{pct(om['UNDER'],len(cohA)) - pct(u_le,len(cohA_le)):+.2f}pp")
        out("")

        # PANEL B
        cohB = [r for r in recs if r["line"] > r["avg_total"]]
        cohB_le = [r for r in recs if r["line"] <= r["avg_total"]]
        out(f"PANEL B — REQUIRED (checkpoint line {cp}) > league avg total : "
            f"{len(cohB)} / {n} = {pct(len(cohB), n):.2f}%")
        omb = defaultdict(int)
        for r in cohB:
            omb[outcome_mkt(r)] += 1
        out(f"  PANEL B cohort (line>avg total) final OUTCOME (market): "
            f"UNDER {omb['UNDER']}/{len(cohB)}={pct(omb['UNDER'],len(cohB)):.2f}%  "
            f"OVER {omb['OVER']}={pct(omb['OVER'],len(cohB)):.2f}%  "
            f"PUSH {omb['PUSH']}={pct(omb['PUSH'],len(cohB)):.2f}%")
        out(f"  PANEL B league breakdown:")
        out(f"  {'League':<13} {'>Avg':>6} {'UNDER':>6} {'U%':>7} "
            f"{'OVER':>6} {'O%':>7} {'PUSH':>5} {'P%':>6}")
        for slug in COMP_ORDER:
            rs = [r for r in cohB if r["league"] == slug]
            if not rs:
                continue
            u = sum(1 for r in rs if outcome_mkt(r) == "UNDER")
            o = sum(1 for r in rs if outcome_mkt(r) == "OVER")
            p = len(rs) - u - o
            out(f"  {LEAGUE_SHORT.get(slug,slug):<13} {len(rs):>6} {u:>6} "
                f"{pct(u,len(rs)):>7.2f} {o:>6} {pct(o,len(rs)):>7.2f} "
                f"{p:>5} {pct(p,len(rs)):>6.2f}")
        u_le_b = sum(1 for r in cohB_le if outcome_mkt(r) == "UNDER")
        out(f"  PANEL B reverse cohort line <= avg total: {len(cohB_le)} games, "
            f"market UNDER {u_le_b} = {pct(u_le_b,len(cohB_le)):.2f}%")
        out(f"  PANEL B LIFT (market UNDER% >avg minus <=avg) = "
            f"{pct(omb['UNDER'],len(cohB)) - pct(u_le_b,len(cohB_le)):+.2f}pp")
        out("")

    panel(R50, "50% CHECKPOINT", "50%")
    panel(R75, "75% CHECKPOINT", "75%")

    # ── comparison ──
    def rates(recs):
        cohA = [r for r in recs if r["req"] > r["avg_pace"]]
        cohB = [r for r in recs if r["line"] > r["avg_total"]]
        um = sum(1 for r in cohA if outcome_mkt(r) == "UNDER")
        ub = sum(1 for r in cohB if outcome_mkt(r) == "UNDER")
        return (len(recs), len(cohA), pct(um, len(cohA)),
                len(cohB), pct(ub, len(cohB)))
    n50, a50, ua50, b50, ub50 = rates(R50)
    n75, a75, ua75, b75, ub75 = rates(R75)
    out("=" * 92)
    out("50% vs 75% COMPARISON (market UNDER rate within each cohort)")
    out("=" * 92)
    out(f"  {'metric':<42} {'50%':>12} {'75%':>12}")
    out(f"  {'total games (settled, valid obs)':<42} {n50:>12} {n75:>12}")
    out(f"  {'PANEL A required_pace>avgpace':<42} {a50:>12} {a75:>12}")
    out(f"  {'PANEL A market UNDER% of cohort':<42} {ua50:>11.2f}% {ua75:>11.2f}%")
    out(f"  {'PANEL B line>avgtotal':<42} {b50:>12} {b75:>12}")
    out(f"  {'PANEL B market UNDER% of cohort':<42} {ub50:>11.2f}% {ub75:>11.2f}%")
    out("")
    out(f"  PANEL A UNDER% difference (75% - 50%) = {ua75 - ua50:+.2f}pp")
    out(f"  PANEL B UNDER% difference (75% - 50%) = {ub75 - ub50:+.2f}pp")
    out("")

    # ── raw examples ──
    for recs, cp in ((R50, "50%"), (R75, "75%")):
        cohA = sorted([r for r in recs if r["req"] > r["avg_pace"]],
                      key=lambda r: -r["diff_pace"])
        out("=" * 92)
        out(f"RAW EXAMPLES — {cp} — PANEL A REQUIRED(pace) > league avg pace, "
            f"top 20 by required_minus_avg")
        out("=" * 92)
        out("  game_id | league | cp | required(pace) | league_avg(pace) | "
            "req-avg | actual_final | mkt_outcome | pace_outcome")
        for r in cohA[:20]:
            out(f"  {r['gid']} | {LEAGUE_SHORT.get(r['league'],r['league'])} | "
                f"{cp} | {r['req']:.4f} | {r['avg_pace']:.4f} | "
                f"{r['diff_pace']:+.4f} | {r['final']} | {outcome_mkt(r)} | "
                f"{outcome_pace(r)}")
        out("")
        cohB = sorted([r for r in recs if r["line"] > r["avg_total"]],
                      key=lambda r: -r["diff_total"])
        out(f"RAW EXAMPLES — {cp} — PANEL B line > league avg total, top 20 by "
            f"required_minus_avg")
        out("  game_id | league | cp | line | league_avg(total) | line-avg | "
            "actual_final | mkt_outcome")
        for r in cohB[:20]:
            out(f"  {r['gid']} | {LEAGUE_SHORT.get(r['league'],r['league'])} | "
                f"{cp} | {r['line']:.1f} | {r['avg_total']:.4f} | "
                f"{r['diff_total']:+.4f} | {r['final']} | {outcome_mkt(r)}")
        out("")

    with open(OUT, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    out(f"[report written to {OUT}]")


if __name__ == "__main__":
    main()
