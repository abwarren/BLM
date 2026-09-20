#!/usr/bin/env python3
"""READ-ONLY — TWO ANALYSES, 2026-09-20.

A. ALL-TIME BACKTEST of the concentrated filter:
     NBA or KBL  AND  required/league_avg margin >= 1.10
   vs the unfiltered production trigger, with league baselines and Wilson
   CIs.  Production mirror: first-firing + re-firing observations (level-
   triggered), point-in-time league mean, frozen boundary settle line,
   OK finals only.

B. EUROLEAGUE AUTOPSY since 09-16: why 52.7% (below breakeven)?
   Hypotheses tested on the raw rows:
   1. margin mix — EuroLeague triggers sit in the thin band while the
      pooled edge concentrates at >=1.10
   2. line state — stale/clean split vs other leagues
   3. activation depth — EuroLeague activates later in progress (less
      remaining game to undershoot)
   4. market behaviour — mean |line move| from activation to end vs other
      leagues (did the book reprice faster there?)
   Both DBs mode=ro; the only file written is this report.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, "/home/ubuntu/BLM")

PROD = "/home/ubuntu/BLM/blm_pokerbet.db"
CLEAN_DB = "/home/ubuntu/BLM/blm_metrics_clean.db"
OUT = "/home/ubuntu/BLM/analysis_filter_backtest_euro_autopsy_2026-09-20.txt"
MARGIN = 1.04
SINCE = "2026-09-16T00:00:00Z"

_spec = importlib.util.spec_from_file_location(
    "q3_audit",
    "/home/ubuntu/BLM/scripts/audit_alert_winrate_q3break_daily_2026-09-20.py")
_q3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_q3)
_audit = _q3._audit
out, REPORT = _q3.out, _q3.REPORT
del REPORT[:]


def main():
    from blm_v4.live_analytics.under_outcome import trigger_observation

    con_p, con_c = _audit.ro(PROD), _audit.ro(CLEAN_DB)
    settled, invalid, league = _audit.load_universe(con_p, con_c)
    sigs = _audit.load_signals(con_p, con_c, settled, invalid, league)

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

    recs = []
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
        firing = []
        for o in obs:
            t = _audit.epoch_of(o["captured_at"])
            avg = league[s["slug"]].avg_before(t)
            if avg is not None and o["required_pts_per_min"] > avg * MARGIN:
                firing.append((t, o, avg))
        if not firing:
            continue
        t1, o1, a1 = firing[0]
        srows = snaps.get(s["gid"]) or []
        trig = trigger_observation(srows, 75, base["cls"]) if srows else {}
        line = _q3.fin(trig.get("total_line"))
        if line is None:
            continue
        pre = [x for x in srows
               if x.get("total_line") is not None
               and (tt := _audit.epoch_of(x.get("captured_at"))) is not None
               and tt < ra]
        end_line = _q3.fin(pre[-1]["total_line"]) if pre else None
        recs.append({
            "gid": s["gid"], "league": s["league"], "slug": s["slug"],
            "t": t1, "margin": o1["required_pts_per_min"] / a1,
            "outcome": ("under" if base["ft"] < line
                        else "over" if base["ft"] > line else "push"),
            "prog": o1["progress_pct"],
            "clean": not (o1["market_status"] == "STALE"
                          or (o1["market_age_seconds"] is not None
                              and o1["market_age_seconds"] > 300.0)),
            "line": line, "end_line": end_line, "ft": base["ft"],
        })

    def eco(rs):
        n = len(rs)
        u = sum(1 for r in rs if r["outcome"] == "under")
        o = sum(1 for r in rs if r["outcome"] == "over")
        stakes = u + o
        net = u * 0.85 - o
        roi = (100.0 * net / stakes) if stakes else None
        lo, hi = _audit.wilson(u, n)
        return n, u, o, (100.0 * u / n if n else None), net, roi, lo, hi

    def row(label, rs):
        n, u, o, wr, net, roi, lo, hi = eco(rs)
        ci = f"[{lo*100:.1f},{hi*100:.1f}%]" if n else "-"
        out(f"  {label:<40} N={n:>5}  UNDER={u:>4}  OVER={o:>4}  "
            f"win={('%6.2f%%' % wr) if wr is not None else '   n/a':>8}  "
            f"{ci:<22} net={net:+8.1f}u  ROI={('%+.2f%%' % roi) if roi is not None else 'n/a'}")

    # ══ A. ALL-TIME BACKTEST ════════════════════════════════════════
    out("=" * 112)
    out("A. ALL-TIME BACKTEST — NBA or KBL with required/avg margin >= 1.10 (concentrated filter)")
    out("   production trigger mirror, frozen boundary settle line, point-in-time league mean, OK finals.")
    out("=" * 112)
    out("")
    row("ALL production triggers (baseline)", recs)
    row("ALL CLEAN (line fresh at trigger)", [r for r in recs if r["clean"]])
    out("")
    filt = [r for r in recs if r["league"] in ("NBA", "KBL") and r["margin"] >= 1.10]
    filt_clean = [r for r in filt if r["clean"]]
    row("FILTER: NBA|KBL & margin>=1.10", filt)
    row("FILTER ... CLEAN", filt_clean)
    out("")
    out("  filter components:")
    row("    NBA & margin>=1.10", [r for r in recs if r["league"] == "NBA" and r["margin"] >= 1.10])
    row("    KBL & margin>=1.10", [r for r in recs if r["league"] == "KBL" and r["margin"] >= 1.10])
    row("    NBA & margin>=1.04 (all NBA trg)", [r for r in recs if r["league"] == "NBA"])
    row("    KBL & margin>=1.04 (all KBL trg)", [r for r in recs if r["league"] == "KBL"])
    out("")
    out("  for contrast — the filter's exclusion zone:")
    row("    other leagues, margin>=1.10", [r for r in recs if r["league"] not in ("NBA", "KBL") and r["margin"] >= 1.10])
    row("    NBA|KBL, thin band <1.10", [r for r in recs if r["league"] in ("NBA", "KBL") and r["margin"] < 1.10])
    out("")
    out("  margin-band gradient within NBA|KBL (is 1.10 doing the work?):")
    for lo, hi in ((1.04, 1.10), (1.10, 1.20), (1.20, 1.35), (1.35, 99)):
        row(f"    NBA|KBL margin [{lo},{hi})", [r for r in recs if r["league"] in ("NBA", "KBL") and lo <= r["margin"] < hi])
    out("")

    # per-era stability of the filter (3 chronological thirds)
    ts = sorted(r["t"] for r in recs)
    k = len(ts) // 3
    t1, t2 = ts[k], ts[2 * k]
    out("  chronological thirds (same windows for filter and baseline):")
    for lab, lo, hi in (("T1", ts[0], t1), ("T2", t1, t2), ("T3", t2, ts[-1] + 1)):
        row(f"    {lab} baseline (all triggers)", [r for r in recs if lo <= r["t"] < hi])
        row(f"    {lab} FILTER", [r for r in filt if lo <= r["t"] < hi])

    # ══ B. EUROLEAGUE AUTOPSY ═══════════════════════════════════════
    t0 = _audit.epoch_of(SINCE)
    since = [r for r in recs if r["t"] >= t0]
    out("")
    out("=" * 112)
    out("B. EUROLEAGUE AUTOPSY — since 09-16 (52.7%, below breakeven) vs the other leagues")
    out("=" * 112)
    eu = [r for r in since if r["league"] == "EuroLeague"]
    rest = [r for r in since if r["league"] != "EuroLeague"]

    out("")
    out("  B1. margin mix (hypothesis: EuroLeague triggers are thinner)")
    for lab, rs in (("EuroLeague", eu), ("all other leagues", rest)):
        if not rs:
            continue
        ms = [r["margin"] for r in rs]
        thin = 100.0 * sum(1 for m in ms if m < 1.10) / len(ms)
        out(f"    {lab:<20} mean={statistics.fmean(ms):.3f}  median={statistics.median(ms):.3f}  "
            f"share in thin band <1.10: {thin:.1f}%")
    row("    EuroLeague margin>=1.10", [r for r in eu if r["margin"] >= 1.10])
    row("    EuroLeague margin <1.10", [r for r in eu if r["margin"] < 1.10])
    row("    others margin>=1.10", [r for r in rest if r["margin"] >= 1.10])
    row("    others margin <1.10", [r for r in rest if r["margin"] < 1.10])

    out("")
    out("  B2. line state at trigger")
    row("    EuroLeague CLEAN", [r for r in eu if r["clean"]])
    row("    EuroLeague STALE", [r for r in eu if not r["clean"]])
    row("    others CLEAN", [r for r in rest if r["clean"]])
    row("    others STALE", [r for r in rest if not r["clean"]])

    out("")
    out("  B3. activation depth (progress at first firing observation)")
    for lab, rs in (("EuroLeague", eu), ("others", rest)):
        if not rs:
            continue
        bands = defaultdict(int)
        for r in rs:
            for lo, hi in ((75, 80), (80, 85), (85, 90), (90, 100)):
                if lo <= (r["prog"] or 0) < hi:
                    bands[f"[{lo},{hi})"] += 1
                    break
        tot = len(rs)
        out(f"    {lab:<20} " + "  ".join(
            f"{b}:{100.0*n/tot:.0f}%" for b, n in sorted(bands.items())))

    out("")
    out("  B4. market reprice (|end line - trigger line|; how far the book moved)")
    for lab, rs in (("EuroLeague", eu), ("others", rest)):
        ds = [abs(r["end_line"] - r["line"]) for r in rs
              if r["end_line"] is not None]
        if ds:
            # direction split: did the line end below the trigger (market
            # agreed with UNDER) or above (market moved against it)?
            below = sum(1 for r in rs if r["end_line"] is not None
                        and r["end_line"] < r["line"])
            out(f"    {lab:<20} mean |move|={statistics.fmean(ds):.2f} pts  "
                f"median={statistics.median(ds):.2f}  "
                f"ended below trigger: {100.0*below/len(ds):.1f}%")
    out("")
    out("  B5. EuroLeague daily since 09-16")
    days = sorted({r["t"] // 86400 for r in eu})
    for d in days:
        seg = [r for r in eu if r["t"] // 86400 == d]
        n, u, o, wr, net, roi, lo_, hi_ = eco(seg)
        import datetime as _dt
        day = _dt.datetime.fromtimestamp(d * 86400, _dt.timezone.utc).strftime("%Y-%m-%d")
        out(f"    {day}  N={n:>4}  UNDER={u:>3}  OVER={o:>3}  "
            f"win={('%5.1f%%' % wr) if wr is not None else '  n/a'}  "
            f"mean margin={statistics.fmean([r['margin'] for r in seg]):.3f}")
    out("")
    out("READ-ONLY: both DBs opened mode=ro; the only file written is this report.")
    con_p.close()
    con_c.close()
    with open(OUT, "w") as fh:
        fh.write("\n".join(REPORT) + "\n")
    print(f"\nwritten: {OUT}")


if __name__ == "__main__":
    main()
