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
