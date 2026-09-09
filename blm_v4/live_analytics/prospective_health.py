"""Prospective health report — §10 monitoring under the FROZEN architecture.

This module is OBSERVATIONAL INFRASTRUCTURE ONLY.  It accumulates and
describes the prospective dataset; it never evaluates, predicts, or
optimises.  All semantics (pace formulas, benchmark key, progress
buckets, MIN_BENCHMARK_N, league classification) are consumed from the
authoritative frozen modules — never reimplemented here.

By construction the report CANNOT contain edge / win-rate /
profitability / signal / staking / EV / probability fields: the
``assert_descriptive_only`` audit walks the whole payload and raises if
any forbidden key appears.  Data-quality checks below (§7) are
integrity controls, not prediction logic.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, List, Optional

from blm_v4.live_analytics.benchmark import MIN_BENCHMARK_N
from blm_v4.live_analytics.league import (PROVIDERS, UNKNOWN,
                                          canonical_competition)
from blm_v4.live_analytics.pace import benchmark_key

# §10: these concepts MUST NOT exist in this phase — structurally banned.
FORBIDDEN_KEYS = {"edge", "win_rate", "profitability", "signal",
                  "staking", "ev", "expected_value", "probability",
                  "threshold", "betting"}

# Append-only snapshot history (directive: append each daily snapshot,
# never rewrite prior snapshots).  DURABLE repository-local default —
# survives reboot; the scheduler honours an explicit
# PROSPECTIVE_HISTORY override.
HISTORY_FILE = "/home/ubuntu/BLM/prospective_health_history.jsonl"

# Longitudinal maturation watch: lifetime populations below this N are
# tracked per-key in each snapshot (their N recorded over time) so the
# maturation audit can report time-to-maturity.  Populations at or above
# the threshold have matured past the watch band and drop out of the
# per-key list (their crossing stays bounded by the last sighting).
WATCH_THRESHOLD_N = 100

_TS_FORMATS = ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
               "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S")


def assert_descriptive_only(payload: Any, _path: str = "") -> None:
    """Raise if any forbidden concept name appears anywhere in payload."""
    if isinstance(payload, dict):
        for k, v in payload.items():
            p = "%s.%s" % (_path, k)
            if str(k).lower() in FORBIDDEN_KEYS:
                raise ValueError("forbidden concept in health report: %s" % p)
            assert_descriptive_only(v, p)
    elif isinstance(payload, (list, tuple)):
        for i, v in enumerate(payload):
            assert_descriptive_only(v, "%s[%d]" % (_path, i))


def _ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    for f in _TS_FORMATS:
        try:
            return datetime.strptime(s.strip(), f).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _pct(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return round(xs[0], 4)
    i = q / 100.0 * (len(xs) - 1)
    lo, hi = int(i), min(int(i) + 1, len(xs) - 1)
    frac = i - lo
    return round(xs[lo] * (1 - frac) + xs[hi] * frac, 4)


def _dist(values: List[float]) -> Dict[str, Any]:
    return {"n": len(values),
            "p05": _pct(values, 5), "p25": _pct(values, 25),
            "p50": _pct(values, 50), "p75": _pct(values, 75),
            "p95": _pct(values, 95)}


_ROW_SQL = """
SELECT id, source_game_id, classification, captured_at, period_label,
       progress_pct, current_total_points, live_total_line,
       market_captured_at, market_status, actual_pts_per_min,
       required_pts_per_min, pace_gap, elapsed_game_minutes, terminal
  FROM clean_projections
 WHERE captured_at >= ? AND captured_at <= ? AND status = 'VALID'
"""


def build_report(main_db: str, clean_db: str, now: datetime,
                 window_hours: float = 24.0,
                 api_url: Optional[str] = None,
                 history_file: Optional[str] = None) -> Dict[str, Any]:
    """Build the §10 prospective health report (read-only)."""
    end = now
    start = now - timedelta(hours=window_hours)
    lo, hi = _iso(start), _iso(end)

    mc = sqlite3.connect("file:%s?mode=ro" % main_db, uri=True, timeout=30)
    cc = sqlite3.connect("file:%s?mode=ro" % clean_db, uri=True, timeout=30)
    mc.row_factory = sqlite3.Row
    cc.row_factory = sqlite3.Row
    try:
        slug_of, cls_of = {}, {}
        for gid, cls, slug in mc.execute(
                "SELECT source_game_id, classification, competition_slug "
                "FROM games").fetchall():
            slug_of[str(gid)] = (slug or "").strip() or None
            cls_of[str(gid)] = cls

        rows = [dict(r) for r in cc.execute(_ROW_SQL, (lo, hi)).fetchall()]
        for r in rows:
            r["source_game_id"] = str(r["source_game_id"])

        # ── §6 accumulation ─────────────────────────────────────────────
        games = sorted({r["source_game_id"] for r in rows})
        prov_counts: Counter = Counter()
        comp_counts: Counter = Counter()
        unknown_games = []
        canon = {}
        for g in games:
            res = canonical_competition(cls_of.get(g),
                                        slug_of.get(g), None)
            canon[g] = res
            prov_counts[res.provider or UNKNOWN] += 1
            comp_counts[res.competition or UNKNOWN] += 1
            if res.status != "classified":
                unknown_games.append(g)

        # ── §5 benchmark populations (via the FROZEN key function) ──────
        key_comps: Dict[str, set] = defaultdict(set)
        resolvable = 0
        for r in rows:
            res = canon.get(r["source_game_id"])
            if res is None or not res.provider or not res.competition:
                continue  # UNKNOWN: benchmark-ineligible by design
            k = benchmark_key(res.provider, res.competition,
                              r["progress_pct"], r["period_label"])
            if k:
                resolvable += 1
                r["_has_key"] = True
                key_comps[k].add(res.competition)
        isolation_violations = sorted(
            k for k, comps in key_comps.items() if len(comps) > 1)

        # Population sizes over the FULL history (what the live service
        # would see as N for each window key).  The clean DB has no
        # `games` table, so aggregate per game there and map each game to
        # its authoritative competition slug (main DB) in Python — same
        # bucket arithmetic and eligibility as pace.benchmark_key
        # (0–100 progress only, quarter periods only).
        _Q = {"1st Quarter": "Q1", "2nd Quarter": "Q2",
              "3rd Quarter": "Q3", "4th Quarter": "Q4"}
        pop_n: Dict[str, int] = {}
        for fam, gid, per, bucket, n in cc.execute(
                """
                SELECT classification, source_game_id, period_label,
                       CAST(CAST(progress_pct / 5 AS INT) * 5 AS INT) AS bucket,
                       COUNT(*)
                  FROM clean_projections
                 WHERE status='VALID' AND actual_pts_per_min IS NOT NULL
                   AND progress_pct BETWEEN 0 AND 100
                   AND period_label IN ('1st Quarter','2nd Quarter',
                                        '3rd Quarter','4th Quarter')
                 GROUP BY classification, source_game_id, period_label, bucket
                """).fetchall():
            prov = PROVIDERS.get(fam)
            slug = slug_of.get(str(gid))
            if not prov or not slug or slug.upper() == UNKNOWN:
                continue
            key = "%s|%s|%s|P%03d" % (prov, slug, _Q.get(per, per), bucket)
            pop_n[key] = pop_n.get(key, 0) + n
        ns = sorted(pop_n.values())
        top_keys = sorted(pop_n.items(), key=lambda kv: -kv[1])[:10]
        watch_populations = sorted(
            ({"key": k, "n": n} for k, n in pop_n.items()
             if n < WATCH_THRESHOLD_N),
            key=lambda x: (x["n"], x["key"]))

        # ── DATA MATURITY COUNTS ONLY (never "statistical significance") ─
        maturity = {str(t): sum(1 for n in ns if n >= t)
                    for t in (30, 100, 500, 1000)}

        # ── COMPETITION MATURATION (descriptive data-quality only) ──────
        comp_of = {g: (res.competition or UNKNOWN)
                   for g, res in canon.items()}
        comp_obs: Counter = Counter()
        comp_games: Dict[str, set] = defaultdict(set)
        comp_noline: Counter = Counter()
        comp_linerows: Counter = Counter()
        comp_stale: Counter = Counter()
        comp_resolvable: Counter = Counter()
        for r in rows:
            c = comp_of.get(r["source_game_id"], UNKNOWN)
            comp_obs[c] += 1
            comp_games[c].add(r["source_game_id"])
            if r["live_total_line"] is None:
                comp_noline[c] += 1
            else:
                comp_linerows[c] += 1
                if r["market_status"] == "STALE":
                    comp_stale[c] += 1
            if r.get("_has_key"):
                comp_resolvable[c] += 1
        comp_keys: Counter = Counter()
        comp_ns: Dict[str, List[int]] = defaultdict(list)
        # both counts derive from the LIFETIME populations (pop_n) so a
        # competition's population count is consistent with its median/
        # min/max N (window key_comps remain a separate health metric)
        for k, n in pop_n.items():
            parts = k.split("|")
            if len(parts) == 4:
                comp_keys[parts[1]] += 1
                comp_ns[parts[1]].append(n)
        comp_rows = []
        for c in sorted(comp_games):
            nsl = comp_ns.get(c, [])
            lr = comp_linerows.get(c, 0)
            prov_c = None
            for g in comp_games[c]:
                res = canon.get(g)
                if res and res.provider:
                    prov_c = res.provider
                    break
            comp_rows.append({
                "provider": prov_c or UNKNOWN,
                "competition": c,
                "games": len(comp_games[c]),
                "observations": comp_obs.get(c, 0),
                "benchmark_keys": comp_keys.get(c, 0),
                "median_benchmark_n": (int(median(nsl)) if nsl else 0),
                "min_benchmark_n": (min(nsl) if nsl else 0),
                "max_benchmark_n": (max(nsl) if nsl else 0),
                # DATA MATURITY COUNTS ONLY (never statistical significance)
                "maturity_n_at_least": {str(t): sum(1 for n in nsl
                                                    if n >= t)
                                        for t in (30, 100, 500, 1000)},
                "z_availability": (round(comp_resolvable.get(c, 0)
                                         / comp_obs.get(c, 1), 4)
                                   if comp_obs.get(c) else None),
                "missing_line_rate": (round(comp_noline.get(c, 0)
                                            / comp_obs.get(c, 1), 4)
                                      if comp_obs.get(c) else None),
                "stale_line_rate": (round(comp_stale.get(c, 0) / lr, 4)
                                    if lr else None),
            })

        # ── §7 integrity controls ───────────────────────────────────────
        seen: Counter = Counter()
        for r in rows:
            seen[(r["source_game_id"], r["captured_at"])] += 1
        duplicate_groups = sum(1 for c in seen.values() if c > 1)

        future_rows = 0
        market_after_rows = 0
        for r in rows:
            t = _ts(r["captured_at"])
            if t and t > end + timedelta(seconds=300):
                future_rows += 1
            mt = _ts(r["market_captured_at"])
            if t and mt and mt > t:
                market_after_rows += 1

        by_game: Dict[str, List[dict]] = defaultdict(list)
        for r in rows:
            by_game[r["source_game_id"]].append(r)
        score_violations = 0
        max_drop = 0.0
        elapsed_violations = 0
        # §CLOCK JITTER — record elapsed-time micro-drops honestly;
        # timestamps are NEVER altered to force monotonicity.
        jitter_events: List[dict] = []
        for g, rs in by_game.items():
            rs.sort(key=lambda x: (x["captured_at"], x["id"]))
            prev_s = prev_e = None
            prev_period = None
            for r in rs:
                s, e = r["current_total_points"], r["elapsed_game_minutes"]
                if s is not None and prev_s is not None and s < prev_s:
                    score_violations += 1
                    max_drop = max(max_drop, prev_s - s)
                if e is not None and prev_e is not None and e < prev_e:
                    elapsed_violations += 1
                    res = canon.get(g)
                    jitter_events.append({
                        "game": g,
                        "competition": (res.competition if res else None)
                                       or UNKNOWN,
                        "drop_minutes": round(prev_e - e, 4),
                        "same_quarter": (prev_period == r["period_label"]),
                        "score_monotonic": not (s is not None
                                                and prev_s is not None
                                                and s < prev_s),
                    })
                prev_s = s if s is not None else prev_s
                prev_e = e if e is not None else prev_e
                prev_period = r["period_label"]

        alt_groups = cc.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT source_game_id, market_captured_at
                FROM clean_projections
               WHERE captured_at >= ? AND captured_at <= ?
                 AND status='VALID' AND live_total_line IS NOT NULL
                 AND market_captured_at IS NOT NULL
               GROUP BY source_game_id, market_captured_at
              HAVING COUNT(DISTINCT live_total_line) > 1)
            """, (lo, hi)).fetchone()[0]

        line_rows = [r for r in rows if r["live_total_line"] is not None]
        missing_rate = (round(1 - len(line_rows) / len(rows), 4)
                        if rows else None)
        stale_rate = (round(sum(1 for r in line_rows
                                if r["market_status"] == "STALE")
                            / len(line_rows), 4)
                      if line_rows else None)

        # ── §2/§3/§4 descriptive distributions (observed values only) ──
        gap = [round(float(r["current_total_points"]) - float(r["live_total_line"]), 1)
               for r in line_rows if r["current_total_points"] is not None]
        ap = [float(r["actual_pts_per_min"]) for r in rows
              if r["actual_pts_per_min"] is not None]
        rp = [float(r["required_pts_per_min"]) for r in rows
              if r["required_pts_per_min"] is not None]
        pg = [float(r["pace_gap"]) for r in rows
              if r["pace_gap"] is not None]

        # ── LIFETIME TOTALS + GROWTH vs the previous snapshot ───────────
        lrow = cc.execute(
            "SELECT COUNT(*), COUNT(DISTINCT source_game_id), "
            "MAX(captured_at) FROM clean_projections WHERE status='VALID'"
        ).fetchone()
        lifetime = {"observations": lrow[0] or 0,
                    "games": lrow[1] or 0,
                    "latest_captured_at": lrow[2]}
        prev = _prev_snapshot(history_file)
        growth = None
        if prev:
            try:
                prev_acc = prev.get("accumulation") or {}
                prev_obs = prev_acc.get("lifetime_observations")
                prev_games = prev_acc.get("lifetime_games")
                # honest baseline: a pre-accumulation snapshot (no lifetime
                # data) is NOT a baseline — no fabricated delta is computed
                if prev_obs is not None and prev_games is not None:
                    growth = {
                        "previous_snapshot_utc": prev.get("generated_utc"),
                        "observations_added": (lifetime["observations"]
                                               - prev_obs),
                        "games_added": (lifetime["games"] - prev_games),
                    }
            except Exception:
                growth = None

        report: Dict[str, Any] = {
            "generated_utc": _iso(end),
            "window_hours": window_hours,
            "window_start_utc": lo,
            "window_end_utc": hi,
            "games_observed": len(games),
            "observations_collected": len(rows),
            "provider_counts": dict(prov_counts),
            "competition_counts": dict(comp_counts),
            "unknown_count": len(unknown_games),
            "benchmark_populations": {
                "window_keys": len(key_comps),
                "keys_meeting_min_n": sum(1 for n in ns
                                          if n >= MIN_BENCHMARK_N),
                "median_population_n": (int(median(ns)) if ns else 0),
                "min_benchmark_n": MIN_BENCHMARK_N,
                "n_distribution": _dist([float(n) for n in ns]),
                "largest_keys": [{"key": k, "n": n} for k, n in top_keys],
            },
            # Longitudinal maturation watch (descriptive tracking only):
            # per-key N for every lifetime population still below the
            # watch threshold.  Populations never altered, never merged.
            "maturation_watch": {
                "watch_threshold_n": WATCH_THRESHOLD_N,
                "populations": watch_populations,
            },
            "z_availability": {
                "valid_rows": len(rows),
                "rows_with_resolvable_benchmark_key": resolvable,
                "availability_rate": (round(resolvable / len(rows), 4)
                                      if rows else None),
            },
            "missing_line_rate": missing_rate,
            "stale_line_rate": stale_rate,
            "score_line_gap_distribution": _dist(gap),
            "actual_pace_distribution": _dist(ap),
            "required_pace_distribution": _dist(rp),
            "pace_gap_distribution": _dist(pg),
            "integrity": {
                "unknown_classification_games": len(unknown_games),
                "duplicate_observation_groups": duplicate_groups,
                "future_timestamp_rows": future_rows,
                "market_ts_after_capture_rows": market_after_rows,
                "score_monotonicity_violations": score_violations,
                "max_score_drop": round(max_drop, 1),
                "elapsed_monotonicity_violations": elapsed_violations,
                "benchmark_isolation_violations": isolation_violations,
                "alternative_line_groups": alt_groups,
            },
            "api_db_integrity": _api_probe(api_url, mc, cc) if api_url
            else "not requested (pass api_url to probe)",
            "api_ui_integrity": "run the browser acceptance suite "
                                "separately (not part of this report)",
            # ── maturation / accumulation sections (descriptive only) ──
            "benchmark_maturity": {
                "note": "DATA MATURITY COUNTS ONLY — not statistical "
                        "significance; sparse populations reported "
                        "honestly, never altered",
                "populations_total": len(ns),
                "n_at_least": maturity,
            },
            "competition_maturation": comp_rows,
            "clock_jitter": {
                "note": "elapsed-time micro-drops are recorded, never "
                        "corrected; timestamps are never altered to "
                        "force monotonicity",
                "micro_drops": elapsed_violations,
                "min_drop_minutes": (min((j["drop_minutes"] for j in
                                          jitter_events), default=None)),
                "max_drop_minutes": (max((j["drop_minutes"] for j in
                                          jitter_events), default=None)),
                "affected_competitions": sorted({j["competition"]
                                                 for j in jitter_events}),
                "affected_games": len({j["game"] for j in jitter_events}),
                "same_quarter": sum(1 for j in jitter_events
                                    if j["same_quarter"]),
                "period_transition": sum(1 for j in jitter_events
                                         if not j["same_quarter"]),
                "score_remained_monotonic": sum(
                    1 for j in jitter_events if j["score_monotonic"]),
            },
            "accumulation": {
                "lifetime_observations": lifetime["observations"],
                "lifetime_games": lifetime["games"],
                "latest_captured_at": lifetime["latest_captured_at"],
                "growth_vs_previous_snapshot": growth,
            },
        }
        assert_descriptive_only(report)
        return report
    finally:
        mc.close()
        cc.close()


def _prev_snapshot(history_file: Optional[str] = None) -> Optional[dict]:
    """The most recent snapshot from the append-only history (or None).
    The history is APPEND-ONLY: prior snapshots are never rewritten.
    Read only when an explicit history file is given (CLI path), so the
    report builder itself stays pure/environment-independent."""
    if not history_file:
        return None
    try:
        with open(history_file) as fh:
            last = None
            for line in fh:
                line = line.strip()
                if line:
                    last = line
        return json.loads(last) if last else None
    except Exception:
        return None


def _api_probe(api_url: str, mc: sqlite3.Connection,
               cc: sqlite3.Connection) -> str:
    """Optional API↔DB spot-check (same two-path rule as api.py)."""
    try:
        import urllib.request
        with urllib.request.urlopen(api_url + "/api/v4/live",
                                    timeout=20) as resp:
            games = [g for g in json.load(resp).get("games", [])
                     if g.get("live") or g.get("status") == "live"]
    except Exception as e:
        return "skipped: API unreachable (%s)" % str(e)[:80]
    ok = 0
    for g in games[:6]:
        gid = str(g.get("game_id"))
        srow = cc.execute(
            "SELECT live_total_line, market_captured_at FROM clean_projections "
            "WHERE source_game_id=? AND live_total_line IS NOT NULL "
            "ORDER BY market_captured_at DESC LIMIT 1", (gid,)).fetchone()
        wrow = mc.execute(
            "SELECT line_value, captured_at FROM market_observations "
            "WHERE source_game_id=? AND market_type='MatchTotal' "
            "AND line_value IS NOT NULL AND captured_at = (SELECT MAX(captured_at) "
            "FROM market_observations WHERE source_game_id=? AND "
            "market_type='MatchTotal' AND line_value IS NOT NULL) "
            "ORDER BY line_value ASC LIMIT 1", (gid, gid)).fetchone()
        db_line, db_ts = (srow[0], srow[1]) if srow else (None, None)
        if wrow and (db_ts is None or wrow[1] > db_ts):
            db_line = wrow[0]
        api_line = (g.get("market", {}) or {}).get("total_line")
        if api_line is None or db_line is None \
                or abs(float(api_line) - float(db_line)) <= 0.001:
            ok += 1
    return "ok x%d of %d sampled" % (ok, min(6, len(games)))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Prospective health report (frozen descriptive "
                    "architecture — §10 monitoring only)")
    ap.add_argument("--main-db", default="/home/ubuntu/BLM/blm_pokerbet.db")
    ap.add_argument("--clean-db",
                    default="/home/ubuntu/BLM/blm_metrics_clean.db")
    ap.add_argument("--window-hours", type=float, default=24.0)
    ap.add_argument("--api-url", default="http://127.0.0.1:8901")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--jsonl-append", default=None,
                    help="append this report as one JSON line (history)")
    args = ap.parse_args()
    rep = build_report(args.main_db, args.clean_db,
                       datetime.now(timezone.utc), args.window_hours,
                       args.api_url or None,
                       history_file=args.jsonl_append)
    text = json.dumps(rep, indent=2, default=str)
    print(text)
    if args.json_out:
        with open(args.json_out, "w") as fh:
            fh.write(text + "\n")
    if args.jsonl_append:
        # append-only: the snapshot is written only after the guard and
        # every integrity check inside build_report have passed
        with open(args.jsonl_append, "a") as fh:
            fh.write(json.dumps(rep, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
