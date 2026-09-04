# BLM Agent Directives — Research Phase Slices 1–4

## Purpose

This document is the authoritative execution brief for the next four BLM engineering slices.

BLM is currently a **historical/live market data analysis system**. Automatic prediction is explicitly deferred.

The immediate objective is to establish a trustworthy dataset and determine what relationships exist between:

- checkpoint game state
- contemporaneous market total line
- actual scoring pace
- required scoring pace
- market deviation
- eventual verified final total

Only after this research phase produces trustworthy evidence should automatic prediction logic be considered.

## Global execution rules

**DO NOT IMPROVISE.**

The agent must execute only the currently authorized slice.

The agent must not:

- build or improve automatic prediction logic
- create betting recommendations
- create Over/Under side-selection logic
- compare Under Win % against Over Win % as a selection mechanism
- add prediction UI
- add prediction endpoints solely for this research phase
- fabricate missing values
- silently substitute missing market lines
- make unrelated refactors or fixes
- restart services unless explicitly authorized
- commit or push changes unless explicitly authorized
- bundle multiple slices into one implementation

If an additional defect is discovered outside the current slice, **REPORT IT AND STOP**.

Each slice ends with a written report and an explicit stop. The next slice requires explicit authorization.

---

# SLICE 1/4 — HISTORICAL DATA INTEGRITY FOUNDATION

## Objective

Establish a trustworthy historical game/checkpoint foundation before any prediction work.

The only objective is to make the underlying observations trustworthy.

## Scope

### 1. Canonical game identity

Trace the complete Path A identity flow:

`source event → event ID → teams → game/session identity → stored game_id → checkpoint → historical population`

Determine exactly how `game_id` is generated.

Prove whether one stored game ID can contain:

- multiple games
- multiple team pairings
- score resets
- different source event IDs
- fragments of different games

A historical observation must be attributable to exactly one canonical source game.

If identity cannot be proven, mark the observation invalid.

### 2. Checkpoint integrity

Determine exactly how checkpoints are generated.

Required conceptual checkpoints:

`10%, 20%, 30%, 40%, 50%, 60%, 70%, 80%, 90%`

Determine whether each checkpoint belongs to the same canonical game.

Do not silently repair historical records.

### 3. Game-end / final total

Trace how BLM determines that a game is finished.

Determine:

- how live → final is detected
- whether final score is authoritative
- whether final total can be confused with last-seen live score
- whether LineTracker state is reset
- whether historical processing can run against incomplete games

A historical result must use a **verified final game state**.

### 4. Staleness / duplicate observations

Determine whether identical source observations are repeatedly written.

Identify:

- unchanged timestamps
- duplicate checkpoint observations
- stale replay
- repeated identical game states
- duplicate source observations

Do not invent a deduplication rule without first documenting current behavior.

### 5. Fabricated/default values

Trace and identify every hard-coded/default/fallback analytical value, especially:

- pace defaults
- expected pace
- line defaults
- `current_line` fallback
- `total/2`
- `expected_total=current total`
- confidence constants
- fabricated historical multipliers
- missing line converted to zero

These are not acceptable as empirical source data.

For missing source data, prefer:

`NULL / INVALID / INSUFFICIENT DATA`

### 6. Odds

Trace the four odds fields identified by the audit.

Determine:

- source
- extraction path
- storage path
- whether values are genuinely populated
- whether values are fabricated/defaulted

Do not fabricate odds.

## Deliverable

Return a forensic report containing:

A. canonical game identity flow  
B. checkpoint flow  
C. game-end/final-total flow  
D. duplicate/staleness behavior  
E. every fabricated/default analytical field  
F. odds extraction/storage status  
G. exact files/functions responsible  
H. severity of each defect  
I. recommended next action for each defect

**No code changes unless absolutely necessary to produce requested evidence.**

**STOP AFTER THE REPORT.**

---

# SLICE 2/4 — CHECKPOINT-LEVEL MARKET-LINE FOUNDATION

## Objective

Establish the historical relationship:

`CHECKPOINT → GAME STATE → CONTEMPORANEOUS MARKET LINE → EVENTUAL VERIFIED FINAL TOTAL`

This is an analysis-data requirement, not a prediction feature.

## Required checkpoints

For every valid canonical game:

`10%, 20%, 30%, 40%, 50%, 60%, 70%, 80%, 90%`

Each checkpoint must preserve its own market observation.

## Required data

For every checkpoint record, preserve:

- `canonical_game_id`
- `checkpoint_pct`
- `checkpoint_timestamp`
- `elapsed_game_time`
- `remaining_game_time`
- `score_at_checkpoint`
- `market_total_line`
- `market_line_timestamp`
- `line_source`
- `verified_final_score`
- `verified_final_total`
- `checkpoint_outcome`

## Outcome definitions

**UNDER**

`final_total < checkpoint_market_line`

**OVER**

`final_total > checkpoint_market_line`

**PUSH**

`final_total == checkpoint_market_line`

**INVALID**

Required source data cannot be verified.

## Critical market-line rule

The market line must be the actual line available at the checkpoint.

Never substitute:

- opening line
- previous checkpoint line
- later checkpoint line
- current live line
- another game's line
- frozen line
- default line
- estimated line
- fabricated line

The same final game total may legitimately resolve UNDER at one checkpoint and OVER at another checkpoint when the market line moves.

Example:

- 10%: market line 198.5, final total 194 → UNDER
- 70%: market line 193.5, final total 194 → OVER

This behavior must not be collapsed into one game-level label.

## Historical population

Historical statistics must be checkpoint-specific.

Example:

`10% Under population = valid 10% checkpoints where final_total < actual 10% market line`

`10% Over population = valid 10% checkpoints where final_total > actual 10% market line`

Repeat independently for 20% through 90%.

Never use:

`Under Win % > Over Win %`

or

`Over Win % > Under Win %`

as a betting-selection mechanism.

Under and Over are independent empirical populations.

## Missing-data rule

If the contemporaneous market line cannot be verified:

- do not substitute
- mark `INVALID / INSUFFICIENT DATA`
- exclude from the relevant historical population

## Deliverable

Provide:

1. exact source of checkpoint market lines
2. exact timestamp relationship
3. exact storage location
4. exact current schema/path
5. whether each checkpoint can preserve its own line
6. examples of contamination/frozen-line behavior
7. proposed minimal schema change, if required
8. historical-data migration implications, if any

**Do not implement changes yet unless explicitly authorized.**

**STOP AND REPORT.**

---

# SLICE 3/4 — PACE VS MARKET-LINE DEVIATION DATA

## Objective

Extend the checkpoint-level historical dataset so BLM can analyze:

`ACTUAL GAME PACE vs PACE REQUIRED TO REACH THE CONTEMPORANEOUS MARKET LINE`

The research question is whether market lines show recurring deviations from the scoring pace implied by actual game state.

This is analysis only.

## Explicit exclusions

Do not:

- build auto-predictions
- build betting signals
- build betting recommendations
- add prediction UI
- claim that a market deviation is "wrong"

A deviation is an observation. Its significance must be determined empirically.

## Required checkpoint data

At every valid checkpoint:

`10%, 20%, 30%, 40%, 50%, 60%, 70%, 80%, 90%`

preserve:

- `canonical_game_id`
- `checkpoint_pct`
- `checkpoint_timestamp`
- `elapsed_game_time`
- `remaining_game_time`
- `points_scored`
- `market_total_line`
- `market_line_timestamp`
- `verified_final_total`

## Derived analytical variables

### 1. Actual pace

`actual_pace_pts_per_min = points_scored / elapsed_game_minutes`

Calculate only from valid elapsed game time and valid score.

### 2. Required remaining pace

The scoring rate required from the checkpoint forward to finish at the contemporaneous market total line:

`required_remaining_pace_pts_per_min = (market_total_line - points_scored) / remaining_game_minutes`

### 3. Pace gap

`pace_gap_pts_per_min = actual_pace_pts_per_min - required_remaining_pace_pts_per_min`

### 4. Pace ratio

`pace_ratio = actual_pace_pts_per_min / required_remaining_pace_pts_per_min`

Only calculate derived values when the underlying variables are valid and denominators are non-zero.

## Important distinction

Do not confuse:

- current pace projected to the end

with:

- remaining pace required to reach the market line

They may both be useful analytical variables but must remain separate.

## Analytical objective

The dataset must allow analysis of:

`actual pace vs required pace vs market line vs final total`

independently at each checkpoint.

The purpose is to determine whether statistically meaningful relationships exist between pace deviation and eventual final totals.

## Market-deviation rule

Do not automatically classify a large deviation as a bookmaker error.

A large deviation is only an observation.

Preserve the raw variables so thresholds can be tested later.

Do not prematurely assume thresholds such as +0.20, +0.30, or +0.50 are meaningful unless they are explicitly used only as analytical buckets.

## Deliverable

Produce:

A. exact formula currently possible from available source data  
B. exact source fields required  
C. exact files/functions involved  
D. whether current BLM pace values are genuine or fabricated  
E. whether current market lines are contemporaneous  
F. whether required pace can currently be calculated reliably  
G. proposed checkpoint analytical schema  
H. examples of missing/invalid data  
I. recommended analytical queries that can later be run

**STOP.**

**WAIT FOR EXPLICIT AUTHORIZATION FOR IMPLEMENTATION.**

---

# SLICE 4/4 — HISTORICAL MARKET-DEVIATION / TREND ANALYSIS

## Objective

Analyze the validated historical dataset to determine whether recurring relationships exist between:

- checkpoint
- game state
- actual pace
- required pace
- market line
- final total

Primary research question:

> Do market lines show recurring deviations from the scoring pace implied by the actual game state, and do those deviations correlate with eventual game outcomes?

This is **descriptive/statistical analysis only**.

## Explicit exclusions

No:

- betting recommendations
- automated side selection
- prediction confidence
- prediction UI
- prediction rules

The analysis must answer **WHAT DOES THE DATA SHOW?**, not **WHAT SHOULD THE MODEL BET?**

## Data-quality gate

Use only validated observations.

Exclude and report:

- contaminated game IDs
- incomplete games
- missing market lines
- fabricated values
- stale replay duplicates
- unverifiable final totals
- substituted lines

Do not silently repair bad data during analysis.

## Analysis level

Perform the analysis separately for:

`10%, 20%, 30%, 40%, 50%, 60%, 70%, 80%, 90%`

Do not initially combine all checkpoints into one population.

## Primary variables

Analyze:

- `actual_pace_pts_per_min`
- `required_remaining_pace_pts_per_min`
- `pace_gap_pts_per_min`
- `pace_ratio`
- `market_total_line`
- `points_at_checkpoint`
- `elapsed_time`
- `remaining_time`
- `final_total`
- `checkpoint_outcome`

## Primary relationships

1. Actual pace vs required pace.
2. Pace gap vs final total.
3. Pace gap vs Under/Over/Push outcome.
4. Market line vs eventual final total.
5. Required pace vs eventual final scoring pace.
6. Size of pace deviation vs outcome.
7. All relationships independently at every checkpoint.

## Market-deviation buckets

Do not assume thresholds beforehand.

First inspect the empirical distribution.

Then, if sample size permits, analyze ranges such as:

- negative deviation
- near-zero deviation
- moderately positive deviation
- strongly positive deviation
- moderately negative deviation
- strongly negative deviation

Use transparent, data-derived buckets.

Do not cherry-pick buckets that produce favorable results.

## Sample-size discipline

Every result must report:

- total sample count
- valid count
- excluded count
- Under count
- Over count
- Push count

Do not present percentages without sample sizes.

## Trend analysis

Determine whether the relationship changes as the game progresses.

Example research question:

> Does a +0.30 pts/min deviation at 10% behave differently from the same +0.30 pts/min deviation at 60%?

Also investigate whether market lines become more or less closely aligned with eventual final scoring as the checkpoint progresses.

## Important interpretation rule

The objective is NOT to prove bookmakers are wrong.

The objective is to determine whether observable deviations from normal historical relationships exist and whether they correlate with outcomes.

Only empirical evidence should determine whether a relationship exists.

## Under / Over separation

Never create a combined side-selection metric.

Do not use:

`Under Win % > Over Win %`

or:

`Over Win % > Under Win %`

for side selection.

Each checkpoint's outcome is evaluated independently against that checkpoint's actual market line.

## Final output

Produce an analytical report containing:

1. data quality summary
2. sample size by checkpoint
3. market-line distribution by checkpoint
4. actual pace distribution
5. required pace distribution
6. pace-gap distribution
7. relationship between pace gap and final total
8. relationship between pace gap and Under/Over outcome
9. checkpoint-by-checkpoint trends
10. statistically interesting deviations
11. weak/nonexistent relationships
12. explicit warnings where sample size is insufficient

Do not turn findings into prediction rules.

**STOP AFTER THE ANALYSIS.**

**WAIT FOR EXPLICIT AUTHORIZATION BEFORE ANY PREDICTION IMPLEMENTATION.**
