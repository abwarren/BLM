# PLAN-004: Visual Analytics Dashboard with Historical Data

**Status:** Proposed
**Date:** 2026-08-03
**Deciders:** warren
**Tags:** dashboard, visualisation, historical, frontend

---

## Goal

Replace the current bare-bones BLM dashboard with a richer, more visual
frontend that shows live game graphs AND full historical data (all
34+ collected games, 575K+ snapshots).

## Current State (verified 2026-08-03)

- Live pipeline runs on :2262 (V1 Playwright collector -> blm.db, V2
  engine -> blm_ts.db, V3 HistoricalCollector -> blm_historical.db).
- **blm.db is the populated dataset**: 34 games, 575,325 snapshots
  (cols: timestamp, quarter, clock, home_score, away_score, total_line,
  spread, total_odds, spread_odds, moneyline_home/away, home/away_projection,
  pace, possessions). Still growing with the live game.
- blm_historical.db (V3) is EMPTY (0 rows — collector opens it but has
  written nothing since Jul 28).
- Existing /dashboard has a broken CSS link (/static/styles.css 404s;
  real path is /dashboard/static/styles.css) and loads Chart.js from CDN.
- /api/v2/health OK; historical research API mounted at /api/v2/historical
  but reads the empty V3 DB.

## Decision

Build a new **Visual Analytics Dashboard** served by the existing dashboard
sub-app, reading the POPULATED blm.db (V1) for history and live data.

### Data source

Read blm.db directly (read-only, WAL-safe) — it holds the real historical
dataset. Live series = newest game's snapshots (already ~250ms-1s cadence).

### Backend — new router `blm_v2/dashboard/analytics_api.py`

Mounted at `/dashboard/api`, all read-only:

| Endpoint | Purpose |
|---|---|
| GET /games | All games: teams, status, final score, final total, result (over/under vs last line), snapshot count, date |
| GET /games/{id}/detail | First/last snapshot, line movement summary, opening vs closing line |
| GET /games/{id}/series?metrics=&step= | Downsampled time series (max ~1200 pts/chart via bucketing) |
| GET /stats | Cross-game aggregates: O/U record, line-movement buckets, pace stats |
| GET /live | Latest live game snapshots (recent N) + current state |

Downsampling: group by step index, take avg per bucket — keeps charts
fast on 10K+ point games.

### Frontend — `blm_v2/dashboard/static/analytics/` (index.html, app.js, styles.css)

Dark theme matching the existing BLM look. Layout:

1. **Header** — live game teams/score/clock + status badge + auto-refresh toggle
2. **Live chart** — total score vs total line over time (auto-updating, 5s poll)
3. **Game browser** — dropdown + grid of all games; selecting one loads its
   full history into the charts
4. **Historical charts** (per selected game):
   - Total score vs line movement (line chart)
   - Spread + score margin
   - Pace / possessions per minute
5. **Stats panel** — cross-game: over/under hit rate, line movement
   distribution histogram (bar chart), games list summary table

### Assets — NO CDN

Vendor Chart.js once into `blm_v2/dashboard/static/vendor/chart.umd.min.js`
via a local Python urllib script (single local file, no runtime CDN
dependency, no curl pipes). If the download is blocked, fall back to
hand-rolled SVG line/bar charts (same data, same look).

## Files

- `docs/PLAN-004-VISUAL-ANALYTICS-DASHBOARD.md` (this plan)
- `blm_v2/dashboard/analytics_api.py` — new read-only API router
- `blm_v2/dashboard/server.py` — mount analytics static + API router
- `blm_v2/dashboard/static/analytics/index.html` / `app.js` / `styles.css`
- `blm_v2/dashboard/static/vendor/chart.umd.min.js` — vendored Chart.js

## Verification

1. `curl /dashboard/analytics` returns 200 with HTML
2. `curl /dashboard/api/games` returns 34 games w/ results
3. `curl /dashboard/api/games/{id}/series` returns downsampled series
4. Open page: charts render, game selection loads history, live poll works
5. blm.db continues growing after server restart (collectors healthy)

## Out of Scope

- V3 blm_historical.db repair (separate issue — collector writes nothing)
- Grafana auto-provisioning
- Auth (localhost/trusted network only, same as current dashboard)
