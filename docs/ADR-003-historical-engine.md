# ADR-003: Historical Data Collection & Time-Series Visualisation Engine

**Status:** Implemented  
**Date:** 2026-07-26  
**Deciders:** ccmaitland  
**Tags:** architecture, data-collection, time-series, ml

---

## Context

The BLM V1/V2 pipeline collects live Cyber Basketball market data at ~1s intervals via Playwright scraping and stores only the current game state plus a few pre-computed metrics (trap meter, TT modifier). This is sufficient for real-time display but insufficient for:

- **Historical research**: What happened at every tick of every game?
- **Signal validation**: Did trap formations actually predict outcomes?
- **Regression analysis**: How did the market behave under specific conditions?
- **ML training**: Can we train models on millions of market observations?

We need a system that captures the *complete* market state at high frequency, computes derived metrics, and stores everything in a queryable format suitable for both interactive exploration and ML pipelines.

## Decision

Build a **parallel pipeline** (not a replacement for V1/V2) under `blm_v3/` that:

### 1. Architecture: Parallel Pipeline (V3 alongside V1/V2)

```
Live Site → V1 Collector (1s) → V2 Enrichment (20s) → V2 Dashboard
         ↓
    V3 Historical Collector (250ms configurable)
         ↓
    Pre-compute: pace, movement deltas
         ↓
    blm_historical.db (SQLite, 7 tables)
         ↓
    Historical Research API (FastAPI, /api/v2/historical)
         ↓
    V3 Research Dashboard (Lightweight Charts, 9+ sync'd panels)
         ↓
    ML Dataset Export (CSV, per-snapshot training rows)
```

**Why parallel?** The V1→V2→Replay pipeline is live-only and works for real-time. The V3 pipeline is research-oriented. They share the scraping approach (Playwright) but differ in: interval, storage schema, and purpose.

### 2. Storage: Denormalised SQLite with Analytical Indexes

**Why SQLite?** It ships with Python, requires zero infrastructure, handles ~200 concurrent writes/second in WAL mode, and a single `blm_historical.db` file is trivially copyable/portable. Postgres would be better for team-scale but is unnecessary for a single-researcher setup.

**Why denormalised?** ML training rows need flat data. Having all metrics in one `snapshots` table with indexed columns avoids joins during export. The 7-table schema (games, snapshots, signals, events, predictions, comparative_queries, market_states) separates concerns while keeping the main analytical table flat.

### 3. Capture: High-Frequency Playwright Scraping

The V3 collector reuses the V1 scraping strategy (Playwright headless Chromium, `page.inner_text("body")` parser) but at configurable 250ms intervals with:

- **Rate limiting detection**: If the page DOM hasn't changed, the interval degrades geometrically (250ms → 500ms → 1s → 2s cap) to avoid being banned
- **Frozen market detection**: After 30 consecutive no-change polls, the collector stops (game is over or page is stale)
- **Pre-computed deltas**: Movement tracking (line/odds/spread deltas) and pace (possessions/min, projected total) are computed at write time, not on read

### 4. Derived Metrics: Pure-Function Compute Pipeline

All metric computations are **pure functions** (no state, no I/O) in `blm_v3/engine/`:

| Module | Computes | Depends On |
|--------|----------|------------|
| inflation | Inflation index (score vs line divergence) | Total score, total line |
| compression | Odds compression index | Over/under decimal odds |
| momentum | EMA-based scoring momentum | Consecutive total scores |
| regression | Regression probability | Line, fair total, game time |
| variance | Rolling line variance/volatility | Recent total line history |
| fair_total | Fair + expected total blended estimate | Projected total, line, confidence |
| compute | Orchestrator — `compute_all()` | All of the above + signal detection |
| ml_pipeline | Training row assembly + label computation | Historical snapshots |

### 5. Signal Detection: Threshold-Based, 15 Types

15 signal types across 5 categories, detected per-snapshot:

| Category | Signal Types |
|----------|-------------|
| Line | freeze, jump, sharp movement, fake movement |
| Odds | compression, expansion |
| Trap | formation, bull trap, bear trap |
| Market | correction, overreaction, regression |
| Game | momentum swing, pace collapse, inflation spike |

Signals are grouped into higher-level **events** by `event_classifier.py` (e.g., multiple `line_jump` + `odds_expansion` signals → `market_volatility` event).

### 6. Research Dashboard: Lightweight Charts (TradingView)

**Why Lightweight Charts instead of Chart.js?** Lightweight Charts is designed for financial time-series data and handles 100K+ data points smoothly with built-in crosshair sync, zoom/pan, and virtualised rendering. Chart.js struggles above ~10K points in a single dataset.

Nine synchronised panels:
1. Total Line (market line + projected + fair + actual score)
2. Trap Meter (+ TT Modifier)
3. Pace (pts/min)
4. Inflation Index
5. Confidence
6. Momentum
7. Regression Probability
8. Compression Index
9. Team Totals (home + away)

All share: crosshair position, zoom level, time axis. Event markers (coloured triangles with hover tooltips) appear on the time axis for signals.

### 7. ML Export: Snapshot-as-Training-Row

Every historical snapshot becomes one ML training observation. Labels include:

- **final_total**: Raw final score (regression target)
- **final_result**: 1 = over, 0 = under (classification target)
- **clv**: Closing Line Value (regression)
- **trap_success**: 1 = trap profitable, 0 = not (classification)

Features default to 21 metrics from the snapshot table but are fully configurable via API parameter.

## Consequences

### Positive

- **Research-grade data**: Every 250ms of every game is captured with full market state
- **Ready for ML**: 21 features + 4 label types, exportable as flat CSV
- **Backward compatible**: V1/V2 pipeline untouched; V3 mounts alongside
- **Portable**: Single SQLite file can be copied to another machine for analysis
- **Testable**: Every derived metric function has unit tests (161 across Phases 1-4)

### Negative

- **Storage growth**: ~2KB per snapshot × 4 snapshots/second × 3,600 seconds/game ≈ 28MB per game. At 10 games/day = 280MB/day. SQLite handles this, but older games may need archiving.
- **Scraping overhead**: Playwright headless Chromium uses ~200MB RAM. Running a second Playwright instance alongside V1 doubles the memory footprint.
- **No live-update in dashboard**: The research dashboard fetches data via REST API rather than WebSocket, so it doesn't auto-update during a live game. This was a deliberate choice (research != real-time), but could be added later.

### Risks Mitigated

| Risk | Mitigation |
|------|-----------|
| Rate limiting by betting site | Automatic interval degradation + mutation observer |
| SQLite write contention | WAL mode, synchronous=NORMAL, retry logic |
| Dashboard rendering lag | Lightweight Charts handles 100K+ points |
| Data loss on crash | `busy_timeout=5000` ensures writes complete |

## Alternatives Considered

### InfluxDB instead of SQLite
Rejected: Added infrastructure dependency. SQLite + WAL handles our volume (<100 writes/sec). InfluxDB would be over-engineered for a single-user research setup.

### React/Vue dashboard instead of vanilla HTML
Rejected: The replay.html is already vanilla HTML. A framework would require a build step. Lightweight Charts works without one.

### Replace V1/V2 entirely
Rejected: V1/V2 is live and known-stable. V3 is experimental/research. Running in parallel avoids risk to the production pipeline.

## Related

- ARCHITECTURE.md (V2 system overview)
- docs/HISTORICAL_ENGINE_PROPOSAL.md (full proposal with 10 deliverable review)
- docs/SCHEMA.md (V2 database schema)
