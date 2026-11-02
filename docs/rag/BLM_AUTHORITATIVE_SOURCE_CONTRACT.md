# BLM Authoritative Source & Settlement Contract

**Document type:** RAG / agent knowledge  
**Scope:** Production BLM UNDER alerts, trigger lines, reconciliation, and settlement  
**Status:** Canonical operational contract  
**Last validated:** 2026-10-03  
**Repository:** `abwarren/BLM`

## Purpose

This document defines exactly which source is authoritative for each part of the BLM UNDER-alert lifecycle.

Agents MUST retrieve and follow this contract before performing alert reconciliation, result settlement, win-rate audits, or result-source conflict resolution.

## 1. Authority hierarchy

### Final game result

The authoritative upstream source for the final game result is the **RESULTS_PAGE** result.

The authoritative persisted BLM settlement record is:

`blm_pokerbet.db.game_results`

A final is authoritative for settlement only when:

`final_result_status = 'OK'`

The authoritative final total is:

`game_results.final_total`

The authoritative final score fields are:

- `final_home`
- `final_away`
- `final_total`
- `result_at`
- `final_result_status`

The settlement chain is:

```
RESULTS_PAGE
    ↓
result reconciliation
    ↓
game_results (final_result_status = OK)
    ↓
final_total
    ↓
under_alert_outcome()
```

### Trigger line

The settlement line is NOT the current live line, opening line, closing line, or model fair value.

The immutable trigger line is produced by:

```
trigger_observation()
    ↓
clean_projections
    ↓
line at the 75% progress crossing
    ↓
trigger_total
```

This is the line that MUST be used for settlement.

Important: the frozen line is anchored at the **75% progress crossing**, not necessarily the later observation at which the alert becomes visibly eligible. If the market is stale between those points, the two lines can differ. The 75% crossing line remains authoritative.

## 2. Settlement rule

The authoritative settlement function is:

`under_alert_outcome()`

Settlement MUST compare:

```
authoritative final_total
        VS
immutable trigger_total
```

Classification:

- `final_total < trigger_total` → **UNDER**
- `final_total > trigger_total` → **OVER**
- `final_total == trigger_total` → **PUSH**

If an authoritative final cannot be proven, the result MUST remain:

**PENDING**

Never infer an UNDER or OVER result from an incomplete final.

## 3. Source precedence

Use this precedence when sources disagree:

1. **RESULTS_PAGE / reconciled game result** — authoritative final result source.
2. **game_results row with final_result_status = OK** — authoritative persisted final used by settlement.
3. **trigger_observation() + clean_projections** — authoritative immutable trigger line.
4. **under_alert_outcome()** — authoritative classification logic.
5. **Terminal observation** — fallback/provisional evidence only where the existing implementation explicitly permits it.
6. **Raw DOM/history capture** — evidence only; MUST NOT override an accepted authoritative result.
7. **Dashboard localStorage / UI history** — NOT authoritative and MUST NOT be treated as a production alert ledger.

## 4. Fail-closed rule

If `game_results.final_result_status` is not `OK`, and no permitted terminal observation proves the final under the current implementation, settlement MUST return PENDING.

Examples of non-settleable states include:

- `INVALID`
- `NEEDS_RECONCILIATION`
- missing `final_total`
- ambiguous virtual-instance identity
- a base-game result that cannot be safely attached to a `#i1` instance

A game being marked `ended` is NOT by itself proof of a valid final.

## 5. Virtual-instance identity

For virtual-instance games such as `source_game_id#i1`:

- Do NOT attach the base-game result to the instance merely because the names appear similar.
- The final must be proven for the exact instance identity.
- If only the base ID has an OK result and the instance ID does not, the instance alert remains PENDING unless the existing reconciliation rules explicitly establish the mapping.
- This protects against settling one virtual game using another game's score.

## 6. Alerts are derived, not persisted

The production v4 alert is derived at read time from production data and implementation logic.

Do NOT treat `blm.db.alerts` as the canonical v4 alert ledger.

Do NOT treat browser localStorage alert history as authoritative.

For audits, reconstruct alerts using the production alert logic and the underlying production databases.

## 7. Production UNDER trigger

The canonical production UNDER trigger is:

```
progress_pct >= 75
AND
required_pts_per_min > league_avg_pace * 1.04
```

The comparison is **strictly greater than** `league_avg_pace * 1.04`.

Other eligibility requirements must also be satisfied by the live implementation, including live-market/game eligibility and the minimum remaining-time condition.

There is no production UNDER alert at 25% or 50% when `ALERT_PROGRESS_PCT = 75.0`.

## 8. Conflict handling

If captured DOM/history totals conflict with the authoritative RESULTS_PAGE result:

- flag the conflict;
- preserve the authoritative RESULTS_PAGE/game_results OK verdict;
- do NOT replace the accepted final merely because another capture disagrees;
- report the conflict in audits;
- never silently rewrite historical settlement.

The authoritative result source wins unless an explicit reconciliation process establishes a correction.

## 9. Audit requirements

Every settlement audit should be able to identify:

- source game ID / exact instance ID;
- checkpoint;
- trigger timestamp;
- immutable trigger line;
- score/progress at trigger;
- final home score;
- final away score;
- final total;
- final result status;
- result source;
- final result timestamp;
- settlement classification;
- whether the final is authoritative or provisional.

For a production win-rate calculation, distinguish:

- **authoritative settled results** — `game_results.status = OK`;
- **provisional observation settlements** — terminal observation fallback;
- **pending** — no provable final.

Do not silently combine these categories.

## 10. Hard prohibitions

Agents MUST NOT:

- settle against the current live line;
- settle against the opening line;
- settle against the closing line;
- settle against BLM fair value;
- use a later market tick instead of the frozen trigger line;
- infer a final from an `ended` game status alone;
- use a base-game result for a virtual instance without identity proof;
- allow raw DOM history to override an authoritative RESULTS_PAGE/game_results OK result;
- manufacture a result to eliminate a PENDING state;
- modify historical trigger lines during reconciliation.

## 11. Canonical mental model

Use this model for every UNDER alert:

```
LIVE GAME
   ↓
75% PROGRESS CROSSING
   ↓
trigger_observation()
   ↓
IMMUTABLE TRIGGER LINE
   ↓
UNDER ALERT
   ↓
GAME FINISHES
   ↓
RESULTS_PAGE
   ↓
RECONCILIATION
   ↓
game_results.status = OK
   ↓
AUTHORITATIVE FINAL TOTAL
   ↓
under_alert_outcome()
   ↓
UNDER / OVER / PUSH

If authoritative final is unavailable:
   ↓
PENDING
```

## Retrieval keywords

Use these terms to retrieve this contract:

- authoritative source
- authoritative final
- RESULTS_PAGE
- game_results
- final_result_status
- final_total
- trigger line
- frozen trigger line
- trigger_total
- trigger_observation
- clean_projections
- 75% progress crossing
- under_alert_outcome
- settlement
- reconciliation
- result conflict
- terminal observation
- virtual instance
- #i1
- pending
- fail closed
- UNDER
- OVER
- PUSH
