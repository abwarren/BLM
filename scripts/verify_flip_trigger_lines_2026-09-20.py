#!/usr/bin/env python3
"""READ-ONLY — VERIFY THE FROZEN TRIGGER LINE for all flip settlements.

For every |final - frozen trigger line| <= 2 game whose verdict flips
between the frozen-first and last-fired conventions, verify the settle line
against TWO independent stores:

  A. blm_metrics_clean.db clean_projections — the first firing observation's
     live_total_line (the projector's own store)
  B. blm_pokerbet.db snapshots — the line in force at-or-before the boundary
     observation via trigger_observation (the production settle authority)

Agreement A==B means the sealed line is convention-proof.  Disagreement, a
missing store line, or a SPLIT identity (a base#iN sibling — the 30964771
defect class, where the boundary series may be truncated at the split) is
flagged per row and counted.  Both DBs mode=ro; only the report is written.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, "/home/ubuntu/BLM")

PROD = "/home/ubuntu/BLM/blm_pokerbet.db"
CLEAN_DB = "/home/ubuntu/BLM/blm_metrics_clean.db"
OUT = "/home/ubuntu/BLM/analysis_flip_line_verification_2026-09-20.txt"
MARGIN = 1.04

_spec = importlib.util.spec_from_file_location(
    "q3_audit",
    "/home/ubuntu/BLM/scripts/audit_alert_winrate_q3break_daily_2026-09-20.py")
_q3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_q3)
_audit = _q3._audit
out, REPORT = _q3.out, _q3.REPORT
del REPORT[:]

from blm_v4.live_analytics.under_outcome import trigger_observation  # noqa: E402


def main():
    con_p, con_c = _audit.ro(PROD), _audit.ro(CLEAN_DB)
    settled, invalid, league = _audit.load_universe(con_p, con_c)
    sigs = _audit.load_signals(con_p, con_c, settled, invalid, league)

    # split-identity games: bases that have a base#iN sibling in games
    split_ids = set()
    for (gid,) in con_p.execute(
            "SELECT source_game_id FROM games WHERE source_game_id LIKE '%#i%'"):
        split_ids.add(gid.split("#")[0])

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

    def verdict(ft, line):
        if line is None or ft is None:
            return None
        return "UNDER" if ft < line else "OVER" if ft > line else "PUSH"

    flips = []
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
        # firing observations
        firing = []
        for o in obs:
            t = _audit.epoch_of(o["captured_at"])
            avg = league[s["slug"]].avg_before(t)
            if avg is not None and o["required_pts_per_min"] > avg * MARGIN:
                firing.append((t, o))
        if not firing:
            continue
        t_first, o_first = firing[0]
        t_last, o_last = firing[-1]
        frozen = _q3.fin(o_first["live_total_line"])          # store A
        last_line = _q3.fin(o_last["live_total_line"])
        if frozen is None or last_line is None:
            continue
        if verdict(base["ft"], frozen) == verdict(base["ft"], last_line):
            continue                                          # not a flip

        # store B: snapshots authority at the boundary
        srows = snaps.get(s["gid"]) or []
        trig = trigger_observation(srows, 75, base["cls"]) if srows else {}
        line_b = _q3.fin(trig.get("total_line"))

        flips.append({
            "gid": s["gid"], "league": s["league"],
            "frozen": frozen, "last": last_line, "storeB": line_b,
            "ft": base["ft"],
            "agree": (line_b is not None and abs(line_b - frozen) < 1e-9),
            "split": s["gid"] in split_ids,
            "v_frozen": verdict(base["ft"], frozen),
            "v_last": verdict(base["ft"], last_line),
        })

    out("=" * 108)
    out("READ-ONLY — FROZEN TRIGGER-LINE VERIFICATION FOR ALL FLIP SETTLEMENTS, 2026-09-20")
    out("flip = |final-frozen|<=2 AND verdict(frozen) != verdict(last-fired).")
    out("store A = clean_projections (first firing obs); store B = snapshots trigger_observation.")
    out("=" * 108)
    out("")
    out(f"flip settlements verified: {len(flips)}")
    c = Counter(
        "A==B (line convention-proof)" if f["agree"]
        else ("B missing (no line at boundary in snapshots)" if f["storeB"] is None
              else "A!=B (STORES DISAGREE — integrity defect)")
        for f in flips)
    for k, v in c.items():
        out(f"  {k}: {v}")
    n_split = sum(1 for f in flips if f["split"])
    out(f"  split-identity games (base#iN sibling exists): {n_split} "
        f"{'— the 30964771 defect class; their boundary lines may be truncated' if n_split else ''}")
    out("")

    out("§1 STORE DISAGREEMENTS (A != B) — the only rows where the sealed line is doubtful")
    out("-" * 108)
    bad = [f for f in flips if f["storeB"] is not None and not f["agree"]]
    if not bad:
        out("  none — every provable line agrees across both stores")
    for f in bad:
        out(f"  {f['gid']:>9} {f['league']:<10} A={f['frozen']} B={f['storeB']} "
            f"final={f['ft']} ({f['v_frozen']} vs {f['v_last']})"
            + (" [SPLIT ID]" if f["split"] else ""))
    out("")

    out("§2 B-MISSING rows (snapshots hold no line at the boundary)")
    out("-" * 108)
    miss = [f for f in flips if f["storeB"] is None]
    out(f"  count: {len(miss)}")
    for f in miss[:20]:
        out(f"  {f['gid']:>9} {f['league']:<10} A={f['frozen']} final={f['ft']} "
            f"({f['v_frozen']} vs {f['v_last']})"
            + (" [SPLIT ID]" if f["split"] else ""))
    if len(miss) > 20:
        out(f"  ... and {len(miss)-20} more")
    out("")

    out("§3 SPLIT-IDENTITY flips — candidates for the settlement bridge to re-verify")
    out("-" * 108)
    sp = [f for f in flips if f["split"]]
    out(f"  count: {len(sp)}")
    for f in sp[:30]:
        out(f"  {f['gid']:>9} {f['league']:<10} A={f['frozen']} B="
            f"{f['storeB'] if f['storeB'] is not None else '-'} last={f['last']} "
            f"final={f['ft']} ({f['v_frozen']} vs {f['v_last']})")
    if len(sp) > 30:
        out(f"  ... and {len(sp)-30} more")
    out("")

    out("§4 FULL FLIP LEDGER (all rows)")
    out("-" * 108)
    hdr = (f"{'game':>9}  {'league':<10}{'A':>7}{'B':>7}{'last':>7}"
           f"{'final':>6}  {'frozen':<7}{'lastfire':<9}{'split':<6}{'A==B':>5}")
    out(hdr)
    for f in sorted(flips, key=lambda x: x["gid"]):
        out(f"{f['gid']:>9}  {f['league']:<10}{f['frozen']:>7.1f}"
            f"{('%7.1f' % f['storeB']) if f['storeB'] is not None else '      -'}"
            f"{f['last']:>7.1f}{f['ft']:>6}  {f['v_frozen']:<7}{f['v_last']:<9}"
            f"{('yes' if f['split'] else ''):<6}{('OK' if f['agree'] else 'NO'):>5}")
    out("")
    out("READ-ONLY: both DBs opened mode=ro; the only file written is this report.")
    con_p.close()
    con_c.close()
    with open(OUT, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    print(f"\nwritten: {OUT}")


if __name__ == "__main__":
    main()
