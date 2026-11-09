# AUTO-BET IMPLEMENTATION AUDIT (Phase One)

Branch: `test/autobet-live-readiness-2026-11-08`
Starting commit: `f506564` (parent branch `handoff-2026-09-07`)
Audit date: 2026-11-08
Live betting: **DISABLED** for the whole audit. No production file, database,
service or credential was touched.

Commits produced by this work:

| Commit | Scope |
|---|---|
| `2de4fbd` | execution core: ledger schema, claims, revalidation, fail-closed provider, reconciler |
| `e95ef71` | PokerBet DOM total-line selection |
| `0be97ce` | dashboard/API state truth, reconciler wiring, test stack observation |
| `f796ae7` | test suite update + new readiness suite |
| this commit | documentation + `STATUS.md` |

The audit below is stated against the code as committed above; every claim was
read from source, not inferred from filenames.

## 1. Original defects

Severity: **S1** = could place/skip/lose a bet or lose money; **S2** = blocks
operation or hides state; **S3** = documentation/hygiene.

| # | Sev | Defect | Evidence | Fix |
|---|---|---|---|---|
| F1 | S1 | The execution ledger could not be constructed at all. A trailing comma before `)` in the `bet_executions` DDL raised `sqlite3.OperationalError: near ")": syntax error`, so **every** `BettingStore()`, the worker and every auto-bet test failed at startup. | `store.py` DDL; baseline run: 54 failed, 50 errors, 237 passed | DDL corrected |
| F2 | S1 | `provider_from_config` returned a provider whose transport was `None`; `submit` then raised `AttributeError`, which the executor classified as **UNKNOWN** — an unsubmitted bet recorded as possibly placed. | `provider.py` | missing transport raises `ProviderUnavailable` → ledger status `FAILED`, never `UNKNOWN` |
| F3 | S1 | The provider's transport idempotency key was derived differently from the ledger key, so the bookmaker de-duplication and the local ledger disagreed. | `provider.py` `_place`/`submit` | one key, computed once and passed through |
| F4 | S1 | The DOM adapter chose the line whose **odds were closest to 1.85** and could fall back to a neighbouring or team total. It could bet a market the alert never triggered. | `execution/pokerbet/dom.py` `find_market` | explicit `TotalOffer` parsing + exact-trigger-line selection; ambiguity/suspension/team total rejected |
| F5 | S1 | Odds and line were conflated (the current total was read into the odds slot), so "odds 1.85" could be satisfied by a total of 185. | executor observation dict | `market.under_odds` is separate from `market.total_line`; both revalidated |
| F6 | S1 | No one-bet-per-game enforcement. Two checkpoints (or two alert ids, or two workers) on the same game each produced an execution row and a submission. | executor `execute`, store | `bet_game_claims` table, claimed atomically inside the same `BEGIN IMMEDIATE` as the ledger insert; blocking states SUBMITTING/SUBMITTED/ACCEPTED/UNKNOWN/RECONCILING |
| F7 | S1 | The canonical rung validator was never called on the autonomous path; a candidate could be executed at any line. | executor `evaluate` | `rung.autonomous_execution_command` called; trigger line immutable; rung decision + size + rungs-moved recorded |
| F8 | S1 | No submission-time revalidation: a candidate evaluated 60 s earlier was submitted with its old line/odds. | executor `execute` | `_revalidate_at_submission` runs identity, rung, alert freshness, market active, line, odds, duplicate, limits, unit-price/stake consistency |
| F9 | S1 | An `ACCEPTED` result without a bookmaker reference was trusted. A click could be recorded as an accepted bet. | executor result mapping | reference-less acceptance downgraded to `UNKNOWN`, which only the reconciler may advance |
| F10 | S1 | `UNKNOWN` submissions were abandonable; nothing reconciled them and nothing blocked a retry, so a "no visible order" could justify a second bet. | no reconciler in tree | `blm_v4/betting/reconcile.py`: read-only pass, advances only on positive bookmaker evidence; UNKNOWN blocks the game claim |
| F11 | S1 | Financial limits could be absent/unbounded and daily exposure ignored PENDING submissions. | `config.py`, `store.py` `today_stats` | limits mandatory at evaluation; PENDING counted in exposure; unit-price/stake arithmetic revalidated at submission |
| F12 | S2 | The dashboard read credential keys that do not exist and displayed a cosmetic switch; it could show an armed system the backend had refused. | `dashboard.js`, `betting/api.py` | backend-truth `worker`, `browser`, `submission_authorized`, `browser_submit_enabled`, status counts |
| F13 | S2 | The worker ignored per-game failures and could continue a pass after a critical persistence error. | `worker.py` | per-game exception fails closed and stops the pass with `summary["error"]` |
| F14 | S2 | Account identity was treated as an opaque string in config but as an integer at the bookmaker, so the guard and provider could disagree. | `config.py`, `account_guard.py`, fixtures | integer contract documented and aligned across config, oracle, guard, provider, fixtures |
| F15 | S3 | A dead `_submit_live`, a duplicated `provider_from_config` block and a duplicated method existed in `provider.py`. | `provider.py` | removed; single provider factory |

Pre-existing, unrelated, still open: `tests/test_capture_efficiency.py::test_phase2_tick_body_records_buckets` raises `KeyError: 'panel_read_ms'` at `blm_v4/collector.py:3628`. `collector.py` is unmodified on this branch; recorded in `AUTO_BET_TEST_RESULTS.md`.

## 2. Actual execution path (stage-by-stage)

Values in **Wired** are: `yes` (on the autonomous path), `manual` (only via the
manual validator), `no` (exists but not on the path), `absent`.

| Stage | File / function | Input | Output | Failure handling | Retry | Persistence | Wired |
|---|---|---|---|---|---|---|---|
| UNDER alert | `blm_v4/live_analytics/under_alert*`, surfaced by `blm_v4/api.py:v4_live` | live game + pace projection | `under_alert{active,checkpoint,trigger_line}` | not active → no candidate | n/a | DB alert row (pre-existing) | yes |
| Alert eligibility | `under_alert_eligibility` in the live payload | projection, market status | `{eligible, reason}` | ineligible → skipped | n/a | payload only | yes |
| Candidate generation | `betting/executor.py:evaluate` | one live game dict, `BettingConfig`, `BettingStore` | candidate dict (trigger line, current line, observed ladder, odds, rung size, rungs moved, market status, alert capture time) | returns `(None, reason)`; nothing persisted | none | no | yes |
| Rung validation | `betting/rung.py:autonomous_execution_command` via `evaluate` | trigger line, current line, rung size, checkpoint | allow / reject reason | `RUNG_REJECTED:<reason>` | none | candidate only | yes |
| Betting evaluator | `evaluate` (same function) | candidate | decision + planned stake | rejection reason returned | none | no | yes |
| Risk validation | `evaluate` (`cfg`), `store.today_stats` | limits, today's count/exposure | accept/reject | error code `LIMIT_*` | none | reads ledger | yes |
| Execution claim | `store.claim` → `_claim_game` + insert | execution id, game id, idempotency key | `(True, record)` or `(False, existing)` | any DB exception → refusal (fail closed) | safe to re-call (idempotent) | `bet_executions` + `bet_game_claims` in one `BEGIN IMMEDIATE` | yes |
| Worker | `betting/worker.py:poll_once` | live payload fn | summary dict | per-game exception → FAILED, pass stops with `error` | next poll, never within a claim | via store | yes |
| Browser provider | `betting/provider.py:PokerBetProvider.submit` | claim record, selection, stake | `ExecutionResult` | transport absent → `ProviderUnavailable`→FAILED | never auto-retried | status update | yes (gate closed) |
| Event resolution | `execution/pokerbet/dom.py:find_position` | game identity, market | event observation | not found / ambiguous → `GAME_NOT_REGISTERED` / refusal | none | no | yes (gate closed) |
| Market resolution | `dom.find_market` + `parse_total_offers` | DOM text | `TotalOffer` list | missing/ambiguous game-total → refusal | none | no | yes (gate closed) |
| Selection | `dom.select_total_offer` | offers, trigger line, position | one `TotalOffer` | no exact line / suspended / no odds → refusal | none | no | yes (gate closed) |
| Click | `dom.click_selection` | observation | `bool` | not unique/clickable → `SELECTION_NOT_CLICKED` | none | no | yes (gate closed) |
| Betslip verification | provider/transport betslip read + `dom.place_parlay` | expected line/odds/stake | verified receipt | mismatch → `BETSLIP_VERIFICATION_FAILED`, no submit | none | status | yes (gate closed) |
| Stake validation | `execute` (`_revalidate_at_submission`) + provider | unit price, units, stake | accept/reject | `UNIT_PRICE_CHANGED`, `STAKE_CALCULATION_MISMATCH` | none | ledger fields | yes |
| Submission authorization | `config.submission_authorized`, `browser_submit_enabled`, kill switch | env + persisted switches | allow/deny | deny → `SUBMISSION_UNAUTHORIZED`/dry-run path | none | `mode` recorded | yes |
| Submission | provider `_place` → transport | idempotency key, selection, stake | bookmaker response | timeout/unknown → `UNKNOWN`; error → FAILED | **never** on UNKNOWN | status `SUBMITTING`→… | yes (gate closed) |
| Confirmation | `_place` response parsing | response payload | `ACCEPTED` + reference | reference missing → `UNKNOWN`; rejected → FAILED | via reconciler only | final status + reason | yes |
| Persistent record | `store.update_status` | execution id, status, extras | row + claim slot | DB error surfaces to worker | n/a | `bet_executions`, `bet_game_claims` | yes |
| Reconciliation | `betting/reconcile.py:BettingReconciler` | UNKNOWN rows, provider lookup | ACCEPTED only on found order | provider error → stays UNKNOWN, marked attempted | bounded interval | `reconciliation` fields | yes |

## 3. Implemented but not wired (must not be counted as live capability)

- **Fast Lane** — this tree contains no Fast Lane module, endpoint, thread or
  setting. `grep -rni "fast lane\|fast_lane"` finds no implementation. There is
  nothing to audit or wire; the directive's Fast Lane requirements are
  satisfied only in the sense that there is no second pathway that could bypass
  the shared controls.
- **`betting/engine.py` (`BetEngine`, `ClaimStore`)** — a replay/backtest engine
  over historical data. It does not build candidates from live games, does not
  reach the provider and does not write `bet_executions`. It is **not** a
  parallel live execution pathway; it is left untouched.
- **`fixture_context.py`** — read-only display helper for the DB explorer.
  Unrelated to execution; committed only because `betting/api.py` imports it.
- **Live browser-session reader** — no reader is installed in this environment,
  so the dashboard's BROWSER tile honestly reports `UNKNOWN` (`connected=None`).
  The verifier itself (`get_authenticated_account_id`) exists and is used for
  the account guard.
- **OPL/opening-line semantics** — not present anywhere in this repository
  (see `AUTO_BET_MARKET_SELECTION.md` §5). No behaviour was invented.

## 4. Wiring evidence

- Worker constructed and started in the real server: `server.py:508`
  (`BettingWorker(...)`, `poll_interval_s` from `BETTING_WORKER_POLL_S`).
- Reconciler constructed and started: `server.py:518` (`BettingReconciler`).
- Live payload served from the same in-process authority as the dashboard:
  `server.py:_live_games_for_betting` → `blm_v4.api.v4_live` (4 s bounded cache;
  any error yields `[]`, so a stale evaluation is never used).
- Provider construction is the only path the worker uses:
  `betting/provider.py:provider_from_config` (line 668) → `PokerBetProvider`.
- The execution boundary checks, in order: persisted auto-bet switch, dry-run,
  authorization, kill switch, then claim (`store.claim`), then revalidation,
  then transport.
