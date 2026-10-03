# ADR-001 — The trigger line freezes at the 75% crossing

Status: ACCEPTED (superseded the snapshot-only rule, 2026-09-20)
Retrieval keywords: trigger line, 75% crossing, freeze, clean_projections, canonical,
trigger_observation, immutable, alert settlement, ADR.

## What we decided

The frozen trigger line for every alert is the market O/U total IN FORCE when the
game reached 75% progress — the last non-null `clean_projections.live_total_line`
observed AT-OR-BEFORE that boundary instant. The `snapshots` series is a fallback
only when the canonical store has no line by the boundary. The line is immutable
once triggered.

## Why

The alert and the settlement must read ONE value, from ONE authority, so the line
on screen and the verdict behind it can never disagree.

## What problem it solved

The 218-flip audit found the `snapshots` series and `clean_projections` carrying
different lines at the same boundary instant (121 of 179 verified flips). Ruling
`clean_projections` canonical re-homed the LINE side of every verdict to one store.

## Alternatives rejected

- Taking the line from the snapshots series alone (the source of the divergence).
- Taking the line at the firing tick (inflated the observed rate by ~14 pp).
- Taking the opening/live/closing line (wrong moment; see `08`).

## Never change without explicit review

- The canonical store (`clean_projections`) and its at-or-before semantics.
- The anchor: the FIRST observation at/after 75% (`trigger_observation`).
- Immutability: a later line can never revise a frozen trigger.
