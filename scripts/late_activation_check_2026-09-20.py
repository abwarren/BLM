#!/usr/bin/env python3
"""READ-ONLY — LATE ACTIVATION CHECK (user observation, 2026-09-20).

The LIVE 75% alert (blm_v4/api.py -> under_alert_state) is re-evaluated on
every /live poll from the CURRENT observation's required_pts_per_min, so a
game that fails the condition at its FIRST >=75% crossing can still activate
LATER in the game.  The audit mirror (audit_alert_improvement_2026-09-18)
evaluates ONLY the first crossing — late activations were counted as
baseline.  This script measures:

  A. first-crossing trigger only (the old mirror view)
  B. live-mirror: the game activates if ANY >=75% non-terminal VALID
     observation satisfies req > league_avg*1.04 (point-in-time mean at that
     observation); the BET MOMENT is the FIRST satisfying observation
  C. the delta population: late activations only
  D. late activations by progress band at activation + winrate/ROI vs the
     frozen 75%-boundary line (the line the dashboard settles against)
  E. daily split of late activations since 09-15

Q3_BREAK is NOT re-evaluated here: its inputs (boundary line AND boundary
score) are frozen at the crossing, so it cannot activate late — only the
projector condition can.  Both DBs mode=ro; the only file written is the
report.
"""
from __future__ import annotations

import importlib.util
import sys

sys.path.insert(0, "/home/ubuntu/BLM")

PROD = "/home/ubuntu/BLM/blm_pokerbet.db"
CLEAN_DB = "/home/ubuntu/BLM/blm_metrics_clean.db"
OUT = "/home/ubuntu/BLM/analysis_late_activation_check_2026-09-20.txt"
MARGIN = 1.04

_spec = importlib.util.spec_from_file_location(
    "q3_audit",
    "/home/ubuntu/BLM/scripts/audit_alert_winrate_q3break_daily_2026-09-20.py")
_q3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_q3)

_audit = _q3._audit
out, REPORT = _q3.out, _q3.REPORT
del REPORT[:]


def main():
    con_p, con_c = _audit.ro(PROD), _audit.ro(CLEAN_DB)
    settled, invalid, league = _audit.load_universe(con_p, con_c)
    sigs = _audit.load_signals(con_p, con_c, settled, invalid, league)

    # EVERY >=75% observation per game (the mirror's load_signals keeps only
    # the first crossing per tier; we need the full tail to mirror /live)
    rows = con_c.execute(
        """SELECT source_game_id, progress_pct, actual_pts_per_min,
                  required_pts_per_min, live_total_line, captured_at,
                  market_age_seconds, market_status, terminal, status
             FROM clean_projections
            WHERE progress_pct >= 75 AND progress_pct < 100
              AND (terminal IS NULL OR terminal = 0) AND status = 'VALID'
              AND actual_pts_per_min IS NOT NULL
              AND required_pts_per_min IS NOT NULL
            ORDER BY source_game_id, captured_at""")
    obs_by_game = {}
    for r in rows:
        obs_by_game.setdefault(r["source_game_id"].split("#")[0],
                               []).append(dict(r))

    live, late, first_only, never = [], [], [], []
    for s in sigs:
        if s["tier"] != 75:
            continue
        obs = [o for o in obs_by_game.get(s["gid"], [])
               if _audit.epoch_of(o["captured_at"]) is not None
               and _audit.epoch_of(o["captured_at"]) < _audit.epoch_of(settled[s["gid"]]["ra"])]
        if not obs:
            never.append(s)
            continue
        # point-in-time league mean at EACH observation (strict before)
        def fires(o):
            t = _audit.epoch_of(o["captured_at"])
            avg = league[s["slug"]].avg_before(t)
            return avg is not None and o["required_pts_per_min"] > avg * MARGIN
        first_obs = obs[0]
        if fires(first_obs):
            first_only.append(s)
            live.append(s)
            continue
        late_obs = next((o for o in obs[1:] if fires(o)), None)
        if late_obs is None:
            never.append(s)
            continue
        t = _audit.epoch_of(late_obs["captured_at"])
        st = dict(s)
        st.update({
            "act_t": t, "act_prog": late_obs["progress_pct"],
            "act_stale": (late_obs["market_status"] == "STALE"
                          or (late_obs["market_age_seconds"] is not None
                              and late_obs["market_age_seconds"] > 300.0)),
            "act_gap": late_obs["required_pts_per_min"] - s["req"],
        })
        late.append(st)
        live.append(st)

    out("=" * 96)
    out("READ-ONLY — LATE ACTIVATION OF THE LIVE 75% ALERT (user observation), 2026-09-20")
    out("live mirror: activation = ANY >=75% VALID non-terminal observation with")
    out("req > league_avg*1.04 (point-in-time mean AT that observation); bet moment = first")
    out("satisfying observation; settled vs the FROZEN 75%-boundary line, OK finals only.")
    out("Q3_BREAK cannot activate late (frozen line AND score) — not re-tested here.")
    out("=" * 96)

    out("")
    out("§1 THE THREE POPULATIONS (one row per game, 75% tier)")
    out("-" * 96)
    _q3.row("A first-crossing trigger only", first_only)
    _q3.row("B late activation ONLY", late)
    _q3.row("B+A live mirror (any obs)", live)
    _q3.row("never fires (true baseline)", never)
    out("")
    out("§1b CLEAN (line fresh at the bet-moment observation)")
    out("-" * 96)
    _q3.row("A first-crossing CLEAN", [s for s in first_only if not s["stale"]])
    _q3.row("B late-activation CLEAN", [s for s in late if not s["act_stale"]])
    _q3.row("B+A live mirror CLEAN", [s for s in live
                                      if not (s.get("act_stale") or s["stale"])])
    out("")
    out("§2 LATE ACTIVATIONS BY PROGRESS AT ACTIVATION (ALL | settled vs frozen line)")
    out("-" * 96)
    for lo_, hi_ in [(75, 80), (80, 85), (85, 90), (90, 100)]:
        g = [s for s in late if lo_ <= (s["act_prog"] or 0) < hi_]
        _q3.row(f"activated at [{lo_},{hi_})", g)
    out("")
    out("§2b How late in GAME time did they fire (progress delta past first crossing)?")
    for lo_, hi_ in [(0, 1), (1, 5), (5, 10), (10, 25)]:
        g = [s for s in late
             if lo_ <= ((s["act_prog"] or 0) - (s["prog"] or 0)) < hi_]
        _q3.row(f"delta progress [{lo_},{hi_})", g)
    out("")
    out("§3 DAILY — late activations (ALL), since 09-15")
    out("-" * 96)
    tot = []
    for d in ["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18",
              "2026-09-19", "2026-09-20"]:
        lo = _audit.epoch_of(d + "T00:00:00Z")
        seg = [s for s in late if lo <= s["act_t"] < lo + 86400]
        tot += seg
        e = _q3.econ(seg)
        rate = f"{e['rate']:>6.2f}%" if e["rate"] is not None else "  n/a  "
        net = f"{e['net']:+7.2f}u" if e["n"] else "       -"
        out(f"  {d}  N={e['n']:>4}  U={e['u']:>3}  O={e['o']:>3}  WIN%={rate}  {net}")
    e = _q3.econ(tot)
    out(f"  {'TOTAL':<12} N={e['n']:>4}  U={e['u']:>3}  O={e['o']:>3}  "
        f"WIN%={e['rate']:>6.2f}%  net={e['net']:+7.2f}u  "
        f"ROI={('%+.2f%%' % e['roi']) if e['roi'] is not None else 'n/a'}")
    out("")
    out("§4 VERDICT INPUTS")
    out("-" * 96)
    # honest per-game activation persistence: how many obs fire per late game
    multi = 0
    for s in late:
        obs = obs_by_game.get(s["gid"], [])
        k = 0
        for o in obs[1:]:
            t = _audit.epoch_of(o["captured_at"])
            avg = league[s["slug"]].avg_before(t)
            if avg is not None and o["required_pts_per_min"] > avg * MARGIN:
                k += 1
        if k >= 2:
            multi += 1
    out(f"  late-activation games with >=2 firing observations (sustained, not a blip): "
        f"{multi}/{len(late)}")
    out(f"  mean required-pace gap gained between first crossing and activation: "
        f"{sum(s['act_gap'] for s in late)/len(late):+.3f} pts/min" if late else
        "  no late activations")
    out("")
    out("READ-ONLY: both DBs opened mode=ro; the only file written is this report.")
    con_p.close()
    con_c.close()
    with open(OUT, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    print(f"\nwritten: {OUT}")


if __name__ == "__main__":
    main()
