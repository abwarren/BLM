# BLM Phase 1 — Clean Data Roadmap

**Status:** ACTIVE — CLEAN-DATA PHASE

**Purpose:** Establish a trustworthy, auditable, leakage-free dataset before any ML retraining, adaptive weighting, or model optimization.

> **READ THIS FIRST**
>
> This document is deliberately explicit so that a weak or inexperienced agent can execute the work without guessing. The agent must work through the phases in order, stop at every gate, and never silently repair evidence while auditing it.

---

## 0. Non-negotiable operating rules

### 0.1 Current objective

The immediate objective is **clean extraction and clean historical/live data capture**.

We are **not** currently optimizing the model.

Do not start ML training, adaptive weighting, model tuning, or performance optimization until the dataset release gate at the end of this document is passed.

### 0.2 Audit before modification

The default workflow is:

```text
AUDIT
  -> FIND THE EVIDENCE
  -> DOCUMENT THE ROOT CAUSE
  -> PROPOSE THE FIX
  -> WAIT FOR APPROVAL WHEN REQUIRED
  -> IMPLEMENT THE MINIMAL FIX
  -> RE-RUN THE AUDIT
  -> RECORD THE RESULT
```

Do not make a change merely because a number “looks wrong.” First trace the source and prove the invariant that is violated.

### 0.3 Never fabricate historical data

Do not interpolate, invent, backfill, average, or reconstruct historical observations unless the source data itself supports the reconstruction and the operation has been explicitly approved.

In particular, a missing 10-second historical score snapshot must remain a missing observation. Do not fabricate one from two surrounding observations.

### 0.4 Preserve raw data

Raw source observations are authoritative evidence. Derived analytical layers must never overwrite the raw layer.

Keep raw and derived values conceptually and physically separable whenever the schema allows it.

### 0.5 Secret handling

Do not print API keys, passwords, access tokens, cookies, private keys, or other credentials into reports, logs, commits, or issue comments.

### 0.6 Production safety

Do not restart, stop, kill, reset, roll back, format, wipe, or otherwise disrupt the live system as part of an audit unless the current step explicitly requires an approved operational change.

### 0.7 Git safety

Never use `reset --hard`, `clean -fd`, blind checkout/revert, force push, or rebase as a shortcut for resolving an audit problem.

---

# 1. Baseline / Freeze

## Goal

Create a complete baseline before touching the collector, database, API, frontend, or model.

## Required inventory

Record:

- Git branch and HEAD SHA
- working-tree status
- running service name(s), PID(s), and start time
- database path and size
- relevant schema for `snapshots`, `market_observations`, `predictions`, `prediction_scores`, `checkpoint_market`, `game_results`, and `market_history`
- row counts in each relevant table
- existing backups and their integrity status
- current collector configuration and nominal cadence

## Required evidence

The baseline report must be reproducible from commands or queries. Do not rely on memory.

## Gate 1

**PASS only when:** baseline is recorded and no unexplained destructive state exists.

**FAIL:** stop; resolve the baseline ambiguity first.

---

# 2. Trace the real score/state capture loop

## Known problem to investigate

Historical audit found approximately **90-second actual score/state snapshot cadence** despite an intended nominal **20-second collector tick**.

The working hypothesis is that page/network/parse work is consuming the intended interval and the scheduler is then sleeping again, making the total cycle much slower than intended.

This is a hypothesis until measured.

## Trace exactly

Follow:

```text
_tick()
  -> tick_start
  -> network/page round-trip
  -> parsing
  -> _store_list_snapshot
  -> _capture_event_state
  -> database write
  -> sleep calculation
  -> next tick
```

Identify which code controls each step.

Measure:

- tick start
- capture start
- capture end
- processing end
- sleep duration
- total cycle duration

## Required decision

Determine whether the scheduler behaves like:

```text
work + full interval sleep
```

instead of:

```text
work + remaining time until next scheduled boundary
```

Also determine whether the round-robin full-event capture architecture contributes to sparsity.

## Do not fix yet

This is trace-first. Produce the evidence and proposed minimal change before implementation.

## Gate 2

**PASS only when:** the exact reason for the observed cadence is established from code/timing evidence.

---

# 3. Establish a real 10-second score/state capture cadence

## Objective

Create a reliable live score/state stream at approximately **10-second intervals**.

The 10-second target is an analytical target, not a requirement to destroy or resample the raw market stream.

## Scheduling requirement

The scheduler should use a target-boundary approach:

```text
next_tick = previous_tick_start + 10 seconds
sleep = max(0, next_tick - now)
```

This prevents work time from being added on top of the configured interval.

If a capture takes longer than 10 seconds, do not introduce unsafe concurrent captures merely to hide the problem. Measure and report the overrun.

## Preserve raw observations

Each captured score/state observation should preserve high-precision `captured_at` and the raw source fields.

Do not silently round timestamps to seconds.

## Add instrumentation

Where practical, record or log:

- `tick_start`
- `capture_start`
- `capture_end`
- `processing_end`
- computed sleep
- cycle duration

## Scope restriction

This step may modify collector scheduling/capture logic only.

Do not change:

- `market_observations` ingestion
- settlement logic
- checkpoint market selection
- OLV/CLV semantics
- BLM prediction formula
- model weighting
- historical outcomes

## Gate 3

**PASS only when:** tests pass and real measured capture intervals demonstrate the new cadence.

A configuration value of `10s` is not evidence of success.

---

# 4. Prove the live 10-second cadence

## Measure real data

For completed games and a controlled live sample, calculate:

- observations per game
- mean interval
- median interval
- P10
- P25
- P75
- P90
- minimum
- maximum
- gaps >5 seconds
- gaps >10 seconds
- gaps >15 seconds
- gaps >30 seconds
- gaps >60 seconds

Separate:

1. score/state snapshots
2. full event-state snapshots
3. raw WS market observations
4. model/checkpoint computations

## Important

Do not call an interval “10 seconds” if the actual data is 20, 30, 60, or 90 seconds.

Do not average bad intervals away. Report them.

## Gate 4

**PASS only when:** score/state cadence is demonstrably close enough to 10 seconds for the intended analytical use, and remaining outages are quantified.

---

# 5. Protect the raw WebSocket market feed

## Known data source

The `market_observations` WebSocket stream is the authoritative raw market source.

Historical audit found it to be **event-driven**, with bursts and long quiet periods. The raw observations must be preserved as received.

## Never do this

Do not:

- average raw WS observations and overwrite them
- downsample the raw feed
- collapse bursts into one row
- interpolate quiet periods
- round away line precision
- replace raw observations with a 10-second timer feed

## Validate

For raw market observations verify:

- source timestamp is preserved
- market line is x.5
- burst observations are retained
- quiet periods remain identifiable
- line movement is observable
- market timestamp <= checkpoint timestamp when used for a checkpoint
- no future observation enters an earlier checkpoint

## Gate 5

**PASS only when:** raw market history remains intact and temporally auditable.

---

# 6. Build a derived analytical 10-second layer

## Principle

The analytical 10-second layer is **derived**. It is not a replacement for raw observations.

```text
RAW SCORE/STATE
      +
RAW MARKET EVENTS
      |
      v
DERIVED 10-SECOND ANALYTICAL GRID
```

## Required provenance

Each derived row should preserve, where applicable:

- game ID
- analytical timestamp
- source score snapshot timestamp
- source market observation timestamp
- home score
- away score
- actual live total
- period label
- derived quarter
- raw game clock
- derived `clock_minutes`
- market line selected at-or-before analytical timestamp
- prediction/fair value if legitimately available
- source/provenance identifiers

## Market selection rule

For analytical point `T`:

```text
market_timestamp <= T
```

Select the appropriate latest authoritative observation at-or-before `T`.

Never use a future market observation.

## Missing data rule

If no score snapshot exists at exactly `T`, do not invent one. Use an explicitly documented nearest/last-observation rule only if the analytical definition requires it and the provenance is retained.

Do not overwrite raw observations.

## Gate 6

**PASS only when:** analytical rows have clear provenance and no look-ahead or fabricated observations.

---

# 7. Validate timestamp, clock, and score integrity

## Preserve three levels

Always distinguish:

```text
RAW PERIOD LABEL
RAW CLOCK
DERIVED QUARTER / CLOCK MINUTES
```

The historical audit found that the `snapshots` table did not carry a dedicated quarter column; quarter must be derived from `period_label`.

## Check score integrity

For each game:

- score does not decrease
- total score does not decrease
- period transitions are valid
- halftime/end sentinels are handled correctly
- one-off source anomalies are identified rather than silently rewritten

## Check clock integrity

Verify:

- valid clock format
- correct period mapping
- no backward movement within a period
- correct handling of 12:00 period-start sentinel
- correct Half End / Half Time handling
- deterministic conversion to elapsed game minutes

## Current pace

Use game-clock time, not wall-clock time:

```text
CURRENT_PACE = (home_score + away_score) / elapsed_game_minutes
```

## Expected pace

Use the authoritative model expected total:

```text
EXPECTED_PACE = expected_total / 40.0
```

Then:

```text
PACE_DIFF  = CURRENT_PACE - EXPECTED_PACE
PACE_RATIO = CURRENT_PACE / EXPECTED_PACE
PACE_PCT   = (CURRENT_PACE / EXPECTED_PACE - 1) * 100
```

These are descriptive/analytical metrics only. They must not settle bets.

## Keep momentum separate

A recent-window velocity/momentum metric is not the same thing as whole-game `CURRENT_PACE`.

Keep independent fields for:

- velocity / recent scoring rate
- acceleration
- whole-game current pace
- expected pace
- pace differential
- pace ratio
- pace percentage

## Gate 7

**PASS only when:** clock/score reconstruction is deterministic and all material anomalies are documented.

---

# 8. Enforce prediction / market / outcome separation

## BLM prediction

The authoritative BLM prediction is a model value.

Current requirement:

```text
prediction = X.0 or X.5 only
```

The model must not produce arbitrary tenths as its authoritative output.

Historical audit found many existing 1-decimal predictions. Those historical values are not silently rewritten merely to make the database look compliant.

For newly generated outputs, enforce the x.0/x.5 invariant at the authoritative model-output boundary.

## Market line

Market betting lines are x.5.

Keep separate fields for:

- market line at checkpoint/decision time
- OLV
- CLV
- any analytical line summaries

Never use an average as the settlement line.

## Position versus settlement

### BLM position

```text
BLM prediction > checkpoint market -> OVER
BLM prediction < checkpoint market -> UNDER
BLM prediction = checkpoint market -> NO_EDGE / NO_BET
```

### Market outcome

```text
actual final total > checkpoint market -> OVER
actual final total < checkpoint market -> UNDER
actual final total = checkpoint market -> PUSH
```

### Bet result

```text
OVER  + actual > market -> OVER_WIN
OVER  + actual < market -> OVER_LOSS
OVER  + actual = market -> PUSH

UNDER + actual < market -> UNDER_WIN
UNDER + actual > market -> UNDER_LOSS
UNDER + actual = market -> PUSH

NO_EDGE / NO_BET -> NO_BET
```

**Prediction-versus-actual is model accuracy, not betting settlement.**

## Critical PUSH rule

`fair == market` is not a settlement PUSH.

It is `NO_EDGE / NO_BET`.

A genuine settlement PUSH requires `actual == market`.

The current dataset uses x.5 market lines and integer final totals, so true settlement pushes should be mathematically impossible unless the underlying raw data violates those assumptions.

## Gate 8

**PASS only when:** a full-population audit proves that model position and market settlement are independent and temporally correct.

---

# 9. Full dataset cleanliness audit

Run a read-only audit across the complete relevant population.

## Identity

- duplicate game IDs
- duplicate game/checkpoint IDs
- missing identifiers

## Timestamp

- missing timestamps
- duplicate timestamps
- backward timestamps
- future timestamps
- impossible event ordering

## Score

- score decreases
- missing scores
- impossible totals
- inconsistent period transitions

## Clock

- missing clocks
- invalid clocks
- backwards clocks
- impossible elapsed minutes
- period-label inconsistencies

## Market

- non-x.5 lines
- missing market lines
- duplicate market observations
- future observations used for earlier checkpoints
- unexplained market gaps

## BLM model

- missing predictions
- newly generated predictions not x.0/x.5
- prediction generated after prohibited future information
- prediction containing future game result information

## OLV/CLV

- missing OLV
- missing CLV
- checkpoint line incorrectly substituted with OLV
- checkpoint line incorrectly substituted with CLV

## Outcome

- actual totals not integer where integer is expected
- settlement result not equal to actual-vs-market rule
- `PUSH` caused by fair-vs-market equality
- `NO_EDGE` incorrectly counted as win/loss/push

## Pace

- wall-clock elapsed time accidentally used in place of authoritative game clock
- invalid current pace
- invalid expected pace
- divide-by-zero or missing-time cases

For every anomaly, provide:

```text
game_id
checkpoint
relevant timestamps
source table
source columns
raw values
expected invariant
observed violation
```

Do not silently repair anomalies during this audit.

## Gate 9

**PASS only when:** all material anomalies are classified as fixed, accepted, or explicitly quarantined with evidence.

---

# 10. Historical data repair policy

Historical repair is a separate operation from live extraction.

## Allowed only when

- the raw authoritative source exists
- the derived value can be deterministically recomputed
- the target rows are explicitly identified
- the repair is transactionally safe
- a verified backup exists
- before/after counts are recorded

## Never silently repair

Do not rewrite historical predictions merely to enforce the new x.0/x.5 model-output invariant.

Do not rewrite immutable historical checkpoint positions solely because the clock parser was improved.

Do not replace OLV/CLV or historical market observations with reconstructed guesses.

## Gate 10

**PASS only when:** any historical repair is separately documented with exact row counts and source lineage.

---

# 11. Dataset release gate — DO NOT START ML BEFORE THIS PASSES

The clean dataset can be released for ML only if all material gates pass.

## Mandatory PASS conditions

1. Real score/state cadence is established and adequate.
2. Raw WS market observations are preserved.
3. No future market observations enter earlier checkpoints.
4. No future game outcomes leak into predictions.
5. Newly generated BLM predictions are x.0/x.5.
6. Market lines are x.5.
7. Actual final totals are integer where expected.
8. OLV and CLV remain separate from checkpoint market lines.
9. BLM prediction is never substituted for the market line.
10. Settlement is actual-versus-checkpoint-market.
11. NO_EDGE is distinct from PUSH.
12. Clock reconstruction is deterministic.
13. Score progression is valid except explicitly documented source anomalies.
14. Raw observations remain immutable.
15. Derived analytical rows retain provenance.
16. Every critical derived field has documented lineage.
17. The database can be independently audited from raw primitives.

## Release decision

Return exactly one:

```text
DATASET RELEASE — PASS
```

or

```text
DATASET RELEASE — FAIL
```

If FAIL, list the exact blockers. Do not begin ML.

---

# 12. After the clean-data gate: ML phase (NOT PART OF PHASE 1)

Only after `DATASET RELEASE — PASS` should model work begin.

Planned order:

```text
CLEAN DATA
   ↓
FEATURE ENGINEERING
   ↓
TIME-ORDERED TRAIN/TEST SPLIT
   ↓
WALK-FORWARD VALIDATION
   ↓
BASELINE MODEL
   ↓
BLM MODEL
   ↓
BLM VS MARKET ERROR ANALYSIS
   ↓
SAMPLE-SIZE LEARNING ANALYSIS
   ↓
ADAPTIVE WEIGHTING
```

Never allow a game's final result to influence the prediction made for that same game.

Any future weighting system must be justified by out-of-sample evidence, not by sample size alone.

---

# 13. Progress tracking template

Update this table after each gated step. Keep it in the repository so any agent can resume without guessing where the work stopped.

| Step | Status | Evidence / artifact | Date | Agent / commit | Blocker |
|---|---|---|---|---|---|
| 0. Baseline / Freeze | NOT STARTED |  |  |  |  |
| 1. Trace capture loop | NOT STARTED |  |  |  |  |
| 2. Implement 10s capture | NOT STARTED |  |  |  |  |
| 3. Prove live cadence | NOT STARTED |  |  |  |  |
| 4. Protect raw WS feed | NOT STARTED |  |  |  |  |
| 5. Analytical 10s layer | NOT STARTED |  |  |  |  |
| 6. Clock/score integrity | NOT STARTED |  |  |  |  |
| 7. Prediction/market/outcome separation | NOT STARTED |  |  |  |  |
| 8. Full cleanliness audit | NOT STARTED |  |  |  |  |
| 9. Historical repair review | NOT STARTED |  |  |  |  |
| 10. Dataset release gate | NOT STARTED |  |  |  |  |
| ML Phase | BLOCKED UNTIL RELEASE |  |  |  |  |

---

# 14. Agent reporting format

At the end of every step, report:

```text
STEP:
STATUS: PASS / FAIL / BLOCKED

WHAT I CHECKED:
[plain-language explanation]

WHAT I FOUND:
[exact evidence]

WHAT I CHANGED:
[or "NOTHING — READ ONLY"]

FILES CHANGED:
[list]

DATABASE CHANGED:
YES / NO

SERVICE RESTARTED:
YES / NO

RAW DATA MODIFIED:
YES / NO

TESTS:
[exact command + result]

NEXT GATE:
[what must happen next]

BLOCKERS:
[none or exact blocker]
```

A weak agent must not respond with “looks good.” It must provide measurements, row counts, source paths, formulas, and test results.

---

# 15. Current known facts to preserve

The initial clean-data investigation established these facts. Future agents should re-verify them when the relevant step is reached rather than assuming they remain true forever:

- Score/state snapshots historically ran at approximately 90-second intervals despite a nominal 20-second tick.
- Raw market observations arrive via an event-driven WebSocket stream and can appear in bursts with long quiet periods.
- Score/state snapshots and market observations are independent streams.
- Historical snapshot data can reconstruct game time from `period_label + clock`, with quarter derived from `period_label`.
- `CURRENT_PACE` should be based on the authoritative game clock, not wall-clock elapsed time.
- The recommended analytical score/state cadence is approximately 10 seconds.
- Raw WS market observations should remain untouched and authoritative.
- Historical settlement checks established that checkpoint settlement uses the checkpoint's frozen live market line at-or-before that checkpoint, not OLV/CLV substitution.
- A previous settlement audit found `fair == market` position pushes being labelled as `PUSH`; the correct semantic state is `NO_EDGE / NO_BET`.
- The current x.5-market / integer-actual population should have zero genuine settlement pushes unless the source data violates those assumptions.

These facts describe the starting point of this roadmap; they are not permission to skip verification.

---

# FINAL RULE

**CLEAN DATA FIRST. ML SECOND.**

Do not optimize a model using a dataset whose timing, market-line lineage, score/clock integrity, prediction granularity, or settlement semantics have not been independently verified.
