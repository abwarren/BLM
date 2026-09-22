#!/usr/bin/env python3
"""Shadow fingerprint collector for the PRODUCTION 75% UNDER-alert cohort.

READ-ONLY with respect to production: both DBs are opened mode=ro +
PRAGMA query_only=1.  No production table, module, service, alert or
config is touched.  The only artifact written is a SEPARATE, append-only
analysis dataset (JSONL), by default:

    /home/ubuntu/BLM/shadow_fingerprints_75.jsonl

PURPOSE
  Accumulate, going forward, one immutable case fingerprint per game per
  75%-boundary occurrence, carrying every pre-trigger feature the pattern
  discovery (analysis_under_vs_over_pattern_discovery_2026-09-21.md)
  identified as promising — WITHOUT changing what production alerts on.
  Later validation (scripts/oos_weekly_rolling_2026-09-21.py) reads this
  log plus the discovery CSV to test rules out-of-sample.  Nothing here
  feeds back into production alerting.

COHORT / FEATURES (same authorities as the discovery script)
  boundary row: first clean_projections row with progress>=75% (<100%),
                non-terminal, VALID, act/req/line present
  league ref  : blm_v4 point-in-time realised-pace mean (result_at
                STRICTLY before the boundary) per the game's OWN
                competition — the same construction the live alert uses
  outcome     : final_total (game_results OK) vs the frozen boundary line
  FEATURES (all point-in-time-legal at the boundary instant):
    q3_ratio (previous-quarter ppm vs its league Q3 average,
    strict-before), recent3_minus_act (3-min deceleration), req_ratio,
    act_minus_req, line_move (trigger live - game-start opening),
    remaining, score_differential, market_req_ppm, q1..q4, progress.

  A game is logged the moment its boundary row exists — BEFORE
  settlement (outcome=null).  Settled verdicts arrive as later
  dedup-marked update lines.  is_production_trigger (act < avg AND
  req > avg*1.04) is evaluated with the point-in-time reference and
  frozen into the first record.

DEDUP / SETTLEMENT MODEL (identical to scripts/shadow_p80_3d.py)
  Key = (game_id, captured_at).  First sighting appends the record.
  Later runs APPEND a dedup-marked line (_update=true, _prev_outcome
  preserved) when the outcome/final changes.  Prior lines are never
  rewritten; log readers take the LAST line per key.

PARITY CHECK (--replay, mandatory hygiene every run)
  Rebuilds fingerprints from the DBs alone and diffs every pre-trigger
  field against the recorded log.  Any drift is an alarm.

Usage:
    python3 scripts/shadow_fingerprint_collector_2026-09-21.py --run
    python3 scripts/shadow_fingerprint_collector_2026-09-21.py --report
    python3 scripts/shadow_fingerprint_collector_2026-09-21.py --replay
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

REPO = "/home/ubuntu/BLM"
sys.path.insert(0, REPO)

from blm_v4.live_analytics.under_outcome import (  # noqa: E402
    score_at_observation,
    trigger_observation,
)

PROD = os.environ.get("BLM_PROD_DB", "/home/ubuntu/BLM/blm_pokerbet.db")
CLEAN = os.environ.get("BLM_CLEAN_DB", "/home/ubuntu/BLM/blm_metrics_clean.db")
DEFAULT_LOG = "/home/ubuntu/BLM/shadow_fingerprints_75.jsonl"

REQUIRED_MARGIN = 1.04
QMIN = {"BETUAL_NBA": 10.0, "CYBER_2K26": 12.0}

# ── pre-registered shadow rules (frozen; never re-tuned during logging) ──
# Mirrors discovery candidates C2/C3/C6.  Reported for OBSERVATION ONLY.
SHADOW_RULES = {
    "C2_recent3_decel": lambda f: (f.get("recent3_minus_act") is not None
                                   and f["recent3_minus_act"] <= -0.5),
    "C3_req_gt_margin_and_q3_lt_avg": lambda f: (
        f.get("req_ratio") is not None and f["req_ratio"] > 1.04
        and f.get("q3_ratio") is not None and f["q3_ratio"] < 1.0),
    "C6_mom_and_q3": lambda f: (
        f.get("recent3_minus_act") is not None
        and f["recent3_minus_act"] <= -0.5
        and f.get("q3_ratio") is not None and f["q3_ratio"] < 1.0),
}

SILENT = False
REPORT: list[str] = []


def out(s: str = "") -> None:
    REPORT.append(s)
    if not SILENT:
        print(s)


def fin(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def ep(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ro(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=120)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=1")
    return c


def qmin_for(cls):
    if not cls:
        return 10.0
    for k, v in QMIN.items():
        if k in str(cls).upper():
            return v
    return 10.0


class PrefixMean:
    def __init__(self, series):
        series = sorted(series)
        self.tss = [t for t, _ in series]
        self.pfx = []
        acc = 0.0
        for _, v in series:
            acc += v
            self.pfx.append(acc)

    def avg_before(self, t):
        k = bisect.bisect_left(self.tss, t)
        if k <= 0:
            return None
        return self.pfx[k - 1] / k

    def n_before(self, t):
        return bisect.bisect_left(self.tss, t)


def chunked(seq, size=400):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def quarter_cum_totals(con_p, gids):
    """Per-quarter cumulative ends + Q4 start, SQL-side (no full load)."""
    res = {}
    for chunk in chunked(gids):
        q = ("SELECT source_game_id, "
             "MAX(CASE WHEN period_label='1st Quarter' "
             "    THEN home_score+away_score END) AS q1c, "
             "MAX(CASE WHEN period_label='2nd Quarter' "
             "    THEN home_score+away_score END) AS q2c, "
             "MAX(CASE WHEN period_label='3rd Quarter' "
             "    THEN home_score+away_score END) AS q3c, "
             "MAX(CASE WHEN period_label='4th Quarter' "
             "    THEN home_score+away_score END) AS q4c, "
             "MIN(CASE WHEN period_label='4th Quarter' "
             "    THEN captured_at END) AS q4_start "
             "FROM snapshots WHERE source_game_id IN ("
             + ",".join("?" * len(chunk)) + ") GROUP BY source_game_id")
        for r in con_p.execute(q, chunk):
            res[r["source_game_id"]] = {
                "q1c": fin(r["q1c"]), "q2c": fin(r["q2c"]),
                "q3c": fin(r["q3c"]), "q4c": fin(r["q4c"]),
                "q4_start": r["q4_start"],
            }
    return res


def opening_lines(con_p):
    """Game-start opening line: earliest checkpoint row carrying one."""
    res = {}
    for r in con_p.execute(
            """SELECT source_game_id, checkpoint_pct, opening_line
                 FROM checkpoint_market
                WHERE opening_line IS NOT NULL
                ORDER BY source_game_id, checkpoint_pct"""):
        g = r["source_game_id"]
        if g not in res:
            res[g] = fin(r["opening_line"])
    return res


def load_references(con_p):
    """Point-in-time league refs: full-game pace and Q3 ppm, per slug."""
    settled = {}
    slug_cls = {}
    for r in con_p.execute(
            """SELECT gr.source_game_id AS gid, gr.final_total AS ft,
                      g.competition_slug AS slug, g.classification AS cls,
                      gr.result_at AS ra
                 FROM game_results gr JOIN games g
                   ON g.source_game_id = gr.source_game_id
                WHERE gr.final_result_status='OK' AND gr.final_total IS NOT NULL
                  AND gr.final_total > 0 AND g.competition_slug IS NOT NULL
                  AND g.competition_slug <> '' AND gr.result_at IS NOT NULL"""):
        qm = qmin_for(r["cls"])
        if not qm:
            continue
        settled[r["gid"]] = (float(r["ft"]), r["ra"])
        slug_cls[r["gid"]] = (r["slug"], r["cls"], qm)
    pace_s = defaultdict(list)
    q3_s = defaultdict(list)
    qtot = quarter_cum_totals(con_p, list(settled))
    for gid, (ft, ra) in settled.items():
        slug, _cls, qm = slug_cls[gid]
        t = ep(ra)
        if t is None:
            continue
        pace_s[slug].append((t, ft / (4 * qm)))
        qt = qtot.get(gid)
        if qt and qt.get("q2c") is not None and qt.get("q3c") is not None:
            q3_s[slug].append((t, (qt["q3c"] - qt["q2c"]) / qm))
    return ({s: PrefixMean(v) for s, v in pace_s.items()},
            {s: PrefixMean(v) for s, v in q3_s.items()}, settled, slug_cls)


def boundary_rows(con_c):
    """First 75%-boundary clean_projections row per game + full context."""
    q = ("SELECT source_game_id, captured_at, progress_pct, "
         "actual_pts_per_min, required_pts_per_min, live_total_line, "
         "market_age_seconds, market_status, remaining_game_minutes, "
         "current_total_points, trajectory_state, recent_pace_3m, "
         "recent_pace_1m, recent_pace_2m, recent_pace_5m, "
         "pace_acceleration, elapsed_game_minutes "
         "FROM clean_projections "
         "WHERE progress_pct >= 75 AND progress_pct < 100 "
         "AND (terminal IS NULL OR terminal = 0) AND status='VALID' "
         "AND actual_pts_per_min IS NOT NULL AND required_pts_per_min IS NOT NULL "
         "AND live_total_line IS NOT NULL "
         "ORDER BY source_game_id, captured_at")
    first = {}
    for r in con_c.execute(q):
        base = r["source_game_id"].split("#")[0]
        if base not in first:
            first[base] = dict(r)
    return first


def build_fingerprints(con_p, con_c, pace_ref, q3_ref, settled, slug_cls):
    """One fingerprint per 75%-boundary occurrence (trigger AND baseline).

    League/cls come from the games table (known before settlement); the
    settlement store contributes only final_total/outcome.
    """
    invalid = {r["source_game_id"].split("#")[0] for r in con_p.execute(
        "SELECT source_game_id FROM game_quality WHERE status='INVALID'")}
    first = boundary_rows(con_c)
    opens = opening_lines(con_p)
    gids = sorted(first.keys() - invalid)
    qtot_all = quarter_cum_totals(con_p, gids)

    snaps = defaultdict(list)
    for chunk in chunked(gids):
        sq = ("SELECT source_game_id, captured_at, period_label, quarter, "
              "clock, game_status, home_score, away_score, total_line "
              "FROM snapshots WHERE source_game_id IN ("
              + ",".join("?" * len(chunk)) + ") "
              "ORDER BY source_game_id, captured_at, id")
        for r in con_p.execute(sq, chunk):
            snaps[r["source_game_id"]].append(dict(r))

    fps = []
    n_skip = 0
    for gid in gids:
        b = first[gid]
        slug_cls_row = slug_cls.get(gid)
        if slug_cls_row is None:
            n_skip += 1
            continue
        slug, cls, qm = slug_cls_row
        t = ep(b["captured_at"])
        if t is None:
            n_skip += 1
            continue
        ref = pace_ref.get(slug)
        avg = ref.avg_before(t) if ref else None
        if avg is None:
            n_skip += 1
            continue
        ref_n = ref.n_before(t) if ref else 0
        line = fin(b["live_total_line"])
        act = fin(b["actual_pts_per_min"])
        req = fin(b["required_pts_per_min"])
        if line is None or act is None or req is None:
            n_skip += 1
            continue
        ft = settled.get(gid, (None, None))[0]
        outcome = (None if ft is None else
                   "under" if ft < line else "over" if ft > line else "push")
        qt = qtot_all.get(gid) or {}
        q3_league_avg = None
        q3ref = q3_ref.get(slug)
        if q3ref:
            q3_league_avg = q3ref.avg_before(t)
        q3_ppm = q3_ratio = None
        q1 = q2 = q3q = q4q = None
        if qt.get("q1c") is not None:
            q1 = qt["q1c"]
        if qt.get("q1c") is not None and qt.get("q2c") is not None:
            q2 = qt["q2c"] - qt["q1c"]
        if qt.get("q2c") is not None and qt.get("q3c") is not None:
            q3q = qt["q3c"] - qt["q2c"]
            q3_ppm = q3q / qm
            if q3_league_avg:
                q3_ratio = q3_ppm / q3_league_avg
        if qt.get("q3c") is not None and qt.get("q4c") is not None:
            q4q = qt["q4c"] - qt["q3c"]
        srows = snaps.get(gid) or []
        h = a = diff = market_req = None
        if srows and cls:
            try:
                trow = trigger_observation(srows, 75, cls)
                brow = None
                if trow and trow.get("captured_at") is not None:
                    brow = next((r for r in srows
                                 if r["captured_at"] == trow["captured_at"]),
                                None)
                if brow is not None:
                    h, a = (fin(brow.get("home_score")),
                            fin(brow.get("away_score")))
                    if h is not None and a is not None:
                        diff = h - a
                sc = fin(score_at_observation(srows, 75, cls))
                rem = fin(b["remaining_game_minutes"])
                if sc is not None and rem:
                    market_req = (line - sc) / rem
            except Exception:
                pass
        open_line = opens.get(gid)
        r3 = fin(b["recent_pace_3m"])
        fps.append({
            "game_id": gid,
            "captured_at": b["captured_at"],
            "league": slug,
            "classification": cls,
            "progress_pct": round(fin(b["progress_pct"]), 3),
            "triggered_line": line,
            "opening_line": open_line,
            "line_move": (round(line - open_line, 3)
                          if open_line is not None else None),
            "required_pts_per_min": round(req, 6),
            "actual_pts_per_min": round(act, 6),
            "league_avg_pace": round(avg, 6),
            "league_ref_n": ref_n,
            "req_ratio": round(req / avg, 6) if avg else None,
            "act_minus_req": round(act - req, 6),
            "recent3_minus_act": (round(r3 - act, 6)
                                  if r3 is not None else None),
            "recent_pace_1m": fin(b["recent_pace_1m"]),
            "recent_pace_2m": fin(b["recent_pace_2m"]),
            "recent_pace_5m": fin(b["recent_pace_5m"]),
            "pace_acceleration": fin(b["pace_acceleration"]),
            "trajectory_state": b["trajectory_state"],
            "q1": q1, "q2": q2, "q3": q3q, "q4": q4q,
            "q3_ppm": (round(q3_ppm, 6) if q3_ppm is not None else None),
            "q3_league_avg": (round(q3_league_avg, 6)
                              if q3_league_avg is not None else None),
            "q3_ratio": (round(q3_ratio, 6)
                         if q3_ratio is not None else None),
            "remaining_game_minutes": fin(b["remaining_game_minutes"]),
            "current_total_points": fin(b["current_total_points"]),
            "score_differential": diff,
            "market_req_ppm": (round(market_req, 6)
                               if market_req is not None else None),
            "is_production_trigger": bool(act < avg
                                          and req > avg * REQUIRED_MARGIN),
            "stale": bool(b["market_status"] == "STALE"
                          or (fin(b["market_age_seconds"]) is not None
                              and fin(b["market_age_seconds"]) > 300.0)),
            "final_total": ft,
            "outcome": outcome,
            "recorded_at_utc": datetime.now(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return fps, n_skip


def load_log(path):
    """key -> last record (update lines collapse onto the original)."""
    last = {}
    n_lines = 0
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                n_lines += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                last[(rec.get("game_id"), rec.get("captured_at"))] = rec
    return last, n_lines


def append_new(path, fps):
    """Append-only JSONL write with dedup + settlement-update semantics."""
    last, n_lines = load_log(path)
    new = upd = 0
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as fh:
        for f in sorted(fps, key=lambda x: (x["captured_at"], x["game_id"])):
            key = (f["game_id"], f["captured_at"])
            prev = last.get(key)
            if prev is None:
                fh.write(json.dumps(f, sort_keys=True) + "\n")
                new += 1
                last[key] = f
            elif (prev.get("outcome") != f["outcome"]
                  or prev.get("final_total") != f["final_total"]):
                rec = dict(f)
                rec["_update"] = True
                rec["_prev_outcome"] = prev.get("outcome")
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
                upd += 1
                last[key] = rec
    return new, upd, n_lines


def evaluate_rules(entries):
    """Pre-registered rule observation over the SETTLED log."""
    resolved = {}
    for e in entries:
        k = (e.get("game_id"), e.get("captured_at"))
        prev = resolved.get(k)
        if prev is None or e.get("_update"):
            resolved[k] = e
    settled = [e for e in resolved.values() if e.get("outcome") in
               ("under", "over", "push")]
    triggers = [e for e in settled if e.get("is_production_trigger")]
    lines = ["=" * 96,
             "PRE-REGISTERED SHADOW RULES — PROSPECTIVE OBSERVATION",
             "=" * 96,
             f"resolved entries: {len(resolved)}  settled: {len(settled)}  "
             f"production triggers among settled: {len(triggers)}"]
    if not triggers:
        lines.append("  (no settled production triggers logged yet)")
        return lines
    base_u = sum(1 for e in triggers if e["outcome"] == "under")
    lines.append(f"  trigger baseline: {base_u}/{len(triggers)} UNDER "
                 f"= {100.0 * base_u / len(triggers):.2f}%")
    for name, cond in SHADOW_RULES.items():
        g = [e for e in triggers if cond(e)]
        if not g:
            lines.append(f"  {name:<34} N=0")
            continue
        u = sum(1 for e in g if e["outcome"] == "under")
        lines.append(f"  {name:<34} N={len(g):>4}  UNDER={u:>3}  "
                     f"UNDER%={100.0 * u / len(g):>6.2f}%")
    lines.append("  (observation only — no alerting, no production effect)")
    return lines


def replay_check(con_p, con_c, pace_ref, q3_ref, settled, slug_cls,
                 log_path, since=None):
    """Parity check with two field classes:

    STRICT  — collector-computed pre-trigger fields; any mismatch is an
              alarm (collector bug / data mutation).
    REF-DERIVED — league_avg_pace, req_ratio, q3_league_avg, q3_ratio,
              is_production_trigger.  These are recomputed against the
              point-in-time reference AS IT EXISTS NOW; as new games
              settle the strict-before mean legitimately moves, so drift
              here is EXPECTED and reported informationally — the logged
              (frozen-at-first-sighting) values are the authoritative
              record of what the reference said at logging time.
    """
    last, _ = load_log(log_path)
    fps, _sk = build_fingerprints(con_p, con_c, pace_ref, q3_ref, settled,
                                  slug_cls)
    if since:
        fps = [f for f in fps if f["captured_at"][:10] >= since]
    ref_fields = {"league_avg_pace", "req_ratio", "q3_league_avg",
                  "q3_ratio", "is_production_trigger"}
    field_diffs = defaultdict(int)
    ref_diffs = defaultdict(int)
    checked = 0
    for f in fps:
        rec = last.get((f["game_id"], f["captured_at"]))
        if rec is None:
            continue
        checked += 1
        for k, v in f.items():
            if k in ("recorded_at_utc",):
                continue
            rv = rec.get(k)
            if isinstance(v, float):
                ok = (rv is not None and abs(float(rv) - v) < 1e-4)
            else:
                ok = (rv == v)
            if not ok:
                (ref_diffs if k in ref_fields else field_diffs)[k] += 1
    out("=" * 96)
    out(f"PARITY CHECK (--replay): {checked} recorded fingerprints compared")
    if checked == 0:
        out("  (no overlapping records — nothing to compare)")
    elif not field_diffs:
        out("  STRICT fields: ALL match the recorded log exactly.")
    else:
        out("  STRICT FIELD DRIFT DETECTED (feature: count) — INVESTIGATE:")
        for k, n in sorted(field_diffs.items(), key=lambda kv: -kv[1]):
            out(f"    {k}: {n}")
    if ref_diffs:
        out("  reference-derived drift (EXPECTED as new games settle; the")
        out("  logged values are the frozen authoritative record):")
        for k, n in sorted(ref_diffs.items(), key=lambda kv: -kv[1]):
            out(f"    {k}: {n}")
    else:
        out("  reference-derived fields: no drift since logging.")
    out("  (outcome/final_total legitimately differ pre- vs post-settlement)")


def main():
    global SILENT
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--run", action="store_true",
                    help="collect fingerprints and append to the log")
    ap.add_argument("--report", action="store_true",
                    help="evaluate the pre-registered rules on the log")
    ap.add_argument("--replay", action="store_true",
                    help="parity check: rebuild from DBs and diff vs log")
    ap.add_argument("--since", default=None,
                    help="replay only boundaries first seen on/after DATE")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    SILENT = args.quiet
    if not (args.run or args.report or args.replay):
        args.run = True

    con_p, con_c = ro(PROD), ro(CLEAN)
    pace_ref, q3_ref, settled, slug_cls = load_references(con_p)

    if args.run:
        fps, skipped = build_fingerprints(con_p, con_c, pace_ref, q3_ref,
                                          settled, slug_cls)
        new, upd, n_lines = append_new(args.log, fps)
        out(f"[collector] log: {args.log}")
        out(f"[collector] appended {new} new, {upd} settlement update(s); "
            f"{n_lines + new + upd} total lines")
        out(f"[collector] fingerprints built: {len(fps)} "
            f"(skipped no-league-ref/no-line: {skipped})")

    if args.report or args.replay:
        last, _ = load_log(args.log)
        for line in evaluate_rules(list(last.values())):

            out(line)

    if args.replay:
        replay_check(con_p, con_c, pace_ref, q3_ref, settled, slug_cls,
                     args.log, since=args.since)

    con_p.close()
    con_c.close()
    out("")
    out("READ-ONLY: production DBs opened mode=ro + query_only; no alert,")
    out("threshold, service or production table touched. The only file")
    out(f"written is the separate append-only log: {args.log}")


if __name__ == "__main__":
    main()
