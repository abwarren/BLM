# ADR-002 — The results page is authoritative; a page-verified final is immutable

Status: ACCEPTED (2026-09-24)
Retrieval keywords: results page, authoritative, RESULTS_PAGE, immutable, clobber,
result_conflicts, is_results_page_verdict, settlement, ADR.

## What we decided

A `game_results` row with `result_source='RESULTS_PAGE'` AND
`final_result_status='OK'` is a page-verified final. It is immutable against
snapshot-derived re-derivation: a disagreeing captured endpoint is FLAGGED in
`result_conflicts`, never overwritten. The predicate is
`result_policy.is_results_page_verdict` and the guard is re-evaluated ATOMICALLY at
write time.

## Why

A disappearing live market is NOT evidence that a game has no result. The results
feed is the authoritative fallback; the snapshots are the very reason the game
needed reconciliation.

## What problem it solved

The pre-fix writers treated a page verdict as "a suggestion" and, when the captured
endpoint disagreed, wrote `final_*=NULL` + `UNKNOWN` while leaving
`result_source='RESULTS_PAGE'` behind. Because the state table still said VERIFIED,
the games were never retried: 2,592 of 2,907 page-verified games ended with NO OK
row.

## Alternatives rejected

- "A disagreeing endpoint lifts the protection" (the clobber).
- A plain `!= 'OK'` guard (wrong: a SNAPSHOT-derived OK row is legitimately
  corrected in place by the M007-M8 re-verification).
- Deleting a flagged conflict automatically (a human closes it).

## Never change without explicit review

- The immutability of a RESULTS_PAGE + OK row.
- The write-time (not read-time) atomic guard.
- The conflict-flag behaviour (flag, never overwrite).
