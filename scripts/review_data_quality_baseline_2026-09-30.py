#!/usr/bin/env python3
"""READ-ONLY DATA-QUALITY BASELINE + capture-health vs accuracy correlation.

Final pre-merge review task (2026-09-30): quantify the BEFORE picture the
capture-efficiency PR targets, and test HISTORICALLY whether degraded alert
accuracy correlates with incomplete/incorrect captures.  The PR is NOT
deployed, so there is no 'after' yet — this records the baseline the
post-deploy re-run must be compared against.

No DB writes anywhere (mode=ro + PRAGMA query_only=ON).  Output:
  analysis/data_quality_baseline_2026-09-30.md
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

MAIN = "/home/ubuntu/BLM"
PROD_DB = os.environ.get("BLM_PROD_DB", os.path.join(MAIN, "blm_pokerbet.db"))
CLEAN_DB = os.environ.get("BLM_CLEAN_DB", os.path.join(MAIN, "blm_metrics_clean.db"))
OUT_MD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "analysis", "data_quality_baseline_2026-09-30.md")

REPORT: list[str] = []


def out(s: str = "") -> None:
    REPORT.append(s)


def ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = 1")
    conn.execute("PRAGMA busy_timeout = 8000")
    return conn


def pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def day_window(days_back: int) -> tuple[str, str]:
    d0 = datetime.now(timezone.utc) - timedelta(days=days_back)
    d1 = d0 + timedelta(days=1)
    fmt = "%Y-%m-%dT00:00"
    return d0.strftime(fmt), d1.strftime(fmt)


def main() -> None:
    con = ro(PROD_DB)
    conc = ro(CLEAN_DB)

    out("# Data-quality baseline (BEFORE capture-efficiency PR) — 2026-09-30")
    out()
    out(f"Generated: {now_iso()}Z.  READ-ONLY (mode=ro + query_only).  The PR is")
    out("NOT deployed; this is the quantified BEFORE the post-deploy re-run")
    out("must be compared against.  All windows use ISO-'T' string compares.")
    out()

    # ── A. per-day capture-quality table (last 7 days) ──────────────
    out("## A. Capture quality per day (production DB)")
    out()
    out("| day | snaps | NULL-score% | NULL-quarter% | line-present% | "
        "games w/ market-obs% | ended games | final-window coverage% | "
        "post-final snaps (60min) |")
    out("|---|---|---|---|---|---|---|---|---|")
    per_day = {}
    for d in range(6, -1, -1):
        t0, t1 = day_window(d)
        day = t0[:10]
        snaps, nulls, nullq, linep = con.execute(
            "SELECT COUNT(*),"
            " SUM(CASE WHEN home_score IS NULL OR away_score IS NULL THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN quarter IS NULL THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN total_line IS NOT NULL THEN 1 ELSE 0 END)"
            " FROM snapshots WHERE captured_at>=? AND captured_at<?",
            (t0, t1)).fetchone()
        gsnaps = con.execute(
            "SELECT COUNT(DISTINCT source_game_id) FROM snapshots"
            " WHERE captured_at>=? AND captured_at<?", (t0, t1)).fetchone()[0]
        gmkt = con.execute(
            "SELECT COUNT(DISTINCT m.source_game_id) FROM market_observations m"
            " WHERE m.captured_at>=? AND m.captured_at<? AND m.market_type='MatchTotal'",
            (t0, t1)).fetchone()[0]
        ended = con.execute(
            "SELECT COUNT(*) FROM game_results gr"
            " WHERE gr.result_at>=? AND gr.result_at<?",
            (t0, t1)).fetchone()[0]
        cov = con.execute(
            "SELECT COUNT(*) FROM game_results gr WHERE gr.result_at>=? AND gr.result_at<?"
            " AND EXISTS (SELECT 1 FROM snapshots s WHERE s.source_game_id=gr.source_game_id"
            "             AND s.captured_at >= datetime(gr.result_at,'-10 minutes')"
            "             AND s.captured_at <= gr.result_at)",
            (t0, t1)).fetchone()[0]
        post = con.execute(
            "SELECT COUNT(*) FROM snapshots s JOIN game_results gr"
            " ON gr.source_game_id=s.source_game_id"
            " WHERE gr.result_at>=? AND gr.result_at<?"
            " AND s.captured_at > gr.result_at"
            " AND s.captured_at <= datetime(gr.result_at,'+60 minutes')",
            (t0, t1)).fetchone()[0]
        per_day[day] = (snaps, nulls, nullq, linep, gsnaps, gmkt, ended, cov, post)
        out(f"| {day} | {snaps} | {pct(nulls or 0, snaps)} | "
            f"{pct(nullq or 0, snaps)} | {pct(linep or 0, snaps)} | "
            f"{pct(gmkt, gsnaps)} | {ended} | {pct(cov, ended)} | {post} |")
    out()
    out("_Notes:_ NULL-quarter% is a board-context proxy (the list page renders")
    out(" quarter blank on degenerate rows).  final-window coverage = ended")
    out(" games having >=1 snapshot in their last 10 min before result_at.")
    out(" post-final snaps = capture WASTE the PR's DONE-lifecycle eliminates")
    out(" (WS-bridge frames after result_at).  Journal tick percentiles are")
    out(" reported separately below (not reconstructible per day from the DB).")
    out()

    # ── B. current collector tick cadence (journal, last 6h) ────────
    out("## B. Collector tick cadence (journal --user, last 6h, current PID)")
    out()
    try:
        txt = subprocess.run(
            ["journalctl", "--user", "-u", "blm-collector", "--since", "-6h",
             "--no-pager"],
            capture_output=True, text=True, timeout=60).stdout
        import re
        xs = sorted(float(m) for m in re.findall(
            r"fast tick \d+ done in ([0-9.]+)s", txt))
        if xs:
            n = len(xs)

            def p(q):
                return xs[min(n - 1, int(round(q * (n - 1))))]
            over = sum(1 for x in xs if x > 3.0)
            out(f"- n={n} ticks: P50={p(.5):.2f}s P90={p(.9):.2f}s "
                f"P95={p(.95):.2f}s P99={p(.99):.2f}s MAX={xs[-1]:.2f}s")
            out(f"- ticks whose TOTAL exceeded the 3.0s page-capture budget: "
                f"{over} ({100.0 * over / n:.1f}%)")
        else:
            out("- no tick lines found in the journal window")
    except Exception as e:
        out(f"- journal read failed: {e}")
    out()

    # ── C. reconciliation frequency (schema-tolerant) ───────────────
    out("## C. Reconciliation activity (last 7 days)")
    out()
    cols = [r[1] for r in con.execute("PRAGMA table_info(reconciliation)")]
    out(f"- reconciliation columns: {cols}")
    try:
        tscol = next((c for c in cols if c in ("created_at", "reconciled_at",
                                               "captured_at", "ts", "updated_at")), None)
        if tscol:
            t0, _ = day_window(6)
            n = con.execute(
                f"SELECT COUNT(*) FROM reconciliation WHERE {tscol}>=?",
                (t0,)).fetchone()[0]
            out(f"- reconciliation rows last 7d (by {tscol}): {n}")
        else:
            n = con.execute("SELECT COUNT(*) FROM reconciliation").fetchone()[0]
            out(f"- no usable timestamp column; reconciliation rows total: {n}")
    except Exception as e:
        out(f"- reconciliation query failed: {e}")
    out()

    # ── D. capture health vs alert accuracy (trigger level) ─────────
    out("## D. Does capture health correlate with accuracy?  (trigger level,")
    out("## first->=75% cohort vs OK finals; alert math untouched)")
    out()
    q = ("SELECT source_game_id, captured_at, progress_pct, live_total_line,"
         " market_age_seconds, market_status FROM clean_projections"
         " WHERE (terminal IS NULL OR terminal = 0) AND status='VALID'"
         " AND actual_pts_per_min IS NOT NULL AND required_pts_per_min IS NOT NULL"
         " AND live_total_line IS NOT NULL AND progress_pct >= 75"
         " AND progress_pct < 100 ORDER BY source_game_id, captured_at")
    first: dict[str, tuple] = {}
    for r in conc.execute(q):
        first.setdefault(r[0], r)
    finals = {r[0]: r[1] for r in con.execute(
        "SELECT source_game_id, final_total FROM game_results"
        " WHERE final_result_status='OK' AND final_total IS NOT NULL"
        " AND final_total > 0")}
    conc.execute("PRAGMA query_only = 1")
    sys.path.insert(0, MAIN)
    from blm_v4.live_analytics.under_alert import REQUIRED_MARGIN  # noqa: E402

    def bucket_age(age):
        if age is None:
            return "no-age"
        if age < 30:
            return "<30s"
        if age < 120:
            return "30-120s"
        return ">120s"

    by_status: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_age: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    unsettled = 0
    for gid, r in first.items():
        fin = finals.get(gid)
        if fin is None:
            unsettled += 1
            continue
        under = fin < r[3] * REQUIRED_MARGIN
        st = by_status[r[5] or "MISSING"]
        st[0] += 1
        st[1] += 1 if under else 0
        ab = by_age[bucket_age(r[4])]
        ab[0] += 1
        ab[1] += 1 if under else 0
    out(f"REQUIRED_MARGIN = {REQUIRED_MARGIN} (imported).  Triggers: "
        f"{len(first)}, settled: {len(first) - unsettled}, unsettled: {unsettled}.")
    out()
    out("| market_status | n | UNDER% |")
    out("|---|---|---|")
    for k in sorted(by_status):
        n, u = by_status[k]
        out(f"| {k} | {n} | {pct(u, n)} |")
    out()
    out("| market age | n | UNDER% |")
    out("|---|---|---|")
    for k in ("<30s", "30-120s", ">120s", "no-age"):
        if k in by_age:
            n, u = by_age[k]
            out(f"| {k} | {n} | {pct(u, n)} |")
    out()
    out("_Read:_ at the 75% boundary, fresher captures (LIVE status, younger")
    out(" market age) showing HIGHER UNDER% means accuracy tracks capture")
    out(" quality — i.e. improving capture efficiency directly improves the")
    out(" data feeding the (unchanged) alert.  This refines the Phase 7")
    out(" finding (LIVE>STALE within every competition by 7-11pp).")
    out()

    con.close()
    conc.close()
    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    with open(OUT_MD, "w") as f:
        f.write("\n".join(REPORT))
    print(f"wrote {OUT_MD}")
    print(json.dumps({"days": 7, "required_margin": REQUIRED_MARGIN}))


if __name__ == "__main__":
    main()
