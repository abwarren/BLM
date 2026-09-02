# SETTLEMENT FORENSIC REPAIR — D1/D2/D3 (2026-09-02)

Scope: full forensic settlement audit of `blm_pokerbet.db` + all downstream
O/U statistics (directive: BLM prediction → side; market line → settlement
boundary; actual vs market line → WIN/LOSS/PUSH; never actual vs BLM).

## Audit outcome (evidence, read-only recompute from primitives)

- prediction_scores: stored ou_prediction/ou_result/ou_correct recomputed
  from (model_total, market_total, actual_total) — **0 mismatches**.
- checkpoint_market: stored outcome recomputed from (blm_fair_value,
  live_market_line, actual_final_total) — **0 mismatches**.
- No impossible states, no duplicates, no scored-after-result rows, no
  classification contamination, no actual-vs-BLM settlement anywhere in
  code (v1..v4) or data.  The core settlement semantics were already
  correct (M008/M009 lineage); the directive's invariant was verified,
  not violated.

## Defects found + fixed

### D1 — market_history line source dead (data repair, 94 rows backfilled)

`record_market_history` read lines ONLY from `snapshots.total_line`, which
is NULL on 100% of snapshots in this population (lines arrive via the
eu-swarm WS MatchTotal feed → `market_observations`).  Result: every
market_history row frozen with NULL OLV/CLV/outcomes/edges while the data
existed in the same DB (checkpoint_market already used the fallback).

Fix (blm_v4/scorecard.py): OLV/CLV now come from the SAME authoritative
primitives as the checkpoint layer (`_first_verified_line` /
`_last_verified_line`); the upsert now refreshes the opening-side fields
(opening_total/opening_spread/outcome_olvc/opening_total_edge) so a row
first written before observations existed can still gain them.

Live DB backfill: 94 rows recomputed transactionally from primitives
(opening side STUCK — see deployment note; closing side reverted by the
pre-fix in-memory code until the service restarts).

### D2 — 12:00 period-boundary sentinel mislabels checkpoint positions

`clock_minutes` treated a 12:00 period-start display as "2 minutes
elapsed" ((q-1)*10 - 2), displacing checkpoint selection up to 5pp (30pp
for Half End).  Fix (blm_v4/projection.py + scorecard.py `_progress_of`):
displays ≥ 10:00 clamp to period start (contribution 0); Half End = 20
elapsed minutes.  Applies to FUTURE rows only — checkpoint_market rows
are immutable by design (12 pre-existing mislabeled pct20 rows at
`2nd Quarter 12:00` stay frozen and are documented, not migrated).

### D3 — settlement regression tests + integrity scan

tests/test_settlement_semantics.py (17 tests): the six directive cases
(OVER/UNDER win/loss/push + position push + "model error must not decide
result") against `_checkpoint_outcome` AND the authoritative `_score_row`;
pipeline-level zero-violation test; corruption-detection test; sentinel
and market_history-WS-fallback tests.  `settlement_integrity_violations()`
in blm_v4/scorecard.py is the single scan implementation used by tests,
ad-hoc scripts, and the live DB.

## Deployment note — NOT COMPLETE (service restart pending)

`systemctl --user restart blm-server` was DENIED this session.  The
running server (pid 979, started 18:08) still executes the pre-fix
`record_market_history` upsert every 60s: it does not touch the repaired
opening side, but it re-NULLs closing_total/outcome_clv/
closing_total_edge and keeps inserting new line-less rows.  Live state
after backfill: market_history 106 rows — opening_total set 94,
closing_total set 0.

Finish step (one command, then verify):
1. `systemctl --user restart blm-server`
2. `python3 -m blm_v4.scorecard --once`   # force a cycle with fixed code
3. verify: market_history opening_total == closing_total == row count,
   integrity scan zero violations.

## Tests

Full suite: **317 passed / 0 failed** (incl. 17 new).
