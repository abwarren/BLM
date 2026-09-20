#!/usr/bin/env python3
"""READ-ONLY AUDIT — GAMES SETTLING 1-2 POINTS FROM THE TRIGGER LINE.

User hypothesis (2026-09-20): the frontend may have recorded the SECOND
alert (the re-fired activation) instead of the first, which would bias the
resulted verdicts.  This audit settles every 75%-tier game whose final lands
within 2 points of the frozen trigger line and shows, for each one, BOTH
conventions on the RAW data:

  frozen-first : the first >=75% observation's line (the production settle
                 authority — what the audit mirror used)
  last-fired   : the line in force at the LAST firing observation of the
                 game (the "2nd alert" hypothesis)
  final line   : the last line observed before the game ended (pure market
                 hindsight, no alert involved)

A game whose verdict DIFFERS between frozen-first and last-fired is exactly
the population where a "which alert got recorded" ambiguity would flip
results — the flip rate IS the maximum possible bias.  Also recomputes
winrate under both conventions (firing observations only; identical for
pushes).  Both DBs mode=ro; nothing written but the report.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys

sys.path.insert(0, "/home/ubuntu/BLM")

PROD = "/home/ubuntu/BLM/blm_pokerbet.db"
CLEAN_DB = "/home/ubuntu/BLM/blm_metrics_clean.db"
OUT = "/home/ubuntu/BLM/analysis_close_settlement_bias_2026-09-20.txt"
MARGIN = 1.04

_spec = importlib.util.spec_from_file_location(
    "q3_audit",
    "/home/ubuntu/BLM/scripts/audit_alert_winrate_q3break_daily_2026-09-20.py")
_q3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_q3)
_audit = _q3._audit
out, REPORT = _q3.out, _q3.REPORT
del REPORT[:]


def verdict(ft, line):
    if line is None or ft is None:
        return None
    return "UNDER" if ft < line else "OVER" if ft > line else "PUSH"


def main():
    from blm_v4.live_analytics.under_outcome import trigger_observation

    con_p, con_c = _audit.ro(PROD), _audit.ro(CLEAN_DB)
    settled, invalid, league = _audit.load_universe(con_p, con_c)
    sigs = _audit.load_signals(con_p, con_c, settled, invalid, league)

    # full >=75% tail per game + the snapshot series for last-line reading
    obs_by_game = {}
    for r in con_c.execute(
            """SELECT source_game_id, progress_pct, required_pts_per_min,
                      live_total_line, captured_at, market_age_seconds,
                      market_status, terminal, status
                 FROM clean_projections
                WHERE progress_pct >= 75 AND progress_pct < 100
                  AND (terminal IS NULL OR terminal = 0) AND status = 'VALID'
                  AND required_pts_per_min IS NOT NULL
                ORDER BY source_game_id, captured_at"""):
        obs_by_game.setdefault(r["source_game_id"].split("#")[0],
                               []).append(dict(r))

    snaps = {}
    for r in con_p.execute(
            """SELECT source_game_id, captured_at, total_line, quarter,
                      clock, period_label, game_status, home_score, away_score
                 FROM snapshots
                ORDER BY source_game_id, captured_at, id"""):
        snaps.setdefault(r["source_game_id"], []).append(dict(r))

    rows = []
    n_firing = 0
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
        srows = snaps.get(s["gid"]) or []
        trig = trigger_observation(srows, 75, base["cls"]) if srows else {}
        frozen = _q3.fin(trig.get("total_line"))
        if frozen is None:
            continue

        # firing observations (production condition, point-in-time mean)
        firing = []
        for o in obs:
            t = _audit.epoch_of(o["captured_at"])
            avg = league[s["slug"]].avg_before(t)
            if avg is not None and o["required_pts_per_min"] > avg * MARGIN:
                firing.append((t, o))
        if not firing:
            continue
        n_firing += 1
        t_first, o_first = firing[0]
        t_last, o_last = firing[-1]

        # last line observed before the result (hindsight view)
        pre = [x for x in srows
               if (x.get("total_line") is not None
                   and (tt := _audit.epoch_of(x.get("captured_at"))) is not None
                   and tt < ra)]
        final_line = _q3.fin(pre[-1]["total_line"]) if pre else None

        v_frozen = verdict(base["ft"], frozen)
        v_lastfire = verdict(base["ft"], _q3.fin(o_last["live_total_line"]))
        v_finalline = verdict(base["ft"], final_line)
        gap = abs(base["ft"] - frozen)
        if gap <= 2.0:
            rows.append({
                "gid": s["gid"], "league": s["league"], "t1": t_first,
                "t2": t_last, "frozen": frozen, "lastfire": _q3.fin(o_last["live_total_line"]),
                "finalline": final_line, "ft": base["ft"],
                "v_frozen": v_frozen, "v_lastfire": v_lastfire,
                "v_finalline": v_finalline, "gap": gap,
                "prog_first": o_first["progress_pct"],
                "prog_last": o_last["progress_pct"],
                "clean_first": not (o_first["market_status"] == "STALE"
                                    or (o_first["market_age_seconds"] is not None
                                        and o_first["market_age_seconds"] > 300.0)),
            })

    out("=" * 116)
    out("READ-ONLY — CLOSE SETTLEMENTS (|final - frozen trigger line| <= 2) vs RECORDING CONVENTIONS, 2026-09-20")
    out("75% tier, production condition, point-in-time mean, OK finals.  frozen-first = production authority;")
    out("last-fired = line at the game's LAST firing observation ('2nd alert' hypothesis); final-line = pure market hindsight.")
    out("=" * 116)
    out("")
    out(f"75% firing games total: {n_firing}  |  "
        f"settling within 2 pts of frozen line: {len(rows)} "
        f"({100.0*len(rows)/n_firing if n_firing else 0:.1f}% of firing games)")
    close_firing = [r for r in rows if r["t1"] == r["t2"]]
    out(f"  of which single-fire games (first==last, no re-fire): {len(close_firing)}")
    out("")

    out("§1 FLIP ANALYSIS — where the recording convention changes the verdict")
    out("-" * 116)
    flips = [r for r in rows if r["v_frozen"] != r["v_lastfire"]]
    out(f"  verdict differs frozen-first vs last-fired: {len(flips)}/{len(rows)} "
        f"({100.0*len(flips)/len(rows) if rows else 0:.1f}% of close settlements)")
    pushes_frozen = sum(1 for r in rows if r["v_frozen"] == "PUSH")
    out(f"  PUSH under frozen-first: {pushes_frozen}  (half-point lines make these "
        f"arithmetically impossible; any here = data defect)")
    out("")

    out("§2 RAW ROWS — every close settlement (sorted by gap)")
    out("-" * 116)
    hdr = (f"{'game':>9}  {'league':<10}{'frz-line':>8}{'last-line':>9}"
           f"{'end-line':>8}  {'final':>5}  {'gap':>4}  "
           f"{'frozen':<6}{'lastfire':<9}{'endline':<8}{'prog1':>6}{'prog2':>6}  fire-dur")
    out(hdr)
    for r in sorted(rows, key=lambda x: (x["gap"], x["gid"])):
        dur = f"{(r['t2']-r['t1'])/60:.0f}m" if r["t2"] != r["t1"] else "single"
        out(f"{r['gid']:>9}  {r['league']:<10}{r['frozen']:>8.1f}"
            f"{('%8.1f' % r['lastfire']) if r['lastfire'] is not None else '       -'}"
            f"{('%8.1f' % r['finalline']) if r['finalline'] is not None else '       -'}  "
            f"{r['ft']:>5}  {r['gap']:>4.1f}  {r['v_frozen']:<6}"
            f"{r['v_lastfire']:<9}{r['v_finalline']:<8}"
            f"{r['prog_first']:>6.1f}{r['prog_last']:>6.1f}  {dur}")
    out("")

    out("§3 WINRATE UNDER EACH CONVENTION (ALL firing games — close subset is §2)")
    out("-" * 116)
    # recompute across every firing game for the honest comparison
    def convention_stats(key):
        u = o = p = 0
        for r in all_rows:
            v = r[key]
            if v == "UNDER":
                u += 1
            elif v == "OVER":
                o += 1
            elif v == "PUSH":
                p += 1
        n = u + o + p
        return u, o, p, n

    # rebuild verdicts for ALL firing games (the close subset is a biased
    # conditional slice — winrate there is compressed toward 50% by design)
    all_rows = []
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
        srows = snaps.get(s["gid"]) or []
        trig = trigger_observation(srows, 75, base["cls"]) if srows else {}
        frozen = _q3.fin(trig.get("total_line"))
        if frozen is None:
            continue
        firing = []
        for o in obs:
            t = _audit.epoch_of(o["captured_at"])
            avg = league[s["slug"]].avg_before(t)
            if avg is not None and o["required_pts_per_min"] > avg * MARGIN:
                firing.append((t, o))
        if not firing:
            continue
        t_last, o_last = firing[-1]
        pre = [x for x in srows
               if (x.get("total_line") is not None
                   and (tt := _audit.epoch_of(x.get("captured_at"))) is not None
                   and tt < ra)]
        final_line = _q3.fin(pre[-1]["total_line"]) if pre else None
        all_rows.append({
            "v_frozen": verdict(base["ft"], frozen),
            "v_lastfire": verdict(base["ft"], _q3.fin(o_last["live_total_line"])),
            "v_finalline": verdict(base["ft"], final_line),
        })
    for label, key in (("frozen-first (production)", "v_frozen"),
                       ("last-fired ('2nd alert')", "v_lastfire"),
                       ("end-of-game line (hindsight)", "v_finalline")):
        u, o, p, n = convention_stats(key)
        wr = (100.0 * u / n) if n else None
        stakes = u + o
        net = u * 0.85 - o
        roi = (100.0 * net / stakes) if stakes else None
        out(f"  {label:<30} N={n:>5}  UNDER={u:>4}  OVER={o:>4}  PUSH={p:>3}  "
            f"winrate={('%6.2f%%' % wr) if wr is not None else '   n/a'}  "
            f"ROI={('%+.2f%%' % roi) if roi is not None else 'n/a'}")
    out("")
    out("§4 VERDICT")
    out("-" * 116)
    out(f"  Max possible bias from recording convention = the flip count in §1 "
        f"({len(flips)} of {len(rows)} close settlements, {100.0*len(flips)/len(rows) if rows else 0:.1f}%).")
    out("  If flips are ~0, the '2nd alert recorded instead of 1st' hypothesis cannot")
    out("  change any verdict on the raw data — the settled record is convention-proof.")
    out("")
    out("READ-ONLY: both DBs opened mode=ro; the only file written is this report.")
    con_p.close()
    con_c.close()
    with open(OUT, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    print(f"\nwritten: {OUT}")


if __name__ == "__main__":
    main()
