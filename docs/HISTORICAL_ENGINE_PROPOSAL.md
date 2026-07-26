# BLM Historical Data Collection & Time-Series Visualisation Engine

## Architecture Proposal — v0.1

---

## Table of Contents

1. [Architecture Review](#1-architecture-review)
2. [Data Model](#2-data-model)
3. [Database Schema](#3-database-schema)
4. [API Design](#4-api-design)
5. [Signal Engine Design](#5-signal-engine-design)
6. [Chart Architecture](#6-chart-architecture)
7. [Playback Architecture](#7-playback-architecture)
8. [Performance Analysis](#8-performance-analysis)
9. [Risk Analysis](#9-risk-analysis)
10. [Incremental Implementation Plan](#10-incremental-implementation-plan)

---

## 1. Architecture Review

### Current State

The existing BLM platform has three data tiers:

| Layer | Technology | Purpose | Status |
|-------|-----------|---------|--------|
| V1 Collection | Playwright → SQLite (`blm.db`) | Legacy scrape pipeline | Operational |
| V2 Enrichment | BLM Engine → Event Bus → TS Writer | Real-time enrichment | Built but idle |
| V2 Storage | Timeseries abstraction → InfluxDB / SQLite (`blm_ts.db`) | High-frequency snapshots | Built but idle |
| V2 API | FastAPI + WebSocket | Live + historical queries | Built but idle |
| V2 Dashboard | Static HTML/JS with Chart.js | Live monitoring | Partially built |
| V2 Replay | Static HTML/JS with Chart.js + annotations | Historical playback | Built (recently improved) |

### Gaps

1. **No high-frequency collector.** The V1 collector scrapes at ~1s, the V2 scheduler polls at 20s. For time-series analysis we need 250ms–500ms resolution.

2. **No dedicated historical database.** `blm_ts.db` stores snapshots as JSON blobs with indexed scalars — this works for replay but isn't optimised for analytical queries (aggregations, cross-game comparisons, signal mining).

3. **No signal detection layer.** Events are typed in the model layer but no engine persists them or generates them retrospectively from historical data.

4. **No research UI.** The replay dashboard shows BLM Score + Trap Meter. A researcher needs 9+ synchronised charts with zoom, pan, crosshair, event overlays, and comparative analysis.

5. **No export pipeline.** CSV/JSON/PNG export doesn't exist.

6. **No ML dataset builder.** The existing `blm_v2/datasets/` module exists but isn't wired to the replay or research workflow.

### Proposed Additions

```
┌──────────────────────────────────────────────────────────┐
│                    NEW: HISTORICAL ENGINE                  │
│                                                           │
│  Collector (250ms) → Snapshot Buffer → Historical DB      │
│       ↓                                                   │
│  Derived Metrics (pace, odds, deltas, inflation, etc.)   │
│       ↓                                                   │
│  Market Events (freeze, jump, compression, traps...)     │
│       ↓                                                   │
│  Signal Engine (momentum, regression, confidence...)     │
│       ↓                                                   │
│  Research API                                             │
│       ↓                                                   │
│  Research Dashboard (9+ sync'd charts, overlays,          │
│    comparative analysis, export, ML export)               │
└──────────────────────────────────────────────────────────┘
```

This is a **parallel pipeline** — the existing V1/V2 collector continues operating. The Historical Engine is a new subsystem that:

1. Attaches to the **same data source** (PokerBet) at a higher frequency
2. Writes to a **separate, analytics-optimised SQLite database** (`blm_historical.db`)
3. Runs **derived calculations** (line deltas, pace, inflation, compression, etc.)
4. Detects **market events** and **signals** in real-time
5. Provides a **research-grade API** and **multi-chart dashboard**

---

## 2. Data Model

### 2.1 HistoricalSnapshot (core observation)

Every row in the historical DB represents one market observation.

```
HistoricalSnapshot {
    // ── Identity ──
    id:                    UUID (v7, time-sortable)
    game_id:               str
    league:                str  // "Cyber 2K26"
    season:                str? // "2026"
    timestamp:             str  // ISO 8601 with microseconds (250ms resolution)

    // ── Game State ──
    quarter:               int  // 1-4, 5+ for OT
    clock:                 str? // "MM:SS"
    possession:            str? // "home" | "away" | null
    home_score:            int
    away_score:            int
    score_difference:      int  // home - away
    total_score:           int  // home + away

    // ── Market ──
    total_line:            float? // Live over/under line
    spread:                float? // Live spread (home perspective)
    home_team_total:       float? // Team total line for home
    away_team_total:       float? // Team total line for away

    // ── Odds (as decimal for computation, stored as float) ──
    over_odds:             float? // Decimal odds for OVER
    under_odds:            float? // Decimal odds for UNDER
    spread_odds_home:      float? // Decimal odds for home spread
    spread_odds_away:      float? // Decimal odds for away spread

    // ── Raw line (pre-computed movement) ──
    total_line_raw:        float? // Un-smoothed line value
    spread_raw:            float? // Un-smoothed spread

    // ── Movement Deltas (computed at write time) ──
    line_delta:            float? // Change in total_line since last snapshot
    odds_delta:            float? // Change in over_odds since last snapshot
    spread_delta:          float? // Change in spread since last snapshot

    // ── Pace (computed) ──
    possessions:           int?   // Estimated total possessions
    possessions_per_min:   float? // Possessions per minute of game time
    projected_possessions: float? // Projected possessions for full game
    projected_total:       float? // Projected final total at current pace

    // ── Derived BLM Metrics (computed) ──
    trap_meter:            float? // Composite trap score 0-100
    tt_modifier:           float? // Team total modifier
    inflation_index:       float? // Market inflation index
    compression_index:     float? // Odds compression index
    momentum:              float? // Directional momentum
    regression_prob:       float? // Regression probability 0-1
    fair_total:            float? // Model's fair value total
    expected_total:        float? // Expected final total
    variance:              float? // Variance estimate
    volatility:            float? // Volatility estimate
    confidence:            float? // Model confidence 0-1
}
```

### 2.2 MarketSignal

A signal is a derived event — something interesting happened.

```
MarketSignal {
    id:                    UUID
    game_id:               str
    timestamp:             str
    signal_type:           enum(SignalType)
    severity:              enum("low", "mid", "high", "critical")
    value:                 float  // The metric value that triggered the signal
    threshold:             float  // The threshold that was crossed
    description:           str    // Human-readable explanation
    related_metrics:       dict   // Snapshot of surrounding metrics
    confirmed:             bool?  // Whether the signal was validated post-hoc
}
```

SignalType enum:
- `line_freeze` — No line movement despite score change for N ticks
- `line_jump` — Line moves >X points in one snapshot
- `odds_compression` — Over/under odds tighten (converge toward -110/-110)
- `odds_expansion` — Over/under odds widen
- `sharp_movement` — Line moves against public betting direction
- `fake_movement` — Line moves but odds don't shift proportionally
- `trap_formation` — Conditions for a trap are forming (Trap Meter > 60)
- `bull_trap` — Trap Meter > 80, bullish pattern
- `bear_trap` — Trap Meter > 80, bearish pattern
- `market_correction` — Market reverts after a sharp move
- `overreaction` — Line move exceeds what score change warrants
- `regression` — Total regresses toward the line after being far from it
- `momentum_swing` — Sudden pace/momentum change
- `pace_collapse` — Pace drops below threshold
- `inflation_spike` — Inflation index exceeds threshold

### 2.3 ComparativeQuery

A saved comparative analysis — a parameterised query that can be re-run.

```
ComparativeQuery {
    id:                    UUID
    name:                  str?   // "High Trap Under Games"
    filters:               dict   // { league, quarter_min, trap_min, inflation_max, ... }
    game_ids:              list   // Explicit game selection
    created_at:            str
    updated_at:            str
}
```

### 2.4 MlTrainingRow

One row per snapshot, exported as CSV/Parquet for ML.

```
MlTrainingRow {
    // All HistoricalSnapshot fields
    // Plus:
    label_final_total:     float?  // Final game total (added post-game)
    label_final_margin:    float?  // Final game margin
    label_over:            bool    // Did total go OVER?
    label_under:           bool    // Did total go UNDER?
    label_clv:             float?  // Closing line value
    label_trap_success:    bool?   // Was trap prediction correct?
    label_model_accuracy:  float?  // How accurate was BLM at this point?
}
```

---

## 3. Database Schema

### Database: `blm_historical.db`

```sql
-- ═══════════════════════════════════════════════════════════════
-- GAMES
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE games (
    id              TEXT PRIMARY KEY,          -- game_id
    league          TEXT NOT NULL DEFAULT 'Cyber 2K26',
    season          TEXT,
    home_team       TEXT NOT NULL,
    away_team       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'live'
                    CHECK(status IN ('pre', 'live', 'halftime', 'ended')),
    start_time      TEXT,                      -- ISO 8601
    end_time        TEXT,                      -- ISO 8601
    final_home      INTEGER,
    final_away      INTEGER,
    final_total     INTEGER,                   -- home + away
    final_margin    INTEGER,                   -- home - away
    total_snapshots INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_games_league ON games(league);
CREATE INDEX idx_games_status ON games(status);
CREATE INDEX idx_games_start ON games(start_time);

-- ═══════════════════════════════════════════════════════════════
-- SNAPSHOTS (the core table — one row per observation)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE snapshots (
    id                  TEXT PRIMARY KEY,       -- UUID v7 (time-sortable)
    game_id             TEXT NOT NULL REFERENCES games(id),
    timestamp           TEXT NOT NULL,           -- ISO 8601 with microseconds
    quarter             INTEGER NOT NULL DEFAULT 1,
    clock               TEXT,                    -- "MM:SS"
    possession          TEXT,

    -- Scoreboard
    home_score          INTEGER NOT NULL DEFAULT 0,
    away_score          INTEGER NOT NULL DEFAULT 0,
    score_difference    INTEGER NOT NULL DEFAULT 0,
    total_score         INTEGER NOT NULL DEFAULT 0,

    -- Market lines
    total_line          REAL,
    spread              REAL,
    home_team_total     REAL,
    away_team_total     REAL,
    total_line_raw      REAL,
    spread_raw          REAL,

    -- Odds (decimal)
    over_odds           REAL,
    under_odds          REAL,
    spread_odds_home    REAL,
    spread_odds_away    REAL,

    -- Movement deltas
    line_delta          REAL,
    odds_delta          REAL,
    spread_delta        REAL,

    -- Pace
    possessions         INTEGER,
    possessions_per_min REAL,
    projected_possessions REAL,
    projected_total     REAL,

    -- Derived BLM metrics
    trap_meter          REAL,
    tt_modifier         REAL,
    inflation_index     REAL,
    compression_index   REAL,
    momentum            REAL,
    regression_prob     REAL,
    fair_total          REAL,
    expected_total      REAL,
    variance            REAL,
    volatility          REAL,
    confidence          REAL,

    -- Raw full snapshot (JSON backup — for forward compatibility)
    raw_json            TEXT,

    -- Metadata
    ingested_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Core query index: get all snapshots for a game ordered by time
CREATE INDEX idx_snapshots_game_time ON snapshots(game_id, timestamp);

-- Analytical indexes
CREATE INDEX idx_snapshots_ts ON snapshots(timestamp);
CREATE INDEX idx_snapshots_qtr ON snapshots(game_id, quarter);
CREATE INDEX idx_snapshots_trap ON snapshots(game_id, trap_meter);
CREATE INDEX idx_snapshots_infl ON snapshots(game_id, inflation_index);
CREATE INDEX idx_snapshots_mom ON snapshots(game_id, momentum);
CREATE INDEX idx_snapshots_conf ON snapshots(game_id, confidence);
CREATE INDEX idx_snapshots_ptot ON snapshots(game_id, projected_total);

-- ═══════════════════════════════════════════════════════════════
-- SIGNALS (derived events)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE signals (
    id              TEXT PRIMARY KEY,           -- UUID v7
    game_id         TEXT NOT NULL REFERENCES games(id),
    snapshot_id     TEXT REFERENCES snapshots(id),  -- Which observation triggered it
    timestamp       TEXT NOT NULL,
    signal_type     TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'mid'
                    CHECK(severity IN ('low', 'mid', 'high', 'critical')),
    value           REAL,                       -- The metric value that triggered
    threshold       REAL,                       -- The threshold that was crossed
    description     TEXT,
    related_json    TEXT,                       -- JSON of surrounding context
    confirmed       INTEGER DEFAULT 0,         -- Post-hoc validation
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_signals_game ON signals(game_id, timestamp);
CREATE INDEX idx_signals_type ON signals(signal_type);
CREATE INDEX idx_signals_severity ON signals(severity);

-- ═══════════════════════════════════════════════════════════════
-- MARKET EVENTS (raw detected events — higher level than signals)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE market_events (
    id              TEXT PRIMARY KEY,
    game_id         TEXT NOT NULL REFERENCES games(id),
    snapshot_id     TEXT REFERENCES snapshots(id),
    timestamp       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    duration_seconds REAL,                     -- How long the event lasted
    magnitude       REAL,                      -- How significant
    description     TEXT,
    data_json       TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_events_game ON market_events(game_id, timestamp);
CREATE INDEX idx_events_type ON market_events(event_type);

-- ═══════════════════════════════════════════════════════════════
-- PREDICTIONS (BLM model predictions at snapshot points)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE predictions (
    id              TEXT PRIMARY KEY,
    game_id         TEXT NOT NULL REFERENCES games(id),
    snapshot_id     TEXT REFERENCES snapshots(id),
    timestamp       TEXT NOT NULL,
    predicted_total REAL,
    predicted_margin REAL,
    predicted_winner TEXT,
    win_probability  REAL,
    confidence      REAL,
    fair_total      REAL,
    expected_pace   REAL,
    model_version   TEXT,
    data_json       TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_predictions_game ON predictions(game_id, timestamp);

-- ═══════════════════════════════════════════════════════════════
-- COMPARATIVE QUERIES (saved analysis presets)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE comparative_queries (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    description     TEXT,
    filters_json    TEXT NOT NULL,              -- Full filter spec as JSON
    game_ids_json   TEXT,                       -- Explicit game selection
    result_count    INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_run_at     TEXT
);

-- ═══════════════════════════════════════════════════════════════
-- ML EXPORT TRACKING
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE ml_exports (
    id              TEXT PRIMARY KEY,
    export_type     TEXT NOT NULL,              -- 'csv', 'parquet', 'json'
    game_ids_json   TEXT,                       -- Which games were included
    row_count       INTEGER NOT NULL DEFAULT 0,
    file_path       TEXT,
    file_size_bytes INTEGER,
    feature_list    TEXT,                       -- Which features were exported
    label_column    TEXT,
    model_version   TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
```

### Key Design Decisions

- **Denormalised snapshots table.** All metrics are columns, not JSON. This lets SQLite index them individually and run `WHERE`, `ORDER BY`, `GROUP BY`, `AVG()`, `MAX()` etc. without extracting JSON at query time.

- **UUID v7 for primary keys.** Time-sortable UUIDs enable chronological ordering without a separate timestamp index for many queries. PostgreSQL-style but usable everywhere.

- **Separate signals and market_events tables.** A signal is a threshold crossing (trap_meter > 80). An event is a higher-level occurrence (a Trap Formation event that lasts 45 seconds, containing multiple signal triggers). This two-tier model mirrors the specification.

- **raw_json column.** A full JSON snapshot is stored alongside the denormalised columns. If we add new derived metrics later, we can re-process historical raw data without re-collecting.

- **Analytical indexes.** Each commonly-queried derived metric gets an index for fast `WHERE trap_meter > 80 AND inflation_index > 5` queries.

---

## 4. API Design

### 4.1 Historical Engine API

New namespace under `/api/v2/historical/`:

```
GET  /api/v2/historical/games
     ?league=Cyber+2K26
     &status=ended
     &from=2026-01-01
     &to=2026-07-25
     &limit=50
     &offset=0
     → { games: [...], total: N, limit, offset }

GET  /api/v2/historical/games/{game_id}
     → { game: {...}, snapshot_count: N, signal_count: N }

GET  /api/v2/historical/snapshots/{game_id}
     ?from=<ISO8601>
     &to=<ISO8601>
     &limit=10000
     &offset=0
     → { snapshots: [...], total: N, limit, offset }

GET  /api/v2/historical/snapshots/{game_id}/aggregated
     ?interval=1s|5s|30s|1m|5m|1q
     → { intervals: [{ ts, avg_total_line, avg_trap_meter, ... }] }

GET  /api/v2/historical/metrics/{game_id}
     ?metrics=trap_meter,inflation_index,confidence,momentum
     → { game_id, series: { trap_meter: [...], inflation_index: [...] } }

GET  /api/v2/historical/signals
     ?game_id=X
     &type=trap_formation,line_freeze
     &severity=high,critical
     &limit=100
     → { signals: [...], total: N }

GET  /api/v2/historical/events/{game_id}
     ?type=sharp_movement,overreaction
     &limit=100
     → { events: [...], total: N }

GET  /api/v2/historical/compare
     ?game_ids=id1,id2,id3
     &metrics=trap_meter,inflation_index,projected_total,total_line
     → { series: { id1: { trap_meter: [...] }, id2: {...} } }

POST /api/v2/historical/compare/query
     Body: { filters: { trap_min: 80, inflation_min: 5, result: "under" } }
     → { matched_games: [...], series: {...}, count: N }

GET  /api/v2/historical/export/csv
     ?game_ids=id1,id2
     &metrics=all
     → Content-Type: text/csv
     → Streamed CSV download

GET  /api/v2/historical/export/json
     ?game_ids=id1,id2
     → JSON download

POST /api/v2/historical/export/ml
     Body: { game_ids: [...], features: [...], label: "final_total" }
     → { export_id, file_path, row_count, status }
```

### 4.2 Research Dashboard API

```
GET  /api/v2/research/chart/{game_id}/{metric}
     ?interval=5s
     &smoothing=none|ema3|ema10
     → { metric, data: [{ x, y }], game_info }

GET  /api/v2/research/chart/{game_id}/multi
     ?metrics=trap_meter,inflation_index,confidence,total_line,projected_total
     &interval=5s
     → { series: { metric: { label, data: [{x,y}], unit } } }

GET  /api/v2/research/overlay
     ?base_game=X&compare_games=a,b,c
     &metric=trap_meter
     → { base: { data: [{x,y}] }, comparisons: [{ game_id, data: [{x,y}] }] }

GET  /api/v2/research/aggregate
     ?game_ids=a,b,c
     &metric=trap_meter
     &agg=avg|min|max|median|p25|p75
     → { metric, mean_series: [{x,y}], bands: [p25, p75] }
```

### 4.3 WebSocket Research Feed

```
WS /ws/research/{game_id}

Server → Client:
  { type: "snapshot", data: { ... HistoricalSnapshot ... } }
  { type: "signal",   data: { ... MarketSignal ... } }
  { type: "event",    data: { ... MarketEvent ... } }
  { type: "batch",    data: [snapshot, signal, event, ...] }
```

---

## 5. Signal Engine Design

### 5.1 Architecture

The Signal Engine runs as a pipeline after each snapshot is written:

```
Snapshot Written
  ↓
1. PRE-PROCESS: Compute derived metrics
   → line_delta, odds_delta, spread_delta
   → pace metrics (possessions/min, projected possessions)
   → inflation_index, compression_index
   → momentum, variance, volatility
  ↓
2. DETECT SIGNALS: Run threshold-checking rules
   → 15 signal types, each with configurable thresholds
  ↓
3. CLASSIFY EVENTS: Group related signals into market events
   → Multiple concurrent signals → event
   → Sustained condition → event with duration
  ↓
4. PERSIST: Write signals and events to DB
  ↓
5. NOTIFY: Push to WebSocket research feed
```

### 5.2 Compute Pipeline (per snapshot)

```python
# pseudocode for each incoming snapshot
def process_snapshot(prev: HistoricalSnapshot, curr: HistoricalSnapshot) -> ProcessedResult:
    # 1. Movement deltas
    line_delta = curr.total_line - prev.total_line if both else None
    odds_delta = curr.over_odds - prev.over_odds if both else None
    spread_delta = curr.spread - prev.spread if both else None

    # 2. Pace
    time_elapsed_s = parse_time(curr.clock) - parse_time(prev.clock)
    time_elapsed_min = time_elapsed_s / 60.0
    score_change = (curr.home_score + curr.away_score) - (prev.home_score + prev.away_score)
    pace_per_min = score_change / time_elapsed_min if time_elapsed_min > 0 else None
    projected_total = pace_per_min * 48 if pace_per_min else None

    # 3. Inflation index
    # Measures how far the total line has moved relative to actual score
    score_movement = curr.total_score - game_start_total
    line_movement = curr.total_line - game_start_line if curr.total_line else 0
    inflation_index = (score_movement - line_movement) / max(line_movement, 1)
    # > 3 means line is inflating faster than scoring warrants

    # 4. Compression index
    # Measures how close over/under odds are to each other
    if over_odds and under_odds:
        spread_pct = abs(over_odds - 2.0) + abs(under_odds - 2.0)  # 2.0 = evens
        compression_index = 1.0 - (spread_pct / max_spread)
    # > 0.8 means odds are tight (high confidence market)

    # 5. Momentum
    # Rate of score change weighted by recency
    momentum = ema(prev.momentum, score_change, alpha=0.3)

    # 6. Variance / Volatility
    variance = rolling_variance(total_line_values[-10:])
    volatility = math.sqrt(variance)

    # 7. Fair Total (model estimate)
    fair_total = projected_total * regression_factor
```

### 5.3 Signal Detection Rules

Each signal type has a detector function that returns `(triggered, severity, value)`:

| Signal | Condition | Severity |
|--------|-----------|----------|
| `line_freeze` | line_delta == 0 for >= 10 consecutive ticks | scales with freeze duration |
| `line_jump` | abs(line_delta) > 2.0 in one snapshot | scales with delta size |
| `odds_compression` | compression_index > 0.85 | scales with value |
| `odds_expansion` | compression_index < 0.4 | scales inversely |
| `sharp_movement` | line moves opposite to projected pace direction | mid/high |
| `fake_movement` | line_delta > 1.0 but odds_delta < 0.02 | mid |
| `trap_formation` | trap_meter_candidate > 60 | scales with value |
| `bull_trap` | inflation_index > 4 AND trap_meter > 80 | high |
| `bear_trap` | inflation_index < -3 AND trap_meter > 80 | high |
| `market_correction` | line reverses after jump > 3 points | scales with reversal |
| `overreaction` | abs(line_delta) > 2 * abs(rolling_avg_line_delta) | mid |
| `regression` | abs(fair_total - total_line) < 1 AND was > 5 | high |
| `momentum_swing` | abs(momentum_change) > 3 | scales with change |
| `pace_collapse` | pace_per_min < 1.0 AND game_minutes > 12 | high |
| `inflation_spike` | inflation_index > 5 | scales with value |

### 5.4 Event Classification

Events group related signals temporally:

```
Event "Trap Formation" = [
    inflation_index > 4 (continuous for N ticks),
    trap_meter > 70 (at tick N-2),
    line_freeze (at tick N-1),
    signal: trap_formation (at tick N)
] → duration: from first condition to signal
```

Events are stored in the `market_events` table with a `duration_seconds` field.

---

## 6. Chart Architecture

### 6.1 Frontend Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Charts | **Lightweight Charts** (TradingView) | Financial-grade time-series, crosshair, zoom, pan, sync built-in. Much lighter than Chart.js for 100K+ points. |
| Framework | Vanilla JS (ES modules) | No build step. Matches existing dashboard pattern. |
| Layout | CSS Grid + custom panel manager | Resizable, hide/show, drag-to-reorder via native HTML5 DnD. |
| Export | Canvas API + JSZip + FileSaver | PNG via canvas.toBlob(), CSV/JSON via Blob streaming. |

### 6.2 Chart Types (9+ charts)

| # | Chart Title | Series | Y-Axis |
|---|-------------|--------|--------|
| 1 | **Total Line** | Total Line (live), Projected Total, Fair Total, Actual Total (score) | Points |
| 2 | **Trap Meter** | Trap Meter, Threshold (80 line) | 0-100 |
| 3 | **Inflation** | Inflation Index, Zero line | Ratio |
| 4 | **Compression** | Compression Index | 0-1 |
| 5 | **Pace** | Possessions/Min, Expected Pace | Rate |
| 6 | **Odds** | Over Odds, Under Odds (both as decimal) | 1.0-2.0 |
| 7 | **Confidence** | Confidence, Regression Probability | 0-1 |
| 8 | **Momentum** | Momentum Score | Signed |
| 9 | **Variance** | Variance, Volatility | Signed |

### 6.3 Synchronisation Model

All charts share a `ChartCoordinator` object:

```javascript
class ChartCoordinator {
    // Single shared crosshair position
    crosshair: { x: number, y: number | null }
    // Time range (zoom/pan)
    timeRange: { from: number, to: number }
    // Playback position (for animation)
    playbackPosition: number

    // All registered chart instances
    charts: ChartInstance[]

    // Called by any chart when crosshair moves
    setCrosshair(x, y) {
        this.crosshair = { x, y }
        this.charts.forEach(c => c.setCrosshair(x, y))
    }

    // Called by any chart when zoom/pan changes
    setTimeRange(from, to) {
        this.timeRange = { from, to }
        this.charts.forEach(c => c.setTimeRange(from, to))
    }
}
```

Lightweight Charts supports `crosshair` and `timeRange` natively on each instance. We just fan out the event to all registered charts.

### 6.4 Event Overlay

Events are rendered as **markers** on the Lightweight Charts time axis:

```javascript
// Each chart subscribes to the coordinator's event feed
chartInstance.setMarkers([
    { time: timestamp, position: 'aboveBar', color: '#ef4444',
      shape: 'circle', text: '🔴 Trap', tooltip: 'Trap Meter: 84\nLine increased +4 without pace support' },
    { time: timestamp, position: 'aboveBar', color: '#f59e0b',
      shape: 'arrowUp', text: '🟡 Momentum', tooltip: 'Momentum swing: 5.2' },
    { time: timestamp, position: 'belowBar', color: '#22c55e',
      shape: 'arrowDown', text: '🟢 Regression' },
    { time: timestamp, position: 'belowBar', color: '#6b7280',
      shape: 'square', text: '⚫ Freeze' },
    { time: timestamp, position: 'aboveBar', color: '#8b5cf6',
      shape: 'diamond', text: '🟣 Sharp' },
])
```

Hovering a marker shows a tooltip with event details.

### 6.5 Comparative Overlay

For comparative mode, use multiple series with different line styles:

```javascript
// Comparative series
chart.addLineSeries({
    data: historicalAverages,
    color: '#55607a',
    lineStyle: 2,  // Dashed
    title: 'Historical Avg (High Trap Under Games)',
    lastValueVisible: false,
})

chart.addLineSeries({
    data: currentGameData,
    color: '#00d4ff',
    title: 'Current Game',
})
```

Support up to 5 overlay series per chart (current + 4 comparisons).

### 6.6 Export

```javascript
// PNG
canvas.toBlob(blob => saveAs(blob, 'chart-trap-meter.png'))

// CSV
let csv = 'timestamp,total_line,projected_total,actual_total\n'
data.forEach(row => csv += `${row.ts},${row.line},${row.proj},${row.actual}\n`)
saveAs(new Blob([csv], {type: 'text/csv'}), 'game-xxx-snapshots.csv')

// JSON
saveAs(new Blob([JSON.stringify(data)], {type: 'application/json'}), 'game-xxx.json')
```

---

## 7. Playback Architecture

### 7.1 Research Playback

The existing replay engine is frame-based (plays back pre-loaded frames). The research dashboard uses **time-based** playback:

```javascript
class ResearchPlayback {
    constructor(coordinator, gameId, apiBase) {
        this.coordinator = coordinator
        this.gameId = gameId
        this.frames = []    // Full snapshot array, loaded once
        this.position = 0   // Index into frames
        this.speed = 1      // Multiplier
        this.playing = false
    }

    // Jump to a specific game time (in game-minutes)
    seekToGameTime(minutes) { ... }

    // Jump to the nearest signal of a given type
    jumpToSignal(type) {
        const idx = this.frames.findIndex(f => f.signals?.includes(type), this.position)
        if (idx >= 0) this.goToFrame(idx)
    }

    // Jump to the nearest event
    jumpToEvent(type) { ... }

    // Jump to quarter boundary
    jumpToQuarter(q) { ... }

    // Play at current speed
    play() {
        this.playing = true
        const interval = 1000 / (this.speed * this.frameRate)
        this._loop(interval)
    }

    // Fast forward / slow motion
    setSpeed(1, 2, 4, 8, 16, 0.5, 0.25) { ... }

    // Frame stepping
    stepForward() { ... }
    stepBackward() { ... }
}
```

### 7.2 Data Loading Strategy

- **Lazy-load:** Load game metadata + snapshot count on page load (~5ms)
- **Demand-load:** Load full snapshot data only when a game is selected (~100ms for 1,000 snapshots)
- **Virtualise:** For games with 100K+ snapshots, use time-interval aggregation:
  - At zoom level "full game": show 1 datapoint per 30 seconds (~96 points)
  - At zoom level "quarter": show 1 datapoint per 5 seconds (~144 points)
  - At zoom level "2 minutes": show every datapoint
- **Progressive loading:** Load coarse data first, then finer resolution as zoom increases

### 7.3 Sync with Replay Engine

The research playback maintains compatibility with the Python `ReplayEngine` by reading from `blm_historical.db` instead of `blm_ts.db`. The API returns frames in the same structure, allowing the existing replay UI to optionally use historical data.

---

## 8. Performance Analysis

### 8.1 Write Path

**Target:** 250ms intervals per game (4 writes/second/game).

| Operation | Est. Time | Notes |
|-----------|-----------|-------|
| Parse scraped data | < 1ms | Simple dict access |
| Compute derived metrics | < 2ms | Math ops, no I/O |
| Detect signals | < 1ms | 15 threshold checks |
| Classify events | < 1ms | Lookback over recent signals |
| SQLite INSERT | < 5ms | WAL mode, local SSD |
| **Total** | **< 10ms** | Well under 250ms budget |

For 20 concurrent games at 250ms: 80 writes/second. SQLite can handle ~5,000 writes/second on WAL mode. **No bottleneck.**

### 8.2 Read Path

**Target:** API responses < 200ms for 10,000 datapoints.

| Query | Strategy | Est. Time |
|-------|----------|-----------|
| Single game snapshots (10K) | Indexed range scan | < 50ms |
| Signal query with filters | Indexed scan + WHERE | < 20ms |
| Comparative overlay (5 games × 10K) | 5 indexed range scans | < 250ms |
| CSV export (50K rows) | Streaming query + write | < 500ms |
| ML export (100K rows) | Streaming with feature selection | < 1s |

### 8.3 Rendering

| Scenario | Strategy | Performance |
|----------|----------|-------------|
| 1,000 points, 9 charts | All points rendered | < 10ms paint |
| 10,000 points, 9 charts | All points, lightweight-charts polyline | < 30ms paint |
| 100,000 points, 9 charts | Time-interval aggregation (every Nth point) | < 50ms paint |
| 1,000,000 points | Server-side aggregation (10s intervals) | < 100ms network + render |

Lightweight Charts uses Canvas2D with auto-binocular rendering — it handles 10K+ points per series without optimisation. For 100K+, we downsample via the API's `aggregated` endpoint.

### 8.4 SQLite WAL Performance

Expected performance for `blm_historical.db` on Mac M1 Air (local SSD):

- Sequential inserts: ~10,000/s
- Indexed range queries: ~50,000 rows/s
- Database size for 100K snapshots: ~200MB
- Database size for 1M snapshots: ~2GB (acceptable)

### 8.5 Bottleneck Risk

The **scraper** is the bottleneck, not the DB. Playwright on PokerBet at 250ms intervals may trigger rate limiting. Mitigation:

1. Cache rendered pages and incrementally update via DOM polling (PokerBet uses SSE)
2. Use the browser's `requestAnimationFrame` or `MutationObserver` instead of timer-based polling
3. Fall back to 500ms if rate-limited

---

## 9. Risk Analysis

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **PokerBet rate limits high-frequency scraping** | Medium | High | Use MutationObserver instead of timer; cache page; degrade to 500ms/1s gracefully |
| **Line and odds data not available at 250ms resolution** | Medium | Medium | Fill gaps with last-known value; mark as unchanged; still record timestamp |
| **SQLite write contention with concurrent game collectors** | Low | Medium | Use separate DB per game, or WAL mode with busy_timeout |
| **100K+ snapshots cause slow dashboard load** | Medium | Medium | Use time-interval aggregation; progressive loading; paginated API |
| **Market event false positives drown real signals** | High | Medium | Severity tiering; cooldown per signal type; post-hoc confirmation flag |
| **Browser memory usage with 9 charts × 10K points** | Low | Medium | Lightweight Charts uses Canvas; 9 instances × 10K points ≈ 50-100MB |
| **Derived metric computation diverges from existing BLM engine values** | Medium | Low | Document formulas; option to seed from BLM engine output |
| **Comparative queries become slow with many games** | Low | Medium | Pre-aggregate per-game summary stats; limit to 50 games per query |

---

## 10. Incremental Implementation Plan

The work is split into **6 phases**. Each phase is independently testable and shippable.

### Phase 1 — Historical Database & Schema (2 days)

```
Deliverable: blm_historical.db with games, snapshots tables
Files:
  blm_v3/historical/__init__.py
  blm_v3/historical/schema.py          # SQL schema as constants
  blm_v3/historical/database.py        # WAL-mode connection + init
  blm_v3/historical/models.py          # Pydantic models (HistoricalSnapshot, etc.)
  blm_v3/historical/config.py          # DB path, default thresholds
Tests:
  test_historical_schema.py            # Verify tables created, indexes exist
  test_historical_models.py            # Pydantic validation
```

Steps:
1. Define schema SQL and run init
2. Implement Pydantic models for all tables
3. Implement Database class with CRUD for games and snapshots
4. Test with 1,000 synthetic snapshots

### Phase 2 — Snapshot Collection at High Frequency (2 days)

```
Deliverable: High-frequency collector that writes to blm_historical.db
Files:
  blm_v3/collector/__init__.py
  blm_v3/collector/historical_collector.py   # High-freq collector (250ms)
  blm_v3/collector/pace_calculator.py        # Pace, possessions calculation
  blm_v3/collector/movement_tracker.py       # Line/odds/spread delta computation
Tests:
  test_historical_collector.py
  test_pace_calculator.py
```

Steps:
1. Create high-frequency collector (subclasses existing V1 collector but polls at 250ms)
2. Implement pace calculator
3. Implement movement delta tracker (needs lookback to previous snapshot)
4. Wire to HistoricalDatabase
5. Manual test: run against a live game, verify 4 snapshots/second

### Phase 3 — Derived Metrics & Signal Engine (3 days)

```
Deliverable: Compute pipeline that enriches snapshots with all derived metrics
Files:
  blm_v3/engine/__init__.py
  blm_v3/engine/compute.py            # All derived metric computations
  blm_v3/engine/inflation.py          # Inflation index
  blm_v3/engine/compression.py        # Compression index
  blm_v3/engine/momentum.py           # Momentum calculation
  blm_v3/engine/regression.py         # Regression probability
  blm_v3/engine/variance.py           # Variance + volatility
  blm_v3/engine/fair_total.py         # Fair total estimation
  blm_v3/signals/__init__.py
  blm_v3/signals/detector.py          # Threshold-based signal detection
  blm_v3/signals/registry.py          # Signal type definitions
  blm_v3/signals/event_classifier.py  # Group signals into market events
Tests:
  test_compute.py                     # Verify all derived metrics
  test_signals.py                     # Verify signal detection
  test_event_classifier.py            # Verify event grouping
```

Steps:
1. Implement all derived metric computations as pure functions
2. Build signal detection engine with configurable thresholds
3. Build event classifier
4. Wire to HistoricalDatabase (signals + events tables)
5. Unit-test with synthetic scenarios (line freeze, jump, inflation spike, etc.)

### Phase 4 — Historical Research API (2 days)

```
Deliverable: FastAPI endpoints under /api/v2/historical/
Files:
  blm_v3/api/__init__.py
  blm_v3/api/historical_routes.py     # FastAPI router with all endpoints
  blm_v3/api/research_routes.py       # Research-specific endpoints
  blm_v3/api/export_routes.py         # CSV/JSON/ML export
  blm_v3/api/services.py              # Service layer (queries, aggregates)
Tests:
  test_historical_api.py              # HTTP tests with FastAPI TestClient
```

Steps:
1. Implement `/api/v2/historical/` routes
2. Implement aggregation endpoints (time-interval grouping)
3. Implement comparative query endpoints
4. Implement CSV/JSON export
5. Wire to existing V2 FastAPI app (mount as sub-router)

### Phase 5 — Research Dashboard (4 days)

```
Deliverable: Full research dashboard with 9+ synchronised charts
Files:
  blm_v3/dashboard/__init__.py
  blm_v3/dashboard/server.py              # FastAPI static mount
  blm_v3/dashboard/static/research.html   # Main dashboard HTML
  blm_v3/dashboard/static/research.js     # Chart coordinator, panels, controls
  blm_v3/dashboard/static/research.css    # Dashboard theme
  blm_v3/dashboard/static/charts.js       # Individual chart factories
  blm_v3/dashboard/static/playback.js     # Research playback engine
  blm_v3/dashboard/static/signals.js      # Signal legend + event timeline
  blm_v3/dashboard/static/export.js       # Export handling
  blm_v3/dashboard/static/compare.js      # Comparative analysis UI
```

Steps:
1. Set up HTML layout with CSS grid (resizable panels)
2. Integrate Lightweight Charts (CDN) with ChartCoordinator
3. Build each of the 9 chart types
4. Implement crosshair synchronisation
5. Implement zoom/pan synchronisation
6. Add event overlay markers with tooltips
7. Add signal legend sidebar with event timeline
8. Add playback controls (play/pause/speed/seek/jump-to)
9. Add comparative overlay (select games, overlay series)
10. Add export buttons (PNG, CSV, JSON)
11. Add filtering controls (league, quarter, signal type, outcome)
12. Implement historical learning query UI

### Phase 6 — ML Export & Polish (2 days)

```
Deliverable: ML dataset export pipeline + dashboard polish
Files:
  blm_v3/ml/__init__.py
  blm_v3/ml/dataset_builder.py        # Feature engineering
  blm_v3/ml/exporter.py               # CSV/Parquet export
  blm_v3/ml/label_generator.py        # Post-game label attachment
Tests:
  test_ml_export.py
```

Steps:
1. Implement ML dataset builder (feature column selection)
2. Implement CSV/Parquet export
3. Implement label attachment (post-game final scores)
4. Wire export to dashboard
5. UX polish: drag-to-reorder charts, hide/show, responsive layout
6. Performance testing with 100K+ snapshots
7. Documentation update

---

## Summary

| Phase | Deliverable | Days | Dependencies |
|-------|-------------|------|-------------|
| 1 | Historical DB + Schema + Models | 2 | None |
| 2 | High-Frequency Collector | 2 | Phase 1 |
| 3 | Derived Metrics + Signal Engine | 3 | Phase 1 |
| 4 | Historical Research API | 2 | Phase 1, 3 |
| 5 | Research Dashboard | 4 | Phase 4 |
| 6 | ML Export + Polish | 2 | Phase 1, 5 |
| **Total** | | **15 days** | |

---

## Appendix: Existing Codebase Mapping

| Current Module | Relationship to Proposal |
|----------------|-------------------------|
| `blm_v1/collector.py` | V1 Playwright scraper — high-freq collector extends this pattern |
| `blm_v1/database.py` | V1 SQLite schema — kept as-is, new historical DB is separate |
| `blm_v2/timeseries/` | Abstract TS interface — historical engine uses its own schema, not this |
| `blm_v2/storage/` | Game/alerts CRUD — historical engine uses its own DB |
| `blm_v2/engine/trap_meter.py` | Existing trap meter — Phase 3 can optionally reuse its logic |
| `blm_v2/models/snapshot.py` | BlmSnapshot model — HistoricalSnapshot is a superset |
| `blm_v2/events/` | Event bus — Phase 3 signal engine is a superset |
| `blm_v2/dashboard/` | Live dashboard — Phase 5 is a separate research dashboard |
| `blm_v2/replay/` | Replay UI — Phase 5 playback is time-based, not frame-based |
| `blm_v2/api/v2_fastapi.py` | V2 API — Phase 4 mounts as sub-router |
| `blm_v2/datasets/` | Existing dataset builder — Phase 6 replaces/extends |

---

**End of proposal.** Awaiting approval before any implementation begins.
