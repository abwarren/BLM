# Candidate OOS Backtest — do any BLM Under signals sustain >55% at 1.85?

**Date:** 2026-09-15 · **Verdict up front: no candidate demonstrates a
persistent, statistically-established >55% edge on unseen chronological
data.** One candidate (E1) is the only one above 55% in the latest period and
is the only one that merits further observation, but its recent sample is too
small to call.

Reproduce: `python3 scripts/candidate_oos_185_2026-09-15.py`
→ `analysis_candidate_oos_185_2026-09-15.txt` (full tables).

Read-only throughout: both databases opened `file:...?mode=ro`; no production
code, threshold, collector, dashboard or database was modified.

---

## 1. Method

Production modules are **imported, never reimplemented**:

| Concern | Authority imported |
| --- | --- |
| game time / progress | `projection.duration_for`, `projection.row_elapsed_minutes`, `under_outcome._row_progress` |
| terminality | `under_outcome._terminal_row` |
| checkpoint identity | `under_alert.checkpoint_for` |
| **settlement line** | `under_outcome.trigger_market_total` |
| verdict | `under_outcome.outcome_status` |
| league pace | `competition_pace.competition_pace_reference` |

Row source is the immutable `snapshots` table — the same rows
`trigger_market_total` settles from — so the condition and the settlement read
one consistent observation series. The pace fields derived from it were
verified equal to the live path's independent `clean_projections` values
(201 observations, 0 mismatches; see §9).

Population: settled games with an authoritative final
(`game_results.final_result_status='OK'`, `final_total>0`), non-`INVALID`
quality, post-clean-epoch, with ≥1 observation at `progress_pct >= 75` that
carries a live market line and `remaining >= 2.5` min. **4,035 eligible games.**

One trigger per game (checkpoint is 75% for every candidate here), settled
against the sealed checkpoint line; unprovable lines are excluded, never
guessed.

Economy: odds **1.85**, stake 1u, win **+0.85**, loss **−1.00**, push **0**.
Break-even = 1/1.85 = **54.054%**. PUSH is arithmetically unreachable (every
line is a half-point, every final an integer) — 0 pushes everywhere, reported
as arithmetic, not as "none observed".

## 2. Candidates

All gated on `progress_pct >= 75`.

| # | Condition |
| --- | --- |
| A | `required > league_avg × 1.04` — **current production rule** |
| A* | `required > trailing league_avg × 1.04` — causal control for A |
| B | `required >= frozen league P80` |
| C | `required >= trailing-3d league P80` |
| D | `required >= trailing-3d league P85` |
| E1 | `actual < required AND actual < league_avg` |
| E1* | `actual < required AND actual < trailing league_avg` — causal control |
| E2 | `actual < required` |
| E3 | `required > league_avg` |
| F | `frozen P80 AND actual < required` |
| G | `frozen P95 AND actual < required` |
| H | `frozen P97.5 AND actual < required` |

**"Frozen" = estimated on the discovery period only** (earliest 50 % of the
span) and applied unchanged afterwards. A whole-sample percentile would be
look-ahead, which §1 forbids; for reference the whole-sample P80 is also
recorded in the raw report but is **not** used for any decision.

## 3. Chronological OOS

Split by trigger timestamp, defined on all eligible observations so it is
candidate-independent — no threshold was chosen from this split.

| Partition | Span |
| --- | --- |
| Discovery | 2026-09-05 05:43 → 2026-09-10 14:15 |
| Validation | 2026-09-10 14:15 → 2026-09-13 06:32 |
| Latest OOS | 2026-09-13 06:32 → 2026-09-15 22:48 |
| Equal thirds | cut at 2026-09-08 19:24 / 2026-09-12 09:06 |

## 4. Baseline control — 50.01% UNDER

Betting UNDER on **every** eligible game, settled the same way:

| | Discovery | Validation | Latest OOS | Overall |
| --- | ---: | ---: | ---: | ---: |
| UNDER % | 51.33% | 51.21% | 47.41% | **50.01%** (n=4,035) |

By equal thirds: 50.44% / 51.65% / **47.73%**. The baseline **also collapses in
the final block**, so the late-period falloff seen in every candidate is at
least partly a market-wide regime shift, not signal-specific decay. Any
candidate's late-period number must be read *against* that 47.41%, not
against a flat 50%.

## 5. Results

| cand | bets | win% | ROI | 95% CI | discovery | validation | latest OOS | coverage | leakage |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- |
| A production | 381 | 56.69% | +4.88% | [51.68, 61.58] | 57.93% | 63.41% | **45.74%** | 9.4% | n/a |
| A* causal | 390 | 56.92% | +5.31% | [51.96, 61.75] | 58.08% | 63.28% | **46.32%** | 9.7% | causal ✓ |
| B frozen P80 | 1,076 | 51.39% | −4.92% | [48.41, 54.37] | 52.59% | 54.23% | 45.70% | 26.7% | discovery-frozen |
| C 3d P80 | 1,353 | 51.44% | −4.83% | [48.78, 54.10] | 52.96% | 53.51% | 46.79% | 33.5% | causal ✓ |
| D 3d P85 | 1,097 | 52.14% | −3.54% | [49.18, 55.09] | 52.67% | 54.98% | 47.13% | 27.2% | causal ✓ |
| **E1** | 456 | **62.72%** | **+16.03%** | **[58.19, 67.03]** | 68.09% | 58.95% | **58.97%** | 11.3% | n/a |
| **E1*** | 451 | **62.75%** | **+16.09%** | **[58.20, 67.09]** | 68.31% | 58.95% | **58.97%** | 11.2% | causal ✓ |
| E2 | 544 | 60.66% | +12.22% | [56.50, 64.68] | 64.55% | 60.73% | 52.38% | 13.5% | n/a |
| E3 | 603 | 56.38% | +4.31% | [52.40, 60.29] | 56.95% | 60.76% | 48.25% | 14.9% | n/a |
| F P80+gap | 207 | 63.29% | +17.08% | [56.53, 69.55] | 62.96% | 77.42% | **40.54%** | 5.1% | discovery-frozen |
| G P95+gap | 88 | 69.32% | +28.24% | [59.04, 77.98] | 70.83% | 88.00% | **33.33%** | 2.2% | discovery-frozen |
| H P97.5+gap | 56 | 64.29% | +18.93% | [51.19, 75.54] | 62.50% | 89.47% | **30.77%** | 1.4% | discovery-frozen |

Partition sample sizes and CIs are in the raw report. The headline tension:
**every candidate looks excellent in validation and every candidate except
E1/E1* is at or below break-even in the latest period.**

### E1 detail

| | n | win% | 95% CI | ROI | units |
| --- | ---: | ---: | --- | ---: | ---: |
| All | 456 | 62.72% | [58.19, 67.03] | +16.03% | +73.10 |
| Discovery | 188 | 68.09% | [61.12, 74.33] | +25.96% | +48.8 |
| Validation | 190 | 58.95% | [51.84, 65.70] | +9.05% | +17.2 |
| Latest OOS | 78 | 58.97% | **[47.89, 69.22]** | +9.10% | +7.1 |

Rolling: 50-bet mean 62.75% (min 46.00%, 84.0% of windows > break-even);
100-bet mean 62.80% (min 52.00%); 200-bet mean 63.64% (min 56.50%, 100% of
windows > break-even). Max drawdown **8.65 u**; longest losing streak **5** —
against production's 16.90 u / 5 and C's 70.75 u / 7.

League (E1): CYBER 66.19% (n=139), CBA 67.74% (31), KBL 62.00% (50), TBSL
60.92% (87), Euro 60.61% (132), NBA 52.94% (17). Largest league contribution
**30.5%** (CYBER); excluding CYBER: n=317, 61.20%, ROI +13.22%. No single
league dominates, and the edge survives the ex-CYBER cut.

## 6. Leakage verification

- **Trailing percentiles (C, D).** Cutoff is the percentile over the same
  league's observations in `[t−3d, t)`, strictly before the trigger. Verified
  programmatically per league: the youngest datum any cutoff consumed is
  **0.1–0.2 s before** its own trigger; the trigger observation is never a
  member of its own cutoff. **0 violations.**
- **`actual < required` (E1, E1*, E2, F, G, H).** Both operands are computed
  from the trigger row's own fields (`home_score+away_score`, `elapsed`,
  `remaining`, `total_line`) at one timestamp — verified equal to production's
  independent `clean_projections` values on 201 observations. Knowable at
  trigger time; no future data. This is the §2 precondition for using it, and
  it holds.
- **League averages.** Production's `league_average_pace` is computed over the
  whole settled population with no time window — a genuine look-ahead in
  *vintage*. It is kept for A because that is the production rule, and the
  causal control **A\*** (and **E1\***) recompute it from games settled
  strictly before `t`. A vs A\* (56.69 vs 56.92) and E1 vs E1\* (62.72 vs
  62.75) are near-identical, so no headline depends on the look-ahead.
- **Settlement.** `trigger_market_total` reads only observations at or before
  the checkpoint boundary; verified equal to the production function on the
  same rows, and the verdict equals `outcome_status` on a settled OK game
  (30890016#i1: final 266 vs sealed line 240.5 → OVER).

## 7. §12 Factual determination

1. **>55% overall:** A, A\*, E1, E1\*, E2, E3, F, G, H. (B, C, D do not.)
2. **Remain >55% in validation:** A, A\*, E1, E1\*, E2, E3, F, G, H.
3. **Remain >55% in latest OOS:** **E1, E1\* only.**
4. **Exceed 54.054% break-even:** overall — A, A\*, E1, E1\*, E2, E3, F, G, H;
   in latest OOS — **E1, E1\* only.**
5. **Sufficient coverage:** G and H are **low coverage** (<100 bets and <5%);
   F is borderline (5.1%). A/A\* 9.4–9.7%, E1/E1\* 11.2–11.3%, E3 14.9%,
   C 33.5%, B 26.7%, D 27.2%. High win rate + 1.4% coverage (H) is **not**
   evidence of anything.
6. **95% CI entirely above 54.054%:** E1, E1\*, E2, F, G. Not A, not A\*
   (their CIs include break-even).
7. **Persistent >55% across chronological OOS:** **E1 and E1\* only** — and
   their latest-OOS CI is [47.89%, 69.22%], which still contains break-even,
   so persistence is *observed but not statistically established* at n=78.
8. **Concentration:** not one league — E1's largest league is 30.5% of bets and
   the ex-CYBER subset holds at 61.20%. **It is concentrated in time**: every
   candidate, and the 50.01% baseline itself, decays in the final third
   (baseline 47.73%), so the recent regime is broadly adverse rather than the
   edge being confined to one cluster.
9. **Does production still hold its edge?** Historically yes (56.69%, +4.88%,
   CI [51.68, 61.58] — but that CI **includes** break-even, p=0.163). Out of
   sample it does **not**: 45.74% in the latest period, −15.37% ROI, stability
   classified **Decaying**. Its edge is not distinguishable from noise at this
   sample size.
10. **Do trailing 3d P80/P85 improve stability over production?** **No.** C
    51.44% and D 52.14% are both below break-even overall and lose money
    (−4.83%, −3.54%). The percentile-in-absolute-pace thresholds are the worst
    family tested — worse than production, and worse than simply taking
    `actual < required`.

## 8. Caveats (load-bearing)

1. **Selection bias on E1.** E1 was nominated *because* it previously exceeded
   55%, so its overall CI/p-value is not a pre-registered test. The honest
   evidence is the OOS partitions (58.95%, 58.97%) — both above break-even,
   both above 55%, but on n=190 and n=78.
2. **10.7 days of data.** 4,035 eligible games is not many for tail
   percentiles; the frozen P95/P97.5 estimates are noisy (n_discovery 195–1,498
   per league) and F/G/H inherit that.
3. **Database growth.** Counts move between runs; ratios are the stable
   quantity. All figures are as of the run date.
4. **The live `g.live` status gate cannot be replayed** (the `games` table
   stores only the final status). The equivalent per-observation evidence
   (non-terminal, `remaining >= 2.5`, live line present) was substituted.
5. **No real-world costs** (commission, line movement, availability) are
   modelled — every ROI here is gross.

## 9. Verification

Ad-hoc, not a green suite (no canonical test command covers analysis scripts).
PASS: all `sqlite3.connect` `mode=ro` and a `mode=ro` handle rejects writes;
frozen-copy hashes unchanged across two runs (**read-only proven**); two frozen
runs byte-identical (**deterministic**); snapshot-derived pace equals
production's independent `clean_projections` (201 obs, 0 mismatches); sealed
line equals `trigger_market_total` and the verdict equals `outcome_status` on a
settled OK game; `Trail` window never consumes the trigger's own second;
leakage section reports no violation. The production DB is live (a collector
writes it continuously), so read-only was proven on a `Connection.backup()`
snapshot rather than by hashing the live file.

## 10. Conclusion

No candidate in this set currently supports a **sustainable** >55% signal at
1.85 in the sense the directive defines it — i.e. >54.054% in genuinely unseen
chronological data with adequate sample and no leakage.

- The **percentile-threshold family (B, C, D) fails outright** (51.4–52.1%,
  negative ROI). Trailing 3-day P80/P85 do not improve on production.
- The **production rule (A)** is profitable historically (56.69%, +4.88%) but
  is classified **Decaying**: 63.41% in validation, **45.74%** in the latest
  period, and its overall CI includes break-even.
- **E1 (`actual < required AND actual < league_avg`)** is the only candidate
  above 55% in **all three** chronological partitions (68.09 / 58.95 / 58.97),
  the only one above break-even in the latest period, with the smallest
  drawdown of any profitable candidate and the highest share of rolling
  windows above break-even. **But** it was selected post hoc, its latest-OOS
  CI ([47.89, 69.22]) still includes break-even, and its coverage is 11.3%.
  The correct statement is: *E1 is the only candidate whose edge has not yet
  broken down out of sample; it is not yet established.*
- The high-win-rate low-coverage cells (F, G, H — 63–69% on 1.4–5.1% coverage)
  are **not** candidates: their latest-period numbers (30–41%) show the
  aggregate was a small-sample artefact.

The decisive evidence the directive asks for — remaining above the 1.85
break-even in genuinely unseen data with adequate sample — is **not present
for any candidate**, and E1 needs more unseen periods before it can be called
anything other than promising.

**No production threshold was changed. No production code was modified.**
