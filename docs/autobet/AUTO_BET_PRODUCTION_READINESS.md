# AUTO_BET PRODUCTION READINESS (Phases Twelve–Thirteen)

Branch: `test/autobet-live-readiness-2026-11-08`
Starting commit: `f506564`
Last code/test commit: `f796ae7` (this document is the following commit)
Live betting: **DISABLED**. Nothing below authorizes a real-money submission.

## 1. Result

**BLM AUTO-BET: PARTIAL — NOT READY FOR OPERATOR REVIEW**

Four gates are unproven (G2, G8, G11, G12) and one is incomplete (G15). A
PARTIAL result is not a PASS, so the READY statement is deliberately withheld.

## 2. Acceptance gates

| Gate | Status | Evidence |
|---|---|---|
| G1 Complete source audit | **PASS** | `AUTO_BET_IMPLEMENTATION_AUDIT.md`: 15 defects, each with file/function/evidence/fix; stage table marks every stage's wiring; absent components named. |
| G2 Correct market and OPL identification | **PARTIAL** | Exact-trigger-line selection, odds/line separation, team-total/suspended/ambiguous refusal all implemented and tested (48-test suite). **OPL has no representation anywhere in the repo**, so OPL ambiguity cannot be identified; behaviour is fail-closed rejection, documented in `AUTO_BET_MARKET_SELECTION.md` §5. |
| G3 Canonical rung validation connected | **PASS** | `evaluate` calls `rung_rule.autonomous_execution_command`; `_revalidate_at_submission` calls it again on the fresh payload; trigger line immutability enforced. Tests: rung one-down allowed, two-down rejected, missing ladder rejected, rung breach at submission rejected. |
| G4 One-bet-per-game | **PASS** | `bet_game_claims` claimed in the same `BEGIN IMMEDIATE` as the ledger insert; blocking states SUBMITTING/SUBMITTED/ACCEPTED/UNKNOWN/RECONCILING; different checkpoint and different alert id refused. Tests: `test_one_bet_per_game_blocks_a_different_checkpoint`, `test_end_to_end_simulated_session_accepts_once_and_rejects_duplicate`. |
| G5 Atomic idempotency | **PASS** | UNIQUE `idempotency_key`, stable `bet-<sha256[:20]>` execution id, `INSERT OR IGNORE` + select in one transaction. Tests: `test_concurrent_alerts_claim_exactly_one`, `test_worker_restart_does_not_resubmit_an_accepted_game`, existing `test_database_enforces_uniqueness`. |
| G6 Submission-time revalidation | **PASS** | 16 checks in `_revalidate_at_submission`, each with a stable refusal code; worker passes the freshly re-read game. Tests: stale, changed odds, line change, rung breach, kill switch mid-flight, DB failure. |
| G7 Mandatory financial limits | **PASS** | All three limits must be configured; missing/invalid/unreadable → NO BET; PENDING/SUBMITTED/UNKNOWN counted in exposure; unit price authoritative from the DB and revalidated. Tests in the readiness, pre-runtime, betting-execution and stake suites. |
| G8 Correct browser execution | **PARTIAL** | DOM adapter + `PokerBetBrowserTransport` implement game/team/market/selection/line/odds/betslip/stake verification and fail-closed refusals, proven against a mocked adapter. **The transport is not installed by `provider_from_config`**, and no authenticated browser session reader exists here, so the real browser path is not wired or exercised end-to-end. In non-dry-run mode today every submission fails closed with `PROVIDER_UNAVAILABLE`. |
| G9 Betslip verification | **PASS** (contract level) | `dom.place_parlay` re-reads the slip, rejects stake mismatch, duplicate legs, missing betslip; transport steps verify event/line/odds then require exactly one slip entry. Tests: `test_browser_refuses_betslip_mismatch`, `test_browser_refuses_duplicate_betslip_entries`. Real-renderer verification is covered by G8's caveat. |
| G10 Submission confirmation | **PASS** | `ACCEPTED` requires a bookmaker reference; reference-less acceptance becomes `SUBMITTED`/`UNKNOWN`; a click never writes `ACCEPTED`. Tests: `test_browser_happy_path_returns_the_bookmakers_receipt`, `test_browser_acceptance_without_receipt_is_only_submitted`, `test_ledger_records_the_complete_audit_trail`. |
| G11 UNKNOWN reconciliation | **PARTIAL** | `BettingReconciler` is read-only, starts in the server, never submits, advances only on positive evidence, and retains UNKNOWN when no order is found. Tests: `test_reconciliation_confirms_only_with_a_bookmaker_order`, `..._never_assumes_a_missing_order_means_no_bet`, `test_end_to_end_unknown_submission_enters_reconciliation`. **Remaining gap:** `PokerBetProvider` implements no `find_order_by_idempotency`, so in the real configuration every UNKNOWN is marked `LOOKUP_UNAVAILABLE` and stays unresolved; resolution is structurally correct but operationally unproven. |
| G12 Worker and Fast Lane integrity | **PARTIAL** | Worker: per-game exceptions fail the pass, no silent exception, kill switch re-read at the boundary, no retry of UNKNOWN, claim loss impossible via the DB. **There is no Fast Lane component in this tree**, so its integrity cannot be verified; the positive finding is that no second pathway exists to bypass the controls. |
| G13 Kill-switch verification | **PASS** | Re-read in the worker, in `evaluate`, and at the execution boundary; persisted switch defaults OFF; dashboard reads backend state. Tests: `test_kill_switch_flipped_between_claim_and_submit_blocks`, `test_end_to_end_kill_switch_after_claim_blocks_before_browser`, `test_kill_switch_off_by_default_in_fresh_test_stack`. |
| G14 Dashboard/backend consistency | **PASS** | `/api/v4/betting/status` exposes worker, browser, `submission_authorized`, `browser_submit_enabled` and per-status counts; frontend renders those values and no local switch claim; credential presence keys corrected. Tests: `tests/test_frontend_operational_views.py`, `tests/test_active_alert_card.py`, readiness browser-status assertions. |
| G15 Complete test suite passes | **PARTIAL** | Focused auto-bet/execution/market suite: **481 passed, 0 failed**. Pre-existing unrelated failure remains: `test_capture_efficiency.py::test_phase2_tick_body_records_buckets` (`KeyError: 'panel_read_ms'`, `collector.py:3628`, file untouched). Full `pytest tests` exceeds the 10-minute limit and was not completed. |
| G16 End-to-end simulation passes | **PASS** (simulated scope) | `test_end_to_end_simulated_session_accepts_once_and_rejects_duplicate` runs alert → candidate → rung → claim → market/line/odds → betslip → stake → mocked submit → receipt → persistence → duplicate refusal → reconciliation. Failure variations: browser disconnect, suspension, changed odds, missing market, unknown, DB interruption, kill switch. No real-money interaction. |
| G17 Production isolation independently verified | **PASS** | See §4. |

### Gate arithmetic

PASS: G1, G3, G4, G5, G6, G7, G9, G10, G13, G14, G16, G17 (12)
PARTIAL: G2, G8, G11, G12, G15 (5)
FAIL: 0

**Overall: PARTIAL.**

## 3. Remaining defects (ordered by distance to live)

1. **Live provider is not wired (`provider_from_config`)** — `PokerBetProvider`
   is built without a transport, so `_place` raises `ProviderUnavailable`. This
   is fail-closed but means live mode cannot place a bet at all. Required:
   install `PokerBetBrowserTransport` with a connected `PokerBetDomAdapter` and
   the canonical game→event registry when `BETTING_BROWSER_SUBMIT=true`.
2. **No authenticated browser-session reader** — account identity and browser
   state remain `UNKNOWN` in this environment. `set_live_session_reader` is the
   intended hook; nothing calls it in `server.py`.
3. **No bookmaker order lookup** — `PokerBetProvider` lacks
   `find_order_by_idempotency`, so UNKNOWN submissions cannot be confirmed;
   they stay UNKNOWN and keep consuming exposure (safe, but operationally
   incomplete).
4. **OPL undefined** — no field, fixture or documentation. Must be established
   from validated PokerBet evidence before it can be modelled; do not guess.
5. **No Fast Lane** — directive expectations cannot be met for a component that
   does not exist. If Fast Lane is expected, it must be specified and added as
   the same worker path, not a parallel engine.
6. **Pre-existing collector failure** — `panel_read_ms` KeyError; unrelated, not
   fixed here because it is production-collector code.
7. **Production schema migration** — the new `bet_game_claims` table and added
   `bet_executions` columns are additive `IF NOT EXISTS` DDL, applied on the
   first `BettingStore` construction. The production DB currently has **no**
   `bet_game_claims` table; it was **not** migrated. Activation must schedule
   this additive migration explicitly and back it up first.

## 4. Production isolation evidence

- Working branch `test/autobet-live-readiness-2026-11-08`; `main`/deployment
  branches untouched; no merge, no push to main, no deploy, no service restart.
- Production collector process `python3 -m blm_v4.collector --tick 10`
  (PID 1487441, started 2026-11-09 01:53:55) is still running and was never
  signalled.
- Production betting DB (`blm_betting.db`) read-only inspection: 1041
  executions, latest at `2026-11-09T00:53:24Z`, **no `bet_game_claims` table**,
  **no `bet_audit` table**. The absence of the new tables proves this branch's
  code never constructed a store against it; all tests used `tmp_path`
  databases. No write, migration or backup was performed on it.
- No `.env` was edited; no credential was read, entered or logged;
  `BETTING_BROWSER_SUBMIT` was never set true and remains false by default.
- `git status` contains only unrelated pre-existing artifacts
  (`prospective_health_history.jsonl`, `betslip_dump.html`, `blm-dev/`,
  `health/`, `reconcile_verify_report.json`, an old DB backup) which were left
  uncommitted and untouched.

## 5. Operator checklist before any live activation (not performed)

1. Review this document and accept or reject each PARTIAL gate.
2. Establish OPL semantics from validated evidence; decide whether it can
   substitute for the trigger line (recommended: no).
3. Implement the real browser transport installation and the order lookup.
4. Configure `BLM_EXPECTED_ACCOUNT_ID`, limits, unit price, and
   `BETTING_BROWSER_SUBMIT=true` in the deployment environment — not in source.
5. Schedule the additive schema migration against a backed-up production DB.
6. Re-run G8/G11/G15 with a real authenticated session in a staged environment.
7. Only then revisit the readiness classification.

## 6. Commits

| Commit | Content |
|---|---|
| `2de4fbd` | execution core: schema fix, game claims, revalidation, fail-closed provider, reconciler |
| `e95ef71` | DOM exact-line selection |
| `0be97ce` | dashboard/API state, reconciler wiring, test-stack observation |
| `f796ae7` | test fixtures + 48-test readiness suite |
| this commit | six `AUTO_BET_*` documents + `STATUS.md` |

## 7. Required statement

**BLM AUTO-BET: PARTIAL — NOT READY FOR OPERATOR REVIEW**

The engine is materially safer and its fail-closed guarantees are tested, but
live browser execution (G8), OPL identification (G2), UNKNOWN order
confirmation (G11), Fast Lane (G12) and the complete suite (G15) are unproven.
No live betting is authorized. `BETTING_BROWSER_SUBMIT` must remain false.
