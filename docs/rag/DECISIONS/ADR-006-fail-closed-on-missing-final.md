# ADR-006 — Fail closed on a missing final (PENDING, never a guess)

Status: ACCEPTED
Retrieval keywords: fail closed, missing final, PENDING, no colour, outcome_status,
never fabricate, NO-LINE, ADR.

## What we decided

`outcome_status(trigger, final)` returns `None` if EITHER operand is `None`. An alert
whose final is unprovable stays PENDING; no colour is rendered. A missing input is
never replaced by a guess. The NO-LINE family (trigger line unprovable at the
boundary) is correct by design and is never force-resolved.

## Why

A fabricated final settles a bet; an invented colour creates a false result. Missing
evidence must be rendered as missing, not as a verdict.

## What problem it solved

It eliminated an entire class of false settlements (the "guess the final from the
last snapshot" behavior) and made "no colour" a correct-by-design outcome rather than
a bug.

## Alternatives rejected

- Inferring a final from `games.status='ended'`.
- Inferring a final from the last snapshot when no authoritative row exists.
- Force-resolving NO-LINE rows.
- Colour-by-default.

## Never change without explicit review

- The `None`-if-either-operand-is-`None` rule.
- The `authoritative` flag (`final_source=='settled'` only) that gates the controlled
  correction path.
- The NO-LINE family staying unresolved by design.
