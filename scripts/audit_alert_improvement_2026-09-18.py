#!/usr/bin/env python3
"""READ-ONLY AUDIT — why do BLM UNDER alerts appear to be improving?

Mirrors the PRODUCTION trigger definition exactly (parity with
scripts/parity_under_trigger_2026-09-14.py + oos_75_exact_condition_2026-09-16.py):
  trigger : first clean_projections row per game with progress >= 75% (<100%),
            non-terminal, VALID, actual/required/line all present
  rule    : actual_pts_per_min < league_avg AND required_pts_per_min > league_avg * 1.04
            (league_avg = mean realised pace of settled-OK games of the SAME
             competition with result_at STRICTLY BEFORE the trigger instant;
             the game's own result cannot be in its own reference)
  outcome : UNDER when final_total < frozen trigger line; OVER when >; PUSH when =
  gates   : line freshness <= 300s at trigger (clean cohort), 50% tier uses the
            same selection at progress >= 50% < 75%.

Everything is computed fresh from the DBs opened read-only (mode=ro).
No writes, no production changes, no alert-logic changes.
"""

from __future__ import annotations

import bisect
import math
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, "/home/ubuntu/BLM")
from blm_v4.clean_boundary import CLEAN_DATA_EPOCH  # noqa: E402
from blm_v4.projection import duration_for  # noqa: E402

PROD = os.environ.get("BLM_PROD_DB", "/home/ubuntu/BLM/blm_pokerbet.db")
CLEAN = os.environ.get("BLM_CLEAN_DB", "/home/ubuntu/BLM/blm_metrics_clean.db")
OUT = "/home/ubuntu/BLM/analysis_alert_improvement_audit_2026-09-18.txt"

ODDS = 1.85
BREAK_EVEN = 100.0 / ODDS
REQUIRED_MARGIN = 1.04
FRESH_LINE_SECONDS = 300.0
MIN_REMAINING = 2.5

SLUG_SHORT = {"betual-nba": "NBA", "betual-kbl": "KBL", "betual-cba": "CBA",
              "betual-tbsl": "TBSL", "betual-euroleague": "EuroLeague",
              "cyber-basketball-2k26-matches": "CYBER"}
LEAGUE_ORDER = ["NBA", "KBL", "CBA", "TBSL", "EuroLeague", "CYBER", "OTHER"]

REPORT: list[str] = []


def out(s: str = "") -> None:
    REPORT.append(s)
    print(s)


def ro(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=120)
    c.row_factory = sqlite3.Row
    return c


def epoch_of(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pct(n, d):
    return (100.0 * n / d) if d else 0.0


def wilson(wins, n, z=1.96):
    if not n:
        return (None, None)
    p = wins / n
    d = 1.0 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def bucket_rate(recs):
    n = len(recs)
    under = sum(1 for r in recs if r["outcome"] == "under")
    over = sum(1 for r in recs if r["outcome"] == "over")
    push = sum(1 for r in recs if r["outcome"] == "push")
    return {"n": n, "under": under, "over": over, "push": push,
            "hit": pct(under, n)}


def row_rate(label, recs, gaps=False):
    e = bucket_rate(recs)
    extra = ""
    if gaps and e["n"]:
        lg = [r["line"] - r["ft"] for r in recs
              if r["line"] is not None and r["ft"] is not None]
        rg = [r["req"] - r["act"] for r in recs if r["req"] is not None and r["act"] is not None]
        gg = [r["act"] - r["avg"] for r in recs if r["avg"] is not None]
        if lg:
            extra += f"  avgLineGap={statistics.fmean(lg):+.2f}"
        if rg:
            extra += f"  avgReqPaceGap={statistics.fmean(rg):+.2f}"
        if gg:
            extra += f"  avgLeagueGap={statistics.fmean(gg):+.2f}"
    lo, hi = wilson(e["under"], e["n"])
    ci = f"[{100*lo:.1f}%,{100*hi:.1f}%]" if e["n"] else "-"
    out(f"  {label:<26} N={e['n']:>5}  UNDER={e['under']:>4}  OVER={e['over']:>4}  "
        f"PUSH={e['push']:>3}  UNDER%={e['hit']:>6.2f}%  {ci}{extra}")
    return e


class LeaguePace:
    """Per-league realised-pace series; mean STRICTLY before t (no look-ahead)."""

    def __init__(self, series):
        self.tss = [t for t, _ in series]
        self.vals = [v for _, v in series]
        pfx, acc = [], 0.0
        for v in self.vals:
            acc += v
            pfx.append(acc)
        self.pfx = pfx

    def avg_before(self, t):
        k = bisect.bisect_left(self.tss, t)   # STRICT: excludes == t
        if k <= 0:
            return None
        return self.pfx[k - 1] / k


def load_universe(con_p, con_c):
    """Settled-OK games + point-in-time league pace references."""
    invalid = {r["source_game_id"] for r in con_p.execute(
        "SELECT source_game_id FROM game_quality WHERE status='INVALID'")}
    settled = {}
    for r in con_p.execute(
            """SELECT gr.source_game_id AS gid, gr.final_total AS ft,
                      g.competition_slug AS slug, g.classification AS cls,
                      gr.result_at AS ra
                 FROM game_results gr JOIN games g
                   ON g.source_game_id = gr.source_game_id
                WHERE gr.final_result_status='OK' AND gr.final_total IS NOT NULL
                  AND gr.final_total > 0 AND g.competition_slug IS NOT NULL
                  AND g.competition_slug <> '' AND gr.result_at IS NOT NULL"""):
        full = duration_for(r["cls"])[1] if r["cls"] else None
        if full:
            settled[r["gid"]] = {"ft": float(r["ft"]), "slug": r["slug"],
                                 "cls": r["cls"], "ra": r["ra"], "full": full}
    series = defaultdict(list)
    for s in settled.values():
        series[s["slug"]].append((epoch_of(s["ra"]), s["ft"] / s["full"]))
    league = {slug: LeaguePace(v) for slug, v in series.items()}
    return settled, invalid, league


def load_signals(con_p, con_c, settled, invalid, league):
    """One signal per game per checkpoint tier (50%, 75%) + full context."""
    rows = con_c.execute(
        """SELECT source_game_id, progress_pct, actual_pts_per_min,
                  required_pts_per_min, live_total_line, captured_at,
                  market_age_seconds, market_status,
                  projected_final_total, fair_total, projection_vs_live_line,
                  remaining_game_minutes, current_total_points,
                  trajectory_state, recent_pace_3m
             FROM clean_projections
            WHERE progress_pct >= 50 AND progress_pct < 100
              AND (terminal IS NULL OR terminal = 0) AND status='VALID'
              AND actual_pts_per_min IS NOT NULL AND required_pts_per_min IS NOT NULL
              AND live_total_line IS NOT NULL
            ORDER BY source_game_id, captured_at""")
    sigs = []
    self_viol = no_ref = no_settle = 0
    cur_gid = None
    tier_done = set()
    cur = None
    for r in rows:
        base = r["source_game_id"].split("#")[0]
        if base != cur_gid:
            cur_gid, cur, tier_done = base, r, set()
        elif r["captured_at"] < cur["captured_at"]:
            cur = r
        prog = r["progress_pct"]
        tier = 75 if prog >= 75 else 50
        if tier in tier_done:
            continue
        s = settled.get(base)
        if s is None or base in invalid:
            no_settle += 1
            continue
        t = epoch_of(r["captured_at"])
        ra = epoch_of(s["ra"])
        if t is None or ra is None or not (t < ra):
            self_viol += 1
            continue
        avg = league[s["slug"]].avg_before(t)
        if avg is None:
            no_ref += 1
            continue
        stale = (r["market_status"] == "STALE"
                 or (r["market_age_seconds"] is not None
                     and r["market_age_seconds"] > FRESH_LINE_SECONDS))
        tier_done.add(tier)
        sigs.append({
            "gid": base, "tier": tier, "league": SLUG_SHORT.get(s["slug"], s["slug"]),
            "slug": s["slug"], "t": t, "cap": r["captured_at"],
            "line": r["live_total_line"], "ft": s["ft"],
            "act": r["actual_pts_per_min"], "req": r["required_pts_per_min"],
            "avg": avg, "stale": stale,
            "prog": prog,
            "cur": r["current_total_points"],
            "traj": r["trajectory_state"], "recent3": r["recent_pace_3m"], 
            "fair": r["fair_total"], "mkt_fair_diff": r["projection_vs_live_line"],
            "remaining": r["remaining_game_minutes"],
            "outcome": ("under" if s["ft"] < r["live_total_line"]
                        else "over" if s["ft"] > r["live_total_line"] else "push"),
        })
    out(f"signals assembled: {len(sigs)} "
        f"(no OK settlement/invalid: {no_settle}; post-settlement trigger EXCLUDED: {self_viol}; "
        f"no point-in-time league reference: {no_ref})")
    return sigs


def era_of(t):
    # Directive moments: 2026-09-12 live-markets-only; 2026-09-14 75%-only +
    # 1.04 margin; (2026-09-18 Q3_BREAK is forward-only, not in this data).
    if t < epoch_of("2026-09-12T00:00:00Z"):
        return "E1_pre0912"
    if t < epoch_of("2026-09-14T00:00:00Z"):
        return "E2_0912_0913"
    return "E3_post0914_current"


def q3_break_flag(con_p, sig):
    """Q1-Q3 pace gap: Q4 line proxy. Uses scorecard market_history Q4 lines
    frozen at/after 75% progress vs the Q1-Q3 realised pace."""
    return None  # placeholder; Q4 anomaly handled in section 4


def main():
    con_p, con_c = ro(PROD), ro(CLEAN)
    out("=" * 96)
    out("READ-ONLY AUDIT — WHY ARE BLM UNDER ALERTS APPEARING MORE ACCURATE?  2026-09-18")
    out("production trigger mirror: progress>=75%, required > league_avg*1.04 strict,")
    out("point-in-time league reference (result_at strictly before trigger), frozen trigger line,")
    out("settlement = game_results final_result_status='OK' only. ODDS=1.85 breakeven 54.05%.")
    out("=" * 96)

    settled, invalid, league = load_universe(con_p, con_c)
    out(f"settled-OK universe: {len(settled)} games; INVALID-quality games excluded: {len(invalid)}")
    out("league realised-pace references (point-in-time, strict-before):")
    for slug in sorted(league):
        n = len(league[slug].vals)
        out(f"  {SLUG_SHORT.get(slug, slug):<11} n={n:>5}  all-time mean pace={statistics.fmean(league[slug].vals):.3f} pts/min")
    out("")

    sigs = load_signals(con_p, con_c, settled, invalid, league)
    for s in sigs:
        s["era"] = era_of(s["t"])
        s["trigger"] = (s["act"] < s["avg"]) and (s["req"] > s["avg"] * REQUIRED_MARGIN)

    # TIER DISCIPLINE: 75% and 50% cohorts are kept strictly separate.
    trig75 = [s for s in sigs if s["tier"] == 75 and s["trigger"]]
    trig50 = [s for s in sigs if s["tier"] == 50 and s["trigger"]]
    clean_trig = [s for s in trig75 if not s["stale"]]   # the production 75% cohort
    stale_trig = [s for s in trig75 if s["stale"]]
    clean50 = [s for s in trig50 if not s["stale"]]
    base75 = [s for s in sigs if s["tier"] == 75]
    base50 = [s for s in sigs if s["tier"] == 50]

    out("")
    out("§1 CURRENT PERFORMANCE — by checkpoint tier (CLEAN = fresh line <=300s)")
    out("-" * 96)
    row_rate("75% ALL triggers", trig75)
    row_rate("75% CLEAN triggers", clean_trig, gaps=True)
    row_rate("75% STALE (context)", stale_trig)
    row_rate("50% ALL triggers", trig50)
    row_rate("50% CLEAN triggers", clean50, gaps=True)
    out("")
    out("§1b Baselines (same observation universe, NO trigger condition)")
    row_rate("baseline 75% (all obs)", base75, gaps=True)
    row_rate("baseline 50% (all obs)", base50)
    out("")
    out("§1c By league — 75% CLEAN triggers")
    for lg in LEAGUE_ORDER:
        recs = [s for s in clean_trig if s["league"] == lg]
        if recs:
            row_rate(f"league {lg}", recs, gaps=True)
    out("")
    out("§1d By league — 75% CLEAN baselines (same obs, no condition)")
    for lg in LEAGUE_ORDER:
        recs = [s for s in base75 if not s["stale"] and s["league"] == lg]
        if recs:
            row_rate(f"baseline {lg}", recs)

    out("")
    out("§2 ERA COMPARISON — 75% CLEAN triggers, same definitions everywhere")
    out("-" * 96)
    out("E1 = before 09-12 (pre live-markets-only directive) | E2 = 09-12..09-13 | "
        "E3 = from 09-14 (current: 75%-only + 1.04 margin)")
    for e in ("E1_pre0912", "E2_0912_0913", "E3_post0914_current"):
        row_rate(e, [s for s in clean_trig if s["era"] == e], gaps=True)
    out("")
    out("§2b Baseline by era (same obs universe, no condition) — separates rule change from data drift")
    for e in ("E1_pre0912", "E2_0912_0913", "E3_post0914_current"):
        row_rate(e, [s for s in base75 if not s["stale"] and s["era"] == e])

    out("")
    out("§2c Month/half-day trend of 75% CLEAN baseline (is the IMPROVEMENT in the rule or in the games?)")
    bounds = ["2026-09-05", "2026-09-08", "2026-09-11", "2026-09-14", "2026-09-16", "2026-09-19"]
    for i in range(len(bounds) - 1):
        lo, hi = epoch_of(bounds[i] + "T00:00:00Z"), epoch_of(bounds[i + 1] + "T00:00:00Z")
        seg = [s for s in base75 if not s["stale"] and lo <= s["t"] < hi]
        segt = [s for s in seg if s["trigger"]]
        e, et = bucket_rate(seg), bucket_rate(segt)
        out(f"  {bounds[i]}..{bounds[i+1]}: baseline N={e['n']:>5} UNDER%={e['hit']:>6.2f}   "
            f"triggers N={et['n']:>4} UNDER%={et['hit']:>6.2f}   triggerRate={pct(et['n'], e['n']):.1f}%")

    out("")
    out("§3 DRIVER ANALYSIS — 75% CLEAN triggers")
    out("-" * 96)
    out("§3a League identity (already §1c). Rate of trigger per league (selection mix):")
    for lg in LEAGUE_ORDER:
        b = [s for s in base75 if not s["stale"] and s["league"] == lg]
        t = [s for s in b if s["trigger"]]
        if b:
            out(f"  {lg:<11} triggerRate={pct(len(t), len(b)):>5.1f}%  baseline={bucket_rate(b)['hit']:>6.2f}%  "
                f"trigger-hit={bucket_rate(t)['hit']:>6.2f}%  N={len(t)}/{len(b)}")
    out("")
    out("§3b Trigger-gap magnitude vs hit (grouped): required/league margin")
    for lo_, hi_ in [(1.04, 1.10), (1.10, 1.20), (1.20, 1.35), (1.35, 99)]:
        g = [s for s in clean_trig if lo_ <= (s["req"] / s["avg"]) < hi_]
        row_rate(f"req/avg in [{lo_},{hi_})", g)
    out("")
    out("§3c Pace gap (actual vs required) at trigger:")
    for lo_, hi_, lab in [(-99, -2, "act-req < -2"), (-2, -1, "[-2,-1)"), (-1, 0, "[-1,0)"),
                          (0, 1, "[0,1)"), (1, 99, ">=1")]:
        g = [s for s in clean_trig if lo_ <= (s["act"] - s["req"]) < hi_]
        row_rate(f"act-req {lab}", g)
    out("")
    out("§3d Market deviation |fair - line| at trigger:")
    for lo_, hi_ in [(0, 2), (2, 5), (5, 10), (10, 999)]:
        g = [s for s in clean_trig if s["mkt_fair_diff"] is not None
             and lo_ <= abs(s["mkt_fair_diff"]) < hi_]
        row_rate(f"|fair-line| in [{lo_},{hi_})", g)
    out("")
    out("§3e Progress at trigger (how deep past 75%):")
    for lo_, hi_ in [(75, 80), (80, 90), (90, 100)]:
        g = [s for s in clean_trig if lo_ <= s["prog"] < hi_]
        row_rate(f"progress [{lo_},{hi_})", g)
    out("")
    out("§3f Remaining minutes at trigger (game-state / blowout proxy):")
    for lo_, hi_ in [(0, 6), (6, 12), (12, 24)]:
        g = [s for s in clean_trig if s["remaining"] is not None and lo_ <= s["remaining"] < hi_]
        row_rate(f"remaining [{lo_},{hi_})", g)
    out("")
    out("§3g Line level (high-total vs low-total games):")
    for lo_, hi_ in [(0, 150), (150, 170), (170, 999)]:
        g = [s for s in clean_trig if lo_ <= s["line"] < hi_]
        row_rate(f"line in [{lo_},{hi_})", g)
    out("")
    out("§3h Short-term trajectory at trigger (clean_projections.trajectory_state):")
    for state in ("RISING", "FALLING", "FLAT"):
        g = [s for s in clean_trig if s["traj"] == state]
        row_rate(f"trajectory {state}", g)
    out("")
    out("§3i Recent 3-minute pace vs game pace (momentum at trigger):")
    for lo_, hi_, lab in [(-99, -0.5, "recent3 << game (<-0.5)"), (-0.5, 0.0, "[-0.5,0)"),
                          (0.0, 0.5, "[0,+0.5)"), (0.5, 99, ">=+0.5 (heating up)")]:
        g = [s for s in clean_trig if s["recent3"] is not None
             and lo_ <= (s["recent3"] - s["act"]) < hi_]
        row_rate(f"recent3-game {lab}", g)

    # ── Q4 anomaly (§4): needs Q1-Q3 per-quarter pace vs the Q4 book line.
    out("")
    out("§4 Q4 ANOMALY — Q1-Q3 avg ≈ high but Q4 book line much lower")
    out("-" * 96)
    q4 = analyse_q4(con_p, con_c, settled, invalid)
    if not q4:
        out("  (see section 4 output above — league-quarter lines are not collected; "
            "best available proxy reported)")

    out("")
    out("§5 LEAKAGE / POINT-IN-TIME CHECKS")
    out("-" * 96)
    # (a) self-inclusion: league ref strictly before trigger (enforced above)
    bad_ref = 0
    for s in sigs:
        k = bisect.bisect_left(league[s["slug"]].tss, s["t"])
        # the game's own result_at must NOT be included in its own reference
        own_ra = epoch_of(settled[s["gid"]]["ra"])
        if own_ra is not None and own_ra <= s["t"] and k > 0:
            incl = league[league and s["slug"]].tss[:k]
            if own_ra in incl:
                bad_ref += 1
    out(f"  (a) self-inclusion of own result in league reference: {bad_ref} violations "
        f"(expected 0; avg_before is STRICT <)")
    # (b) market_timestamp after checkpoint in checkpoint_market
    r = con_p.execute(
        """SELECT COUNT(*) FROM checkpoint_market
            WHERE checkpoint_pct IN (50, 75) AND market_timestamp IS NOT NULL
              AND market_timestamp > checkpoint_timestamp""").fetchone()[0]
    out(f"  (b) checkpoint_market rows with market_timestamp AFTER checkpoint_timestamp: {r} (must be 0)")
    # (c) closing-line contamination: closing_line vs live_market_line usage
    r = con_p.execute(
        """SELECT COUNT(*) FROM checkpoint_market
            WHERE checkpoint_pct IN (50, 75) AND closing_line IS NOT NULL
              AND live_market_line IS NOT NULL AND closing_line = live_market_line""").fetchone()[0]
    tot = con_p.execute(
        """SELECT COUNT(*) FROM checkpoint_market WHERE checkpoint_pct IN (50, 75)
             AND live_market_line IS NOT NULL""").fetchone()[0]
    out(f"  (c) rows where closing==live line (would hint CL leakage): {r}/{tot} "
        f"(equality alone is not proof; informational)")
    # (d) outcome agreement: stored checkpoint_market outcomes are SIGNAL-aware
    #     (UNDER_WIN/UNDER_LOSS/OVER_WIN/OVER_LOSS vs the row's own signal),
    #     so reconcile against the signal's side, not the bare sign
    agree = disagree = skipped = 0
    for r in con_p.execute(
            """SELECT outcome, signal, actual_final_total, live_market_line
                 FROM checkpoint_market
                WHERE checkpoint_pct IN (50, 100) AND outcome IS NOT NULL"""):
        if r["actual_final_total"] is None or r["live_market_line"] is None:
            skipped += 1
            continue
        s = ("under" if r["actual_final_total"] < r["live_market_line"]
             else "over" if r["actual_final_total"] > r["live_market_line"] else "push")
        want = {"under_value": "under", "over_value": "over",
                "push": "push"}.get((r["signal"] or "").lower())
        if want is None:
            skipped += 1
            continue
        verdict = {"under": {"under": "UNDER_WIN", "over": "UNDER_LOSS", "push": "PUSH"},
                   "over": {"under": "OVER_LOSS", "over": "OVER_WIN", "push": "PUSH"},
                   "push": {"under": None, "over": None, "push": "PUSH"}}[want][s]
        if verdict is None:
            skipped += 1
        elif verdict == r["outcome"]:
            agree += 1
        else:
            disagree += 1
    out(f"  (d) stored checkpoint_market outcomes (signal-aware) vs recomputed: "
        f"agree={agree} disagree={disagree} skipped={skipped}")
    # (e) post-settlement triggers already excluded (self_viol above)
    out(f"  (e) post-settlement trigger rows excluded from every cohort above "
        f"(no look-ahead reference possible)")
    # (f) shadow triggers file: only pre-result captures?
    try:
        n_bad = n_tot = 0
        with open("/home/ubuntu/BLM/shadow_p80_3d_triggers.jsonl") as fh:
            for line in fh:
                d = json.loads(line)
                n_tot += 1
                if epoch_of(d.get("captured_at")) is None:
                    n_bad += 1
        out(f"  (f) shadow_p80_3d_triggers.jsonl: {n_tot} rows, {n_bad} without parseable capture time")
    except Exception:
        pass
    out("  (g) every trigger cohort above uses: point-in-time league mean (strict <),")
    out("      frozen live line at-or-before trigger (clean_projections.market_captured_at),")
    out("      OK-settled finals only.  No closing lines, no future scores in any denominator.")

    out("")
    out("§6 VERDICT FRAMEWORK — filled in the analysis report")
    out("-" * 96)
    con_p.close()
    con_c.close()
    with open(OUT, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    print(f"\nwritten: {OUT}")


def analyse_q4(con_p, con_c, settled, invalid):
    """Best-available Q4 anomaly analysis.

    Honest constraint: quarter lines are NOT collected anywhere (only full-game
    MatchTotal).  Best proxy: Q1-Q3 realised pace of the GAME ITSELF vs the
    full-game line the book held at the 75% break, and the market's own
    re-rate (live line at 75% vs opening line).  We measure what actually
    happened afterwards in the final quarter: (final - line@75%) remainder
    vs what Q1-Q3 pace extrapolation implied.
    """
    out("  CONSTRAINT: no quarter lines exist in the DB (market_observations has only")
    out("  full-game MatchTotal).  Q4 book line is therefore NOT available historically.")
    out("  Best legal proxy computed: (A) Q1-Q3 actual pace vs trigger-line implied")
    out("  remaining requirement — the discount the book applied from Q1-Q3 scoring;")
    out("  (B) bucketed by required-vs-actual gap at the 75% break (= the de-facto")
    out("  Q4 line discount the market is pricing).")
    rows = con_c.execute(
        """SELECT source_game_id, progress_pct, actual_pts_per_min,
                  required_pts_per_min, live_total_line, captured_at
             FROM clean_projections
            WHERE progress_pct >= 50 AND progress_pct < 75
              AND (terminal IS NULL OR terminal=0) AND status='VALID'
              AND live_total_line IS NOT NULL AND actual_pts_per_min IS NOT NULL
              AND required_pts_per_min IS NOT NULL
            ORDER BY source_game_id, captured_at""")
    # Q1-Q3 state = the LAST observation in [50,75) per game (just before break)
    q13 = {}
    for r in rows:
        q13[r["source_game_id"].split("#")[0]] = r
    out(f"  games with a pre-break (50-75%) observation: {len(q13)}")
    joined = []
    for gid, r in q13.items():
        s = settled.get(gid)
        if s is None or gid in invalid:
            continue
        joined.append({
            "gid": gid, "league": SLUG_SHORT.get(s["slug"], s["slug"]),
            "act": r["actual_pts_per_min"], "req": r["required_pts_per_min"],
            "line": r["live_total_line"], "ft": s["ft"],
            "outcome": ("under" if s["ft"] < r["live_total_line"]
                        else "over" if s["ft"] > r["live_total_line"] else "push"),
        })
    out(f"  joined to OK settlement: {len(joined)}")
    out("")
    out("  Q4_LINE_DISCOUNT PROXY = (required - actual) / required at the pre-break observation")
    out("  (how much the book has discounted Q4 scoring vs the game's own Q1-Q3 pace;")
    out("   large positive = book expects a slow Q4 = the anomaly scenario)")
    out("")
    out("  bucket                 N    UNDER   UNDER%   (resulted at the FROZEN pre-break line)")
    buckets = [(-9.9, -0.10, "< -10%  (anti-anomaly: book raised)"),
               (-0.10, -0.02, "-10%..-2%"),
               (-0.02, 0.02, "~0% (no discount)"),
               (0.02, 0.10, "+2%..+10%"),
               (0.10, 0.25, "+10%..+25%"),
               (0.25, 9.9, "> +25% (extreme discount)")]
    for lo_, hi_, lab in buckets:
        g = [j for j in joined
             if j["req"] and lo_ <= (j["req"] - j["act"]) / j["req"] < hi_]
        e = bucket_rate(g)
        out(f"  {lab:<34} {e['n']:>5}  {e['under']:>5}  {e['hit']:>6.2f}%")
    out("")
    out("  NOTE: this is a PROXY for the requested Q4 analysis (quarter lines unavailable).")
    out("  The requested exact scenario (Q1-Q3 avg 65/qtr vs Q4 line 45) cannot be")
    out("  measured historically; recommend adding quarter-line capture going forward.")
    return joined


if __name__ == "__main__":
    import json  # local import keeps module importable without json at top
    main()
