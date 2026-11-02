# BLM Deferred TODO

## Priority 1 — Existing-Strategy OVER Cohort Database Analysis

**STATUS: TO DO — READ-ONLY RESEARCH IN TEST.**

Analyze the available historical game database in the isolated TEST environment to determine which combinations of the **existing BLM strategy parameters** identify the strongest, most repeatable OVER cohort.

The objective is to identify a cohort with a genuinely high, statistically defensible OVER win rate. Do not assume that a high historical percentage is a real edge.

**Detailed instructions:** `docs/OVER_COHORT_DATABASE_RESEARCH.md`

### Required work

- [ ] Verify the isolated TEST database, schema, source tables, game counts and settlement coverage.
- [ ] Trace the current strategy's actual parameter definitions and calculations in code and the frozen research specification.
- [ ] Reconstruct eligible historical OVER observations using only information available at each checkpoint.
- [ ] Evaluate the existing nominated OVER residual-magnitude × market-age cohorts.
- [ ] Measure performance by unique game, not just by checkpoint row.
- [ ] Report sample size, wins, losses, pushes, win rate and Wilson 95% confidence interval for every cohort.
- [ ] Compare cohorts chronologically and across available leagues/classifications.
- [ ] Identify which existing strategy cohort, if any, meets the pre-existing evidence gate.
- [ ] Produce a ranked, evidence-backed research report with limitations and a recommendation.
- [ ] Have QA independently reproduce the cohort counts and settlement calculations.

### Success standard

A candidate cohort must meet the existing research gate:

- At least 30 unique settled games.
- OVER win rate at least 60%.
- Wilson 95% lower confidence bound greater than 50%.

These are the existing research criteria, not permission to retune the frozen prospective protocol. If no cohort meets the gate, report that honestly. Do not lower the gate to manufacture a winner.

### Scope boundary

This task is **analysis only**. Read-only database access in isolated TEST; no code changes, model changes, threshold changes, scorecard changes, collector changes, database writes, backfills or production changes. Do not add new indicators or invent a new strategy. Any future implementation requires a separate proposal and approval.

---

## Deferred — After Product Stability

- [ ] Confirm product stability gate is satisfied.
- [ ] Research Cyber 2K26 game supplier/platform.
- [ ] Determine human vs simulation/operator architecture.
- [ ] Determine whether Cyber team identity is persistent.
- [ ] Analyze team-level historical behavior.
- [ ] Analyze matchup-level behavior.
- [ ] Investigate game-ID / fixture structure.
- [ ] Correlate legitimate live observation with BLM game IDs.
- [ ] Test whether structural features have predictive value.
- [ ] Create a separate proposal before adding any Cyber structural feature.

**Gate:** None of the above Cyber research is authorized until the core BLM product is stable and the stability gate in `PLAN.md` has been explicitly satisfied.

**Scope rule:** This TODO is roadmap documentation only. It authorizes no production code, database, collector, scorecard, Market/Fair, projection, or model changes by itself.
