# BLM — Betting Logic Model

A production-grade quantitative sports analytics platform for live basketball betting market analysis. Captures every BLM decision over time, stores telemetry in a time-series database, exposes realtime APIs, and provides professional dashboards with historical replay.

**Target:** BetConstruct Cyber Basketball 2K26 matches on PokerBet.co.za

## Quick Start

### V1 — Research Console (Legacy)

```bash
cd ~/projects/blm
python3 app.py
# Open http://localhost:5000
```

### V2 — Platform (Recommended)

```bash
cd ~/projects/blm
python3 server.py
# Open http://localhost:8000/dashboard
# API: http://localhost:8000/api/v2/health
```

## V4 — PokerBet Live Basketball Pipeline (Current)

BLM's production game source is **PokerBet.co.za** (BetConstruct). The V4
collector discovers live basketball games from PokerBet's live panel,
classifies each into an isolated statistical population, snapshots the
full market state, and reconciles against the underlying BetConstruct
event.

```bash
# One-shot live capture (both categories)
python -m blm_v4.collector --once --ticks 1

# Continuous collector (20s cadence) — systemd: blm-collector.service
# API server (port 2262) — systemd: blm-server.service
```

### Classifications (isolated populations — zero statistical leakage)

| Classification | Competition (PokerBet) | Region | Example |
|---|---|---|---|
| `CYBER_2K26` | Cyber Basketball. 2K26 Matches | World | OKC Thunder Cyber vs SAS Spurs Cyber |
| `BETUAL_NBA` | Betual NBA | Virtual Matches | Sacramento Kings Virtual vs Miami Heat Virtual |

Every game/snapshot record carries its own `classification`, `game_family`,
`competition`, `region` — identity is `source + source_game_id` (the
BetConstruct event ID from the event-view URL). Snapshots are append-only
timestamped observations (score, period, clock, totals, spreads, odds,
status, source metadata). Historical/statistical processing is scoped per
classification; the two populations are never mixed.

### Reconciliation

Each recorded game is cross-verified against the BetConstruct event-view:
game ID, teams, competition header, score/period/clock, totals/spreads
present, and W1/W2 pricing — logged as `matched` or `mismatch` with the
reason (blm_pokerbet.db `reconciliation` table).

### Tests

```bash
python -m pytest tests/           # 112 passed (incl. blm_v4 fixtures both categories)
```

Replayable fixtures exist for both categories
(`tests/test_blm_v4_pipeline.py`) proving discovery, classification,
source, dedup, snapshot persistence, and statistical separation.

## Architecture

```
┌─ V1 Legacy ───────────────────────────────────────┐
│ Collector → SQLite → Flask API → Research Console │
├─ V2 Platform ─────────────────────────────────────┤
│ Collector → BLM Engine → Event Bus → TS DB        │
│ ↓                                                  │
│ FastAPI + WebSocket → Dashboard + Replay           │
│ ↓                                                  │
│ AI Dataset Builder → CSV / Parquet / Arrow         │
└────────────────────────────────────────────────────┘
```

## Project Structure

```
blm/
├── blm_v1/              # V1: Legacy pipeline (preserved)
│   ├── collector.py     # Playwright scraper
│   ├── database.py      # SQLite schema + queries
│   ├── app.py           # Flask API (port 5000)
│   └── static/          # Research console
├── blm_v2/              # V2: Platform
│   ├── config.py        # Centralised configuration
│   ├── collector/       # Collector interface + scheduler
│   ├── engine/          # BLM Engine (confidence, momentum, traps)
│   ├── models/          # Pydantic schemas
│   ├── events/          # Event bus (pub/sub)
│   ├── timeseries/      # TS abstraction (InfluxDB + SQLite)
│   ├── storage/         # Storage interface
│   ├── api/             # FastAPI v2 + WebSocket
│   ├── dashboard/       # Live dashboard
│   ├── replay/          # Historical replay engine
│   ├── datasets/        # ML dataset builder
│   ├── alerts/          # Real-time alert rules
│   └── analytics/       # Model analytics
├── tests/               # Unit + integration tests
├── docs/                # Architecture, API, schema docs
├── app.py               # V1 entry point
├── server.py            # V2 entry point
└── requirements.txt
```

## V2 API Endpoints

| Endpoint | Description |
|----------|-------------|
| `/api/v2/health` | Health check |
| `/api/v2/live` | Current live game with full BLM enrichment |
| `/api/v2/game/{id}` | Game details |
| `/api/v2/history/{id}` | Historical snapshots |
| `/api/v2/replay/{id}` | Replay data |
| `/api/v2/chart/{id}` | Chart-optimized data |
| `/api/v2/events/{id}` | Game events |
| `/api/v2/alerts` | Active alerts |
| `/api/v2/traps/{id}` | Trap detection data |
| `/api/v2/model` | BLM model state |
| `/api/v2/games` | All games |
| `/ws` | WebSocket for live push (20s cadence) |

## Performance Targets

| Metric | Target |
|--------|--------|
| Snapshot write | <50ms |
| Dashboard refresh | <200ms |
| Replay | 60 FPS |
| Concurrent games | 10,000 |
| Snapshot loss | Zero |

## Engineering Principles

- Historical data is the primary source of truth.
- Every module has a single responsibility.
- Presentation never contains business logic.
- Business logic never contains scraping logic.
- Everything is reproducible from stored data.
- Dependency injection for testability.
- Strong typing throughout.

## License

Proprietary — Red Cape Technologies (Pty) Ltd
