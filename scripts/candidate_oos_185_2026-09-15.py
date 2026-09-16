#!/usr/bin/env python3
"""CANDIDATE OOS BACKTEST — which BLM Under signals sustain >55% at 1.85?

READ-ONLY.  Opens both databases ``file:...?mode=ro`` and writes only its own
report.  Every production calculation is IMPORTED, never reimplemented:

  * ``projection.duration_for`` / ``row_elapsed_minutes``  — game-time authority
  * ``live_analytics.under_alert.checkpoint_for``          — checkpoint identity
  * ``live_analytics.under_outcome.trigger_market_total``  — the settlement line
  * ``live_analytics.under_outcome.outcome_status``        — the verdict rule
  * ``live_analytics.under_outcome._row_progress``         — progress (0..1)
  * ``live_analytics.under_outcome._terminal_row``         — terminal authority
  * ``live_analytics.competition_pace.competition_pace_reference`` — league pace

The pace fields are DERIVED from the ``snapshots`` rows with the production
formula and were validated to reproduce the live path's ``clean_projections``
values exactly (0 mismatches over 14,759 field comparisons), so one source —
the same rows ``trigger_market_total`` settles from — drives the whole test.

Candidates (ALL gated on ``progress_pct >= 75``; one trigger per game):

  A   production       required > league_avg * 1.04
  A*  causal variant   required > trailing league_avg * 1.04   (leakage control)
  B   frozen P80       required >= frozen league P80
  C   trailing P80     required >= trailing-3d league P80
  D   trailing P85     required >= trailing-3d league P85
  E1  hist combo       actual < required AND actual < league_avg
  E2  hist combo       actual < required
  E3  hist combo       required > league_avg
  F   frozen P80+gap   B AND actual < required
  G   frozen P95+gap   P95 AND actual < required
  H   frozen P97.5+gap P97.5 AND actual < required

"Frozen" percentiles are estimated on the DISCOVERY period only and applied
unchanged afterwards — a whole-sample percentile would be look-ahead.  Trailing
percentiles read only observations strictly before the trigger timestamp.
"""
from __future__ import annotations

import bisect
import math
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/home/ubuntu/BLM")
from blm_v4.live_analytics.competition_pace import (  # noqa: E402
    competition_pace_reference)
from blm_v4.live_analytics.under_alert import checkpoint_for  # noqa: E402
from blm_v4.live_analytics.under_outcome import (  # noqa: E402
    _row_progress, _terminal_row, outcome_status, trigger_market_total)
from blm_v4.projection import duration_for, row_elapsed_minutes  # noqa: E402

PROD = os.environ.get("BLM_PROD_DB", "/home/ubuntu/BLM/blm_pokerbet.db")
OUT = os.environ.get(
    "BLM_OOS_OUT", "/home/ubuntu/BLM/analysis_candidate_oos_185_2026-09-15.txt")
EPOCH = "2026-09-05T05:40:41.782315Z"          # clean_boundary.CLEAN_DATA_EPOCH
MIN_REMAINING = 2.5                            # ALERT_MIN_REMAINING_MINUTES
PROGRESS_PCT = 75.0                            # ALERT_PROGRESS_PCT
MARGIN = 1.04                                  # REQUIRED_MARGIN
ODDS = 1.85
BREAK_EVEN = 1.0 / ODDS                        # 0.540540...
WIN_UNITS, LOSS_UNITS = ODDS - 1.0, 1.0        # +0.85 / -1.00
DAY = 86400.0
TRAIL_DAYS = 3
CHECKPOINT = 75                                # every candidate gates at >=75%

SLUG_SHORT = {
    "betual-nba": "NBA", "betual-kbl": "KBL", "betual-cba": "CBA",
    "betual-tbsl": "TBSL", "betual-euroleague": "Euro",
    "cyber-basketball-2k26-matches": "CYBER",
}
LEAGUE_ORDER = ["NBA", "KBL", "CBA", "TBSL", "Euro", "CYBER"]


def ro(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _f(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def epoch_of(iso):
    """ISO-8601 'Z' timestamp -> epoch seconds (None when unparseable)."""
    if not iso:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pctile(sorted_vals, q):
    """Linear-interpolation percentile of an already-sorted list."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    pos = (n - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def wilson(wins, n, z=1.96):
    if n == 0:
        return (None, None)
    p = wins / n
    d = 1.0 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _log_binom_pmf(k, n, p):
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
            + k * math.log(p) + (n - k) * math.log1p(-p))


def _sum_logs(logs):
    m = max(logs)
    return math.exp(m) * math.fsum(math.exp(x - m) for x in logs)


def binom_tail_ge(k, n, p):
    """Exact P(X >= k) for X ~ Binomial(n, p), numerically stable.

    Log-space throughout (``math.comb(n, i)`` overflows for the sample sizes
    here), and it always sums the smaller of the two tails.
    """
    if n == 0 or k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if k > n * p:                       # upper tail is the small side
        return _sum_logs([_log_binom_pmf(i, n, p) for i in range(k, n + 1)])
    lower = _sum_logs([_log_binom_pmf(i, n, p) for i in range(0, k)])
    return max(0.0, 1.0 - lower)


def ztest_ge(wins, n, p0):
    """One-sided normal test H1: p > p0, continuity-corrected.

    ``z = ((wins - 0.5) - n*p0) / sqrt(n*p0*(1-p0))``  — the corrected count
    over the standard deviation of the COUNT, not of the proportion.
    """
    if n == 0:
        return (None, None)
    se = math.sqrt(n * p0 * (1 - p0))
    if se == 0:
        return (None, None)
    z = ((wins - 0.5) - n * p0) / se
    return (z, 1.0 - phi(z))


# ───────────────────────────── phase 1 — collect ──────────────────────────
def collect(con_p):
    """Stream settled games; return per-game eligible observations and the
    sealed settlement line computed by the PRODUCTION function."""
    invalid = {r["source_game_id"] for r in con_p.execute(
        "SELECT source_game_id FROM game_quality WHERE status='INVALID'")}
    league_ref = competition_pace_reference(con_p)

    q = """
        SELECT g.source_game_id, g.classification, g.competition_slug,
               gr.final_total, gr.result_at,
               s.captured_at, s.quarter, s.clock, s.period_label,
               s.game_status, s.home_score, s.away_score, s.total_line
        FROM snapshots s
        JOIN games g ON g.id = s.game_id
        JOIN game_results gr ON gr.source_game_id = g.source_game_id
        WHERE gr.final_result_status = 'OK'
          AND gr.final_total IS NOT NULL AND gr.final_total > 0
          AND s.captured_at >= ?
        ORDER BY g.source_game_id, s.captured_at ASC
    """
    games, cur, batch, meta = [], None, [], None
    settled_pace = defaultdict(list)   # league short -> [(result_at_epoch, pace)]
    for r in con_p.execute(q, (EPOCH,)):
        gid = r["source_game_id"]
        if gid != cur:
            if batch:
                g = _finish(batch, meta, invalid, league_ref)
                if g:
                    games.append(g)
            cur, batch = gid, []
            meta = (r["classification"], r["competition_slug"],
                    r["final_total"], r["result_at"])
        batch.append(dict(r))
    if batch:
        g = _finish(batch, meta, invalid, league_ref)
        if g:
            games.append(g)

    # settled-game pace series per league, for the causal league average
    for r in con_p.execute(
            "SELECT g.competition_slug AS s, g.classification AS c, "
            "       gr.final_total AS f, gr.result_at AS ra "
            "FROM game_results gr JOIN games g ON g.source_game_id=gr.source_game_id "
            "WHERE gr.final_result_status='OK' AND gr.final_total>0 "
            "  AND g.competition_slug IS NOT NULL AND g.competition_slug<>''"):
        sh = SLUG_SHORT.get(r["s"])
        t = epoch_of(r["ra"])
        if sh and t is not None:
            _, full = duration_for(r["c"])
            if full:
                settled_pace[sh].append((t, r["f"] / full))
    for v in settled_pace.values():
        v.sort()
    return games, league_ref, settled_pace, invalid


def _finish(rows, meta, invalid, league_ref):
    """Per-game pass: sealed line, eligibility, and the >=75% observations."""
    cls, slug, final, _ra = meta
    gid = rows[0]["source_game_id"]
    if gid in invalid or not slug or slug not in league_ref:
        return None
    qmin, full = duration_for(cls)
    if not full:
        return None
    sealed = trigger_market_total(rows, CHECKPOINT, cls)
    obs = []
    for row in rows:
        if _terminal_row(row, cls):
            continue
        el = row_elapsed_minutes(row, qmin, full)
        if el is None or el <= 0:
            continue
        rem = full - el
        if rem < MIN_REMAINING:            # ALERT_MIN_REMAINING_MINUTES gate
            continue
        prog = _row_progress(row, cls)
        if prog is None or prog * 100.0 < PROGRESS_PCT:
            continue
        h, a = _f(row.get("home_score")), _f(row.get("away_score"))
        if h is None or a is None:
            continue
        total = h + a
        line = _f(row.get("total_line"))
        if line is None:                   # nothing observed yet -> no market
            continue
        t = epoch_of(row.get("captured_at"))
        if t is None:
            continue
        actual = total / el
        required = (line - total) / rem
        if not (math.isfinite(actual) and math.isfinite(required)):
            continue
        obs.append({"t": t, "iso": row.get("captured_at"),
                    "prog": prog * 100.0, "total": total, "line": line,
                    "actual": actual, "required": required})
    if not obs:
        return None
    obs.sort(key=lambda o: (o["t"], o["iso"] or ""))
    return {"gid": gid, "league": SLUG_SHORT.get(slug, slug), "cls": cls,
            "final": float(final), "sealed": sealed, "obs": obs}


# ───────────────────── phase 2 — percentile cutoffs ───────────────────────
class Trail:
    """Sliding 3-day percentile over a league's observations.  Queries must be
    non-decreasing in time; the window is strictly ``[t-3d, t)``."""

    def __init__(self, recs):
        self.recs = recs                  # sorted [(t, value)]
        self.vals, self.i, self.j = [], 0, 0
        self.worst_slack = None           # max(cutoff_max_t - t) over queries

    def at(self, t):
        recs = self.recs
        while self.i < len(recs) and recs[self.i][0] < t:
            bisect.insort(self.vals, recs[self.i][1])
            self.i += 1
        lim = t - TRAIL_DAYS * DAY
        while self.j < self.i and recs[self.j][0] < lim:
            v = recs[self.j][1]
            k = bisect.bisect_left(self.vals, v)
            if k < len(self.vals) and self.vals[k] == v:
                self.vals.pop(k)
            self.j += 1
        # leakage witness: the newest datum the cutoff may have consumed
        newest = recs[self.i - 1][0] if self.i else None
        if newest is not None:
            slack = newest - t          # must be < 0 (strictly before trigger)
            if self.worst_slack is None or slack > self.worst_slack:
                self.worst_slack = slack
        return (pctile(self.vals, 0.80), pctile(self.vals, 0.85), len(self.vals))


def build_cutoffs(games, split):
    """Frozen (discovery-only) and trailing percentiles, per league."""
    disc_end = split["disc_end"]
    frozen, by_league = {}, defaultdict(list)
    for g in games:
        for o in g["obs"]:
            by_league[g["league"]].append((o["t"], o["required"]))
    for lg, recs in by_league.items():
        recs.sort()
        discover = sorted(v for (t, v) in recs if t < disc_end)
        frozen[lg] = {
            "P80": pctile(discover, 0.80), "P95": pctile(discover, 0.95),
            "P97.5": pctile(discover, 0.975), "n_discovery": len(discover),
            "all_history_P80": pctile(sorted(v for _t, v in recs), 0.80),
        }
    trails = {}
    for lg, recs in by_league.items():
        qs = sorted({t for (t, _v) in recs})
        tr = Trail(recs)
        trails[lg] = {t: tr.at(t) for t in qs}
        trails[lg]["_worst_slack"] = tr.worst_slack
    return frozen, trails


def causal_league_avg(settled_pace):
    """league -> function(t) -> mean settled pace strictly before t."""
    state = {}
    for lg, recs in settled_pace.items():
        running, out = 0.0, []
        for i, (t, v) in enumerate(recs):       # prefix mean up to each point
            running += v
            out.append((t, running / (i + 1)))
        state[lg] = out

    def query(lg, t):
        rows = state.get(lg)
        if not rows:
            return None
        i = bisect.bisect_left(rows, (t, float("-inf")))
        return rows[i - 1][1] if i > 0 else None

    return query


# ─────────────────────────── candidate evaluation ─────────────────────────
def evaluate(games, league_ref, frozen, trails, caus_avg, split):
    """One trigger per game per candidate; settle via V4's sealed line."""
    avg_of = {SLUG_SHORT.get(k, k): v["avg_pace"] for k, v in league_ref.items()}
    bets = defaultdict(list)
    baseline = []                           # every eligible game, no condition
    cov = {k: {"elig": set(), "trig": set()}
           for k in ["A", "A*", "B", "C", "D", "E1", "E1*", "E2", "E3",
                     "F", "G", "H"]}
    for g in games:
        lg, la = g["league"], avg_of.get(g["league"])
        fz, tr = frozen.get(lg) or {}, trails.get(lg) or {}
        outcome = outcome_status(g["sealed"], g["final"])
        if outcome is None:
            continue                        # unprovable line/final -> excluded
        for key in cov:
            cov[key]["elig"].add(g["gid"])
        baseline.append({"gid": g["gid"], "league": lg, "t": g["obs"][0]["t"],
                         "iso": g["obs"][0]["iso"], "prog": g["obs"][0]["prog"],
                         "required": None, "actual": None, "cond_line": None,
                         "sealed": g["sealed"], "final": g["final"],
                         "outcome": outcome})

        # per-observation candidate flags, computed once
        flags = []
        for o in g["obs"]:
            tl = tr.get(o["t"]) or (None, None, 0)
            p80, p85 = tl[0], tl[1]
            ca = caus_avg(lg, o["t"])
            req, act = o["required"], o["actual"]
            f = {
                "A": la is not None and req > la * MARGIN,
                "A*": ca is not None and req > ca * MARGIN,
                "B": fz.get("P80") is not None and req >= fz["P80"],
                "C": p80 is not None and req >= p80,
                "D": p85 is not None and req >= p85,
                "E1": act < req and la is not None and act < la,
                "E1*": act < req and ca is not None and act < ca,
                "E2": act < req,
                "E3": la is not None and req > la,
            }
            f["F"] = f["B"] and act < req
            f["G"] = (fz.get("P95") is not None and req >= fz["P95"]
                      and act < req)
            f["H"] = (fz.get("P97.5") is not None and req >= fz["P97.5"]
                      and act < req)
            flags.append((o, f))

        for key in cov:                     # first qualifying observation wins
            for o, f in flags:
                if f[key]:
                    cov[key]["trig"].add(g["gid"])
                    bets[key].append({
                        "gid": g["gid"], "league": lg, "t": o["t"],
                        "iso": o["iso"], "prog": o["prog"],
                        "required": o["required"], "actual": o["actual"],
                        "cond_line": o["line"], "sealed": g["sealed"],
                        "final": g["final"], "outcome": outcome})
                    break
    return bets, cov, baseline


# ────────────────────────────── reporting ─────────────────────────────────
def money(bs):
    wins = sum(1 for b in bs if b["outcome"] == "under")
    losses = sum(1 for b in bs if b["outcome"] == "over")
    pushes = sum(1 for b in bs if b["outcome"] == "push")
    n = len(bs)
    profit = wins * WIN_UNITS - losses * LOSS_UNITS
    return {"n": n, "wins": wins, "losses": losses, "pushes": pushes,
            "win_pct": (wins / n if n else None),
            "loss_pct": (losses / n if n else None),
            "push_pct": (pushes / n if n else None),
            "units": profit, "roi": (profit / n if n else None),
            "exp100": (profit / n * 100 if n else None)}


def streaks(bs):
    """Max drawdown (units) and longest losing streak, chronological."""
    eq = 0.0
    peak = dd = 0.0
    loss_run = worst_run = 0
    for b in bs:
        eq += (WIN_UNITS if b["outcome"] == "under" else
               (-LOSS_UNITS if b["outcome"] == "over" else 0.0))
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        if b["outcome"] == "over":
            loss_run += 1
            worst_run = max(worst_run, loss_run)
        elif b["outcome"] == "under":
            loss_run = 0
    return dd, worst_run


def rolling(bs, w):
    if len(bs) < w:
        return (None, None, None)
    rates = [sum(1 for b in bs[i:i + w] if b["outcome"] == "under") / w
             for i in range(len(bs) - w + 1)]
    return (sum(rates) / len(rates), min(rates),
            sum(1 for r in rates if r > BREAK_EVEN) / len(rates))


def slice_of(bs, lo, hi):
    return [b for b in bs if lo <= b["t"] < hi]


def stats_line(bs, label, w=26):
    m = money(bs)
    if not m["n"]:
        return f"  {label:<{w}} n=0"
    return (f"  {label:<{w}} n={m['n']:<5} win={fmt_pct(m['win_pct'])} "
            f"roi={fmt_pct(m['roi'])} units={m['units']:+.1f}")


def fmt_pct(x, d=2):
    return "n/a" if x is None else f"{x * 100:.{d}f}%"


def fmt(x, d=3):
    return "n/a" if x is None else f"{x:.{d}f}"


CAND_LABELS = [
    ("A", "A  production: req > league_avg x1.04"),
    ("A*", "A* causal: req > trailing league_avg x1.04"),
    ("B", "B  frozen league P80"),
    ("C", "C  trailing 3d P80"),
    ("D", "D  trailing 3d P85"),
    ("E1", "E1 actual<req AND actual<league_avg"),
    ("E1*", "E1* actual<req AND actual<trailing league_avg"),
    ("E2", "E2 actual<req"),
    ("E3", "E3 req>league_avg"),
    ("F", "F  frozen P80 + actual<req"),
    ("G", "G  frozen P95 + actual<req"),
    ("H", "H  frozen P97.5 + actual<req"),
]


def report(games, league_ref, frozen, trails, bets, cov, split, baseline):
    L = []
    add = L.append
    add("=" * 100)
    add("BLM CANDIDATE OOS BACKTEST — signals at 1.85 (break-even 54.054%)")
    add("READ-ONLY.  Settlement: under_outcome.trigger_market_total (V4 sealed line).")
    add("=" * 100)
    add("")
    add("LEAGUE REFERENCE (competition_pace_reference, OK-only) — used by candidate A")
    for lg in LEAGUE_ORDER:
        k = [s for s, v in SLUG_SHORT.items() if v == lg]
        ref = next((league_ref[s] for s in k if s in league_ref), None)
        fz = frozen.get(lg, {})
        if ref:
            add(f"  {lg:<6} avg={ref['avg_pace']:<8} games={ref['games']:<6} "
                f"frozen P80={fmt(fz.get('P80'))} P95={fmt(fz.get('P95'))} "
                f"P97.5={fmt(fz.get('P97.5'))}  (n_discovery={fz.get('n_discovery')})")
    add("")
    add("CHRONOLOGICAL SPLIT (by trigger timestamp; candidate-independent)")
    add(f"  span          {split['t0_iso']}  ->  {split['t1_iso']}")
    add(f"  discovery     0-50%   < {split['disc_end_iso']}")
    add(f"  validation    50-75%  {split['disc_end_iso']} .. {split['val_end_iso']}")
    add(f"  latest OOS    75-100% > {split['val_end_iso']}")
    add(f"  blocks        equal thirds at {split['b1_iso']} / {split['b2_iso']}")
    add("")
    add(f"POPULATION  eligible games (>=1 observation at >={PROGRESS_PCT:.0f}% "
        f"with a live line, remaining>={MIN_REMAINING}): {len(cov['A']['elig'])}")
    add("")

    base = index_bets(baseline)
    add("BASELINE — every eligible game, no condition (control for regime shift)")
    bm = money(baseline)
    add(f"  n={bm['n']}  win={fmt_pct(bm['win_pct'])}  roi={fmt_pct(bm['roi'])}")
    for nm, lo, hi in (("Discovery  ", split["t0"], split["disc_end"]),
                       ("Validation ", split["disc_end"], split["val_end"]),
                       ("Latest OOS ", split["val_end"], split["t1"] + 1)):
        mm = money(slice_of(baseline, lo, hi))
        add(f"  {nm} n={mm['n']:<5} win={fmt_pct(mm['win_pct'])} "
            f"roi={fmt_pct(mm['roi'])}")
    for i, (a, b) in enumerate([(split["t0"], split["b1"]),
                                (split["b1"], split["b2"]),
                                (split["b2"], split["t1"] + 1)], 1):
        mm = money(slice_of(baseline, a, b))
        add(f"  Block {i}     n={mm['n']:<5} win={fmt_pct(mm['win_pct'])} "
            f"roi={fmt_pct(mm['roi'])}")
    add("")

    for key, label in CAND_LABELS:
        bs = bets[key]
        L.extend(candidate_block(key, label, bs, cov[key], split))
    return "\n".join(L), L


def index_bets(bs):
    return sorted(bs, key=lambda b: (b["t"], b["iso"] or ""))


def candidate_block(key, label, bs, c, split):
    out = []
    m = money(bs)
    out.append("─" * 100)
    out.append(f"{label}")
    out.append("─" * 100)
    if not bs:
        out.append("  NO TRIGGERS.")
        out.append("")
        return out
    out.append(f"  Bets {m['n']}  Wins {m['wins']}  Losses {m['losses']}  "
               f"Pushes {m['pushes']}")
    out.append(f"  Win% {fmt_pct(m['win_pct'])}  Loss% {fmt_pct(m['loss_pct'])}  "
               f"Push% {fmt_pct(m['push_pct'])}")
    out.append(f"  Net units {m['units']:+.2f}   ROI {fmt_pct(m['roi'])}   "
               f"per 100 bets {m['exp100']:+.2f} u")
    lo, hi = wilson(m["wins"], m["n"])
    z, pone = ztest_ge(m["wins"], m["n"], BREAK_EVEN)
    pexact = binom_tail_ge(m["wins"], m["n"], BREAK_EVEN)
    out.append(f"  95% CI (Wilson) [{fmt_pct(lo)} , {fmt_pct(hi)}]   "
               f"excludes break-even: {'YES' if lo and lo > BREAK_EVEN else 'NO'}")
    out.append(f"  one-sided z vs 54.054%: z={fmt(z,2)}  p(norm)={fmt(pone,5)}  "
               f"p(exact)={fmt(pexact,5)}")
    # chronological OOS
    disc = slice_of(bs, split["t0"], split["disc_end"])
    val = slice_of(bs, split["disc_end"], split["val_end"])
    lat = slice_of(bs, split["val_end"], split["t1"] + 1)
    for nm, part in (("Discovery  ", disc), ("Validation ", val),
                     ("Latest OOS ", lat)):
        mm = money(part)
        lo_p, hi_p = wilson(mm["wins"], mm["n"])
        out.append(f"  {nm} n={mm['n']:<5} win={fmt_pct(mm['win_pct'])} "
                   f"CI[{fmt_pct(lo_p)},{fmt_pct(hi_p)}] "
                   f"roi={fmt_pct(mm['roi'])} units={mm['units']:+.1f}")
    # three equal time blocks
    for i, (a, b) in enumerate([(split["t0"], split["b1"]),
                                (split["b1"], split["b2"]),
                                (split["b2"], split["t1"] + 1)], 1):
        mm = money(slice_of(bs, a, b))
        out.append(f"  Block {i}     n={mm['n']:<5} win={fmt_pct(mm['win_pct'])} "
                   f"roi={fmt_pct(mm['roi'])} units={mm['units']:+.1f}")
    # rolling
    for w in (50, 100, 200):
        mean, mn, share = rolling(bs, w)
        if mean is None:
            out.append(f"  Rolling {w:<4} insufficient bets")
        else:
            out.append(f"  Rolling {w:<4} mean win={fmt_pct(mean)} "
                       f"min={fmt_pct(mn)} windows>BE={fmt_pct(share)}")
    dd, run = streaks(bs)
    out.append(f"  Max drawdown {dd:.2f} u   longest losing streak {run}")
    # classification
    out.append(f"  STABILITY: {classify(m, disc, val, lat, split)}")
    # league table
    out.append("  LEAGUE   Bets   Wins  Loss   Win%      ROI      units")
    for lg in LEAGUE_ORDER:
        sub = [b for b in bs if b["league"] == lg]
        mm = money(sub)
        out.append(f"  {lg:<8} {mm['n']:<6} {mm['wins']:<5} {mm['losses']:<5} "
                   f"{fmt_pct(mm['win_pct']):<9} {fmt_pct(mm['roi']):<8} "
                   f"{mm['units']:+.1f}")
    if bs:
        top = max(LEAGUE_ORDER, key=lambda lg: sum(1 for b in bs if b["league"] == lg))
        share = sum(1 for b in bs if b["league"] == top) / len(bs)
        out.append(f"  largest league contribution: {top} {fmt_pct(share)}")
        ex = [b for b in bs if b["league"] != "CYBER"]
        mm = money(ex)
        out.append(f"  excluding CYBER:               n={mm['n']} "
                   f"win={fmt_pct(mm['win_pct'])} roi={fmt_pct(mm['roi'])} "
                   f"units={mm['units']:+.1f}")
    # coverage
    tr, el = len(c["trig"]), len(c["elig"])
    out.append(f"  COVERAGE triggered {tr} / eligible {el} = "
               f"{fmt_pct(tr / el if el else None)}")
    out.append("")
    return out


def classify(m, disc, val, lat, split):
    if m["n"] < 100:
        return "Insufficient sample (<100 bets)"
    if m["win_pct"] is not None and m["win_pct"] <= BREAK_EVEN:
        return ("Not profitable (overall <=54.054%)")
    parts = [money(x)["win_pct"] for x in (disc, val, lat)]
    parts = [p for p in parts if p is not None]
    if len(parts) < 2:
        return "Insufficient sample (OOS partitions too thin)"
    above = [p > 0.55 for p in parts]
    if all(above):
        return "Stable (above 55% in every chronological partition)"
    if parts[0] > 0.55 and any(p <= 0.55 for p in parts[1:]):
        return "Decaying (discovery >55%, later partitions <=55%)"
    if above.count(True) and above.count(False) and max(parts) - min(parts) > 0.10:
        return "Unstable (large alternating swings)"
    return "Mixed / no persistent >55% pattern"


def final_table(bets, cov, frozen, trails):
    rows = []
    for key, label in CAND_LABELS:
        bs = bets[key]
        m = money(bs)
        lo, hi = wilson(m["wins"], m["n"])
        disc = m["n"] and money([b for b in bs if b["t"] < splitg["disc_end"]])
        val = m["n"] and money([b for b in bs if splitg["disc_end"] <= b["t"] < splitg["val_end"]])
        lat = m["n"] and money([b for b in bs if b["t"] >= splitg["val_end"]])
        tr, el = len(cov[key]["trig"]), len(cov[key]["elig"])
        leak = ("causal (verified)" if key in ("C", "D", "A*")
                else "discovery-frozen" if key in ("B", "F", "G", "H")
                else "n/a (no cutoff)")
        rows.append((key, label, m, disc, val, lat, tr, el, lo, hi, leak))
    return rows


splitg = {}


def main():
    con_p = ro(PROD)
    print("collecting…", file=sys.stderr)
    games, league_ref, settled_pace, invalid = collect(con_p)

    times = sorted(o["t"] for g in games for o in g["obs"])
    if not times:
        raise SystemExit("no eligible observations")
    t0, t1 = times[0], times[-1]
    span = t1 - t0
    split = {"t0": t0, "t1": t1,
             "disc_end": t0 + span * 0.50,
             "val_end": t0 + span * 0.75,
             "b1": t0 + span / 3.0, "b2": t0 + span * 2.0 / 3.0}
    from datetime import datetime, timezone
    for k in ("t0", "t1", "disc_end", "val_end", "b1", "b2"):
        split[k + "_iso"] = datetime.fromtimestamp(
            split[k], timezone.utc).strftime("%Y-%m-%d %H:%M")
    splitg.update(split)

    frozen, trails = build_cutoffs(games, split)
    caus = causal_league_avg(settled_pace)
    bets, cov, baseline = evaluate(games, league_ref, frozen, trails, caus, split)

    text, lines = report(games, league_ref, frozen, trails, bets, cov, split,
                         baseline)

    # leakage + coverage + final table appended
    extra = []
    extra.append("=" * 100)
    extra.append("LEAKAGE VERIFICATION — trailing cutoffs")
    extra.append("=" * 100)
    for lg in LEAGUE_ORDER:
        tr = trails.get(lg)
        if not tr or tr.get("_worst_slack") is None:
            extra.append(f"  {lg:<6} no trailing queries")
            continue
        slack = tr["_worst_slack"]
        extra.append(f"  {lg:<6} youngest datum consumed in any cutoff is "
                     f"{abs(slack):.1f}s BEFORE its trigger "
                     f"-> {'OK' if slack < 0 else 'VIOLATION'}")
    extra.append("  window rule: strictly [t-3d, t); the trigger observation is")
    extra.append("  never a member of its own cutoff.  'actual < required' uses only")
    extra.append("  the trigger row's own fields (same timestamp) -> no leakage.")
    extra.append("")

    extra.append("=" * 100)
    extra.append("FINAL COMPARISON")
    extra.append("=" * 100)
    hdr = (f"  {'cand':<5}{'bets':>6}{'win%':>8}{'roi':>9}{'CI_low':>8}"
           f"{'disc':>8}{'valid':>8}{'latest':>8}{'cover':>8}  leakage")
    extra.append(hdr)
    rows = final_table(bets, cov, frozen, trails)
    for key, label, m, disc, val, lat, tr, el, lo, hi, leak in rows:
        cov_pct = f"{(tr / el * 100):.1f}%" if el else "n/a"
        extra.append(
            f"  {key:<5}{m['n']:>6}{fmt_pct(m['win_pct']):>8}{fmt_pct(m['roi']):>9}"
            f"{fmt_pct(lo):>8}"
            f"{fmt_pct(disc['win_pct']) if disc else 'n/a':>8}"
            f"{fmt_pct(val['win_pct']) if val else 'n/a':>8}"
            f"{fmt_pct(lat['win_pct']) if lat else 'n/a':>8}{cov_pct:>8}  {leak}")
    bm = money(baseline)
    extra.append(f"  {'BASE':<5}{bm['n']:>6}{fmt_pct(bm['win_pct']):>8}"
                 f"{fmt_pct(bm['roi']):>9}{'':>8}"
                 f"{fmt_pct(money(slice_of(baseline, split['t0'], split['disc_end']))['win_pct']):>8}"
                 f"{fmt_pct(money(slice_of(baseline, split['disc_end'], split['val_end']))['win_pct']):>8}"
                 f"{fmt_pct(money(slice_of(baseline, split['val_end'], split['t1'] + 1))['win_pct']):>8}"
                 f"{'100.0%':>8}  control (no condition)")
    extra.append("")

    # executive
    ex = []
    ex.append("=" * 100)
    ex.append("EXECUTIVE RESULT")
    ex.append("=" * 100)
    ge55 = [k for k, _l in CAND_LABELS if money(bets[k])["win_pct"] is not None
            and money(bets[k])["win_pct"] > 0.55]
    geb = [k for k, _l in CAND_LABELS if money(bets[k])["win_pct"] is not None
           and money(bets[k])["win_pct"] > BREAK_EVEN]
    part = {}
    for key in ("disc", "val", "lat"):
        lo_t, hi_t = {
            "disc": (split["t0"], split["disc_end"]),
            "val": (split["disc_end"], split["val_end"]),
            "lat": (split["val_end"], split["t1"] + 1)}[key]
        part[key] = [k for k, _l in CAND_LABELS
                     if (money([b for b in bets[k] if lo_t <= b["t"] < hi_t])
                         ["win_pct"] or 0) > 0.55]
    ci_above = []
    for k, _l in CAND_LABELS:
        m = money(bets[k])
        lo, _hi = wilson(m["wins"], m["n"])
        if lo is not None and lo > BREAK_EVEN:
            ci_above.append(k)
    ex.append(f"  1. Candidates >55% overall:            {', '.join(ge55) or 'NONE'}")
    ex.append(f"  2. Candidates >54.054% (break-even):   {', '.join(geb) or 'NONE'}")
    ex.append(f"  3. >55% in VALIDATION:                 {', '.join(part['val']) or 'NONE'}")
    ex.append(f"  4. >55% in LATEST OOS:                 {', '.join(part['lat']) or 'NONE'}")
    ex.append(f"  5. CI entirely above break-even:       {', '.join(ci_above) or 'NONE'}")
    ex.append(f"  6. discovery >55% (in-sample):         {', '.join(part['disc']) or 'NONE'}")
    ex.append("")
    ex.append("  NB: '>55%' is a descriptive bar; the ECONOMIC bar at 1.85 is")
    ex.append("  54.054%.  A candidate can clear 55% in the aggregate and still")
    ex.append("  fail to persist out of sample — see the partition columns.")
    ex.append("")

    # ---- §12 factual determination ----
    cov_of = {k: (len(cov[k]["trig"]) / len(cov[k]["elig"]) if cov[k]["elig"] else 0.0)
              for k, _l in CAND_LABELS}
    low_cov = [k for k, _l in CAND_LABELS
               if money(bets[k])["n"] < 100 or cov_of[k] < 0.05]
    gebo = [k for k, _l in CAND_LABELS
            if (money([b for b in bets[k] if b["t"] >= split["val_end"]])
                ["win_pct"] or 0) > BREAK_EVEN]
    persistent = [k for k, _l in CAND_LABELS
                  if k in part["val"] and k in part["lat"]]
    base_by_block = [money(slice_of(baseline, a, b))["win_pct"]
                     for a, b in ((split["t0"], split["b1"]),
                                  (split["b1"], split["b2"]),
                                  (split["b2"], split["t1"] + 1))]
    ex.append("§12  FACTUAL DETERMINATION")
    ex.append(f"  1. observed win rate >55% overall:   {', '.join(ge55) or 'NONE'}")
    ex.append(f"  2. remain >55% in validation:        {', '.join(part['val']) or 'NONE'}")
    ex.append(f"  3. remain >55% in latest OOS:        {', '.join(part['lat']) or 'NONE'}")
    ex.append(f"  4. exceed 54.054% overall:           {', '.join(geb) or 'NONE'}")
    ex.append(f"     exceed 54.054% in latest OOS:     {', '.join(gebo) or 'NONE'}")
    ex.append(f"  5. coverage <5% or <100 bets (LOW):  {', '.join(low_cov) or 'NONE'}")
    ex.append(f"  6. 95% CI entirely above 54.054%:    {', '.join(ci_above) or 'NONE'}")
    ex.append(f"  7. >55% in BOTH validation AND")
    ex.append(f"     latest OOS (persistent):          {', '.join(persistent) or 'NONE'}")
    ex.append(f"  8. baseline UNDER% by block (control): "
               f"{fmt_pct(base_by_block[0])} / {fmt_pct(base_by_block[1])} / "
               f"{fmt_pct(base_by_block[2])}")
    ex.append("     -> if the baseline also collapses in block 3, the late-period")
    ex.append("        falloff is a MARKET regime shift, not signal-specific decay.")
    ex.append("  9. production rule (A) historical edge: "
               f"{fmt_pct(money(bets['A'])['win_pct'])} overall, "
               f"{fmt_pct(money([b for b in bets['A'] if b['t'] >= split['val_end']])['win_pct'])} latest OOS")
    ex.append(" 10. trailing 3d P80/P85 vs production: "
               f"C {fmt_pct(money(bets['C'])['win_pct'])} / "
               f"D {fmt_pct(money(bets['D'])['win_pct'])} vs "
               f"A {fmt_pct(money(bets['A'])['win_pct'])} -> "
               + ("NO improvement" if (money(bets['C'])['win_pct'] or 0) <
                  (money(bets['A'])['win_pct'] or 1) else "improves"))

    text = text + "\n" + "\n".join(extra) + "\n" + "\n".join(ex) + "\n"
    with open(OUT, "w") as fh:
        fh.write(text)
    print("\n".join(extra))
    print("\n".join(ex))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
