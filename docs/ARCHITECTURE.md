# BLM Architecture

## System Overview

BLM is a production-grade quantitative sports analytics platform for live basketball betting market analysis. It captures every Betting Logic Model decision over time, stores telemetry in a time-series database, exposes realtime APIs, and visualises model evolution like Bloomberg/TradingView/Grafana.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        V1 LAYER (Legacy)                            │
│                                                                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌───────────────┐ │
│  │Collector │───▶│  SQLite  │───▶│  Flask   │───▶│Research       │ │
│  │Playwright│    │ database │    │  API     │    │Console (HTML) │ │
│  └──────────┘    │.db       │    │ :5000    │    └───────────────┘ │
│                  └──────────┘    └──────────┘                      │
├─────────────────────────────────────────────────────────────────────┤
│                        V2 LAYER (Platform)                          │
│                                                                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────────┐  │
│  │Collector │───▶│   BLM    │───▶│  Event   │───▶│  TS Writer   │  │
│  │(V1 reused)│   │  Engine  │    │   Bus    │    │              │  │
│  └──────────┘    └──────────┘    └──────────┘    └──────┬───────┘  │
│                                                         │          │
│                                                         ▼          │
│                                                  ┌──────────────┐  │
│                                                  │   InfluxDB   │  │
│                                                  │  (primary)   │  │
│                                                  │              │  │
│                                                  │  SQLite      │  │
│                                                  │  (fallback)  │  │
│                                                  └──────┬───────┘  │
│                                                         │          │
│                    ┌────────────────────────────────────┼──────┐   │
│                    ▼                ▼                   ▼      │   │
│  ┌──────────────────────┐  ┌──────────────┐  ┌──────────────┐  │   │
│  │    FastAPI v2 REST   │  │  WebSocket   │  │   Storage    │  │   │
│  │  /api/v2/*  :8000    │  │  /ws         │  │  Interface   │  │   │
│  └──────────┬───────────┘  └──────┬───────┘  └──────────────┘  │   │
│             │                     │                              │   │
│             ▼                     ▼                              │   │
│  ┌──────────────────────────────────────────────────┐            │   │
│  │              V2 Dashboard (HTML/JS/Chart.js)      │            │   │
│  │  ● Live charts with overlays                      │            │   │
│  │  ● Replay engine (play/pause/ff/rw)              │            │   │
│  │  ● Real-time alerts                               │            │   │
│  │  ● Quarter separators, markers, annotations       │            │   │
│  └──────────────────────────────────────────────────┘            │   │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────┐       │
│  │              AI Dataset Builder                          │       │
│  │  Every snapshot → supervised learning sample             │       │
│  │  Output: CSV / Parquet / Arrow                           │       │
│  │  Targets: Winner, Margin, Final Total, Closing Value     │       │
│  └──────────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────┘
```

## Data Flow

1. **Collection**: Playwright (V1) scrapes PokerBet every ~1s → raw game state
2. **Scheduling**: V2 Scheduler polls V1 collector every 20s → raw snapshot
3. **Enrichment**: BLM Engine computes traps, momentum, confidence, projections
4. **Eventing**: Enriched snapshot published to Event Bus (typed events)
5. **Storage**: Snapshot written to InfluxDB (primary) or SQLite (fallback)
6. **Streaming**: WebSocket pushes enriched snapshot to all connected clients
7. **Visualization**: Dashboard renders live charts, alerts, and overlays
8. **Replay**: Completed games are replayable with play/pause/seek controls
9. **ML**: Dataset builder converts historical snapshots into training samples

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| V1/V2 split | Preserve existing working code. V2 adds on top, not replaces. |
| Event Bus | Decouples components. Enables replay, alerts, analytics independently. |
| Abstract TS Interface | Swap InfluxDB ↔ SQLite without changing business logic. |
| 20s snapshot cadence | Balances data resolution vs storage costs. |
| Full-state snapshots | No partial updates. Every snapshot is independently complete. |
| Dependency injection | Testable components. No hard-coded dependencies. |
| Pydantic everywhere | Runtime type safety. Self-documenting schemas. |
| WebSocket push | No polling. Real-time updates under 200ms. |
