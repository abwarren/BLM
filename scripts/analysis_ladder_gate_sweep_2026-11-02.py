#!/usr/bin/env python3
"""Which gate produced the operator's ladder table? (read-only sweep, 2026-11-02)

Target totals from the screenshot: 145 bets · 124 W · 21 L · 85.5% ·
ladder net +203.22u · flat net +74.76u · 24 trading days · top stake 5.83u.

Sweeps the legs that could differ (price floor, progress ceiling, shortfall
margin, contested-loss rule) over the tc_all population and prints every
combination's totals so the target can be identified instead of guessed.
"""
from __future__ import annotations

import itertools
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/home/ubuntu/BLM")
from blm_v4.live_analytics.competition_pace import competition_pace_reference  # noqa: E402

PROD = "/home/ubuntu/BLM/blm_pokerbet.db"
CLEAN = "/home/ubuntu/BLM/blm_metrics_clean.db"
SINCE, UNTIL = "2026-10-01", "2026-10-30"
MIN_PROGRESS, MIN_POINTS, MIN_PACE_RATIO = 75.0, 70.0, 1.04
EXCLUDED = {"cyber-basketball-2k26-matches"}
DEPTH, FACTOR = 4, 1.8


def f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


conn = sqlite3.connect(f"file:{PROD}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
meta = {r["source_game_id"]: r["competition_slug"]
        for r in conn.execute("SELECT source_game_id, competition_slug FROM games")}
regions = {r["source_game_id"]: r["region"]
           for r in conn.execute("SELECT source_game_id, region FROM games")}
finals = {r["source_game_id"]: r["final_total"] for r in conn.execute(
    "SELECT source_game_id, final_total FROM game_results "
    "WHERE final_result_status='OK' AND final_total IS NOT NULL")}
pace_ref = competition_pace_reference(conn)
conn.close()

cc = sqlite3.connect(f"file:{CLEAN}?mode=ro", uri=True)
cc.row_factory = sqlite3.Row
peaks = {r[0]: r[1] for r in cc.execute(
    "SELECT source_game_id, MAX(total_points) FROM clean_observations "
    "WHERE total_points IS NOT NULL GROUP BY source_game_id")}
rows = cc.execute(
    "SELECT source_game_id, captured_at, total_points, progress_pct, "
    "required_pts_per_min, actual_pts_per_min, live_total_line, under_price "
    "FROM clean_observations WHERE status='VALID' "
    "AND progress_pct BETWEEN ? AND 92 AND total_points >= ? "
    "AND required_pts_per_min IS NOT NULL AND actual_pts_per_min IS NOT NULL "
    "AND live_total_line IS NOT NULL AND under_price IS NOT NULL "
    "AND captured_at >= ? AND captured_at <= ? "
    "ORDER BY source_game_id, captured_at",
    (MIN_PROGRESS, MIN_POINTS, f"{SINCE}T00:00:00", f"{UNTIL}T23:59:59.999")
).fetchall()
cc.close()
print(f"rows in the 75-92 band before pace/price/shortfall: {len(rows)}")


def build(max_prog, price_floor, shortfall, contest, require_region):
    pick = {}
    for r in rows:
        gid = r["source_game_id"]
        if gid in pick:
            continue
        if f(r["progress_pct"]) is None or r["progress_pct"] > max_prog:
            continue
        slug = meta.get(gid)
        if not slug or slug in EXCLUDED:
            continue
        if require_region:
            reg = (regions.get(gid) or "").replace("%20", " ").strip().lower()
            if reg != "virtual matches":
                continue
        req, act = f(r["required_pts_per_min"]), f(r["actual_pts_per_min"])
        price, line = f(r["under_price"]), f(r["live_total_line"])
        if None in (req, act, price, line) or price < price_floor:
            continue
        avg = (pace_ref.get(slug) or {}).get("avg_pace")
        if not avg or avg <= 0 or not (req / avg) > MIN_PACE_RATIO:
            continue
        if shortfall is not None and (req - act) < shortfall:
            continue
        res = None
        final = finals.get(gid)
        if final is None:
            peak = peaks.get(gid)
            if contest and peak is not None and peak > line:
                res, pnl = "L", -1.0
            else:
                continue                       # unsettled: excluded from the curve
        elif final < line:
            res, pnl = "W", price - 1.0
        elif final > line:
            res, pnl = "L", -1.0
        else:
            res, pnl = "P", 0.0
        pick[gid] = dict(ts=str(r["captured_at"])[:19], res=res, pnl=pnl,
                         price=price)
    return pick


def curve(pick):
    days = defaultdict(list)
    for c in pick.values():
        days[c["ts"][:10]].append(c)
    rung = 1
    lad = flat = 0.0
    w = l = banks = brk = n = 0
    top = 0.0
    for day in sorted(days):
        for c in sorted(days[day], key=lambda c: c["ts"]):
            stake = round(FACTOR ** (rung - 1), 4)
            top = max(top, stake)
            n += 1
            if c["res"] == "W":
                lad += stake * c["pnl"]
                flat += c["pnl"]
                w += 1
                rung = 1 if rung >= DEPTH else rung + 1
                if rung == 1:
                    banks += 1
            elif c["res"] == "L":
                lad -= stake
                flat -= 1.0
                l += 1
                rung = 1
                brk += 1
    return dict(n=n, w=w, l=l, win=w / max(1, w + l) * 100, lad=lad, flat=flat,
                days=len(days), top=top, banks=banks, brk=brk)


TARGET = (145, 124, 21)
print(f"\n{'maxprog':>7s} {'price':>5s} {'short':>6s} {'contest':>7s} {'region':>6s} "
      f"{'bets':>5s} {'W':>4s} {'L':>4s} {'win%':>6s} {'ladder':>9s} {'flat':>8s} "
      f"{'days':>5s} {'banks':>5s} {'brk':>4s}")
for max_prog, price_floor, shortfall, contest, region in itertools.product(
        (92.0, 88.0), (1.60, 1.70), (None, 0.50), (True, False), (False, True)):
    e = curve(build(max_prog, price_floor, shortfall, contest, region))
    hit = "  <<< MATCH" if (e["n"], e["w"], e["l"]) == TARGET else ""
    print(f"{max_prog:7.0f} {price_floor:5.2f} {str(shortfall):>6s} "
          f"{str(contest):>7s} {str(region):>6s} {e['n']:5d} {e['w']:4d} "
          f"{e['l']:4d} {e['win']:6.1f} {e['lad']:+9.2f} {e['flat']:+8.2f} "
          f"{e['days']:5d} {e['banks']:5d} {e['brk']:4d}{hit}")
