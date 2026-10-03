# 03 — Settlement & Reconciliation Playbook

Retrieval keywords: settlement, reconciliation, PENDING, unresolved, unresulted,
missing final, result_reconciler, settle_worker, candidate_games, REJECTED,
FAILED_ATTEMPT, TEMPLATE_FAILED, VERIFIED, retry cooldown, max_attempts,
instance split, base id, #i1, results page, swarm feed, needs_reconciliation,
final backfill, stuck pending, no colour.

**What this pack answers:** why a game is stuck RESULT PENDING, how the reconciler
and settle worker decide a final, when a result may safely become settled, and when
to remain PENDING.

---

## Rule 3.1 — A RESULT PENDING row is ALWAYS a missing final

**Rule.** `outcome_status(trigger, final)` returns `None` whenever EITHER operand
is `None`. So a PENDING row is always a missing FINAL (or, rarely, an unprovable
line = the NO-LINE family). There is no "resolve the alert" operation.

**Source.** `under_outcome.outcome_status`; ADR-004.

**Valid.** Repair = recover the final into `game_results`; the derived alert then
settles itself.

**Invalid.** Looking for an alert row to update.

**Edge.** A NO-LINE row (trigger line unprovable at the boundary) is correct by
design and must NOT be force-resolved.

**Forbidden.** Guessing a final to clear the colour.

**Verify.** `SELECT final_total, final_result_status FROM game_results WHERE
source_game_id=?` — a NULL/UNKNOWN/INVALID means the final is missing.

---

## Rule 3.2 — The reconciler retries a LIVE, IMPROVING source (the 2026-10-03 fix)

**Rule.** `ResultReconciler.candidate_games` admits a `REJECTED`/`FAILED_ATTEMPT`
game again once `RETRY_COOLDOWN_S` (=3600 s) has elapsed
(`st.outcome IN ('FAILED_ATTEMPT','REJECTED') AND st.updated_at <= ?`).
`retry_cooldown_s=0` restores the old terminal `attempt < max_attempts` cap (3).
`TEMPLATE_FAILED` stays terminal (SPA-cached render).

**Source.** `blm_v4/result_reconciler.py` — `RETRY_COOLDOWN_S = 3600.0`,
`candidate_games` SQL, `_retry_cutoff`.

**Why.** A rejection reason of `render_complete=false` reflects the feed's render
AT THAT MOMENT (game still in play). The old count cap made it PERMANENT: 2,486
games permanently exhausted; 7,233 ended games lacked an OK final; their alerts
stayed RESULT PENDING forever.

**Valid.** A REJECTED game at attempt 3, `updated_at` > 1h ago → re-armed.

**Invalid.** A REJECTED game inside the cooldown → NOT readmitted (no request
storm).

**Edge.** A `VERIFIED` game whose OK row is no longer persisted IS a candidate
again (the pre-fix writers destroyed verified finals).

**Forbidden.** Treating a partial-feed rejection as a terminal verdict on the
game.

**Verify.** `COUNT(*) FROM result_reconciliation_state WHERE attempt >= 4` — any
row at attempt>=4 is impossible under the old cap.

---

## Rule 3.3 — Identity, not just the render, decides an accepted final

**Rule.** The reconciler re-checks IDENTITY (team block / fixture start), not only
completeness. A reply for the BASE id of a virtual fixture is REJECTED for an
`#iN` instance game (`instance_splits`: the same id replayed after a score drop).
A base result cannot be attributed to one instance.

**Source.** `result_reconciler.py` (`_fixture_start_consistent`,
`instance_splits`); `result_policy.validate_page_result` (check d).

**Why.** Betual/Cyber virtuals replay the same fixture back-to-back and the SPA
silently keeps the previous scoreboard when the requested id does not resolve —
739 of 2,907 page-verified results (25.4%) had an arithmetically impossible final.

**Valid.** A base result that matches the fixture identity → accepted.

**Invalid.** Attaching a base final to `#i1`.

**Edge.** The fixture-start window uses `max(regulation, observed_span)` as the
lower bound (regulation is PLAYING time; a virtual fixture's wall-clock span
carries pre-roll/breaks).

**Forbidden.** Attaching a foreign instance's score to an `#iN` game.

**Verify.** `result_reconciliation_state.source` for the id; a feed answer for the
base id does not settle the `#iN` row.

---

## Rule 3.4 — Page-result validity gate (what "a verified final" requires)

**Rule.** A page render is accepted as a final ONLY when: (a) the team block
proves the same fixture (identity policy, prior gate); (b) the render is COMPLETE
— all four quarters present (`parse_quality == 'full'`); (c) the quarter scores SUM
to the compact final line; and (d) the final is CONSISTENT WITH THE CAPTURED
HISTORY — neither side lower than a score already observed.

**Source.** `blm_v4/result_policy.py::validate_page_result`.

**Why.** A completed 4-quarter game always renders four quarters; a 2/3-quarter
render is a mid-game or foreign-instance frame. Scores are monotonic within a
fixture.

**Valid.** quarters sum to the final; both sides >= observed max → passed.

**Invalid.** `parse_quality='partial'`, `render_complete=false` → rejected.

**Edge.** No captured history at all → the history-consistency check is
INAPPLICABLE (the render stands on its own), not a failure.

**Forbidden.** Publishing a partial mid-game feed line as a final.

**Verify.** `validate_page_result(parsed, history_rows)` → `{"passed": bool,
"checks": {...}, "failures": [...]}`.

---

## Rule 3.5 — The settle worker never destroys a page verdict

**Rule.** `settle_worker` selects ended games whose stored result is not VERIFIED
OK and NOT (`result_source='RESULTS_PAGE'` AND OK). Its writes carry an ATOMIC
guard `WHERE NOT (result_source='RESULTS_PAGE' AND final_result_status='OK')`,
evaluated at WRITE time — so a verdict that lands between the provenance read and
the write is still protected.

**Source.** `blm_v4/settle_worker.py::settle_once` (candidate SQL), `_settle_game`
(the atomic guards).

**Why.** The pre-fix "a disagreeing endpoint lifts the protection" clause NULLed
verified finals. A plain `!= 'OK'` guard would be wrong: a snapshot-derived OK row
is legitimately corrected in place.

**Valid.** A RESULTS_PAGE OK row is skipped entirely; a disagreement is flagged.

**Invalid.** Re-deriving a snapshot verdict over a page verdict.

**Edge.** The same worker's candidate query has no `!= 'OK'` guard on the
`NEEDS_RECONCILIATION` label, so the label cycles NEEDS_RECONCILIATION ↔ UNKNOWN
(counts drift; timestamp your numbers). No verified final is at risk from the
candidate query (RESULTS_PAGE is excluded).

**Forbidden.** Writing NULL over a verified final.

**Verify.** `is_results_page_verdict(row)` gate at write time; `result_conflicts`
row on disagreement.

---

## Rule 3.6 — Distinguish "correctly blank" from "lost final"

**Rule.** "The card is correctly blank (no provable verdict)" and "the pipeline
lost the final" look identical on screen. Only the DB tells them apart.

**Source.** `DECISIONS.md` §13.F; result-colouring audit.

**Valid.** Blank because `final_result_status` ∈ {UNKNOWN, NEEDS_RECONCILIATION,
INVALID} → investigate reconciliation.

**Invalid.** Asserting a lost final without reading `game_results`.

**Edge.** Confirmed live (97 ended games/24h): 45 GREEN, 15 RED, 0 PUSH, 37 blank
— every blank had a non-OK status.

**Forbidden.** Calling every blank a bug.

**Verify.** `SELECT COUNT(*), final_result_status ... WHERE final_total IS NULL`.

---

## When to remain PENDING vs when a result may settle

| Situation | Action |
|---|---|
| `final_result_status='OK'` (+ optionally RESULTS_PAGE) | SETTLE (authoritative) |
| `game.status='ended'`, `final_result_status` non-OK / NULL | **remain PENDING** — investigate reconciliation |
| `final_result_status='INVALID'` | **remain PENDING** — never infer a final |
| Feed absence (no entry for the id) | **remain PENDING** — an observation, never a failure; the live worker re-arms hourly |
| Terminal observation present, no OK row | verdict `final_source='observation'` — NON-authoritative; may not revise a settlement |
| Base OK final, `#iN` instance INVALID | **remain PENDING** for `#iN` — do not attach the base final |
| Trigger line unprovable at boundary (NO-LINE) | **remain PENDING** by design — never force-resolve |

---

## Rule 3.7 — Backfill safety (idempotence)

**Rule.** A backfill drives the reconciler's OWN path
(`candidate_games` → `_attach_history_bounds` → `_apply_swarm_result`); it adds NO
second classification. `_persist_result`'s upsert carries
`WHERE final_result_status != 'OK'` and `_stamp_unresolved` inserts only when no
row exists, so an existing OK is IMMUTABLE and a re-run is a no-op.

**Source.** `scripts/backfill_resulted_finals_2026-10-03.py`;
`result_reconciler._persist_result` / `_stamp_unresolved`.

**Why.** Safety that makes a drain repeatable without clobbering verified finals.

**Valid.** 10 rounds over ~6,900 candidates added 2,065 OK rows, OK ids LOST 0.

**Invalid.** A bespoke re-classification of the feed answer.

**Edge.** Feed answers a ROTATING subset per call → run several rounds; a reply
for the base id of a virtual fixture is rejected for an `#iN` instance.

**Forbidden.** Stamping an absence as a failure.

**Verify.** Id-level OK diff before/after (0 ids lost); provenance split.

---

## Rule 3.8 — assert the invariant "no final was lost", not "no row changed status"

**Rule.** The settle worker can reclassify OK→INVALID in a guard-vs-write race
(`_settle_game` reads provenance then may write INVALID). Assert the DATA
invariant — "no final was lost" — not the status invariant.

**Source.** `settle_worker._settle_game` (provenance read then write);
`DECISIONS.md`; STATUS 2026-10-03.

**Why.** Measured: 74 reconciler-verified ids flipped OK→INVALID having KEPT their
final (74/74 non-null) — a status change, not data loss.

**Valid.** "74 ids changed status; 74/74 kept final_total."

**Invalid.** "74 finals were lost."

**Edge.** Pre-existing: the live reconciler worker races the settle worker
identically; a backfill only raises the rate.

**Forbidden.** Reporting a status churn as data loss.

**Verify.** Non-null `final_total` count among the changed ids.

---

## Services that run the workers (where a fix must be activated)

| Worker | Runs in | Cadence env | Default |
|---|---|---|---|
| `ResultReconcilerWorker` | `blm-server` (`server.py`) | `BLM_RESULT_RECONCILE_INTERVAL_S` / `_BATCH` | 300 s / 25 |
| `SettleWorker` | `blm-server` | `BLM_SETTLE_INTERVAL_S` | 45 s |
| full-history scorecard sweep | `blm-server` | (4 h) | — |

**Consequence:** a finals/retry fix lives in **blm-server**, so activating it
needs a `blm-server` restart (its own authorization).
