# AUTO_BET RISK CONTROLS (Phases Six, Eight, Ten)

## 1. Mandatory financial limits

Configuration object: `blm_v4/betting/config.py:BettingConfig`.
Environment names, no unlimited defaults:

| Variable | Field | Missing value |
|---|---|---|
| `BETTING_MAX_STAKE_PER_BET` | `max_stake_per_bet` | `None` → `limits_not_configured` → NO BET |
| `BETTING_MAX_BETS_PER_DAY` | `max_bets_per_day` | `None` → `limits_not_configured` → NO BET |
| `BETTING_MAX_DAILY_EXPOSURE` | `max_daily_exposure` | `None` → `limits_not_configured` → NO BET |

`evaluate` refuses **before** any claim when any of the three is `None`. An
invalid (non-numeric, non-finite) value parses to `None` and therefore blocks.
There is no code path that infers a "generous" limit, and the frontend cannot
raise them: `betting/api.py` only accepts a unit price and the auto-bet switch;
the limits are server-side environment values.

Staking is unit-based: `stake_amount = round(unit_price × stake_units, 2)`.

## 2. Mandatory checks, in order

| # | Check | Where | Refusal |
|---|---|---|---|
| 1 | all three limits configured | `evaluate` | `limits_not_configured` |
| 2 | unit price present, finite, > 0 | `evaluate` | `unit_price_invalid` |
| 3 | unit price inside `[min_unit_price, max_unit_price]` | `evaluate` | `unit_price_out_of_range` |
| 4 | stake units finite, > 0, **≤ 1.0** (one-unit maximum) | `evaluate` | `stake_units_invalid`, `ONE_UNIT_MAXIMUM` |
| 5 | stake finite and > 0 | `evaluate` | `stake_calculation_failed` |
| 6 | stake ≤ max per bet | `evaluate` + revalidation | `max_stake_exceeded` / `MAX_STAKE_EXCEEDED` |
| 7 | limits readable from the DB | `every` | `limits_unverifiable` / `LIMITS_UNVERIFIABLE` |
| 8 | today's bet count < max bets | `evaluate` + revalidation | `daily_bet_limit_reached` / `DAILY_BET_LIMIT_REACHED` |
| 9 | today's exposure + stake ≤ max exposure | `evaluate` + revalidation | `daily_exposure_reached` / `DAILY_EXPOSURE_REACHED` |
| 10 | unit price still the persisted one | revalidation | `UNIT_PRICE_CHANGED` |
| 11 | stake still equals price × units | revalidation | `STAKE_CALCULATION_MISMATCH` |
| 12 | duplicate/claim check readable | revalidation | `DUPLICATE_CHECK_UNAVAILABLE` |
| 13 | claim held by another attempt | `store.claim` + revalidation | `duplicate_execution` / `DUPLICATE_GAME_EXECUTION` |

## 3. Exposure accounting (what counts)

`BettingStore.today_stats()` counts, for the current UTC day:

`PENDING`, `WOULD_BET`, `SUBMITTING`, `SUBMITTED`, `ACCEPTED`, `UNKNOWN`,
`RECONCILING`

So committed-but-unsubmitted stake, in-flight submissions and unresolved
UNKNOWN submissions all consume the daily exposure. A UNKNOWN bet is never
assumed unsuccessful. Only terminal negative states (`FAILED`, `REJECTED`,
`BLOCKED`, `CANCELLED`) stop consuming exposure — and those release the
one-bet-per-game slot at the same time.

At the submission boundary the executor subtracts the candidate's own
`PENDING` row before comparing against the limits (`own_counted`,
`own_amount`), so a legitimate bet is not blocked by counting itself twice.

## 4. Day rollover and timezone

- `_day()` is **UTC** (`datetime.now(timezone.utc).strftime("%Y-%m-%d")`).
- `day_utc` is written on the ledger row at claim time and never rewritten.
- The day boundary is therefore 00:00 UTC. There is no local-time or
  competition-timezone variant; the dashboard's "today" tiles read the same
  UTC day.
- A record claimed at 23:59:59Z belongs to that UTC day even if it settles
  after midnight. The operator must be aware that this is 02:00 in South
  African local time (SAST = UTC+2).
- **Known limitation:** there is no automatic reset job; "reset" is simply the
  change of the UTC date key. No cron or scheduled mutation is required or
  used.

## 5. Unit-price consistency (dashboard ↔ DB ↔ evaluator ↔ provider)

| Layer | Source |
|---|---|
| DB | `betting_config` key `unit_price`, read by `store.get_unit_price()` |
| Dashboard | `POST /api/v4/betting/settings {"unit_price": x}` writes the DB; status reads it back |
| Evaluator | the worker passes `store.get_unit_price()` into `evaluate` |
| Ledger | `unit_price`, `stake_units`, `stake_amount`, `unit_limit` recorded on the claim |
| Revalidation | persisted price is authoritative; mismatch → `UNIT_PRICE_CHANGED` |
| Provider | receives the computed `stake_amount`; betslip must show exactly that value or `BETSLIP_VERIFICATION_FAILED` |

A missing persisted price is not defaulted: `unit_price_invalid` / blocked.
The API refuses to arm auto-betting without a stake price
(`test_test_stack` workflow arms the price first).

## 6. Authorization and kill switch

Gates, in the order a submission actually encounters them:

1. **Persisted kill switch** (`auto_betting_enabled` + global betting switch)
   checked by the worker per pass, again in `evaluate`, and **again at the
   submission boundary** in `execute` → `GLOBAL_KILL_SWITCH`.
2. **Per-game switch** → `PER_GAME_AUTO_BET_OFF`.
3. **Dry run** (`BETTING_DRY_RUN`, default `true`) → `DryRunProvider`, which
   never reads credentials and never calls the network; result is
   `WOULD_BET`/dry-run ACCEPTED with a dry-run reference.
4. **Live money consistency** (`live_money_enabled`) — belt-and-braces stop
   before any provider interaction.
5. **Account identity** (`verify_account`) — `BLM_EXPECTED_ACCOUNT_ID` must be
   configured and equal the authenticated account; else `ACCOUNT_MISMATCH`.
6. **Browser submit** (`BETTING_BROWSER_SUBMIT`, default `false`) — checked
   inside `PokerBetBrowserTransport._place` immediately before the DOM
   sequence; not true → `ProviderUnavailable` → `FAILED`, no click.

The dashboard's `submission_authorized` is the conjunction of live mode,
browser-submit and configured account identity, computed in
`BettingConfig.submission_authorized` — it cannot be forced by the frontend.
The kill switch overrides every path because it is re-read at the boundary,
not cached at worker start.

## 7. Fail-closed summary

| Situation | Result |
|---|---|
| limit missing/invalid | NO BET |
| DB unreadable for limits or duplicates | NO BET |
| provider transport missing | `FAILED` (never `UNKNOWN`) |
| browser disconnected / ambiguous / suspended / unreadable slip | `FAILED`, no click or no submit |
| account identity unprovable | `BLOCKED` |
| authorization off | `BLOCKED` / `FAILED` |
| submission outcome unclear | `UNKNOWN`, slot held, reconciliation only |
| retry of an unclear submission | refused |

## 8. Test evidence

Readiness suite (`tests/test_autobet_live_readiness_2026_11_08.py`):
`test_one_bet_per_game_blocks_a_different_checkpoint`,
`test_concurrent_alerts_claim_exactly_one`,
`test_kill_switch_flipped_between_claim_and_submit_blocks`,
`test_database_failure_is_not_permission_to_bet`,
`test_unknown_submission_is_never_retried`,
`test_end_to_end_kill_switch_after_claim_blocks_before_browser`,
`test_browser_submission_is_disabled_by_default`,
`test_browser_transport_still_enforces_account_identity`,
`test_ledger_records_the_complete_audit_trail`.

Existing suites covering stake/limit arithmetic:
`tests/test_autobet_stake_and_unit_size_2026_10_03.py`,
`tests/test_autobet_pre_runtime.py`, `tests/test_betting_execution.py`.
