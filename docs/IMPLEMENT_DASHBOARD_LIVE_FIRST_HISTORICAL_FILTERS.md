# BLM V4 — STRICT DASHBOARD IMPLEMENTATION DIRECTIVE
## Live-First Dashboard, Collapsible Historical Data, and Granular Filters

## PURPOSE

Redesign the BLM V4 dashboard so the primary operational view is clean, compact, and focused on **LIVE GAMES ONLY**.

Historical/completed-game information must remain available, but it must be moved into clearly separated **collapsible historical sections/tabs** with granular filters.

This is a dashboard information-architecture and usability implementation. Preserve the existing backend calculations and validated data semantics unless a separate defect is proven.

---

# 1. HARD REQUIREMENT — LIVE GAMES FIRST

The default dashboard view must show **only currently live games**.

Do NOT mix:

- live games;
- finished games;
- old scorecard checkpoints;
- historical predictions;
- historical settlements

into the primary live-game list.

The initial screen should answer immediately:

> **What games are live right now, what is happening, what is the current line, and what does the model currently say?**

Completed games must not visually compete with live games.

If there are zero live games, show a clean explicit empty state such as:

`NO LIVE GAMES`

rather than filling the screen with historical games.

---

# 2. LIVE GAME CARD — REQUIRED CONTENT

Each live game should have a compact, information-dense card/row.

At minimum show:

- game/team identity;
- classification;
- live status;
- period;
- countdown clock;
- current score;
- elapsed game time or progress;
- remaining game time;
- current live total line;
- line freshness/status;
- actual Pts/Min;
- required Pts/Min to reach the current live line;
- pace gap;
- fair/projected total;
- market-vs-fair difference;
- existing validated Over/Under directional signal where applicable.

Do not overload the default card with historical checkpoint tables.

The user should be able to understand the live state without opening a modal.

---

# 3. PACE DISPLAY MUST BE SYMMETRICAL

Do NOT make the dashboard visually favour Under merely because required pace is above current pace.

Display both directions neutrally:

```text
ACTUAL PACE       4.2 pts/min
REQUIRED TO LINE  5.6 pts/min
PACE GAP          +1.4 pts/min
```

And conversely:

```text
ACTUAL PACE       5.8 pts/min
REQUIRED TO LINE  4.1 pts/min
PACE GAP          -1.7 pts/min
```

The dashboard must not label either situation as a "trap".

The purpose is to expose the state so later clean statistical analysis can determine whether either direction has predictive value.

If trajectory fields have already been implemented, optionally show a compact indicator for:

- recent pace;
- acceleration/deceleration;
- 1–3 minute pace trend.

Do not invent probability from pace.

---

# 4. LIVE LINE MUST BE UNAMBIGUOUS

Clearly label the current market number as:

`LIVE TOTAL LINE`

Do not display a stale line as if it were current.

If the API marks the line stale/missing, show the existing status semantics such as:

- LIVE
- STALE
- MISSING

and suppress any live-edge claim when the line is not sufficiently fresh.

Retain the existing market-source/freshness behaviour.

Do not redesign market selection in this dashboard task.

---

# 5. FAIR TOTAL MUST BE DISTINCT FROM LIVE LINE

Never visually merge:

`LIVE TOTAL LINE`

with:

`FAIR / PROJECTED TOTAL`

They must be clearly distinguishable.

Show the relationship explicitly, for example:

```text
LIVE LINE       214.5
FAIR TOTAL      209.0
DIFFERENCE       -5.5
```

Use the existing API/model values.

Do not change the fair-total formula as part of this dashboard task.

---

# 6. CHECKPOINT PERCENTAGE MUST BE CLARIFIED

The existing `10% / 20% / 30% ...` checkpoint value is a **checkpoint target**, not a probability.

Do not present a bare `30%` in a context where it could be mistaken for:

- probability;
- confidence;
- edge;
- Over probability;
- Under probability.

Where historical checkpoint data is shown, label it clearly as:

`CHECKPOINT`

and show actual game state alongside it when available:

```text
CP 30%
Q2 07:45
12.25 / 40.00 min
Actual 30.6%
```

For CYBER_2K26 use the correct 48-minute denominator.

Use API-provided checkpoint fields rather than independently reconstructing them in JavaScript when those fields already exist.

---

# 7. HISTORICAL DATA — SEPARATE FROM LIVE

Create a dedicated historical area.

It should be collapsed by default.

Suggested structure:

```text
LIVE GAMES
  [live game cards]

HISTORICAL
  > Completed Games
  > Scorecards
  > Predictions
  > Market vs Fair
  > Pace Analysis
  > Results / Settlement
```

The exact tab names may follow the existing dashboard terminology, but the separation must be obvious.

Historical content must not consume the primary viewport when the dashboard opens.

---

# 8. COLLAPSIBLE HISTORICAL SECTIONS

Historical sections must be independently collapsible/expandable.

Default state:

- LIVE GAMES = expanded;
- HISTORICAL = collapsed;
- individual historical subsections = collapsed unless explicitly selected.

Do not force the user to scroll through hundreds of historical rows to reach live games.

Preserve the selected collapse/filter state during normal dashboard refreshes where practical.

Do not cause live polling to unexpectedly reopen historical sections.

---

# 9. GRANULAR HISTORICAL FILTERS

Historical data must support granular filtering.

At minimum provide filters for:

### Game
- game ID;
- team/game identity.

### Classification
- BETUAL_NBA;
- CYBER_2K26;
- other supported classifications.

### Status
- completed;
- cancelled/void if represented;
- other existing statuses.

### Date/time
- today;
- yesterday;
- last 7 days;
- last 30 days;
- custom date range where practical.

### Checkpoint
- 10%;
- 20%;
- 30%;
- 40%;
- 50%;
- 60%;
- 70%;
- 80%;
- 90%;
- final.

### Direction
- Over;
- Under;
- No Edge;
- existing directional categories.

### Market state
- live line range;
- fair-total range;
- market-vs-fair difference range where supported.

### Pace state
- actual pace range;
- required pace range;
- pace gap range;
- positive pace gap;
- negative pace gap.

Do not require every filter to be visible simultaneously if that makes the UI cluttered. Use an expandable `FILTERS` control or grouped filter panel.

---

# 10. HISTORICAL PACE ANALYSIS

Because the new clean metrics work is specifically intended to study both apparent Under-friendly and Over-friendly states, historical filtering must eventually allow the user to isolate both directions.

At minimum make it possible to answer:

> Show games where required pace was substantially ABOVE actual pace.

and:

> Show games where required pace was substantially BELOW actual pace.

Do not hard-code a conclusion that either is a trap.

The UI should expose the data for empirical analysis.

If trajectory data exists, filters should eventually support:

- pace acceleration;
- pace deceleration;
- subsequent 1-minute pace;
- subsequent 3-minute pace;
- subsequent 5-minute pace;
- final settlement.

Only expose fields actually present in the API/database.

---

# 11. LIVE/HISTORICAL DATA SEPARATION MUST BE SEMANTIC

Do not simply hide completed rows with CSS while continuing to treat them as live data.

The frontend must use explicit status/state filtering from the API data.

A game belongs in LIVE only if it is currently live according to the validated backend state.

Finished/replayed/stale-invalid states must not appear as live.

Do not duplicate or fabricate game-state logic in the frontend.

---

# 12. POLLING / REFRESH BEHAVIOUR

The live dashboard must continue polling/refreshing normally.

On refresh:

- live games update in place;
- historical sections do not suddenly expand;
- historical filters do not reset unnecessarily;
- a finished live game moves out of LIVE automatically;
- a newly live game appears in LIVE automatically.

Do not introduce additional scraping.

Use the existing API.

---

# 13. PERFORMANCE

The default dashboard must NOT render thousands of historical DOM rows merely because historical data exists.

Historical data should be:

- loaded on demand;
- paginated;
- filtered server-side where supported;
- or otherwise bounded.

Do not create a frontend performance problem by moving all historical records into hidden HTML.

If an API endpoint currently returns excessive historical data for the live dashboard, identify the smallest safe API/query adjustment needed, but do not modify backend behaviour unnecessarily.

---

# 14. VISUAL HIERARCHY

The dashboard should have a clear hierarchy:

```text
BLM V4

LIVE GAMES                         [count]
────────────────────────────────────────

GAME CARD
Teams / classification
Score + clock
Live line
Fair total
Actual pace / required pace
Pace gap
Signal

GAME CARD
...

────────────────────────────────────────
HISTORICAL                         [collapsed]
```

Avoid:

- excessive tables in the main view;
- duplicated information;
- repeated headers;
- huge empty cards;
- historical rows mixed with live rows;
- unexplained percentages;
- visually competing panels.

The design should prioritise fast scanning during live betting analysis.

---

# 15. DO NOT CHANGE MODEL LOGIC

This dashboard implementation must NOT alter:

- fair-total weighting;
- projection formulas;
- classification duration;
- replay handling;
- market-selection logic;
- Over/Under settlement logic;
- statistical baselines;
- z-scores;
- probability models.

If the dashboard exposes a backend defect, document it separately rather than silently changing model behaviour.

---

# 16. DO NOT USE OLD HISTORICAL STATISTICS AS A NEW BASELINE

Historical UI visibility does NOT mean historical data is valid for the new clean statistical model.

The dashboard may display historical operational records for inspection.

However:

- old historical data must not be imported into the clean statistical database;
- old historical distributions must not become new z-score baselines;
- old contaminated metrics must not be presented as a validated clean statistical population.

Clearly distinguish **historical records** from **clean statistical data**.

---

# 17. RESPONSIVE / DESKTOP PRIORITY

The primary use case is desktop live monitoring.

Ensure the live-game cards use available horizontal space efficiently without creating excessive scrolling.

Do not sacrifice readability for density.

Important numbers should remain immediately visible:

`CLOCK → SCORE → LIVE LINE → FAIR → ACTUAL PACE → REQUIRED PACE → GAP → SIGNAL`

---

# 18. REQUIRED TESTS

Add or update tests for:

1. only live games appear in the default live-game list;
2. completed games are excluded from LIVE;
3. historical games remain accessible;
4. historical sections default collapsed;
5. filters can isolate classification;
6. filters can isolate checkpoint;
7. filters can isolate Over/Under direction;
8. filters can isolate positive pace gap;
9. filters can isolate negative pace gap;
10. live refresh does not reset historical collapse state unnecessarily;
11. live refresh does not reset selected historical filters unnecessarily;
12. stale/invalid finished games do not appear as live;
13. current live line remains distinct from fair total;
14. checkpoint target percentage is distinct from actual progress;
15. no historical rows are rendered unnecessarily in the default live view.

Where frontend tests already exist, follow their established testing pattern.

---

# 19. REAL-DATA VERIFICATION

After implementation, verify against real API data.

Demonstrate:

- number of live games shown;
- number of completed games excluded from live view;
- historical filters returning expected subsets;
- current live line displayed correctly;
- actual/required pace displayed correctly;
- fair total displayed separately;
- checkpoint state displayed correctly.

Use representative BETUAL_NBA and CYBER_2K26 games where available.

---

# 20. VALIDATION ORDER

### Stage A
Inspect current dashboard and API data flow.

### Stage B
Implement live-first information architecture.

### Stage C
Implement collapsible historical sections.

### Stage D
Implement granular filters.

### Stage E
Add/update tests.

### Stage F
Run the targeted frontend/backend tests.

### Stage G
Run the full existing suite.

### Stage H
Restart services only if changed files require it.

### Stage I
Verify live API and dashboard behaviour against real data.

---

# 21. PROHIBITED ACTIONS

Do NOT:

- reset the repository;
- stash or discard unrelated changes;
- overwrite unrelated working-tree work;
- delete historical operational records;
- rewrite old predictions;
- alter the frozen replay fix;
- alter classification duration;
- change projection mathematics;
- create a second collector;
- import old statistics into the clean metrics database;
- hide backend defects through frontend-only transformations;
- claim historical statistics are clean merely because they are displayed.

---

# 22. FINAL REPORT

Return an evidence-based report containing:

## DASHBOARD STRUCTURE
- live-first layout;
- live fields shown;
- historical sections;
- collapse behaviour.

## FILTERS
- implemented filters;
- which operate client-side/server-side;
- exact fields used.

## PACE VISIBILITY
- actual pace;
- required pace;
- pace gap;
- trajectory fields if available.

## MARKET / FAIR
- live line;
- freshness/status;
- fair total;
- market-vs-fair.

## TESTS
- targeted result;
- full-suite result.

## LIVE VERIFICATION
- live games detected;
- historical games excluded;
- refresh behaviour;
- stale/replay safety.

## CODE CHANGES
List exact files changed.

## DATABASE CHANGES
State exactly whether any database was modified.

## SERVICE STATUS
State whether services were restarted and their resulting status.

If any requirement cannot be proven, report `NOT VERIFIED` rather than assuming success.

---

# FINAL DESIGN PRINCIPLE

The dashboard is a **live decision interface first** and a **historical research interface second**.

The opening screen must be clean enough to use while actively watching games.

Historical information must remain powerful, but it belongs behind explicit collapsible sections and granular filters.

The user should never have to fight through historical data to find the games that are live now.
