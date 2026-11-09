# AUTO_BET TEST RESULTS (Phase Eleven)

All runs executed on branch `test/autobet-live-readiness-2026-11-08`, in this
isolated checkout, with an isolated `tmp_path` SQLite database per test. No
production database, service, credential or browser session was used.

Environment note: tests must be run as

```
cd /home/ubuntu/BLM
BLM_ALLOW_HEAVY_TESTS=1 python3 -m pytest <files> -q
```

`BLM_ALLOW_HEAVY_TESTS=1` is required because `tests/conftest.py` refuses to run
heavy suites while the production collector process (`python3 -m
blm_v4.collector --tick 10`) is alive. Setting it is the repository's own
documented override for isolated test runs; it does not touch that process.

## 1. Baseline (before any change, commit `f506564`)

`python3 -m pytest tests/test_betting_execution.py ...` (the auto-bet subset):

**54 failed, 50 errors, 237 passed**

Root cause: the `bet_executions` DDL had a trailing comma, so `BettingStore()`
raised `sqlite3.OperationalError: near ")": syntax error` for every test that
constructed a store. Once repaired, 20 account-identity failures remained
(string vs integer contract), then 0 after alignment.

## 2. New readiness suite

File: `tests/test_autobet_live_readiness_2026_11_08.py` — **48 tests collected**.

```
BLM_ALLOW_HEAVY_TESTS=1 python3 -m pytest \
  tests/test_autobet_live_readiness_2026_11_08.py -q
```

Result at the final code state: **48 passed** (included in the 481-pass run
below; run standalone earlier in the session also 48 passed).

Coverage map (directive Phase Eleven table):

| Scenario | Test |
|---|---|
| valid candidate eligible | `test_end_to_end_simulated_session_accepts_once_and_rejects_duplicate` |
| invalid alert rejected | `tests/test_betting_execution.py::test_alert_not_active_or_ineligible_means_no_bet`, pre-runtime `test_inactive_alert_is_refused` / `test_ineligible_alert_is_refused` |
| stale alert rejected | `test_submission_revalidation_rejects_a_stale_candidate` |
| wrong game rejected | `test_browser_refuses_an_unregistered_game` (identity recheck `GAME_IDENTITY_CHANGED` exercised in the end-to-end variations) |
| wrong market / ambiguous market | `test_ambiguous_unbound_multi_line_market_never_guesses`, `test_browser_refuses_missing_market`, `test_browser_refuses_ambiguous_selection` |
| OPL ambiguity | **not applicable** — no OPL representation in this repository; exact-line refusal tested instead |
| incorrect line rejected | `test_missing_trigger_line_is_refused_not_substituted`, `test_browser_refuses_a_different_resolved_line` |
| suspended market rejected | `test_suspended_market_is_refused`, `test_end_to_end_failure_variations_never_place` |
| missing odds rejected | `test_missing_odds_on_the_selected_line_is_refused` |
| changed odds rejected | `test_submission_revalidation_rejects_changed_odds`, `test_browser_refuses_changed_odds` |
| missing risk limit rejected | `tests/test_betting_execution.py::test_unconfigured_limits_block_betting`, pre-runtime `test_unconfigured_limits_are_refused` |
| maximum stake exceeded | `tests/test_betting_execution.py::test_max_stake_exceeded`, pre-runtime `test_multi_unit_stake_is_refused` |
| daily exposure exceeded | `tests/test_betting_execution.py::test_daily_exposure_reached`, `test_daily_bet_limit_reached` |
| duplicate game rejected | `test_one_bet_per_game_blocks_a_different_checkpoint` |
| concurrent duplicate rejected | `test_concurrent_alerts_claim_exactly_one` |
| database failure rejected | `test_database_failure_is_not_permission_to_bet` |
| browser unavailable | `test_end_to_end_failure_variations_never_place` |
| betslip mismatch | `test_browser_refuses_betslip_mismatch`, `test_browser_refuses_duplicate_betslip_entries` |
| submission unauthorized | `test_browser_submission_is_disabled_by_default`, `test_browser_transport_still_enforces_account_identity` |
| kill switch activated | `test_kill_switch_flipped_between_claim_and_submit_blocks`, `test_end_to_end_kill_switch_after_claim_blocks_before_browser` |
| UNKNOWN → reconciliation only | `test_unknown_submission_is_never_retried`, `test_end_to_end_unknown_submission_enters_reconciliation` |
| valid submission confirmed only with mocked receipt | `test_browser_happy_path_returns_the_bookmakers_receipt`, `test_browser_acceptance_without_receipt_is_only_submitted` |

Additional coverage in the same file: rung one-down allowed / two-down rejected
/ missing ladder rejected, trigger-line immutability, line/odds separation in
parsing, every-line parsing, duplicate-line ambiguity, team-total exclusion,
unclickable outcome, ledger audit trail, reconciler positive/negative evidence,
worker restart without resubmission, account guard integer match/mismatch.

## 3. Regression runs at the final code state

### 3.1 Auto-bet / execution / market focused suite — 16 files

```
BLM_ALLOW_HEAVY_TESTS=1 python3 -m pytest \
  tests/test_autobet_live_readiness_2026_11_08.py \
  tests/test_autobet_execution_subsystem.py \
  tests/test_autobet_pre_runtime.py \
  tests/test_autobet_rung_rule_2026_10_03.py \
  tests/test_autobet_rung_wiring_2026_10_03.py \
  tests/test_autobet_stake_and_unit_size_2026_10_03.py \
  tests/test_betting_execution.py \
  tests/test_betting_manual_contract.py \
  tests/test_betting_account_oracle.py \
  tests/test_execution_game_binding.py \
  tests/test_parlay_execution.py \
  tests/test_pokerbet_live_adapter.py \
  tests/test_pokerbet_quarter_dom.py \
  tests/test_market_line_selection_2026_09_16.py \
  tests/test_results_dom_rows.py \
  tests/test_test_stack.py -q
```

**481 passed, 0 failed** (193.60 s).

### 3.2 Operational/API-adjacent files

```
BLM_ALLOW_HEAVY_TESTS=1 python3 -m pytest \
  tests/test_active_alert_card.py tests/test_auth_authorization.py \
  tests/test_auth_security.py tests/test_fixture_context.py \
  tests/test_frontend_operational_views.py tests/test_test_stack.py -q
```

First run: **85 passed, 2 failed, 1 skipped**. Both failures were in
`test_test_stack.py` and were caused by the isolated fixture game lacking the
new mandatory market observation (no `under_odds`, no `observed_lines`), so the
worker correctly refused to bet (`would_bet == 0`).

Fix: the fixture (`blm_v4/test_stack.py`) now supplies a complete observation
(`under_odds=1.85`, `observed_lines=[177.5 … 182.5]`). This is a fixture
completeness fix, **not** a weakening of the gate — the real gate still rejects
a missing observation. After the fix:

**9 passed** for `tests/test_test_stack.py`, and the 6-file combination passed
apart from the one unrelated skip.

### 3.3 Wider run before the timeout

An earlier broad run (all tests alphabetically before `test_capture_efficiency`)
reached **659 passed, 1 skipped** before hitting the pre-existing collector
failure below and being stopped. The full `pytest tests` suite does not fit the
10-minute tool limit on this machine, so it was not run to completion.

## 4. Failures

### 4.1 Pre-existing, unrelated, NOT fixed

```
tests/test_capture_efficiency.py::test_phase2_tick_body_records_buckets
KeyError: 'panel_read_ms'   at blm_v4/collector.py:3628
```

- Reproduced in isolation at the final code state: **1 failed in 0.76 s**.
- `blm_v4/collector.py` is unmodified on this branch (`git status` clean for
  that file), and no auto-bet module imports it.
- Not fixed because it is outside the directive's scope and touching the
  production collector during a live-readiness task would violate the
  read-only-production boundary.
- It is a real defect in the collector's tick stats initialisation and should
  be fixed separately.

### 4.2 Not run / not proven

| Item | Reason |
|---|---|
| complete `pytest tests` | exceeds the 600 s tool timeout (ran >10 min once) |
| real browser end-to-end | no authenticated PokerBet session reader is installed in this environment; `BETTING_BROWSER_SUBMIT` stays false |
| live bookmaker HTTP transport | intentionally not wired; `PokerBetTransport._place` raises `ProviderUnavailable` |
| OPL-specific scenarios | no OPL representation exists to test |
| Fast Lane scenarios | no Fast Lane component exists in this tree |

## 5. No test was weakened

- No test was deleted, skipped, or marked xfail for this work.
- Two existing expectations were **corrected to the intended contract**:
  same-game-different-checkpoint now expects `duplicate_execution` (previously
  it asserted a second bet was allowed), and account-identity fixtures use
  integers. Both encode stricter behaviour, not looser behaviour.
- New gates are exercised with refusal assertions; nothing was relaxed to make
  the suite green.
