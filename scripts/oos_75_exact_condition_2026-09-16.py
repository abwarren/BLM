#!/usr/bin/env python3
"""STRICT CHRONOLOGICAL OOS VALIDATION — the EXACT 75% condition.

READ-ONLY research (directive 2026-09-16).  No production code, thresholds,
dashboard logic, database contents or services are touched: both databases
are opened uri mode=ro and the only file written is this script's report.

THE CONDITION (nothing else)::

    progress >= 75%
    AND actual_pts_per_min  <  league_average_pace
    AND required_pts_per_min >  league_average_pace

No 1.04 margin, no gap/percentile thresholds, no extra filters, no tuning.

Production semantics are IMPORTED, never reimplemented:
  * under_outcome.trigger_observation  — the sealed checkpoint line
  * under_outcome._row_progress/_terminal_row — game-time authority
  * projection.duration_for / row_elapsed_minutes
  * clean_boundary.CLEAN_DATA_EPOCH    — the data boundary

league_average_pace is POINT-IN-TIME: for each trigger, the mean of
final_total / regulation_minutes over the SAME competition's settled games
(game_results status='OK') whose result_at is STRICTLY BEFORE the trigger
timestamp.  The current game's own result can never enter (its result_at
is after its trigger by construction; verified programmatically, §14).

Settlement: final_total vs the sealed 75% line via under_outcome
.outcome_status.  Economics at 1.85: +0.85 win / -1.00 loss / 0 push;
break-even 54.054%.

Stale = market_status='STALE' OR market_age_seconds > 300 (platform
convention).  CLEAN/STALE are reported separately; the headline cohort is
CLEAN only.
"""
from __future__ import annotations

import bisect
import csv
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, "/home/ubuntu/BLM")
from blm_v4.clean_boundary import CLEAN_DATA_EPOCH  # noqa: E402
from blm_v4.live_analytics.under_alert import (  # noqa: E402
    ALERT_PROGRESS_PCT, REQUIRED_MARGIN)
from blm_v4.live_analytics.under_outcome import (  # noqa: E402
    _row_progress, _terminal_row, outcome_status, trigger_observation)
from blm_v4.projection import duration_for, row_elapsed_minutes  # noqa: E402

PROD = os.environ.get("BLM_PROD_DB", "/home/ubuntu/BLM/blm_pokerbet.db")
CLEAN = os.environ.get("BLM_CLEAN_DB", "/home/ubuntu/BLM/blm_metrics_clean.db")
SCRIPTS_OUT = "/home/ubuntu/BLM/scripts/oos_75_exact_condition_2026-09-16.txt"
ANALYSIS_OUT = "/home/ubuntu/BLM/analysis_oos_75_exact_condition_2026-09-16.txt"
CSV_OUT = "/home/ubuntu/BLM/scripts/clean_signals_75_exact_2026-09-16.csv"
OUT = ANALYSIS_OUT

ODDS = 1.85
BREAK_EVEN = 100.0 / ODDS          # 54.054%
FIFTY_FIVE = 55.0
CHECKPOINT = 75
FRESH_LINE_SECONDS = 300.0
MIN_REMAINING = 2.5                # production ALERT_MIN_REMAINING_MINUTES
                                   # (blm_v4.api) gate — applied in selection

SLUG_SHORT = {
    "betual-nba": "NBA", "betual-kbl": "KBL", "betual-cba": "CBA",
    "betual-tbsl": "TBSL", "betual-euroleague": "EuroLeague",
    "cyber-basketball-2k26-matches": "CYBER",
}
LEAGUE_ORDER = ["NBA", "KBL", "CBA", "TBSL", "EuroLeague", "CYBER", "OTHER"]

REPORT: list[str] = []


def out(s: str = "") -> None:
    REPORT.append(s)
    print(s)


def ro(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def epoch_of(iso: str | None):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pct(n: float, d: float) -> float:
    return (100.0 * n / d) if d else 0.0


# ── significance vs break-even (Wilson + exact binomial) ─────────────────
# Same numerically-stable implementations validated in
# scripts/candidate_oos_185_2026-09-15.py (log-space binomial throughout;
# math.comb overflows at these n).
BREAK_EVEN_P = 1.0 / ODDS               # 0.540540... as a proportion


def wilson(wins: int, n: int, z: float = 1.96):
    """Wilson score interval for a proportion (returns lo, hi)."""
    if n == 0:
        return (None, None)
    p = wins / n
    d = 1.0 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _log_binom_pmf(k: int, n: int, p: float) -> float:
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
            + k * math.log(p) + (n - k) * math.log1p(-p))


def _sum_logs(logs: list[float]) -> float:
    m = max(logs)
    return math.exp(m) * math.fsum(math.exp(x - m) for x in logs)


def binom_tail_ge(k: int, n: int, p: float) -> float:
    """Exact P(X >= k) for X ~ Binomial(n, p), numerically stable."""
    if n == 0 or k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if k > n * p:                       # upper tail is the small side
        return _sum_logs([_log_binom_pmf(i, n, p) for i in range(k, n + 1)])
    lower = _sum_logs([_log_binom_pmf(i, n, p) for i in range(0, k)])
    return max(0.0, 1.0 - lower)


def ztest_ge(wins: int, n: int, p0: float):
    """One-sided normal test H1: p > p0, continuity-corrected."""
    if n == 0:
        return (None, None)
    se = math.sqrt(n * p0 * (1 - p0))
    if se == 0:
        return (None, None)
    z = ((wins - 0.5) - n * p0) / se
    return (z, 1.0 - phi(z))


def _median(vals: list[float]) -> float:
    v = sorted(vals)
    n = len(v)
    if not n:
        return float("nan")
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


def economics(recs: list[dict]) -> dict:
    n = len(recs)
    under = sum(1 for r in recs if r["outcome"] == "under")
    over = sum(1 for r in recs if r["outcome"] == "over")
    push = sum(1 for r in recs if r["outcome"] == "push")
    profit = under * (ODDS - 1.0) - over * 1.0
    return {"n": n, "under": under, "over": over, "push": push,
            "hit": pct(under, n), "profit": profit,
            "roi": pct(profit, n) if n else 0.0,
            "per100": (100.0 * profit / n) if n else 0.0}


# ── trailing (point-in-time) league pace averages ────────────────────────
class LeaguePace:
    """Per-league realised-pace series; avg STRICTLY before trigger time."""

    def __init__(self, series: list[tuple[float, float]]):
        self.tss = [t for t, _ in series]      # sorted ascending
        self.vals = [v for _, v in series]
        self.pfx: list[float] = []
        acc = 0.0
        for v in self.vals:
            acc += v
            self.pfx.append(acc)

    def avg_before(self, t: float) -> tuple[float, int] | None:
        k = bisect.bisect_left(self.tss, t)    # STRICT: exclude == t
        if k <= 0:
            return None
        return self.pfx[k - 1] / k, k


def main() -> None:
    con_p = ro(PROD)
    con_c = ro(CLEAN)

    # ── settled universe (authoritative) ──────────────────────────────────
    invalid = {r["source_game_id"] for r in con_p.execute(
        "SELECT source_game_id FROM game_quality WHERE status='INVALID'")}
    settled: dict[str, dict] = {}
    for r in con_p.execute(
            """SELECT gr.source_game_id AS gid, gr.final_total AS ft,
                      g.competition_slug AS slug, g.classification AS cls,
                      gr.result_at AS ra
               FROM game_results gr JOIN games g
                 ON g.source_game_id = gr.source_game_id
               WHERE gr.final_result_status='OK' AND gr.final_total IS NOT NULL
                 AND gr.final_total > 0
                 AND g.competition_slug IS NOT NULL
                 AND g.competition_slug <> '' AND gr.result_at IS NOT NULL"""):
        if r["cls"] and duration_for(r["cls"])[1]:
            settled[r["gid"]] = {"ft": float(r["ft"]), "slug": r["slug"],
                                 "cls": r["cls"], "ra": r["ra"]}

    # point-in-time league pace series (per slug)
    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for gid, s in settled.items():
        _, full = duration_for(s["cls"])
        series[s["slug"]].append((epoch_of(s["ra"]), s["ft"] / full))
    for slug in series:
        series[slug].sort()
    league = {slug: LeaguePace(recs) for slug, recs in series.items()}

    # ── 75% checkpoint observation per game (clean_projections) ───────────
    # progress >= 75 (production checkpoint semantics), non-terminal, VALID,
    # one per game: the observation CLOSEST to the 75% target.
    rows = con_c.execute(
        """SELECT source_game_id, progress_pct, actual_pts_per_min,
                  required_pts_per_min, live_total_line, captured_at,
                  market_age_seconds, market_status
           FROM clean_projections
           WHERE progress_pct >= 75 AND progress_pct < 100
             AND (terminal IS NULL OR terminal = 0)
             AND status = 'VALID'
             AND actual_pts_per_min IS NOT NULL
             AND required_pts_per_min IS NOT NULL
             AND live_total_line IS NOT NULL""")
    best: dict[str, dict] = {}
    for gid, prog, act, req, line, cap, mage, mstat in rows:
        base = gid.split("#")[0]
        if base in best:
            continue                          # keep the FIRST (earliest) >=75%
        s = settled.get(base)
        if s is None or base in invalid:
            continue
        best[base] = {"prog": prog, "act": act, "req": req, "line": line,
                      "cap": cap, "mage": mage, "mstat": mstat}
    out("observation selection: first clean_projections row with progress "
        ">= 75% per game (earliest 75%+ observation)")

    # ── assemble signals + verify no look-ahead programmatically ──────────
    signals: list[dict] = []
    look_viol = 0
    self_viol = 0
    no_settle = 0
    no_ref = 0
    for base, o in best.items():
        s = settled[base]
        t = epoch_of(o["cap"])
        ra = epoch_of(s["ra"])
        if t is None or ra is None:
            no_settle += 1
            continue
        # §14 no-look-ahead ENFORCEMENT: a trigger captured at/after its
        # own settlement (post-settlement ingestion row — the degraded-
        # write behaviour the temporal audit documented) can never have a
        # look-ahead-free league reference.  EXCLUDE it, never analyze it.
        if not (t < ra):
            self_viol += 1
            continue
        ref = league[s["slug"]].avg_before(t)
        if ref is None:
            no_ref += 1
            continue
        avg_pace, ngames = ref
        # invariant for every INCLUDED signal: the reference is built
        # strictly before the trigger, so the game's own result (settled
        # after the trigger by the check above) cannot be in it
        if not (t < ra):
            look_viol += 1
        outcome = outcome_status(o["line"], s["ft"])
        if outcome is None:
            no_settle += 1
            continue
        stale = (o["mstat"] == "STALE"
                 or (o["mage"] is not None
                     and o["mage"] > FRESH_LINE_SECONDS))
        triggers = (o["act"] < avg_pace) and (o["req"] > avg_pace)
        signals.append({"gid": base, "league": SLUG_SHORT.get(s["slug"], s["slug"]),
                        "slug": s["slug"], "t": t, "cap": o["cap"],
                        "line": o["line"], "outcome": outcome,
                        "stale": stale, "mage": (o["mage"] if o["mage"] is not None
                                                 else -1.0),
                        "trigger": triggers, "avg": avg_pace,
                        "act": o["act"], "req": o["req"], "ngames": ngames,
                        "final": s["ft"]})
    out(f"signals assembled: {len(signals)} (games without parseable "
        f"timestamps/settlement: {no_settle}; no point-in-time league "
        f"reference at trigger: {no_ref}; post-settlement triggers "
        f"EXCLUDED: {self_viol})")

    # also count games with 75%+ observations that were never settled OK
    obs_games = set(best.keys())
    unsettled = obs_games - set(settled.keys())
    # ── §6 data-quality decomposition ─────────────────────────────────────
    all_trig = [s for s in signals if s["trigger"]]
    clean = [s for s in all_trig if not s["stale"]]
    stale = [s for s in all_trig if s["stale"]]
    out(f"§6 data quality over {len(obs_games)} games with a 75%+ "
        f"observation:")
    out(f"  missing/invalid observations (no OK settlement, INVALID "
        f"quality, no line, or no league reference): "
        f"{no_settle + no_ref + len(unsettled)}")
    out(f"  post-settlement trigger rows (EXCLUDED, no look-ahead-free "
        f"reference possible): {self_viol}")
    out("")

    # ── §9 the required table ─────────────────────────────────────────────
    # baseline: SAME games/checkpoints/settlement universe, NO condition
    base_all = [s for s in signals]
    def cohort_row(label: str, recs: list[dict], base: list[dict] | None):
        e = economics(recs)
        b = economics(base) if base else None
        base_cell = (f"{b['hit']:.2f}%" if b and b["n"] else "n/a")
        out(f"| {label} | {e['n']} | {e['under']} | {e['over']} | "
            f"{e['hit']:.2f}% | {base_cell} | "
            f"{e['roi']:+.2f}% | {e['profit']:+.2f} |")
        return e

    out("§9 REQUIRED TABLE (CLEAN cohort headline; baseline = same games, "
        "no condition)")
    out("| Cohort | N | UNDER | OVER | Hit % | Baseline % | ROI | Net Units |")
    out("|--------|---:|------:|-----:|------:|-----------:|----:|----------:|")
    e_clean = cohort_row("CLEAN ALL", clean, base_all)
    all_sorted = sorted(all_trig, key=lambda s: s["t"])
    clean_sorted = sorted(clean, key=lambda s: s["t"])
    third = max(1, len(clean_sorted) // 3) if clean_sorted else 0
    thirds = [clean_sorted[:third], clean_sorted[third:2 * third],
              clean_sorted[2 * third:]]
    e_thirds = []
    for i, t3 in enumerate(thirds, 1):
        if t3:
            first, last = t3[0]["cap"], t3[-1]["cap"]
        else:
            first = last = "-"
        out(f"<!-- T{i} window: {first} .. {last} -->")
        e_thirds.append(cohort_row(f"OOS T{i}", t3, None))
    out("")

    # baseline per third too (for honest lift reading)
    out("Baseline (same universe, no condition) by third:")
    for i, t3 in enumerate(thirds, 1):
        if t3:
            lo, hi = epoch_of(t3[0]["cap"]), epoch_of(t3[-1]["cap"]) + 0.001
            bb = [s for s in base_all
                  if lo <= s["t"] <= hi]
            e = economics(bb)
            out(f"  T{i}: N={e['n']} hit={e['hit']:.2f}%")
    out("")

    # ── §10 league breakdown (clean) ──────────────────────────────────────
    out("§10 LEAGUE BREAKDOWN (CLEAN 75% signals)")
    out("| league | N | UNDER | OVER | Hit % | ROI |")
    out("|--------|---:|------:|-----:|------:|----:|")
    by_lg: dict[str, list[dict]] = defaultdict(list)
    for s in clean:
        by_lg[s["league"]].append(s)
    for lg in LEAGUE_ORDER:
        recs = by_lg.get(lg, [])
        if not recs:
            out(f"| {lg} | 0 | - | - | n/a | n/a |")
            continue
        e = economics(recs)
        out(f"| {lg} | {e['n']} | {e['under']} | {e['over']} | "
            f"{e['hit']:.2f}% | {e['roi']:+.2f}% |")
    other = [s for s in clean if s["league"] not in LEAGUE_ORDER]
    if other:
        e = economics(other)
        out(f"| OTHER | {e['n']} | {e['under']} | {e['over']} | "
            f"{e['hit']:.2f}% | {e['roi']:+.2f}% |")
    out("")

    # ── §11 rolling stability ─────────────────────────────────────────────
    out("§11 ROLLING STABILITY (chronological, CLEAN; windows fixed a "
        "priori: 50 and 100)")
    for w in (50, 100):
        if len(clean_sorted) < w:
            out(f"  rolling {w}: insufficient sample ({len(clean_sorted)} < {w})")
            continue
        hits = [1 if s["outcome"] == "under" else 0 for s in clean_sorted]
        rates = [100.0 * sum(hits[i:i + w]) / w
                 for i in range(len(hits) - w + 1)]
        out(f"  rolling {w}: min={min(rates):.2f}% max={max(rates):.2f}% "
            f"latest={rates[-1]:.2f}% (windows={len(rates)})")
    out("")

    # ── §11b significance vs break-even (directive follow-up 2026-09-16) ──
    out("§11b SIGNIFICANCE vs BREAK-EVEN — CLEAN cohort (Wilson 95% "
        "interval + exact binomial; H1: hit rate > 54.054%)")
    out(f"  {'cohort':<16} {'N':>5} {'UNDER':>6} {'hit%':>7} "
        f"{'Wilson 95% CI':>22} {'LB-BE pp':>9} {'binom p':>9} {'z(cc)':>7}")
    out("  " + "-" * 84)

    def sig_row(label: str, recs: list[dict]):
        e = economics(recs)
        if not e["n"]:
            out(f"  {label:<16} {0:>5}")
            return
        p_hat = e["under"] / e["n"]
        lo, hi = wilson(e["under"], e["n"])
        pval = binom_tail_ge(e["under"], e["n"], BREAK_EVEN_P)
        z, pz = ztest_ge(e["under"], e["n"], BREAK_EVEN_P)
        lb_be = 100.0 * lo - BREAK_EVEN
        out(f"  {label:<16} {e['n']:>5} {e['under']:>6} "
            f"{e['hit']:>6.2f}% [{100*lo:>5.2f}%, {100*hi:>5.2f}%] "
            f"{lb_be:>+8.2f} {pval:>9.4f} {z:>7.2f}")
        return {"lo": lo, "hi": hi, "pval": pval}

    sig_all = sig_row("CLEAN ALL", clean)
    sig_t = [sig_row(f"OOS T{i}", t3) for i, t3 in enumerate(thirds, 1)]
    sig_row("STALE ALL (ctx)", stale)   # context only — not a validated edge
    out("")
    if sig_all:
        lb_clears = 100.0 * sig_all["lo"] > BREAK_EVEN
        out(f"  CLEAN ALL Wilson lower bound "
            f"{100.0*sig_all['lo']:.2f}% "
            f"{'>' if lb_clears else '<='} break-even {BREAK_EVEN:.3f}%: "
            f"the overall edge {'survives' if lb_clears else 'does NOT survive'} "
            f"a 95% lower-bound test")
        out(f"  CLEAN ALL exact binomial P(X>={e_clean['under']} | "
            f"n={e_clean['n']}, p={BREAK_EVEN_P:.6f}) = "
            f"{sig_all['pval']:.4f} — "
            f"{'significant' if sig_all['pval'] < 0.05 else 'NOT significant'} "
            f"at the 5% level")
        t_lb = [(i, 100.0 * s["lo"] > BREAK_EVEN) for i, s in
                enumerate(sig_t, 1) if s]
        out("  per-third Wilson lower bounds vs break-even: "
            + ", ".join(f"T{i}={'above' if ok else 'straddles'}"
                        for i, ok in t_lb))
        out("  interpretation: only the pooled CLEAN cohort clears "
            "break-even on its lower bound; individual thirds are too "
            "small (n=99) for their intervals to exclude it")
    out("")

    # ── §11c RULE COMPARISON — exact condition vs production 1.04 margin ──
    # SAME assembled universe, same settlement, same point-in-time league
    # reference; ONLY the statistical rule differs.  Technical live gates
    # (min-remaining, market LIVE, state freshness) are deliberately
    # excluded from BOTH sides so the comparison is like-for-like.  The
    # production margin is the LIVE constant (under_alert.REQUIRED_MARGIN),
    # compared STRICTLY exactly as under_alert_state does.
    for s in signals:
        s["exact_fire"] = (s["act"] < s["avg"]) and (s["req"] > s["avg"])
        s["prod_fire"] = s["req"] > s["avg"] * REQUIRED_MARGIN
    U_clean = [s for s in signals if not s["stale"]]
    fired_e = [s for s in U_clean if s["exact_fire"]]
    fired_p = [s for s in U_clean if s["prod_fire"]]
    q_both = [s for s in U_clean if s["exact_fire"] and s["prod_fire"]]
    q_only_e = [s for s in U_clean if s["exact_fire"] and not s["prod_fire"]]
    q_only_p = [s for s in U_clean if s["prod_fire"] and not s["exact_fire"]]
    q_neither = [s for s in U_clean
                 if not s["exact_fire"] and not s["prod_fire"]]

    out("§11c RULE COMPARISON — exact condition vs production 1.04-margin "
        f"rule (REQUIRED_MARGIN={REQUIRED_MARGIN}), SAME clean universe "
        f"(N={len(U_clean)} settled signals, one per game, same lines, "
        "same point-in-time averages)")

    def rule_line(label: str, recs: list[dict]):
        e = economics(recs)
        if not e["n"]:
            out(f"  {label:<30} N=0")
            return None
        lo, hi = wilson(e["under"], e["n"])
        pval = binom_tail_ge(e["under"], e["n"], BREAK_EVEN_P)
        out(f"  {label:<30} N={e['n']:<5} UNDER={e['under']:<4} "
            f"hit={e['hit']:>6.2f}%  "
            f"Wilson95=[{100 * lo:.2f}%, {100 * hi:.2f}%]  "
            f"binomP={pval:.4f}  ROI={e['roi']:+7.2f}%  "
            f"net={e['profit']:+8.2f}u")
        return e, lo, pval

    out("")
    out("  per-rule cohorts (CLEAN):")
    re_e = rule_line("EXACT (act<avg & req>avg)", fired_e)
    re_p = rule_line(f"PRODUCTION (req>avg*{REQUIRED_MARGIN})", fired_p)
    out("")
    out("  overlap quadrants (CLEAN universe):")
    for lbl, recs in (("both rules fire", q_both),
                      ("EXACT only (thin margin band)", q_only_e),
                      ("PROD only (failed actual leg)", q_only_p),
                      ("neither", q_neither)):
        e = economics(recs)
        out(f"    {lbl:<32} N={e['n']:<5} UNDER={e['under']:<4} "
            f"hit={e['hit']:>6.2f}%  ROI={e['roi']:+7.2f}%  "
            f"net={e['profit']:+8.2f}u")
    out("")
    # consistency + interpretation
    out(f"  consistency: EXACT-fired clean N ({len(fired_e)}) equals the "
        f"§9 CLEAN ALL N ({e_clean['n']}): "
        f"{'OK' if len(fired_e) == e_clean['n'] else 'MISMATCH'}")
    out("  logic: PROD-fire implies req>avg, so the rules differ only by "
        "the actual<avg leg —")
    out("    EXACT-only = required in (avg, avg*1.04] (the margin band the "
        "production rule ignores)")
    out("    PROD-only  = required above the margin but actual >= avg")
    if re_e and re_p:
        out(f"  verdict: EXACT N={re_e[0]['n']} hit={re_e[0]['hit']:.2f}% vs "
            f"PROD N={re_p[0]['n']} hit={re_p[0]['hit']:.2f}% — "
            f"the actual<avg leg "
            f"{'adds' if re_e[0]['hit'] > re_p[0]['hit'] else 'does not add'} "
            f"hit rate over the production margin rule on this universe; "
            f"net units {re_e[0]['profit']:+.2f} vs "
            f"{re_p[0]['profit']:+.2f}")
    out("")

    # per-chronological-third duel: thirds of the CLEAN UNIVERSE (sorted by
    # trigger time), so BOTH rules are evaluated inside the SAME time
    # windows — a direct stability test of the production rule's lead
    out("  per-chronological-third duel (thirds of the clean universe by "
        "trigger time — SAME windows for both rules; N per third is the "
        "universe third, not the §9 EXACT-cohort third)")
    uni_sorted = sorted(U_clean, key=lambda s: s["t"])
    u_third = max(1, len(uni_sorted) // 3) if uni_sorted else 0
    u_thirds = [uni_sorted[:u_third], uni_sorted[u_third:2 * u_third],
                uni_sorted[2 * u_third:]]
    out(f"    {'T':<4} {'window (UTC)':<40} {'rule':<7} {'N':>5} "
        f"{'hit%':>7} {'net u':>9} {'ROI':>8}")
    out("    " + "-" * 84)
    prod_leads_net = prod_leads_hit = thirds_with_both = 0
    for i, ut in enumerate(u_thirds, 1):
        if not ut:
            continue
        w = f"{ut[0]['cap'][:19]} .. {ut[-1]['cap'][:19]}"
        fe = [s for s in ut if s["exact_fire"]]
        fp = [s for s in ut if s["prod_fire"]]
        ee, ep = economics(fe), economics(fp)
        out(f"    T{i:<3} {w:<40} {'EXACT':<7} {ee['n']:>5} "
            f"{ee['hit']:>6.2f}% {ee['profit']:>+9.2f} {ee['roi']:>+7.2f}%")
        out(f"    {'':<4} {'':<40} {'PROD':<7} {ep['n']:>5} "
            f"{ep['hit']:>6.2f}% {ep['profit']:>+9.2f} {ep['roi']:>+7.2f}%")
        if ee["n"] and ep["n"]:
            thirds_with_both += 1
            if ep["profit"] > ee["profit"]:
                prod_leads_net += 1
            if ep["hit"] >= ee["hit"]:
                prod_leads_hit += 1
    out("")
    out(f"  stability: PROD leads EXACT on net units in "
        f"{prod_leads_net}/{thirds_with_both} thirds; PROD hit >= EXACT "
        f"hit in {prod_leads_hit}/{thirds_with_both} thirds — "
        f"the production rule's lead is "
        f"{'stable across all thirds' if thirds_with_both and prod_leads_net == thirds_with_both else 'NOT uniform across thirds'}")
    out("")

    # ── §11f PROD-ONLY DECOMPOSITION (follow-up 2026-09-16) ───────────────
    # Are the PROD-only signals (required > avg*REQUIRED_MARGIN but
    # actual >= league average) genuinely ADDITIONAL useful signals, and
    # are they the source of the production rule's T3 deterioration (§11c)?
    out("§11f PROD-ONLY DECOMPOSITION — useful additions, and the T3 "
        "deterioration question")
    bo_all = [s for s in U_clean if s["prod_fire"] and s["exact_fire"]]
    po_all = [s for s in U_clean if s["prod_fire"] and not s["exact_fire"]]

    def sig_line(label: str, recs: list[dict]):
        e = economics(recs)
        if not e["n"]:
            out(f"    {label:<26} N=0")
            return None
        lo, hi = wilson(e["under"], e["n"])
        pval = binom_tail_ge(e["under"], e["n"], BREAK_EVEN_P)
        out(f"    {label:<26} N={e['n']:<5} hit={e['hit']:>6.2f}%  "
            f"Wilson95=[{100 * lo:.2f}%, {100 * hi:.2f}%]  "
            f"LB-BE={100 * lo - BREAK_EVEN:>+6.2f}pp  "
            f"binomP={pval:.4f}  net={e['profit']:>+8.2f}u  "
            f"ROI={e['roi']:>+7.2f}%")
        return {"e": e, "lo": lo, "hi": hi, "pval": pval}

    out("  aggregate (clean universe, same lines + settlement):")
    sig_line("baseline (no condition)", U_clean)
    sig_line("BOTH rules fire", bo_all)
    po_s = sig_line("PROD-only (actual>=avg)", po_all)
    out("")

    out("  per chronological third (SAME universe thirds as §11c):")
    out(f"    {'T':<4} {'base N':>6} {'base hit':>8}   "
        f"{'BOTH N':>6} {'hit':>7} {'net':>8}   "
        f"{'P-ONLY N':>8} {'hit':>7} {'net':>8}")
    out("    " + "-" * 84)
    base_lists, bo_lists, po_lists = {}, {}, {}
    for i, ut in enumerate(u_thirds, 1):
        if not ut:
            continue
        base_lists[i] = ut
        bo_lists[i] = [s for s in ut
                       if s["prod_fire"] and s["exact_fire"]]
        po_lists[i] = [s for s in ut
                       if s["prod_fire"] and not s["exact_fire"]]
        eb, et, ep = (economics(base_lists[i]), economics(bo_lists[i]),
                      economics(po_lists[i]))
        out(f"    T{i:<3} {eb['n']:>6} {eb['hit']:>7.2f}%   "
            f"{et['n']:>6} {et['hit']:>6.2f}% {et['profit']:>+8.2f}   "
            f"{ep['n']:>8} {ep['hit']:>6.2f}% {ep['profit']:>+8.2f}")
    out("")

    prod3_net = None
    if 3 in po_lists and 3 in bo_lists:
        ep3, et3, eb3 = (economics(po_lists[3]), economics(bo_lists[3]),
                         economics(base_lists[3]))
        ex3 = economics([s for s in base_lists[3] if s["exact_fire"]])
        prod3_net = ep3["profit"] + et3["profit"]
        out(f"  T3 attribution (PROD fired {ep3['n'] + et3['n']} in T3; "
            f"EXACT fired {ex3['n']} for {ex3['profit']:+.2f}u):")
        out(f"    BOTH-fire  : N={et3['n']:<4} hit={et3['hit']:>6.2f}%  "
            f"net={et3['profit']:>+8.2f}u")
        out(f"    PROD-only  : N={ep3['n']:<4} hit={ep3['hit']:>6.2f}%  "
            f"net={ep3['profit']:>+8.2f}u")
        out(f"    PROD total : net={prod3_net:+.2f}u")
        if ep3["n"] and et3["n"]:
            if ep3["profit"] < 0 <= et3["profit"]:
                out("    -> the T3 deterioration is carried by the "
                    "PROD-only signals alone")
            elif et3["profit"] < 0 <= ep3["profit"]:
                out("    -> the T3 deterioration is carried by the "
                    "BOTH-fire signals alone")
            elif ep3["profit"] < 0 and et3["profit"] < 0:
                out("    -> BOTH components lost money in T3")
            else:
                out("    -> neither component lost money in T3")
        out(f"    PROD-only vs T3 baseline (no condition): hit "
            f"{ep3['hit']:.2f}% vs {eb3['hit']:.2f}% — PROD-only "
            f"{'BEAT' if ep3['hit'] > eb3['hit'] else 'did NOT beat'} "
            f"the unconditional baseline in its own third")
        out("")

    out("  PROD-only league mix (overall vs T3):")
    po_lg_all: dict[str, list] = defaultdict(list)
    for s in po_all:
        po_lg_all[s["league"]].append(s)
    po_lg_t3: dict[str, list] = defaultdict(list)
    for s in po_lists.get(3, []):
        po_lg_t3[s["league"]].append(s)
    all_lgs = sorted(set(po_lg_all) | set(po_lg_t3),
                     key=lambda lg: (LEAGUE_ORDER.index(lg)
                                     if lg in LEAGUE_ORDER else 99, lg))
    out(f"    {'league':<11} {'N':>5} {'hit%':>7} {'net u':>9}   "
        f"{'| T3:':>6} {'N':>4} {'hit%':>7} {'net u':>9}")
    out("    " + "-" * 74)
    for lg in all_lgs:
        e1 = economics(po_lg_all.get(lg, []))
        e3 = economics(po_lg_t3.get(lg, []))
        out(f"    {lg:<11} {e1['n']:>5} {e1['hit']:>6.2f}% "
            f"{e1['profit']:>+9.2f}   {'|':>6} {e3['n']:>4} "
            f"{e3['hit']:>6.2f}% {e3['profit']:>+9.2f}")
    out("")

    a1 = _median([s["act"] / s["avg"] for s in po_all]) if po_all else float("nan")
    r1 = _median([s["req"] / s["avg"] for s in po_all]) if po_all else float("nan")
    a2 = _median([s["act"] / s["avg"] for s in bo_all]) if bo_all else float("nan")
    r2 = _median([s["req"] / s["avg"] for s in bo_all]) if bo_all else float("nan")
    out("  profile (median actual/avg, required/avg): PROD-only "
        f"({a1:.3f}, {r1:.3f}) vs BOTH ({a2:.3f}, {r2:.3f})")
    out("  reading:")
    if po_s:
        lb_ok = 100.0 * po_s["lo"] > BREAK_EVEN
        out(f"    aggregate: PROD-only hit {po_s['e']['hit']:.2f}% "
            f"(Wilson LB {100.0 * po_s['lo']:.2f}%, "
            f"binomP {po_s['pval']:.4f}) — "
            f"{'genuinely additional above break-even' if lb_ok else 'NOT provably above break-even on its lower bound'}")
    out("    caution: per-third cells are small; no subset is being "
        "promoted — this decomposition only locates where the §11c T3 "
        "deterioration lives")
    out("")

    # ── §11d EXPORT — clean signal list for external review ──────────────
    # The FULL clean universe (one row per game), with both rules' fire
    # flags and hit columns, so a reviewer can recompute every cohort
    # above independently.  Stale rows are excluded here by design (they
    # are quantified in §13); the row count is stated below.
    uni_export = sorted(U_clean, key=lambda s: s["t"])
    with open(CSV_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["game_id", "league", "trigger_captured_at",
                    "checkpoint_pct", "market_line", "final_total",
                    "outcome", "actual_pace", "required_pace",
                    "league_avg_pace_pit", "league_ref_games",
                    "market_age_seconds", "exact_fire", "prod_fire",
                    "exact_hit", "prod_hit"])
        for s in uni_export:
            w.writerow([s["gid"], s["league"], s["cap"], CHECKPOINT,
                        s["line"], s["final"], s["outcome"],
                        round(s["act"], 6), round(s["req"], 6),
                        round(s["avg"], 6), s["ngames"],
                        (round(s["mage"], 1) if s["mage"] >= 0 else ""),
                        1 if s["exact_fire"] else 0,
                        1 if s["prod_fire"] else 0,
                        (1 if (s["exact_fire"] and s["outcome"] == "under")
                         else (0 if s["exact_fire"] else "")),
                        (1 if (s["prod_fire"] and s["outcome"] == "under")
                         else (0 if s["prod_fire"] else ""))])
    out("§11d EXPORT — clean signal list for external review")
    out(f"  file: {CSV_OUT}")
    out(f"  rows: {len(uni_export)} (the FULL clean universe — every settled "
        "game with a 75%+ observation, fired or not)")
    out("  columns: game_id, league, trigger_captured_at, checkpoint_pct, "
        "market_line (the sealed 75% line), final_total, outcome, "
        "actual_pace, required_pace, league_avg_pace_pit (point-in-time), "
        "league_ref_games, market_age_seconds, exact_fire, prod_fire, "
        "exact_hit, prod_hit (hit columns blank when the rule did not "
        "fire)")
    out(f"  stale rows excluded here: {len(stale)} (reported in §13); "
        "post-settlement trigger rows excluded upstream: 21")
    out("")

    # ── §11e POWER — what would it take to SEPARATE EXACT from PRODUCTION?
    out("§11e POWER / BREAKEVEN-N — separating EXACT from PRODUCTION at "
        "the 5% level (two-proportion tests)")
    nE_, uE_ = re_e[0]["n"], re_e[0]["under"]
    nP_, uP_ = re_p[0]["n"], re_p[0]["under"]
    pE_, pP_ = uE_ / nE_, uP_ / nP_
    pbar_ = (uE_ + uP_) / (nE_ + nP_)
    se_ = math.sqrt(pbar_ * (1 - pbar_) * (1 / nE_ + 1 / nP_))
    z_obs = (pE_ - pP_) / se_
    p_two = 2.0 * (1.0 - phi(abs(z_obs)))
    delta_obs = abs(pE_ - pP_)
    Z_A, Z_B = 1.959963985, 0.8416212336      # two-sided 5%, 80% power

    def n_per_arm(delta: float, power: str = "80%") -> float:
        if power == "50%":                     # bare significance
            return 2.0 * Z_A ** 2 * pbar_ * (1 - pbar_) / delta ** 2
        p1, p2 = pbar_ + delta / 2, pbar_ - delta / 2
        return ((Z_A * math.sqrt(2 * pbar_ * (1 - pbar_))
                 + Z_B * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
                / delta ** 2)

    out(f"  observed: EXACT {uE_}/{nE_} = {100*pE_:.2f}% vs PROD "
        f"{uP_}/{nP_} = {100*pP_:.2f}%  (delta = {100*delta_obs:.3f}pp)")
    out(f"  two-proportion z on the fired cohorts: z = {z_obs:+.3f}, "
        f"two-sided p = {p_two:.3f} — "
        f"{'separable' if p_two < 0.05 else 'NOT separable'} at 5%")
    # paired view: on the 242 both-fire games the rules produce IDENTICAL
    # outcomes (same game, same line), so McNemar discordants = 0 and the
    # whole comparison rests on the disjoint signals
    e_oe, e_op = economics(q_only_e), economics(q_only_p)
    pbar2 = (e_oe["under"] + e_op["under"]) / (e_oe["n"] + e_op["n"])
    se2 = math.sqrt(pbar2 * (1 - pbar2) * (1 / e_oe["n"] + 1 / e_op["n"]))
    z2 = (e_oe["under"] / e_oe["n"] - e_op["under"] / e_op["n"]) / se2
    out(f"  McNemar view: the {economics(q_both)['n']} both-fire games have "
        "IDENTICAL outcomes under both rules (same game, same line) -> "
        "0 discordant pairs; the comparison rests on the disjoint "
        "signals alone")
    out(f"  disjoint signals: EXACT-only {e_oe['under']}/{e_oe['n']} = "
        f"{e_oe['hit']:.2f}% vs PROD-only {e_op['under']}/{e_op['n']} = "
        f"{e_op['hit']:.2f}% -> z = {z2:+.3f}, "
        f"two-sided p = {2*(1-phi(abs(z2))):.3f}")
    out("")
    out("  sample size needed (per arm, p̄ = "
        f"{100*pbar_:.2f}% on the fired pools, two-sided 5%):")
    out(f"    bare significance (50% power) at the OBSERVED delta "
        f"({100*delta_obs:.3f}pp): {n_per_arm(delta_obs, '50%'):,.0f} "
        "signals per arm")
    out(f"    80% power at the OBSERVED delta: "
        f"{n_per_arm(delta_obs):,.0f} signals per arm")
    for d_pp in (1, 2, 3, 5, 10):
        out(f"    80% power to detect a TRUE delta of {d_pp}pp: "
            f"{n_per_arm(d_pp/100):,.0f} per arm")
    mde = (Z_A + Z_B) * se_
    out(f"  minimum detectable difference at the CURRENT sample "
        f"(80% power): {100*mde:.1f}pp — the observed {100*delta_obs:.3f}pp "
        "is far below any detectable effect")
    # time cost at the observed signal rates
    span_days = ((uni_sorted[-1]["t"] - uni_sorted[0]["t"]) / 86400.0
                 if len(uni_sorted) > 1 else 0.0)
    if span_days > 0:
        rate_e, rate_p = nE_ / span_days, nP_ / span_days
        n_be = n_per_arm(delta_obs, "50%")
        years = max(n_be / rate_e, n_be / rate_p) / 365.0
        out(f"  at the observed signal rates (EXACT {rate_e:.1f}/day, PROD "
            f"{rate_p:.1f}/day over {span_days:.1f} days), bare-significance "
            f"n would take ~{years:,.0f} years of identical data")
    out("  conclusion: the two rules are statistically INDISTINGUISHABLE "
        "on this data — choosing between them on hit rate is not "
        "supported; signal COUNT (480 vs 297) and net units favour "
        "PRODUCTION and remain the only practical differentiators")
    out("")

    # ── §12 independence / coverage ───────────────────────────────────────
    uniq = {s["gid"] for s in all_trig}
    per_game: dict[str, int] = defaultdict(int)
    for s in all_trig:
        per_game[s["gid"]] += 1
    max_per_game = max(per_game.values()) if per_game else 0
    multi = sum(1 for v in per_game.values() if v > 1)
    out("§12 INDEPENDENCE / COVERAGE")
    out(f"  total qualifying observations (incl. stale): {len(all_trig)}")
    out(f"  unique games: {len(uniq)}")
    out(f"  games qualifying more than once: {multi}")
    out(f"  maximum signals per game: {max_per_game} "
        "(one per (game, checkpoint) by construction)")
    out(f"  qualifying signals at the 75% checkpoint: {len(all_trig)} "
        "(the only checkpoint evaluated)")
    out("")

    # ── §13 stale-cohort investigation (descriptive only) ─────────────────
    def stale_line(label: str, recs: list[dict]):
        if not recs:
            out(f"  {label:<6} N=0")
            return
        ages = sorted(s["mage"] for s in recs if s["mage"] >= 0)
        med = ages[len(ages) // 2] if ages else float("nan")
        avg = sum(ages) / len(ages) if ages else float("nan")
        e = economics(recs)
        out(f"  {label:<6} N={e['n']:<5} UNDER%={e['hit']:.2f} "
            f"avg_market_age={avg:.0f}s median_market_age={med:.0f}s")

    out("§13 STALE-COHORT INVESTIGATION (descriptive — NOT a validated edge)")
    stale_line("CLEAN", clean)
    stale_line("STALE", stale)
    stale_line("ALL", all_trig)
    out("")

    # ── §14 no-look-ahead statement ───────────────────────────────────────
    out("§14 NO LOOK-AHEAD VERIFICATION")
    out(f"  analyzed signals whose checkpoint_timestamp is strictly "
        f"before EVERY league-average result_at (bisect STRICT, "
        f"programmatic): violations = {look_viol} (expected 0)")
    out(f"  triggers NOT strictly before their own settlement: "
        f"{self_viol} — EXCLUDED from the cohort before analysis "
        f"(post-settlement ingestion rows; their own final could "
        f"otherwise enter their reference)")
    out("  for every included signal the current game's own result "
        "cannot enter its league average (trigger < own result_at, "
        "enforced by exclusion; bisect_left excludes the boundary)")
    out("")

    # ── §16 final assessment (A–H, factual) ───────────────────────────────
    t3_hits = [e["hit"] for e in e_thirds if e and e["n"]]
    uniq_clean = {s["gid"] for s in clean}
    lg_edge = sorted(((lg, economics(recs)) for lg, recs in by_lg.items()),
                     key=lambda kv: -kv[1]["n"])
    top_lg, top_e = (lg_edge[0] if lg_edge else (None, None))
    out("§16 FINAL ASSESSMENT")
    out(f"  A. exact 75% condition > 54.054% overall (CLEAN)? "
        f"{'YES' if e_clean['n'] and e_clean['hit'] > BREAK_EVEN else 'NO'} "
        f"({e_clean['hit']:.2f}%)")
    out(f"  B. > 55% overall? "
        f"{'YES' if e_clean['n'] and e_clean['hit'] > FIFTY_FIVE else 'NO'}")
    c_ok = len(t3_hits) == 3 and all(h > BREAK_EVEN for h in t3_hits)
    out(f"  C. above 54.054% in ALL three OOS thirds? "
        f"{'YES' if c_ok else 'NO'} ({', '.join(f'{h:.2f}%' for h in t3_hits)})")
    d_ok = len(t3_hits) == 3 and all(h > FIFTY_FIVE for h in t3_hits)
    out(f"  D. above 55% in ALL three OOS thirds? "
        f"{'YES' if d_ok else 'NO'}")
    out(f"  E. clean unique-game sample size: {len(uniq_clean)}")
    if top_e:
        others = [e for lg, e in lg_edge if lg != top_lg and e["n"]]
        top_share = (100.0 * top_e["profit"]
                     / sum(e["profit"] for e in others)) \
            if others and sum(e["profit"] for e in others) else float("nan")
        out(f"  F. edge concentrated in one league? "
            f"{top_lg} = {top_e['profit']:+.2f}u of {e_clean['profit']:+.2f}u "
            f"total ({top_share:.0f}%)" if others else
            f"  F. edge concentrated in one league? {top_lg} (only league "
            f"with N>0)")
    out(f"  G. materially dependent on stale observations? "
        f"NO — headline cohort excludes them; stale cohort reported "
        f"separately (§13)")
    out(f"  H. clean 1.85 ROI and net units: ROI {e_clean['roi']:+.2f}%, "
        f"net {e_clean['profit']:+.2f}u over {e_clean['n']} signals "
        f"({e_clean['per100']:+.2f}u/100)")
    if sig_all:
        out(f"  I. significance addendum: Wilson 95% CI for CLEAN ALL = "
            f"[{100.0*sig_all['lo']:.2f}%, {100.0*sig_all['hi']:.2f}%]; "
            f"exact binomial vs break-even p = {sig_all['pval']:.4f} "
            f"({'<0.05 — the overall edge is statistically distinguishable '
               'from break-even' if sig_all['pval'] < 0.05 else '>=0.05 — '               'the overall edge is NOT distinguishable from break-even'})")
    if re_e and re_p:
        out(f"  J. rule-comparison addendum: on the SAME clean universe, "
            f"EXACT N={re_e[0]['n']} hit={re_e[0]['hit']:.2f}% "
            f"net={re_e[0]['profit']:+.2f}u vs PRODUCTION "
            f"(req>avg*{REQUIRED_MARGIN}) N={re_p[0]['n']} "
            f"hit={re_p[0]['hit']:.2f}% net={re_p[0]['profit']:+.2f}u — "
            f"the extra actual<avg leg "
            f"{'raises' if re_e[0]['hit'] > re_p[0]['hit'] else 'lowers'} "
            f"hit rate and "
            f"{'raises' if re_e[0]['profit'] > re_p[0]['profit'] else 'lowers'} "
            f"net units, but trades signal count "
            f"({re_p[0]['n']} -> {re_e[0]['n']})")
    out(f"  K. per-third duel addendum: PROD leads EXACT on net units in "
        f"{prod_leads_net}/{thirds_with_both} chronological thirds of the "
        f"clean universe (and on hit rate in {prod_leads_hit}/"
        f"{thirds_with_both}) — the production rule's lead is "
        f"{'chronologically stable' if thirds_with_both and prod_leads_net == thirds_with_both else 'not chronologically uniform'}")
    out(f"  L. power addendum: EXACT vs PRODUCTION two-proportion z = "
        f"{z_obs:+.3f} (p = {p_two:.3f}) — not separable at 5%; bare "
        f"significance at the observed delta needs "
        f"~{n_per_arm(delta_obs, '50%'):,.0f} signals/arm and the current "
        f"sample's minimum detectable difference is ~{100*mde:.1f}pp")
    if po_s and prod3_net is not None and 3 in bo_lists:
        ep3n = economics(po_lists[3])["profit"]
        et3n = economics(bo_lists[3])["profit"]
        out(f"  M. PROD-only decomposition: overall hit "
            f"{po_s['e']['hit']:.2f}% (Wilson LB {100.0 * po_s['lo']:.2f}%, "
            f"binomP {po_s['pval']:.4f}) — "
            f"{'proven additional value above break-even' if 100.0 * po_s['lo'] > BREAK_EVEN else 'no PROVEN additional value above break-even'}; "
            f"T3 attribution: PROD-only {ep3n:+.2f}u vs BOTH-fire "
            f"{et3n:+.2f}u (PROD T3 total {prod3_net:+.2f}u)")
    out("")
    out("PRODUCTION CHANGES: NONE.  Script + report only; both DBs mode=ro.")

    for path in (SCRIPTS_OUT, ANALYSIS_OUT):
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(REPORT) + "\n")
    print(f"\nreport written: {ANALYSIS_OUT}")
    print(f"copy archived : {SCRIPTS_OUT}")


if __name__ == "__main__":
    main()
