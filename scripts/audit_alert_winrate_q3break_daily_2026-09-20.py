#!/usr/bin/env python3
"""READ-ONLY AUDIT — alert winrate with Q3_BREAK included, ex-TBSL cut,
daily breakdown for the last week.  2026-09-20.

Reuses the production-mirror machinery of
scripts/audit_alert_improvement_2026-09-18.py (imported, not copied) so the
75% projector-condition cohorts are IDENTICAL to that audit.  Adds:

  Q3_BREAK cohort (directive 2026-09-18, blm_v4/live_analytics/under_alert.py):
    boundary = the game's FIRST observation with progress >= 75.0 (the SAME
               boundary row the 75% tier records)
    line     = trigger_observation(snapshots, 75, cls).total_line — the live
               line in force at the boundary, frozen
    score    = score_at_observation(snapshots, 75, cls) — the SAME row
    required = (line - score) / quarter_minutes   (duration_for(cls)[0])
    alert    = required > league_avg * 1.04  (STRICT, point-in-time mean)
    settle   = final_total vs frozen boundary line (under/over/push)

  The league reference is the SAME point-in-time mean (result_at STRICTLY
  before the trigger) the 75% mirror uses — the live system's own authority.
  (The 2026-09-18 Q3 backtest used the all-time mean; noted as a difference,
  not silently reconciled.)

Everything opens both DBs mode=ro.  The only file written is this report.
"""
from __future__ import annotations

import importlib.util
import math
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/home/ubuntu/BLM")
from blm_v4.live_analytics.under_outcome import (  # noqa: E402
    score_at_observation,
    trigger_observation,
)
from blm_v4.projection import duration_for  # noqa: E402

PROD = os.environ.get("BLM_PROD_DB", "/home/ubuntu/BLM/blm_pokerbet.db")
CLEAN = os.environ.get("BLM_CLEAN_DB", "/home/ubuntu/BLM/blm_metrics_clean.db")
OUT = "/home/ubuntu/BLM/analysis_alert_winrate_q3break_daily_2026-09-20.txt"

ODDS = 1.85
REQUIRED_MARGIN = 1.04
TBSL = "TBSL"

# ── reuse the production-mirror audit module (dashed filename → importlib) ──
_spec = importlib.util.spec_from_file_location(
    "audit_alert_improvement",
    "/home/ubuntu/BLM/scripts/audit_alert_improvement_2026-09-18.py")
_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_audit)

REPORT: list[str] = []


def out(s: str = "") -> None:
    REPORT.append(s)
    print(s)


def wilson(wins, n, z=1.96):
    return _audit.wilson(wins, n, z)


def fin(x):
    """Finite float or None (same semantics as under_alert._finite)."""
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def econ(recs):
    """N / under / over / push / winrate / net units / ROI at ODDS.

    Pushes stake-returned: excluded from stakes and from the win/loss
    denominators, reported separately — never silently dropped.
    """
    n = len(recs)
    u = sum(1 for r in recs if r["outcome"] == "under")
    o = sum(1 for r in recs if r["outcome"] == "over")
    p = sum(1 for r in recs if r["outcome"] == "push")
    stakes = u + o
    net = u * (ODDS - 1.0) - o
    roi = (100.0 * net / stakes) if stakes else None
    rate = (100.0 * u / n) if n else None
    lo, hi = wilson(u, n)
    return {"n": n, "u": u, "o": o, "p": p, "rate": rate, "net": net,
            "roi": roi, "lo": lo, "hi": hi}


def row(label, recs):
    e = econ(recs)
    ci = (f"[{e['lo']*100:.1f}%,{e['hi']*100:.1f}%]"
          if e["n"] and e["rate"] is not None else "-")
    roi = f"{e['roi']:+.2f}%" if e["roi"] is not None else "n/a"
    out(f"  {label:<34} N={e['n']:>5}  U={e['u']:>4}  O={e['o']:>4}  "
        f"P={e['p']:>2}  WIN%={e['rate']:>6.2f}%  {ci}  "
        f"net={e['net']:+8.2f}u  ROI={roi}")
    return e


def main():
    con_p, con_c = _audit.ro(PROD), _audit.ro(CLEAN)
    out("=" * 96)
    out("READ-ONLY AUDIT — ALERT WINRATE WITH Q3_BREAK INCLUDED + EX-TBSL + DAILY, 2026-09-20")
    out("75% mirror = audit_alert_improvement_2026-09-18 (projector required pace).")
    out("Q3_BREAK mirror = directive 2026-09-18 geometry: required=(line-Q3score)/quarter_minutes,")
    out("frozen trigger_observation line, SAME point-in-time league mean (strict-before),")
    out("settlement = game_results final_result_status='OK' only. ODDS=1.85 breakeven 54.05%.")
    out("=" * 96)

    settled, invalid, league = _audit.load_universe(con_p, con_c)
    sigs = _audit.load_signals(con_p, con_c, settled, invalid, league)
    for s in sigs:
        s["trigger"] = ((s["act"] < s["avg"])
                        and (s["req"] > s["avg"] * REQUIRED_MARGIN))

    # ── snapshots for the Q3_BREAK authorities ───────────────────────────
    snaps = defaultdict(list)
    for r in con_p.execute(
            """SELECT source_game_id, captured_at, total_line, quarter,
                      clock, period_label, game_status, home_score, away_score
                 FROM snapshots
                ORDER BY source_game_id, captured_at, id"""):
        snaps[r["source_game_id"]].append(dict(r))

    # ── Q3_BREAK cohort: same boundary row as the 75% tier signal ────────
    q3_unprovable = 0
    q3_records = []          # EVERY provable boundary game (alert or baseline)
    for s in sigs:
        if s["tier"] != 75:
            continue
        srows = snaps.get(s["gid"])
        cls = (settled.get(s["gid"]) or {}).get("cls")
        if not srows or not cls:
            q3_unprovable += 1
            continue
        trig = trigger_observation(srows, 75, cls)
        line = fin(trig.get("total_line"))
        score = fin(score_at_observation(srows, 75, cls))
        qmin = fin(duration_for(cls)[0])
        if line is None or score is None or not qmin or qmin <= 0:
            q3_unprovable += 1          # live rule fails closed here too
            continue
        required = (line - score) / qmin
        rec = dict(s)
        rec.update({"q3_line": line, "q3_score": score, "q3_req": required,
                    "outcome": ("under" if s["ft"] < line
                                else "over" if s["ft"] > line else "push")})
        rec["trigger"] = required > s["avg"] * REQUIRED_MARGIN
        q3_records.append(rec)

    # cohorts — the 75% mirror and Q3_BREAK kept as separate identities
    trig75 = [s for s in sigs if s["tier"] == 75 and s["trigger"]]
    base75 = [s for s in sigs if s["tier"] == 75]
    clean75 = [s for s in trig75 if not s["stale"]]
    q3_all = [s for s in q3_records if s["trigger"]]
    q3_clean = [s for s in q3_all if not s["stale"]]
    q3_base = q3_records

    out("")
    out(f"Q3_BREAK boundary games with provable line+score: {len(q3_records)}"
        f"   (unprovable / fail-closed: {q3_unprovable})")
    out("")
    out("§1 HEADLINE WINRATE — both alert identities (ALL and CLEAN = fresh line <=300s)")
    out("-" * 96)
    row("75% projector ALL", trig75)
    row("75% projector CLEAN", clean75)
    row("Q3_BREAK ALL", q3_all)
    row("Q3_BREAK CLEAN", q3_clean)
    out("")
    out("§1b Baselines (same boundary universe, NO condition)")
    row("baseline 75% (all obs)", base75)
    row("baseline Q3_BREAK (boundary)", q3_base)
    out("")
    out("§2 OVERLAP — a game can fire BOTH identities at the same boundary; never summed")
    out("-" * 96)
    set75 = {s["gid"] for s in trig75}
    both = [s for s in q3_all if s["gid"] in set75]
    q3_only = [s for s in q3_all if s["gid"] not in set75]
    p_only = [s for s in trig75 if s["gid"] not in {t["gid"] for t in q3_all}]
    row("both identities fire", both)
    row("Q3_BREAK only", q3_only)
    row("75% projector only", p_only)

    out("")
    out("§3 EX-TBSL IMPACT — the league cut and what it does to winrate/ROI")
    out("-" * 96)
    for label, cohort in (("75% projector ALL", trig75),
                          ("75% projector CLEAN", clean75),
                          ("Q3_BREAK ALL", q3_all),
                          ("Q3_BREAK CLEAN", q3_clean)):
        with_, without = cohort, [s for s in cohort if s["league"] != TBSL]
        e1 = row(f"{label} — with TBSL", with_)
        e2 = row(f"{label} — ex-TBSL", without)
        if e1["rate"] is not None and e2["rate"] is not None:
            d_roi = ((e2["roi"] - e1["roi"])
                     if e1["roi"] is not None and e2["roi"] is not None else None)
            out(f"      -> TBSL share={100.0*len(with_ and [s for s in with_ if s['league']==TBSL])/len(with_):.1f}% of cohort; "
                f"winrate {e2['rate']-e1['rate']:+.2f}pp ex-TBSL; "
                f"ROI {('%+.2f' % d_roi) if d_roi is not None else 'n/a'}pp ex-TBSL")
        out("")
    out("§3b Per-league contribution (Q3_BREAK CLEAN)")
    for lg in _audit.LEAGUE_ORDER:
        recs = [s for s in q3_clean if s["league"] == lg]
        if recs:
            row(f"Q3_CLEAN league {lg}", recs)
    out("")

    out("§4 DAILY BREAKDOWN — last week, 2026-09-13 .. 2026-09-20 (UTC)")
    out("-" * 96)
    days = ["2026-09-13", "2026-09-14", "2026-09-15", "2026-09-16",
            "2026-09-17", "2026-09-18", "2026-09-19", "2026-09-20"]
    cohorts = (("75% ALL", trig75), ("75% CLEAN", clean75),
               ("Q3_BREAK ALL", q3_all), ("Q3_BREAK CLEAN", q3_clean))
    for label, cohort in cohorts:
        out(f"  {label}:")
        tot = []
        for d in days:
            lo, hi = (_audit.epoch_of(d + "T00:00:00Z"),
                      _audit.epoch_of(d + "T00:00:00Z") + 86400)
            seg = [s for s in cohort if lo <= s["t"] < hi]
            tot += seg
            e = econ(seg)
            rate = f"{e['rate']:>6.2f}%" if e["rate"] is not None else "  n/a  "
            net = f"{e['net']:+7.2f}u" if e["n"] else "       -"
            out(f"    {d}  N={e['n']:>4}  U={e['u']:>3}  O={e['o']:>3}  "
                f"P={e['p']:>2}  WIN%={rate}  {net}")
        e = econ(tot)
        ci = (f"[{e['lo']*100:.1f}%,{e['hi']*100:.1f}%]" if e["n"] else "-")
        roi = f"{e['roi']:+.2f}%" if e["roi"] is not None else "n/a"
        out(f"    {'WEEK TOTAL':<12} N={e['n']:>4}  U={e['u']:>3}  O={e['o']:>3}  "
            f"P={e['p']:>2}  WIN%={e['rate']:>6.2f}%  {ci}  "
            f"net={e['net']:+7.2f}u  ROI={roi}")
        out("")

    out("§5 METHOD NOTES")
    out("-" * 96)
    out("  - 75% cohorts: identical to audit_alert_improvement_2026-09-18 (imported, not copied).")
    out("  - Q3_BREAK: line+score from the production trigger_observation /")
    out("    score_at_observation authorities over blm_pokerbet.snapshots; required pace")
    out("    is market-implied ((line-score)/q_min), NOT the projector's number.")
    out("  - Q3_BREAK league mean is POINT-IN-TIME (strict before trigger) like the 75%")
    out("    mirror; the 2026-09-18 Q3 backtest used the all-time mean — expect small")
    out("    differences from that report for this reason alone.")
    out("  - CLEAN = boundary row line fresh (market_status LIVE, age <=300s).")
    out("  - PUSH settles are stake-returned: excluded from ROI stakes, counted honestly.")
    out("  - READ-ONLY: both DBs opened mode=ro; the only file written is this report.")

    con_p.close()
    con_c.close()
    with open(OUT, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    print(f"\nwritten: {OUT}")


if __name__ == "__main__":
    main()
