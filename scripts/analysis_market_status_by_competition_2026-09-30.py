#!/usr/bin/env python3
"""READ-ONLY ANALYSIS — LIVE vs STALE UNDER% WITHIN each competition.

Capture-efficiency directive 2026-09-30, PHASE 7 (accuracy investigation,
SEPARATE from code).  The directive forbids changing any alert algorithm;
this script only READS.

Cohort: EXACTLY the validated trigger reconstruction from
scripts/c2_forensic_backtest_2026-09-23.py — the FIRST clean_projections
row per game with progress_pct >= 75 (< 100), non-terminal, status='VALID',
actual/required/line present.  UNDER is determined against the OK final:
    final_total < line * REQUIRED_MARGIN
with REQUIRED_MARGIN IMPORTED from production code (blm_v4.live_analytics.
under_alert) — never re-declared.

Aggregates produced:
  * per competition_slug: n, UNDER%, and WITHIN it LIVE / STALE / MISSING
    n + UNDER% (the directive's core ask — never aggregate-only)
  * per checkpoint bucket (75-80 / 80-90 / 90-100) x market status
  * per sampling-gap bucket (<10s / 10-30s / 30-60s / >60s) x market status,
    where gap = boundary captured_at minus the game's previous VALID
    projection captured_at (any progress)
  * per ISO week x market status

No DB writes (mode=ro + PRAGMA query_only), no thresholds changed, no
production code touched.  Output:
  analysis/market_status_by_competition_2026-09-30.md
"""
from __future__ import annotations

import bisect
import json
import os
import sqlite3
import sys
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from blm_v4.live_analytics.under_alert import REQUIRED_MARGIN  # noqa: E402
from blm_v4.projection import duration_for                     # noqa: E402

# Production databases live in the MAIN checkout (this script may run from
# a worktree); both are opened READ-ONLY.
MAIN = "/home/ubuntu/BLM"
CLEAN_DB = os.environ.get("BLM_CLEAN_DB", os.path.join(MAIN, "blm_metrics_clean.db"))
PROD_DB = os.environ.get("BLM_PROD_DB", os.path.join(MAIN, "blm_pokerbet.db"))
OUT_MD = os.path.join(REPO, "analysis",
                      "market_status_by_competition_2026-09-30.md")

REPORT: list[str] = []


def out(s: str = "") -> None:
    REPORT.append(s)


def ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = 1")
    conn.execute("PRAGMA busy_timeout = 8000")
    return conn


def trigger_rows(con_c: sqlite3.Connection) -> list[dict]:
    """Validated trigger reconstruction + sampling gap + status/week.

    First >=75% boundary row per game over the game's VALID projection
    stream; sampling gap = boundary captured_at minus previous VALID
    projection captured_at (any progress) for the same game.
    """
    q = ("SELECT source_game_id, captured_at, progress_pct, "
         "live_total_line, market_age_seconds, market_status "
         "FROM clean_projections "
         "WHERE (terminal IS NULL OR terminal = 0) AND status='VALID' "
         "AND actual_pts_per_min IS NOT NULL AND required_pts_per_min IS NOT NULL "
         "AND live_total_line IS NOT NULL "
         "ORDER BY source_game_id, captured_at")
    per_game: dict[str, list[tuple]] = defaultdict(list)
    for r in con_c.execute(q):
        per_game[r[0]].append(r)
    triggers: list[dict] = []
    for gid, rows in per_game.items():
        prev_ts = None
        for r in rows:
            progress = r[2]
            if progress is not None and 75.0 <= float(progress) < 100.0:
                gap = None
                if prev_ts is not None:
                    gap = _ts_diff_s(prev_ts, r[1])
                triggers.append({
                    "game_id": gid, "captured_at": r[1],
                    "progress_pct": float(progress), "line": r[3],
                    "market_age_s": r[4], "market_status": r[5],
                    "gap_s": gap,
                })
                break
            prev_ts = r[1]
    return triggers


def _ts_diff_s(a: str, b: str) -> float:
    from datetime import datetime
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    try:
        ta = datetime.strptime(a, fmt)
        tb = datetime.strptime(b, fmt)
    except ValueError:
        ta = datetime.fromisoformat(a.replace("Z", "+00:00"))
        tb = datetime.fromisoformat(b.replace("Z", "+00:00"))
    return (tb - ta).total_seconds()


def load_finals(con_p: sqlite3.Connection) -> dict[str, float]:
    """gid -> final_total (OK finals only)."""
    q = ("SELECT source_game_id, final_total FROM game_results "
         "WHERE final_result_status='OK' AND final_total IS NOT NULL "
         "AND final_total > 0")
    return {r[0]: float(r[1]) for r in con_p.execute(q)}


def load_slugs(con_p: sqlite3.Connection) -> dict[str, str]:
    q = ("SELECT source_game_id, competition_slug FROM games "
         "WHERE competition_slug IS NOT NULL AND competition_slug <> ''")
    return {r[0]: r[1] for r in con_p.execute(q)}


def iso_week(ts: str) -> str:
    from datetime import datetime
    try:
        d = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"


def pct(hits: int, n: int) -> str:
    return f"{(100.0 * hits / n):.1f}%" if n else "n/a"


def main() -> None:
    con_c = ro(CLEAN_DB)
    con_p = ro(PROD_DB)
    triggers = trigger_rows(con_c)
    finals = load_finals(con_p)
    slugs = load_slugs(con_p)
    con_c.close()
    con_p.close()

    # Attach outcome + dimensions
    rows: list[dict] = []
    for t in triggers:
        fin = finals.get(t["game_id"])
        if fin is None or t["line"] is None or t["line"] <= 0:
            continue
        under = fin < t["line"] * REQUIRED_MARGIN
        rows.append({
            **t,
            "slug": slugs.get(t["game_id"], "UNKNOWN"),
            "week": iso_week(t["captured_at"]),
            "under": under,
            "ckpt": ("75-80" if t["progress_pct"] < 80 else
                     "80-90" if t["progress_pct"] < 90 else "90-100"),
            "gap_b": ("n/a" if t["gap_s"] is None else
                      "<10s" if t["gap_s"] < 10 else
                      "10-30s" if t["gap_s"] < 30 else
                      "30-60s" if t["gap_s"] < 60 else ">60s"),
        })

    out("# LIVE vs STALE UNDER% WITHIN each competition — 2026-09-30")
    out()
    out("Directive 2026-09-30 PHASE 7 (read-only; alert algorithm untouched).")
    out(f"Trigger cohort: validated first->=75% boundary reconstruction "
        f"(same as c2_forensic_backtest_2026-09-23). REQUIRED_MARGIN = "
        f"{REQUIRED_MARGIN} (IMPORTED). UNDER = final_total < line * margin.")
    out(f"Settled triggers analysed: **{len(rows)}** "
        f"(unsettled/missing-final triggers excluded).")
    out()

    def table(title: str, keyfn, order=None) -> None:
        out(f"## {title}")
        out()
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            groups[keyfn(r)].append(r)
        keys = order or sorted(groups)
        out("| bucket | n | UNDER% | LIVE n | LIVE UNDER% | "
            "STALE n | STALE UNDER% | MISSING n | MISSING UNDER% |")
        out("|---|---|---|---|---|---|---|---|---|")
        for k in keys:
            g = groups.get(k) or []
            if not g:
                continue
            live = [r for r in g if r["market_status"] == "LIVE"]
            stale = [r for r in g if r["market_status"] == "STALE"]
            miss = [r for r in g if r["market_status"] not in ("LIVE", "STALE")]
            out(f"| {k} | {len(g)} | {pct(sum(r['under'] for r in g), len(g))} "
                f"| {len(live)} | {pct(sum(r['under'] for r in live), len(live))} "
                f"| {len(stale)} | {pct(sum(r['under'] for r in stale), len(stale))} "
                f"| {len(miss)} | {pct(sum(r['under'] for r in miss), len(miss))} |")
        out()

    table("BY COMPETITION (LIVE vs STALE WITHIN each)",
          lambda r: r["slug"])
    table("BY CHECKPOINT x market status", lambda r: r["ckpt"],
          order=["75-80", "80-90", "90-100"])
    table("BY SAMPLING GAP x market status", lambda r: r["gap_b"],
          order=["n/a", "<10s", "10-30s", "30-60s", ">60s"])
    table("BY ISO WEEK x market status", lambda r: r["week"])

    # LIVE vs STALE paired within competition — the directive's headline
    out("## Headline: LIVE vs STALE WITHIN competition")
    out()
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["slug"]].append(r)
    out("| competition | LIVE UNDER% | STALE UNDER% | delta (STALE-LIVE) | "
        "LIVE n | STALE n |")
    out("|---|---|---|---|---|---|")
    for slug in sorted(groups):
        g = groups[slug]
        live = [r for r in g if r["market_status"] == "LIVE"]
        stale = [r for r in g if r["market_status"] == "STALE"]
        if not live or not stale:
            out(f"| {slug} | {pct(sum(r['under'] for r in live), len(live))} "
                f"| {pct(sum(r['under'] for r in stale), len(stale))} | n/a "
                f"| {len(live)} | {len(stale)} |")
            continue
        lu = 100.0 * sum(r["under"] for r in live) / len(live)
        su = 100.0 * sum(r["under"] for r in stale) / len(stale)
        out(f"| {slug} | {lu:.1f}% | {su:.1f}% | {su - lu:+.1f}pp "
            f"| {len(live)} | {len(stale)} |")
    out()
    out("_Read:_ the aggregate STALE>LIVE gradient must be judged WITHIN "
        "each competition; a positive delta in a competition with healthy "
        "n means staleness is NOT neutral there. n is small per cell — "
        "treat differences under ~10 triggers as noise.")
    out()

    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    with open(OUT_MD, "w") as f:
        f.write("\n".join(REPORT))
    print(f"wrote {OUT_MD}")
    print(json.dumps({
        "triggers": len(triggers), "settled": len(rows),
        "required_margin": REQUIRED_MARGIN,
    }))


if __name__ == "__main__":
    main()
