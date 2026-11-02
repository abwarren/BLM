# Existing-Strategy OVER Cohort — Database Research Directive

**Status:** TO DO  
**Type:** Read-only historical research  
**Environment:** Isolated TEST only  
**Owner:** BLM Agent  
**Independent verification:** QA Agent  
**Primary objective:** Determine whether an existing BLM strategy cohort can identify OVER outcomes with a high, repeatable and statistically defensible win rate.

---

## 1. Objective

Analyze the available historical game database and establish which combinations of the **current strategy's existing parameters** produce the strongest OVER cohort.

This is not a request to invent a new OVER model.

The agent must:
- Understand the current strategy as implemented and documented.
- Use its existing variables, definitions, bands and time rules.
- Reconstruct the information available at each historical checkpoint.
- Compare the nominated OVER cohorts against actual settled outcomes.
- Identify whether any cohort meets the existing evidence standard.
- Explain which existing parameters distinguish the strongest cohort and whether the result is robust.

A high historical win rate alone is not sufficient. The cohort must have enough unique games, confidence bounds above chance, and performance that is not concentrated in one short period or one small subgroup.

## 2. Absolute environment and scope rules

**ALL analysis and any resulting artifacts must be produced in isolated TEST. LOCAL PRODUCTION MUST NOT BE MODIFIED.**

- Use an isolated TEST database or an approved TEST snapshot.
- Open the database read-only.
- Do not write to, migrate, backfill, repair, vacuum, delete from or otherwise alter any database.
- Do not change application code, strategy logic, thresholds, collector behaviour, scorecards or model outputs.
- Do not restart services or collectors.
- Do not run experiments against production.
- Do not point TEST tools at production databases.
- If a suitable isolated TEST dataset is unavailable, stop and report BLOCKED. Do not copy or export production data without explicit authorization.
- Keep all reports, temporary extracts and analysis outputs in TEST/research locations. Do not commit private game datasets or database files.

This directive authorizes research only. It does not authorize implementation or promotion of any cohort.

## 3. Current strategy — source of truth

Read and trace these sources before analyzing data:

1. `BLM_CONSTITUTION.md`
2. `docs/PLAN-005-RESEARCH-GATES-AND-PROSPECTIVE-CONFIRMATION.md`
3. `docs/IMPLEMENT_PACE_TRAJECTORY_AND_LIVE_LINE_ANALYSIS.md`
4. The actual current implementation of the relevant calculations and data fields.
5. The database schema and source-row provenance.

The existing nominated OVER research cells in PLAN-005 are:

| Side | Residual magnitude | Market age |
|---|---:|---:|
| OVER | 5–10 | >300 seconds |
| OVER | 10–20 | >300 seconds |
| OVER | >20 | >300 seconds |

The existing residual magnitude bands are:
- 0–2.5
- 2.5–5
- 5–10
- 10–20
- >20

The existing market-age bands are:
- 0–5 seconds
- 5–10 seconds
- 10–20 seconds
- 20–30 seconds
- 30–60 seconds
- 60–120 seconds
- 120–300 seconds
- >300 seconds

**Do not change these frozen bands or nominated cells.** Do not retune them using the same historical outcomes that are being evaluated.

The actual sign, magnitude, units and calculation of `market_trajectory_residual` must be verified from the current implementation and research specification. Do not assume that a positive/negative pace gap means OVER or UNDER. The pace/trajectory directive explicitly treats pace gaps as descriptive state, not automatic direction signals.

Do not add arbitrary predictors, price filters, team filters, quarter filters, thresholds or combinations that are not part of the current strategy. League/classification and checkpoint position may be used to stratify and diagnose results; do not silently turn them into new selection rules.

## 4. Database discovery and data audit

First establish what data actually exists in TEST.

Document:
- Database path and engine.
- Schema and relevant tables/views.
- Total games.
- Settled games.
- Unsettled games.
- Available classifications/leagues.
- Snapshot and market-observation counts.
- Games with usable live-line observations.
- Games with usable residual and market-age fields.
- Games with valid settlement and line linkage.
- Date/time coverage.
- Missingness and duplicate rates.

Trace every field back to its source row and calculation.

Do not treat missing values as zero. Do not silently drop incomplete rows. Report exclusion counts and reasons.

Before analysis, verify that the live line, its observation timestamp, the residual and the eventual settlement all refer to the same game and correct market.

## 5. Define the outcome correctly

For each eligible historical OVER observation, compare the final settled game total with the **specific live total line available at that observation's checkpoint**.

- OVER WIN: final settled total is greater than that checkpoint's line.
- LOSS: final settled total is lower than that checkpoint's line.
- PUSH: final settled total equals that checkpoint's line.
- PENDING/VOID: not a settled outcome; report separately and exclude from settled win-rate denominator.

Use the correct market line and preserve its identity/timestamp. Never substitute the opening line, closing line, a later line, or a model-generated fair total for the checkpoint line.

For settled win rate, report:
`wins / (wins + losses)`

Report pushes separately. Also report the push-inclusive outcome counts so the denominator is transparent.

## 6. Point-in-time and anti-leakage controls

At checkpoint T, cohort membership may use only information genuinely available at or before T.

- No final score, settlement, future snapshot, future line or future price may enter cohort selection.
- Settlement is used only after cohort membership is fixed, to score the outcome.
- Exclude 100% terminal checkpoints from confirmation evidence.
- Use eligible checkpoints from 10% through 90% as specified by the frozen research protocol.
- Preserve checkpoint timestamps and market observation timestamps.
- Verify market age is calculated from the correct source timestamps.
- Check duplicate observations and ordering.
- Do not allow a later observation from the same game to change an earlier cohort assignment.

If a game qualifies more than once for the same cell, count it once for game-weighted performance. Use a deterministic rule: the earliest eligible qualifying checkpoint in that cell. Preserve all qualifying checkpoint rows for a separate checkpoint-weighted diagnostic.

When combining cells into a candidate cohort, deduplicate games using the earliest eligible qualifying OVER observation across the nominated cells. Explicitly report cell overlap and unique-game totals.

## 7. Required cohort analysis

Evaluate each of the three nominated OVER cells independently.

For every cell, report:
- Unique settled games.
- Wins.
- Losses.
- Pushes.
- Pending/void games.
- OVER win rate.
- Wilson 95% confidence interval and lower bound.
- Checkpoint-weighted result as a secondary diagnostic.
- Date range.
- League/classification breakdown.
- Chronological performance blocks.
- Missing/excluded records and reasons.

Then report the combined nominated OVER population using unique games, not a sum of overlapping cell counts.

Also show the existing residual and market-age bands as a descriptive matrix so the reader can see the complete current-strategy landscape. Clearly distinguish nominated confirmation cells from exploratory descriptive cells. Do not use exploratory cells to rewrite the frozen prospective protocol.

## 8. High-win-rate evidence gate

Use the existing PLAN-005 criterion without alteration.

A nominated cohort passes only when all conditions are met:

- `N >= 30` unique settled games.
- OVER win rate `>= 0.60`.
- Wilson 95% lower confidence bound `> 0.50`.

Do not lower these thresholds, change the bands, or redefine the denominator to make a cohort pass.

If a cohort has fewer than 30 unique settled games, label it `INSUFFICIENT_N`.

If the observed win rate or confidence bound fails, label it `DOES_NOT_PASS`.

If data is incomplete or not point-in-time safe, label it `NOT_EVALUABLE`.

A cell-level pass is not by itself proof of a durable strategy. Show the chronological and league/classification checks before recommending it.

## 9. Robustness and overfitting checks

The analysis must explicitly investigate:

- Whether performance is driven by a small number of games.
- Whether one league/classification dominates the result.
- Whether one date block dominates the result.
- Whether multiple checkpoints from the same game inflate the apparent sample size.
- Whether the same game appears in several cells.
- Whether market-age or residual values are missing non-randomly.
- Whether results change materially under game-weighted versus checkpoint-weighted reporting.
- Whether the result survives a chronological holdout that was not used to select or tune the cohort.

Do not repeatedly search historical combinations until one produces an attractive win rate. The nominated cells are frozen. Any additional exploratory analysis must be labelled exploratory and cannot be represented as confirmation.

## 10. Relationship to frozen prospective confirmation

PLAN-005 is a frozen prospective protocol.

This task must not:
- Change its freeze timestamp.
- Change its nominated cells.
- Change residual or market-age bands.
- Change its N, win-rate or confidence-bound gates.
- Reclassify historical games as prospective.
- Use historical results to retune the prospective test.

Historical analysis is diagnostic and may identify whether the already nominated OVER cohorts have adequate historical support. The frozen prospective population remains separate and can only be evaluated under its existing temporal firewall and acceptance rules.

## 11. Required final deliverable

Create a research report in TEST with:

### A. Executive result
State whether any existing nominated OVER cohort passes the evidence gate.

### B. Data inventory
Database/schema, game counts, coverage, date range and exclusions.

### C. Strategy trace
Exact code locations and formulas for residual magnitude and market age. Explain the actual units and timestamp basis.

### D. Cohort results
A table for each nominated cell with N, wins, losses, pushes, win rate, Wilson interval and pass/fail status.

### E. Combined unique-game result
Deduplicated combined cohort with overlap accounting.

### F. Robustness
Chronological blocks, league/classification splits, game-weighted versus checkpoint-weighted results and holdout results.

### G. Limitations
Missing data, small samples, coverage bias, unsettled games, market-line linkage issues and any remaining leakage risk.

### H. Recommendation
Identify the strongest existing cohort only if it passes the evidence gate and robustness checks. Otherwise state clearly that no defensible high-win-rate cohort has been demonstrated.

### I. Change audit
Explicitly state:
- Code changes: NONE.
- Strategy/threshold changes: NONE.
- Database writes: NONE.
- Collector/service changes: NONE.
- Production changes: NONE.

## 12. QA acceptance

QA must independently:
- Reproduce the database counts and inclusion/exclusion logic.
- Verify the checkpoint line-to-settlement linkage.
- Recalculate win/loss/push counts.
- Recalculate win rates and Wilson intervals.
- Verify unique-game deduplication and cell overlap.
- Check point-in-time leakage and 100% terminal exclusion.
- Confirm that the frozen prospective protocol was not changed.

QA result must be PASS, FAIL or BLOCKED. A report without reproducible evidence is not a PASS.

## 13. Completion rule

This task is complete only when the report answers:

1. Which of the existing nominated OVER cohorts performs best?
2. How many unique games support it?
3. What is its win rate and Wilson 95% interval?
4. Does it pass the existing evidence gate?
5. Does it remain credible across chronological blocks and available classifications?
6. Is the result free of point-in-time leakage and duplicate-game inflation?

**Do not implement the cohort. Do not alter the strategy. Analyze the existing database in TEST, prove the result, and report it.**
