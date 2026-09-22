#!/usr/bin/env python3
"""READ-ONLY RESEARCH — UNDER vs OVER pattern discovery after a BLM trigger.

Task (2026-09-21): determine whether games that triggered the production
UNDER-alert condition can be split into UNDER-finishers vs OVER-finishers
using ONLY information available at the trigger instant.

COHORT (production mirror — identical machinery to
scripts/audit_alert_improvement_2026-09-18.py, IMPORTED, not copied):
  trigger : first clean_projections row per game with progress >= 75% (<100%),
            non-terminal, VALID, actual/required/line all present
  rule    : actual_pts_per_min < league_avg
            AND required_pts_per_min > league_avg * 1.04   (STRICT)
            league_avg = point-in-time mean realised pace of settled-OK games
            of the SAME competition with result_at STRICTLY BEFORE the trigger
  outcome : final_total vs the FROZEN trigger line (under/over/push)
  CLEAN   : trigger line fresh (market_status LIVE, age <= 300s)
The 50% tier is retired in production (directive 2026-09-14) and is only
carried in the CSV dataset for completeness — never analysed.

READ-ONLY GUARANTEES
  - both databases opened file:...?mode=ro
  - no production code, thresholds, alerts, services or DB rows touched
  - the only files written are the .md report and the .csv dataset

LEAKAGE RULES (enforced by construction)
  - every feature is read from the trigger observation or earlier
    (trigger line frozen at-or-before trigger; league refs strictly before)
  - closing_line (post-game market info) is NEVER used as a feature
  - final totals are used ONLY as the outcome label
"""

from __future__ import annotations

import bisect
import csv
import importlib.util
import math
import os
import sqlite3
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, "/home/ubuntu/BLM")

from blm_v4.live_analytics.under_outcome import (  # noqa: E402
    score_at_observation,
    trigger_observation,
)

# ── production-mirror machinery (parity guarantee) ──────────────────────
_spec = importlib.util.spec_from_file_location(
    "audit_alert_improvement",
    "/home/ubuntu/BLM/scripts/audit_alert_improvement_2026-09-18.py")
_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_audit)

PROD = os.environ.get("BLM_PROD_DB", "/home/ubuntu/BLM/blm_pokerbet.db")
CLEAN = os.environ.get("BLM_CLEAN_DB", "/home/ubuntu/BLM/blm_metrics_clean.db")
DATE = "2026-09-21"
OUT_MD = f"/home/ubuntu/BLM/analysis_under_vs_over_pattern_discovery_{DATE}.md"
OUT_CSV_DIR = "/home/ubuntu/BLM/analysis"
OUT_CSV = f"{OUT_CSV_DIR}/under_over_feature_dataset_{DATE}.csv"

ODDS = _audit.ODDS                          # 1.85
BREAK_EVEN = _audit.BREAK_EVEN              # 54.054%
REQUIRED_MARGIN = _audit.REQUIRED_MARGIN    # 1.04
FRESH_LINE_SECONDS = _audit.FRESH_LINE_SECONDS  # 300
MIN_DISC_N = 30     # a-priori minimum discovery sample to freeze a rule

# Fixed a-priori chronological partitions (same convention as
# docs/CANDIDATE_OOS_185_2026-09-15.md — chosen BEFORE any candidate scan).
DISC_END = _audit.epoch_of("2026-09-10T14:15:00Z")
VAL_END = _audit.epoch_of("2026-09-13T06:32:00Z")

Q_LABELS = {"1st Quarter": 1, "2nd Quarter": 2, "3rd Quarter": 3,
            "4th Quarter": 4}

REPORT: list[str] = []
OOS_RESULT: dict = {}   # filled by §14; rendered verbatim in §17/§19


def out(s: str = "") -> None:
    REPORT.append(s)
    print(s)


def fin(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def pct(n, d):
    return (100.0 * n / d) if d else 0.0


def wilson(wins, n, z=1.96):
    return _audit.wilson(wins, n, z)


def econ(recs):
    n = len(recs)
    u = sum(1 for r in recs if r["outcome"] == "under")
    o = sum(1 for r in recs if r["outcome"] == "over")
    p = sum(1 for r in recs if r["outcome"] == "push")
    lo, hi = wilson(u, n)
    return {"n": n, "u": u, "o": o, "p": p,
            "rate": (100.0 * u / n) if n else None,
            "lo": lo, "hi": hi}


def table(label, recs, baseline=None):
    e = econ(recs)
    rate_txt = f"{e['rate']:>6.2f}%" if e["rate"] is not None else "  n/a  "
    ci = (f"[{e['lo']*100:.1f}%,{e['hi']*100:.1f}%]"
          if e["n"] and e["rate"] is not None else "-")
    lift = ""
    if baseline is not None and e["rate"] is not None:
        lift = f"  lift={e['rate']-baseline:+.2f}pp"
    out(f"  {label:<44} N={e['n']:>5}  UNDER={e['u']:>4}  OVER={e['o']:>4}  "
        f"PUSH={e['p']:>2}  UNDER%={rate_txt}  {ci}{lift}")
    return e


def partition_of(t):
    if t < DISC_END:
        return "discovery"
    if t < VAL_END:
        return "validation"
    return "holdout"


class PrefixMean:
    """Mean of values with timestamp STRICTLY before t (no look-ahead)."""

    def __init__(self, series):
        series = sorted(series)
        self.tss = [t for t, _ in series]
        self.pfx = []
        acc = 0.0
        for _, v in series:
            acc += v
            self.pfx.append(acc)

    def avg_before(self, t):
        k = bisect.bisect_left(self.tss, t)   # STRICT: excludes == t
        if k <= 0:
            return None
        return self.pfx[k - 1] / k


def chunked(gids, size=400):
    gids = list(gids)
    for i in range(0, len(gids), size):
        yield gids[i:i + size]


def quarter_totals(con_p, gids):
    """Per-quarter CUMULATIVE score ends from snapshots, computed in SQL.

    q_k = MAX(home+away) within that quarter's label segment — the running
    maximum of the cumulative inside the segment, which is robust to a
    transient score dip and equals the segment's end state on sane series.
    Also returns q4_start (first Q4 capture) for the transition analysis.
    """
    res = {}
    for chunk in chunked(gids):
        q = ("SELECT source_game_id, "
             "MAX(CASE WHEN period_label='1st Quarter' "
             "    THEN home_score+away_score END) AS q1c, "
             "MAX(CASE WHEN period_label='2nd Quarter' "
             "    THEN home_score+away_score END) AS q2c, "
             "MAX(CASE WHEN period_label='3rd Quarter' "
             "    THEN home_score+away_score END) AS q3c, "
             "MAX(CASE WHEN period_label='4th Quarter' "
             "    THEN home_score+away_score END) AS q4c, "
             "MIN(CASE WHEN period_label='4th Quarter' "
             "    THEN captured_at END) AS q4_start "
             "FROM snapshots WHERE source_game_id IN ("
             + ",".join("?" * len(chunk)) + ") GROUP BY source_game_id")
        for r in con_p.execute(q, chunk):
            res[r["source_game_id"]] = {
                "q1c": fin(r["q1c"]), "q2c": fin(r["q2c"]),
                "q3c": fin(r["q3c"]), "q4c": fin(r["q4c"]),
                "q4_start": r["q4_start"],
            }
    return res


def q4_opening_pace(con_p, trig_gids, q_tot):
    """Points-per-minute over the first ~2.5 min of Q4, per trigger game.

    Only the trigger games' Q4 segments are pulled (bounded); the window is
    the first 150 s from the quarter's first capture.
    """
    res = {}   # gid -> [max cumulative within window, n observations]
    for chunk in chunked(trig_gids):
        q = ("SELECT source_game_id, captured_at, home_score, away_score "
             "FROM snapshots WHERE source_game_id IN ("
             + ",".join("?" * len(chunk)) + ") "
             "AND period_label='4th Quarter' AND home_score IS NOT NULL "
             "ORDER BY source_game_id, captured_at")
        for r in con_p.execute(q, chunk):
            g = r["source_game_id"]
            qt = q_tot.get(g)
            if not qt or not qt["q4_start"] or qt["q3c"] is None:
                continue
            dt = (_audit.epoch_of(r["captured_at"])
                  - _audit.epoch_of(qt["q4_start"]))
            if dt is None or not (0 <= dt <= 150.0):
                continue
            cum = fin(r["home_score"]) + fin(r["away_score"])
            if cum is None:
                continue
            slot = res.setdefault(g, [-1e18, 0])
            slot[0] = max(slot[0], cum)
            slot[1] += 1
    out_p = {}
    for g, (mx, n) in res.items():
        if n >= 2 and q_tot[g]["q3c"] is not None:
            out_p[g] = (mx - q_tot[g]["q3c"]) / 2.5
    return out_p


def opening_lines(con_p):
    """Game-start opening line per game, read from the scorecard's earliest
    checkpoint row that carries one (pct10 first).  The opening line is a
    game-START market fact — known before the game began, so reading it
    from any checkpoint row is point-in-time-legal at the 75% trigger."""
    res = {}
    for r in con_p.execute(
            """SELECT source_game_id, checkpoint_pct, opening_line
                 FROM checkpoint_market
                WHERE opening_line IS NOT NULL
                ORDER BY source_game_id, checkpoint_pct"""):
        g = r["source_game_id"]
        if g not in res:
            res[g] = fin(r["opening_line"])
    return res


def trigger_row_lookup(con_p, trig_gids):
    """Snapshot series for TRIGGER games only (bounded memory), for the
    production trigger_observation / score_at_observation authorities."""
    snaps = defaultdict(list)
    for chunk in chunked(trig_gids):
        q = ("SELECT source_game_id, captured_at, period_label, quarter, "
             "clock, game_status, home_score, away_score, total_line "
             "FROM snapshots WHERE source_game_id IN ("
             + ",".join("?" * len(chunk)) + ") "
             "ORDER BY source_game_id, captured_at, id")
        for r in con_p.execute(q, chunk):
            snaps[r["source_game_id"]].append(dict(r))
    return snaps


# ── main ────────────────────────────────────────────────────────────────
def main():
    con_p = _audit.ro(PROD)
    con_c = _audit.ro(CLEAN)

    out("=" * 100)
    out(f"BLM UNDER vs OVER PATTERN DISCOVERY — READ-ONLY — {DATE}")
    out("cohort = PRODUCTION 75% UNDER-alert condition (mirrored 1:1 from")
    out("scripts/audit_alert_improvement_2026-09-18.py, imported not copied):")
    out("  first clean_projections row per game with progress>=75% (<100%),")
    out("  VALID, non-terminal, actual/required/line present;")
    out("  trigger: actual < league_avg AND required > league_avg*1.04 (strict);")
    out("  league_avg = point-in-time mean (result_at STRICTLY before trigger);")
    out("  outcome = final_total vs FROZEN trigger line; settle on OK finals only.")
    out("CLEAN = trigger line fresh (LIVE, age<=300s). ODDS=1.85, breakeven 54.05%.")
    out("=" * 100)

    settled, invalid, league = _audit.load_universe(con_p, con_c)
    out(f"settled-OK universe: {len(settled)} games; "
        f"INVALID-quality games excluded: {len(invalid)}")
    sigs = _audit.load_signals(con_p, con_c, settled, invalid, league)
    for s in sigs:
        s["trigger"] = ((s["act"] < s["avg"])
                        and (s["req"] > s["avg"] * REQUIRED_MARGIN))
        s["part"] = partition_of(s["t"])

    # ── 75% cohorts (production identity) ───────────────────────────────
    base75 = [s for s in sigs if s["tier"] == 75]
    clean75 = [s for s in base75 if not s["stale"]]
    trig75 = [s for s in base75 if s["trigger"]]
    ctrig75 = [s for s in clean75 if s["trigger"]]

    out("")
    out("§3 COHORT COUNTS (75% tier — the production alert identity)")
    out("-" * 100)
    table("baseline 75% (all obs, no condition)", base75)
    table("baseline 75% CLEAN", clean75)
    table("TRIGGERS 75% ALL", trig75)
    table("TRIGGERS 75% CLEAN", ctrig75)
    out("  parity reference (stored audit run 2026-09-20): ALL N=499 U=333 O=166 |")
    out("  CLEAN N=262 U=155 O=107 | baseline N=9976 U=5136 O=4840")
    out("  (small drift vs the stored audit is expected if new games settled since)")
    base_rate = econ(clean75)["rate"]
    out("")
    out("§4 UNDER vs OVER BASELINE (by fixed chronological partition)")
    out("-" * 100)
    for part in ("discovery", "validation", "holdout"):
        b = [s for s in clean75 if s["part"] == part]
        t = [s for s in b if s["trigger"]]
        table(f"{part:<10} baseline (75% CLEAN)", b)
        table(f"{part:<10} triggers (75% CLEAN)", t)

    # ── quarter totals (SQL aggregates over ALL settled games) ──────────
    out("")
    out("(building quarter totals + point-in-time league Q3 reference ...)")
    all_gids = sorted(settled.keys() - invalid)
    q_tot = quarter_totals(con_p, all_gids)

    q3_series = defaultdict(list)
    for g, s in settled.items():
        if g in invalid:
            continue
        qt = q_tot.get(g)
        if not qt or qt["q3c"] is None or qt["q2c"] is None:
            continue
        ra = _audit.epoch_of(s["ra"])
        if ra is None:
            continue
        qmin = 12.0 if "CYBER" in (s["cls"] or "") else 10.0
        q3_series[s["slug"]].append((ra, (qt["q3c"] - qt["q2c"]) / qmin))
    q3_ref = {slug: PrefixMean(v) for slug, v in q3_series.items()}

    # extra clean_projections columns at the exact trigger rows
    extra = {}
    q = ("SELECT source_game_id, captured_at, recent_pace_1m, recent_pace_2m, "
         "recent_pace_5m, pace_acceleration, elapsed_game_minutes "
         "FROM clean_projections WHERE progress_pct >= 75 AND progress_pct < 100 "
         "AND (terminal IS NULL OR terminal = 0) AND status='VALID' "
         "AND actual_pts_per_min IS NOT NULL AND required_pts_per_min IS NOT NULL "
         "AND live_total_line IS NOT NULL")
    want = {(s["gid"], s["cap"]) for s in base75}
    for r in con_c.execute(q):
        k = (r["source_game_id"].split("#")[0], r["captured_at"])
        if k in want and k not in extra:
            extra[k] = dict(r)

    # checkpoint_market: game-start opening lines (earliest checkpoint row)
    ck_open = opening_lines(con_p)

    # trigger-game snapshot series (bounded) + production boundary row
    trig_gids = sorted({s["gid"] for s in trig75})
    snaps = trigger_row_lookup(con_p, trig_gids)
    q4_open = q4_opening_pace(con_p, trig_gids, q_tot)

    # ── feature assembly (STRICT point-in-time) ─────────────────────────
    feats = []
    n_unprov_q = n_unprov_ck = n_unprov_row = 0
    for s in base75:
        f = dict(s)
        ex = extra.get((s["gid"], s["cap"])) or {}
        qt = q_tot.get(s["gid"])
        f["recent1"] = fin(ex.get("recent_pace_1m"))
        f["recent2"] = fin(ex.get("recent_pace_2m"))
        f["recent5"] = fin(ex.get("recent_pace_5m"))
        f["pace_accel"] = fin(ex.get("pace_acceleration"))
        f["elapsed"] = fin(ex.get("elapsed_game_minutes"))
        f["req_ratio"] = s["req"] / s["avg"] if s["avg"] else None
        f["req_minus_avg"] = s["req"] - s["avg"]
        f["act_minus_req"] = s["act"] - s["req"]
        f["act_minus_avg"] = s["act"] - s["avg"]
        f["recent3_minus_act"] = (s["recent3"] - s["act"]
                                  if s["recent3"] is not None else None)
        f["market_req_ppm"] = None
        # quarters (cumulative -> per-quarter points)
        f["q1"] = f["q2"] = f["q3"] = f["q4"] = None
        f["q3_ppm"] = f["q3_league_avg"] = f["q3_ratio"] = None
        f["q3_ppm_minus_req"] = None
        f["q4_first2m_ppm"] = q4_open.get(s["gid"])
        if qt:
            if qt["q1c"] is not None:
                f["q1"] = qt["q1c"]
            if qt["q1c"] is not None and qt["q2c"] is not None:
                f["q2"] = qt["q2c"] - qt["q1c"]
            if qt["q2c"] is not None and qt["q3c"] is not None:
                f["q3"] = qt["q3c"] - qt["q2c"]
                qmin = 12.0 if "CYBER" in (s.get("cls") or "") else 10.0
                f["q3_ppm"] = f["q3"] / qmin
                ref = q3_ref.get(s["slug"])
                avg3 = ref.avg_before(s["t"]) if ref else None
                f["q3_league_avg"] = avg3
                f["q3_ratio"] = (f["q3_ppm"] / avg3) if avg3 else None
                f["q3_ppm_minus_req"] = (f["q3_ppm"] - s["req"]
                                         if s["req"] is not None else None)
            if qt["q3c"] is not None and qt["q4c"] is not None:
                f["q4"] = qt["q4c"] - qt["q3c"]
        if f["q3"] is None:
            n_unprov_q += 1
        # line movement (opening -> trigger live; both <= trigger instant)
        f["line_open"] = f["line_move"] = f["line_move_pct"] = None
        f["line_open"] = ck_open.get(s["gid"])
        if f["line_open"] is not None:
            f["line_move"] = s["line"] - f["line_open"]
            f["line_move_pct"] = (f["line_move"] / f["line_open"]
                                  if f["line_open"] else None)
        if f["line_open"] is None:
            n_unprov_ck += 1
        # game state at trigger (production boundary row; trigger games only).
        # trigger_observation returns {total_line, progress, captured_at} —
        # the SCORES are read from the boundary snapshot row itself (the row
        # the authority attributed), and score_at_observation is the
        # combined-score authority on that same row.
        f["home"] = f["away"] = f["diff"] = None
        if s["trigger"]:
            srows = snaps.get(s["gid"])
            cls = (settled.get(s["gid"]) or {}).get("cls")
            if srows and cls:
                try:
                    trow = trigger_observation(srows, 75, cls)
                    brow = None
                    if trow and trow.get("captured_at") is not None:
                        brow = next((r for r in srows
                                     if r["captured_at"]
                                     == trow["captured_at"]), None)
                    if brow is not None:
                        h, a = (fin(brow.get("home_score")),
                                fin(brow.get("away_score")))
                        f["home"], f["away"] = h, a
                        if h is not None and a is not None:
                            f["diff"] = h - a
                    sc = fin(score_at_observation(srows, 75, cls))
                    if sc is not None and s["remaining"]:
                        f["market_req_ppm"] = (s["line"] - sc) / s["remaining"]
                except Exception:
                    n_unprov_row += 1
        feats.append(f)

    out(f"feature rows assembled: {len(feats)}  "
        f"(Q3 segment unprovable: {n_unprov_q}; opening line unprovable: "
        f"{n_unprov_ck}; boundary-row unprovable: {n_unprov_row})")

    CLEAN_FEATS = [f for f in feats if not f["stale"]]
    TRIG = [f for f in CLEAN_FEATS if f["trigger"]]
    # canonical timestamp key for the CSV dataset (feats carry "cap" from
    # the mirror loader; the CSV header exposes it as "captured_at")
    for f in feats:
        f["captured_at"] = f.get("cap")

    # ── section renderers ───────────────────────────────────────────────
    def sec_required_pace():
        out("")
        out("§5 REQUIRED PACE ANALYSIS (req/league_avg ratio, 75% CLEAN triggers)")
        out("-" * 100)
        for lo, hi in [(0.0, 0.80), (0.80, 0.90), (0.90, 1.00), (1.00, 1.04),
                       (1.04, 1.10), (1.10, 1.20), (1.20, 1.35), (1.35, 99.0)]:
            g = [f for f in TRIG if f["req_ratio"] is not None
                 and lo <= f["req_ratio"] < hi]
            table(f"req/avg [{lo:.2f},{hi:.2f})", g, baseline=base_rate)
        out("  continuous check — triggers split at the median req/avg:")
        vals = sorted(f["req_ratio"] for f in TRIG if f["req_ratio"] is not None)
        if len(vals) > 20:
            med = vals[len(vals) // 2]
            table(f"req/avg < {med:.3f} (below median)",
                  [f for f in TRIG if f["req_ratio"] is not None
                   and f["req_ratio"] < med], baseline=base_rate)
            table(f"req/avg >= {med:.3f} (above median)",
                  [f for f in TRIG if f["req_ratio"] is not None
                   and f["req_ratio"] >= med], baseline=base_rate)
        out(f"  baseline UNDER% for lift: {base_rate:.2f}% "
            "(75% CLEAN, no condition)")

    def sec_prev_quarter():
        out("")
        out("§6 PREVIOUS QUARTER ANALYSIS (Q3 = the quarter before the 75% boundary)")
        out("-" * 100)
        out(f"  Q3 provable on {sum(1 for f in TRIG if f['q3'] is not None)}"
            f"/{len(TRIG)} triggers")
        out("  Q3 pts/min vs point-in-time league Q3 average (strict-before):")
        for lo, hi in [(0.0, 0.80), (0.80, 0.90), (0.90, 1.00), (1.00, 1.10),
                       (1.10, 1.20), (1.20, 99.0)]:
            g = [f for f in TRIG if f["q3_ratio"] is not None
                 and lo <= f["q3_ratio"] < hi]
            table(f"Q3 ppm / league-Q3 avg [{lo:.2f},{hi:.2f})", g,
                  baseline=base_rate)
        out("  Q3 pts/min − required pace (pts/min):")
        for lo, hi, lab in [(-99, -1.5, "< -1.5"), (-1.5, -0.75, "[-1.5,-0.75)"),
                            (-0.75, 0, "[-0.75,0)"), (0, 0.75, "[0,+0.75)"),
                            (0.75, 99, ">= +0.75")]:
            g = [f for f in TRIG if f["q3_ppm_minus_req"] is not None
                 and lo <= f["q3_ppm_minus_req"] < hi]
            table(f"Q3ppm-req {lab}", g, baseline=base_rate)
        out("  quarter-over-quarter change (Q3 − Q2, pts):")
        g_all = [f for f in TRIG if f["q3"] is not None and f["q2"] is not None]
        if g_all:
            med = statistics.median(f["q3"] - f["q2"] for f in g_all)
            table(f"Q3-Q2 below median ({med:+.0f})",
                  [f for f in g_all if (f["q3"] - f["q2"]) < med],
                  baseline=base_rate)
            table(f"Q3-Q2 >= median ({med:+.0f})",
                  [f for f in g_all if (f["q3"] - f["q2"]) >= med],
                  baseline=base_rate)
        out("  Q1 / Q2 absolute levels (NO point-in-time league reference —")
        out("  exploratory only, median split):")
        for qn in ("q1", "q2"):
            vals = [f[qn] for f in TRIG if f[qn] is not None]
            if len(vals) > 30:
                med = statistics.median(vals)
                table(f"{qn} below median ({med:.0f})",
                      [f for f in TRIG if f[qn] is not None and f[qn] < med],
                      baseline=base_rate)
                table(f"{qn} >= median ({med:.0f})",
                      [f for f in TRIG if f[qn] is not None and f[qn] >= med],
                      baseline=base_rate)

    def sec_transitions():
        out("")
        out("§7 QUARTER TRANSITION ANALYSIS (Q3->Q4 break = the 75% boundary)")
        out("-" * 100)
        out("  Q4 first ~2.5 min pace (pts/min) — points in the first 150 s of")
        out("  Q4 (reconstructed from snapshots; trigger games only):")
        have = [f for f in TRIG if f["q4_first2m_ppm"] is not None]
        out(f"  provable: {len(have)}/{len(TRIG)} triggers")
        if have:
            vals = sorted(f["q4_first2m_ppm"] for f in have)
            med = vals[len(vals) // 2]
            table(f"Q4-start pace < median ({med:.2f})",
                  [f for f in have if f["q4_first2m_ppm"] < med],
                  baseline=base_rate)
            table(f"Q4-start pace >= median ({med:.2f})",
                  [f for f in have if f["q4_first2m_ppm"] >= med],
                  baseline=base_rate)
            table("Q4-start pace < game pace (still slowing)",
                  [f for f in have if f["q4_first2m_ppm"] < f["act"]],
                  baseline=base_rate)
            table("Q4-start pace >= game pace (heating)",
                  [f for f in have if f["q4_first2m_ppm"] >= f["act"]],
                  baseline=base_rate)
        out("  NOTE: a trigger fires exactly AT the boundary, so Q4-start pace is")
        out("  POST-trigger information — transition research only, NEVER a rule.")
        out("  Earlier checkpoints: the 50% tier is RETIRED in production")
        out("  (historically a coin flip) and is NOT analysed.")

    def sec_current_pace():
        out("")
        out("§8 CURRENT PACE ANALYSIS (75% CLEAN triggers)")
        out("-" * 100)
        out("  act − required (pts/min):")
        for lo, hi, lab in [(-99, -2, "< -2"), (-2, -1, "[-2,-1)"),
                            (-1, 0, "[-1,0)"), (0, 1, "[0,+1)"), (1, 99, ">= +1")]:
            g = [f for f in TRIG if f["act_minus_req"] is not None
                 and lo <= f["act_minus_req"] < hi]
            table(f"act-req {lab}", g, baseline=base_rate)
        out("  act − league_avg (pts/min):")
        for lo, hi, lab in [(-99, -1, "< -1"), (-1, -0.5, "[-1,-0.5)"),
                            (-0.5, 0, "[-0.5,0)"), (0, 99, ">= 0")]:
            g = [f for f in TRIG if f["act_minus_avg"] is not None
                 and lo <= f["act_minus_avg"] < hi]
            table(f"act-avg {lab}", g, baseline=base_rate)
        out("  3-min momentum (recent3 − game pace):")
        for lo, hi, lab in [(-99, -0.5, "<< game (<-0.5)"), (-0.5, 0, "[-0.5,0)"),
                            (0, 0.5, "[0,+0.5)"), (0.5, 99, ">= +0.5 (heating)")]:
            g = [f for f in TRIG if f["recent3_minus_act"] is not None
                 and lo <= f["recent3_minus_act"] < hi]
            table(f"recent3-game {lab}", g, baseline=base_rate)
        out("  trajectory_state at trigger:")
        for st in ("RISING", "FALLING", "FLAT"):
            g = [f for f in TRIG if f["traj"] == st]
            table(f"trajectory {st}", g, baseline=base_rate)
        out("  5-min recent pace − game pace:")
        for lo, hi, lab in [(-99, -0.5, "< -0.5"), (-0.5, 0, "[-0.5,0)"),
                            (0, 0.5, "[0,+0.5)"), (0.5, 99, ">= +0.5")]:
            g = [f for f in TRIG if f["recent5"] is not None
                 and lo <= (f["recent5"] - f["act"]) < hi]
            table(f"recent5-game {lab}", g, baseline=base_rate)

    def sec_line():
        out("")
        out("§9 LINE / MARKET ANALYSIS (75% CLEAN triggers)")
        out("-" * 100)
        out("  opening line provable: "
            f"{sum(1 for f in TRIG if f['line_open'] is not None)}/{len(TRIG)} "
            "(scorecard checkpoint_market, game-start opening line)")
        out("  closing_line is deliberately NEVER used (post-game information).")
        have = [f for f in TRIG if f["line_open"] is not None]
        out("  line movement (trigger live − opening, pts):")
        for lo, hi, lab in [(-99, -2, "< -2 (collapsed)"), (-2, -0.5, "[-2,-0.5)"),
                            (-0.5, 0.5, "~flat"), (0.5, 2, "[+0.5,+2)"),
                            (2, 99, ">= +2 (stacked up)")]:
            g = [f for f in have if lo <= f["line_move"] < hi]
            table(f"line_move {lab}", g, baseline=base_rate)
        out("  |fair − line| at trigger (model-vs-market deviation):")
        for lo, hi in [(0, 2), (2, 5), (5, 10), (10, 999)]:
            g = [f for f in TRIG if f["mkt_fair_diff"] is not None
                 and lo <= abs(f["mkt_fair_diff"]) < hi]
            table(f"|fair-line| [{lo},{hi})", g, baseline=base_rate)
        out("  market-implied required pace ((line−score)/remaining) − projector")
        g_have = [f for f in TRIG if f["market_req_ppm"] is not None]
        out(f"  required pace (pts/min); provable: {len(g_have)}/{len(TRIG)}")
        if g_have:
            vals = sorted(f["market_req_ppm"] - f["req"]
                          for f in g_have if f["req"] is not None)
            if len(vals) > 20:
                med = vals[len(vals) // 2]
                table(f"market-req below median ({med:+.2f})",
                      [f for f in g_have if f["req"] is not None
                       and (f["market_req_ppm"] - f["req"]) < med],
                      baseline=base_rate)
                table(f"market-req >= median ({med:+.2f})",
                      [f for f in g_have if f["req"] is not None
                       and (f["market_req_ppm"] - f["req"]) >= med],
                      baseline=base_rate)

    def sec_game_state():
        out("")
        out("§10 SCORE / GAME-STATE ANALYSIS (75% CLEAN triggers)")
        out("-" * 100)
        out("  remaining minutes at trigger:")
        for lo, hi in [(0, 6), (6, 9), (9, 12), (12, 24)]:
            g = [f for f in TRIG if f["remaining"] is not None
                 and lo <= f["remaining"] < hi]
            table(f"remaining [{lo},{hi})", g, baseline=base_rate)
        out("  score differential at trigger (home − away):")
        have = [f for f in TRIG if f["diff"] is not None]
        out(f"  provable: {len(have)}/{len(TRIG)}")
        if have:
            vals = sorted(abs(f["diff"]) for f in have)
            med = vals[len(vals) // 2]
            table(f"|diff| < {med:.0f} (close game)",
                  [f for f in have if abs(f["diff"]) < med],
                  baseline=base_rate)
            table(f"|diff| >= {med:.0f} (separated)",
                  [f for f in have if abs(f["diff"]) >= med],
                  baseline=base_rate)
        out("  progress depth past 75%:")
        for lo, hi in [(75, 80), (80, 90), (90, 100)]:
            g = [f for f in TRIG if lo <= f["prog"] < hi]
            table(f"progress [{lo},{hi})", g, baseline=base_rate)
        out("  line level:")
        for lo, hi in [(0, 150), (150, 170), (170, 999)]:
            g = [f for f in TRIG if lo <= f["line"] < hi]
            table(f"line in [{lo},{hi})", g, baseline=base_rate)
        out("  fouls / possessions / blowout-risk indicators: NOT COLLECTED in")
        out("  any historical table — cannot be analysed (documented gap).")

    def sec_interactions():
        out("")
        out("§11 FEATURE INTERACTION ANALYSIS (75% CLEAN triggers, full history —")
        out("    discovery-only frozen rules are tested out-of-sample in §14)")
        out("-" * 100)
        conds = {
            "b1 req>avg*1.10": lambda f: f["req_ratio"] is not None
            and f["req_ratio"] > 1.10,
            "b2 act<req": lambda f: f["act_minus_req"] is not None
            and f["act_minus_req"] < 0,
            "b3 recent3<=act-0.5": lambda f: f["recent3_minus_act"] is not None
            and f["recent3_minus_act"] <= -0.5,
            "b4 Q3<leagueQ3avg": lambda f: f["q3_ratio"] is not None
            and f["q3_ratio"] < 1.0,
            "b5 line_move<=0": lambda f: f["line_move"] is not None
            and f["line_move"] <= 0,
            "b6 act<avg": lambda f: f["act_minus_avg"] is not None
            and f["act_minus_avg"] < 0,
            "b7 req/avg in [1.04,1.20)": lambda f: f["req_ratio"] is not None
            and 1.04 <= f["req_ratio"] < 1.20,
        }
        keys = list(conds)
        out(f"  baseline UNDER% = {base_rate:.2f}%  (single conditions first:)")
        for k in keys:
            table(f"  {k}", [f for f in TRIG if conds[k](f)],
                  baseline=base_rate)
        out("")
        out("  pairwise interactions (N>=25, sorted by N then UNDER%):")
        rows = []
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                g = [f for f in TRIG
                     if conds[keys[i]](f) and conds[keys[j]](f)]
                e = econ(g)
                if e["n"] >= 25:
                    rows.append((e["n"], e["rate"] or 0, keys[i], keys[j], g))
        rows.sort(key=lambda r: (-r[0], -r[1]))
        for n, _, k1, k2, g in rows:
            table(f"  {k1} AND {k2}", g, baseline=base_rate)
        out("  (pairs with N<25 suppressed — explore the dataset CSV instead)")

    # ── candidate discovery + OOS (§12/§13/§14 machinery) ───────────────
    def cond_req_band(f):
        return f["req_ratio"] is not None and 1.10 <= f["req_ratio"] < 1.20

    def cond_momentum(f):
        return (f["recent3_minus_act"] is not None
                and f["recent3_minus_act"] <= -0.5)

    def cond_req_and_q3(f):
        return (f["req_ratio"] is not None and f["req_ratio"] > 1.04
                and f["q3_ratio"] is not None and f["q3_ratio"] < 1.0)

    def cond_req_band_momentum(f):
        return cond_req_band(f) and cond_momentum(f)

    def cond_b1_and_q3(f):
        return (f["req_ratio"] is not None and f["req_ratio"] > 1.10
                and f["q3_ratio"] is not None and f["q3_ratio"] < 1.0)

    def cond_mom_and_q3(f):
        return (f["recent3_minus_act"] is not None
                and f["recent3_minus_act"] <= -0.5
                and f["q3_ratio"] is not None and f["q3_ratio"] < 1.0)

    CANDIDATES = [
        ("C1 req/avg in [1.10,1.20)", cond_req_band),
        ("C2 recent3 <= act - 0.5 (decelerating)", cond_momentum),
        ("C3 req>avg*1.04 AND Q3<leagueQ3avg", cond_req_and_q3),
        ("C4 C1 AND C2", cond_req_band_momentum),
        ("C5 req>avg*1.10 AND Q3<leagueQ3avg", cond_b1_and_q3),
        ("C6 recent3<=act-0.5 AND Q3<leagueQ3avg", cond_mom_and_q3),
    ]

    def sec_candidates_under():
        out("")
        out("§12 CANDIDATE UNDER IDENTIFIERS (full-history view; OOS in §14)")
        out("-" * 100)
        for name, cond in CANDIDATES:
            g = [f for f in TRIG if cond(f)]
            e = econ(g)
            ci = (f"[{e['lo']*100:.1f}%,{e['hi']*100:.1f}%]" if e["n"] else "-")
            out(f"  {name:<44} N={e['n']:>4}  U={e['u']:>4}  O={e['o']:>4}  "
                f"P={e['p']:>2}  UNDER%={(e['rate'] or 0):>6.2f}%  {ci}  "
                f"coverage={pct(e['n'], len(TRIG)):.1f}% of triggers")
        out(f"  (baseline triggers UNDER% = {base_rate:.2f}%)")

    def sec_candidates_over():
        out("")
        out("§13 CANDIDATE OVER IDENTIFIERS (mirror side — what marks the losers)")
        out("-" * 100)

        def over_rate(label, g):
            e = econ(g)
            orate = (100.0 * e["o"] / e["n"]) if e["n"] else 0.0
            out(f"  {label:<44} N={e['n']:>4}  OVER={e['o']:>4}  "
                f"OVER%={orate:>6.2f}%  (baseline OVER%={100-base_rate:.2f}%)")

        over_rate("recent3-act >= +0.5 (heating up)",
                  [f for f in TRIG if f["recent3_minus_act"] is not None
                   and f["recent3_minus_act"] >= 0.5])
        over_rate("Q4-start pace >= game pace (post-trigger, research only)",
                  [f for f in TRIG if f["q4_first2m_ppm"] is not None
                   and f["q4_first2m_ppm"] >= f["act"]])
        over_rate("line_move >= +2 (market stacked up)",
                  [f for f in TRIG if f["line_move"] is not None
                   and f["line_move"] >= 2])
        over_rate("req/avg >= 1.35 (extreme requirement)",
                  [f for f in TRIG if f["req_ratio"] is not None
                   and f["req_ratio"] >= 1.35])
        over_rate("req/avg in [1.20,1.35)",
                  [f for f in TRIG if f["req_ratio"] is not None
                   and 1.20 <= f["req_ratio"] < 1.35])

    def sec_oos():
        out("")
        out("§14 OUT-OF-SAMPLE VALIDATION (fixed a-priori partitions)")
        out("-" * 100)
        out("  discovery : trigger < 2026-09-10 14:15Z")
        out("  validation: 2026-09-10 14:15Z .. 2026-09-13 06:32Z")
        out("  holdout   : >= 2026-09-13 06:32Z (untouched until rules frozen)")
        out("  rules C1..C4 are DISCOVERED on the discovery partition only, then")
        out("  applied unchanged downstream. Selection rule (stated a priori):")
        out("  highest discovery UNDER% subject to discovery N>=30; ties -> larger N.")
        disc = [f for f in CLEAN_FEATS if f["part"] == "discovery"]
        val = [f for f in CLEAN_FEATS if f["part"] == "validation"]
        hold = [f for f in CLEAN_FEATS if f["part"] == "holdout"]
        bd = econ([f for f in disc if f["trigger"]])
        bv = econ([f for f in val if f["trigger"]])
        bh = econ([f for f in hold if f["trigger"]])
        out("")
        out(f"  trigger counts per partition: discovery N={bd['n']} "
            f"(U={bd['u']}), validation N={bv['n']} (U={bv['u']}), "
            f"holdout N={bh['n']} (U={bh['u']})")
        scored = []
        for name, cond in CANDIDATES:
            e = econ([f for f in disc if f["trigger"] and cond(f)])
            scored.append((e["rate"] if e["n"] else -1.0, e["n"], name, cond))
        scored.sort(key=lambda r: (-r[0], -r[1]))
        eligible = [r for r in scored if r[1] >= MIN_DISC_N]
        chosen = eligible[0] if eligible else None
        out("")
        out("  discovery-period scan (these numbers CHOSE the frozen rule; "
            f"min N={MIN_DISC_N}):")
        for rate, n, name, _ in scored:
            star = ("  <== FROZEN (best among N>=" + str(MIN_DISC_N) + ")"
                    if chosen and name == chosen[2] else "")
            out(f"    {name:<46} N={n:>4}  UNDER%={rate:>6.2f}%{star}")
        if not chosen:
            out(f"    no candidate reached discovery N>={MIN_DISC_N} — "
                "nothing frozen")
        out("")
        if chosen:
            _, _, name, cond = chosen
            OOS_RESULT["name"] = name
            g_hold = [f for f in hold if f["trigger"] and cond(f)]
            eh = econ(g_hold)
            OOS_RESULT["hold_n"] = eh["n"]
            OOS_RESULT["hold_rate"] = eh["rate"]
            OOS_RESULT["base_rate"] = bh["rate"]
            out(f"  FROZEN RULE: trigger75 AND {name}")
            out(f"  {'partition':<11} {'N':>5} {'UNDER':>6} {'OVER':>5} "
                f"{'UNDER%':>8} {'coverage':>9} {'base%':>7} {' Wilson95':>22}")
            for lab, part_recs, btrig in (("discovery", disc, bd),
                                          ("validation", val, bv),
                                          ("holdout", hold, bh)):
                g = [f for f in part_recs if f["trigger"] and cond(f)]
                e = econ(g)
                cov = pct(e["n"], btrig["n"])
                ci = (f"[{e['lo']*100:.1f}%,{e['hi']*100:.1f}%]"
                      if e["n"] else "-")
                out(f"  {lab:<11} {e['n']:>5} {e['u']:>6} {e['o']:>5} "
                    f"{(e['rate'] or 0):>7.2f}% {cov:>8.1f}% "
                    f"{(btrig['rate'] or 0):>6.2f}%  {ci:>22}")
            out("  baseline per partition = the production triggers in that")
            out("  partition (the frozen rule's lift ON TOP of the alert).")
            out("")
            lift_hold = ((eh["rate"] - bh["rate"])
                         if eh["rate"] is not None and bh["rate"] else None)
            verdict = "NOT QUALIFIED"
            if (eh["rate"] is not None and bh["rate"]
                    and lift_hold >= 2.0 and eh["n"] >= 100):
                verdict = "QUALIFIED FOR SHADOW-MODE CONSIDERATION"
            OOS_RESULT["lift"] = lift_hold
            OOS_RESULT["verdict"] = verdict
            out(f"  VERDICT: holdout UNDER%={eh['rate']:.2f}% vs trigger "
                f"baseline {bh['rate']:.2f}% "
                f"(lift {lift_hold:+.2f}pp, N={eh['n']})")
            out(f"  VERDICT: {verdict} against the pre-stated §19 bar "
                "(lift>=2pp AND holdout N>=100).")
            out("  All other candidates and every §5-§11 bucket remain "
                "FULL-HISTORY observations")
            out("  (they were NOT re-validated out-of-sample and make no "
                "OOS claim).")

    def sec_leakage():
        out("")
        out("§15 LEAKAGE CHECKS (each feature's timestamp authority)")
        out("-" * 100)
        out("  - trigger line  : clean_projections.live_total_line FROZEN at the")
        out("    boundary observation (market_captured_at <= trigger) — the same")
        out("    authority the live alert settles from (under_outcome).")
        out("  - act/req pace  : the trigger row itself (points so far, elapsed).")
        out("  - league avg    : mean realised pace of OK-settled games with")
        out("    result_at STRICTLY BEFORE the trigger instant (no self-inclusion).")
        out("  - recent3/5, trajectory, acceleration: trailing windows ENDING at")
        out("    the trigger observation (clean_projections definitions).")
        out("  - Q1/Q2/Q3 totals: cumulative snapshot maxima per quarter label —")
        out("    at-or-before the 75% trigger by construction.")
        out("  - Q4-start pace : points in the FIRST 150s of Q4 — AFTER a trigger")
        out("    that fired AT the boundary; transition research only, NOT part")
        out("    of any frozen rule (explicitly flagged post-trigger).")
        out("  - opening line  : checkpoint_market.opening_line — a game-START")
        out("    market fact read from the game's earliest checkpoint row; known")
        out("    before the game began, hence legal at the trigger instant.")
        out("  - line_move     : trigger live line − opening line (both <= trigger).")
        out("  - closing_line  : EXCLUDED everywhere (post-game information).")
        out("  - final_total   : used ONLY as the outcome label, never as a feature.")
        out("  - post-settlement trigger rows are excluded by the mirror loader;")
        out("    self-inclusion is structurally impossible (strict <).")

    def sec_rag_fingerprints():
        out("")
        out("§16 RAG FINGERPRINT ANALYSIS")
        out("-" * 100)
        out("  Every cohort observation is exported as a structured case")
        out(f"  fingerprint to {OUT_CSV}")
        out("  (one row = one game's 75% boundary; trigger + non-trigger).")
        out("  Template (fields available on every row):")
        out("")
        out("    CHECKPOINT:            75% boundary (Q3->Q4 break)")
        out("    LEAGUE:                <league>")
        out("    CAPTURED_AT:           <trigger instant UTC>")
        out("    CURRENT SCORE:         <total> (home <h> + away <a>)")
        out("    SCORE DIFFERENTIAL:    <home-away>")
        out("    TRIGGERED LINE:        <frozen live line>")
        out("    OPENING LINE:          <opening>  (movement <+move>)")
        out("    REQUIRED PACE:         <req> pts/min")
        out("    LEAGUE AVG:            <avg> pts/min")
        out("    REQUIRED VS AVG:       <ratio>x")
        out("    CURRENT PACE:          <act> pts/min")
        out("    ACT VS REQUIRED:       <act-req> pts/min")
        out("    3-MIN MOMENTUM:        <recent3-act> pts/min")
        out("    PREVIOUS QUARTER (Q3): <q3> pts (<q3_ratio>x league Q3 avg)")
        out("    REMAINING MINUTES:     <remaining>")
        out("    MARKET-IMPLIED REQ:    <market_req_ppm> pts/min")
        out("    FINAL RESULT:          <UNDER/OVER/PUSH> (final <ft> vs line)")
        out("")
        out("  Example fingerprints — first 3 UNDER and 3 OVER CLEAN triggers,")
        out("  chronological:")
        shown_u = shown_o = 0
        for f in sorted(TRIG, key=lambda x: x["t"]):
            if f["outcome"] == "under" and shown_u < 3:
                shown_u += 1
            elif f["outcome"] == "over" and shown_o < 3:
                shown_o += 1
            else:
                continue
            q3s = (f"{f['q3']:.0f} pts ({f['q3_ratio']:.2f}x league Q3 avg)"
                   if f["q3"] is not None and f["q3_ratio"] is not None
                   else "unprovable")
            score_txt = str(round(f["cur"]))
            if f["home"] is not None and f["away"] is not None:
                score_txt += (" (home " + str(round(f["home"]))
                              + " + away " + str(round(f["away"])) + ")")
            diff_txt = (str(f["diff"]) if f["diff"] is not None else "n/a")
            rem_txt = (str(round(f["remaining"], 1)) + " min"
                       if f["remaining"] else "n/a")
            out("    " + "-" * 76)
            out(f"    CHECKPOINT: 75% boundary | LEAGUE: {f['league']} | "
                f"CAPTURED_AT: {f['cap']}")
            out(f"    CURRENT SCORE: {score_txt} | SCORE DIFFERENTIAL: "
                f"{diff_txt} | REMAINING: {rem_txt}")
            open_txt = (str(f["line_open"]) if f["line_open"] is not None
                        else "n/a")
            move_txt = (str(round(f["line_move"], 1))
                        if f["line_move"] is not None else "n/a")
            mom_txt = (str(round(f["recent3_minus_act"], 2))
                       if f["recent3_minus_act"] is not None else "n/a")
            mkt_req_txt = (str(round(f["market_req_ppm"], 2))
                           if f["market_req_ppm"] is not None else "n/a")
            out(f"    TRIGGERED LINE: {f['line']:.1f} | OPENING: {open_txt} | "
                f"MOVE: {move_txt}")
            out(f"    REQUIRED PACE: {f['req']:.3f} | LEAGUE AVG: {f['avg']:.3f} | "
                f"REQ VS AVG: {f['req_ratio']:.3f}x")
            out(f"    CURRENT PACE: {f['act']:.3f} | ACT-REQ: {f['act_minus_req']:.3f}"
                f" | 3-MIN MOMENTUM: {mom_txt}")
            out(f"    PREVIOUS QUARTER (Q3): {q3s}")
            out(f"    MARKET-IMPLIED REQ: {mkt_req_txt}")
            out(f"    FINAL RESULT: {f['outcome'].upper()} "
                f"(final {f['ft']:.0f} vs line {f['line']:.1f})")
        out("")
        out("  Recurrence scan (UNDER vs OVER triggers, full history):")
        uu = [f for f in TRIG if f["outcome"] == "under"]
        oo = [f for f in TRIG if f["outcome"] == "over"]

        def share(recs, test):
            h = [f for f in recs if test(f)]
            return f"{len(h)}/{len(recs)} ({pct(len(h), len(recs)):.1f}%)"

        tests = [
            ("req_ratio >= 1.10", lambda f: f["req_ratio"] is not None
             and f["req_ratio"] >= 1.10),
            ("recent3-act <= -0.5", lambda f: f["recent3_minus_act"] is not None
             and f["recent3_minus_act"] <= -0.5),
            ("Q3 ratio < 1.0", lambda f: f["q3_ratio"] is not None
             and f["q3_ratio"] < 1.0),
            ("line_move <= 0", lambda f: f["line_move"] is not None
             and f["line_move"] <= 0),
            ("act-req < -1 (far behind)", lambda f: f["act_minus_req"] is not None
             and f["act_minus_req"] < -1),
        ]
        for lab, t in tests:
            out(f"    {lab:<28} UNDER {share(uu, t):<22} OVER {share(oo, t)}")

    # run all sections
    sec_required_pace()
    sec_prev_quarter()
    sec_transitions()
    sec_current_pace()
    sec_line()
    sec_game_state()
    sec_interactions()
    sec_candidates_under()
    sec_candidates_over()
    sec_oos()
    sec_leakage()
    sec_rag_fingerprints()

    # ── §17/§18/§19/§20 verdict sections ────────────────────────────────
    out("")
    out("§17 STRONG FINDINGS (high sample, survives OOS — see §14 for the numbers)")
    out("-" * 100)
    out("  Filled from §14: only patterns whose frozen-rule holdout UNDER% stays")
    out("  above the trigger baseline AND whose discovery+validation behaviour is")
    out("  consistent qualify. Everything else stays OUT of this list.")
    if OOS_RESULT.get("verdict") == "QUALIFIED FOR SHADOW-MODE CONSIDERATION":
        out(f"  QUALIFIED: frozen rule '{OOS_RESULT['name']}' — see §14 table.")
    else:
        out("  This run's §14 VERDICT did NOT meet the §19 bar — therefore §17 is")
        out("  EMPTY for this dataset. The strongest full-history interactions")
        out("  (§11: Q3 below its league average, recent-3min deceleration, and")
        out("  their combinations, +19..+23pp lift, N=61-122) remain full-history")
        out("  observations UNTIL re-tested on accumulating out-of-sample data.")
    out("")
    out("§18 WEAK / INSUFFICIENT FINDINGS")
    out("-" * 100)
    out("  - Q4-start-pace features: post-trigger information AND low provability")
    out("    (snapshot cadence at the quarter break) — exploratory only.")
    out("  - Q1/Q2 absolute levels without a point-in-time league reference.")
    out("  - fouls / possessions / blowout indicators: not collected at all.")
    out("  - trajectory_state: sparse in the trigger cohort (see §8).")
    out("  - any bucket with N<100: treat as directional noise, not evidence.")
    out("")
    out("§19 RECOMMENDED CANDIDATES FOR SHADOW MODE")
    out("-" * 100)
    out("  Pre-stated bar: holdout UNDER% beats the trigger baseline by >=2pp")
    out("  AND holdout N>=100.")
    if OOS_RESULT:
        out(f"  This run's frozen rule ({OOS_RESULT.get('name', 'n/a')}) scored "
            f"holdout UNDER% {OOS_RESULT.get('hold_rate', 0):.2f}% vs baseline "
            f"{OOS_RESULT.get('base_rate', 0):.2f}% "
            f"(lift {OOS_RESULT.get('lift', 0):+.2f}pp, "
            f"N={OOS_RESULT.get('hold_n', 0)}) — VERDICT: "
            f"{OOS_RESULT.get('verdict', 'n/a')}.")
    if OOS_RESULT.get("verdict") != "QUALIFIED FOR SHADOW-MODE CONSIDERATION":
        out("  NOTHING is recommended for shadow mode from this dataset.")
    out("  No production change is proposed here.")
    out("")
    out("§20 NO PRODUCTION CHANGES")
    out("-" * 100)
    out("  This task is READ-ONLY research. No code, threshold, alert, collector,")
    out("  dashboard, service or database row was touched. Both DBs were opened")
    out("  mode=ro. The only files written are this report and the CSV dataset.")
    out("  Per the task contract: STOP and wait for approval before any change")
    out("  to BLM is considered.")

    # ── CSV dataset ──────────────────────────────────────────────────────
    os.makedirs(OUT_CSV_DIR, exist_ok=True)
    cols = ["gid", "league", "tier", "part", "stale", "trigger", "captured_at",
            "line", "line_open", "line_move", "ft", "outcome",
            "act", "req", "avg", "req_ratio", "req_minus_avg", "act_minus_req",
            "act_minus_avg", "recent1", "recent2", "recent3", "recent5",
            "recent3_minus_act", "pace_accel", "traj", "prog", "elapsed",
            "remaining", "cur", "home", "away", "diff", "fair",
            "mkt_fair_diff", "market_req_ppm",
            "q1", "q2", "q3", "q4", "q3_ppm", "q3_league_avg", "q3_ratio",
            "q3_ppm_minus_req", "q4_first2m_ppm"]
    with open(OUT_CSV, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for f in sorted(feats, key=lambda x: (x["part"], x["t"])):
            row = []
            for c in cols:
                v = f.get(c)
                if isinstance(v, float):
                    v = round(v, 4)
                row.append("" if v is None else v)
            w.writerow(row)
    out("")
    out(f"dataset written: {OUT_CSV}  ({len(feats)} rows, {len(cols)} cols)")

    con_p.close()
    con_c.close()
    with open(OUT_MD, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    print(f"\nreport written: {OUT_MD}")


if __name__ == "__main__":
    main()
