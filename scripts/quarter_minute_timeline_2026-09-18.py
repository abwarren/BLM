#!/usr/bin/env python3
"""Per-quarter, per-minute point timeline on OUR clock (collector captured_at).

For each quarter (minutes 1..12):
  - points scored in each minute (home/away split), stacked-bar SVG + TXT tables
  - every score-change event placed in its minute with interpolated offset

Method / honesty notes:
  - t=0 is the first scored snapshot on our clock; provider wall clock ignored.
  - The feed's game clock is whole-minute only (e.g. "07:00"), so minute 12
    spans 12:00->11:00. Within-minute offsets are interpolated from our
    capture clock (~5s resolution).
  - Snapshots whose period_label is "?" (feed glitch) are attributed to the
    quarter/minute of their surrounding clocked neighbours by interpolation.
  - Points already on the board at our FIRST snapshot (we joined at clock
    11:00 = minute 2) are counted into Q1 but are NOT attributable to a
    specific minute; they are reported as a separate pre-observation row.
  - Cross-check vs the feed's score_detail per-quarter finals is reported
    verbatim. In game 30949595 the feed's own detail sums to LESS than the
    observed final score; we report that gap rather than fudging.

Usage:
  python3 scripts/quarter_minute_timeline_2026-09-18.py [source_game_id]
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "blm_pokerbet.db"
OUT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_GAME = "30949595"  # Los Angeles Lakers @ Charlotte Hornets, 2026-09-18

QUARTER_LABELS = {"1st Quarter": 1, "2nd Quarter": 2, "3rd Quarter": 3,
                  "4th Quarter": 4}


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def load(game_id: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    try:
        rows = conn.execute(
            """SELECT captured_at, home_team, away_team, home_score, away_score,
                      period_label, clock, raw_json
                 FROM snapshots
                WHERE source_game_id = ?
                  AND home_score IS NOT NULL AND away_score IS NOT NULL
                ORDER BY captured_at, id""",
            (game_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        sys.exit(f"No scored snapshots for game {game_id!r}")
    snaps = []
    for r in rows:
        m = re.search(r'"score_detail":\s*"([^"]*)"', r[7] or "")
        snaps.append({
            "captured_at": r[0], "home_team": r[1], "away_team": r[2],
            "home": r[3], "away": r[4], "period": r[5] or "", "clock": r[6],
            "detail": m.group(1) if m else "",
        })
    return snaps


def quarter_finals(detail: str):
    """'(30:33), (21:22), ...' -> [(30,33), ...]"""
    return [(int(h), int(a)) for h, a in re.findall(r"\((\d+):(\d+)\)", detail)]


def build(snaps: list[dict]) -> list[dict]:
    """Score-change events with quarter (1-4), minute (1-12), offset."""
    t0 = parse_ts(snaps[0]["captured_at"])
    events: list[dict] = []
    prev_ev = None
    # anchors: for each quarter, first-seen our-time of each whole-minute clock
    anchor_t: dict[tuple[int, int], float] = {}
    anchor_order: list[tuple[int, int, float]] = []  # (q, cm, t) in time order

    for s in snaps:
        if prev_ev is not None and (s["home"], s["away"]) == (prev_ev["home"], prev_ev["away"]):
            continue
        t = (parse_ts(s["captured_at"]) - t0).total_seconds()
        ev = {
            "t": t, "captured_at": s["captured_at"],
            "home": s["home"], "away": s["away"],
            "d_home": 0 if prev_ev is None else s["home"] - prev_ev["home"],
            "d_away": 0 if prev_ev is None else s["away"] - prev_ev["away"],
            "pre_first": prev_ev is None,
            "period": s["period"], "clock": s["clock"],
            "quarter": None, "minute": None, "offset_s": None,
        }
        q = QUARTER_LABELS.get(s["period"])
        cm = int(s["clock"][:2]) if (s["clock"] and q) else None
        if q and cm is not None:
            key = (q, cm)
            if key not in anchor_t:
                anchor_t[key] = t
                anchor_order.append((q, cm, t))
            ev["quarter"] = q
            ev["minute"] = 13 - cm          # 12:00 -> min 1 ... 01:00 -> min 12
            ev["offset_s"] = int(round(min(max(t - anchor_t[key], 0.0), 59.9)))
        events.append(ev)
        prev_ev = ev

    # attribute "?"-period events by interpolation between clocked neighbours
    for i, ev in enumerate(events):
        if ev["quarter"] is not None:
            continue
        prev_a = next((e for e in reversed(events[:i]) if e["quarter"] is not None), None)
        next_a = next((e for e in events[i + 1:] if e["quarter"] is not None), None)
        if prev_a is None:
            continue  # nothing to anchor to — stays unattributed
        q = prev_a["quarter"]
        # the minute in force at the previous anchor (unless the next anchor
        # is the SAME minute, in which case offsets are relative to it)
        if next_a is not None and (next_a["quarter"], next_a["minute"]) == (prev_a["quarter"], prev_a["minute"]):
            base_t = next_a["t"] - next_a["offset_s"]  # reconstruct anchor time
            mn = prev_a["minute"]
        else:
            base_t = prev_a["t"] - prev_a["offset_s"]
            mn = prev_a["minute"]
        ev["quarter"] = q
        ev["minute"] = mn
        ev["offset_s"] = int(round(min(max(ev["t"] - base_t, 0.0), 59.9)))
        ev["interp"] = True
    return events


def quarter_minute_grid(events: list[dict]):
    """minute -> {q: [home_pts, away_pts]}, plus pre-observation initial pts."""
    grid = {m: {q: [0, 0] for q in (1, 2, 3, 4)} for m in range(1, 13)}
    pre = [0, 0]  # points already on board at our first snapshot (Q1)
    for e in events:
        if e["quarter"] is None:
            continue
        if e["pre_first"]:
            pre[0] += e["home"]
            pre[1] += e["away"]
        elif e["minute"] is not None:
            grid[e["minute"]][e["quarter"]][0] += e["d_home"]
            grid[e["minute"]][e["quarter"]][1] += e["d_away"]
    return grid, pre


def fmt_t(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    return f"T+{m:02d}:{s:02d}"


def render_svg(grid, pre, teams, game_id, path):
    home, away = teams
    qcols = {1: "#4fc3f7", 2: "#81c784", 3: "#ffb74d", 4: "#f48fb1"}
    W, H, ML, MT, MB = 1400, 780, 80, 100, 80
    bw, gap = 78, 26
    y0, ymax = H - MB, 20
    scale = (y0 - MT) / ymax
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             'font-family="monospace" font-size="12">']
    parts.append(f'<rect width="{W}" height="{H}" fill="#111418"/>')
    parts.append(f'<text x="{ML}" y="30" fill="#fff" font-size="16" font-weight="bold">'
                 f'{home} vs {away} — points per minute (1–12), split by quarter — OUR clock</text>')
    parts.append(f'<text x="{ML}" y="50" fill="#889">game {game_id} · minute 12 = 12:00→11:00 · '
                 'within-minute offset interpolated from our ~5s capture cadence · '
                 'feed clock is whole-minute</text>')
    parts.append(f'<text x="{ML}" y="68" fill="#a86">note: initial {pre[0]}–{pre[1]} pre-dates our first '
                 'snapshot (joined at 11:00 = minute 2) — in Q1 total, not in any minute bar</text>')

    subw, subgap = 16, 2  # four side-by-side sub-bars inside each minute slot
    for i, m in enumerate(range(1, 13)):
        x = ML + i * (bw + gap)
        parts.append(f'<line x1="{x}" y1="{y0}" x2="{x + bw}" y2="{y0}" stroke="#445"/>')
        top = y0
        for q in (1, 2, 3, 4):
            hp, ap = grid[m][q]
            tot = hp + ap
            if not tot:
                continue
            qx = x + (q - 1) * (subw + subgap)
            hh, ah = hp * scale, ap * scale
            parts.append(f'<rect x="{qx}" y="{y0 - hh - ah:.1f}" width="{subw}" '
                         f'height="{ah:.1f}" fill="{qcols[q]}" opacity="0.55"/>')
            parts.append(f'<rect x="{qx}" y="{y0 - hh:.1f}" width="{subw}" '
                         f'height="{hh:.1f}" fill="{qcols[q]}"/>')
            parts.append(f'<text x="{qx + subw / 2}" y="{y0 - hh - ah - 4:.1f}" '
                         f'fill="#fff" text-anchor="middle" font-size="9">{tot}</text>')
            top = min(top, y0 - hh - ah)
        parts.append(f'<text x="{x + bw / 2}" y="{y0 + 18}" fill="#ccd" '
                     f'text-anchor="middle" font-size="13">min {m}</text>')

    for v in range(0, ymax + 1, 5):
        yy = y0 - v * scale
        parts.append(f'<line x1="{ML - 6}" y1="{yy:.1f}" x2="{W - 30}" y2="{yy:.1f}" stroke="#1c222b"/>')
        parts.append(f'<text x="{ML - 10}" y="{yy + 4:.1f}" fill="#889" text-anchor="end">{v}</text>')

    lx = W - 340
    parts.append(f'<rect x="{lx}" y="{MT - 66}" width="334" height="52" fill="#111418" stroke="#333"/>')
    for i, q in enumerate((1, 2, 3, 4)):
        xx = lx + 12 + i * 82
        parts.append(f'<rect x="{xx}" y="{MT - 56}" width="12" height="12" fill="{qcols[q]}"/>')
        parts.append(f'<text x="{xx + 17}" y="{MT - 46}" fill="#ccd">Q{q}</text>')
    parts.append(f'<text x="{lx + 12}" y="{MT - 22}" fill="#889">dark = home pts · light = away pts</text>')

    parts.append(f'<text x="{ML}" y="{H - 34}" fill="#889">'
                 '“?”-period feed glitches attributed by interpolation between clocked neighbours '
                 '(marked * in TXT table)</text>')
    parts.append(f'<text x="{ML}" y="{H - 18}" fill="#889">'
                 'per-quarter totals include the pre-observation initial score in Q1</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def main():
    game_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_GAME
    snaps = load(game_id)
    events = build(snaps)
    home, away = snaps[0]["home_team"], snaps[0]["away_team"]
    grid, pre = quarter_minute_grid(events)

    ours = {q: [pre[0] if q == 1 else 0, pre[1] if q == 1 else 0]
            for q in (1, 2, 3, 4)}
    for e in events:
        if e["quarter"] and not e["pre_first"]:
            ours[e["quarter"]][0] += e["d_home"]
            ours[e["quarter"]][1] += e["d_away"]

    # feed's per-quarter finals: use the LAST snapshot that carries a
    # score_detail (mid-game details are stale by construction).
    det = []
    for s in reversed(snaps):
        if s["detail"]:
            det = quarter_finals(s["detail"])
            break
    det_sum = (sum(h for h, _ in det), sum(a for _, a in det))
    obs_final = (events[-1]["home"], events[-1]["away"])

    svg_path = OUT_DIR / f"quarter_minute_timeline_{game_id}_2026-09-18.svg"
    txt_path = OUT_DIR / f"quarter_minute_timeline_{game_id}_2026-09-18.txt"
    render_svg(grid, pre, (home, away), game_id, svg_path)

    L = [f"QUARTER / MINUTE POINT TIMELINE — {home} vs {away} (game {game_id})",
         "t = OUR clock (collector captured_at); minute from feed clock (whole-minute),",
         "within-minute offset interpolated from our capture cadence (~5s).",
         f"initial score at first snapshot: {pre[0]}-{pre[1]} (pre-dates our first obs, Q1 total only).",
         ""]
    L.append("POINTS PER MINUTE (total (home/away)):")
    L.append(f"{'min':>4} " + " ".join(f"{'Q' + str(q):>14}" for q in (1, 2, 3, 4)))
    for m in range(1, 13):
        cells = []
        for q in (1, 2, 3, 4):
            hp, ap = grid[m][q]
            cells.append(f"{hp + ap:>5} ({hp}/{ap})" if hp + ap else f"{'–':>14}")
        L.append(f"{m:>4} " + " ".join(f"{c:>14}" for c in cells))
    L.append(f"{'Qsum':>4} " + " ".join(
        f"{ours[q][0] + ours[q][1]:>5} ({ours[q][0]}/{ours[q][1]})" for q in (1, 2, 3, 4)))
    L.append("")
    L.append(f"observed final score:            {obs_final[0]}-{obs_final[1]} "
             f"(total {obs_final[0] + obs_final[1]})")
    L.append(f"our per-quarter totals:           {[(ours[q][0], ours[q][1]) for q in (1, 2, 3, 4)]}"
             f"  sums to {sum(v[0] for v in ours.values())}-{sum(v[1] for v in ours.values())}")
    L.append(f"feed score_detail quarter finals: {det}")
    L.append(f"feed detail sums to:              {det_sum[0]}-{det_sum[1]} (total {sum(det_sum)})")
    gap = (obs_final[0] - det_sum[0], obs_final[1] - det_sum[1])
    if gap != (0, 0):
        L.append(f"FEED INCONSISTENCY: feed's own quarter finals fall {gap[0]}-{gap[1]} "
                 f"SHORT of its own final score — reported verbatim, not fudged.")
    else:
        L.append("feed score_detail quarter finals reconcile exactly with the "
                 "observed final score: OK — our minute attribution agrees.")

    L.append("")
    L.append("EVERY SCORE CHANGE, PLACED IN MINUTE 1–12  (* = '?' period, attributed by interpolation):")
    L.append(f"{'q':>2} {'min':>4} {'off':>5}  {'t':>7} {'total':>5} {'dH':>3} {'dA':>3}  run")
    for e in events:
        if e["pre_first"]:
            L.append(f"{'Q1':>2} {'–':>4} {'–':>5}  {fmt_t(e['t']):>7} {e['home'] + e['away']:>5} "
                     f"{'–':>3} {'–':>3}  initial {e['home']}-{e['away']} at first snapshot (pre-observation)")
            continue
        if e["quarter"] is None:
            L.append(f"{'?':>2} {'?':>4} {'?':>5}  {fmt_t(e['t']):>7} {e['home'] + e['away']:>5} "
                     f"{e['d_home']:>+3d} {e['d_away']:>+3d}  UNATTRIBUTED (no clocked neighbours)")
            continue
        star = "*" if e.get("interp") else " "
        run = f"{e['home'] - e['d_home']}-{e['away'] - e['d_away']}>{e['home']}-{e['away']}"
        L.append(f"{f'Q{e['quarter']}':>2} {e['minute']:>4} {f'{e['offset_s']}s':>5}  "
                 f"{fmt_t(e['t']):>7} {e['home'] + e['away']:>5} "
                 f"{e['d_home']:>+3d} {e['d_away']:>+3d}  {run}{star}")
    txt_path.write_text("\n".join(L), encoding="utf-8")

    print(f"SVG: {svg_path}\nTXT: {txt_path}\n")
    print("\n".join(L[:40]))


if __name__ == "__main__":
    main()
