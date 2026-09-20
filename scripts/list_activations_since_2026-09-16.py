#!/usr/bin/env python3
"""READ-ONLY — EVERY ALERT ACTIVATION SINCE 2026-09-16 (UTC), 2026-09-20.

The production (level-triggered) definition, per identity:

  75% projector : activation = the FIRST >=75% VALID non-terminal observation
                  whose required_pts_per_min > league_avg*1.04 (point-in-time
                  mean at THAT observation), on or after 09-16.  Re-firing
                  observations of the same game are the SAME bet (the settle
                  line is the frozen 75%-boundary line) — counted, listed in
                  n_fire, never double-settled.
  Q3_BREAK      : activation = boundary condition (line - score)/qmin >
                  league_avg*1.04 at the game's first >=75% observation,
                  boundary on or after 09-16 (frozen inputs; one instant).

Every activation settles vs its FROZEN trigger_observation line; OK finals
only.  Both DBs mode=ro; outputs: the txt report and a CSV of every row.
"""
from __future__ import annotations

import csv
import importlib.util
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/home/ubuntu/BLM")

PROD = "/home/ubuntu/BLM/blm_pokerbet.db"
CLEAN_DB = "/home/ubuntu/BLM/blm_metrics_clean.db"
OUT = "/home/ubuntu/BLM/analysis_activations_since_2026-09-16.txt"
CSV_OUT = "/home/ubuntu/BLM/activations_since_2026-09-16.csv"
SINCE = "2026-09-16T00:00:00Z"
MARGIN = 1.04

_spec = importlib.util.spec_from_file_location(
    "q3_audit",
    "/home/ubuntu/BLM/scripts/audit_alert_winrate_q3break_daily_2026-09-20.py")
_q3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_q3)
_audit = _q3._audit
out, REPORT = _q3.out, _q3.REPORT
del REPORT[:]


def iso_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def iso_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S")


def main():
    from blm_v4.live_analytics.under_outcome import (
        score_at_observation, trigger_observation)
    from blm_v4.projection import duration_for

    con_p, con_c = _audit.ro(PROD), _audit.ro(CLEAN_DB)
    settled, invalid, league = _audit.load_universe(con_p, con_c)
    sigs = _audit.load_signals(con_p, con_c, settled, invalid, league)
    t0 = _audit.epoch_of(SINCE)

    snaps = {}
    for r in con_p.execute(
            """SELECT source_game_id, captured_at, total_line, quarter,
                      clock, period_label, game_status, home_score, away_score
                 FROM snapshots
                ORDER BY source_game_id, captured_at, id"""):
        snaps.setdefault(r["source_game_id"], []).append(dict(r))

    # full >=75% observation tail per game (the level-triggered feed)
    obs_by_game = {}
    for r in con_c.execute(
            """SELECT source_game_id, progress_pct, actual_pts_per_min,
                      required_pts_per_min, live_total_line, captured_at,
                      market_age_seconds, market_status, terminal, status
                 FROM clean_projections
                WHERE progress_pct >= 75 AND progress_pct < 100
                  AND (terminal IS NULL OR terminal = 0) AND status = 'VALID'
                  AND actual_pts_per_min IS NOT NULL
                  AND required_pts_per_min IS NOT NULL
                ORDER BY source_game_id, captured_at"""):
        obs_by_game.setdefault(r["source_game_id"].split("#")[0],
                               []).append(dict(r))

    rows_out = []
    for s in sigs:
        if s["tier"] != 75:
            continue
        base = settled.get(s["gid"])
        if not base:
            continue
        ra = _audit.epoch_of(base["ra"])
        obs = [o for o in obs_by_game.get(s["gid"], [])
               if (t := _audit.epoch_of(o["captured_at"])) is not None
               and t < ra]
        if not obs:
            continue
        # frozen settle line (the 75% boundary authority)
        srows = snaps.get(s["gid"]) or []
        trig = trigger_observation(srows, 75, base["cls"]) if srows else {}
        line = _q3.fin(trig.get("total_line"))
        if line is None:
            continue
        outcome = ("UNDER" if base["ft"] < line
                   else "OVER" if base["ft"] > line else "PUSH")

        # ── Q3_BREAK: boundary instant IS the first >=75% observation ──
        b = obs[0]
        bt = _audit.epoch_of(b["captured_at"])
        score = _q3.fin(score_at_observation(srows, 75, base["cls"])) if srows else None
        qmin = _q3.fin(duration_for(base["cls"])[0])
        bavg = league[s["slug"]].avg_before(bt)
        if (score is not None and qmin and qmin > 0 and bavg is not None):
            q3req = (line - score) / qmin
            if q3req > bavg * MARGIN and bt >= t0:
                stale = (b["market_status"] == "STALE"
                         or (b["market_age_seconds"] is not None
                             and b["market_age_seconds"] > 300.0))
                rows_out.append({
                    "date": iso_day(bt), "time_utc": iso_time(bt),
                    "game_id": s["gid"], "identity": "Q3_BREAK",
                    "league": s["league"], "activation_progress": round(b["progress_pct"], 2),
                    "trigger_line": line, "q3_score": int(score),
                    "required": round(q3req, 3), "league_avg": round(bavg, 3),
                    "final_total": int(base["ft"]), "outcome": outcome,
                    "line_state": "stale" if stale else "clean",
                    "n_fire": 1,
                })

        # ── 75% projector: first firing observation on/after 09-16 ──
        fires = []
        for o in obs:
            t = _audit.epoch_of(o["captured_at"])
            avg = league[s["slug"]].avg_before(t)
            if avg is not None and o["required_pts_per_min"] > avg * MARGIN:
                fires.append((t, o, avg))
        if not fires:
            continue
        ft_, fo, favg = fires[0]
        if ft_ >= t0:
            stale = (fo["market_status"] == "STALE"
                     or (fo["market_age_seconds"] is not None
                         and fo["market_age_seconds"] > 300.0))
            rows_out.append({
                "date": iso_day(ft_), "time_utc": iso_time(ft_),
                "game_id": s["gid"], "identity": "75%",
                "league": s["league"],
                "activation_progress": round(fo["progress_pct"], 2),
                "trigger_line": line,
                "q3_score": int(score) if score is not None else "",
                "required": round(fo["required_pts_per_min"], 3),
                "league_avg": round(favg, 3),
                "final_total": int(base["ft"]), "outcome": outcome,
                "line_state": "stale" if stale else "clean",
                "n_fire": len(fires),
            })

    rows_out.sort(key=lambda r: (r["date"], r["time_utc"], r["identity"]))

    out("=" * 112)
    out("READ-ONLY — EVERY ALERT ACTIVATION SINCE 2026-09-16 (UTC), run 2026-09-20")
    out("75% = first firing observation (level-triggered, point-in-time mean); Q3_BREAK = boundary instant.")
    out("Both settle vs the FROZEN 75%-boundary line; n_fire = firing observations of the same game (same bet).")
    out("=" * 112)

    for ident in ("75%", "Q3_BREAK"):
        sel = [r for r in rows_out if r["identity"] == ident]
        out("")
        out(f"§ {ident} — {len(sel)} activations since 09-16")
        out("-" * 112)
        hdr = (f"{'date':<11}{'time':>9}  {'game':>9}  {'league':<10}"
               f"{'prog':>6}  {'line':>6} {'req':>6}{'avg':>7}  "
               f"{'final':>5}  {'result':<6}{'line':<7}{'fire':>5}")
        out(hdr)
        day_tot = {}
        for r in sel:
            out(f"{r['date']:<11}{r['time_utc']:>9}  {r['game_id']:>9}  "
                f"{r['league']:<10}{r['activation_progress']:>6.1f}  "
                f"{r['trigger_line']:>6.1f} {r['required']:>6.2f}"
                f"{r['league_avg']:>7.3f}  {r['final_total']:>5}  "
                f"{r['outcome']:<6}{r['line_state']:<7}{r['n_fire']:>5}")
            d = day_tot.setdefault(r["date"], {"U": 0, "O": 0, "P": 0, "n": 0})
            d["n"] += 1
            d[r["outcome"][0]] += 1
        out("")
        for d in sorted(day_tot):
            v = day_tot[d]
            wr = (100.0 * v["U"] / v["n"]) if v["n"] else 0.0
            out(f"  {d}: N={v['n']:>4}  UNDER={v['U']:>3}  OVER={v['O']:>3}  "
                f"winrate={wr:.1f}%")
        v_n = sum(v["n"] for v in day_tot.values())
        v_u = sum(v["U"] for v in day_tot.values())
        if v_n:
            out(f"  TOTAL: N={v_n}  UNDER={v_u}  winrate={100.0*v_u/v_n:.2f}%")

    out("")
    out(f"rows written: {len(rows_out)} -> {OUT}")
    out("READ-ONLY: both DBs opened mode=ro; only the report and CSV were written.")

    con_p.close()
    con_c.close()
    with open(OUT, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    with open(CSV_OUT, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(rows_out)
    print(f"\nwritten: {OUT}\nwritten: {CSV_OUT} ({len(rows_out)} rows)")


if __name__ == "__main__":
    main()
