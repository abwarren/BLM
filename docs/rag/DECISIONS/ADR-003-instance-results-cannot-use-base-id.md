# ADR-003 — An instance result cannot use the base id's score

Status: ACCEPTED
Retrieval keywords: instance identity, #i1, instance split, base id, virtual replay,
impossible final, fixture identity, result_policy, ADR.

## What we decided

A result is accepted only when the render proves the SAME fixture (team block +
start time) AND is consistent with the game's captured history (neither side below a
score already observed). A feed reply for the BASE id of a virtual fixture may be
REJECTED for an `#iN` instance game; a base result is never attributed to one
instance.

## Why

Betual/Cyber virtuals replay the same fixture back-to-back and the SPA silently
keeps the previously rendered scoreboard when the requested game id does not
resolve. Scores never decrease within a fixture.

## What problem it solved

Measured: 739 of 2,907 page-verified results (25.4%) had a stored final total LOWER
than a total already observed live — arithmetically impossible; 270 sat in
`game_results` as OK and inside the performance statistics. The same defect produced
"wrong winner" alerts.

## Alternatives rejected

- Identity from TEAM NAMES only (the source of the defect).
- Trusting a scoreboard without a history-consistency check.
- Attaching a base final to an `#iN` game.

## Never change without explicit review

- The identity gate (fixture proof) before any score is believed.
- The history-consistency bound (final side >= observed max).
- The instance split protection (`instance_splits`; a base reply does not settle an
  `#iN`).
- The fixture-start window lower bound `max(regulation, observed_span)`.
