#!/usr/bin/env python3
"""Point timeline for one game, on OUR clock (collector captured_at).

Reads snapshots from blm_pokerbet.db (read-only), diffs total/home/away
scores between consecutive scored snapshots, and renders:

  1. an SVG step chart  (x = T+ seconds on our clock, y = points)
  2. an ASCII step chart (same axes, for the terminal)
  3. an event table     (every observed score change, first-seen time)

The provider's game clock is ignored entirely: t=0 is the first scored
snapshot our collector captured. Points are bucketed at the first
snapshot where the new score was observed (cadence ~5s -> that is the
resolution of "exact time").

Usage:
  python3 scripts/point_timeline_2026-09-18.py [source_game_id]
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "blm_pokerbet.db"
OUT_DIR = Path(__file__).resolve().parent.parent

DEFAULT_GAME = "30949595"  # Los Angeles Lakers @ Charlotte Hornets, 2026-09-18


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def load_snapshots(game_id: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    try:
        rows = conn.execute(
            """SELECT captured_at, home_team, away_team, home_score, away_score,
                      period_label, clock, total_line
                 FROM snapshots
                WHERE source_game_id = ?
                  AND home_score IS NOT NULL AND away_score IS NOT NULL
                ORDER BY captured_at, id""",
            (game_id,),
        ).fetchall()
        meta = conn.execute(
            """SELECT home_team, away_team, competition
                 FROM games WHERE source_game_id = ?""",
            (game_id,),
        ).fetchone()
    finally:
        conn.close()
    if not rows:
        sys.exit(f"No scored snapshots for game {game_id!r} in {DB}")
    snaps = []
    for r in rows:
        snaps.append({
            "captured_at": r[0],
            "home_team": r[1],
            "away_team": r[2],
            "home": r[3],
            "away": r[4],
            "period": r[5] or "?",
            "clock": r[6],
            "line": r[7],
        })
    return snaps, meta


def build_events(snaps: list[dict]) -> list[dict]:
    """Consecutive score changes, bucketed at first-seen on our clock."""
    t0 = parse_ts(snaps[0]["captured_at"])
    events, prev = [], None
    for s in snaps:
        key = (s["home"], s["away"])
        if prev is not None and key == prev["key"]:
            prev["line"] = s["line"]  # keep freshest line at same score
            continue
        dt = (parse_ts(s["captured_at"]) - t0).total_seconds()
        ev = {
            "t": dt,
            "captured_at": s["captured_at"],
            "home": s["home"],
            "away": s["away"],
            "total": s["home"] + s["away"],
            "period": s["period"],
            "clock": s["clock"],
            "line": s["line"],
            "prev": prev["ev"] if prev else None,
        }
        if prev is not None:
            ev["d_home"] = s["home"] - prev["ev"]["home"]
            ev["d_away"] = s["away"] - prev["ev"]["away"]
        events.append(ev)
        prev = {"key": key, "ev": ev}
    return events


def fmt_t(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    return f"T+{m:02d}:{s:02d}"


# ── SVG ───────────────────────────────────────────────────────────────

def render_svg(events: list[dict], meta, game_id: str, out: Path) -> None:
    W, H = 1400, 700
    ML, MR, MT, MB = 70, 30, 80, 60
    pw, ph = W - ML - MR, H - MT - MB
    t_max = max(e["t"] for e in events) or 1.0
    y_max = max(e["total"] for e in events) + 8
    x = lambda t: ML + (t / t_max) * pw
    y = lambda v: MT + ph - (v / y_max) * ph

    def poly(vals_getter):
        pts = []
        for e in events:
            pts.append(f"{x(e['t']):.1f},{y(vals_getter(e)):.1f}")
        return " ".join(pts)

    home, away = meta[0], meta[1]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             'font-family="monospace" font-size="12">']
    parts.append(f'<rect width="{W}" height="{H}" fill="#111418"/>')
    parts.append(
        f'<text x="{ML}" y="28" fill="#fff" font-size="16" font-weight="bold">'
        f'{home} vs {away} — points on OUR clock (collector captured_at)</text>')
    parts.append(f'<text x="{ML}" y="48" fill="#889">game {game_id} · '
                 f'{len(events)} score states · t=0 = first scored snapshot · '
                 'resolution = collector cadence (~5s)</text>')

    # gridlines + y labels
    step = 10 if y_max > 60 else 5
    v = 0
    while v <= y_max:
        parts.append(f'<line x1="{ML}" y1="{y(v):.1f}" x2="{W-MR}" y2="{y(v):.1f}" '
                     f'stroke="#222831"/>')
        parts.append(f'<text x="{ML-8}" y="{y(v)+4:.1f}" fill="#889" text-anchor="end">{v}</text>')
        v += step

    # quarter boundaries (period changes between events)
    prev_p = None
    for e in events:
        if e["period"] != prev_p:
            if prev_p is not None:
                parts.append(f'<line x1="{x(e["t"]):.1f}" y1="{MT}" x2="{x(e["t"]):.1f}" '
                             f'y2="{MT+ph}" stroke="#445" stroke-dasharray="4 4"/>')
                parts.append(f'<text x="{x(e["t"])+4:.1f}" y="{MT+14}" fill="#667">{e["period"]}</text>')
            prev_p = e["period"]

    # x tick labels every 5 min
    tm = 0
    while tm <= t_max:
        parts.append(f'<line x1="{x(tm):.1f}" y1="{MT+ph}" x2="{x(tm):.1f}" '
                     f'y2="{MT+ph+5}" stroke="#445"/>')
        parts.append(f'<text x="{x(tm):.1f}" y="{MT+ph+20}" fill="#889" '
                     f'text-anchor="middle">{fmt_t(tm)}</text>')
        tm += 300

    # live total line (market), dotted, where known
    pts = [f"{x(e['t']):.1f},{y(e['line']):.1f}" for e in events if e["line"] is not None]
    if pts:
        parts.append(f'<polyline points="{" ".join(pts)}" fill="none" '
                     'stroke="#d8a028" stroke-width="1.2" stroke-dasharray="3 4" opacity="0.85"/>')

    # step lines: total (white), home (cyan), away (magenta)
    parts.append(f'<polyline points="{poly(lambda e: e["total"])}" fill="none" '
                 'stroke="#e8e8e8" stroke-width="2.2"/>')
    parts.append(f'<polyline points="{poly(lambda e: e["home"])}" fill="none" '
                 'stroke="#4fc3f7" stroke-width="1.4"/>')
    parts.append(f'<polyline points="{poly(lambda e: e["away"])}" fill="none" '
                 'stroke="#f48fb1" stroke-width="1.4"/>')

    # score-change markers on the total line
    for e in events:
        if e["prev"] is not None:
            parts.append(f'<circle cx="{x(e["t"]):.1f}" cy="{y(e["total"]):.1f}" '
                         'r="2.6" fill="#ffca28"/>')

    # legend
    lx = W - MR - 260
    parts.append(f'<rect x="{lx}" y="{MT+6}" width="254" height="58" fill="#111418" stroke="#333"/>')
    for i, (col, label) in enumerate([
            ("#e8e8e8", "total (home+away)"),
            ("#4fc3f7", f"home — {home}"),
            ("#f48fb1", f"away — {away}"),
            ("#d8a028", "market total line")]):
        yy = MT + 20 + i * 13
        dash = ' stroke-dasharray="3 4"' if i == 3 else ""
        parts.append(f'<line x1="{lx+8}" y1="{yy}" x2="{lx+38}" y2="{yy}" '
                     f'stroke="{col}" stroke-width="2"{dash}/>')
        parts.append(f'<text x="{lx+44}" y="{yy+4}" fill="#ccd">{label}</text>')

    parts.append(f'<text x="{ML}" y="{H-18}" fill="#667">gold dots = observed score changes · '
                 'vertical dashes = quarter changes · times are elapsed on our clock</text>')
    parts.append("</svg>")
    out.write_text("\n".join(parts), encoding="utf-8")


# ── ASCII ─────────────────────────────────────────────────────────────

def render_ascii(events: list[dict], cols: int = 100, rows: int = 24) -> str:
    t_max = max(e["t"] for e in events) or 1.0
    y_max = max(e["total"] for e in events)
    grid = [[" "] * cols for _ in range(rows)]

    def cell(t: float, v: float) -> tuple[int, int]:
        c = min(cols - 1, int(t / t_max * (cols - 1)))
        r = min(rows - 1, int(v / max(y_max, 1) * (rows - 1)))
        return rows - 1 - r, c

    prev_pt = None
    for e in events:
        r, c = cell(e["t"], e["total"])
        if prev_pt:
            pr, pc = prev_pt
            for cc in range(pc, c + 1):
                rr = pr + (r - pr) * (cc - pc) // max(c - pc, 1)
                if grid[rr][cc] == " ":
                    grid[rr][cc] = "·"
        grid[r][c] = "●"
        prev_pt = (r, c)

    out = []
    for i, rowvals in enumerate(grid):
        v = round(y_max - (i / (rows - 1)) * y_max)
        out.append(f"{v:4d} |{''.join(rowvals)}")
    out.append("     +" + "-" * cols)
    out.append("      " + "".join(
        "|" if (i * (cols - 1)) % 60 < 1 else
        ("+" if i and (i * t_max / (cols - 1)) % 300 < t_max / (cols - 1) else " ")
        for i in range(cols)))
    out.append(f"      0:00{' ' * (cols // 2 - 5)}{fmt_t(t_max)} (our clock)")
    return "\n".join(out)


# ── main ──────────────────────────────────────────────────────────────

def main() -> None:
    game_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_GAME
    snaps, meta = load_snapshots(game_id)
    events = build_events(snaps)
    home, away, comp = meta[0], meta[1], meta[2]

    svg_path = OUT_DIR / f"point_timeline_{game_id}_2026-09-18.svg"
    txt_path = OUT_DIR / f"point_timeline_{game_id}_2026-09-18.txt"
    render_svg(events, meta, game_id, svg_path)

    lines = []
    lines.append(f"POINT TIMELINE — {home} vs {away}  ({comp})  game {game_id}")
    lines.append(f"t=0 = first scored snapshot on OUR clock "
                 f"({events[0]['captured_at']}); provider clock ignored.")
    lines.append(f"scored snapshots: {len(snaps)}  ·  score states: {len(events)}  ·  "
                 f"final {events[-1]['home']}-{events[-1]['away']} "
                 f"(total {events[-1]['total']})")
    lines.append("")
    lines.append(render_ascii(events))
    lines.append("")
    lines.append("EVERY OBSERVED SCORE CHANGE (first-seen on our clock):")
    lines.append(f"{'t':>7}  {'total':>5}  {'dH':>3} {'dA':>3}  {'run':>11}  "
                 f"{'period':<6} {'clock':>6}  line")
    for e in events:
        if e["prev"] is None:
            run = f"{e['home']}-{e['away']}"
            dh = da = "–"
        else:
            dh, da = f"{e['d_home']:+d}", f"{e['d_away']:+d}"
            run = f"{e['prev']['home']}-{e['prev']['away']}>{e['home']}-{e['away']}"
        ln = f"{e['line']:.1f}" if e["line"] is not None else "–"
        lines.append(f"{fmt_t(e['t']):>7}  {e['total']:>5}  {dh:>3} {da:>3}  "
                     f"{run:>11}  {e['period']:<6} {e['clock'] or '–':>6}  {ln}")
    txt_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"SVG: {svg_path}")
    print(f"TXT: {txt_path}")
    print()
    print("\n".join(lines[:40]))


if __name__ == "__main__":
    main()
