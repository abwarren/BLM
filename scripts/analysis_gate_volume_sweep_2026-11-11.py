#!/usr/bin/env python3
"""GATE VOLUME x EDGE SWEEP — last 7 days (read-only, 2026-11-11).

Question: the live UNDER gate (pace ratio > 1.04, shortfall >= 0.50,
under_price >= 1.60, progress 75-92, points >= 70, cyber excluded) produces
~6 candidates/day.  Is there a parameter set that produces ~40/day and still
has a real edge?

Method: identical population + settlement rules to
scripts/analysis_ladder_daily_shortfall_2026-11-02.py, over the 7 complete
days 2026-11-04 .. 2026-11-10 (today 2026-11-11 is partial and excluded).

  * candidate = the FIRST qualifying observation per game (live semantics);
  * settlement: OK final < live line = WIN (price - 1), > line = LOSS (-1),
    == line = push (0); games with no OK final are counted as candidates but
    excluded from the win-rate denominator;
  * both SQLite files are opened mode=ro;
  * the clean DB is sliced by rowid (captured_at is insert-monotonic) because
    captured_at has no index and a full scan of the 8 GB file is too slow.

Writes nothing.  Prints funnel, volume-matched combos, named-gate comparison,
day-by-day curves, and residual-filter variants.
"""
from __future__ import annotations

import itertools
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np

sys.path.insert(0, "/home/ubuntu/BLM")
from blm_v4.live_analytics.competition_pace import competition_pace_reference  # noqa: E402

PROD = "/home/ubuntu/BLM/blm_pokerbet.db"
CLEAN = "/home/ubuntu/BLM/blm_metrics_clean.db"

# 7 complete days; id bounds located by binary search on captured_at.
SINCE, UNTIL = "2026-11-04", "2026-11-11"
ID_LO, ID_HI = 5_528_123, 6_552_368

MIN_PROGRESS, MAX_PROGRESS = 75.0, 92.0
MIN_POINTS = 70.0
MIN_PACE_RATIO = 1.04
MIN_PRICE = 1.60
SHORTFALL = 0.50
EXCLUDED = {"cyber-basketball-2k26-matches"}

N_DAYS = 7


def f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def wilson_lower(w: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    p = w / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - half) / d


def load():
    conn = sqlite3.connect(f"file:{PROD}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    meta = {r["source_game_id"]: (r["competition_slug"], r["region"])
            for r in conn.execute(
                "SELECT source_game_id, competition_slug, region FROM games")}
    finals = {r["source_game_id"]: r["final_total"] for r in conn.execute(
        "SELECT source_game_id, final_total FROM game_results "
        "WHERE final_result_status='OK' AND final_total IS NOT NULL")}
    pace_ref = competition_pace_reference(conn)
    conn.close()

    cc = sqlite3.connect(f"file:{CLEAN}?mode=ro", uri=True)
    cc.row_factory = sqlite3.Row
    rows = cc.execute(
        "SELECT source_game_id, captured_at, total_points, progress_pct, "
        "required_pts_per_min, actual_pts_per_min, live_total_line, "
        "under_price, fair_total, z_score, market_captured_at "
        "FROM clean_observations "
        "WHERE status='VALID' AND id BETWEEN ? AND ? "
        "AND progress_pct IS NOT NULL AND progress_pct BETWEEN 40 AND 99 "
        "AND total_points >= 30 "
        "AND required_pts_per_min IS NOT NULL AND actual_pts_per_min IS NOT NULL "
        "AND live_total_line IS NOT NULL AND under_price IS NOT NULL "
        "ORDER BY source_game_id, captured_at",
        (ID_LO, ID_HI)).fetchall()
    cc.close()
    return meta, finals, pace_ref, rows


def build_arrays(meta, pace_ref, rows):
    gids = np.array([r["source_game_id"] for r in rows], dtype=object)
    # game code: first-appearance index preserves the (game, ts) sort order
    uniq, codes = np.unique(gids, return_inverse=True)
    slug = np.array([(meta.get(g) or (None, None))[0] for g in uniq], dtype=object)
    region = np.array([(meta.get(g) or (None, None))[1] for g in uniq], dtype=object)
    avg = np.array([f((pace_ref.get(s) or {}).get("avg_pace")) or np.nan
                    for s in slug], dtype=float)
    g_codes = codes.astype(np.int32)

    def col(name):
        return np.array([f(r[name]) if r[name] is not None else np.nan
                         for r in rows], dtype=np.float64)

    req = col("required_pts_per_min")
    act = col("actual_pts_per_min")
    prog = col("progress_pct")
    pts = col("total_points")
    line = col("live_total_line")
    price = col("under_price")
    fair = col("fair_total")
    ts = np.array([r["captured_at"] for r in rows], dtype=object)
    age = np.array([market_age(r["captured_at"], r["market_captured_at"])
                    for r in rows], dtype=np.float64)

    # attach per-row competition
    row_slug = slug[g_codes]
    excluded = np.array([s in EXCLUDED for s in row_slug], dtype=bool)
    g_avg = avg[g_codes]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(g_avg > 0, req / g_avg, np.nan)
    short = req - act
    resid = fair - line       # negative = BLM fair below the line (UNDER side)
    return dict(g_codes=g_codes, req=req, act=act, prog=prog, pts=pts,
                line=line, price=price, fair=fair, ts=ts, ratio=ratio,
                short=short, resid=resid, excluded=excluded, age=age,
                row_slug=row_slug, n_games=len(uniq))


def market_age(captured_at, market_at):
    if not captured_at or not market_at:
        return np.nan
    try:
        a = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(market_at).replace("Z", "+00:00"))
    except ValueError:
        return np.nan
    return (a - b).total_seconds()


def gate_mask(A, prog_lo, prog_hi, pts_min, pace_min, short_min,
              price_min, resid_max=None, max_age=None):
    m = (~A["excluded"]) & np.isfinite(A["ratio"]) & (A["ratio"] > pace_min)
    m &= (A["prog"] >= prog_lo) & (A["prog"] <= prog_hi)
    m &= A["pts"] >= pts_min
    m &= A["price"] >= price_min
    m &= np.isfinite(A["short"])
    if short_min is not None:
        m &= A["short"] >= short_min
    if resid_max is not None:
        m &= np.isfinite(A["resid"]) & (A["resid"] <= resid_max)
    if max_age is not None:
        m &= np.isfinite(A["age"]) & (A["age"] <= max_age)
    return m


def first_per_game(A, mask):
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return idx
    g = A["g_codes"][idx]
    _, first = np.unique(g, return_index=True)
    return idx[first]


def score(A, finals, sel):
    days = defaultdict(int)
    w = l = p = unknown = 0
    flat = 0.0
    win_prices = []
    for i in sel:
        gid = A["gid_lookup"][i]
        day = A["ts"][i][:10]
        days[day] += 1
        final = finals.get(gid)
        line = A["line"][i]
        price = A["price"][i]
        if final is None:
            unknown += 1
            continue
        if final < line:
            w += 1
            flat += price - 1.0
            win_prices.append(price)
        elif final > line:
            l += 1
            flat -= 1.0
        else:
            p += 1
    n = w + l
    win_rate = w / n * 100 if n else 0.0
    avg_price = (sum(win_prices) / len(win_prices)) if win_prices else 0.0
    breakeven = (1.0 / avg_price * 100) if avg_price else 0.0
    roi = flat / n * 100 if n else 0.0
    return dict(n=len(sel), settled=n, w=w, l=l, p=p, unknown=unknown,
                win=win_rate, flat=flat, roi=roi, avg_price=avg_price,
                breakeven=breakeven, wilson=wilson_lower(w, n),
                days=len(days), daymap=dict(days))


def fmt(st):
    return (f"{st['n']:5d} {st['n']/N_DAYS:5.1f} {st['settled']:5d} "
            f"{st['win']:6.1f} {st['breakeven']:6.1f} {st['flat']:+8.2f} "
            f"{st['roi']:+7.1f} {st['wilson']*100:6.1f}")


HDR = (f"{'bets':>5s} {'/day':>5s} {'sett':>5s} {'win%':>6s} {'bk-even':>6s} "
       f"{'flat':>8s} {'ROI%':>7s} {'wlb%':>6s}")


def main() -> int:
    meta, finals, pace_ref, rows = load()
    A = build_arrays(meta, pace_ref, rows)
    A["gid_lookup"] = np.array([r["source_game_id"] for r in rows], dtype=object)

    print(f"7-day slice {SINCE} → {UNTIL} (today excluded)")
    print(f"observations in slice: {len(rows):,} · games: {A['n_games']:,} · "
          f"settled OK finals available: {len(finals):,}")
    print(f"first-moment population rules: status VALID · cyber excluded · "
          f"first qualifying observation per game")
    print(f"settlement: OK final < line = W (price-1) · > line = L (-1) · "
          f"== line = push · no OK final = excluded from win%")
    print()

    # ── 1. funnel at the live gate and its relaxations ──────────────
    print("1. VOLUME FUNNEL (first moment per game, exactly as the live gate)")
    print("-" * 78)
    funnel = [
        ("live: pace>1.04 short>=0.50 price>=1.60 prog75-92 pts>=70",
         dict(prog_lo=75, prog_hi=92, pts_min=70, pace_min=1.04,
              short_min=0.50, price_min=1.60)),
        ("shortfall off (pace>1.04, price>=1.60, prog75-92, pts>=70)",
         dict(prog_lo=75, prog_hi=92, pts_min=70, pace_min=1.04,
              short_min=None, price_min=1.60)),
        ("shortfall off + price>=1.50",
         dict(prog_lo=75, prog_hi=92, pts_min=70, pace_min=1.04,
              short_min=None, price_min=1.50)),
        ("shortfall off + prog 60-95",
         dict(prog_lo=60, prog_hi=95, pts_min=70, pace_min=1.04,
              short_min=None, price_min=1.60)),
        ("shortfall off + prog 45-95 + pts>=40",
         dict(prog_lo=45, prog_hi=95, pts_min=40, pace_min=1.04,
              short_min=None, price_min=1.60)),
        ("shortfall off + pace>1.00 (any price>=1.50, prog45-95)",
         dict(prog_lo=45, prog_hi=95, pts_min=40, pace_min=1.00,
              short_min=None, price_min=1.50)),
    ]
    print(f"{'gate':62s} {'bets':>5s} {'/day':>5s} {'sett':>5s} {'win%':>6s}")
    for label, kw in funnel:
        sel = first_per_game(A, gate_mask(A, **kw))
        st = score(A, finals, sel)
        print(f"{label:62s} {st['n']:5d} {st['n']/N_DAYS:5.1f} "
              f"{st['settled']:5d} {st['win']:6.1f}")

    # ── 2. full grid, filtered to ~25-60 candidates/day ─────────────
    print("\n2. PARAMETER SWEEP — every combo in the 25-60 candidates/day band")
    print("-" * 78)
    print(f"grid: progress (75-92, 70-95, 60-95, 45-95) x pts (70,50,30) x "
          f"pace (1.00,1.04,1.08,1.12,1.20) x shortfall (off,0.00,0.25,0.50) "
          f"x price (1.50,1.60,1.70)")
    print(HDR)
    results = []
    for prog, pts_min, pace, short, price in itertools.product(
            ((75, 92), (70, 95), (60, 95), (45, 95)),
            (70, 50, 30), (1.00, 1.04, 1.08, 1.12, 1.20),
            (None, 0.00, 0.25, 0.50), (1.50, 1.60, 1.70)):
        sel = first_per_game(A, gate_mask(
            A, prog[0], prog[1], pts_min, pace, short, price))
        st = score(A, finals, sel)
        if st["settled"] < 100:
            continue
        results.append((st, prog, pts_min, pace, short, price))
    band = [r for r in results if 175 <= r[0]["n"] <= 420]
    band.sort(key=lambda r: (r[0]["roi"], r[0]["win"]), reverse=True)
    for st, prog, pts_min, pace, short, price in band[:25]:
        s = "off " if short is None else f"{short:.2f}"
        print(f"{fmt(st)}  prog{prog[0]}-{prog[1]} pts>={pts_min} "
              f"pace>{pace:.2f} short>={s} price>={price:.2f}")

    print("\n   best ROI overall (settled >= 100, ANY volume):")
    for st, prog, pts_min, pace, short, price in sorted(
            results, key=lambda r: (r[0]["roi"], r[0]["win"]), reverse=True)[:10]:
        s = "off " if short is None else f"{short:.2f}"
        print(f"{fmt(st)}  prog{prog[0]}-{prog[1]} pts>={pts_min} "
              f"pace>{pace:.2f} short>={s} price>={price:.2f}")

    # ── 3. named-gate comparison ────────────────────────────────────
    print("\n3. NAMED GATE COMPARISON")
    print("-" * 78)
    print(HDR)
    named = [
        ("LIVE  pace>1.04 short>=0.50 price>=1.60 prog75-92 pts>=70",
         dict(prog_lo=75, prog_hi=92, pts_min=70, pace_min=1.04,
              short_min=0.50, price_min=1.60)),
        ("TC-S  pace>1.08 short>=0.50 (same band)",
         dict(prog_lo=75, prog_hi=92, pts_min=70, pace_min=1.08,
              short_min=0.50, price_min=1.60)),
        ("PACE-ONLY  pace>1.04 short off (same band)",
         dict(prog_lo=75, prog_hi=92, pts_min=70, pace_min=1.04,
              short_min=None, price_min=1.60)),
        ("PACE-ONLY  pace>1.08 short off (same band)",
         dict(prog_lo=75, prog_hi=92, pts_min=70, pace_min=1.08,
              short_min=None, price_min=1.60)),
        ("VOL40  pace>1.04 short off prog60-95 pts>=70 price>=1.60",
         dict(prog_lo=60, prog_hi=95, pts_min=70, pace_min=1.04,
              short_min=None, price_min=1.60)),
        ("VOL40  pace>1.04 short off prog45-95 pts>=50 price>=1.60",
         dict(prog_lo=45, prog_hi=95, pts_min=50, pace_min=1.04,
              short_min=None, price_min=1.60)),
    ]
    named_states = {}
    for label, kw in named:
        st = score(A, finals, first_per_game(A, gate_mask(A, **kw)))
        named_states[label] = (st, kw)
        print(f"{fmt(st)}  {label}")

    # ── 4. residual (BLM fair vs line) as an alternative leg ────────
    print("\n4. RESIDUAL LEG (fair_total - live line) added to a volume gate")
    print("-" * 78)
    print("base: pace>1.04 · shortfall off · price>=1.60 · prog 60-95 · pts>=70")
    print(HDR)
    for resid_max in (None, 0.0, -2.5, -5.0, -8.0):
        sel = first_per_game(A, gate_mask(
            A, 60, 95, 70, 1.04, None, 1.60, resid_max=resid_max))
        st = score(A, finals, sel)
        lab = "none" if resid_max is None else f"resid <= {resid_max:+.1f}"
        print(f"{fmt(st)}  {lab}")

    # ── 5. line freshness: is the edge tradeable? ───────────────────
    print("\n5. MARKET-LINE FRESHNESS (age = captured_at - market_captured_at)")
    print("-" * 78)
    print("A stale line can be 10+ points off the current score late in a game.")
    print("Those rows win almost automatically but are NOT bettable prices.")
    print(f"{'gate':52s} {'n':>5s} {'<=60s':>7s} {'61-300':>7s} {'>300':>7s} "
          f"{'win%<=60':>9s} {'win%>300':>9s}")
    for label, kw in named:
        sel = first_per_game(A, gate_mask(A, **kw))
        age = A["age"][sel]
        fresh = np.isfinite(age) & (age <= 60)
        mid = np.isfinite(age) & (age > 60) & (age <= 300)
        old = np.isfinite(age) & (age > 300)
        wr = []
        for bucket in (fresh, mid, old):
            idx = sel[bucket]
            w = l = 0
            for i in idx:
                final = finals.get(A["gid_lookup"][i])
                if final is None:
                    continue
                if final < A["line"][i]:
                    w += 1
                elif final > A["line"][i]:
                    l += 1
            wr.append(w / (w + l) * 100 if w + l else 0.0)
        print(f"{label:52s} {len(sel):5d} {fresh.sum():7d} {mid.sum():7d} "
              f"{old.sum():7d} {wr[0]:9.1f} {wr[2]:9.1f}")

    # ── 6. the same grid, fresh lines only ─────────────────────────
    print("\n6. VOLUME SWEEP ON FRESH LINES ONLY (age <= 60s)")
    print("-" * 78)
    print(HDR)
    fresh_rows = []
    for prog, pts_min, pace, short, price in itertools.product(
            ((75, 92), (70, 95), (60, 95), (45, 95)),
            (70, 50, 30), (1.00, 1.04, 1.08, 1.12, 1.20),
            (None, 0.00, 0.25, 0.50), (1.50, 1.60, 1.70)):
        sel = first_per_game(A, gate_mask(
            A, prog[0], prog[1], pts_min, pace, short, price, max_age=60))
        st = score(A, finals, sel)
        if st["settled"] < 60:
            continue
        fresh_rows.append((st, prog, pts_min, pace, short, price))
    fb = [r for r in fresh_rows if 175 <= r[0]["n"] <= 420]
    fb.sort(key=lambda r: (r[0]["roi"], r[0]["win"]), reverse=True)
    if not fb:
        print("(no fresh-line combo reaches 25-60 candidates/day)")
    for st, prog, pts_min, pace, short, price in fb[:20]:
        s = "off " if short is None else f"{short:.2f}"
        print(f"{fmt(st)}  prog{prog[0]}-{prog[1]} pts>={pts_min} "
              f"pace>{pace:.2f} short>={s} price>={price:.2f}")
    print("\n   best fresh-line ROI at ANY volume (settled >= 60):")
    for st, prog, pts_min, pace, short, price in sorted(
            fresh_rows, key=lambda r: (r[0]["roi"], r[0]["win"]), reverse=True)[:10]:
        s = "off " if short is None else f"{short:.2f}"
        print(f"{fmt(st)}  prog{prog[0]}-{prog[1]} pts>={pts_min} "
              f"pace>{pace:.2f} short>={s} price>={price:.2f}")

    # ── 7. how much of the edge is just line latency? ──────────────
    print("\n7. EDGE DECAY BY LINE AGE (same gate, increasingly fresh lines)")
    print("-" * 78)
    age_tests = [
        ("A vol~36/day prog70-95 pace>1.12 short off price>=1.50",
         dict(prog_lo=70, prog_hi=95, pts_min=70, pace_min=1.12,
              short_min=None, price_min=1.50)),
        ("B vol~56/day prog70-95 pace>1.08 short off price>=1.60",
         dict(prog_lo=70, prog_hi=95, pts_min=70, pace_min=1.08,
              short_min=None, price_min=1.60)),
        ("LIVE     prog75-92 pace>1.04 short>=0.50 price>=1.60",
         dict(prog_lo=75, prog_hi=92, pts_min=70, pace_min=1.04,
              short_min=0.50, price_min=1.60)),
    ]
    for label, kw in age_tests:
        print(f"\n{label}")
        print(f"{'max age':>9s} {'bets':>5s} {'/day':>5s} {'sett':>5s} {'win%':>6s} "
              f"{'bk-even':>7s} {'flat':>8s} {'ROI%':>7s}")
        for max_age in (5, 15, 30, 60, 120, 300, None):
            sel = first_per_game(A, gate_mask(A, max_age=max_age, **kw))
            st = score(A, finals, sel)
            lab = "any" if max_age is None else f"<={max_age}s"
            print(f"{lab:>9s} {st['n']:5d} {st['n']/N_DAYS:5.1f} {st['settled']:5d} "
                  f"{st['win']:6.1f} {st['breakeven']:7.1f} {st['flat']:+8.2f} "
                  f"{st['roi']:+7.1f}")

    # ── 8. day-by-day for the volume candidates ─────────────────────
    print("\n8. DAY-BY-DAY (volume-matched named gates)")
    print("-" * 78)
    ddb = [
        ("A vol~36/day fresh<=60s (pace>1.12, prog70-95, price>=1.50)",
         dict(prog_lo=70, prog_hi=95, pts_min=70, pace_min=1.12,
              short_min=None, price_min=1.50, max_age=60)),
        ("B vol~56/day fresh<=60s (pace>1.08, prog70-95, price>=1.60)",
         dict(prog_lo=70, prog_hi=95, pts_min=70, pace_min=1.08,
              short_min=None, price_min=1.60, max_age=60)),
        ("PACE-ONLY  pace>1.04 short off (same band)",
         dict(prog_lo=75, prog_hi=92, pts_min=70, pace_min=1.04,
              short_min=None, price_min=1.60)),
    ]
    for label, kw in ddb:
        daymap = defaultdict(lambda: [0, 0, 0, 0.0])
        for i in first_per_game(A, gate_mask(A, **kw)):
            gid = A["gid_lookup"][i]
            day = A["ts"][i][:10]
            final = finals.get(gid)
            if final is None:
                daymap[day][3] += 0
                continue
            if final < A["line"][i]:
                daymap[day][0] += 1
                daymap[day][3] += A["price"][i] - 1.0
            elif final > A["line"][i]:
                daymap[day][1] += 1
                daymap[day][3] -= 1.0
            else:
                daymap[day][2] += 1
        print(f"\n{label}")
        print(f"{'day':11s} {'W':>3s} {'L':>3s} {'P':>3s} {'win%':>6s} {'flat':>8s}")
        for day in sorted(daymap):
            w, l, p, pnl = daymap[day]
            n = w + l
            print(f"{day:11s} {w:3d} {l:3d} {p:3d} "
                  f"{(w/n*100 if n else 0):6.1f} {pnl:+8.2f}")

    # competition concentration for candidate B
    print("\nCOMPETITION MIX — candidate B (fresh <=60s):")
    kw = dict(prog_lo=70, prog_hi=95, pts_min=70, pace_min=1.08,
              short_min=None, price_min=1.60, max_age=60)
    comp = defaultdict(lambda: [0, 0, 0.0])
    for i in first_per_game(A, gate_mask(A, **kw)):
        final = finals.get(A["gid_lookup"][i])
        c = comp[A["row_slug"][i]]
        if final is None:
            continue
        c[2] += A["price"][i] - 1.0 if final < A["line"][i] else (
            0.0 if final == A["line"][i] else -1.0)
        if final < A["line"][i]:
            c[0] += 1
        elif final > A["line"][i]:
            c[1] += 1
    for slug, (w, l, pnl) in sorted(comp.items(), key=lambda kv: -(kv[1][0]+kv[1][1])):
        n = w + l
        print(f"  {str(slug):26s} n={n:4d} W={w:4d} L={l:3d} "
              f"win={w/n*100 if n else 0:5.1f}% flat={pnl:+7.2f}")

    print("\nCAVEAT: 7 calendar days, one competition mix, in-sample; "
          "40/day target met by dropping the shortfall leg, which raises")
    print("volume ~7x and cuts win rate; check the day-by-day before "
          "treating any row as a durable edge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
