# Auto-bet execution window, price floor, and the threshold question

**Date:** 2026-10-08 · **Author:** prior agent session · **Status:** DECISION PENDING — nothing relaxed

Read this before touching the UNDER execution window or the alert threshold.
It records what is deployed, how the numbers were produced, the measurement
traps that produced **wrong** answers first, and the recommendation on the
open question.

---

## 1. The open question

Today 45.5% of alerts first fire at ≥92% progress, i.e. after PokerBet has
pulled the game total. PokerBet closes at ~2.5-3 minutes remaining and **every
other book closes similarly**, so switching books is not a fix. The only lever
is to act earlier.

The candidate: relax the alert's pace-ratio bar from `> 1.04` to `~0.95` **for
trading only**. Measured effect is good (section 5) but the evidence is
archive-only. **Recommendation: sweep → shadow → arm. Do not arm directly.**

---

## 2. What is LIVE right now (verified 2026-10-08 06:22:55 UTC)

```
blm_v4/trade_window.py
  EXEC_MIN_PROGRESS_PCT = 75.0          # the ALERT's own floor
  EXEC_MAX_PROGRESS_PCT = 92.0          # top of the last fully-quoted band
  MIN_PRICE_BANDS = ((80.0, 1.51), (85.0, 1.37), (None, 1.28))

blm_v4/live_analytics/under_alert.py:63
  REQUIRED_MARGIN = 1.04                # UNTOUCHED — still strict

blm-server: pid 374374, started 06:22:55 UTC, port 2262, healthz 200
branch: fix/price-mapping-2026-10-06
commits: bb36979 (85-92 window) -> 1bea545 (final-4-min ceiling) ->
         87f37ed (floor 75) -> ab1b12f (ceiling 92 + price floor)
tests: 223 passed (tests/test_execution_window_2026_10_07.py + 4 betting suites)
```

Verify the live band (requires an authenticated dashboard tab on CDP :9222):

```
/usr/bin/python3 ~/.hermes/cache/scratch/verify_window_live.py
# expect: BETUAL_NBA | min=75 max=92, 100/100 games with the block
```

Source of truth is `blm_v4/trade_window.py` alone. Three consumers — the
executor gate (what is TRADED), the API payload block, the dashboard BETTABLE
badge (what is SHOWN). A second copy of the numbers is the failure the module
exists to prevent; a test asserts the executor declares none of them.

**Known divergence to be aware of:** an ABSENT price (`None`) is deliberately
NOT gated by the price floor. Line and price come from the same market row, so
absence is the existing `market_missing` concern; gating it broke 67 existing
tests without making any trade safer. Measured live: 0 of 17 in-window games
lacked a price, and all 40 price-less games were out-of-window (closed market).
A price that is PRESENT and below the band's break-even IS refused.

---

## 3. The validated construction (do not re-invent)

Per game, the FIRST observation by time satisfying:

```
progress >= 75  AND  round((line - total) / remaining, 4) > league_avg_pace * 1.04
```

then the settled final, and whether the final total is BELOW the total line
quoted at that moment. Break-even price = 1 / hit_rate.

Validation: the same code reproduces the platform's own served cohort
(archive n=1,540 / 64.42% vs served n=1,528 / 64.46%; the 12-game delta is
games settled after the payload snapshot).

---

## 4. THREE MEASUREMENT TRAPS — each produced a wrong number first

### 4.1 MatchTotal is a LADDER, not a price

One capture quotes several lines with different prices. Real example, one
capture in game 113398:

```
line 163.5 -> under 2.25
line 165.5 -> under 1.85
line 167.5 -> under 1.57
```

Matching a price by TIME PROXIMITY ALONE prices the bet at the WRONG LINE.
Match on the line being bet: among captures in window take the nearest in
time, then within it the row whose `line_value` is closest to the entry's
`live_total_line`. **Validate by reporting the median
`abs(matched_line - entry_line)` — it must be 0.00.** If it is not, the EV
numbers are fiction.

### 4.2 Never let a price window reach past the entry moment

Only captures AT OR BEFORE entry are legitimate; a `±180s` window leaks future
information into the price. The drift down the chain IS the artifact:

```
time-only, ±180s        baseline 75-80 EV +8.7%
line-matched, ±180s     baseline 75-80 EV +6.0%
line-matched, <=entry   baseline 75-80 EV +5.4%   <- report this one
```

Always run the strict variant. It turned out not to change the conclusion, but
it must be run to know that.

### 4.3 Never trust a blended "baseline is +EV"

A blended baseline reads +3% to +12% EV at EVERY band and looks like free
money. It is not harvestable — you only learn a game's population after the
fact. Split it:

```
NEVER-ALERTED   46.6% - 50.0%   BE 2.00 - 2.15   EV -5% to -10%
ALERTED         68.0% - 79.2%   BE 1.26 - 1.47   EV +29% to +50%
```

Never-alerted games lose at every band. **The alert IS the edge**, not a
filter on top of a soft market. Any claim that the market itself is beatable
must survive this split first.

---

## 5. Findings

### 5.1 First fire by band (n=7,631 games with a detectable fire)

| band | n | hit% | break-even |
|---|---|---|---|
| 75-80 | 2,389 | 66.39% | 1.506 |
| 80-85 | 645 | 73.33% | 1.364 |
| 85-90 | 574 | 78.40% | 1.276 |
| 90-92 | 316 | 78.48% | 1.274 |
| 92-95 | 403 | 83.62% | 1.196 |
| 95-100 | 3,010 | 93.26% | 1.072 |
| **92-100** | **3,413** | **92.12%** | **1.085** |

The late band is the MOST accurate and the LEAST placeable. Accuracy and
reachability are in direct opposition in this structure.

### 5.2 The late cohort entered earlier (HINDSIGHT — upper bound only)

For the 3,476 games whose first fire lands ≥92%:

| entry band | n | hit% | break-even | median price |
|---|---|---|---|---|
| 75-80 | 3,061 | 72.62% | 1.377 | 1.94 |
| 80-85 | 3,129 | 75.46% | 1.325 | 1.90 |
| 85-90 | 3,190 | 78.71% | 1.270 | 1.90 |
| 90-92 | 2,979 | 85.06% | 1.176 | 1.90 |

**This is not a plan.** The cohort is defined by firing late; no
hindsight-free rule selects it at 80%. It is the ceiling on any early-entry
strategy, not a forecast.

### 5.3 The threshold decomposition (the actionable one)

For every game, the first moment (`progress >= 75`) the ratio crosses 0.95, and
separately 1.04:

```
crossing 0.95 : 12,473 games
  cohort A  also cross 1.04 -> would alert anyway, now caught EARLIER   7,638
  cohort B  never cross 1.04 -> newly traded by relaxing                4,835
```

| population | entry | n | hit% | BE | med px | EV@med | fires <92% | median fire |
|---|---|---|---|---|---|---|---|---|
| A | relaxed 0.95 | 7,381 | 76.74% | 1.303 | 1.85 | +42.0% | 73% | 77.5% |
| A | strict 1.04 | 7,381 | 80.21% | 1.247 | 1.82 | +46.0% | 54% | 90.0% |
| B | relaxed 0.95 | 4,479 | 58.14% | 1.720 | 1.82 | +5.8% | 84% | 77.5% |
| ALL | relaxed 0.95 | 11,860 | 69.71% | 1.434 | 1.85 | +29.0% | 77% | 77.5% |
| ALL | strict 1.04 | 7,381 | 80.21% | 1.247 | 1.82 | +46.0% | 54% | 90.0% |

**The real result is the `median fire` column.** The relaxed rule does not just
add volume: it moves the median fire from 90.0% to 77.5% progress, bringing 73%
of the currently-late games inside the market, for a 3.5-point win-rate cost.
That is the actual fix for "fires after the close".

The cost is cohort B: 58.14%, below a 60% bar, and inseparable from A because
"never crosses 1.04" is only knowable afterwards. Blended it still clears 60%
and is +29% EV.

Static ratio buckets at 75-80% (different measurement — ratio WITHIN a band,
not the crossing event), for reference:

```
ratio >= 1.04     65.82%   BE 1.519
ratio 0.95-1.04   59.9%    BE 1.67
ratio <= 0.95     51.27%   BE 1.951   <- loses; never go here
```

### 5.4 Price is half the question

A 60% hit rate is break-even at exactly **1.667**. "Wins more than 60%" is only
+EV because the quoted under price is ~1.82-1.85. At 1.60 a 60% cohort loses
money. Always pair a hit rate with its break-even and the price actually
available.

---

## 6. Recommendation (do not skip to step 3)

1. **Sweep the threshold — read-only, cheap, do this first.** Measure the
   cohort A/B split, hit rate, break-even and fire progress at
   0.90 / 0.95 / 1.00 / 1.02 / 1.04. The question is whether the curve has a
   knee, not whether 0.95 alone looks good. **Two points is not a robust
   parameter.** If nothing is robust, the correct answer is "no robust
   parameter found" and stop.
2. **Shadow it on live data — zero money.** A read-only report that replays
   each newly settled batch against the relaxed rule and records what it WOULD
   have taken, at what price, and how it settled. This gives the
   out-of-sample, prod-scale evidence the archive structurally cannot. No code
   in the trading path changes.
3. **Only if 1 and 2 hold, arm it** — with the price floor re-derived for the
   blended population (BE 1.434, not 1.51) and an explicit decision on badge
   parity.

Never, regardless of outcome:

- Not below 0.95 (static ≤0.95 measured 51.27% and loses).
- Not without a price floor — the floor is what makes "60% is good" true.
- Not silently. If the traded rule and the badge diverge, that is the
  operator's call, not a side effect. Badge parity was built deliberately.

---

## 7. Corrections log — numbers that were WRONG and must not be reused

| superseded claim | correct |
|---|---|
| "56% of alerts fire too late" | **45.5%** (3,466 / 7,617) |
| break-even "1.685" for the window cohort | **~1.49** on the operator's own model |
| "early entry is a coin flip, don't do it" | accuracy-true only for ON-pace games (51.27%); off-pace early entry is +EV |
| "the market itself is +EV at every band" | **composition artifact** — split by alerted vs never-alerted (4.3) |
| the time-only price-matched EV table (+8.7%) | superseded by line-matched, at-or-before (+5.4%) (4.1, 4.2) |

---

## 8. Reproduce

Scripts (all read-only) in `~/.hermes/cache/scratch/`:

```
band_rates.py                        per-band first-fire hit rates
first_fire.py                        first-fire distribution by band
late_cohort_early_entry.py           late cohort entered earlier (hindsight)
early_entry_line_matched.py          line-matched, at-or-before prices  <- use this
edge_source_split.py                 alerted vs never-alerted; time split
relaxed_threshold_decomposition.py   cohort A/B split; the actionable table
```

Data (read-only connections, `mode=ro`):

```
/home/ubuntu/BLM/blm_pokerbet.db     games, game_results, market_observations
/home/ubuntu/BLM/blm_metrics_clean.db clean_projections, clean_games,
                                      clean_market_observations
```

**Query cost:** `clean_projections` scans cheaply when filtered
`progress_pct >= 75` (indexed) — ~500k rows, seconds (182M rows total). A
full-table `GROUP BY` over `market_observations` (4.47M rows) TIMES OUT at
180s; probe via `MAX(id)` and bounded `rowid` windows instead.
`clean_market_observations` has no index on `source_game_id` beyond the
autoindex, but MatchTotal is sparse (~33 captures/game), so bulk-load beats
per-game lookups.

**Robustness note:** a time split by `clean_games.first_seen_at` must report
price COVERAGE per half. The older half had 70% coverage and a median matched
price of 2.25 (implausibly deep line) — unreliable. The newer half at 96-99%
coverage reads 59.74% at 75-80%. A time split that ignores coverage is not a
robustness check.

---

## 9. Other open items (not this analysis)

- **Collector finalize worker** — written and tested in `/home/ubuntu/BLM`
  (`blm_v4/collector.py` + `tests/test_finalize_worker_off_fast_path_2026_10_08.py`,
  suites green) but **NOT restarted into the running collector**. ~20s of
  collection gap when applied. The deviation offload IS live (mean tick phase
  1.992s -> 0.864s, phases >5s 9.2% -> 2.8%).
- **`forensic_relative_pace_freeze` drift** — alert cohort UNDER% 62.20% vs
  frozen 69.75% (~7.5pp, outside the ±1 band). Flagged, not investigated.
- **`provider_ref` empty on SUBMITTED rows**; three finals still
  NEEDS_RECONCILIATION.
- **Badge parity** if the trade threshold ever moves.

---

## 10. Operational notes

- Restart is operator-gated: confirm with the operator before restarting
  `blm-server`. A deliberate restart costs ~20-30s of API time; boot-to-first
  data on the collector measured 17s.
- The collector runs from `/home/ubuntu/BLM` (its own repo), NOT the shared
  tree `/home/ubuntu/blm-fix-price-mapping`. Collector bugs live in the former.
- The shared tree carries other agents' uncommitted work. Land additively —
  whole-file copy only for files that differ from it SOLELY by your edits;
  patch others in place. Verify with a diff before copying.
