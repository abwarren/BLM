# BLM V4 — STRICT IMPLEMENTATION DIRECTIVE
## Bidirectional Pace-Gap, Pace-Trajectory, and Live-Line Analysis

## AUTHORIZATION

This directive authorizes implementation of the pace-analysis layer identified after the completed game-time/live-line audit.

The objective is to measure **both directions** of apparent live-line pressure without assuming that either direction is a bookmaker trap.

The implementation must answer empirically:

> When the pace required to reach the current live total is materially above the current scoring pace, does scoring subsequently accelerate, remain low, or behave otherwise?
>
> When the pace required to reach the current live total is materially below the current scoring pace, does scoring subsequently decelerate, remain high, or behave otherwise?

Do not encode either outcome as an assumption. Capture the state and subsequent trajectory so the data can determine the relationship.

---

# 1. HARD SCOPE

Implement only the following:

1. Correct/complete B1 projection time fallback identified by the audit, if not already implemented.
2. Correct/complete B2 checkpoint game-state presentation, if not already implemented.
3. Establish a separate clean metrics database for validated post-fix observations.
4. Store bidirectional pace-gap metrics.
5. Store short-window pace trajectory and acceleration/deceleration metrics.
6. Store subsequent outcomes needed to study whether apparent Over/Under-friendly states reverse, persist, or regress.
7. Add deterministic tests and validation.

Do NOT redesign the fair-total formula.

Do NOT introduce bookmaker-trap logic.

Do NOT assume mean reversion.

Do NOT assume momentum.

Do NOT add arbitrary Over/Under probabilities.

Do NOT use the old historical database as a statistical baseline.

Do NOT alter the frozen replay, duration, or market-duplication fixes unless a new regression is specifically proven.

Do NOT create a second independent scraper/collector unless technically unavoidable. Prefer deriving clean metrics from the validated existing collection pipeline.

---

# 2. REPOSITORY-FIRST REQUIREMENT

Before modifying anything:

```bash
cd ~/BLM
git status --short
find blm_v4 -maxdepth 3 -type f | sort
find tests -maxdepth 2 -type f | sort
grep -RniE "pace|required|pts.?/min|progress|elapsed|remaining|total_line|fair|project|checkpoint|market|over|under" blm_v4 tests --exclude-dir=__pycache__
git log --oneline -10
```

Read the existing audit directive and its findings:

`docs/AUDIT_GAME_TIME_PACE_LIVE_LINE.md`

The implementation must respect the existing repository architecture and frozen fixes.

Do not reset, stash, clean, restore, or discard unrelated working-tree changes.

---

# 3. B1 — GAME-TIME FALLBACK

Where `projection.project()` or `pace_from_snapshots()` receives a snapshot with:

```text
quarter = NULL
period_label = valid
clock = valid
```

derive the quarter from `period_label` using the existing repository convention, then perform the normal classification-specific time calculation.

Do not duplicate or invent a competing period parser if an existing helper can be reused safely.

Durations remain:

```text
BETUAL_NBA = 40 minutes
CYBER_2K26 = 48 minutes
```

Do not use a universal 40-minute denominator.

The corrected projection layer must expose valid:

- elapsed minutes;
- progress;
- remaining minutes where applicable;
- game-clock pace fallback where applicable.

This must work for the real production case where approximately 95% of snapshots may have `quarter=NULL` but a valid period label.

---

# 4. B2 — CHECKPOINT DISPLAY

Checkpoint target percentage and actual game progress are different concepts.

Do not remove the checkpoint target.

Do not relabel the checkpoint target as actual elapsed time.

Where checkpoint rows currently show only:

```text
30%
```

make the actual state visible alongside it using the API-provided values where available:

- checkpoint target percentage;
- period;
- countdown clock;
- elapsed minutes;
- actual progress percentage;
- remaining minutes where appropriate.

Example representation:

```text
CP 30% · Q2 07:45 · 12.25/40.00 min · 30.6%
```

The exact UI may be cleaner, but the distinction must be unambiguous.

Do not independently invent frontend formulas when authoritative API fields already exist.

---

# 5. CLEAN DATABASE — SEPARATE DATASET

Create/use a separate database, for example:

```text
blm_metrics_clean.db
```

The existing operational database remains untouched for historical/operational purposes.

Do NOT import old statistical observations into the clean metrics population.

Do NOT calculate z-scores from the old database.

Do NOT backfill old contaminated/incomplete records merely to increase sample size.

The clean statistical population starts at zero and grows only from validated post-fix observations.

---

# 6. CLEAN DATABASE DATA SOURCE

Preferred architecture:

```text
PokerBet source
    ↓
existing validated collector
    ↓
existing replay/staleness/quality gates
    ↓
clean metrics writer
    ↓
blm_metrics_clean.db
```

Do not duplicate the source scraper unless necessary.

The clean writer should consume the same validated state used by the operational pipeline so the two paths cannot silently observe different games/lines.

---

# 7. REQUIRED BASE STATE FIELDS

Every clean observation must retain enough raw state to reproduce the calculations.

At minimum:

```text
game_id
game_identity/classification
source_timestamp
ingestion_timestamp
period_label
quarter
clock
home_score
away_score
current_total_points
total_game_minutes
elapsed_game_minutes
remaining_game_minutes
progress_pct
live_total_line
live_line_timestamp
live_line_age_seconds
over_price
under_price
fair/projected_total
market_fair_difference
quality/status flags
```

Preserve the target identity used for required pace.

For the current implementation the principal target is:

```text
LIVE_TOTAL_LINE
```

Store target type explicitly so future targets cannot be confused with the live line.

---

# 8. ACTUAL PACE

Calculate:

```text
actual_pts_per_min = current_total_points / elapsed_game_minutes
```

Only calculate when elapsed time is greater than zero.

Do not substitute zero for undefined pace.

Retain the underlying score and elapsed-time values so the calculation is reproducible.

---

# 9. REQUIRED PACE

For the live total line target:

```text
required_remaining_points = live_total_line - current_total_points

required_pts_per_min = required_remaining_points / remaining_game_minutes
```

Only calculate when remaining time is greater than zero.

Do NOT clamp negative required pace.

If the score has already exceeded the target line, a negative required pace is mathematically meaningful and must remain observable unless an explicit later model rule says otherwise.

---

# 10. PACE GAP — BOTH DIRECTIONS

Create:

```text
pace_gap = required_pts_per_min - actual_pts_per_min
```

Interpretation must remain neutral:

### Positive pace gap

```text
required pace > actual pace
```

The game currently needs faster scoring than it has produced so far to reach the target.

### Negative pace gap

```text
required pace < actual pace
```

The game currently needs slower scoring than it has produced so far to remain at/reach the target.

Do NOT label positive gap as an Under prediction.

Do NOT label negative gap as an Over prediction.

These are state descriptors only.

---

# 11. PACE RATIO

Where actual pace is non-zero and defined, retain:

```text
required_actual_pace_ratio = required_pts_per_min / actual_pts_per_min
```

This allows states to be compared proportionally rather than only by raw points/minute.

If actual pace is undefined or zero, use NULL rather than fabricating a ratio.

Also retain the raw `pace_gap`; never replace it with only the ratio.

---

# 12. SHORT-WINDOW PACE TRAJECTORY

The cumulative actual pace alone is insufficient.

For validated snapshots where sufficient recent observations exist, calculate short-window scoring pace such as:

```text
pace_last_1m
pace_last_2m
pace_last_3m
pace_last_5m
```

Use actual game time, not merely row count, to define these windows.

Do not assume snapshots arrive at a constant interval.

If insufficient observations/time coverage exist for a window, return NULL rather than fabricate a value.

The implementation must account for classification-specific game time.

---

# 13. PACE CHANGE / ACCELERATION

Where the required windows exist, calculate trajectory metrics such as:

```text
pace_change_1m
pace_change_3m
pace_change_5m
```

and, where mathematically justified:

```text
pace_acceleration
```

The exact implementation may use the most robust time-aware method already compatible with the repository, but it must be documented and tested.

Do not confuse:

- cumulative game pace;
- recent-window pace;
- pace change;
- pace acceleration.

They are distinct fields.

---

# 14. SUBSEQUENT PACE OUTCOMES

This is a critical requirement.

At an observation time, preserve enough information to measure what happened AFTER that state.

For suitable clean observations, capture/derive future outcomes such as:

```text
pace_after_1m
pace_after_2m
pace_after_3m
pace_after_5m
```

and corresponding future pace changes where possible.

These must be based on actual subsequent game observations, not predictions.

Do not populate these fields until the required future time has actually occurred.

This allows analysis of whether a large positive or negative pace gap is followed by:

- acceleration;
- deceleration;
- persistence;
- reversal.

---

# 15. LINE TRAJECTORY

Because the research question concerns the live market as well as scoring pace, retain subsequent market information where available:

```text
line_after_1m
line_after_2m
line_after_3m
line_after_5m
line_change_after_1m
line_change_after_3m
line_change_after_5m
```

Use actual observed market lines and timestamps.

Do not invent a line movement where no valid subsequent line exists.

Maintain distinct market-line identity.

Do not treat simultaneous distinct MatchTotal lines as duplicate observations merely because timestamps match.

---

# 16. FINAL OUTCOME

For completed games, retain:

```text
final_total
final_over_under_result
```

relative to the specific line associated with the observation.

Where multiple lines existed, preserve the line identity/timestamp necessary to know which line the observation refers to.

Do not compare pooled Over and Under percentages as competing predictions.

Settlement remains independent.

---

# 17. FAIR TOTAL RELATIONSHIP

Continue storing:

```text
fair/projected_total
market_fair_difference
```

at the observation point.

Do not replace fair total with required pace.

Do not replace required pace with fair total.

The clean dataset must permit this relationship to be studied:

```text
CURRENT STATE
score + time + actual pace

MARKET
live line

REQUIRED STATE
required pace to reach line

MODEL
fair/projected total

TRAJECTORY
subsequent pace and line movement

OUTCOME
final settlement
```

---

# 18. NO “TRAP” FEATURE

Do NOT create fields named or interpreted as:

```text
book_trap
under_trap
over_trap
trap_score
trap_probability
```

The implementation must remain hypothesis-neutral.

The research question is whether certain state combinations predict subsequent scoring acceleration/deceleration or settlement outcomes.

Only a later statistical analysis may determine whether a repeatable effect exists.

---

# 19. NO HISTORICAL Z-SCORES YET

Do not create historical z-score baselines from the old database.

If z-score/range fields exist in the architecture, they remain NULL until enough clean data exists and a separately validated baseline methodology is authorized.

Do not mix:

- old contaminated observations;
- new clean observations.

Any future distribution must be classification-aware and preferably time/progress-aware.

---

# 20. CLASSIFICATION ISOLATION

All duration and trajectory calculations must respect:

```text
BETUAL_NBA = 40 minutes
CYBER_2K26 = 48 minutes
```

Do not compare raw game-clock windows incorrectly across classifications.

For example, “5 minutes remaining” means five actual game minutes, regardless of snapshot frequency.

Future statistical baselines must retain classification and must not blindly combine classifications with different game structures.

---

# 21. QUALITY GATES

Only validated observations enter the clean statistical population.

Reject/exclude from statistical analysis observations that are:

- post-final replay;
- stale/replayed invalid state;
- missing required game-time information;
- impossible clock state;
- invalid classification;
- invalid market state;
- otherwise rejected by existing quality gates.

Do not silently discard evidence. Retain diagnostic reason/status where useful, but keep invalid observations out of the clean statistical population.

---

# 22. TESTS — MANDATORY

Add tests covering:

### Time

1. label-only snapshot derives quarter correctly;
2. BETUAL uses 40 minutes;
3. CYBER uses 48 minutes;
4. elapsed/progress are populated;
5. remaining time is correct.

### Actual pace

6. actual Pts/Min formula is correct;
7. undefined pace at zero elapsed time remains NULL.

### Required pace

8. required pace changes correctly when time remaining changes;
9. required pace changes correctly when live line changes;
10. negative required pace is preserved.

### Bidirectional gap

11. positive pace gap is represented correctly;
12. negative pace gap is represented correctly;
13. neither direction is automatically classified as Over or Under.

### Trajectory

14. recent 1/2/3/5-minute pace uses actual game time;
15. insufficient window data returns NULL;
16. pace-change calculations are reproducible;
17. subsequent pace fields remain NULL until future observations exist;
18. subsequent observations populate the correct future window.

### Market trajectory

19. subsequent live line fields use valid observed market data;
20. distinct simultaneous market lines are not incorrectly deduplicated.

### Clean DB

21. clean DB is separate from operational DB;
22. no old statistical data is imported;
23. validated observations are written;
24. invalid/replay observations do not enter the clean statistical population;
25. classification is retained;
26. raw state is sufficient to reproduce derived metrics;
27. z-score baseline remains NULL/unpopulated until separately authorized.

### API/UI

28. checkpoint target percentage remains distinct from actual progress;
29. actual period/clock/elapsed/progress are exposed/displayed;
30. CYBER checkpoint timing uses 48-minute duration.

---

# 23. REAL-DATA VALIDATION

After implementation, inspect real post-fix observations from both classifications if available.

Produce examples covering BOTH directions:

### Positive gap example

```text
required pace > actual pace
```

Show subsequent pace and eventual result.

### Negative gap example

```text
required pace < actual pace
```

Show subsequent pace and eventual result.

Do not select examples merely because they support a theory.

If one direction has no sufficient clean sample yet, report that honestly.

The objective is to collect evidence, not prove a predetermined conclusion.

---

# 24. REQUIRED VALIDATION TABLE

The final report must contain a table similar to:

| Metric | Current State | Subsequent State | Final Outcome |
|---|---:|---:|---|
| Score | ... | ... | ... |
| Elapsed min | ... | ... | ... |
| Remaining min | ... | ... | ... |
| Actual Pts/Min | ... | ... | ... |
| Required Pts/Min | ... | ... | ... |
| Pace Gap | ... | ... | ... |
| Required/Actual Ratio | ... | ... | ... |
| Recent Pace | ... | ... | ... |
| Pace Change | ... | ... | ... |
| Live Line | ... | ... | ... |
| Fair Total | ... | ... | ... |
| Market-Fair Difference | ... | ... | ... |
| Final Total | ... | ... | ... |
| Settlement | ... | ... | ... |

The report must include both positive-gap and negative-gap examples where the clean sample permits.

---

# 25. FULL TEST / SERVICE VALIDATION

Run targeted tests first.

Then run the full suite.

Report exact:

- passed;
- failed;
- skipped;
- warnings.

If services require restart because changed backend code is imported by the live process, restart only the necessary service(s).

Then verify:

```text
blm-server.service = active/running
blm-collector.service = active/running
/api/v4/status = 200
/api/v2/live = 200
/api/v4/live = 200
```

Check collector logs for new errors/tracebacks.

Verify that the frozen replay protection remains intact.

Verify no new exact duplicate market rows were introduced by the implementation.

---

# 26. DATABASE SAFETY

Before and after implementation record:

- operational DB size/row counts where relevant;
- clean DB size/row counts;
- clean DB schema;
- clean statistical population count.

The implementation must not erase operational historical data.

Do not rewrite old predictions merely to populate new metrics.

Do not contaminate the clean database with historical records.

---

# 27. PROHIBITED MODEL INTERPRETATIONS

The following are NOT permitted without separate authorization:

- “positive gap means Under”;
- “negative gap means Over”;
- “large gap means bookmaker trap”;
- “pace always mean-reverts”;
- “pace always continues”;
- “the book knows the next scoring burst”;
- “required pace proves value”;
- “actual pace proves value”;
- “fair total proves value”;
- “market line proves value”.

These are hypotheses to be tested, not implementation rules.

---

# 28. STATISTICAL ANALYSIS — NOT YET

This implementation is a **data-capture and measurement foundation**.

Do not use the first observations to establish statistical conclusions.

Do not generate z-scores from insufficient samples.

Do not generate confidence intervals without an authorized methodology.

Do not tune thresholds based on a handful of games.

Do not optimize retrospectively against observed results.

First collect clean observations.

Then perform a separate statistical-model review.

---

# 29. FINAL REPORT FORMAT

Return:

## IMPLEMENTATION SUMMARY
- exact files changed;
- exact database created;
- data source;
- architecture/data flow.

## B1
- fallback implementation;
- tests;
- real-data verification.

## B2
- dashboard changes;
- distinction between checkpoint target and actual game state.

## CLEAN DATABASE
- schema;
- clean observation count;
- rejected count/reasons;
- confirmation no historical statistics were imported.

## PACE METRICS
- actual pace;
- required pace;
- pace gap;
- ratio;
- recent-window pace;
- acceleration/deceleration;
- subsequent pace.

## MARKET TRAJECTORY
- live line;
- subsequent line;
- line movement.

## OUTCOME
- final total;
- settlement;
- representative positive-gap case;
- representative negative-gap case.

## TESTS
- targeted result;
- full-suite result.

## LIVE VERIFICATION
- services;
- endpoints;
- collector errors;
- replay/duplicate checks.

## MODEL LIMITATIONS
Explicitly state what has NOT yet been proven.

Do not claim that the data demonstrates a bookmaker trap unless a later statistical analysis establishes it.

---

# 30. STOP CONDITION

After implementing and validating this data-capture layer:

**STOP.**

Do not proceed to:

- z-score modelling;
- range modelling;
- probability calibration;
- trap detection;
- automated Over/Under strategy changes;
- threshold optimization;
- fair-total redesign.

Those require a separate authorization after a sufficient clean dataset has accumulated.

## CORE OBJECTIVE

Build the dataset required to determine whether the following states are predictive in either direction:

```text
REQUIRED PACE >> ACTUAL PACE
```

followed by:

```text
ACCELERATION / PERSISTENCE / DECELERATION
```

and:

```text
REQUIRED PACE << ACTUAL PACE
```

followed by:

```text
DECELERATION / PERSISTENCE / ACCELERATION
```

Do not decide the answer in advance.

Measure it.

Store it cleanly.

Preserve the live line, fair total, current pace, required pace, trajectory, and final outcome together so the eventual statistical model can determine what is actually predictive.
