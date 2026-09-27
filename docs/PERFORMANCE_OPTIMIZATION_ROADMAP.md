# BLM Performance Optimization Roadmap

## Objective

Reduce end-to-end live-data latency and collector CPU/I/O cost while preserving collection correctness, model semantics, alert behavior, and frontend freshness.

Primary target:

- Fast collector cadence: **10 seconds or better** under normal 2-game load.
- Frontend freshness: accepted observations should become queryable/renderable as soon as practical after capture.
- Avoid rebuilding historical state when an incremental update is sufficient.
- Keep production correctness ahead of raw throughput.

---

## Phase 0 — Baseline Measurement Policy

**Status:** Required foundation / policy

### Rules

1. Measure before optimizing.
2. Every optimization phase must have a before/after benchmark.
3. Use the same workload, game count, observation window, and environment when comparing results.
4. Record p50, p95, max, cumulative time, and call counts for expensive operations.
5. Record SQL call counts and cumulative SQL time separately from Python operation time.
6. Track work amplification per accepted snapshot.
7. For physical I/O, use `/proc/<pid>/io` `read_bytes` and `write_bytes`; do not treat `rchar` as physical disk I/O.
8. Never claim duplicate observations without a row-level audit.
9. Do not scan production observation tables as part of a performance benchmark.
10. Do not copy production databases for benchmarking.
11. Do not run `VACUUM` as a performance experiment.
12. Do not manually checkpoint WAL during measurements.
13. Do not change SQLite PRAGMAs during benchmark phases unless a separately approved experiment explicitly targets them.
14. Do not enable additional collectors merely to generate benchmark load.
15. Each phase must preserve a rollback point and document the exact code revision measured.

### Required baseline outputs

- Exact top 10 expensive operations.
- Exact top 10 SQL operations.
- Accepted snapshots.
- Observations loaded.
- Observations loaded / accepted snapshot.
- Projections rebuilt.
- Projections rebuilt / accepted snapshot.
- Residuals processed.
- Residuals / accepted snapshot.
- Collector tick p50/p95/max.
- Slow-worker p50/p95/max.
- Persistence p50/p95/max.
- Physical read/write rates from `/proc/<pid>/io`.
- WAL size trend, observed only; no manual checkpointing.

---

## Phase 1 — Instrumentation

**Status:** Implemented in working tree / deploy as controlled step

Instrumentation must be low overhead and bounded.

Measure:

- collector tick duration
- page/content acquisition
- competition parsing
- tracked-row processing
- snapshot lookup/persistence
- clean recording
- clean-metrics operations
- projection refresh
- deviation refresh
- state writing
- named SQL operations
- accepted snapshots
- observations loaded
- projections rebuilt
- residuals processed

Expose rolling p50/p95/max, cumulative milliseconds, call counts, per-tick counts, and amplification ratios.

Do not change application behavior solely for instrumentation.

---

## Phase 2 — Fresh Production Baseline

**Status:** Baseline completed

Reference measurements from the controlled production sample:

| Metric | Phase 2 baseline |
|---|---:|
| Tick p50 | 31,971 ms |
| Tick p95 | 38,455 ms |
| Tick max | 43,012 ms |
| Tick overruns | 19/20 (95%) |
| `sql.clean.valid_observations` | 736 calls / 336,909 ms total / 452 ms p50 |
| `deviation.refresh_game` | 22,421 calls / 175,783 ms total |
| `sql.deviation.prior_bucket_stats` | 467 calls / 93,597 ms total |
| `sql.storage.betual_line_count_distinct` | 21 calls / 73,740 ms total / 3,401 ms p50 |
| Observations / snapshot | 51.2 |
| Projections / snapshot | 51.2 |
| Residuals / snapshot | 0.64 |
| Physical read rate | 241 B/s over 68 s |

Previous baseline for comparison:

- Tick p50: 21.517 s
- Slow worker p50: 11.725 s
- Persistence p50: 6.449 s

The Phase 2 sample must remain the comparison anchor until a new controlled baseline is explicitly declared.

---

## Phase 3 — Eliminate Work Amplification

**Status:** In progress

### P1 — Observation cache

Cache the clean observations already loaded for a game so the same history is not repeatedly reread within the same processing cycle.

Requirements:

- Cache keyed by `source_game_id`.
- Invalidate/update only when a new accepted observation changes the relevant history.
- Preserve ordering and existing projection semantics.
- Bound memory usage.
- Add tests for cache hit/miss/invalidation behavior.

### P2 — Deviation dirty gating

Do not run deviation processing for a game when no relevant clean state changed.

Pattern:

1. Mark game dirty when an accepted observation changes the required inputs.
2. Continue collecting without immediately rebuilding the full deviation history.
3. Flush dirty games once per tick or at the appropriate batch boundary.
4. Clear the dirty marker after successful processing.

### Acceptance criteria

- `observations_loaded / accepted_snapshot` decreases materially from 51.2.
- `projections_rebuilt / accepted_snapshot` decreases materially from 51.2.
- `residuals / accepted_snapshot` does not increase unexpectedly.
- `sql.clean.valid_observations` call count falls materially.
- `deviation.refresh_game` call count falls materially.
- Tick p50 and p95 improve without loss of correctness.

No optimization is accepted solely because CPU usage falls; correctness and freshness must remain intact.

---

## Phase 4 — Decouple the Slow Worker

**Goal:** Prevent Playwright/event-page work from blocking the fast collection path.

Current evidence shows the slow worker has been a major contributor to cycle time.

### Design

Split work into independent stages:

```text
FAST PATH
lobby/feed -> parse -> accept observation -> persist minimal state -> publish

SLOW PATH
tracked event -> Playwright hydration -> deep market extraction -> enrichment
```

The fast path must not wait for the slow path except where an explicit dependency exists.

### Requirements

- Queue slow-worker jobs.
- Deduplicate pending work by stable game/event ID.
- Apply a timeout to slow browser operations.
- Prevent a slow event page from blocking unrelated games.
- Preserve retry/backoff behavior.
- Record queue wait time separately from browser execution time.

### Acceptance criteria

- Slow-worker latency no longer dominates fast-tick latency.
- A slow browser page cannot consume the entire fast-tick budget.
- Fast observations remain available while enrichment is running.

---

## Phase 5 — SQL and SQLite Work Reduction

**Goal:** Make every database operation proportional to the data actually required.

### 5.1 Fix high-cost query access paths

Investigate and benchmark indexes for the known expensive paths, especially:

- clean observation lookup by `source_game_id` and status/order requirements
- snapshot history lookup by `(game_id, created_at)` where supported by actual query predicates
- deviation prior-bucket queries
- Betual line-count/distinct aggregation

Do not add indexes blindly. For every candidate index:

1. Capture `EXPLAIN QUERY PLAN` before.
2. Add the index in a controlled branch/test database.
3. Capture the new plan.
4. Benchmark representative query latency and write overhead.
5. Keep only indexes that materially improve the workload.

### 5.2 Remove whole-history rebuilds

Replace:

```text
new observation
 -> read entire game history
 -> rebuild all projections
 -> delete/reinsert projection history
```

with:

```text
new observation
 -> update incremental state
 -> append/update only affected projection
```

Preserve trajectory semantics exactly.

### 5.3 Remove repeated whole-table metrics work

Metrics such as counts/distinct counts that currently execute every fast tick should be:

- computed on a slower cadence,
- maintained incrementally where safe, or
- served from cached counters.

They must not block live observation capture.

### Acceptance criteria

- SQL cumulative cost decreases.
- SQL call count per tick decreases.
- No production-table full scans on the hot path unless explicitly justified.
- Tick latency improves without changing output semantics.

---

## Phase 6 — Persistence Pipeline Optimization

**Goal:** Reduce the ~6-second-class persistence component seen in earlier baselines.

### Design principles

- Batch logically related writes.
- Avoid reopening/requerying the same state repeatedly.
- Use one transaction for a coherent observation batch where semantics permit.
- Avoid delete/reinsert cycles for data that can be incrementally updated.
- Keep the hot path focused on data required by live alerts/frontend output.

### Required measurements

- write calls per tick
- transaction count
- transaction duration
- rows written per transaction
- persistence queue depth
- persistence wait time vs actual SQLite execution time

Do not change PRAGMAs as part of this phase.

---

## Phase 7 — Live Data Publication / Frontend Latency

**Goal:** Make alerts and new observations appear immediately after they are accepted.

### Target pipeline

```text
provider observation
        ↓
parse
        ↓
accept + timestamp
        ↓
minimal persistence
        ↓
alert evaluation
        ↓
publish/event stream
        ↓
frontend update
```

The frontend must not wait for an unrelated slow-worker or expensive historical recomputation.

### Instrument end-to-end latency

Record timestamps for:

- provider observation received
- parse complete
- accepted
- persisted
- alert evaluated
- published
- frontend request/event served

Report:

- capture → persist
- capture → alert
- capture → API availability
- capture → frontend render

Use p50/p95/max.

---

## Phase 8 — Collection Correctness and Deduplication

**Goal:** Increase collection coverage without creating duplicate logical observations.

This phase happens **after** the hot path is efficient enough to safely support the additional load.

### Quarter-market collection

The quarter-market WebSocket path currently lacks the same cross-page in-memory gate used by the full-game path.

Investigate stable provider identity such as:

- source game ID
- provider market ID
- market period
- line value
- stable frame/market identifier where available

Do not use receive timestamp as the primary identity for deduplication.

### Required policy

- Separate logical identity from receive time.
- Allow genuine line changes to persist.
- Suppress identical provider frames delivered through multiple sockets/pages.
- Add counters for accepted, rejected-as-duplicate, and changed observations.
- Validate against controlled fixtures before production rollout.

Do not claim that duplicates exist until a controlled row-level audit is explicitly authorized.

---

## Phase 9 — Multi-Game Scaling

**Goal:** Ensure performance scales with the number of tracked games.

Benchmark at:

- 1 game
- 2 games
- 5 games
- 10 games
- 20 games

Measure:

- tick latency
- SQL calls/tick
- observations/game/tick
- browser workers/game
- memory
- CPU
- physical reads
- queue depth

The collector should scale approximately with new work rather than repeatedly multiplying historical work for every additional game.

---

## Phase 10 — Production Hardening

Before declaring the optimization complete:

1. Run the complete automated test suite.
2. Run focused collector/storage/clean-metrics/deviation tests.
3. Run a controlled production benchmark for at least 20 ticks.
4. Compare directly against the declared baseline.
5. Verify alert timing and frontend freshness.
6. Verify no loss of observations.
7. Verify no semantic change in projection/deviation outputs.
8. Verify service restart/recovery.
9. Verify the watchdog does not interfere with normal operation.
10. Record the exact commit used for the production benchmark.
11. Keep a rollback commit available.

---

# Optimization Order

The default implementation order is:

1. **Phase 0 — Baseline policy**
2. **Phase 1 — Instrumentation**
3. **Phase 2 — Fresh baseline**
4. **Phase 3 — Observation cache + deviation dirty gating**
5. **Phase 4 — Decouple slow Playwright worker**
6. **Phase 5 — SQL/query and projection optimization**
7. **Phase 6 — Persistence optimization**
8. **Phase 7 — Live publication/frontend latency**
9. **Phase 8 — Deduplication/correctness expansion**
10. **Phase 9 — Multi-game scaling**
11. **Phase 10 — Production hardening**

Do not skip measurement between phases.

---

# Phase Gate Policy

A phase is complete only when all four gates pass:

### Performance
The measured bottleneck improves against the previous baseline.

### Correctness
Existing tests and targeted invariants pass.

### Freshness
Live data and alerts are not delayed by the optimization.

### Safety
No prohibited production operation was used during measurement, and rollback remains available.

---

# Current State

As of the Phase 2/Phase 3 transition:

- Phase 0: defined.
- Phase 1: instrumentation implemented.
- Phase 2: baseline collected.
- Phase 3 P1: observation cache implemented in the working tree.
- Phase 3 P2: deviation dirty gating is the next implementation step.
- Phases 4–10: planned, not implemented.

**Important:** This document is a roadmap. It does not authorize implementation of future phases by itself. Each phase must be executed and benchmarked separately.
