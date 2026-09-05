# BLM V4 — Game-Time, Pace, Required-Pace, and Live-Line Audit Directive

## Purpose

This is a **READ-ONLY AUDIT DIRECTIVE** for BLM V4. The objective is to establish whether BLM correctly represents:

1. actual game time elapsed;
2. percentage/progress through the game;
3. actual scoring pace (points/minute);
4. required scoring pace to reach the relevant live target;
5. the current live total market line;
6. model fair/projected total at the current game state;
7. the relationship between actual pace, required pace, live line, and Over/Under ranges;
8. independent Over and Under evaluation.

**Do not modify code during this audit.**

## Hard Safety Rules

Do NOT:

- modify code;
- modify tests;
- modify the database;
- restart services;
- change dashboard files;
- change projection logic;
- change scorecard logic;
- change market logic;
- change thresholds or probabilities;
- backfill historical data;
- rescore historical predictions;
- commit or push code changes;
- reset, restore, stash, or clean the working tree.

All database investigation must use SQLite read-only mode:

```text
file:blm_pokerbet.db?mode=ro
```

If a confirmed defect is found, document it and STOP. A separate authorization is required before implementation.

## Repository-First Requirement

A new agent must first understand the actual repository rather than relying on summaries.

Run:

```bash
cd ~/BLM
git status --short
find blm_v4 -maxdepth 3 -type f | sort
find tests -maxdepth 2 -type f | sort
grep -RniE "30%|percent|percentage|elapsed|remaining|progress|game_time|game clock|clock|period|quarter|duration|total_line|fair|projected|over|under|edge|probability|win|pace|pts/min|required pace" blm_v4 tests --exclude-dir=__pycache__
```

Trace the actual data path:

```text
SOURCE
  ↓
COLLECTOR
  ↓
SNAPSHOT / MARKET STORAGE
  ↓
PROJECTION / FAIR TOTAL
  ↓
SCORECARD / SIGNALS
  ↓
API
  ↓
DASHBOARD
```

Identify the actual functions, fields, formulas, and transformations at every stage.

## 1. Investigate the “30%” Value

Determine exactly what the dashboard's `30%` represents.

It may be:

- elapsed-game percentage;
- remaining-game percentage;
- quarter progress;
- model confidence;
- probability;
- edge;
- projected completion;
- another metric.

Do not assume the user's interpretation is correct.

Trace:

- the backend calculation;
- input variables;
- API JSON property;
- dashboard JavaScript field;
- displayed label/context.

Determine whether the value is correctly calculated **and correctly labeled**.

## 2. Actual Game Time

Determine whether BLM can derive actual elapsed game time from:

- classification;
- period/quarter;
- countdown clock;
- game status;
- stored timestamps.

Classification-specific duration must remain:

```text
BETUAL_NBA  = 4 × 10 minutes = 40 minutes
CYBER_2K26  = 4 × 12 minutes = 48 minutes
```

Do not use a universal 40-minute denominator.

For a 40-minute game, conceptually:

```text
Q1 10:00 → 0 elapsed
Q1 05:00 → 5 elapsed
Q2 10:00 → 10 elapsed
Q2 05:00 → 15 elapsed
Q4 00:00 → 40 elapsed
```

For CYBER_2K26:

```text
Q1 12:00 → 0 elapsed
Q1 06:00 → 6 elapsed
Q2 12:00 → 12 elapsed
...
Q4 00:00 → 48 elapsed
```

Audit the actual implementation rather than assuming these examples are implemented.

## 3. Game Progress Percentage

If BLM exposes a game-progress percentage, determine its exact formula.

The mathematically expected concept is:

```text
elapsed_game_minutes / total_game_minutes × 100
```

Determine whether the current implementation instead uses a fixed duration, quarter number, wall-clock time, snapshot count, or another proxy.

Do not change it during this audit.

## 4. Live Total Market Line

Trace the authoritative live total-line source.

Inspect fields such as:

```text
total_line
home_total_line
away_total_line
line_value
market_type
market_name
over_price
under_price
captured_at
```

Determine:

1. which field is authoritative;
2. which market type/name represents MatchTotal;
3. how multiple simultaneous lines are represented;
4. which line the API/dashboard selects;
5. whether the selected line is actually current;
6. whether timestamp ordering is correct;
7. whether a stale line can be presented as current.

Do not modify market collection.

## 5. MANDATORY PACE ANALYSIS

Determine whether BLM tracks **actual scoring pace versus required scoring pace** throughout the live game.

This is essential to accurate live Over/Under ranges.

At minimum, investigate values equivalent to:

```text
Actual Pts/Min
Required Pts/Min
Difference Pts/Min
```

Conceptually:

```text
ACTUAL PACE
= points scored / game minutes elapsed
```

and, for a target total:

```text
REQUIRED PACE
= points still required to reach target / game minutes remaining
```

Do not invent the target. Determine whether BLM uses the current live total line, model fair total, or another explicit target.

## 6. Pace Must Be Time-Aware

Determine whether pace calculations use:

- current score;
- actual elapsed game time;
- time remaining;
- classification-specific total duration;
- current live market line.

At the same score and target, required pace must change as time remaining changes.

For example:

```text
Score = 90
Target = 180.5
```

with 20 minutes remaining:

```text
(180.5 - 90) / 20
```

with 10 minutes remaining:

```text
(180.5 - 90) / 10
```

The latter required pace must be twice the former.

Audit whether BLM behaves this way.

## 7. Pace Must Respond to the Live Line

At identical score and time remaining, changing the live line must change required pace.

Example:

```text
Score = 90
Time remaining = 20 minutes
```

For line 180.5:

```text
(180.5 - 90) / 20
```

For line 190.5:

```text
(190.5 - 90) / 20
```

These must produce different required-pace values.

Trace whether the actual implementation has this relationship.

## 8. Actual Pace Must Not Be Confused With Required Pace

If BLM exposes “pace”, determine exactly whether it means:

- actual Pts/Min;
- required Pts/Min;
- projected Pts/Min;
- another derived value.

Do not allow ambiguous labels to be treated as equivalent.

## 9. Fair / Projected Total

Trace the authoritative fair/projected-total calculation.

Identify whether it incorporates:

```text
current score
+ actual elapsed time
+ time remaining
+ observed scoring pace
+ projection of remaining scoring
= projected/fair final total
```

Determine whether classification-specific duration is respected.

Do not modify `projection.py` during the audit.

## 10. Live Line vs Fair/Projected Total

Determine whether BLM evaluates the **current live total line relative to the model's fair/projected total at the actual game state**.

The analysis should conceptually establish:

```text
CURRENT GAME STATE
→ current score
→ actual time elapsed
→ time remaining
→ actual pace
→ projected/fair final total

MARKET
→ current live total line

RELATIONSHIP
→ market line vs fair/projected total
→ required pace vs actual pace
→ Over/Under implications
```

Do not assume that a generic percentage or projection multiplier is sufficient.

## 11. Over and Under Must Be Independent

Do NOT compare Over win % and Under win % against each other as if one must beat the other.

Audit whether BLM evaluates independently:

```text
P(Over at current line)
P(Under at current line)
```

and whether both are conditioned on the same current game state and live line.

Trace the actual formulas and UI/API mapping.

## 12. Over/Under Range Generation

Determine whether live Over/Under ranges are conditioned on:

1. actual game time;
2. actual score;
3. observed scoring pace;
4. required pace to reach the current live line;
5. current live line;
6. classification;
7. time remaining.

If any component is absent, document it.

Do not call it a confirmed defect unless the repository's intended behavior/specification establishes that the component is required.

## 13. Real Production Data Audit

Use `blm_pokerbet.db` read-only.

Inspect representative recent games, including:

- at least one BETUAL_NBA game;
- at least one CYBER_2K26 game if available.

For representative snapshots, independently calculate:

```text
classification
period / quarter
clock
elapsed minutes
remaining minutes
current score
actual Pts/Min
current live total line
points required to target
required Pts/Min
projected/fair total if available
Over probability/range if available
Under probability/range if available
```

Compare these against the values exposed by BLM/API/dashboard.

Do not accept a formula because the output merely “looks reasonable.”

## 14. API / Dashboard Trace

Identify:

- the API endpoint supplying the dashboard;
- the JSON fields for game time/progress;
- the JSON fields for current market line;
- the JSON fields for fair/projected total;
- the JSON fields for actual/required pace;
- the JavaScript fields that render them.

Determine whether the backend has correct information that is simply being displayed incorrectly, or whether the backend calculation itself is wrong.

## 15. Test Coverage

Identify existing tests for:

- classification-specific duration;
- clock/quarter handling;
- projection;
- total line;
- Over/Under;
- API responses;
- dashboard data;
- pace;
- required pace.

Identify gaps for:

```text
actual elapsed game time
classification-specific progress
actual Pts/Min
required Pts/Min
pace vs required pace
live line vs fair total
line-dependent required pace
time-dependent required pace
independent Over/Under evaluation
```

Do not add tests during this audit.

## 16. Distinguish Separate Issues

Do not conflate:

### A. Stale replay
Finished game being re-adopted as live. This has already been fixed and is frozen.

### B. Game-time/progress representation
Whether the UI/model correctly represents actual game time and progress.

### C. Pace/range calculation
Whether actual Pts/Min and required Pts/Min are correctly tracked and used.

### D. Live-line/fair relationship
Whether the current live line is evaluated against the model's fair/projected total at the current game state.

### E. Over/Under independence
Whether Over and Under are independently evaluated.

The current audit covers B–E only.

## 17. Required Pace Audit Table

The final report must include:

| Pace Component | Present? | Correct? | Evidence |
|---|---|---|---|
| Actual game elapsed time | YES/NO | PASS/FINDING | ... |
| Time remaining | YES/NO | PASS/FINDING | ... |
| Actual Pts/Min | YES/NO | PASS/FINDING | ... |
| Required Pts/Min | YES/NO | PASS/FINDING | ... |
| Projected Pts/Min | YES/NO | PASS/FINDING | ... |
| Live line input | YES/NO | PASS/FINDING | ... |
| Classification-specific duration | YES/NO | PASS/FINDING | ... |
| Pace vs required pace | YES/NO | PASS/FINDING | ... |
| Over range uses pace | YES/NO | PASS/FINDING | ... |
| Under range uses pace | YES/NO | PASS/FINDING | ... |
| Pace recalculates as time changes | YES/NO | PASS/FINDING | ... |
| Required pace changes with market line | YES/NO | PASS/FINDING | ... |

## 18. Required Final Report

Return:

### REPOSITORY UNDERSTANDING
- relevant files;
- actual data flow;
- authoritative functions;
- API/dashboard path.

### GAME-TIME AUDIT
- clock source;
- elapsed-time calculation;
- total-duration calculation;
- progress calculation;
- classification handling;
- exact meaning of `30%`.

### PACE AUDIT
- actual Pts/Min;
- required Pts/Min;
- projected Pts/Min;
- time remaining;
- pace-vs-required relationship;
- response to time changes;
- response to live-line changes.

### LIVE MARKET AUDIT
- authoritative current-line field;
- market type/name;
- timestamp handling;
- multiple-line handling;
- stale-line risk.

### FAIR-VS-LIVE AUDIT
- fair/projected total;
- current market line;
- difference/edge;
- time remaining;
- Over probability/range;
- Under probability/range.

### OVER/UNDER AUDIT
Explicitly state whether Over and Under are evaluated independently.

### REAL-DATA VERIFICATION
Provide representative read-only DB/API evidence.

### TEST COVERAGE
List relevant existing tests and gaps.

### CONFIRMED DEFECTS
Only proven defects.

### CORRECT BEHAVIOR
Investigated areas that are working correctly.

### UNCERTAIN / REQUIRES FURTHER EVIDENCE
Do not label these defects.

### CODE CHANGES
`NONE — AUDIT ONLY`

### DATABASE CHANGES
`NONE`

### SERVICE RESTART
`NONE`

### FINAL VERDICT
Use exactly one:

`PASS — CURRENT IMPLEMENTATION MATCHES INTENDED GAME-TIME, PACE, AND LIVE-LINE BEHAVIOR`

or

`FINDING — SPECIFIC GAME-TIME/PACE/LIVE-LINE DEFECT IDENTIFIED; STOPPED BEFORE MODIFICATION`

## 19. Hard Stop

If a confirmed defect is identified:

**STOP. Do not fix it.**

Report:

1. exact file;
2. exact function;
3. exact current formula/behavior;
4. real-data evidence;
5. expected behavior;
6. smallest proposed correction;
7. tests required.

Wait for separate implementation authorization.

## Core Principle

The objective is not merely to display a percentage. The live analytical chain must be coherent:

```text
WHERE ARE WE?
→ actual game time

HOW FAST ARE WE SCORING?
→ actual Pts/Min

HOW FAST DO WE NEED TO SCORE?
→ required Pts/Min

WHERE DOES THAT PUT THE FINAL TOTAL?
→ projected/fair total

WHAT DOES THE MARKET SAY?
→ current live total line

WHAT IS THE DIFFERENCE?
→ market vs fair/projection

WHAT ARE THE OUTCOMES?
→ independent Over range/probability
→ independent Under range/probability
```

If the chain breaks anywhere, identify the exact break with evidence and stop before modification.
