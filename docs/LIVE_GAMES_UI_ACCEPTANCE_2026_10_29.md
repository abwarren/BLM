# Live Games UI — acceptance report (EC2 test instance `127.0.0.1:2263`)

**Scope:** the Live Games destination only. Frontend presentation, filter
interaction, polling lifecycle, responsive layout.
**Not touched:** production services/databases, the `:2262` mirror, Auto-Bet
execution/stakes/limits/kill switch/submission authorization, cohort evaluation
or historical scoring.
**Date:** 2026-10-29 · **Host:** `blm-stage-cpt` (`i-0d6c4d01114f52bf4`, af-south-1)

## 1. Commits in this phase

| Commit | What it changes | Evidence |
|---|---|---|
| `7a9ac79` | Poll `/api/v4/live` only while LIVE GAMES is the open destination | lifecycle 5/5 (away-window 2 → 0 requests) |
| `ee94113` | Two-row top bar and non-wrapping destinations at ≤1200 px; keep UNDER PRICE in the narrow table; empty state recomputed every render | 26/26 interaction, 5/5 lifecycle, 3 viewports without page overflow |

Branch `feature/tc-cohort-refactor-test`, base `992cd21`, tip after this phase
`ee94113`. Working tree clean; deployed to `:2263` from that commit.

## 2. Defects found and fixed

| # | Defect | Evidence it was real | Fix |
|---|---|---|---|
| 1 | The live feed kept polling while another destination was open | 2 requests per 45 s measured on TRADE COHORTS | timer now follows the destination; **0** requests in the same window |
| 2 | Top bar stacked into four rows at 1152 px (brand / Auto-Bet / status / orphaned AUDIO OFF), ~200 px of lost table space | baseline screenshot | two deliberate rows at ≤1200 px |
| 3 | Eight destination labels each wrapped onto two lines | baseline screenshot | `white-space: nowrap` + horizontal scroll on the nav |
| 4 | Narrow layout dropped UNDER PRICE while keeping AGE | baseline screenshot | UND kept; LEAGUE and AGE move out at ≤1200 px |
| 5 | Empty state was created once and never updated: it read "no games in the feed" while the header read "0 live · 100 games" | screenshot + live probe | message recomputed each render, and it now names the action that helps (`ADVANCED → SHOW ALL`) |
| 6 | `RESET` cleared the five select widgets but not the filter state behind them (found in the previous phase, re-verified here) | acceptance check failed on the old build | store cleared explicitly next to the controls |

Deliberately **not** changed: the live-only default filter, the 20 s cadence, the
served payload, the card view, the manual-bet block, and every cohort number.

## 3. Acceptance results

Command (test host):
`/home/ubuntu/blm/.venv/bin/python -u /tmp/tc-evidence/live_acceptance.py`
and `…/poll_lifecycle.py`. Artifacts under `/tmp/tc-evidence/value/live-final/`.

### 3.1 Interaction and layout — `acc_final.out`

- table is the default presentation; TABLE/CARDS toggle is a primary control and persists
- 100 rows render for the served feed; segment tabs filter and appear as chips
- league dropdown filters (NBA 40 / EUROLEAGUE 21) and is reflected in a chip
- removing a chip clears its control; other filters are untouched
- empty result set shows an explicit empty state, never "loading"
- RESET clears chips, selects and search back to the documented default
- row order stable across a poll; scroll position retained (600 → 600)
- returning to Live Games creates no duplicate rows and no duplicate poll loop
- 1440×900, 1280×800, 1152×800: no page-level horizontal overflow, filters and toggle visible
- no console errors, no page errors, no failed requests

### 3.2 Polling lifecycle — `poll_final.out`

Three windows of 66 s with 45 s on TRADE COHORTS in between:

| Phase | Before | After |
|---|---:|---:|
| on LIVE GAMES | 3 | 3 |
| on TRADE COHORTS | **2** | **0** |
| back on LIVE GAMES | 4 | 4 |

The cadence is 20 s (`LIVE_POLL_MS`), so 3 requests per 66 s is one per interval;
the fourth on return is the single immediate refresh.

## 4. Screenshots

`/tmp/tc-evidence/value/live-final/` — `final-desktop.png`, `final-laptop.png`,
`final-small.png`, `final-desktop-filtered.png`.
`/tmp/tc-evidence/value/live-baseline/` — `baseline-{desktop,laptop,small}-{default,showall}.png`
(the pre-change state, captured before any edit).

## 5. Known remaining defects

1. **`#collectorPill` reads `UNVERIFIED` and an orange banner sits above every
   page** on this host because no collector heartbeat is published. Truthful for
   this deployment; it costs ~30 px at the top of every destination.
2. **REQ / ACT / PROG are `–` for every row in this data slice** because the
   games are ended and the projector block is live-only. The columns are correct
   but currently empty; they populate on live games.
3. **`betting_status` polls every 5 s** (`POLL_MS`) from every destination. It
   feeds the Auto-Bet pills in the top bar, which are meant to be globally
   visible, so it was left alone — but it is the largest remaining background
   request rate (36 per ~3 min) and is worth a separate decision.
4. **The table has no column sorting** beyond the SORT dropdown (league / team /
   progress). Click-to-sort headers were not added; the dropdown satisfies the
   stability requirement.
5. **No row-level drill-down affordance is visible** — clicking a row opens the
   existing game card, but nothing in the row says so. A chevron or hover hint
   would help discoverability.

## 6. Boundaries

Production, the `:2262` mirror, Auto-Bet configuration/execution and the cohort
evaluator were untouched. Deployment target was `127.0.0.1:2263` only; the deploy
script refuses any other host and restarts only the transient
`blm-tc2263.service`. `:2262` health remained 200 throughout.
