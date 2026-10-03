# ADR-005 — C2 and its composites are retired

Status: ACCEPTED
Retrieval keywords: C2 retired, retired logic, fingerprint, C4, C6, do not resurrect,
under_fingerprints, R1 excluded, ADR.

## What we decided

The live historical UNDER fingerprint layer is EXACTLY `C1, C3, C5, R2`. **C2 and
its dependent composites C4 and C6 were REMOVED** from the live layer. The legacy
momentum operands (`recent3_pace`, `actual_pace`, `recent3_minus_act`) are accepted
for API/backward compatibility but are deliberately IGNORED. R1 (the widened
required-pace band `[1.10, 1.35)`) is EXCLUDED.

## Why

C2 (`recent3_minus_act <= -0.5`) and its composites were retired. R1 measured BELOW
the historical UNDER baseline (56.47% vs 59.71%), so the C1 upper cut at 1.20
(exclusive) exists precisely because `[1.20, 1.35)` dragged the widened band below
baseline.

## What problem it solved

It prevents an agent reading historical code or docs from restoring C2/C4/C6/R1. The
compatibility operands still exist in the signature, which is exactly why this is a
trap: the code ACCEPTS C2's inputs but must not USE them.

## Alternatives rejected

- Keeping C2/C4/C6 as active conditions.
- Widening C1 to 1.35 (R1).

## Never change without explicit review

- `FINGERPRINT_KEYS == ("C1","C3","C5","R2")` (exactly four).
- The retirement of C2/C4/C6 and the exclusion of R1 (AST-enforced by test).
- The context-only role: fingerprints gate nothing and create no alert.

## Contradiction to be aware of

`DECISIONS.md` §16.K still describes SEVEN fingerprints (C1..C6 + R2) including
C2/C4/C6 with per-fingerprint rates. That record PREDATES the retirement and is
STALE; the current code (four fingerprints) is authoritative. See
`../CONTRADICTIONS.md`.
