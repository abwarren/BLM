# BLM V4 — Frontend Checkpoints Must Use Game Time

## Objective

The frontend must identify checkpoints by the actual observed game state/time, not by a percentage of game completion.

## Hard Requirement

Do not use `10%`, `20%`, `30%`, etc. as the primary visible checkpoint label.

The visible checkpoint must use:

- period / quarter
- actual game clock
- optionally elapsed game minutes

Examples:

```text
Q1 · 05:00
Q2 · 07:49
Q3 · 04:15
Q4 · 01:20
```

For a detailed view:

```text
Q2 · 07:49
Elapsed 16.18 / 48.00 min
Progress 33.7%
```

`progress` may remain as an internal/stored descriptive field, but it must not replace the human-readable checkpoint time.

## Data Source

Use the already-corrected checkpoint fields:

- `period_label_at_checkpoint`
- `clock_at_checkpoint`
- `elapsed_minutes`
- `remaining_minutes`
- `progress`
- `classification`

Where a valid period/clock exists, it is authoritative for display. Do not reconstruct a displayed clock from percentage.

## Historical Checkpoints

Existing database percentage fields may remain for compatibility and research. The frontend should display the actual period/clock as the primary checkpoint identifier.

Example:

```text
TIME        MARKET     CURRENT PACE   REQUIRED PACE   PACE GAP   FINAL
Q1 05:00    170.5      4.12           4.45            +0.33      181
Q2 07:49    174.5      4.38           4.21            -0.17      181
Q3 04:15    179.5      4.76           3.91            -0.85      181
```

Do not headline these rows as `10%`, `30%`, `60%` when the actual game-time state is available.

## No Prediction Change

This is a presentation/data-label correction only.

Do not introduce:

- predictions
- Over/Under calls
- probabilities
- fair value
- edge
- confidence
- z-scores

The checkpoint answers **when the observation occurred**, not what the system predicts.

## Acceptance Tests

Verify a live/detail checkpoint renders like:

```text
Q2 · 07:49
```

and not:

```text
30%
```

If progress is also shown, it may appear separately:

```text
Q2 · 07:49
Elapsed 16.18 / 48.00 min
Progress 33.7%
```

The percentage is supplementary information only.

## Scope

Do not modify:

- duration classification
- replay protection
- transient regression handling
- velocity logic
- fair-total weighting
- clean-data boundary
- prediction freeze
- pace trajectory calculations

STOP after implementing and verifying the frontend checkpoint presentation.
