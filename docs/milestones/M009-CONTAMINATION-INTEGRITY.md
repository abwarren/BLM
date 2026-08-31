# M009 CONTAMINATION INTEGRITY — INVESTIGATION + APPLIED POLICY (2026-08-31)

STATUS: **AUTHORIZED + APPLIED (2026-08-31, consolidated M009 directive).**
The logical-exclusion fix is in the working tree (scorecard.py
`_CM_ELIGIBLE_SQL` + applied in `_market_vs_fair_sql` and
`/api/v4/scorecard/events`); the RETRO_XFAIL markers are removed; all
11 contamination-integrity tests PASS.  RED confirmed by running the
marker-free suite against HEAD code in a clean worktree (5 fail without
the fix).  See docs/milestones/M009-INTEGRITY-EVIDENCE.md.

## 1. ROOT CAUSE

`checkpoint_market` rows are intentionally immutable (M009-M1) and the
writer (`record_checkpoint_market`) checks eligibility ONLY at INSERT
time: OK result, >= 15 snapshots, starts Q1, not quality-INVALID.  The
M007-M8 re-verification (`capture_results` loop, scorecard.py ~line 869)
re-checks every ended game's snapshot history against the CURRENT
quality gate EVERY run and, on failure, writes:

- `game_quality` row: `status='INVALID'`, `reason`, `checked_at`
- `game_results.final_result_status = 'INVALID'`

But the headline readers of `checkpoint_market` — `market_vs_fair()`
(`_market_vs_fair_sql`) and `GET /api/v4/scorecard/events` — SELECT all
rows with no eligibility predicate.  So a game that was clean at record
time and is later re-verified INVALID KEEPS feeding headline market-line
analytics indefinitely.  The M4 claim "contaminated games never enter
checkpoint_market" is true only for insert-time contamination.

## 2. LIFECYCLE TRACE (verified in code)

- `games.classification` = COMPETITION classification (classifications.py),
  NOT game quality.  Game quality lives in `game_quality.status`.
- `game_quality` schema: `(source_game_id PK, classification, status
  OK|INVALID, reason, checked_at)` — the invalidity record.
- `capture_results()` — the ONLY path that flips a recorded game to
  INVALID: `_snapshot_history_quality(rows)` -> INVALID ->
  INSERT OR IGNORE into game_quality + game_results INVALID.
  INVALID is final (never rescored) — idempotent.
- `record_checkpoint_market()` — insert-time gate: same `NOT EXISTS
  (game_quality INVALID)` predicate used everywhere else (also in the
  prediction_scores fragment marker at line ~614).
- Consumers of checkpoint_market (all headline): `_market_vs_fair_sql`
  (scorecard), `/api/v4/scorecard/events` (api), `/api/v4/game/{id}`
  checkpoint rows (api — this one is per-game diagnostic and MAY keep
  showing the rows; it is not a headline aggregator).

## 3. DECISION: LOGICAL EXCLUSION (Option B) — not physical purge

The invalidity model ALREADY exists (`game_quality.status='INVALID'` +
`game_results.final_result_status`).  The fix is to make every headline
reader apply the SAME eligibility predicate the writer already uses —
centrally, so no consumer can accidentally include invalid rows.

- `_CM_ELIGIBLE_SQL` (scorecard.py): a shared fragment:
  `JOIN game_results r ON r.source_game_id = cm.source_game_id
   WHERE r.final_result_status = 'OK'
     AND NOT EXISTS (SELECT 1 FROM game_quality q
                     WHERE q.source_game_id = cm.source_game_id
                       AND q.status = 'INVALID')`
- Applied in `_market_vs_fair_sql` and `/scorecard/events`.
- `game detail` (per-game) left as-is: diagnostic, shows the historical
  rows (auditability).

Reasons:
- Preserves historical observations (line, timestamp, checkpoint,
  freshness, BLM differential) — auditability requirement.
- Matches the existing architecture (the predicate already exists at
  insert time + fragment marking; no new semantic model).
- No schema change, no row mutation, no migration.
- Freshness classification (LIVE/STALE/MISSING) is untouched — game
  quality is a SEPARATE dimension from market-observation freshness.
- Duplicate protection preserved (INSERT OR IGNORE + UNIQUE untouched).

## 4. VERIFIED READY-TO-APPLY FIX (authorization required)

Patch: `/tmp/m009-contamination-fix.patch` (git diff of scorecard.py +
api.py at investigation time).  It adds `_CM_ELIGIBLE_SQL` + applies it
in the two headline readers.

Verified WITH the fix applied (11/11 tests green + 81 M3/M4/M5 targeted
green + full suite 277 green incl. the 11 new tests): the retrospective
contamination tests all passed, including:
- rows recorded while clean -> re-verify INVALID -> excluded from
  `market_vs_fair()` games + checkpoints and from `/scorecard/events`
- rows RETAINED in checkpoint_market intact (auditable)
- freshness (STALE) preserved after invalidation
- repeated re-verify / aggregation: idempotent, no inflation, no dupes

## 5. RED TESTS (the documented defect, current behavior)

`tests/test_m009_contamination_integrity.py` — 11 tests, 6 of which are
`xfail` under CURRENT production code (they fail by design, documenting
the defect).  The 5 green tests cover: clean included, initial
contamination excluded, unrelated-INVALID isolation, multiple clean
included, aggregation-stability.

The 6 xfail tests (remove the `@RETRO_XFAIL` marker when the fix is
authorized):
- test_retrospective_contamination_excluded      (THE critical case)
- test_later_invalid_excludes_all_checkpoints
- test_reverification_repeated_idempotent
- test_historical_rows_retained_and_auditable
- test_freshness_classification_preserved
- test_events_exclude_retrospective_invalid

A second independent agent's `tests/test_m009_legacy_contamination.py`
(untracked, not committed by me) documents the same defect independently.

## 6. AUTHORIZATION GATE — to apply when approved

1. `git apply /tmp/m009-contamination-fix.patch` (or re-derive the diff)
2. Remove the `@RETRO_XFAIL` markers in test_m009_contamination_integrity.py
3. Run: contamination suite + M3/M4/M5 targeted + full canonical suite
4. Deploy (restart blm-server + blm-collector), verify live events exclude
   an invalid game while game detail still shows its history

## 7. OUT OF SCOPE (per master directive)

- Do NOT purge/backfill the 4,654 historical rows (pre-M3/M4 NULL
  market_timestamp — converting them would manufacture history).
- Do NOT touch freshness thresholds or semantics (M3).
- Sentinel-position (12:00 Half/Quarter End) and progress-storage NULLs:
  separate investigations (#2, #3) — tests + docs only, no production
  change until authorized.
- Live-pipeline anomaly G (#5): separate investigation.
