# Reconstructed BLM Performance Optimization Roadmap
# (No single PERFORMANCE_OPTIMIZATION_ROADMAP.md exists; reconstructed from docs/perf/BASELINE_2026-09-26.md,
#  docs/perf/PHASE3_P2_BENCHMARK_2026-09-27.md, blm_v4/performance.py, and collector.py annotations)

## Performance Phase Inventory (reconstructed from existing docs)

### Phase 1 — Performance Instrumentation (ALREADY IMPLEMENTED)
Status: DONE (working tree, uncommitted)
- blm_v4/performance.py — RollingPerformance class with tick_scope, measure, snapshot
- Wired into collector.py: tick_duration, page_content, store_list_snapshot, record_clean,
  state_metrics, write_state, sql.main.get_game, deviation.refresh_game, etc.
- tests/test_performance_metrics.py — targeted perf tests
- Baseline captured: docs/perf/BASELINE_2026-09-26.md
- Known limitation: instrumentation not yet deployed to running collector (PID 3406)
  — running build is pre-instrumentation; new-process measurements require restart

### Phase 2 — Baseline Capture (ALREADY COMPLETE)
Status: DONE
- docs/perf/BASELINE_2026-09-26.md — full baseline captured 2026-09-26T22:30Z
- Key findings:
  - Tick p50 = 21,313 ms (target 10,000 ms, 2.1x over)
  - Tick overruns: 445/446 (99.8%)
  - Persistence p50 = 5,575 ms
  - Slow event-view p50 = 12,542 ms
  - Top costs: Playwright slow-worker, SQLite persistence
  - Top SQL: betual_line_count_distinct (Phase 2: 73,740ms/21 calls/3,401ms p50)
  - Index gaps: blm.db snapshots missing (game_id, created_at) composite
  - WAL healthy, I/O disproportionate to DB size

### Phase 3 P1 — Clean Recording / Observation Cache (ALREADY IMPLEMENTED)
Status: DONE (per PHASE3_P2_BENCHMARK_2026-09-27.md)
- observations_loaded / accepted_snapshot: 51.2 → 0.0 (-100%)
- Implemented in working tree, measured in Phase 3 P2 benchmark

### Phase 3 P2 — Deviation Dirty Gating (ALREADY IMPLEMENTED)
Status: DONE (commit 9d0582f, measured)
- deviation.refresh_game live calls: 22,421 → ~82 (-99.6%)
- projections_rebuilt / accepted_snapshot: 51.2 → 19.4 (-62%)
- persistence p50: 6,449ms → 571ms (-91%)
- BUT tick p50 WORSE: 31,971ms → 47,635ms (+49%) — betual_line_count_distinct dominates
- 15 new tests in test_deviation_dirty_gating.py, all passing
- Phase 3 P2 benchmark: docs/perf/PHASE3_P2_BENCHMARK_2026-09-27.md

### Phase 4 — Fast/Slow Worker Decoupling (STATUS UNKNOWN — MUST VERIFY)
Status: NEED TO VERIFY
- collector.py references "decoupled event-view path" (tests/test_m009_market_capture.py:218)
- tests/test_collector_scheduler_step2.py references STEP 3 decoupling
- Need to verify: is the slow event-view actually running in a separate thread/process?
- If already decoupled: benchmark to confirm improvement
- If not decoupled: implement the split

### Phase 5 — SQL Optimization (NOT STARTED — BLOCKED BY Phase 3 P2 OUTCOME)
Status: NOT STARTED
Priority fixes identified by baseline + Phase 3 P2 benchmark:

  5a. Fix betual_line_count_distinct — #1 bottleneck, 24.9s/tick p50
      Replace full-table COUNT DISTINCT with incremental counter
      Expected: tick p50 from ~47s → ~22s

  5b. Fix sql.main.get_game amplification — 25,128 calls/tick
      The _game_db_id() is called per-snapshot; cache in-game

  5c. Fix sql.deviation.* backfill overhead
      deviation.refresh_game: 22,370 calls (22,288 are startup backfill)
      deviation.projection_rows: 22,370 calls
      deviation.residual_ids: 22,370 calls
      Consider: batch backfill, defer to background, or skip for old games

  5d. Fix blm.db snapshots index — missing (game_id, created_at) composite
      idx_snapshots_game_id causes TEMP B-TREE for ORDER BY created_at

  5e. Fix blm_pokerbet.db checkpoint_market game_id index
      No plain game_id index; queries by game_id DESC LIMIT 1 likely scanning

### Phase 6 — Persistence Optimization (PARTIALLY DONE)
Status: PARTIALLY DONE
- Persistence p50 already improved from 6,449ms → 571ms by P1+P2
- Remaining: evaluate write batching, WAL tuning (within allowed PRAGMA changes),
  connection management
- blm_ts.db shows connection pool contention (12+ fds open simultaneously)

### Phase 7-10 — Not Defined
Status: NOT DEFINED
- No performance roadmap phases beyond 6 exist in the documentation
- The AGENTS.md §4 ladder (Phases 0-6) is a different build roadmap, not performance

## Current Working Tree State (git status summary)
- Branch: handoff-2026-09-07, HEAD 9d0582f "Phase 3 P2: deviation dirty gating"
- 38 tracked files modified + many untracked files
- Key modified files for performance work:
  - blm_v4/performance.py (NEW, untracked) — instrumentation
  - blm_v4/collector.py (MODIFIED) — perf wiring + deviation dirty gating
  - blm_v4/storage.py (MODIFIED) — ?
  - tests/test_performance_metrics.py (NEW, untracked) — perf tests
  - tests/test_deviation_dirty_gating.py (NEW, untracked) — 15 P2 tests
- Pre-existing failures (baseline 2026-09-25): 4 failed / 1534 passed
  - 3x test_forensic_relative_pace_freeze
  - 1x test_prospective_freeze
- Additional failures from dirty tree: 11 more (dashboard JS, live-route tests, etc.)
- Production services running from WORKING TREE (not HEAD):
  - blm-server PID 1602
  - blm-collector PID 3406 (pre-instrumentation build)

## Execution Plan

Phase 4: Verify fast/slow worker split → benchmark
Phase 5a: Fix betual_line_count_distinct → benchmark
Phase 5b: Fix sql.main.get_game amplification → benchmark
Phase 5c: Fix deviation backfill overhead → benchmark
Phase 5d: Fix blm.db snapshots index → benchmark
Phase 5e: Fix blm_pokerbet.db checkpoint_market index → benchmark
Phase 6: Persistence optimization → benchmark
Final: Phase 2 baseline → final comparison
