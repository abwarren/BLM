# BLM — Agent Context

Stable architecture and durable facts. This file changes rarely — when it does
change, the change itself is notable. Live state lives in `docs/AGENT_HANDOFF.md`.

## Purpose

BLM is a live basketball analytics and betting system. Target:
PokerBet.co.za (BetConstruct) — Cyber Basketball 2K26 and Betual virtual
leagues. It measures where the live market deviates from historical
expectation and produces evidence-based UNDER/OVER entry signals.

## Core Architecture

```
Collector (PokerBet/BetConstruct live panel)
    ↓
Raw/live observations (append-only)
    ↓
Snapshots (full market + game state, immutable)
    ↓
Analytics (pace, checkpoints, fingerprints)
    ↓
Signals / fingerprints
    ↓
Alerts (UNDER/OVER, threshold-gated)
    ↓
Dashboard
    ↓
Execution (betting)
    ↓
Reconciliation (results, execution)
    ↓
Audit (forensics, scorecard, integrity checks)
```

## Important Areas

```
blm_v1/    Legacy collector components (Playwright scraper, Flask console).
           Preserved, still partially live via server.py.
blm_v2/    Platform layer: scheduler, engine, event bus, time-series store,
           FastAPI + WebSocket, replay, datasets. Partially superseded by v4.
blm_v3/    Early experiment skeleton. Not production.
blm_v4/    Current production system (PokerBet pipeline).
blm_v4/collector.py          Live discovery + snapshot capture (20s cadence).
blm_v4/live_analytics/       Live pace, fingerprints, momentum, alert logic.
blm_v4/execution/            Betting execution + reconciliation (incl. pokerbet/).
blm_v4/betting/              Betting API + worker.
blm_v4/dashboard/            Frontend (static JS/HTML/CSS).
blm_v4/scorecard.py          Checkpoint market-vs-fair analytics (M009).
blm_v4/api.py                /api/v4/* endpoints.
blm_v4/settle_worker.py      Settlement worker.
blm_v4/result_reconciler.py  Result reconciliation (swarm feed first, DOM fallback).
server.py                    Production entry point (API on port 2262).
tests/                       Automated verification (pytest, ~1500 tests).
scripts/                     Forensic analysis, audits, operational tooling.
docs/                        Architecture, milestones, agent context.
analysis/                    Dated forensic analysis artifacts.
```

## Data Stores

SQLite, append-heavy, production-live:

```
blm_pokerbet.db       Games, snapshots, checkpoint_market, reconciliation,
                      game_results, under_alerts.
blm_historical.db     Historical game/checkpoint data.
blm_metrics_clean.db  clean_market_observations, deviation_residuals.
blm_ts.db             V2 time-series snapshots.
blm_v2.db             V2 storage layer.
blm_betting.db        Betting/execution records.
```

Identity rule: game identity is `source + source_game_id` (the BetConstruct
event ID), never display text. Classifications (`CYBER_2K26`, `BETUAL_NBA`)
are isolated statistical populations — never mixed. Note: `BETUAL_NBA`
covers FIVE distinct competitions (EuroLeague, NBA, TBSL, KBL, CBA) — group
forensics by `competition_slug`, not `classification`.

## Production Services

```
blm-server.service    API + embedded reconciler/scorecard/settle workers.
                      Runs from the WORKING TREE, not HEAD.
blm-collector.service Standalone live collector.
```

Restarts require explicit user authorization (a restart loads the dirty
working tree and activates pending code).

## Important Principles

- Raw data must remain auditable.
- Live observations must not be contaminated by future information.
- Checkpoint semantics must remain explicit (checkpoints are GAME progress:
  deciles + pct75; per-quarter linkage is largely unpopulated).
- Alert logic must be reproducible.
- Every snapshot is immutable and append-only.
- Historical data is the primary source of truth; every recommendation
  must be statistically explainable.
- Leagues are independent statistical populations.
- Execution must be idempotent and reconciled.
- One-unit maximum enforced server-side; the frontend is never trusted
  for security enforcement.
- Unknown execution states must be reconciled before retrying.
- Presentation never contains business logic; business logic never
  contains scraping logic; scraping never performs analytics.

## Known Stable Facts

- Pre-existing full-suite baseline (2026-09-25): 4 failed / 1534 passed
  (3 × test_forensic_relative_pace_freeze, 1 × test_prospective_freeze).
- Identity for betting is verified against the BetConstruct event-view,
  not page DOM text (the results-page DOM carries no game id).
- Results come from the eu-swarm WebSocket feed (authoritative); the
  results-page DOM path is fallback only.

## Agent Continuity

The three-file continuity system:

```
AGENTS.md          permanent rules + directive
STATUS.md          current living state (canonical)
DECISIONS.md       durable decisions (why the architecture is so)
```

The current state of active work is maintained in:

    STATUS.md

That file is authoritative for the current unfinished task.
`docs/AGENT_HANDOFF.md` is a pointer to it, not a second source of state.
