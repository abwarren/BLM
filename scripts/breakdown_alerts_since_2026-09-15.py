#!/usr/bin/env python3
"""READ-ONLY breakdown — BLM UNDER alerts SINCE 2026-09-15 (UTC).

Slices the production-mirror cohorts of
audit_alert_winrate_q3break_daily_2026-09-20.py to trigger time
>= 2026-09-14T00:00:00Z: the 75% projector alert (rule since 09-14) and the
Q3_BREAK alert (shipped 09-18).  Adds a ONE-BET-PER-GAME union view (a game
firing both identities at the same boundary is ONE stake, settled vs the
Q3_BREAK frozen line) — never the naive sum of both cohorts.

Both DBs opened mode=ro; the only file written is this report.
"""
from __future__ import annotations

import importlib.util
import sys

sys.path.insert(0, "/home/ubuntu/BLM")

PROD = "/home/ubuntu/BLM/blm_pokerbet.db"
CLEAN = "/home/ubuntu/BLM/blm_metrics_clean.db"
OUT = "/home/ubuntu/BLM/analysis_alert_breakdown_since_2026-09-15.txt"
SINCE = "2026-09-15T00:00:00Z"

_spec = importlib.util.spec_from_file_location(
    "q3_audit",
    "/home/ubuntu/BLM/scripts/audit_alert_winrate_q3break_daily_2026-09-20.py")
_q3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_q3)          # module-level defs only; main() is guarded

_audit = _q3._audit
out = _q3.out
REPORT = _q3.REPORT
del REPORT[:]


def main():
    con_p, con_c = _audit.ro(PROD), _audit.ro(CLEAN)
    settled, invalid, league = _audit.load_universe(con_p, con_c)
    sigs = _audit.load_signals(con_p, con_c, settled, invalid, league)

    snaps_by_gid = {}
    for r in con_p.execute(
            """SELECT source_game_id, captured_at, total_line, quarter, clock,
                      period_label, game_status, home_score, away_score
                 FROM snapshots
                ORDER BY source_game_id, captured_at, id"""):
        snaps_by_gid.setdefault(r["source_game_id"], []).append(dict(r))

    from blm_v4.live_analytics.under_outcome import (
        score_at_observation, trigger_observation)
    from blm_v4.projection import duration_for

    for s in sigs:
        s["trigger"] = ((s["act"] < s["avg"])
                        and (s["req"] > s["avg"] * 1.04))

    t0 = _audit.epoch_of(SINCE)
    q3_records = []
    for s in sigs:
        if s["tier"] != 75:
            continue
        srows = snaps_by_gid.get(s["gid"])
        cls = (settled.get(s["gid"]) or {}).get("cls")
        if not srows or not cls:
            continue
        trig = trigger_observation(srows, 75, cls)
        line = _q3.fin(trig.get("total_line"))
        score = _q3.fin(score_at_observation(srows, 75, cls))
        qmin = _q3.fin(duration_for(cls)[0])
        if line is None or score is None or not qmin or qmin <= 0:
            continue
        rec = dict(s)
        rec["outcome"] = ("under" if s["ft"] < line
                          else "over" if s["ft"] > line else "push")
        rec["trigger"] = ((line - score) / qmin) > s["avg"] * 1.04
        q3_records.append(rec)

    trig75 = [s for s in sigs if s["tier"] == 75 and s["trigger"]
              and s["t"] >= t0]
    base75 = [s for s in sigs if s["tier"] == 75 and s["t"] >= t0]
    clean75 = [s for s in trig75 if not s["stale"]]
    q3_all = [s for s in q3_records if s["trigger"] and s["t"] >= t0]
    q3_clean = [s for s in q3_all if not s["stale"]]
    q3_base = [s for s in q3_records if s["t"] >= t0]

    out("=" * 96)
    out("READ-ONLY BREAKDOWN — BLM UNDER ALERTS SINCE 2026-09-15 (UTC), run 2026-09-20")
    out("cohorts mirror audit_alert_winrate_q3break_daily_2026-09-20 (production triggers,")
    out("point-in-time league mean, frozen trigger line, OK-settled finals). ODDS=1.85.")
    out("=" * 96)

    out("")
    out("§1 AGGREGATE SINCE 09-14 (CLEAN = fresh line <=300s)")
    out("-" * 96)
    _q3.row("75% projector ALL", trig75)
    _q3.row("75% projector CLEAN", clean75)
    _q3.row("Q3_BREAK ALL", q3_all)
    _q3.row("Q3_BREAK CLEAN", q3_clean)
    out("")
    _q3.row("baseline 75% (all obs)", base75)
    _q3.row("baseline Q3_BREAK", q3_base)

    out("")
    out("§2 ONE-BET-PER-GAME UNION — any alert fired, one stake, settled vs the")
    out("   Q3_BREAK frozen line (the two identities share the same boundary)")
    out("-" * 96)
    union, seen = [], set()
    for s in sorted(q3_all + trig75, key=lambda x: x["t"]):
        if s["gid"] in seen:
            continue
        seen.add(s["gid"])
        union.append(s)
    _q3.row("union ALL (one bet/game)", union)
    _q3.row("union CLEAN", [s for s in union if not s["stale"]])

    out("")
    out("§3 PER LEAGUE SINCE 09-15 — CLEAN cohorts")
    out("-" * 96)
    for label, cohort in (("75% CLEAN", clean75), ("Q3_CLEAN", q3_clean)):
        out(f"  {label}:")
        for lg in _audit.LEAGUE_ORDER:
            recs = [s for s in cohort if s["league"] == lg]
            if recs:
                _q3.row(f"  {label} {lg}", recs)
        out("")

    out("§4 DAILY SINCE 09-15")
    out("-" * 96)
    days = ["2026-09-15", "2026-09-16", "2026-09-17",
            "2026-09-18", "2026-09-19", "2026-09-20"]
    for label, cohort in (("75% ALL", trig75), ("Q3_BREAK ALL", q3_all),
                          ("union ALL", union)):
        out(f"  {label}:")
        tot = []
        for d in days:
            lo = _audit.epoch_of(d + "T00:00:00Z")
            seg = [s for s in cohort if lo <= s["t"] < lo + 86400]
            tot += seg
            e = _q3.econ(seg)
            rate = f"{e['rate']:>6.2f}%" if e["rate"] is not None else "  n/a  "
            net = f"{e['net']:+7.2f}u" if e["n"] else "       -"
            out(f"    {d}  N={e['n']:>4}  U={e['u']:>3}  O={e['o']:>3}  "
                f"WIN%={rate}  {net}")
        e = _q3.econ(tot)
        out(f"    {'TOTAL':<12} N={e['n']:>4}  U={e['u']:>3}  O={e['o']:>3}  "
            f"WIN%={e['rate']:>6.2f}%  net={e['net']:+7.2f}u  "
            f"ROI={('%+.2f%%' % e['roi']) if e['roi'] is not None else 'n/a'}")
        out("")

    out("READ-ONLY: both DBs opened mode=ro; only this report was written.")
    con_p.close()
    con_c.close()
    with open(OUT, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    print(f"\nwritten: {OUT}")


if __name__ == "__main__":
    main()
