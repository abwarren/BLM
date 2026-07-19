# BLM — Betting Logic Model

A research and decision-support platform for evaluating live basketball betting
opportunities. Measures whether the sportsbook's live market has deviated from
historically expected behaviour and identifies high-probability entry timing for
UNDER opportunities.

**Target:** BetConstruct Cyber Basketball 2K26 matches on PokerBet.co.za

**Status:** Phase 1 — Infrastructure & Collection (TB-001 in progress)

## Architecture

```
Playwright → Snapshot Collector → SQLite → Flask API → Research Console
```

## Quick Start

```bash
cd ~/projects/blm
python3 app.py
```

Open http://localhost:5000 in a browser.

## Project Structure

```
BLM_CONSTITUTION.md   — Core architecture & philosophy
PLANNING.md           — Roadmap, architecture ledger, tracer bullet plans
ROADMAP.md            — Short roadmap reference
database.py           — SQLite schema + queries
collector.py          — Playwright scraper + snapshot loop
app.py                — Flask API (scraper thread + REST endpoints)
static/               — Research console (index.html, style.css, script.js)
```

## Engineering Principles

- Historical data is the primary source of truth.
- Every module has a single responsibility.
- Presentation never contains business logic.
- Business logic never contains scraping logic.
- Everything is reproducible from stored data.
- Architecture over frameworks: plain HTML/JS, SQLite, Flask.
