# AUTO-BET EXECUTION FLOW (Phases Three, Four, Five, Eight, Nine)

Every statement below names the function that implements it. Status vocabulary
is the ledger's, not a dashboard label.

## 1. End-to-end path

```
BLM live payload (blm_v4/api.py:v4_live)
  │  under_alert{active, checkpoint, trigger_line}
  │  under_alert_eligibility{eligible, reason}
  │  market{total_line, under_odds, market_status, observed_lines}
  │  projector{required_pts_per_min, progress_pct, captured_at}
  ▼
BettingWorker.poll_once                          betting/worker.py:66
  │  (a) global kill switch + auto-bet switch re-read from the ledger
  │  (b) for each game: executor.evaluate(...)   betting/executor.py:199
  ▼
evaluate — gates in this order, each returning NO_BET + reason
  1 kill switch / per-game switch
  2 under_alert.active is True
  3 under_alert_eligibility.eligible is True
  4 alert identity present  -> alert_id = "game|checkpoint",
                              ik = "game|checkpoint|alert_id"
  5 game.live is True
  6 alert freshness: age(projector.captured_at) <= cfg.alert_max_age_s
  7 market present: finite total_line, finite trigger_line
  8 market ACTIVE: market_status in {LIVE, OPEN}
  9 odds present and in [MIN_ODDS, MAX_ODDS]   (never derived from the line)
 10 required projection fields finite
 11 all three money limits CONFIGURED (missing -> blocked)
 12 unit price present, in range; stake_units in (0, 1]; one unit maximum
 13 stake = round(unit_price * units, 2) > 0 and <= max_stake_per_bet
 14 daily limits: today_stats().verifiable, bets < max_bets_per_day,
    exposure + stake <= max_daily_exposure
 15 rung_rule.autonomous_execution_command(game, TOTAL, UNDER) == ALLOW
    and its trigger_line == the alert's trigger_line (immutability)
   ▼
store.claim(rec)                                  betting/store.py:237
  │  one BEGIN IMMEDIATE:
  │    idempotency_key exists        -> refuse, return existing row
  │    game slot held by a blocking  -> refuse, return holder
  │      status (SUBMITTING, SUBMITTED, ACCEPTED, UNKNOWN, RECONCILING)
  │    else INSERT ledger row (PENDING) + INSERT claim (held)
  │  any sqlite error -> exception -> worker fails closed, no bet
  ▼
executor.execute(candidate, game=<fresh re-read>)  betting/executor.py:391
  1 kill switch re-read at the boundary  -> BLOCKED/GLOBAL_KILL_SWITCH
  2 _revalidate_at_submission(...)       executor.py:103
      identity, liveness/status, alert identity, alert freshness,
      trigger line unchanged, rung still ALLOW, market ACTIVE,
      current line unchanged, odds unchanged, duplicate claim,
      limits + unit-price/stake arithmetic
  3 dry_run guard (live_money_enabled must be consistent)
  4 account identity guard (verify_account) -> BLOCKED/ACCOUNT_MISMATCH
  5 ledger -> SUBMITTING
  ▼
PokerBetProvider.submit                           betting/provider.py:103
  │  transport must exist (else ProviderUnavailable -> FAILED)
  │  idempotency key recomputed by provider._idempotency_key and asserted
  │  to equal the ledger key
  │  transport.place_order(...)  (PokerBetBrowserTransport -> verified DOM adapter)
  ▼
Browser DOM adapter                              execution/pokerbet/dom.py
  find_position  (event by canonical game id + teams)
  find_market    (exactly one game-total block)
  parse_total_offers / select_total_offer (exact trigger line, active, odds)
  click_selection (single clickable target)
  betslip read + place_parlay (line, odds, stake verified)
  ▼
Result mapping                                   betting/provider.py:_place
  ACCEPTED only with a bookmaker reference
  reference-less acceptance -> UNKNOWN
  explicit rejection -> FAILED
  timeout / unparseable / unmapped -> UNKNOWN
  ▼
store.update_status(execution_id, status, ...)     betting/store.py:329
  │  ledger row updated; game claim synced:
  │    blocking statuses keep the slot held
  │    FAILED / CANCELLED / BLOCKED / REJECTED release the slot
  ▼
BettingReconciler (separate thread, never submits)  betting/reconcile.py
  needs_reconciliation() -> provider.find_order_by_idempotency(key)
  ACCEPTED only when the bookmaker reports the order produced by this
  idempotency key with a reference; otherwise the row stays UNKNOWN and
  keeps consuming exposure
```

## 2. Status machine

```
PENDING ──claim──▶ PENDING (ledger) ──execute──▶ SUBMITTING
SUBMITTING ──▶ ACCEPTED   (bookmaker reference present)
SUBMITTING ──▶ REJECTED   (bookmaker explicitly refused)
SUBMITTING ──▶ FAILED     (transport/provider error, no money assumed moved)
SUBMITTING ──▶ UNKNOWN    (timeout, missing/unparseable response)
PENDING/SUBMITTING ──▶ BLOCKED   (kill switch, revalidation, account guard,
                                  unauthorized submission)
UNKNOWN ──reconciler──▶ ACCEPTED (positive evidence only)
UNKNOWN ──reconciler──▶ UNKNOWN  (no evidence — never "no bet")
ACCEPTED/REJECTED ──▶ settlement/reconciliation (existing components)
```

A click is never recorded as `ACCEPTED`: only `_place` with a reference, or the
reconciler with positive bookmaker evidence, can produce it.

## 3. Idempotency

- Key format: `{game_id}|{checkpoint}|{alert_id}` where the autonomous
  `alert_id` is itself `{game_id}|{checkpoint}`. Computed once in `evaluate`,
  stored in the ledger, recomputed identically by the provider and asserted
  equal before transport.
- Execution id: `"bet-" + sha256(key)[:20]`, so the id is stable across
  restarts and independent of wall-clock time.
- `bet_executions.idempotency_key` is UNIQUE; `store.claim` checks and inserts
  inside one `BEGIN IMMEDIATE`, so two processes cannot both insert.
- A second worker cycle sees the existing row and returns
  `duplicate_execution` without touching the provider.
- An UNKNOWN row is never retried automatically. `needs_reconciliation()` hands
  it to the reconciler; a new claim for the same game is refused while the
  slot is held.

## 4. One bet per game

`bet_game_claims` is the persistent authority, keyed by `game_id`, inside the
same transaction as the ledger insert:

- A held slot whose status is SUBMITTING, SUBMITTED, ACCEPTED, UNKNOWN or
  RECONCILING refuses every new claim on that game — including one with a
  different checkpoint or a different alert id.
- A released slot (FAILED, CANCELLED, BLOCKED, REJECTED) may be taken over by a
  new opportunity, because no money moved.
- A ledger read error is not caught and converted: the exception propagates,
  the worker records the pass as failed and places no bet.
- The claim is checked again by `_revalidate_at_submission`
  (`has_blocking_execution`), so a race that slips past evaluation is still
  stopped at the boundary.

## 5. Submission-time revalidation checklist

| Check | Failure code | Source of truth |
|---|---|---|
| game id matches candidate | `GAME_IDENTITY_CHANGED` | fresh payload |
| canonical rung still allows this line | `RUNG_REJECTED:<reason>` | `rung_rule.autonomous_execution_command` |
| trigger line unchanged | `TRIGGER_LINE_CHANGED` | fresh `under_alert.trigger_line` |
| alert fresh at submission | `ALERT_STALE_AT_SUBMISSION` | `projector.captured_at` |
| market active | `MARKET_NOT_ACTIVE` | `market.market_status` |
| current line unchanged | `LINE_CHANGED` | rung decision `current_line` |
| odds present and valid | `ODDS_UNAVAILABLE` | `market.under_odds` |
| odds unchanged | `ODDS_CHANGED` | `market.under_odds` |
| duplicate guard readable | `DUPLICATE_CHECK_UNAVAILABLE` | `bet_game_claims` |
| duplicate execution | `DUPLICATE_GAME_EXECUTION` | `bet_game_claims` |
| limits verifiable | `LIMITS_UNVERIFIABLE` | `today_stats` |
| unit price consistent | `UNIT_PRICE_CHANGED` | config + ledger |
| stake arithmetic | `STAKE_CALCULATION_MISMATCH` | unit_price × units |
| max stake | `MAX_STAKE_EXCEEDED` | config |
| daily count | `DAILY_BET_LIMIT_REACHED` | `today_stats` |
| daily exposure | `DAILY_EXPOSURE_REACHED` | `today_stats` |
| kill switch at boundary | `GLOBAL_KILL_SWITCH` | ledger switch (checked before revalidation) |
| account identity at boundary | `ACCOUNT_MISMATCH` | `verify_account(provider)` (after revalidation) |

A manual (`checkpoint=MANUAL`) execution skips only the rung, trigger-line and
freshness rows above; duplicate, limit, unit-price, stake and account gates
still apply.

Any missing mandatory field is a refusal, not a skip. The worker always passes
the freshly re-read game; direct callers fall back to the candidate snapshot,
and the freshness gate still rejects an old snapshot.

## 6. Restart, disconnect, interruption

| Event | Behaviour |
|---|---|
| worker restart mid-cycle | ledger row already claimed; the next pass refuses a duplicate; reconciler handles UNKNOWN |
| browser disconnects before click | `BROWSER_UNAVAILABLE` → FAILED, slot released |
| browser disconnects after click, before response | `UNKNOWN` (timeout path), slot held, reconciler only |
| DB locked/unavailable at claim | exception → no bet, `summary["error"]`, pass stops |
| DB fails after submission | status update raises; worker stops the pass; the row remains SUBMITTING and blocks the game; reconciliation/settlement re-read it |
| timeout during submission | `UNKNOWN`, never auto-retried |
| lost bookmaker response | `UNKNOWN`, never treated as "no bet" |
| kill switch turned off between evaluation and submission | `BLOCKED/GLOBAL_KILL_SWITCH` at the boundary |

## 7. Worker/Fast Lane notes

Only one autonomous loop exists (`BettingWorker`) plus one read-only
`BettingReconciler`. There is no Fast Lane component in this tree
(`grep -rni "fast_lane\|fast lane"` → no implementation). No second pathway can
bypass the kill switch, the claim or the revalidation because no second pathway
exists. The manual `checkpoint=MANUAL` entry point skips only the rung and
alert-freshness gates (a human is explicitly betting the current market) and
still enforces duplicate, limit, market, line/odds and identity gates.
