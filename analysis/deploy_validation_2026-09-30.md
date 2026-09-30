# PR #3 deployment + T+30 validation — 2026-09-30

**Directive:** operator-authorized merge + controlled deployment of PR #3
(`capture-eff-20260930` @ `f174ade`, base `d50b230`), executed 2026-09-30
19:16 UTC as a single transaction (merge → restart → verify).
**Alert logic untouched throughout** (PR content is capture lifecycle/scheduling
only; the 11 alert-critical modules were proven blob-identical pre-merge and
were not modified after).

## Transaction record

| Step | Result |
|---|---|
| Pre-flight | prod HEAD `d50b230`, 63 dirty owner paths intact, merge-base == `d50b230`, PR tree clean, file set == reviewed 6 files (+1327/−2), both units active/healthy |
| Overlap check | zero overlap between PR files and the 63 dirty prod paths; `blm_v4/collector.py` clean at prod (== HEAD) |
| Merge | `git merge --ff-only capture-eff-20260930` → HEAD `d50b230..f174ade`, 6 files, dirty count still 63 |
| Restart | `systemctl --user restart blm-collector blm-server` @ 19:16:24Z; collector PID 105593→206959, server PID 9520→207231 |
| Rollback path (unused) | `git checkout d50b230 -- blm_v4/collector.py` + restart |

## Immediate post-restart verification (all PASS)

- `healthz`: `{"status":"ok","service":"blm","auth":true,"users":2}`
- New instrumentation visible in logs: `page.content() START/COMPLETE [tick]
  timeout=3.0s`, OVERRAN warnings with capture-kept semantics, per-tick
  `page-capture budget remaining`
- State payload carries `tick_timing` (9 buckets), `null_score_recovery`,
  `ws_lifecycle_suppressed`
- Old-process shutdown noise (blm_v1 TargetClosedError ticks during teardown,
  19:16:25–19:16:37) is expected on any restart; new processes: **0 errors**

## BEFORE vs AFTER (T+30, restart 19:16:25Z, window 19:16:38→19:48, n=206 ticks)

| Metric | BEFORE | AFTER | Δ |
|---|---|---|---|
| tick P50 | 2.47s | 1.44s | −42% |
| tick P95 | 9.54s | 4.65s | −51% |
| tick MAX | 54.9s | 28.1s | −49% |
| ticks > 3.0s page budget | 42.1% | 16.5% (34/206) | −61% |
| line-present% (market_observations) | ~27% (09-28+) | **100%** (1077/1077, all MatchTotal) | recovered |
| post-final snapshots | up to 24/day | ~2 real (4–5 rows incl. boundary rows; see note) | → ~0 |
| NULL-score% | 56% (09-24) → ~13% (09-30) | 13.3% (104/780) | held |
| Service restarts since deploy | — | 0 (collector + server) | — |
| Errors since new PIDs | — | 0 collector / 0 server | — |

**Post-final note:** the raw join count (198) was a fan-out artifact (multiple
`games` rows per `source_game_id`). EXISTS-form truth: 4–5 post-final rows
across 47 games ended since restart; ≥3 are exact-microsecond boundary rows
(`result_at` is stamped from the final snapshot). One sampled tail game was
genuinely live at each capture (clock 02:00→01:00, scores 81→84) and ended
~1 min later — the collector kept capturing until the game disappeared
(19:43:58 log: "game ended (disappeared): 31076786") and wrote **0** snaps
afterwards. This is the new lifecycle logic working as designed.

## New-subsystem activity in window (honest caveats)

- `null_score_recovery`: attempts=0 (nothing eligible in window; mechanism
  live in state payload)
- `ws_lifecycle_suppressed`: 0 activations; the disappearance path handled
  all 47 endings — suppression fires only when the WS bridge delivers DONE
  before the board drops the row
- checkpoint pipeline: 22 rows written post-restart (burst-on-end pattern
  confirmed; end-of-game batch for the 5 live games pending their endings)
- Remaining tail: `slow_event_view_ms` P95 6.7s / max 35.3s (slow-path page,
  30s budget) and one 28s tick / 24.9s persistence stall — overruns are down
  61% but not zero; keep monitoring before declaring full tick-budget victory

## Alert integrity during deployment

- No alert-logic file was modified at any point (verified pre-merge by blob +
  AST; post-merge tree is exactly the reviewed head `f174ade`)
- Alert pipeline live: checkpoint_market writes resumed post-restart;
  REQUIRED_MARGIN, required-rate, fingerprints, final-result logic untouched
- No alert-formula changes were made during validation (per directive)

## Git State

```
Branch: handoff-2026-09-07 (production working tree)
Commit: f174adeb15723ae924953c26c9f24d0cec6ad724 (== PR #3 head)
Remote: git@github.com:abwarren/BLM.git (SSH)
Push status: see STATUS.md "Git State" section (SSH-key dependent)
Working tree: 63 dirty owner paths preserved, untouched
Uncommitted files: owner work + untracked STATUS.md/AGENTS.md (by convention)
Next agent should start from: post-deploy monitoring of capture metrics;
do not modify alert formulas during the validation period
```
