# 02 — Alert Lifecycle

Retrieval keywords: alert lifecycle, trigger, freeze, settlement, frozen trigger line,
75% crossing, progress_75, Q3_BREAK, target, under alert, derived alert, pending,
under over push, state machine, transition, valid transition, invalid transition.

**What this pack answers:** what a BLM alert is, how it comes into existence, how it
freezes, and how it settles — plus which transitions are valid and which are not.

---

## The lifecycle

```
GAME DISCOVERED
   ↓  (collector panel/WS discovery → games row)
SNAPSHOTS  (append-only, source_game_id identity)
   ↓
MARKET OBSERVATION  (event-view and/or WS → snapshots.total_line / market_observations)
   ↓
75% PROGRESS CROSSING   (progress_pct >= 75.0)
   ↓
TRIGGER OBSERVATION   (trigger_observation: first observation at/after 75%)
   ↓
FROZEN TRIGGER LINE   (clean_projections.live_total_line at/before boundary; immutable)
   ↓
UNDER ALERT ACTIVE   (progress>=75 AND required > league_avg*1.04 AND eligible)
   ↓
GAME ENDS   (terminal observation captured and/or status='ended')
   ↓
RESULT RECONCILIATION   (result_reconciler → RESULTS_PAGE verdict)
   ↓
AUTHORITATIVE FINAL   (game_results.final_result_status='OK')
   ↓
SETTLEMENT   (under_outcome.outcome_status: final vs trigger line)
   ↓
UNDER / OVER / PUSH
```

---

## Rule 2.1 — There is no persisted alert row

**Rule.** A v4 alert has NO row anywhere. The legacy `alerts` tables (`blm.db`,
`blm_v2.db`) are EMPTY. Every verdict is computed on the fly from
`snapshots`/`clean_projections` + the settled `game_results` row, via
`under_outcome.under_alert_outcome`, served through `/live` and
`/api/v4/alert-outcomes`.

**Source.** `under_outcome.py`; live `alerts` tables empty; `docs/rag/` ADR-004.

**Why.** Reframes every "stuck PENDING" task: there is no "mark the alert resolved"
operation. The only repair is to recover the final.

**Valid.** "Un-PENDING the alerts" is translated to "recover the missing finals".

**Invalid.** Searching for an `alerts` table and updating a row.

**Edge.** A `RESULT PENDING / PENDING` row is ALWAYS a missing final.

**Forbidden.** Creating an alert-row table to "fix" it.

**Verify.** Check for a real `alerts` table before believing one exists; the live
tables are empty.

---

## Rule 2.2 — The alert condition (the whole rule)

**Rule.**

```
active = eligible is True
     AND progress_pct >= 75.0                              (ALERT_PROGRESS_PCT)
     AND required_pts_per_min > league_average_pace * 1.04  (REQUIRED_MARGIN, STRICT)
```

`actual_pace` is reported but takes no part. No 50% tier; no `actual < league`
leg. Any missing/non-finite operand, or missing `eligible`, → `active=False`.

**Source.** `blm_v4/live_analytics/under_alert.py::under_alert_state`; constants
`ALERT_PROGRESS_PCT=75.0`, `REQUIRED_MARGIN=1.04`, `CHECKPOINTS=(25,50,75)`.

**Why.** This exact parameter set produced the ~69.89% historical UNDER cohort;
the 50% tier was a coin flip (48.51%) and was removed.

**Valid.** progress=0.83, required=1.30, league=1.20 → 1.30 > 1.248 → active.

**Invalid.** required exactly `league*1.04` → NOT active (strict, not `>=`).

**Edge.** Missing league reference → `active=False`; never borrow another
competition's rate.

**Forbidden.** Re-deriving the condition in the frontend; adding an absolute
pts/min offset (`DECISIONS.md` §14.D).

**Verify.** Call `under_alert_state(...)`; it equals the served `under_alert.active`.

---

## Rule 2.3 — The trigger boundary is the 75% CROSSING, not the firing tick

**Rule.** The frozen trigger line is anchored at the FIRST observation the game
reached >=75% progress, from `trigger_observation(rows, 75, cls, projection_rows)`.
It is NOT the later tick at which the alert became eligible.

**Source.** `under_outcome.trigger_observation`; skill pitfall
(under-alert-window-audit): a hand-rolled firing-tick line inflated the observed
48h rate by ~14 pp (77.8% vs the correct 64.2%).

**Why.** The alert can become eligible a tick or two after the crossing (market
goes LIVE, state freshens); the frozen line must be the one in force AT the
crossing.

**Valid.** Take `total_line` from `trigger_observation`, its `progress` >= 0.75.

**Invalid.** Taking `clean_projections.live_total_line` at the firing row.

**Edge.** A line first observed after the boundary never leaks into the frozen
line.

**Forbidden.** Re-anchoring the trigger to the current/live line.

**Verify.** Compare the firing-row line to `trigger_observation` — they may differ.

---

## Rule 2.4 — Once triggered, the trigger line is immutable

**Rule.** Once an alert's checkpoint is reached, the trigger line never moves when
the market moves later. A later line can never revise it.

**Source.** `under_alert.py` (frozen `trigger_line`/`trigger_progress`/
`trigger_captured_at`); `DECISIONS.md` §8.F.

**Valid.** line frozen at 205.5; market later moves to 199.5; the alert still
settles vs 205.5.

**Invalid.** Settling against the closing line 199.5.

**Edge.** The Q3_BREAK checkpoint reuses the SAME boundary line as the 75%
checkpoint (one authority, one line, one verdict) under its own string identity.

**Forbidden.** Rebasing a frozen trigger from a later model build.

**Verify.** `by_checkpoint[cp].trigger_total` equals the live alert's
`trigger_line`.

---

## Rule 2.5 — Q3_BREAK is additive, never a replacement

**Rule.** `Q3_BREAK` is an additional checkpoint identity (the STRING
`"Q3_BREAK"`, deliberately not in the numeric `CHECKPOINTS` tuple), at exactly
75.0% progress (3 of 4 regulation quarters). Same condition, break geometry:
`remaining_minutes = quarter_minutes`;
`required = (triggered_line - score_at_trigger)/quarter_minutes`;
`active = eligible AND required > league_average_pace*1.04`.

**Source.** `under_alert.py::q3_break_snapshot`, `Q3_BREAK_PROGRESS=75.0`,
`Q3_BREAK_CHECKPOINT="Q3_BREAK"`.

**Why.** A separate checkpoint identity lets the Q3/Q4 break be analysed without
colliding with the numeric 25/50/75 checkpoints.

**Valid.** A Q3_BREAK row appears in `by_checkpoint["Q3_BREAK"]`.

**Invalid.** Treating Q3_BREAK as replacing the numeric 75 checkpoint.

**Edge.** `quarter_minutes` is classification-specific (10 BETUAL_NBA, 12
CYBER_2K26) via `duration_for` — never hardcoded/shared.

**Forbidden.** Hardcoding a quarter length.

**Verify.** `q3_break_snapshot(...)` active flag; `by_checkpoint` keys.

---

## Valid vs invalid transitions

| From | To | Valid? | Note |
|---|---|---|---|
| 75% crossing | FROZEN TRIGGER LINE | ✅ | line = last at-or-before the boundary |
| 75% crossing | UNDER ALERT ACTIVE | ✅ if eligible + required>league*1.04 | else no alert |
| UNDER ALERT ACTIVE | SETTLED (under/over/push) | ✅ | needs an authoritative final |
| GAME ENDS | SETTLED | ❌ without an OK final | `ended` ≠ result |
| ENDED + INVALID result | VALID FINAL | ❌ | INVALID is not a final |
| missing final | SETTLED | ❌ | stays PENDING before an OK row exists |
| base game OK final | base result | ✅ | base identity is the alert identity |
| base OK final | `#iN` instance final | ❌ | DO NOT attach a base final to `#iN` |
| `#iN` instance INVALID | fall back to base OK | ❌ | instance identity is protected |

**The load-bearing distinction:** `ENDED + INVALID result ≠ VALID FINAL`. An
`ended` status with `final_result_status ∈ {UNKNOWN, NEEDS_RECONCILIATION,
INVALID}` is NOT a settled alert.

---

## Rule 2.6 — Colour is a consequence, not a first act

**Rule.** A result is coloured only when BOTH a provable FINAL and a provable
LINE exist, then compared: final<trigger → GREEN (UNDER); final>trigger → RED
(OVER); equal → PUSH (neutral); either missing → NO COLOUR (fail closed).

**Source.** `under_outcome.outcome_status`; `DECISIONS.md` §13.F.

**Why.** Missing evidence must never be rendered as a verdict.

**Valid.** final 217, line 199.5 → OVER (RED).

**Invalid.** Leaving no colour and reporting it as a bug — no colour is correct
when the final is missing.

**Edge.** A blank status on a closed row can be a legitimate NO-LINE row (trigger
line unprovable at the boundary) — correct by design, never force-resolved.

**Forbidden.** Force-colouring a NO-LINE row.

**Verify.** `outcome_status(trigger, final)` is `None` iff either operand is
`None`.
