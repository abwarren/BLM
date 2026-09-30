# Data-quality baseline (BEFORE capture-efficiency PR) — 2026-09-30

Generated: 2026-09-30T17:09:40Z.  READ-ONLY (mode=ro + query_only).  The PR is
NOT deployed; this is the quantified BEFORE the post-deploy re-run
must be compared against.  All windows use ISO-'T' string compares.

## A. Capture quality per day (production DB)

| day | snaps | NULL-score% | NULL-quarter% | line-present% | games w/ market-obs% | ended games | final-window coverage% | post-final snaps (60min) |
|---|---|---|---|---|---|---|---|---|
| 2026-09-24 | 54565 | 56.2% | 77.2% | 82.2% | 99.2% | 1244 | 98.7% | 14 |
| 2026-09-25 | 31532 | 56.3% | 77.2% | 82.5% | 99.1% | 973 | 87.9% | 2 |
| 2026-09-26 | 12641 | 26.7% | 78.8% | 49.6% | 100.0% | 812 | 17.0% | 24 |
| 2026-09-27 | 13421 | 8.2% | 59.1% | 49.5% | 98.7% | 259 | 91.9% | 0 |
| 2026-09-28 | 46370 | 11.7% | 85.9% | 26.5% | 100.0% | 372 | 59.7% | 8 |
| 2026-09-29 | 37743 | 13.8% | 85.9% | 28.7% | 100.0% | 429 | 45.9% | 2 |
| 2026-09-30 | 27584 | 12.8% | 86.3% | 27.3% | 100.0% | 289 | 50.2% | 0 |

_Notes:_ NULL-quarter% is a board-context proxy (the list page renders
 quarter blank on degenerate rows).  final-window coverage = ended
 games having >=1 snapshot in their last 10 min before result_at.
 post-final snaps = capture WASTE the PR's DONE-lifecycle eliminates
 (WS-bridge frames after result_at).  Journal tick percentiles are
 reported separately below (not reconstructible per day from the DB).

## B. Collector tick cadence (journal --user, last 6h, current PID)

- n=2095 ticks: P50=2.47s P90=7.44s P95=9.54s P99=16.30s MAX=54.94s
- ticks whose TOTAL exceeded the 3.0s page-capture budget: 883 (42.1%)

## C. Reconciliation activity (last 7 days)

- reconciliation columns: ['id', 'source', 'source_game_id', 'classification', 'bc_event_id', 'bc_event_name', 'bc_competition_id', 'bc_url', 'checked_at', 'checks_json', 'result']
- no usable timestamp column; reconciliation rows total: 0

## D. Does capture health correlate with accuracy?  (trigger level,
## first->=75% cohort vs OK finals; alert math untouched)

REQUIRED_MARGIN = 1.04 (imported).  Triggers: 19355, settled: 14614, unsettled: 4741.

| market_status | n | UNDER% |
|---|---|---|
| LIVE | 12368 | 88.1% |
| STALE | 2246 | 79.3% |

| market age | n | UNDER% |
|---|---|---|
| <30s | 10239 | 88.7% |
| 30-120s | 1337 | 87.5% |
| >120s | 3038 | 79.7% |

_Read:_ at the 75% boundary, fresher captures (LIVE status, younger
 market age) showing HIGHER UNDER% means accuracy tracks capture
 quality — i.e. improving capture efficiency directly improves the
 data feeding the (unchanged) alert.  This refines the Phase 7
 finding (LIVE>STALE within every competition by 7-11pp).
