# Betual NBA Quarterly Line + Score Collection

## Status

**SPECIFICATION ADDED — IMPLEMENTATION REQUIRED**

This document defines the required Betual NBA quarterly collection layer. It is intentionally separate from BLM prediction generation, settlement, and alert gating.

## Objective

For every clean Betual NBA game, retain an auditable quarter-by-quarter history of:

- Q1, Q2, Q3, Q4
- score progression
- live total line progression
- exact timestamps
- elapsed game time measured by BLM's own timer
- relationship between scoring and line movement

The raw source observations remain authoritative. Quarterly records are derived from those observations and must never replace or rewrite the raw data.

## Scope

**Classification:** `BETUAL_NBA`

Do not change CYBER_2K26 behaviour as part of this feature.

Do not change:

- BLM prediction generation/freeze
- BLM model weights
- alert gates
- settlement semantics
- OLV/CLV semantics
- raw WebSocket market ingestion
- existing snapshot timestamps

## Existing source data

The current V4 model already contains:

- `home_score`
- `away_score`
- `period_label`
- `quarter`
- `clock`
- `captured_at`
- `total_line`

The current raw market-observation layer also contains timestamped total-line observations.

The implementation must use these existing raw primitives rather than creating a second competing source.

## 1. Game timer

Betual has no timeouts, so BLM must maintain its own elapsed-game timer.

### Start

When the game first becomes a valid live/stat-tracked game, persist:

```
game_started_at
```

The source/book timestamp is used only to establish the starting point.

### Runtime timer

Use a monotonic local timer for elapsed duration:

```
elapsed_seconds = monotonic_now - monotonic_game_start
```

Persist the wall-clock equivalent alongside it for auditability.

Do **not** copy the sportsbook's displayed countdown clock as the authoritative elapsed timer.

The displayed bookmaker clock may be retained as raw source metadata.

### Restart/recovery

The timer must survive process restarts.

Persist enough information to reconstruct elapsed game time without resetting the timer to zero.

Never reset the game timer merely because:

- the browser was restarted
- the collector restarted
- the event-view page changed
- a market disappeared temporarily
- a new snapshot was captured

## 2. Quarterly classification

Use the authoritative source `period_label` to determine quarter.

Required mapping:

```
1st Quarter -> Q1
2nd Quarter -> Q2
3rd Quarter -> Q3
4th Quarter -> Q4
```

Do not infer quarter solely from wall-clock elapsed time when the source period label is available.

The quarter field must be an integer 1-4.

Halftime/end labels must not create a fake fifth quarter.

## 3. Raw observation requirements

Every qualifying Betual observation must retain:

```
source_game_id
classification
captured_at
game_started_at
elapsed_seconds
home_score
away_score
total_score
period_label
quarter
book_clock
total_line
source/provenance
```

where `total_score = home_score + away_score`.

If a value is unavailable in the source, store NULL rather than inventing it.

## 4. Quarterly derived table

Add a dedicated table, preferably named:

```
quarterly_line_score
```

Minimum fields:

```
id
source_game_id
classification
quarter

quarter_started_at
quarter_ended_at

quarter_start_elapsed_seconds
quarter_end_elapsed_seconds

start_home_score
start_away_score
start_total_score

end_home_score
end_away_score
end_total_score

points_scored_in_quarter

line_at_quarter_start
line_at_quarter_end

line_min
line_max
line_first_seen_at
line_last_seen_at

line_move_count
line_up_moves
line_down_moves
line_net_move

score_line_delta
line_move_per_point

game_started_at
created_at
updated_at
```

All timestamps must retain full precision.

## 5. Line selection

For every quarter, retain the actual market observations that occurred during that quarter.

The derived summary should identify:

### Opening line for quarter

The first authoritative total-line observation at or after the quarter begins.

### Closing line for quarter

The final authoritative total-line observation at or before the quarter ends.

### Range

```
line_min
line_max
```

### Movement

```
line_net_move = line_at_quarter_end - line_at_quarter_start
```

Count each genuine line change:

```
line_move_count
line_up_moves
line_down_moves
```

Do not interpolate a line where no observation exists.

## 6. Score progression

For each quarter calculate:

```
points_scored_in_quarter =
    end_total_score - start_total_score
```

Also retain the home and away score components.

Score must never be reconstructed from the bookmaker clock.

## 7. Line movement versus scoring

Calculate descriptive relationships only.

Required fields:

```
score_line_delta
line_move_per_point
```

where:

```
score_line_delta = points_scored_in_quarter - line_net_move
```

and, where points_scored_in_quarter > 0:

```
line_move_per_point =
    line_net_move / points_scored_in_quarter
```

If the denominator is zero, store NULL.

Also retain the full timestamped observation history so more detailed analysis can later calculate:

- line movement before scoring
- line movement after scoring
- time between scoring events and line changes
- line movement per elapsed minute
- score acceleration versus market movement

These are analytical fields only. They must not automatically become betting signals.

## 8. Quarter boundary rules

Q1:

- first valid Q1 observation establishes the Q1 opening state
- last Q1 observation establishes the Q1 closing state

Q2:

- first valid Q2 observation establishes the Q2 opening state
- halftime/end markers are metadata, not a separate quarter

Q3:

- first valid Q3 observation establishes the Q3 opening state
- last Q3 observation establishes the Q3 closing state

Q4:

- first valid Q4 observation establishes the Q4 opening state
- the final game result must come from the existing reconciliation/result pipeline

Do not manufacture a quarter record when no source observations exist.

## 9. Missing observations

Never fabricate:

- score
- line
- quarter
- timestamp
- elapsed time

If a quarter has score observations but no market observation:

```
line_at_quarter_start = NULL
line_at_quarter_end = NULL
```

and the quarter remains present with explicit market-missing provenance.

If a quarter has market observations but no valid score observation:

```
score fields = NULL
```

Do not backfill from the next quarter.

## 10. Database integrity

Add indexes for:

```
(source_game_id, quarter)
(classification, quarter)
(game_started_at)
(quarter_started_at)
```

Require uniqueness:

```
(source_game_id, quarter)
```

The same quarter must not be duplicated by repeated collector ticks.

Updates should be deterministic/idempotent.

## 11. API

Add a read-only V4 endpoint:

```
GET /api/v4/quarterly/{game_id}
```

Response must expose:

- game identity
- game start timestamp
- timer provenance
- Q1-Q4 records
- line movement fields
- score fields
- source timestamps
- missing-data indicators

Also expose the raw observation timestamps used to construct each quarterly record, or provide enough IDs/timestamps to audit the derivation.

## 12. Audit endpoint

Add:

```
GET /api/v4/quarterly/audit
```

or an equivalent read-only audit function.

It must report, for Betual NBA:

- games with Q1
- games with Q2
- games with Q3
- games with Q4
- games missing one or more quarters
- duplicate quarter records
- quarters with missing scores
- quarters with missing lines
- timer resets
- timer regressions
- invalid quarter transitions
- score regressions
- line movements observed
- source observations used

Do not label missing market observations as final results.

## 13. Tests

Add tests covering at minimum:

1. Q1/Q2/Q3/Q4 period mapping.
2. Quarter uniqueness.
3. Correct score delta.
4. Correct opening and closing line selection.
5. Correct line min/max.
6. Correct line movement counts.
7. Correct net line movement.
8. Zero-point quarter produces NULL `line_move_per_point`.
9. Missing market does not fabricate a line.
10. Missing score does not fabricate a score.
11. Future market observations cannot populate an earlier quarter.
12. Timer survives collector restart.
13. Timer does not use bookmaker countdown as authoritative elapsed time.
14. Timer cannot move backwards.
15. Re-running the same observations is idempotent.
16. CYBER_2K26 is not altered by the Betual-only quarterly layer.
17. Existing raw snapshots remain unchanged.
18. Existing raw market observations remain unchanged.

## 14. Acceptance audit

The implementation is not complete merely because the table exists.

Run a historical Betual audit and report:

```
total Betual games
games with Q1
games with Q2
games with Q3
games with Q4
games with all four quarters
quarters with valid score
quarters with valid line
quarters with both
quarters missing market
quarters missing score
timer anomalies
duplicate quarters
```

Then provide examples of at least several completed games showing:

```
Game
Q1 score + line start/end
Q2 score + line start/end
Q3 score + line start/end
Q4 score + line start/end
line net movement by quarter
points scored by quarter
elapsed timer by quarter
```

## 15. No look-ahead

This is mandatory.

For every quarterly record:

```
source_observation_timestamp <= derived_record_timestamp
```

A later line or score observation must never be used to populate an earlier quarter.

The final game result must never be available to the collector while constructing an earlier quarter record.

## 16. Separation from BLM decisions

The quarterly dataset is a research/feature dataset.

It must NOT:

- loosen alert gates
- create alerts by itself
- alter prediction generation
- alter model weights
- change settlement
- rewrite historical results
- classify a missing market as a final result

The first implementation should be collection + storage + API + audit + tests only.

## 17. Completion requirement

Do not report:

```
QUARTERLY COLLECTION — COMPLETE
```

until both conditions are met:

1. Automated tests pass.
2. A real completed Betual game is demonstrated with Q1, Q2, Q3 and Q4 score/line data retrieved from the stored raw observations and exposed through the API.

If no completed live game is available, report:

```
QUARTERLY COLLECTION — IMPLEMENTED, LIVE PROOF PENDING
```

Do not claim live collection success from unit tests alone.
