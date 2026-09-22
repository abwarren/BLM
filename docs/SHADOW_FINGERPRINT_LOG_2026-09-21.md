# Shadow Fingerprint Log — Design (READ-ONLY, 2026-09-21)

**Status:** designed + first backfill appended.  **No production change.**
The production UNDER alert (`blm_v4/live_analytics/under_alert.py`), its
thresholds, and every service are untouched.  This document specifies the
observation layer only.

## 1. Purpose

The 2026-09-21 pattern discovery
(`analysis_under_vs_over_pattern_discovery_2026-09-21.md`) found strong
full-history separators of UNDER vs OVER trigger outcomes — Q3 below its
league average, 3-minute deceleration, and their combinations (+19..+23pp
lift, N=61–122) — but the **holdout could not confirm them** (fixed
partitions left N=9–46 per partition).  The only way to decide is
prospective accumulation: record every trigger fingerprint from now on,
with its eventual settlement, until the pre-registered rules have enough
out-of-sample N to judge.

This log is that accumulation.  It changes nothing about what fires.

## 2. What is collected

One JSONL record per **(game, 75%-boundary occurrence)** — both
production triggers and non-triggers (the baseline is required for lift
calculation).  Pre-trigger fields, all provable at the boundary instant:

| field | authority |
|---|---|
| `game_id`, `captured_at`, `progress_pct` | boundary row identity |
| `league`, `classification` | `games` (known pre-settlement) |
| `triggered_line` | frozen live line at-or-before the boundary (canonical store) |
| `opening_line`, `line_move` | `checkpoint_market` game-start opening |
| `required_pts_per_min`, `actual_pts_per_min`, `league_avg_pace`, `req_ratio`, `act_minus_req` | boundary row + point-in-time strict-before league mean |
| `recent3_minus_act`, `recent_pace_1m/2m/5m`, `pace_acceleration`, `trajectory_state` | trailing windows ending at the boundary row |
| `q1, q2, q3, q4, q3_ppm, q3_league_avg, q3_ratio` | per-quarter cumulative snapshot maxima; Q3 ref strict-before |
| `remaining_game_minutes`, `current_total_points`, `score_differential`, `market_req_ppm` | boundary row + boundary snapshot scores |
| `is_production_trigger` | act < avg AND req > avg×1.04 (strict), point-in-time |
| `stale` | line freshness at the boundary (LIVE ≤300s = clean) |
| `final_total`, `outcome` | `game_results` OK settlement — **null until settled** |

## 3. Read-only guarantees (frozen in code)

- both DBs opened `file:...?mode=ro` **and** `PRAGMA query_only=1`
- the only file written is the separate log:
  `/home/ubuntu/BLM/shadow_fingerprints_75.jsonl`
- append-only + dedup by `(game_id, captured_at)`; settlement updates are
  appended as dedup-marked lines (`_update: true`, `_prev_outcome`
  preserved) — prior lines are never rewritten (identical semantics to
  `scripts/shadow_p80_3d.py`)
- pre-registered rules (`SHADOW_RULES` = discovery C2/C3/C6, **frozen**)
  are reported for observation only; nothing alerts, nothing feeds back
- `--replay` parity check rebuilds fingerprints from the DBs and diffs
  every pre-trigger field against the log — any drift is an alarm
  (features are recomputed, so a collector bug or data mutation shows up
  immediately)

## 4. Operation

```bash
# one collection pass (backfills history on first run, then incremental)
venv/bin/python scripts/shadow_fingerprint_collector_2026-09-21.py --run

# observation report over the settled log (pre-registered rules only)
venv/bin/python scripts/shadow_fingerprint_collector_2026-09-21.py --report

# parity check (hygiene; run after any data repair or code change)
venv/bin/python scripts/shadow_fingerprint_collector_2026-09-21.py --replay
```

Daily scheduling (cron, same convention as `shadow_p80_3d_daily.sh`):

```bash
scripts/shadow_fingerprint_daily.sh install-cron     # 45 15 * * * (UTC)
scripts/shadow_fingerprint_daily.sh                  # manual run
scripts/shadow_fingerprint_daily.sh uninstall-cron
```

## 5. Downlink to validation

`scripts/oos_weekly_rolling_2026-09-21.py` merges this log with the
2026-09-21 discovery CSV and re-runs the pre-registered rules
(C1–C6 + R1–R8) over **fixed weekly partitions**, appending its own
append-only history (`analysis/oos_weekly_history.jsonl`).  Weekly
read-outs continue as new games settle; no threshold is ever re-tuned.

## 6. Explicitly NOT done here

- no production code, threshold, alert, collector, dashboard or service change
- no alert volume increase (the log is silent observation)
- no rule is "promoted" from this log by this design alone — promotion
  requires the weekly OOS protocol's pre-stated bar and human approval
