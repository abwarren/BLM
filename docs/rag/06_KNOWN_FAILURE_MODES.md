# 06 — Known Failure Modes & Signatures

Retrieval keywords: known bugs, failure modes, defect, symptom, root cause, signature,
PENDING alerts, stuck pending, no colour, blank result, capture alarm, quarter score
zero rows, panel discovery, indentation bug, clobber, impossible final, watchdog flap,
collector offline, WAL balloon, contamination, score regression, lessons.

**What this pack answers:** for each historical defect — problem, symptom, root
cause, correct response, forbidden response.

---

## FM-01 — PENDING alerts remain after games finish

**Problem.** A finished game's alert stays RESULT PENDING indefinitely.

**Symptom.** `games.status='ended'` AND `game_results.final_result_status != 'OK'`
AND `final_total = NULL`.

**Root cause (two independent, both 2026-10-03).** (1) `max_attempts` was a
TERMINAL cap on a live improving source — after 3 in-play rejections a game left the
work set forever, so the complete final published later was never ingested
(2,486 games exhausted). (2) The fixture-identity window used REGULATION as wall
clock, rejecting our own fixture on IDENTITY even once the render was complete.

**Correct response.** Do not settle. Investigate reconciliation; the fix is the
retry cooldown + the wall-clock identity bound (`03_SETTLEMENT_RECONCILIATION.md`).

**Forbidden response.** Guess the final from `game.status='ended'` or the last
snapshot.

---

## FM-02 — Result colouring: blank cards that are NOT bugs

**Problem.** Some finished games show no GREEN/RED/PUSH.

**Symptom.** 37 of 97 ended games blank; every blank had
`final_result_status ∈ {UNKNOWN, NEEDS_RECONCILIATION, INVALID}`.

**Root cause.** No colour is a DELIBERATE fail-closed outcome when a final or line
is unprovable. The "bug" is the missing final, not the absence of colour.

**Correct response.** Distinguish "correctly blank (no provable verdict)" from "the
pipeline lost the final" via the DB.

**Forbidden response.** Force-colouring every card or asserting the colouring rule
is broken.

---

## FM-03 — The clobber: page-verified finals destroyed by re-derivation

**Problem.** 2,592 of 2,907 page-verified games had NO OK row.

**Symptom.** `result_source='RESULTS_PAGE'` present but `final_total=NULL` +
`UNKNOWN`; `result_reconciliation_state.outcome='VERIFIED'` so the game never
retried.

**Root cause.** Both re-derivation paths treated a results-page verdict as "a
suggestion" and lifted protection when the captured endpoint disagreed.

**Correct response.** RESULTS_PAGE + OK is immutable; a disagreement is FLAGGED
(`result_conflicts`), never overwritten. The one-line guard re-checks
`is_results_page_verdict` at WRITE time.

**Forbidden response.** Re-deriving a final from snapshots over a page verdict.

---

## FM-04 — The impossible final (foreign instance)

**Problem.** 739 of 2,907 page-verified results (25.4%) had a stored final total
LOWER than a total already observed live — arithmetically impossible.

**Symptom.** A final below the observed max; provenance looks normal.

**Root cause.** Identity proven from TEAM NAMES only; virtuals replay the same
fixture and the SPA keeps the previous scoreboard when the id doesn't resolve.

**Correct response.** Require fixture identity (team block + start time) AND
history consistency (final side >= observed max).

**Forbidden response.** Trusting a scoreboard without identity + history checks.

---

## FM-05 — "Wrong winner" alerts (wrong instance's score)

**Problem.** An alert settles OVER having appeared to be an obvious UNDER.

**Root cause.** A foreign `#iN` instance's scoreboard was attached to the base
game (the same instance-identity defect as FM-04).

**Correct response.** Do not attach a base final to `#iN`; keep the base id's own
identity.

**Forbidden response.** Mixing a base result into an instance game.

---

## FM-06 — Stale/finished games shown as LIVE (fixed 2026-09-16)

**Problem.** Up to 15-minute stale "live" presentation.

**Root cause (three combined).** (1) `upsert_game` refreshed `last_seen_at` on the
ended transition; (2) the API `live` flag was age-only; (3) the dashboard didn't
filter on status.

**Fix.** Single `300s` state-age bound (`ALERT_MAX_STATE_AGE_S`) + fail-closed
`stale_state` + an independent 60s game-finished reconciler thread. All betting
thresholds/model math bit-identical.

---

## FM-07 — `quarter_score_observations` was 0 rows for the table's whole life

**Problem.** A whole data-collection table stayed empty while the pipeline looked
healthy.

**Root cause.** Commit `36d7538` made `parse_event_view` take teams/score/quarters
from the SELECTED sidebar section by HTML CLASS attributes, but every caller fed
`page.inner_text("body")` (rendered text, no markup), so the section regex never
matched, teams parsed `""/""`, and `_verified_event_view` rejected every capture.
The quarter writer sat BELOW the guard.

**Fix.** `parse_event_view(text, identity_html=None)`; the collector reads the
selected section with a single-element DOM call `_selected_section_html` (NOT
`page.content()`). Fail-closed: a section with no scoreboard yields NULL scores.

**Symptom to grep.** A table at 0 rows while `snapshots` keeps flowing.
**Forbidden response.** Blaming the INSERT or its caller before checking the
identity guard.

---

## FM-08 — Panel discovery coverage capped at the competition count

**Problem.** Tracked coverage capped; most panel rows closed as "vanished" every
tick.

**Root cause.** Commit `0561ea2` re-indented the per-row discovery block OUT of the
inner `for row in comp.games:` loop up to the `for comp` level — so
canonicalisation/`seen_keys`/resolve-queue ran ONCE PER COMPETITION on the leaked
last row.

**Symptom.** Live panel had 40 relevant rows / 6 competitions while the collector
reported `games_tracked=6`, `pending_resolve=1`; `_mark_ended` closed the other
rows.

**Fix.** `_discover_panel_rows(comps)` with the block correctly nested.

**Lesson.** A re-indentation in a large `_tick_body` is invisible to lint and
compiles fine; it shows only as a LOW COUNTER. Diagnose coverage by comparing a
live external truth (fetch the panel, count rows) against `games_tracked` /
`pending_resolve` / `rows_seen`.

**Forbidden response.** Raising a batch/timeout to "fix" a coverage symptom.

---

## FM-09 — Collector watchdog flap on `page.content()` overruns

**Problem.** `blm-collector` self-restarts (`Watchdog timeout (limit 1min 30s)`).

**Symptom.** NRestarts increments; the journal shows a watchdog stop/start with no
agent action; `page.content()` overruns its 3.0s budget (observed 3.9–18.1s).

**Root cause.** Pre-existing: the whole-page round-trip overruns the tick budget
on the CDBL event view.

**Correct response.** Read the journal
(`journalctl --user -u blm-collector --since … | grep -iE "watchdog|Stopped|Started"`)
and `ExecMainStartTimestamp`; report it as a separate finding. Do NOT attribute it
to your own change, and do NOT raise the timeout to hide it.

**Forbidden response.** Restarting to "fix" the flap; hiding it.

---

## FM-10 — WAL balloon / DB write pressure

**Problem.** The SQLite WAL grows without bound under continuous writes.

**Fix.** Periodic PASSIVE `wal_checkpoint` (`blm_v4/wal_hygiene.py`; server
`WAL_HYGIENE_INTERVAL_S` default 60).

**Forbidden response.** Running `PRAGMA wal_checkpoint(TRUNCATE)` against a live DB
while the collector is running; or a write pragma during READ-ONLY diagnosis.

---

## FM-11 — Contamination signature: score regression + impossible jumps

**Problem.** A game's snapshot history contains foreign rows.

**Symptom.** A score that DROPS (regression) or a >50pt hop in <90s.

**Root cause.** Lobby "1st Quarter 23:00 15-2" snapshots mixed into real games
(old resolve-path hole), then jumping to the true mid-game state.

**Correct response.** The quality gate `_snapshot_history_quality` marks the game
INVALID (excluded from scoring). A transient dip of <=4 pts is tolerated only if
scores recover within 5 rows.

**Forbidden response.** Scoring a contaminated game; weakening the gate to make a
test pass.

---

## FM-12 — `_final_result` "100 = pace unavailable" sentinel

**Problem.** A checkpoint projects exactly 100.0 with a 50/50 split while the live
score is far higher.

**Root cause.** The source snapshot carries `quarter=NULL` (event-view rows hold
only the label); `clock_minutes(q=None)` → None; pace unavailable → the model's
fallback `expected_total = 100`.

**Correct response.** 100 is a "pace unavailable" SENTINEL, not a prediction. The
live-score floor masks nothing. Fix the pace INPUT (label→quarter), never the
floor.

**Forbidden response.** Removing the floor or reading 100 as a real projection.

---

## FM-13 — Settle-worker status churn (OK→INVALID) — a status change, not data loss

**Problem.** `NEEDS_RECONCILIATION` counts oscillate; verified OK rows can flip to
INVALID.

**Root cause.** `settle_worker` selects `NEEDS_RECONCILIATION` rows and its upsert
has no `!= 'OK'` guard (label cycles); plus a guard-vs-write race reclassifies a
row written between the provenance read and the write.

**Correct response.** Assert the DATA invariant "no final was lost" (measured
74/74 flipped ids kept `final_total`). RESULTS_PAGE OK rows are excluded from
settlement, so no verified final is at risk.

**Forbidden response.** Reporting the churn as data loss; timestamp-free counts.

---

## FM-14 — Deployment gap: committed ≠ live

**Problem.** A "deployed" fix is not actually running.

**Root cause.** The collector/server run from the WORKING TREE and load modules at
process START. A process started before the fix keeps running old code.

**Correct response.** Prove the running process (start time > module mtime,
NRestarts unmoved, a behavioural counter unreachable under the old code,
`py-spy`/`/proc/<pid>/mem`).

**Forbidden response.** Claiming a fix is live from on-disk correctness.

---

## FM-15 — `search_files` false zeros / denied inline runners

**Problem.** Tooling gives misleading results on this box.

**Symptoms.** `search_files` returns 0 for a pattern clearly present (confirm with
`grep`); `python3 -c "..."` and long compound commands are DENIED by the approval
layer.

**Correct response.** Confirm with `grep` before concluding "absent"; write probes
to a `/tmp/*.py` file and run `python3 /tmp/x.py`.

**Forbidden response.** Concluding "absent" from a single `search_files` zero;
retrying a denied compound command in the same form.
