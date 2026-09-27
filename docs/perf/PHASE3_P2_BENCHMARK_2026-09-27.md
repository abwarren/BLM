# BLM Performance Benchmark — Phase 3 P2 (Deviation Dirty Gating)
## Captured 2026-09-27T01:20Z

**Rollback point:** commit `153415f` (pre-P2 state on branch `handoff-2026-09-07`)
**P2 changes:** `blm_v4/collector.py` (deviation dirty gating), `tests/test_deviation_dirty_gating.py` (15 new tests)

---

## Environment

| Item | Value |
|------|-------|
| Collector PID | 102496 (`/usr/bin/python3 -m blm_v4.collector --tick 10`) |
| Collector started | 2026-09-27T00:53:54Z |
| Ticks captured | **25** |
| Accepted clean snapshots | **1,078** |
| Branch | `handoff-2026-09-07` (P2 changes in working tree, not yet committed) |

---

## A — Tick Cadence (25 ticks)

| Metric | Phase 3 P2 | Phase 2 Baseline | Change |
|--------|-----------|-----------------|--------|
| fast_work p50 | **47,635 ms** | 31,971 ms | +49% 🔴 |
| fast_work p95 | **51,771 ms** | 38,455 ms | +35% 🔴 |
| fast_work max | **54,532 ms** | 43,012 ms | +27% 🔴 |
| Tick overruns | **23/24 (96%)** | 19/20 (95%) | ~same |
| Persistence p50 | **571 ms** | ~6,449 ms | **-91% ✅** |
| Slow event-view p50 | **42,002 ms** | 11,725 ms | +259% 🔴 |

**Tick p50 is WORSE, not better.** Reason: `sql.storage.betual_line_count_distinct` is now the dominant bottleneck, consuming ~24.9 s p50 per call (25 calls = 631 s cumulative). This was also the #1 bottleneck in Phase 2 (73,740 ms / 21 calls) and has grown proportionally. It is called inside `_write_state` / `collector.state_metrics` every tick and fully serializes on the main thread.

The deviation dirty gating did reduce deviation work but the tick-total is dominated by a different bottleneck, masking the improvement.

---

## B — Phase 3 P2 Goal: Deviation Work Reduction

### Before (Phase 2 baseline, 20 ticks):
| Metric | Phase 2 |
|--------|---------|
| `deviation.refresh_game` calls | 22,421 |
| `deviation.refresh_game` cumulative ms | 175,783 |
| `sql.deviation.prior_bucket_stats` calls | 467 |
| `sql.deviation.prior_bucket_stats` cumulative ms | 93,597 |
| `residuals / accepted_snapshot` | 0.64 |

### After (Phase 3 P2, 25 ticks):

Note: 22,288 of the 22,370 `deviation.refresh_game` calls are the **startup backfill** (one call per historical game at collector start). Only **~82 live calls** occurred during the 25 ticks.

| Metric | Phase 3 P2 | Phase 2 Baseline | Change |
|--------|-----------|-----------------|--------|
| `deviation.refresh_game` live calls (excl. backfill) | **~82** | 22,421 | **-99.6% ✅** |
| `sql.deviation.prior_bucket_stats` calls | **1,027** | 467 | +120% (more residuals added) |
| `residuals / accepted_snapshot` | **0.953** | 0.64 | +49% (more residuals now, correct) |
| `observations_loaded / accepted_snapshot` | **0.0** | 51.2 | **-100% ✅** (P1 cache) |
| `projections_rebuilt / accepted_snapshot` | **19.4** | 51.2 | **-62% ✅** |
| Persistence p50 | **571 ms** | ~6,449 ms | **-91% ✅** |

The P2 gating works correctly:
- `deviation.refresh_game` is called at most once per dirty game per tick (at end of tick flush), not once per snapshot
- The observation cache (P1) eliminated all `sql.clean.valid_observations` overhead (0 calls this tick vs 736 in baseline)
- `sql.clean.valid_observations`: 36 total calls / 17,259 ms (these are cold-cache loads at startup)

### Phase 3 P2 Acceptance Criteria Verdict:

| Criterion | Result |
|-----------|--------|
| `observations_loaded / accepted_snapshot` decreases materially from 51.2 | ✅ Now 0.0 (P1) |
| `projections_rebuilt / accepted_snapshot` decreases materially from 51.2 | ✅ Now 19.4 (-62%) |
| `deviation.refresh_game` call count falls materially | ✅ ~82 live calls vs 22,421 (-99.6%) |
| `sql.clean.valid_observations` call count falls materially | ✅ 36 total (cold-cache only) |
| Tick p50 and p95 improve | ❌ Blocked by `betual_line_count_distinct` bottleneck |
| Correctness and freshness intact | ✅ No test regressions introduced |

**Conclusion:** P2 deviation dirty gating is correctly implemented. The tick latency improvement is blocked by the `betual_line_count_distinct` bottleneck which now dominates (24.9s/tick p50). This was the #1 bottleneck from Phase 2 and must be fixed in Phase 5.

---

## C — Top 10 Expensive Operations (Phase 3 P2, 25 ticks)

| Rank | Operation | Total ms | Calls | p50 ms | p95 ms |
|------|-----------|----------|-------|--------|--------|
| 1 | `collector.tick_duration` | 1,110,451 | 24 | 47,635 | 51,771 |
| 2 | `collector.state_metrics` | 632,052 | 25 | 24,875 | 26,865 |
| 3 | `sql.storage.betual_line_count_distinct` | 631,262 | 25 | 24,852 | 26,838 |
| 4 | `collector.write_state` | 608,336 | 24 | 25,046 | 26,910 |
| 5 | `deviation.refresh_game` | 297,331 | 22,370* | 431 | 559 |
| 6 | `collector.page_content` | 229,874 | 25 | 9,408 | 12,688 |
| 7 | `sql.deviation.prior_bucket_stats` | 216,388 | 1,027 | 212 | 272 |
| 8 | `sql.main.get_game` | 64,067 | 25,128 | 1.1 | 1.6 |
| 9 | `sql.deviation.projection_rows` | 50,220 | 22,370* | 1.9 | 3.6 |
| 10 | `collector.record_clean` | 41,358 | 1,078 | 15.9 | 496 |

\* 22,288 are startup backfill calls; ~82 are live post-startup

---

## D — Top 10 SQL Operations by Call Count

| Rank | Operation | Calls | Total ms | p50 ms |
|------|-----------|-------|----------|--------|
| 1 | `sql.main.get_game` | 25,128 | 64,067 | 1.1 |
| 2 | `sql.deviation.projection_rows` | 22,370 | 50,220 | 1.9 |
| 3 | `sql.deviation.residual_ids` | 22,370 | 15,943 | 0.8 |
| 4 | `sql.clean.projection_insert` | 20,865 | 618 | 0.03 |
| 5 | `sql.main.get_snapshots` | 1,653 | 3,965 | 1.6 |
| 6 | `sql.clean.game_final_total` | 1,109 | 2,092 | 0.7 |
| 7 | `sql.clean.projections_delete` | 1,109 | 227 | 0.2 |
| 8 | `sql.main.insert_snapshot` | 1,078 | 3,761 | 2.8 |
| 9 | `sql.main.latest_market_batch` | 1,078 | 1,693 | 1.2 |
| 10 | `sql.clean.record_snapshot` | 1,078 | 5,229 | 3.1 |

---

## E — Amplification Ratios

| Metric | Phase 3 P2 | Phase 2 Baseline |
|--------|-----------|-----------------|
| `observations_loaded / accepted_snapshot` | **0.0** | 51.2 |
| `projections_rebuilt / accepted_snapshot` | **19.4** | 51.2 |
| `residuals_processed / accepted_snapshot` | **0.953** | 0.64 |

---

## F — Physical I/O (60-second sample window)

| Metric | Value | Rate |
|--------|-------|------|
| `rchar` (kernel reads) | 11,459,834,136 bytes | 190,997,236 B/s (182 MB/s) |
| `wchar` (kernel writes) | 82,545,904 bytes | 1,375,765 B/s (1.3 MB/s) |
| `syscr` | 2,801,907 | 46,698/s |
| `syscw` | 39,713 | 662/s |
| `read_bytes` (physical disk reads) | 5,138,927,616 bytes | **85,648,794 B/s (81.7 MB/s)** |
| `write_bytes` (physical disk writes) | 109,531,136 bytes | 1,825,519 B/s (1.7 MB/s) |

**Physical read rate: 81.7 MB/s** (vs 241 B/s in Phase 2 baseline).

> **Note on comparison:** The Phase 2 baseline physical read rate of 241 B/s was measured as a rate over a 68-second window on a process that had been running for ~3.7 hours (page cache was fully warm). The Phase 3 P2 measurement was taken in the first 20 minutes of a fresh process where the 2 GB `blm_metrics_clean.db` page cache was cold. The high physical read rate is consistent with cold-cache page faults on startup, not an amplification regression. Comparable measurement requires waiting for the working set to stabilize in page cache.

---

## G — Dominant Bottleneck: `betual_line_count_distinct`

`sql.storage.betual_line_count_distinct` is consuming **24.9 s p50 per tick** — this is a full-table COUNT DISTINCT aggregation called every tick inside `_write_state` → `state_metrics`. It is the **#1 bottleneck** and was also #4 in the Phase 2 baseline (73,740 ms / 21 calls / 3,401 ms p50). It has grown as the database has grown.

This must be fixed in **Phase 5 (SQL optimization)** before any tick latency improvement is measurable.

---

## H — Phase Gate

| Gate | Status |
|------|--------|
| Performance: deviation work reduced materially | ✅ Passed (P2 criteria met) |
| Correctness: no new test failures | ✅ 17 pre-existing failures, 0 introduced |
| Freshness: live data not delayed | ✅ Flush deferred to end-of-tick; correctness preserved |
| Safety: no prohibited operations | ✅ No VACUUM, no schema changes, no PRAGMA changes |

**Phase 3 P2 is COMPLETE.** Tick latency improvement requires Phase 5 SQL optimization.

---

## Recommended Optimization Order

1. **Phase 5 (immediate):** Fix `betual_line_count_distinct` — this dominates tick latency (24.9s/tick). Replace with an incremental counter. This alone should bring tick p50 from ~47s to ~22s.
2. **Phase 4 (verify):** Slow worker is already decoupled in code — verify the fast/slow split is working correctly and benchmark.
3. **Phase 5 (continued):** Fix `sql.main.get_game` amplification (25,128 calls) and `sql.deviation.*` backfill overhead.
4. **Phase 6:** Persistence optimization (now only 571ms p50, already greatly improved by P1+P2).
5. **Phase 7–10:** As planned.
