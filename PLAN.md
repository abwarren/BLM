# BLM Project Plan

## Project Status

The core product is being stabilized before deferred research work is started.

## Stability Gate

The following must be stable and reviewed before deferred Cyber 2K26 research begins:

- Core data integrity closed.
- Point-in-time checkpoint methodology stable.
- Market/Fair calculations stable.
- Classification-specific timing stable.
- Momentum integrity stable.
- 90-minute live measurement reviewed.
- Live product verified.
- Current scorecard stable.
- No unresolved critical production defects.

## Completed / Stabilized Work

### Checkpoint and Anti-Leakage Integrity

- Point-in-time checkpoint selection.
- Prefix-only Market.
- Prefix-only Fair.
- Prefix-only Momentum.
- No terminal-score leakage into earlier checkpoint calculations.
- Historical checkpoint rows remain immutable.
- Checkpoint timestamp identifies the source snapshot.

### Classification-Specific Game Time

- `BETUAL_NBA` = 4 × 10 minutes = 40 regulation minutes.
- `CYBER_2K26` = 4 × 12 minutes = 48 regulation minutes.
- Progress uses classification-specific regulation duration.
- Elapsed and remaining time use classification-specific duration.
- Pace calculations use classification-specific duration.
- 50% BETUAL checkpoint = 20 elapsed minutes.
- 50% CYBER checkpoint = 24 elapsed minutes.
- 75% BETUAL checkpoint = 30 elapsed minutes.
- 75% CYBER checkpoint = 36 elapsed minutes.

### API Checkpoint State

Checkpoint API data exposes the state required for time-projection analysis, including:

- `elapsed_minutes`
- `progress`
- `remaining_minutes`
- `home_score_at_checkpoint`
- `away_score_at_checkpoint`
- `clock_at_checkpoint`
- `current_pace`
- `required_pace`
- existing Market/Fair/result fields

### Momentum Replay Integrity

- Consecutive identical source states are collapsed at momentum calculation time.
- Raw snapshot rows are preserved.
- Historical frozen momentum values are not backfilled.
- Pre-fix checkpoint momentum is treated as legacy/unrecalculated data.
- New analytical momentum calculations use the corrected basis.

## 90-Minute Live Measurement

Status: completed; results must be reviewed before additional production changes.

Rules:

- Measurement is isolated.
- No code or DB changes during the measurement window.
- Collector and measurement data are not altered by subsequent audits.

## Open Integrity Review

Raw collector replay still exists at the source layer: repeated observations of the same source state can be written at new capture timestamps. Momentum is analytically protected by `_velocity()`, but the raw replay mechanism remains an architectural characteristic of the collector.

No collector change is authorized solely by this document.

## Time-Projection Scorecard

Current goal: progressive, time-based Market vs Fair analysis per clean completed game.

At each checkpoint, preserve the point-in-time state and calculate/display as applicable:

- checkpoint percentage
- checkpoint total
- elapsed minutes
- remaining minutes
- current pace
- required pace to reach the market line
- projected final / existing Fair representation as defined by the current methodology
- Market
- Fair
- Market − Fair
- classification
- result

Quarter/half-specific presentation is deferred. The current scorecard focus is time-based projections.

## Deferred — Cyber 2K26 Game-Structure Research

**STATUS: DEFERRED — DO NOT START UNTIL THE CORE PRODUCT IS STABLE.**

### Purpose

Understand how `CYBER_2K26` games are actually generated and whether persistent underlying identities, operators, simulation configurations, or other structural characteristics can be measured from the data.

### Research Questions

#### 1. Game Production

- Who supplies the Cyber game product?
- What platform or engine produces the games?
- Are games human-controlled, AI/simulated, operator-controlled, or another architecture?
- Is there authoritative documentation describing the production process?

#### 2. Team / Operator Identity

Determine whether a displayed Cyber team corresponds consistently to:

- a particular operator
- a particular development/production team
- a persistent simulation configuration
- a particular AI profile
- a particular station/system
- or simply a label with no persistent underlying identity

Do not assume any of these relationships.

#### 3. Persistence

Test whether the same displayed Cyber team has persistent statistical characteristics across games:

- scoring pace
- total points
- first-half pace
- second-half pace
- scoring distribution
- volatility
- late-game behavior
- matchup effects

#### 4. Matchup Structure

Determine:

- whether teams repeatedly play one another
- whether particular pairings have persistent characteristics
- whether game IDs encode useful information
- whether fixture ordering contains structure
- whether games appear independently generated or part of a repeatable schedule

#### 5. Live Observation

When the stability gate permits:

- observe a Cyber game live through a legitimate public source, where available
- record the corresponding game ID
- correlate visible game state with BLM's captured source state
- determine whether the public presentation reveals information not currently represented in the BLM feed

#### 6. Statistical Investigation

Using existing historical data only:

- identify recurring Cyber teams
- calculate per-team and per-matchup distributions
- compare pace and totals
- test persistence versus random variation
- determine whether any discovered structure has statistically meaningful predictive value

#### 7. Supplier / Source Research

Where public evidence permits, map:

`bookmaker → data/odds supplier → Cyber production platform → game engine / simulation`

Do not claim a supplier, operator, developer, or player identity without evidence.

### Research Sequence

Once the stability gate is satisfied:

1. Identify authoritative Cyber source/supplier information.
2. Establish whether games are human/operator/simulation generated.
3. Determine whether displayed team identity is persistent.
4. Build read-only historical team/matchup statistics.
5. Observe/correlate selected live games where legitimately available.
6. Test whether persistent structure exists.
7. Quantify predictive value out-of-sample.
8. Create a separate proposal before adding any Cyber structural feature to production.

### Data and Modeling Rules

- Never modify historical data for this research.
- No backfill, rewriting, or deletion of raw snapshots.
- No Cyber structural feature enters the production model merely because it appears interesting.
- Every proposed predictive feature requires a separate controlled research/validation slice.

### Explicit Prohibition Before Stability

Until the stability gate is satisfied:

- Do not perform Cyber-structure research.
- Do not modify production code for Cyber structure.
- Do not alter the collector.
- Do not alter the database.
- Do not alter the scorecard.
- Do not alter Market/Fair.
- Do not alter projection methodology.
- Do not add team/operator features.
- Do not infer developer/operator identity.
- Do not change the current Cyber 48-minute model.

This section is a roadmap only.
