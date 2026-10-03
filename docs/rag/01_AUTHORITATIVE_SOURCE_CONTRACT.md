# 01 — Authoritative Source Contract

Retrieval keywords: authoritative source, source of truth, results page, RESULTS_PAGE,
game_results, final_result_status, OK, INVALID, UNKNOWN, NEEDS_RECONCILIATION,
clean_projections, trigger line, market observation, dashboard not authoritative,
which source, trust, provenance, final_source, settled, observation.

**What this pack answers:** for any critical value in BLM, which source is trusted,
and which sources are NOT trusted. When two sources disagree, this contract names
the winner.

---

## Rule 1.1 — `game_results` with `final_result_status='OK'` is the authoritative final

**Rule.** The game's FINAL total comes from the `game_results` row where
`final_result_status='OK'`. Only an OK row is a verified final.

**Source.** `blm_v4/result_policy.py` (`STATUS_OK="OK"`);
`blm_v4/live_analytics/under_outcome.py::final_total_for` ("backend-authoritative,
then observations"); live schema `game_results.final_result_status`.

**Why.** The results feed is the only source that proves a completed game's
scoreboard; snapshots can lose the terminal frame when a game rotates off the live
panel.

**Valid.** `final_result_status='OK'`, `final_total=201`, `trigger_total=205.5`
→ UNDER.

**Invalid.** `game_results.final_result_status` ∈ {`UNKNOWN`,
`NEEDS_RECONCILIATION`, `INVALID`} or `final_total IS NULL` → NOT a final → stay
PENDING.

**Edge.** An OK row's provenance matters: `result_source='RESULTS_PAGE'` is
immutable; a snapshot-derived OK row may be legitimately corrected in place
(§Rule 1.2).

**Forbidden.** Inferring a final from `games.status='ended'` or from the last
snapshot's score. `games.status='ended'` is a *state flag*, not a result.

**Verify.** `SELECT source_game_id, final_result_status, final_total,
result_source FROM game_results WHERE source_game_id=?`.

---

## Rule 1.2 — A `RESULTS_PAGE` OK row is VERIFIED and IMMUTABLE

**Rule.** A `game_results` row with `result_source='RESULTS_PAGE'` AND
`final_result_status='OK'` is a page-verified final. It is immutable against
snapshot-derived re-derivation, always. A disagreeing captured endpoint is FLAGGED
in `result_conflicts`, never overwritten.

**Source.** `blm_v4/result_policy.py::is_results_page_verdict` /
`is_authoritative_verdict`; `blm_v4/settle_worker.py::_settle_game` (the
`WHERE NOT (result_source='RESULTS_PAGE' AND final_result_status='OK')` guards on
every write).

**Why.** Measured 2026-09-24: the pre-fix writers destroyed 2,592 of 2,907
page-verified finals by writing `final_*=NULL` + `UNKNOWN` while leaving
`result_source='RESULTS_PAGE'` behind — and because the state table still said
VERIFIED, none was ever retried.

**Valid.** Reconciler writes 217/OK/RESULTS_PAGE; snapshots disagree → row keeps
217, a flag is written to `result_conflicts`.

**Invalid.** A settle-worker pass reading `_snapshot_history_quality='INVALID'` and
writing `final_total=NULL` over a RESULTS_PAGE OK row.

**Edge.** A *snapshot-derived* OK row (no `RESULTS_PAGE`) IS rewritable — the
M007-M8 re-verification path corrects it in place. The `!= 'OK'` guard would be
wrong; only the page-verified verdict is immutable.

**Forbidden.** Treating a results-page verdict as "a suggestion" to be re-derived
from snapshots.

**Verify.** `is_results_page_verdict(row)` returns True; the upsert guards
`WHERE NOT (result_source='RESULTS_PAGE' AND final_result_status='OK')`.

---

## Rule 1.3 — The trigger line is anchored to `clean_projections`, not `snapshots`

**Rule.** The frozen trigger line is the last non-null `live_total_line` in
`clean_projections` observed at-or-before the game's 75% progress boundary. The
`snapshots` series is the fallback only when the canonical store has no line by
the boundary.

**Source.** `under_outcome.trigger_observation` + `_projection_line_at`
("CANONICAL TRIGGER-LINE STORE (ruling 2026-09-20)").

**Why.** The 218-flip audit found `snapshots` and `clean_projections` carrying
different lines at the same boundary instant (121 of 179 verified flips);
`clean_projections` was ruled canonical so the alert and the settlement read ONE
value.

**Valid.** `projection_rows` yields a line at/before the boundary → use it.

**Invalid.** `clean_projections` has no line by the boundary → fall back to
`snapshots`; do NOT declare the trigger unprovable while the snapshots prove a
line.

**Edge.** A line first observed AFTER the boundary never leaks in; rows without a
usable progress never extend the line (fail closed).

**Forbidden.** Using the opening line, a later live line, the closing line, or a
reconstructed value as the trigger.

**Verify.** Call `trigger_observation(rows, 75, cls, projection_rows=...)` and
compare to the served `trigger_line` and `by_checkpoint[75].trigger_total` — they
must be identical.

---

## Rule 1.4 — Market/live truth is observed PokerBet data only

**Rule.** Every market line is an OBSERVED PokerBet value. The model never
fabricates a line. Live line = `market_observations` / `snapshots.total_line`
(last non-null); opening (OLV) = first verified line; closing (CLV) = last
verified line at-or-before terminal (None while live).

**Source.** `blm_v4/projection.py::market_snapshot` / `opening_snapshot` /
`closing_snapshot`; `DECISIONS.md` §10.K.

**Why.** A fabricated line would produce a false edge and a false alert.

**Valid.** `market_snapshot` returns the most recent row carrying a total line;
panel ticks without a market payload never count as "no market".

**Invalid.** Treating a stub snapshot (no `total_line`) as "no market" and
dropping the last real line.

**Edge.** `closing_line` is `None` for a live game — the latest live line is NOT
the closing line.

**Forbidden.** Substituting the opening line for a stale/missing live line
(`DECISIONS.md` §14.F).

**Verify.** Trace the line back to a `snapshots` or `market_observations` row.

---

## Rule 1.5 — Live state is NOT settled truth

**Rule.** Display freshness (`live`, `live_reason`, `market_status`) is a
presentation/eligibility concept; it is never evidence a result exists. A
disappearing live market is NOT evidence a game has no result.

**Source.** `blm_v4/api.py::_live_state` / `_alert_gate`;
`result_policy.py` module docstring.

**Why.** The pre-fix "a disagreeing endpoint lifts the protection" logic destroyed
page-verified finals precisely when the game vanished early.

**Valid.** A game with no fresh snapshot but a stored RESULTS_PAGE OK final → the
final stands.

**Invalid.** Marking a game "no result" because its `live` flag is false.

**Forbidden.** Deriving settlement or absence-of-result from a freshness flag.

**Verify.** `game_results` is consulted independently of `live`.

---

## Rule 1.6 — The dashboard / frontend is NOT authoritative

**Rule.** The dashboard renders what the backend serves; it never re-derives the
alert condition, the trigger line or the verdict. If the dashboard and the API
disagree, the API is authoritative.

**Source.** `under_alert.py` module docstring ("the frontend renders the boolean
and the numbers it is built from; it never re-derives them"); `under_outcome.py`.

**Why.** Two surfaces that re-derive the same opportunity can disagree.

**Valid.** Dashboard history rows are localStorage-only — never a source of
truth for fired alerts.

**Invalid.** Asking the dashboard UI "how many alerts fired" as evidence.

**Forbidden.** Using frontend state as the audit source.

**Verify.** Compare the UI to `/api/v4/alert-outcomes` / `_v4_live_uncached`.

---

## Source-resolution table

| Critical value | Authoritative source | NOT authoritative |
|---|---|---|
| Final total | `game_results.final_result_status='OK'` | `games.status`, last snapshot, dashboard |
| Page-verified final | `result_source='RESULTS_PAGE'` + OK | snapshot re-derivation |
| Trigger line | `clean_projections.live_total_line` at/before boundary | opening/live/closing line, snapshots (fallback only) |
| Trigger boundary | `trigger_observation` progress (75% crossing) | the tick the alert became eligible |
| Market line | `market_observations` / `snapshots.total_line` | reconstructed/fair value |
| League average pace | `competition_pace` per `competition_slug` | a global/cross-league rate |
| Settlement verdict | `under_outcome.outcome_status` | frontend colour class |
| Fired-alert win rate | `fingerprint_stats.compute_stats` | a hand-rolled rate |
| Live/eligibility | `api._alert_gate` / `under_alert_eligibility` | UI badge |
