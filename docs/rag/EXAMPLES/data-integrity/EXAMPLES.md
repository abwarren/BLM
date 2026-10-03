# EXAMPLES — Data Integrity

Retrieval keywords: data integrity examples, positive, negative, edge case, quality gate,
contamination, score regression, impossible jump, snapshot history, terminal, schema.

Each case: the RULE, a POSITIVE example, a NEGATIVE example, an EDGE case.

---

## Case D-1 — Snapshot-history quality gate

**Rule.** `_snapshot_history_quality` checks ordering, identity, monotonicity, no
impossible transitions, classification consistency. A single bad snapshot poisons the
game.

**Positive.**
```
order ok, one source_game_id, scores non-decreasing, classification stable
-> "OK"
```

**Negative.**
```
home score drops from 81 to 60 -> "INVALID", "score regression (contamination?)"
(scores never decrease within a fixture)
```

**Edge (transient glitch).**
```
a 1-4 pt dip that recovers within GLITCH_RECOVERY_ROWS (5) -> tolerated, stays OK
a sustained regression -> "INVALID", "sustained score regression"
```

**Edge (impossible jump).**
```
+50 pts in <90s -> impossible (virtual ~33 pts/min) -> contamination
+50 pts across a multi-minute gap -> legitimate fast game
```

---

## Case D-2 — Terminal-checkpoint exclusion

**Rule.** Terminality comes from the row's own game-time evidence, never a bucket.

**Positive.**
```
elapsed 40.0 of 40 (BETUAL_NBA) -> terminal (ELAPSED_FULL_DURATION)
```

**Negative.**
```
row in the pct100 bucket but elapsed 39.25 of 40 (98.1%) -> NOT terminal
(bucket name is a grouping label, not evidence)
```

**Edge.**
```
status='ended' + a mid-game frame's own elapsed time -> the per-frame evidence wins;
the isolation flag is not trusted over game time
```

---

## Case D-3 — Frozen checkpoint immutability

**Rule.** A `checkpoint_market` row is INSERT-OR-IGNORE keyed
`(source_game_id, checkpoint_pct)`; never rebased.

**Positive.**
```
re-run record_checkpoint_market -> byte-identical row (immutability)
```

**Negative.**
```
Rebasing a frozen checkpoint with a later model build -> forbidden
(predictions are current-code-wins; checkpoint_market is frozen)
```

**Edge.** Missing market at a checkpoint -> honest NULL, never a borrowed line.

---

## Case D-4 — No fabricated value

**Rule.** Missing input -> NULL / fail-closed.

**Positive.**
```
no market at pct10 -> live_market_line NULL, signal NULL, outcome NULL
```

**Negative.**
```
Substituting opening_line for a missing live_market_line -> WRONG
```

**Edge.** A missing league reference -> active=False (never a global borrow).

---

## Case D-5 — Schema/field integrity

**Rule.** Select the market line by price/type, group by `competition_slug`, and
never use a display name or a status flag for a result.

**Positive.**
```
SELECT ... WHERE market_type='MatchTotal'  ; GROUP BY competition_slug
```

**Negative.**
```
markets[0] (array position)  ;  GROUP BY games.competition (display name)
  ;  treat games.status='ended' as a final
```

**Edge.** `final_result_status` production values are OK / UNKNOWN /
NEEDS_RECONCILIATION / INVALID — even though the DDL comment reads only "OK |
UNKNOWN" (see ../CONTRADICTIONS.md).

---

## Case D-6 — Provenance integrity for a page verdict

**Rule.** `result_source='RESULTS_PAGE'` must only stand where an OK row exists.

**Positive.**
```
475 stale provenance rows repaired to 0 in bootstrap (idempotent repair_stale_provenance)
0 verdict rows stripped
```

**Negative.**
```
353 rows claimed RESULTS_PAGE while holding no final -> a stale lie
```

**Edge.** The repair is idempotent and never strips a verdict-bearing row.
