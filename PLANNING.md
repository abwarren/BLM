# BLM — Planning Document

## Current Phase: Platform Evolution

The repository has evolved from a docs-only skeleton into a full quantitative sports analytics platform with V1 (legacy) and V2 (platform) layers.

## Architecture Ledger

| Module | Status | Notes |
|--------|--------|-------|
| V1 Collector (Playwright) | C2 DEMONSTRATED | blm_v1/collector.py |
| V1 Database (SQLite) | C2 DEMONSTRATED | blm_v1/database.py — WAL mode |
| V1 Flask API | C2 DEMONSTRATED | blm_v1/app.py — port 5000 |
| V1 Research Console | C2 DEMONSTRATED | blm_v1/static/ — HTML/SVG/JS |
| V2 Config | C1 IMPLEMENTED | blm_v2/config.py — pydantic-settings |
| V2 Models | C1 IMPLEMENTED | blm_v2/models/ — pydantic schemas |
| V2 Event Bus | C1 IMPLEMENTED | blm_v2/events/bus.py — pub/sub |
| V2 BLM Engine | C1 IMPLEMENTED | blm_v2/engine/ — traps, momentum, confidence |
| V2 Time Series Abstraction | C1 IMPLEMENTED | blm_v2/timeseries/ — InfluxDB + SQLite |
| V2 Storage Abstraction | C1 IMPLEMENTED | blm_v2/storage/ — game CRUD |
| V2 FastAPI | C1 IMPLEMENTED | blm_v2/api/v2_fastapi.py — port 8000 |
| V2 WebSocket | C1 IMPLEMENTED | blm_v2/api/websocket.py — /ws |
| V2 Dashboard | C0 DESIGNED | blm_v2/dashboard/ — in progress |
| V2 Replay Engine | C0 DESIGNED | blm_v2/replay/ — in progress |
| V2 Alerts | C0 DESIGNED | blm_v2/alerts/ — in progress |
| V2 AI Datasets | C0 DESIGNED | blm_v2/datasets/ — in progress |
| V2 Analytics | C0 DESIGNED | blm_v2/analytics/ — in progress |
| Tests | C0 DESIGNED | tests/ — in progress |
| Documentation | C2 WRITTEN | docs/ — ARCHITECTURE, API, SCHEMA |

## V2 File Map — 55+ files

```
blm_v2/
├── __init__.py
├── config.py
├── collector/
│   ├── __init__.py
│   ├── base.py
│   ├── snapshot.py
│   └── scheduler.py
├── engine/
│   ├── __init__.py
│   ├── blm_engine.py
│   ├── confidence.py
│   ├── momentum.py
│   ├── trap_meter.py
│   └── market.py
├── models/
│   ├── __init__.py
│   ├── snapshot.py
│   ├── game.py
│   ├── events.py
│   ├── predictions.py
│   └── api.py
├── analytics/
│   ├── __init__.py
│   ├── drift.py
│   ├── stability.py
│   ├── frequency.py
│   └── calibration.py
├── events/
│   ├── __init__.py
│   ├── bus.py
│   └── handlers.py
├── timeseries/
│   ├── __init__.py
│   ├── base.py
│   ├── influx.py
│   └── sqlite_fallback.py
├── storage/
│   ├── __init__.py
│   ├── base.py
│   ├── sqlite.py
│   └── influx.py
├── api/
│   ├── __init__.py
│   ├── dependencies.py
│   ├── v2_fastapi.py
│   └── websocket.py
├── dashboard/
│   ├── __init__.py
│   ├── server.py
│   └── static/
│       ├── index.html
│       ├── dashboard.js
│       └── styles.css
├── replay/
│   ├── __init__.py
│   ├── engine.py
│   └── static/
│       └── replay.html
├── datasets/
│   ├── __init__.py
│   ├── builder.py
│   └── exporter.py
├── alerts/
│   ├── __init__.py
│   └── manager.py
└── telemetry/
    ├── __init__.py
    ├── logging.py
    └── metrics.py
```

## Tracer Bullet Slice Map

| Slice | What it proves | Depends on | Status |
|-------|---------------|------------|--------|
| 1 | V1 pipeline: scrape → store → serve → display | nothing | ✅ DONE |
| 2 | V2 models + event bus + config | nothing | ✅ DONE |
| 3 | BLM engine: confidence, momentum, traps, market | Slice 2 | ✅ DONE |
| 4 | TS abstraction: InfluxDB + SQLite write/read | Slice 2 | ✅ DONE |
| 5 | FastAPI + WebSocket: v2 REST + live push | Slices 2-4 | ✅ DONE |
| 6 | Dashboard + replay + alerts | Slices 3-5 | 🔄 IN PROGRESS |
| 7 | Datasets + analytics + tests | Slices 2-5 | 🔄 IN PROGRESS |
| 8 | Production hardening | ALL | ⏳ NOT STARTED |

## Next Actions

1. Wait for subagent builds of dashboard/replay/alerts (Slice 6)
2. Wait for subagent builds of datasets/analytics/tests (Slice 7)
3. Verify V1 server starts: `python3 app.py`
4. Verify V2 server starts: `python3 server.py`
5. Run test suite: `source venv/bin/activate && pytest tests/`
6. Commit and push to GitHub
7. Save BLM platform as a skill
