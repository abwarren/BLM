# BLM Phase 1 — Clean Data: Detailed Gated Execution Plan

**Purpose:** Establish a trustworthy, auditable, leakage-free BLM dataset before any ML optimization, adaptive weighting, retraining, or predictive-performance tuning.

**Execution model:** one stage at a time. Audit first. Produce evidence. Review the evidence. Only then authorize the next modification. Never combine a diagnosis and an irreversible repair into an opaque operation.

---

## 0. Operating doctrine

The immediate goal is **data integrity**, not model accuracy.

Do not attempt to improve model accuracy until we can prove that the model is being trained/evaluated against correctly extracted information and that each historical checkpoint represents information that was actually available at that moment.

The system must maintain a strict distinction between:

- raw observations;
- derived analytical values;
- BLM predictions;
- market lines;
- OLV;
- CLV;
- actual final outcomes;
- settlement classifications;
- NO_EDGE classifications.

### Absolute semantic rules

1. **BLM prediction is not the market line.**
2. **BLM prediction must be restricted to increments of 0.5:** `X.0` or `X.5`.
3. **Market O/U lines must be increments of 0.5.**
4. **Actual final totals are integer totals.**
5. BLM's betting position is evaluated from **BLM prediction versus the market line available at the relevant checkpoint/decision time**.
6. Market settlement is evaluated from **actual final total versus that same checkpoint market line**.
7. BLM prediction must never be substituted for the market line when calculating whether a wager would have won or lost.
8. OLV is the opening market reference and CLV is the closing market reference. Neither may silently replace a checkpoint market line.
9. A market observation used for a checkpoint must satisfy `market_timestamp <= checkpoint_timestamp`.
10. A game's final result must never be available to the prediction-generation process for that same game's earlier checkpoint.
11. `BLM prediction == market line` is **NO_EDGE / NO_BET**, not settlement PUSH.
12. `actual final total == market line` is a genuine **PUSH**.
13. Raw observations are authoritative and must not be destroyed by analytical transformations.
14. Derived values must be traceable to their raw inputs.
15. Missing observations must not be silently fabricated by interpolation.
16. A configured sampling interval is not evidence of actual sampling frequency; cadence must be measured from stored timestamps.

---

# Phase 1 execution sequence

```text
STEP 0  Baseline / freeze
   ↓
STEP 1  Trace current score/state collector
   ↓
STEP 2  Implement true 10-second score/state capture
   ↓
STEP 3  Empirically prove cadence
   ↓
STEP 4  Audit and protect raw WS market observations
   ↓
STEP 5  Build derived analytical 10-second layer
   ↓
STEP 6  Validate timestamps / score / clock
   ↓
STEP 7  Audit BLM / market / actual separation
   ↓
STEP 8  Full-population cleanliness audit
   ↓
STEP 9  Dataset release gate
   ↓
ONLY AFTER PASS → ML PHASE
```

Each step has a **STOP condition**. If a material blocker is found, stop and investigate it before moving forward.

---

# STEP 0 — Baseline, freeze, and evidence capture

## Objective

Create a known-good reference point before any changes are made.

## Agent directive

```text
PHASE 1 — STEP 0
CLEAN-DATA BASELINE / FREEZE

We are beginning the BLM clean-data phase.

The objective is DATA INTEGRITY, not ML improvement.

Do NOT:
- train any ML model
- change model formulas
- change model weights
- change the 70/30 methodology
- change prediction logic
- change settlement logic
- change PUSH/NO_EDGE semantics
- change OLV/CLV semantics
- alter historical outcomes
- alter raw market observations
- alter production database records
- modify the live collector

Before making ANY modification:

1. Record current git status.
2. Record current HEAD commit.
3. Record branch and upstream state.
4. Record running BLM service PID and executable/version.
5. Record production database path.
6. Record database size.
7. Record relevant schema.
8. Record row counts for every table involved in:
   - games
   - snapshots
   - event/state snapshots
   - market_observations
   - predictions
   - checkpoint_market
   - settlement/outcome records
9. Record existing backups/checkpoints.
10. Record current test-suite result.
11. Record existing integrity-scan result.
12. Record current snapshot cadence statistics if an existing
    diagnostic exists.

Produce a read-only baseline report containing exact commands,
paths, commits, counts, and test results.

Do NOT change anything.

STOP after the baseline report.
```

## Gate

Proceed only when the baseline is recorded and reproducible.

---

# STEP 1 — Trace the score/state collector

## Objective

Explain why the existing nominal 20-second configuration produced approximately 90-second observed intervals. Do not fix the collector until the timing path is understood.

## Agent directive

```text
PHASE 1 — STEP 1
TRACE THE SCORE/STATE CAPTURE PIPELINE

READ-ONLY INVESTIGATION.

We have evidence that the nominal score/state collection interval
is much shorter than the observed interval. Trace the complete
execution path before changing anything.

Trace:

scheduler/timer
    → tick entry
    → tick_start
    → page/network request
    → response wait
    → parsing
    → event selection
    → state extraction
    → _store_list_snapshot / equivalent persistence
    → _capture_event_state / equivalent persistence
    → database commit
    → sleep calculation
    → next tick

For every stage identify:

- source file
- function
- caller
- callee
- timing behavior
- blocking operations
- retry behavior
- timeout behavior
- database writes
- lock behavior
- sleep behavior

Determine whether the current cadence effectively performs:

    work + full configured sleep

instead of:

    work + remaining time until next target boundary

Measure or otherwise establish:

- network/page latency
- parsing time
- database write time
- processing time
- sleep time
- total cycle time

Determine whether one-event-per-tick round-robin behavior causes
additional sparsity in event-state capture.

Determine whether a single slow event blocks other events.

Do NOT fix anything.

Return:

A. exact collector architecture
B. exact timing architecture
C. measured latency components
D. reason nominal cadence differs from observed cadence
E. minimal safe repair proposal
F. possible concurrency/duplication risks
G. tests required to prove the repair

STOP.
```

## Gate

No implementation until the cause is understood.

---

# STEP 2 — Implement true 10-second score/state capture

## Objective

Move the score/state capture to an empirically achievable approximately 10-second target without modifying market ingestion or betting semantics.

## Design requirement

The collector should schedule from the intended cadence boundary rather than simply sleeping the full interval after completing work.

Conceptually:

```text
next_target = previous_tick_start + 10 seconds
sleep = max(0, next_target - current_time)
```

Do not introduce overlapping captures unless the architecture explicitly supports them safely.

## Agent directive

```text
PHASE 1 — STEP 2
IMPLEMENT TRUE 10-SECOND SCORE/STATE CAPTURE

You may modify ONLY the score/state collection mechanism.

Objective:

Establish a genuine approximately 10-second score/state capture
cadence.

DO NOT modify:

- raw WebSocket market ingestion
- market_observations semantics
- settlement
- checkpoint_market
- BLM prediction mathematics
- BLM weights
- OLV
- CLV
- outcome classification
- historical records

The collector must schedule from a target cadence boundary.
Do not simply perform a 10-second sleep after each completed
capture.

Use the equivalent of:

    target_next_tick = previous_tick_start + 10 seconds
    sleep_duration = max(0, target_next_tick - current_time)

If capture work exceeds 10 seconds, do not overlap another capture
unless the existing architecture explicitly guarantees safe
concurrency and deduplication.

Preserve full timestamp precision.

Do not fabricate observations.
Do not interpolate scores.
Do not backfill synthetic raw snapshots.

Add instrumentation sufficient to measure:

- tick_start
- capture_start
- capture_end
- processing_end
- database-write end
- sleep duration
- total cycle duration
- source timestamp
- persistence timestamp

Run all relevant tests.

Then perform a controlled live capture sufficient to evaluate
actual cadence.

Do not claim success from configuration alone.

STOP after implementation, tests, and initial measured results.
```

## Gate

No analytical layer until actual cadence is demonstrated.

---

# STEP 3 — Empirically prove live cadence

## Objective

Prove that the system is actually capturing at approximately the intended resolution.

## Required metrics

For score/state snapshots calculate:

- count per game
- mean interval
- median interval
- P10
- P25
- P75
- P90
- minimum
- maximum
- gaps >5s
- gaps >10s
- gaps >15s
- gaps >30s
- gaps >60s

Also separately calculate the same style of information for raw market observations and model snapshots.

## Agent directive

```text
PHASE 1 — STEP 3
LIVE CADENCE VERIFICATION

READ/OBSERVE FIRST. DO NOT CHANGE CODE OR DATA.

Verify the newly implemented collector using real captured data.

Separate these streams:

1. SCORE/STATE SNAPSHOTS
2. EVENT-STATE SNAPSHOTS
3. RAW MARKET OBSERVATIONS
4. MODEL/PROJECTION SNAPSHOTS
5. CHECKPOINT SNAPSHOTS

For each stream calculate:

- observations per game
- mean interval
- median interval
- P10
- P25
- P75
- P90
- minimum interval
- maximum interval
- count of gaps >5s
- count of gaps >10s
- count of gaps >15s
- count of gaps >30s
- count of gaps >60s

Also report:

- duplicate timestamps
- timestamp regressions
- missing timestamps
- long stalls
- dropped captures
- network stalls
- parser stalls
- database stalls
- score regressions
- clock regressions

Do not smooth the intervals.
Do not average away bad intervals.

Use actual timestamp differences.

The score/state stream must be empirically shown to be near the
intended 10-second target before proceeding.

If it is not, identify the remaining bottleneck and STOP.

Do not modify anything in this step.
```

## Gate

A nominal `10s` configuration is insufficient. The measured timestamps must support the claim.

---

# STEP 4 — Raw WebSocket market preservation audit

## Objective

Ensure that the high-resolution market feed is preserved exactly as raw event-driven data.

## Principle

**Never replace raw market observations with a resampled series.**

The raw WS stream is the evidence from which later analytical snapshots can be constructed.

## Agent directive

```text
PHASE 1 — STEP 4
RAW WEBSOCKET MARKET DATA IMMUTABILITY AUDIT

READ-ONLY.

Treat market_observations produced by the WebSocket feed as the
authoritative raw market source.

DO NOT:
- change its cadence
- resample it
- average it
- interpolate it
- round it
- collapse bursts
- delete observations merely because they are close together
- overwrite source observations
- replace raw observations with analytical snapshots

Audit the complete WS ingestion path.

Prove:

1. source timestamps are retained
2. observation ordering is retained
3. x.5 market-line invariant is preserved
4. legitimate same-time observations are not silently collapsed
5. burst behavior is retained
6. quiet periods are distinguishable from missing feed data
7. provenance is retained
8. source game identity is retained
9. raw observations are not modified by checkpoint selection
10. no future market observation can enter an earlier checkpoint

For checkpoint selection verify:

    market_timestamp <= checkpoint_timestamp

For representative games report:

- raw observation count
- unique timestamps
- duplicate timestamp count
- interval distribution
- shortest interval
- median interval
- longest interval
- burst count
- quiet-period count
- line-movement count
- invalid-line count
- missing-field count

Do not modify anything.

STOP after the audit.
```

---

# STEP 5 — Build the derived 10-second analytical layer

## Objective

Create a regular analytical grid without destroying or fabricating the raw data.

## Critical distinction

The analytical layer is **derived**. It is not the raw feed.

If there is no real score snapshot at a target timestamp, the system must not invent one and call it observed.

The raw source timestamp must remain visible.

## Agent directive

```text
PHASE 1 — STEP 5
BUILD THE DERIVED 10-SECOND ANALYTICAL LAYER

You may create a derived analytical representation.

Do NOT rewrite raw observations.
Do NOT fabricate historical raw observations.
Do NOT interpolate score values and label them observed.

For every analytical point preserve:

- game_id
- analytical_timestamp
- source_snapshot_timestamp
- home_score
- away_score
- total_score
- raw period_label
- derived quarter
- raw game clock
- derived clock_minutes
- applicable market observation timestamp
- applicable market line
- BLM prediction if genuinely available at that time
- source/provenance identifiers

Market selection rule:

    selected_market_timestamp <= analytical_timestamp

If no valid prior market observation exists, leave the analytical
market value unavailable and record the reason.

Do not use future market observations.

Do not replace the raw market stream with the analytical value.

Do not average market lines unless a separately named analytical
feature explicitly requires an average.

The analytical layer must clearly identify which values are:

    OBSERVED RAW
    SELECTED FROM RAW
    DERIVED

Do not silently convert one category into another.

STOP after implementation and tests.
```

---

# STEP 6 — Validate timestamps, score, and game clock

## Objective

Make sure pace calculations and temporal features are based on trustworthy game-time information.

## Required raw/derived separation

Preserve:

```text
raw timestamp
raw period label
raw clock
raw score

and separately:

derived quarter
derived clock_minutes
current pace
expected pace
pace difference
pace ratio
pace percentage
```

## Pace definitions

For a 40-minute game:

```text
CURRENT_PACE = actual_live_total / elapsed_game_minutes

EXPECTED_PACE = authoritative_expected_total / 40.0

PACE_DIFF = CURRENT_PACE - EXPECTED_PACE

PACE_RATIO = CURRENT_PACE / EXPECTED_PACE

PACE_PCT = (CURRENT_PACE / EXPECTED_PACE - 1) * 100
```

These metrics are descriptive/analytical and must not change settlement.

## Agent directive

```text
PHASE 1 — STEP 6
CLOCK / SCORE / TIMESTAMP INTEGRITY AUDIT

READ-ONLY FIRST.

For every game validate:

1. score progression
2. score non-decrease
3. period progression
4. period labels
5. raw game clock validity
6. quarter derivation
7. clock_minutes derivation
8. halftime transition
9. end-of-period transitions
10. end-of-game sentinel handling
11. timestamp ordering
12. timestamp precision

Preserve raw fields.
Never replace raw clock/period values with derived values.

Verify that clock_minutes is calculated from the authoritative
stored game clock/period representation rather than accidentally
using wall-clock time.

Calculate and audit:

    CURRENT_PACE = actual_live_total / elapsed_game_minutes

    EXPECTED_PACE = authoritative_expected_total / 40.0

    PACE_DIFF = CURRENT_PACE - EXPECTED_PACE

    PACE_RATIO = CURRENT_PACE / EXPECTED_PACE

    PACE_PCT = (CURRENT_PACE / EXPECTED_PACE - 1) * 100

Check divide-by-zero and pre-game handling explicitly.

Determine whether any existing dashboard metric called:

    momentum
    velocity
    extreme
    acceleration
    pace

is actually whole-game current pace or a different recent-window
or derivative metric.

Do not assume semantic equivalence merely because units are similar.

Do not modify settlement or betting statistics.

Report every anomaly with game_id and timestamps.

Do not silently correct anomalies.

STOP after report.
```

---

# STEP 7 — BLM prediction / market / actual separation audit

## Objective

Eliminate the specific failure mode where `OVER_WIN` / `UNDER_WIN` is evaluated against the BLM prediction rather than against the actual market line available at the decision time.

## Required semantic model

```text
BLM PREDICTION
      │
      │ compared with
      ▼
CHECKPOINT MARKET LINE
      │
      └──→ BLM POSITION / EDGE

ACTUAL FINAL TOTAL
      │
      │ compared with
      ▼
SAME CHECKPOINT MARKET LINE
      │
      └──→ MARKET OUTCOME / SETTLEMENT
```

Never:

```text
actual final total vs BLM prediction
```

for the purpose of determining whether the **market wager** won or lost.

## Agent directive

```text
PHASE 1 — STEP 7
PREDICTION / MARKET / ACTUAL SEPARATION AUDIT

Perform a complete lineage audit of every betting-statistic path.

The following must remain separate:

A. BLM prediction
B. checkpoint market line
C. actual final total
D. OLV
E. CLV

Prove exactly where each field originates and where it is consumed.

BLM POSITION must be:

    BLM prediction vs checkpoint market line

MARKET OUTCOME must be:

    actual final total vs SAME checkpoint market line

NEVER use BLM prediction as the market reference for settlement.

NEVER use OLV as the checkpoint market unless it is genuinely the
market observation available at that exact checkpoint.

NEVER use CLV as the checkpoint market unless it was genuinely
available at that exact checkpoint.

Verify:

    market_timestamp <= checkpoint_timestamp

Verify that actual final score/result is unavailable to the
prediction process for earlier checkpoints.

Verify BLM predictions:

    allowed values = X.0 or X.5

Verify market lines:

    allowed values = X.5

Verify actual final totals:

    integer totals

Explicitly distinguish:

    BLM prediction == market line
        → NO_EDGE / NO_BET

from:

    actual final total == market line
        → PUSH

Audit every code path that produces:

    OVER_WIN
    OVER_LOSS
    UNDER_WIN
    UNDER_LOSS
    PUSH
    NO_EDGE

Trace those fields all the way back to their source columns.

Search for any implementation where OVER_WIN/UNDER_WIN or equivalent
is derived by comparing the actual result to the BLM prediction.

Report every occurrence with file, function, expression, and
consumer.

Do NOT fix anything in this step.

STOP after producing the complete lineage report.
```

---

# STEP 8 — Full-population cleanliness audit

## Objective

Audit the entire dataset systematically before any ML work.

## Agent directive

```text
PHASE 1 — STEP 8
FULL DATABASE CLEANLINESS AUDIT

READ-ONLY.

Audit the complete available dataset.

Produce both counts and percentages.

IDENTITY:
- duplicate game IDs
- duplicate game/checkpoint combinations
- missing game IDs
- inconsistent game identity

TIMESTAMP:
- missing timestamps
- duplicate timestamps
- backwards timestamps
- impossible ordering
- future timestamps
- low-precision timestamps where precision is required

SCORE:
- score decreases
- impossible scores
- missing scores
- unexplained score gaps
- score values inconsistent with source state

CLOCK:
- missing clock
- invalid clock
- backwards clock
- impossible period transition
- invalid elapsed minutes
- sentinel errors

MARKET:
- non-x.5 lines
- missing checkpoint market
- duplicate market observations
- future market observations
- look-ahead contamination
- unexplained market gaps
- OLV/CLV substituted for checkpoint market

BLM:
- missing predictions
- non-X.0/X.5 predictions
- prediction generated after result
- prediction using future observations
- prediction timestamp mismatch

OUTCOME:
- non-integer actual totals
- actual-before-checkpoint leakage
- incorrect OVER_WIN
- incorrect OVER_LOSS
- incorrect UNDER_WIN
- incorrect UNDER_LOSS
- incorrect PUSH
- incorrect NO_EDGE
- prediction-vs-result settlement errors

OLV/CLV:
- missing OLV
- missing CLV
- OLV incorrectly used as checkpoint market
- CLV incorrectly used as checkpoint market
- OLV/CLV timestamp errors

PACE:
- invalid current pace
- invalid expected pace
- clock conversion errors
- wall-clock used instead of game clock
- divide-by-zero cases

LINEAGE:
- derived fields without source lineage
- source fields overwritten
- raw observations modified
- analytical values stored as if raw

For every material anomaly provide:

- game_id
- checkpoint
- relevant timestamps
- source table
- source column
- actual value
- expected invariant
- classification

Do not automatically repair anomalies.
Do not silently discard bad records.
Do not hide failures behind aggregate percentages.

STOP after the complete audit.
```

---

# STEP 9 — Dataset release gate

## Objective

Formally decide whether the dataset is clean enough for ML.

## PASS requirements

The dataset must demonstrate:

1. Actual score/state capture cadence is known and acceptable.
2. Raw WS market observations are preserved.
3. Market selection has no look-ahead.
4. Prediction generation has no future-information leakage.
5. BLM outputs obey x.0/x.5.
6. Market lines obey x.5.
7. Actual totals are integers.
8. OLV and CLV remain distinct from checkpoint market.
9. BLM prediction is never substituted for market.
10. Settlement is actual vs checkpoint market.
11. NO_EDGE is distinct from settlement PUSH.
12. Clock reconstruction is deterministic.
13. Score progression is valid or source anomalies are explicitly documented.
14. Raw observations remain immutable.
15. Derived analytical fields are explicitly labelled as derived.
16. Important fields have traceable lineage.
17. Historical data has not been silently fabricated or interpolated.

## Agent directive

```text
PHASE 1 — STEP 9
DATASET RELEASE GATE

DO NOT TRAIN ML.

Review all results from Steps 0–8.

Determine whether the clean-data gate PASSES or FAILS.

PASS requires all material invariants to be satisfied:

1. reliable score/state cadence established
2. raw WS market observations preserved
3. no look-ahead market observations
4. no future information in predictions
5. BLM prediction X.0/X.5 only
6. market line X.5 only
7. actual total integer
8. OLV/CLV separate from checkpoint market
9. BLM prediction never used as market reference
10. settlement uses actual vs checkpoint market
11. NO_EDGE distinct from PUSH
12. deterministic clock reconstruction
13. valid score progression or explicit source exception
14. raw observations immutable
15. derived data clearly identified
16. lineage documented
17. no silent interpolation/fabrication

Return exactly:

    DATASET GATE: PASS

or:

    DATASET GATE: FAIL

For FAIL, list every material blocker, its evidence, affected rows,
and the exact next corrective action required.

Do NOT train ML if the gate fails.
Do NOT modify data to force a PASS.
Do NOT suppress anomalies.

STOP.
```

---

# Repair protocol for any discovered defect

Never combine diagnosis and repair without evidence.

Use this protocol:

```text
AUDIT
  ↓
IDENTIFY DEFECT
  ↓
SHOW EXAMPLES
  ↓
TRACE ROOT CAUSE
  ↓
PROPOSE MINIMAL REPAIR
  ↓
GET APPROVAL
  ↓
BACK UP
  ↓
IMPLEMENT
  ↓
RUN TESTS
  ↓
RERUN ORIGINAL AUDIT
  ↓
COMPARE BEFORE/AFTER
  ↓
ONLY THEN ACCEPT
```

### Mandatory repair constraints

- Never silently mutate historical outcomes.
- Never rewrite raw market observations merely to make statistics look cleaner.
- Never fabricate missing observations.
- Never use a model prediction to repair the market line.
- Never use final results to repair historical prediction inputs.
- Never change settlement semantics merely to make aggregate statistics look plausible.
- Never delete anomalous records without recording why and preserving a recoverable backup.

---

# What the dashboard should eventually distinguish

The dashboard may eventually display, as separate concepts:

### Raw / observed

- live score
- elapsed game clock
- raw market line
- OLV
- CLV
- raw market timestamp
- score snapshot timestamp

### Derived analytical

- current pace
- expected pace
- pace difference
- pace ratio
- pace percentage
- recent scoring rate
- momentum
- velocity
- acceleration

### Model

- BLM prediction
- model confidence
- model error
- model-vs-market edge

### Settlement

- checkpoint market
- actual final total
- market outcome
- BLM position
- BLM result
- NO_EDGE
- PUSH

These categories must not be collapsed into a single generic `line`, `prediction`, `pace`, or `result` field.

---

# ML is explicitly deferred

Do not begin ML optimization until the Phase 1 dataset gate is PASS.

When the gate passes, the next phase should be:

```text
CLEAN DATA
    ↓
FEATURE ENGINEERING
    ↓
STRICT TIME-ORDERED TRAIN/TEST SPLIT
    ↓
WALK-FORWARD VALIDATION
    ↓
BASELINE MODEL
    ↓
BLM MODEL
    ↓
BLM VS MARKET ERROR ANALYSIS
    ↓
SAMPLE-SIZE / LEARNING-CURVE ANALYSIS
    ↓
ONLY THEN → ADAPTIVE WEIGHTING
```

The future ML system must obey the same information-availability rule:

> A prediction at timestamp T may use only information that was genuinely available at or before T.

It may not use final score, future market movement, future snapshots, or CLV information that was unavailable at T.

---

# Final execution rule

**Do not give an agent the entire roadmap as permission to execute every step automatically.**

Run one step, inspect the evidence, and only then issue the next step.

The correct operational rhythm is:

```text
STEP 0 → REPORT → REVIEW
STEP 1 → REPORT → REVIEW
STEP 2 → CHANGE → TEST → REPORT → REVIEW
STEP 3 → MEASURE → REVIEW
STEP 4 → AUDIT → REVIEW
STEP 5 → CHANGE → TEST → REVIEW
STEP 6 → AUDIT → REVIEW
STEP 7 → AUDIT → REVIEW
STEP 8 → AUDIT → REVIEW
STEP 9 → PASS/FAIL
```

**No ML, adaptive weighting, or accuracy optimization before STEP 9 = PASS.**
