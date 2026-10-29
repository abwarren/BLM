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

### 3.3 Which lifecycle artifacts are canonical

Four lifecycle runs exist under `/tmp/tc-evidence`; not all of them are evidence.

| Artifact | Result | Status |
|---|---|---|
| `poll_final.out` (after `ee94113`, settled server) | 3 / **0** / 4 — 5/5 | **canonical** |
| `poll2.out` (after `7a9ac79`, settled server) | 3 / **0** / 4 — 5/5 | canonical (same code) |
| first run, before any fix | 3 / **2** / 4 — 4/5 | **the defect measurement** — the failure it reports is the bug this commit fixes |
| run started while a deploy restarted the server | 2 / 0 / 3 — 4/5 | **invalid** — the server was down inside the first window, so only two intervals were observed. Not a product result. |

Lesson kept in the harness: never start an acceptance run in the same command as
a deploy; the deploy's restart can land inside a measurement window and produce a
false failure (or a false "away = 0").

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

---

# Phase 2 — charcoal terminal redesign

**Commits:** `b55f6e8` (theme, market zone, signal tags) · `0f5be49` (filter
toolbar, inline ADVANCED) · `228545b` (toolbar row grouping).
**Deployed:** `127.0.0.1:2263` from `228545b`; worktree clean.

## Why, from the populated screenshots

The earlier captures were taken in the default state, which on this slice is zero
rows — they could not show density or hierarchy. Populated captures at 1440 and
1280 (SHOW ALL) showed a flat blue-black field: header, toolbar and rows shared
one surface; the price 1.90 was typographically identical to the line 178.5;
numeric columns floated in uniform grey; SIGNAL could only ever be a dash because
it rendered as plain text; and the ADVANCED popover floated over the toolbar and
clipped the RESET button.

## What changed

| Area | Before | After |
|---|---|---|
| Palette | blue-black surfaces | neutral charcoal (`#141517` / `#1a1c1f` / `#202327`) with the cyan accent carrying hierarchy |
| Rows | 34 px, near-invisible separation | 32-34 px, real zebra contrast, cyan hover, focus ring |
| Numbers | uniform grey | near-white, tabular, right-aligned |
| Market | line and price identical | MARKET zone tinted; price bold cyan at 13 px, line in a lighter slate |
| Signal | plain text (`–`) | pill: amber `ACTIVE`, cyan `CP 75%`, dim `—` |
| Cohort | plain text | bordered tag |
| Toolbar | controls pinned left, empty middle, RESET stranded | `FILTERS` rule, boxed dataset tabs, grouped selectors, search, `ADVANCED` |
| ADVANCED | absolutely positioned popover that covered RESET | inline disclosure that pushes content, never covers it |
| Chips | own detached line | one row with `ACTIVE` label, row count and RESET |
| Nav | — | active destination is a filled cyan pill, consistent with the toolbar |

## Verification on the deployed build

- interaction/layout acceptance: **26/26** (`acc_redesign.out`)
- three viewports 1440×900 / 1280×800 / 1152×800: no page-level overflow, filters
  and presentation toggle visible
- no console errors, no page errors, no failed requests
- row order and scroll position still survive a poll; no duplicate rows or poll
  loop after navigation
- read-only lifecycle: **5/5**, away-window **0** requests

## Remaining, not fixed

1. With ADVANCED **open**, its summary occupies a row above the expanded body, so
   the toolbar reads as three rows. Closed (the default) it shares the row with
   the ACTIVE chips and RESET. Cosmetic; not worth another CSS pass without a
   visual sign-off.
2. SIGNAL and COHORT are dashes for every row in this slice (no live games, no
   cohort membership on ended games) — correct, but the tags cannot be visually
   exercised here.
3. The collector banner still costs ~30 px at the top on this host.
4. REQ / ACT / PROG stay dashed because the projector block is live-only.

## Phase 2b — banner click-through (commit `404f758`)

A capture run died with `collectorBanner intercepts pointer events` while
clicking a filter control. The banner is prepended to `<body>` as
`position: sticky; top: 0; z-index: 999` and carries text only, but had no
`pointer-events` rule, so it floated above the sticky top bar and swallowed
clicks on anything scrolled underneath it.

`#collectorBanner { pointer-events: none; }` restores click-through; the element
has no interactive child, so nothing is lost. Probe after deploy:
`pointer_events: none`, and `document.elementFromPoint` at its centre no longer
returns the banner.

Its stacking over the top bar when the page is scrolled remains cosmetic and was
not changed. Acceptance after the change: **26/26**, no console errors.
