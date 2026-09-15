# BLM V4 — Live UNDER Alert: Historical Backtest (2026-09-13 condition)

**Status: SUPERSEDED — this documents a condition that is no longer in
production. Read §0 before using any number here.**

Reproduce with:

```
python3 scripts/backtest_under_alert_settlement_basis_2026-09-13.py
# -> analysis_under_alert_settlement_basis_2026-09-13.txt
```

Read-only: both databases are opened `file:...?mode=ro`; nothing is written
to either. No production code is imported except `under_outcome.
trigger_market_total` (the settlement authority being tested).

---

## 0. Supersession notice

This backtest evaluates the **2026-09-13** live UNDER condition, which was
the production rule when the directive was issued:

```
active = eligible AND actual_pts_per_min < required_pts_per_min
                    AND actual_pts_per_min < league_average_pace
```

Production has since moved on. The condition in
`blm_v4/live_analytics/under_alert.py` (directive 2026-09-14) is now:

```
active = eligible AND progress_pct >= 75
                    AND required_pts_per_min > league_average_pace * 1.04
```

The `actual < league_average` leg is **gone**, a **75% progress floor** was
added, and a **1.04 relative margin** on required pace replaced it. Its
historical rate is documented separately in
`analysis_parity_under_trigger_2026-09-14.txt` (703 obs, **65.43%** UNDER;
the ~69.89% figure is the wider `[65%,85%]` sweep window). Do **not** quote
the numbers below as the current system's rate.

This document is retained because it (a) is an independent reproduction of
the 2026-09-13 cohort that corroborates
`analysis_live_under_alert_backtest_2026-09-13.txt` (57.87%) from a
different implementation, and (b) establishes the **settlement-basis**
result in §5, which applies to *any* UNDER condition and exposed a real
settlement-coverage defect in the live path.

---

## 1. What was tested

Condition source: `blm_v4/live_analytics/under_alert.py`
(`under_alert_state`). `league_average_pace` =
`competition_pace_reference(competition_slug).avg_pace` from
`blm_v4/live_analytics/competition_pace.py` — the mean of
`final_total / regulation_minutes` over that competition's settled games.

Eligibility gates replayed as far as historical data permits (the live
`g.live` status flag cannot be replayed — the `games` table stores only the
final status):

| Gate | Rule applied | Source |
| --- | --- | --- |
| settled game | `game_results.final_result_status = 'OK'` | `scorecard._CM_ELIGIBLE_SQL` |
| quality | `game_quality.status <> 'INVALID'` | `_quality_map` |
| clean epoch | `first_seen_at >= 2026-09-05T05:40:41.782315Z` | `clean_boundary` |
| live market | `clean_projections.market_status = 'LIVE'` (≤300s) | `_alert_gate` |
| market line | `live_total_line` non-null | `project()` |
| game time | `elapsed > 0`, `remaining >= 2.5` min | `ALERT_MIN_REMAINING_MINUTES` |
| league ref | competition present in the reference | `competition_pace.py` |
| checkpoint | `checkpoint_for(progress)` ∈ {25,50,75} | `under_alert.checkpoint_for` |
| non-finite | every operand finite (bools excluded) | `under_alert._finite` |

The `pace_gap > 1.5` dashboard badge is **not** used, and `required > average`
is **not** used as the alert — both are reported only as separate cohorts.

**Checkpoint attribution** follows V4: a row belongs to the highest
checkpoint its progress has reached, so the bands are `[25,50)`, `[50,75)`,
`[75,100)`. This differs from the sibling script, which picks the single
observation closest to each target inside a `±10%` window; the two agree in
aggregate (§7).

League reference reproduced (OK-only), matching the production live values
in `analysis_parity_under_trigger_2026-09-14.txt` exactly:

| League | avg pace (pts/min) | settled games |
| --- | ---: | ---: |
| NBA | 5.6367 | 2,974 |
| KBL | 3.8887 | 1,249 |
| CBA | 4.4582 | 1,257 |
| TBSL | 4.1683 | 2,052 |
| EuroLeague | 4.3009 | 1,959 |
| CYBER 2K26 | 4.4696 | 493 |

Population funnel (observations):

```
clean_projections rows (LIVE market, non-null fields)   468,335
  drop quality INVALID                                    8,238
  drop pre-clean-epoch                                       564
  drop no settled OK final                                63,743
  drop remaining < 2.5 min                                18,817
  drop progress < 25%                                     70,876
  usable                                                  306,097
  -> 16,737 (game,checkpoint) pairings
       ALERT 4,717   BASELINE 12,020   distinct alert games 2,711
```

---

## 2. Settlement basis — the point of this script

An alert's outcome is `final_total` versus *some* market line, and which
line you pick moves the headline. Every alert is settled **twice**:

* **BOARD** — V4's own sealed "triggered line": the value
  `blm_v4.live_analytics.under_outcome.trigger_market_total` returns for the
  checkpoint (the last line observed at-or-before the boundary). This is the
  line the dashboard displays and the line V4 settles against.
* **TRADE** — the live line in force on the **first** observation at which
  the condition became TRUE — the price actually available to a trader.
  Every alert has one by construction, so this basis has full coverage.

The real V4 function is imported and applied to the real `snapshots` rows
V4 itself passes it — never reimplemented. A hand-rolled approximation that
forward-fills from `clean_projections` instead is materially wrong: that
column is far sparser early in a game, which inflates the apparent UNDER
rate.

---

## 3. Results — alert cohort by checkpoint

**BOARD** (vs V4's sealed trigger line):

| Checkpoint | N | UNDER | OVER | PUSH | unprovable | UNDER % |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 25% | 1,202 | 670 | 532 | 0 | 334 | 55.74% |
| 50% | 1,368 | 742 | 626 | 0 | 465 | 54.24% |
| 75% | 1,133 | 706 | 427 | 0 | 215 | 62.31% |
| **ALL** | **3,703** | **2,118** | **1,585** | **0** | **1,014** | **57.20%** |

**TRADE** (vs the tradeable line at the first TRUE observation — full
coverage):

| Checkpoint | N | UNDER | OVER | PUSH | UNDER % |
| --- | ---: | ---: | ---: | ---: | ---: |
| 25% | 1,536 | 760 | 776 | 0 | 49.48% |
| 50% | 1,833 | 995 | 838 | 0 | 54.28% |
| 75% | 1,348 | 856 | 492 | 0 | 63.50% |
| **ALL** | **4,717** | **2,611** | **2,106** | **0** | **55.35%** |

`UNDER % = UNDER / (UNDER + OVER + PUSH)`. PUSH is 0 throughout because
every trigger line is a half-point line (x.5), so `final == line` is
arithmetically unreachable — consistent with the note in
`scorecard._checkpoint_outcome`.

---

## 4. Baseline and lift

Baseline = the game reached the same checkpoint (≥1 eligible observation in
the band) but never satisfied the condition, settled against the same V4
line.

| Checkpoint | alert N | alert UNDER % | baseline N | baseline UNDER % | lift | relative |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 25% | 1,202 | 55.74% | 2,247 | 44.01% | +11.73 pp | +26.6% |
| 50% | 1,368 | 54.24% | 3,203 | 46.24% | +8.00 pp | +17.3% |
| 75% | 1,133 | 62.31% | 3,992 | 46.59% | +15.72 pp | +33.7% |

The edge is real and monotonically strongest at 75%. The 25% checkpoint is
the weakest and was the reason the 2026-09-14 directive added a progress
floor rather than keeping all three.

---

## 5. Settlement coverage — a live-path defect

`trigger_market_total` can only forward-fill from lines actually observed
in the snapshot series, and early-game lines are sparse. Alerts with **no
provable trigger line**:

| Checkpoint | alerts without a line | share |
| --- | ---: | ---: |
| 25% | 334 / 1,536 | 21.7% |
| 50% | 465 / 1,833 | 25.4% |
| 75% | 215 / 1,348 | 15.9% |

25.4% of 50%-checkpoint alerts and 21.7% of 25% alerts settle against
nothing. In production those records render a permanently PENDING verdict
with a "–" triggered line. This is independent of whether the edge is real
and applies to any condition built on this settlement path. (The 2026-09-14
75%-only trigger sidesteps most of it by dropping the two worst
checkpoints.)

---

## 6. Separate cohorts and intersection

`required_pts_per_min > league_average_pace` — kept strictly separate from
the live alert (BOARD basis):

| Checkpoint | N | UNDER | OVER | UNDER % |
| --- | ---: | ---: | ---: | ---: |
| 25% | 1,565 | 765 | 800 | 48.88% |
| 50% | 1,917 | 973 | 944 | 50.76% |
| 75% | 1,669 | 940 | 729 | 56.32% |
| **ALL** | **5,151** | **2,678** | **2,473** | **51.99%** |

Intersection (BOARD basis, game-checkpoint grain):

| Cohort | N | UNDER | OVER | UNDER % |
| --- | ---: | ---: | ---: | ---: |
| A `required > league avg` | 5,151 | 2,678 | 2,473 | 51.99% |
| B `actual < required` | 4,476 | 2,543 | 1,933 | 56.81% |
| C `actual < league avg` | 6,341 | 3,481 | 2,860 | 54.90% |
| **D CURRENT ALERT (B ∧ C)** | 3,703 | 2,118 | 1,585 | **57.20%** |
| E `A ∧ D` | 1,850 | 1,105 | 745 | 59.73% |

The 2026-09-13 alert (D) dominated every single-condition cohort. The
2026-09-14 rule effectively moved to a version of cohort A restricted to
≥75% progress, which is why its rate is higher.

---

## 7. Per league × checkpoint (BOARD basis)

| League | cp | alert N | UNDER | OVER | PUSH | alert % | base N | base % | lift pp |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| NBA | 25 | 303 | 188 | 115 | 0 | 62.05% | 967 | 44.98% | +17.06 |
| NBA | 50 | 287 | 151 | 136 | 0 | 52.61% | 1,366 | 47.66% | +4.96 |
| NBA | 75 | 79 | 53 | 26 | 0 | 67.09% | 1,651 | 47.91% | +19.18 |
| NBA | ALL | 669 | 392 | 277 | 0 | 58.59% | 3,984 | 47.11% | +11.48 |
| KBL | 25 | 132 | 66 | 66 | 0 | 50.00% | 199 | 45.23% | +4.77 |
| KBL | 50 | 171 | 85 | 86 | 0 | 49.71% | 327 | 45.57% | +4.14 |
| KBL | 75 | 145 | 94 | 51 | 0 | 64.83% | 435 | 44.37% | +20.46 |
| KBL | ALL | 448 | 245 | 203 | 0 | 54.69% | 961 | 44.95% | +9.73 |
| CBA | 25 | 156 | 74 | 82 | 0 | 47.44% | 334 | 40.12% | +7.32 |
| CBA | 50 | 188 | 96 | 92 | 0 | 51.06% | 466 | 46.78% | +4.28 |
| CBA | 75 | 123 | 79 | 44 | 0 | 64.23% | 540 | 46.30% | +17.93 |
| CBA | ALL | 467 | 249 | 218 | 0 | 53.32% | 1,340 | 44.93% | +8.39 |
| TBSL | 25 | 258 | 143 | 115 | 0 | 55.43% | 349 | 41.55% | +13.88 |
| TBSL | 50 | 303 | 166 | 137 | 0 | 54.79% | 481 | 41.58% | +13.21 |
| TBSL | 75 | 303 | 183 | 120 | 0 | 60.40% | 618 | 42.39% | +18.00 |
| TBSL | ALL | 864 | 492 | 372 | 0 | 56.94% | 1,448 | 41.92% | +15.02 |
| EuroLeague | 25 | 269 | 152 | 117 | 0 | 56.51% | 368 | 45.92% | +10.58 |
| EuroLeague | 50 | 321 | 186 | 135 | 0 | 57.94% | 526 | 46.77% | +11.18 |
| EuroLeague | 75 | 330 | 197 | 133 | 0 | 59.70% | 679 | 49.04% | +10.65 |
| EuroLeague | ALL | 920 | 535 | 385 | 0 | 58.15% | 1,573 | 47.55% | +10.60 |
| CYBER | 25 | 84 | 47 | 37 | 0 | 55.95% | 30 | 53.33% | +2.62 |
| CYBER | 50 | 98 | 58 | 40 | 0 | 59.18% | 37 | 45.95% | +13.24 |
| CYBER | 75 | 153 | 100 | 53 | 0 | 65.36% | 69 | 44.93% | +20.43 |
| CYBER | ALL | 335 | 205 | 130 | 0 | 61.19% | 136 | 47.06% | +14.14 |

All-checkpoints per league (BOARD): NBA 58.59% (n=669), KBL 54.69% (448),
CBA 53.32% (467), TBSL 56.94% (864), EuroLeague 58.15% (920), CYBER 61.19%
(335). Alert N at 75% × NBA is only 79 — treat that cell as indicative, not
established.

---

## 8. Raw validation rows

Alert cohort, spread across leagues (line = the live line at the first TRUE
observation; `board` = V4's sealed checkpoint line the outcome settles
against):

| game_id | league | cp | prog% | score | line | el | rem | actual | required | lg_avg | board | final | result |
| --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 30804617 | CYBER | 25 | 41.98 | 54-36 | 221.5 | 20.1 | 27.9 | 4.4665 | 4.7217 | 4.4696 | 221.5 | 190 | UNDER |
| 30804740 | CYBER | 25 | 25.00 | 26-25 | 208.5 | 12.0 | 36.0 | 4.2500 | 4.3750 | 4.4696 | 209.5 | 213 | OVER |
| 30804834 | EuroLeague | 25 | 25.00 | 21-15 | 154.5 | 10.0 | 30.0 | 3.6000 | 3.9500 | 4.3009 | 156.5 | 149 | UNDER |
| 30804835 | EuroLeague | 25 | 42.50 | 38-34 | 179.5 | 17.0 | 23.0 | 4.2353 | 4.6739 | 4.3009 | 176.5 | 190 | OVER |
| 30804836 | EuroLeague | 25 | 26.25 | 23-13 | 138.5 | 10.5 | 29.5 | 3.4286 | 3.4746 | 4.3009 | 138.5 | 158 | OVER |
| 30804968 | TBSL | 25 | 28.75 | 19-26 | 157.5 | 11.5 | 28.5 | 3.9130 | 3.9474 | 4.1683 | 157.5 | 131 | UNDER |
| 30804969 | TBSL | 25 | 28.12 | 20-24 | 157.5 | 11.2 | 28.8 | 3.9111 | 3.9478 | 4.1683 | 160.5 | 147 | UNDER |
| 30804971 | TBSL | 25 | 28.75 | 21-17 | 147.5 | 11.5 | 28.5 | 3.3043 | 3.8421 | 4.1683 | 151.5 | 142 | UNDER |
| 30805017 | NBA | 25 | 33.12 | 35-32 | 202.5 | 13.2 | 26.8 | 5.0566 | 5.0654 | 5.6367 | 213.5 | 201 | UNDER |
| 30805061 | CBA | 25 | 40.00 | 30-34 | 162.5 | 16.0 | 24.0 | 4.0000 | 4.1042 | 4.4582 | 168.5 | 164 | UNDER |
| 30805390 | KBL | 25 | 25.00 | 23-13 | 148.5 | 10.0 | 30.0 | 3.6000 | 3.7500 | 3.8887 | 148.5 | 163 | OVER |

Every row satisfies `actual < required` and `actual < league_avg`. Worked
example, `30804617`: current total = 54 + 36 = **90**; 90 / 20.15 = **4.4665**
actual; (221.5 − 90) / 27.85 = **4.7217** required; both 4.4665 < 4.7217 and
4.4665 < 4.4696 (CYBER average) hold, so the condition is TRUE. (elapsed /
remaining print rounded at 20.1 / 27.9; the stored values are ≈20.15 /
27.85 — that is the 0.00005-level rounding of §9.) The outcome settles
against V4's sealed `board` line 221.5, and final 190 < 221.5 → **UNDER**.

---

## 9. Formula validation

```
actual_pts_per_min   = current_total_points / elapsed_game_minutes
required_pts_per_min = (live_total_line - current_total_points) / remaining_game_minutes
```

Checked on the **full** usable population: **306,097 rows, 0 disagreements,
max |stored − recomputed| = 0.000050** (float rounding only). These are the
same fields the live path consumes (`_analyze_game` → `under_alert_state`),
derived from the same store (`PaceProjector` over `clean_projections`), so
the backtest operates on the production values.

---

## 10. The answer

**What percentage of historical games that would have triggered the
2026-09-13 live UNDER alert actually finished UNDER?**

| Checkpoint | BOARD (V4 sealed line) | TRADE (tradeable line) |
| --- | ---: | ---: |
| 25% | 55.74% (n=1,202) | 49.48% (n=1,536) |
| 50% | 54.24% (n=1,368) | 54.28% (n=1,833) |
| 75% | 62.31% (n=1,133) | 63.50% (n=1,348) |
| **ALL** | **57.20% (n=3,703)** | **55.35% (n=4,717)** |

Per league, all checkpoints (BOARD): NBA 58.59%, EuroLeague 58.15%, TBSL
56.94%, KBL 54.69%, CBA 53.32%, CYBER 61.19%.

Against a **45.86%** baseline (BOARD), the 2026-09-13 alert carried a
**+11.34 pp / +24.7%** relative edge overall, concentrated at ≥75% progress.

---

## 11. Cross-checks

| Source | Condition | Result |
| --- | --- | --- |
| this document (BOARD / TRADE) | `actual<req ∧ actual<avg` | 57.20% / 55.35% |
| `analysis_live_under_alert_backtest_2026-09-13.txt` | same | 57.87% |
| `analysis_parity_under_trigger_2026-09-14.txt` | `progress≥75 ∧ req>avg·1.04` | 65.43% (693-cut 69.89%) |

An independent implementation landing within ~0.7 pp of the sibling script,
with byte-identical league references, is the corroboration that matters.
The residual is explained by checkpoint attribution (V4 band vs
closest-to-target window), not by any disagreement about the condition or
the settled outcomes.

---

## 12. Caveats

1. `league_average_pace` is computed over the whole settled population with
   no time window, exactly as `competition_pace_reference()` does. The
   backtest inherits whatever look-ahead that implies: a live poll at time
   *t* uses averages that include games settling after *t*.
2. The live `g.live` status gate cannot be replayed; the equivalent
   per-observation evidence (non-terminal, live market ≤300 s, ≥2.5 min
   remaining) was applied instead.
3. Results shift as the databases grow — the population here (13,421 games /
   9,984 settled) is larger than at the 2026-09-13 runs (~8,617 settled), so
   exact cell counts are not a stable target. Ratios are.
4. BOARD-basis results cover only alerts with a provable trigger line
   (§5); TRADE-basis results cover every alert.
