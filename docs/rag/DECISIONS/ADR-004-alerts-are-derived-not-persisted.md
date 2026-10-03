# ADR-004 — Alerts are derived, not persisted

Status: ACCEPTED
Retrieval keywords: derived alert, no alert row, alerts table empty, computed on the fly,
under_alert_outcome, alert-outcomes, PENDING, ADR.

## What we decided

A v4 alert has NO row anywhere. The legacy `alerts` tables (`blm.db`, `blm_v2.db`)
are EMPTY. Every alert's verdict is computed on the fly from `snapshots` /
`clean_projections` + the settled `game_results` row, via
`under_outcome.under_alert_outcome`, and served through `/live` and
`/api/v4/alert-outcomes`.

## Why

Deriving the verdict from stored primitives guarantees the number on screen and the
verdict behind it come from one formula, and removes a whole class of drift between
a stored alert row and the data it summarises.

## What problem it solved

It reframes every "stuck PENDING" task: there is no "mark the alert resolved"
operation. A PENDING row is ALWAYS a missing FINAL; the only repair is to recover the
final, after which the derived alert settles itself.

## Alternatives rejected

- Persisting alert rows and repairing them.
- A second alert store or a "resolution" write path.

## Never change without explicit review

- The derived-alert model (no alert row to repair).
- The translation rule: "un-PENDING" means "recover the missing finals".
