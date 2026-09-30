# LIVE vs STALE UNDER% WITHIN each competition — 2026-09-30

Directive 2026-09-30 PHASE 7 (read-only; alert algorithm untouched).
Trigger cohort: validated first->=75% boundary reconstruction (same as c2_forensic_backtest_2026-09-23). REQUIRED_MARGIN = 1.04 (IMPORTED). UNDER = final_total < line * margin.
Settled triggers analysed: **14570** (unsettled/missing-final triggers excluded).

## BY COMPETITION (LIVE vs STALE WITHIN each)

| bucket | n | UNDER% | LIVE n | LIVE UNDER% | STALE n | STALE UNDER% | MISSING n | MISSING UNDER% |
|---|---|---|---|---|---|---|---|---|
| betual-cba | 2038 | 87.2% | 1689 | 88.6% | 349 | 80.8% | 0 | n/a |
| betual-euroleague | 3125 | 86.8% | 2766 | 87.7% | 359 | 79.1% | 0 | n/a |
| betual-kbl | 1561 | 82.5% | 1264 | 84.7% | 297 | 73.4% | 0 | n/a |
| betual-nba | 4653 | 89.2% | 3867 | 90.7% | 786 | 81.8% | 0 | n/a |
| betual-tbsl | 2588 | 84.8% | 2167 | 86.1% | 421 | 77.9% | 0 | n/a |
| cyber-basketball-2k26-matches | 605 | 86.1% | 571 | 86.5% | 34 | 79.4% | 0 | n/a |

## BY CHECKPOINT x market status

| bucket | n | UNDER% | LIVE n | LIVE UNDER% | STALE n | STALE UNDER% | MISSING n | MISSING UNDER% |
|---|---|---|---|---|---|---|---|---|
| 75-80 | 13139 | 86.1% | 10945 | 87.5% | 2194 | 79.1% | 0 | n/a |
| 80-90 | 1078 | 92.0% | 1054 | 91.8% | 24 | 100.0% | 0 | n/a |
| 90-100 | 353 | 95.8% | 325 | 96.9% | 28 | 82.1% | 0 | n/a |

## BY SAMPLING GAP x market status

| bucket | n | UNDER% | LIVE n | LIVE UNDER% | STALE n | STALE UNDER% | MISSING n | MISSING UNDER% |
|---|---|---|---|---|---|---|---|---|
| n/a | 918 | 91.3% | 903 | 91.5% | 15 | 80.0% | 0 | n/a |
| <10s | 778 | 85.1% | 740 | 85.5% | 38 | 76.3% | 0 | n/a |
| 10-30s | 3808 | 85.2% | 3159 | 86.0% | 649 | 81.4% | 0 | n/a |
| 30-60s | 5304 | 85.1% | 4218 | 87.2% | 1086 | 77.1% | 0 | n/a |
| >60s | 3762 | 90.0% | 3304 | 91.0% | 458 | 82.1% | 0 | n/a |

## BY ISO WEEK x market status

| bucket | n | UNDER% | LIVE n | LIVE UNDER% | STALE n | STALE UNDER% | MISSING n | MISSING UNDER% |
|---|---|---|---|---|---|---|---|---|
| 2026-W36 | 697 | 79.2% | 373 | 83.1% | 324 | 74.7% | 0 | n/a |
| 2026-W37 | 5342 | 84.2% | 3466 | 86.5% | 1876 | 79.9% | 0 | n/a |
| 2026-W38 | 4554 | 87.3% | 4538 | 87.3% | 16 | 93.8% | 0 | n/a |
| 2026-W39 | 3665 | 91.3% | 3645 | 91.3% | 20 | 85.0% | 0 | n/a |
| 2026-W40 | 312 | 86.9% | 302 | 86.8% | 10 | 90.0% | 0 | n/a |

## Headline: LIVE vs STALE WITHIN competition

| competition | LIVE UNDER% | STALE UNDER% | delta (STALE-LIVE) | LIVE n | STALE n |
|---|---|---|---|---|---|
| betual-cba | 88.6% | 80.8% | -7.8pp | 1689 | 349 |
| betual-euroleague | 87.7% | 79.1% | -8.6pp | 2766 | 359 |
| betual-kbl | 84.7% | 73.4% | -11.3pp | 1264 | 297 |
| betual-nba | 90.7% | 81.8% | -8.9pp | 3867 | 786 |
| betual-tbsl | 86.1% | 77.9% | -8.2pp | 2167 | 421 |
| cyber-basketball-2k26-matches | 86.5% | 79.4% | -7.1pp | 571 | 34 |

_Read:_ the aggregate STALE>LIVE gradient must be judged WITHIN each competition; a positive delta in a competition with healthy n means staleness is NOT neutral there. n is small per cell — treat differences under ~10 triggers as noise.
