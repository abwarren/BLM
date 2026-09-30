# PR: Fix capture efficiency without changing alert logic

- **base:** `handoff-2026-09-07` (production HEAD `d50b230`)
- **head:** `capture-eff-20260930` (commit `f86e0a5` + this dossier commit)
- **Scope:** TEST ENVIRONMENT ONLY — implemented and verified in the scratch
  worktree `/home/ubuntu/blm-test`.

## Required statements

1. **TEST ENVIRONMENT ONLY.** All work happened on branch
   `capture-eff-20260930` in an isolated worktree. The production working
   tree at `/home/ubuntu/BLM` was not modified by this work.
2. **Alert formulas unchanged.** No line of the diff touches
   `evaluate_under_alert`, fingerprint math, pace/required-rate math, or any
   threshold (verified: every collector.py hunk is a pure insertion except
   two timing-tuple line reformats; sensitive-token grep over the full diff
   shows only IMPORTS and test assertions).
3. **REQUIRED_MARGIN unchanged.** Never re-declared; the analysis script
   IMPORTS it from `blm_v4.live_analytics.under_alert` exactly as the
   validated 2026-09-23 forensic backtest does.
4. **50%/75% checkpoint semantics unchanged.** `ALERT_PROGRESS_PCT` (75.0),
   the boundary set and all checkpoint code are untouched;
   `test_phase5_no_checkpoint_definition_changed` pins
   `ALERT_PROGRESS_PCT == 75.0` and `duration_for()` results as a regression
   guard.
5. **Playwright thread-affinity behavior preserved.** `_page_content_timed`
   is byte-identical to base (no diff hunk intersects its body, lines
   2613–2700); Phase 6 regression tests pin same-thread execution, no worker
   threads, SIGALRM overrun-flag semantics.
6. **Ended-game capture suppression added.** Explicit
   LIVE → FINALIZING → DONE lifecycle: `_end_game` (verified final) and
   `_mark_ended` (disappeared) record the gid terminal; the WS→snapshot
   bridge suppresses DONE games (this closed the dominant waste: game
   31075046 logged 294 snapshots in the ~31 min after `result_at`).
   FINALIZING (final-capture window) still accepts frames — the final
   capture remains possible. A live game cannot become DONE (both writers
   fire only from ended-path code).
7. **NULL-score recovery bounded.** Max 2 re-reads per tick
   (`NULL_SCORE_RECOVER_MAX_PER_TICK`), one per game, budget-aware (skips at
   page-capture budget ≤ 0), and 3 failed episodes → INCOMPLETE marker +
   30-tick backoff. No infinite loop: backoff excludes the game until the
   window expires, and each episode is budget-capped.
8. **Checkpoint-aware scheduling added.** `_progress_tier` (pure arithmetic
   over the clean trajectory) orders the market-rotation queue —
   checkpoint-critical games first. It changes ONLY visit priority: no
   checkpoint definition, trigger condition, required-rate or league-average
   calculation, market-line or result handling is read or written by it.
9. **Instrumentation added.** Nine per-tick timing buckets
   (tick_total/game_discovery/page_content/score_parse/board_parse/
   line_parse/db_write/retry/ended_game) with P50/P95/P99/MAX in the state
   payload, plus `null_score_recovery` / `ws_lifecycle_suppressed` counters.
   Strictly observational — no decision path reads them.
10. **Production was not deployed.** No restart, no deploy, no production DB
    write at any point (analysis used `mode=ro` + `PRAGMA query_only=ON`).

## Files changed

| file | change |
|---|---|
| `blm_v4/collector.py` | +311/−2 (2 removed lines are timing-tuple reformats) |
| `tests/test_capture_efficiency.py` | new, 23 regression tests (Phases 2–6) |
| `scripts/analysis_market_status_by_competition_2026-09-30.py` | new, Phase 7 read-only analysis |
| `analysis/capture_baseline_2026-09-30.md` | new, Phase 1 read-only baseline evidence |
| `analysis/market_status_by_competition_2026-09-30.md` | new, Phase 7 evidence (14,570 settled triggers: LIVE UNDER% > STALE by 7–11pp within EVERY competition) |
| `docs/PR_capture_eff_2026-09-30.md` | this dossier |

## Audit for unintended changes (Phase 2 of the review directive)

Checked and NOT present anywhere in the diff: alert formula edits,
REQUIRED_MARGIN / ALERT_PROGRESS_PCT / duration_for() changes, fingerprint
definitions, market-line interpretation, final-result calculation, database
schema (no CREATE/ALTER/DROP), production configuration
(deploy/*.service), watchdog configuration (WatchdogSec / self-watchdog
constants untouched).

## Test evidence

- 458 passed / 2 skipped across capture-efficiency, collector,
  under-alert, fingerprint, scorecard/deviation and checkpoint suites
  (BLM_ALLOW_HEAVY_TESTS=1, branch worktree).
- `test_live_alert_eligibility` needs the pipeline DB: fails with
  `sqlite3.OperationalError` in a scratch worktree AT BASE `d50b230` TOO
  (independently verified); with `BLM_POKERBET_DB` set it passes 31/31 ON
  THIS BRANCH.
- The 3 `test_under_alert_lifecycle` vocabulary failures are reproduced
  IDENTICALLY at base `d50b230` (independent verification, fresh worktree,
  not assumed) — pre-existing, not attributable.
