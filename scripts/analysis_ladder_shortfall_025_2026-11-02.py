#!/usr/bin/env python3
"""The 0.25 shortfall ladder, canonical and deterministic (read-only, 2026-11-02).

Same population and ladder as scripts/analysis_ladder_daily_shortfall_2026-11-02.py
(tc_all's first qualifying moment per game; depth 4, factor 1.8, 1.00u start),
but the bet order inside a day is pinned to ``(captured_at, game_id)`` so the
ladder P&L does not depend on SQLite's row order.  That mattered: with ~1,400
bets on the same timestamp the run-to-run ladder total moved by ~22u; at 192
bets it moves by ~0.3u.

Read-only: both SQLite files are opened mode=ro.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/home/ubuntu/BLM")
from blm_v4.live_analytics.competition_pace import competition_pace_reference  # noqa: E402

PROD = "/home/ubuntu/BLM/blm_pokerbet.db"
CLEAN = "/home/ubuntu/BLM/blm_metrics_clean.db"
SINCE, UNTIL = "2026-10-01", "2026-10-30"
MIN_PROGRESS, MAX_PROGRESS = 75.0, 92.0
MIN_POINTS, MIN_PACE_RATIO, MIN_PRICE = 70.0, 1.04, 1.60
EXCLUDED = {"cyber-basketball-2k26-matches"}
DEPTH, FACTOR, START = 4, 1.8, 1.0


def f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def load():
    conn = sqlite3.connect(f"file:{PROD}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    meta = {r["source_game_id"]: r["competition_slug"]
            for r in conn.execute("SELECT source_game_id, competition_slug FROM games")}
    finals = {r["source_game_id"]: r["final_total"] for r in conn.execute(
        "SELECT source_game_id, final_total FROM game_results "
        "WHERE final_result_status='OK' AND final_total IS NOT NULL")}
    ref = competition_pace_reference(conn)
    conn.close()
    cc = sqlite3.connect(f"file:{CLEAN}?mode=ro", uri=True)
    cc.row_factory = sqlite3.Row
    peaks = {r[0]: r[1] for r in cc.execute(
        "SELECT source_game_id, MAX(total_points) FROM clean_observations "
        "WHERE total_points IS NOT NULL GROUP BY source_game_id")}
    rows = cc.execute(
        "SELECT source_game_id, captured_at, progress_pct, total_points, "
        "required_pts_per_min, actual_pts_per_min, live_total_line, under_price "
        "FROM clean_observations WHERE status='VALID' "
        "AND progress_pct BETWEEN ? AND ? AND total_points >= ? "
        "AND required_pts_per_min IS NOT NULL AND actual_pts_per_min IS NOT NULL "
        "AND live_total_line IS NOT NULL AND under_price IS NOT NULL "
        "AND captured_at >= ? AND captured_at <= ? "
        "ORDER BY source_game_id, captured_at",
        (MIN_PROGRESS, MAX_PROGRESS, MIN_POINTS, f"{SINCE}T00:00:00",
         f"{UNTIL}T23:59:59.999")).fetchall()
    cc.close()
    return meta, finals, peaks, rows, ref


def build(meta, rows, ref, shortfall):
    pick = {}
    for r in rows:
        gid = r["source_game_id"]
        if gid in pick:
            continue
        slug = meta.get(gid)
        if not slug or slug in EXCLUDED:
            continue
        req, act = f(r["required_pts_per_min"]), f(r["actual_pts_per_min"])
        price, line = f(r["under_price"]), f(r["live_total_line"])
        if None in (req, act, price, line) or price < MIN_PRICE:
            continue
        avg = (ref.get(slug) or {}).get("avg_pace")
        if not avg or avg <= 0 or not (req / avg) > MIN_PACE_RATIO:
            continue
        if shortfall is not None and (req - act) < shortfall:
            continue
        pick[gid] = dict(gid=gid, ts=str(r["captured_at"])[:19], price=price, line=line)
    return pick


def settle(pick, finals, peaks):
    out = []
    for c in pick.values():
        final = finals.get(c["gid"])
        if final is None:
            peak = peaks.get(c["gid"])
            if peak is not None and peak > c["line"]:
                out.append((c["ts"], c["gid"], "L", -1.0))
            else:
                continue
        elif final < c["line"]:
            out.append((c["ts"], c["gid"], "W", c["price"] - 1.0))
        elif final > c["line"]:
            out.append((c["ts"], c["gid"], "L", -1.0))
        else:
            out.append((c["ts"], c["gid"], "P", 0.0))
    return sorted(out)                     # (ts, gid): deterministic


def curve(bets):
    days = defaultdict(list)
    for ts, gid, res, pnl in bets:
        days[ts[:10]].append((ts, gid, res, pnl))
    rung, cap = 1, START
    lad = flat = 0.0
    n = w = l = banks = brk = 0
    top = 0.0
    per = {}
    rows = []
    for day in sorted(days):
        dl = df = 0.0
        dw = dlo = db = dk = 0
        dtop = 0.0
        for ts, gid, res, pnl in sorted(days[day]):
            stake = round(FACTOR ** (rung - 1), 4)
            dtop = max(dtop, stake)
            top = max(top, stake)
            n += 1
            if res == "W":
                lad += stake * pnl
                flat += pnl
                dl += stake * pnl
                df += pnl
                dw += 1
                rung = 1 if rung >= DEPTH else rung + 1
                if rung == 1:
                    banks += 1
                    db += 1
            elif res == "L":
                lad -= stake
                flat -= 1.0
                dl -= stake
                df -= 1.0
                dlo += 1
                rung = 1
                brk += 1
                dk += 1
        cap += dl
        w += dw
        l += dlo
        per[day] = dl
        rows.append(dict(day=day, n=len(days[day]), w=dw, l=dlo,
                         win=(dw / (dw + dlo) * 100) if (dw + dlo) else 0.0,
                         lad=dl, flat=df, cap=cap, top=dtop, banks=db, brk=dk))
    return dict(n=n, w=w, l=l, win=w / max(1, w + l) * 100, lad=lad, flat=flat,
                days=rows, top=top, banks=banks, brk=brk, cap=cap,
                best=max(per.values(), default=0.0),
                worst=min(per.values(), default=0.0),
                up=sum(1 for v in per.values() if v > 0))


def main() -> int:
    meta, finals, peaks, rows, ref = load()
    print(f"shortfall >= 0.25 · pace > {MIN_PACE_RATIO:.2f} · price >= {MIN_PRICE:.2f}"
          f" · progress {MIN_PROGRESS:.0f}-{MAX_PROGRESS:.0f} · cyber-2k26 out"
          f" · depth {DEPTH}, factor {FACTOR}, {START:.2f}u start"
          f" · ordered by (captured_at, game_id)")
    e = curve(settle(build(meta, rows, ref, 0.25), finals, peaks))
    print(f"\n{'day':11s} {'bets':>4s} {'W':>3s} {'L':>3s} {'win%':>6s} "
          f"{'ladder net':>11s} {'flat net':>9s} {'capital':>8s} "
          f"{'top stake':>10s} {'banks':>6s} {'brk':>4s}")
    for d in e["days"]:
        print(f"{d['day']:11s} {d['n']:4d} {d['w']:3d} {d['l']:3d} {d['win']:6.1f} "
              f"{d['lad']:+11.2f} {d['flat']:+9.2f} {d['cap']:8.2f} "
              f"{d['top']:10.2f} {d['banks']:6d} {d['brk']:4d}")
    print(f"{'TOTAL':11s} {e['n']:4d} {e['w']:3d} {e['l']:3d} {e['win']:6.1f} "
          f"{e['lad']:+11.2f} {e['flat']:+9.2f} {e['cap']:8.2f} "
          f"{e['top']:10.2f} {e['banks']:6d} {e['brk']:4d}")
    print(f"  {len(e['days'])} trading days · {e['up']} up · "
          f"{len(e['days']) - e['up']} down · best {e['best']:+.2f}u · "
          f"worst {e['worst']:+.2f}u · top stake never exceeded {e['top']:.2f}u · "
          f"banks {e['banks']}/{e['brk']}")

    print("\nshortfall comparison (pace 1.04, price 1.60, same ordering):")
    print(f"{'shortfall':>10s} {'bets':>5s} {'W':>4s} {'L':>4s} {'win%':>6s} "
          f"{'ladder':>9s} {'flat':>8s} {'banks':>5s} {'brk':>4s} "
          f"{'best':>7s} {'worst':>7s}")
    for label, margin in (("off", None), ("0.00", 0.00), ("0.25", 0.25),
                          ("0.50", 0.50)):
        x = curve(settle(build(meta, rows, ref, margin), finals, peaks))
        print(f"{label:>10s} {x['n']:5d} {x['w']:4d} {x['l']:4d} {x['win']:6.1f} "
              f"{x['lad']:+9.2f} {x['flat']:+8.2f} {x['banks']:5d} {x['brk']:4d} "
              f"{x['best']:+7.2f} {x['worst']:+7.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
