# BLM — Roadmap

| Phase | TB | Description | Slice | Status |
|-------|----|-------------|-------|--------|
| 1 | 001 | V1 Foundation — Collector, SQLite, Flask API, Research Console | 1 | DONE |
| 2 | 001b | V2 Models — Pydantic schemas, config, event bus | 2 | DONE |
| 3 | 002 | V2 Engine — BLM calculations, traps, confidence, momentum | 3 | DONE |
| 4 | 003 | Time Series + Storage — InfluxDB + SQLite fallback | 4 | DONE |
| 5 | 004 | FastAPI + WebSocket — v2 REST API + live push | 5 | DONE |
| 6 | 005 | Dashboard — Live charts, overlays, replay, alerts | 6 | IN PROGRESS |
| 7 | 006 | AI Datasets + Analytics — ML dataset builder, drift analysis | 7 | IN PROGRESS |
| 8 | 007 | Tests + Docs — Comprehensive testing, architecture docs | 8 | IN PROGRESS |
| 9 | 008 | Production hardening — Performance tuning, monitoring | 9 | NOT STARTED |

## Capability Maturity

| Capability | C-Level | Status |
|------------|---------|--------|
| Snapshot Collection | C2 | Demonstrated (V1 Playwright) |
| SQLite Storage | C2 | Demonstrated |
| Flask API (V1) | C2 | Demonstrated |
| Research Console | C2 | Demonstrated |
| BLM Engine | C1 | Implemented |
| Event Bus | C1 | Implemented |
| Time Series Abstraction | C1 | Implemented |
| FastAPI v2 | C1 | Implemented |
| WebSocket Push | C1 | Implemented |
| Live Dashboard | C0 | Designed |
| Historical Replay | C0 | Designed |
| AI Datasets | C1 | Implemented |
| Alerts System | C1 | Implemented |
| Analytics | C1 | Implemented |
| Test Suite | C0 | Designed |
| Documentation | C2 | Written |
