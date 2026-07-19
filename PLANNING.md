# BLM — Planning Document

## Roadmap

| Phase | TB | Description | Status |
|-------|----|-------------|--------|
| 1 | 001 | Infrastructure & Collection — End-to-end pipeline: scrape, store, serve, display | IN PROGRESS |
| 2 | 002 | League Distributions + Historical Inflation Index | NOT STARTED |
| 3 | 003 | Similarity Engine — comparable games + historical outcomes | NOT STARTED |
| 4 | 004 | Signal Detection — Freeze, Compression, Pace Regression | NOT STARTED |
| 5 | 005 | Under Timing Engine + Decision output | NOT STARTED |
| 6 | 006 | BetConstruct Market Fingerprint Engine (bookmaker behaviour modelling) | NOT STARTED |

## Architecture Ledger

| Module | Status | Notes |
|--------|--------|-------|
| Snapshot Collector | IN PROGRESS | TB-001 |
| Database Schema | DESIGNED | games + snapshots tables |
| Flask API | DESIGNED | TB-001 |
| Research Console | DESIGNED | TB-001 |
| League Registry | DESIGNED | First league: Cyber 2K26 |
| Historical Database | DESIGNED | SQLite WAL, append-only |
| Historical Inflation Engine | NOT STARTED | TB-002 |
| Similarity Engine | NOT STARTED | TB-003 |
| Freeze Detector | NOT STARTED | TB-004 |
| Compression Detector | NOT STARTED | TB-004 |
| Regression Engine | NOT STARTED | TB-004 |
| Trap Meter | DESIGNED | Constitutional concept |
| Under Timing Engine | NOT STARTED | TB-005 |
| Confidence Engine | NOT STARTED | TB-005 |
| Market State | DESIGNED | Data model supports it |
| Game State | DESIGNED | Data model supports it |

## Tracer Bullet Slice Map

| TB | What it proves | Touches | Depends on |
|----|---------------|---------|------------|
| 001 | End-to-end pipeline works — scrape, store, serve, display | Playwright → SQLite → Flask → HTML | nothing |
| 002 | League distributions + Historical Inflation Index | Calc engine + API fields + UI metrics | TB-001 (needs data) |
| 003 | Similarity engine — comparable games | Regression query + comparables endpoint + UI | TB-002 (needs distributions) |
| 004 | Signal detection (Freeze, Compression, Pace) | Signal analysis + API + UI panel | TB-002 (needs baselines) |
| 005 | Under Timing Engine + Decision output | UTS calc + confidence + decision | TB-003 + TB-004 |

## TB-001 Detailed Plan

### Objective
Build and validate the end-to-end BLM pipeline. One Playwright script scrapes live
Cyber Basketball 2K26 data from PokerBet, stores snapshots in SQLite, a Flask API
serves the current state, and a plain HTML page renders it.

### Success Criteria
- Playwright extracts score, quarter, clock, total line, and odds from live match
- Snapshots write to SQLite (games + snapshots) in WAL mode
- Flask serves `GET /api/live` with current game state
- HTML page auto-refreshes every second, shows score + SVG line chart
- Everything runs from `~/projects/blm/`

### Scope
- Single league: World Cyber Basketball 2K26 Matches
- Single game at a time
- SQLite storage
- Flask API — only `/api/live` for TB-001
- Plain HTML research console

### Out of Scope (TB-001)
- Historical Inflation Engine (TB-002)
- Similarity Engine (TB-003)
- Signal detection (TB-004)
- Under Timing Engine (TB-005)
- Pre-match / multiple games simultaneously
- Authentication / login
- Docker or deployment infra
- Any JavaScript framework

### Implementation Stages

#### Stage 1.1 — Scaffold + Schema
- Create `~/projects/blm/` structure
- `database.py` with schema creation
- Verify tables created

#### Stage 1.2 — Playwright Scraper
- Navigate to PokerBet cyber basketball page
- Extract: teams, scores, quarter, clock, current total
- Handle: no game, game in progress, game ended
- Write snapshots to SQLite

#### Stage 1.3 — Flask API
- `GET /api/live` returns current game + line history

#### Stage 1.4 — Research Console
- HTML fetches `/api/live` every 1s
- Display: score, quarter, clock, current total
- SVG line chart of total movement

#### Stage 1.5 — Integration Test
- Run collector + Flask together
- Verify console updates in real-time
- Verify DB accumulating snapshots
- Commit + handoff

### Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Playwright blocked by auth wall | High | Page loaded fine in test; add cookie handling if needed |
| DOM selectors change | Medium | Centralize in constants; log parse failures |
| No live game during testing | Medium | Handle gracefully; test with known game ID |
| Flask + Playwright in one process | Low | Threading: scraper thread, Flask in main |

### Architecture Diagram

```
Playwright (headless Chromium)
     │   polls every 1s
     ▼
Snapshot Collector (collector.py)
     │   writes
     ▼
SQLite (database.py)
  ├── games table
  └── snapshots table
     │   reads
     ▼
Flask API (app.py)
  └── GET /api/live
     │
     ▼
Research Console (index.html + style.css + script.js)
```

### Architecture Confidence: 94%
Requirements clear. Dependencies identified. No duplicate modules. No overengineering.
Uncertainty: DOM stability on PokerBet, Playwright auth requirements (resolved in Stage 1.2).
