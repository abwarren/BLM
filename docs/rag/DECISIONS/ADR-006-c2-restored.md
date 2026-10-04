# ADR-006 — C2 restored to the live fingerprint layer (non-gating)

Status: ACCEPTED (2026-10-04)
Retrieval keywords: C2 restored, fingerprint, non-gating, recent deceleration,
under_fingerprints, ADR, supersedes ADR-005.

## What we decided

C2 (`recent_pace_3m - actual_pts_per_min <= -0.5`, recent deceleration) is
RESTORED to the live historical UNDER fingerprint layer. `FINGERPRINT_KEYS` is now
EXACTLY `("C1", "C2", "C3", "C5", "R2")` (five keys).

C2 is a RECORDED, NON-GATING fingerprint: it gates nothing and creates no alert.
The production trigger is UNCHANGED::

    eligible AND progress_pct >= 75 AND required_pts_per_min > league_average_pace * 1.04

C4 and C6 (C2's composites) remain RETIRED; R1 remains EXCLUDED.

## Why

- An A/B on the frozen historical cohort (2026-10-04) measured C2 TRUE at 67.23%
  UNDER (N=656, Wilson [63.5, 70.7]) and its UNIQUE pocket (C2-only) at 58.01%
  (N=281, +0.073u/bet, Wilson lower bound 52.2% — below break-even 54.05%).
  C2 shows POSITIVE OBSERVED EV while its incremental pocket is not yet
  statistically separated; the operator decision is to accumulate more evidence.
- Restoring C2 to the NON-GATING layer is production-neutral (zero alert change)
  and makes C2 recorded/queryable on every live UNDER evaluation.
- SEMANTIC NOTE: C2 is a market-AGNOSTIC momentum measure (it reads neither the
  frozen line nor the required pace). It is NOT a market-relative condition —
  recorded here so it is not mistaken for one.

## What problem it solved

Removes the ambiguity of ADR-005 ("C2 retired") now that the operator has decided
to observe C2 prospectively, and restores live recording of C2.

## Alternatives rejected

- Keeping C2 retired (ADR-005): rejected — positive observed EV + the decision to
  observe prospectively.
- Making C2 a gate (Locus B: gate the alert; Locus C: gate the executor): rejected
  — both would change the trigger rule / arm Auto-Bet and are unauthorised; out of
  scope for this ADR.

## Never change without explicit review

- `FINGERPRINT_KEYS == ("C1","C2","C3","C5","R2")`.
- The retirement of C4/C6 and the exclusion of R1 (enforced by test).
- The context-only role: fingerprints gate nothing and create no alert.

## Scope limits (this ADR)

- No gate, no alert-behaviour change, no settlement/threshold change, no Auto-Bet.
- Deployment / restart / commit are SEPARATE authorisations.
