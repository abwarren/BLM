# 09 — BLM Model Knowledge

Retrieval keywords: BLM model, pace, projection, fair total, expected total, OLV,
market vs fair, pace reality, line movement, rotation volatility, blowout, team total,
model weighting, MODEL_VERSION, fingerprint context, deterministic engine.

**What this pack answers:** what the BLM model computes, from what inputs, and the
rules it must hold. It is a deterministic engine; an LLM only INTERPRETS it.

---

## Rule 9.1 — BLM is a deterministic engine; a model INTERPRETS, never calculates

**Rule.** The deterministic BLM engine (`blm_v4/projection.py`,
`live_analytics/*`) calculates every number and every verdict. Any LLM/7B layer
only interprets; production decision logic stays deterministic. Nothing in a model
layer may modify `blm_v4/**`, alerts, or Auto-Bet.

**Source.** `knowledge-first-llm` skill →
`references/deterministic-engine-interpretation.md`; `docs/BLM_MODEL_SPEC.md` §8.

**Why.** A reasoning model must never become the authority for a bet.

**Valid.** "The engine computed fair 181.5; the model explains why the market
diverged."

**Invalid.** "The model predicts the final will be 181.5."

**Forbidden.** Letting a model layer write a projection, an alert or a bet.

**Verify.** The production path imports the deterministic functions, not a model.

---

## Rule 9.2 — The three live pace quantities

**Rule.**

```
actual_pts_per_min  = observed scoring rate, scaled to full regulation, kept in [20, 400]
required_pts_per_min = (live_total_line - current_total_points) / remaining_game_minutes
league_average_pace  = mean over SETTLED games (game_results OK) of (final_total / regulation),
                       partitioned by competition_slug
```

**Source.** `projection.pace_from_snapshots`;
`live_analytics.competition_pace.competition_pace_reference`; `docs/BLM_MODEL_SPEC.md`
§1.3.

**Why.** The alert compares `required` against the game's OWN competition average.

**Valid.** required 1.30 vs league 1.20 → above the bar.

**Invalid.** Using a global (cross-league) average.

**Forbidden.** Mixing BETUAL_NBA and CYBER_2K26 populations
(`DECISIONS.md` §7.C).

**Verify.** `competition_pace` for the slug; never a single global number.

---

## Rule 9.3 — The projection formula (single source of truth)

**Rule.**

```
pace            = pace_from_snapshots(rows)   (fallback: market line, else 100.0)
expected_total  = quantize_half(max( 0.7*pace + 0.3*total_line , home_proj + away_proj ))
expected_margin = (home_score - away_score) / elapsed * full   (>=60s span fallback)
home_projection = max((expected_total + expected_margin)/2, home_score)
away_projection = max((expected_total - expected_margin)/2, away_score)
```

The 70/30 pace/market blend is the model. The live-score floor ensures a projection
never sits below points already on the board. The final value snaps to the x.0/x.5
half-point grid (`quantize_half`, ties HALF-UP — NOT banker's rounding).

**Source.** `blm_v4/projection.py::project`; `docs/BLM_MODEL_SPEC.md` §1.5;
`DECISIONS.md` §10.

**Why.** One projection consumed by the API, dashboard and scorecard — a scored
prediction is exactly what the model would have displayed.

**Valid.** `0.7*174 + 0.3*180 = 175.8 → 176.0`.

**Invalid.** Using `round(x*2)/2` (banker's rounding); quantizing BEFORE the floor.

**Edge.** `expected_total` may be 100.0 = a "pace unavailable" sentinel, not a
prediction (FM-12).

**Forbidden.** A second projection definition anywhere else.

**Verify.** `project(rows)` equals the API's `expected_total` (parity test).

---

## Rule 9.4 — Pace reality: the model compares BLM to the MARKET line, not to itself

**Rule.** The model's value is defined by the divergence between its fair total and
the observed market line. The comparison is `market_vs_fair = live_market_line -
blm_fair_value` (signed, never discarded). BLM must compare its prediction against
the market line (OLV / live / CLV), NOT against its own prediction.

**Source.** `checkpoint_market.market_vs_fair`; `DECISIONS.md` §10.J; the user's
established rule.

**Why.** Comparing BLM to BLM is circular and yields no edge.

**Valid.** market 180, fair 148 → UNDER_VALUE (−32).

**Invalid.** "BLM vs BLM" or "model beat model".

**Forbidden.** Presenting a circular comparison as an edge.

**Verify.** The comparison row names the market line type (§Rule 8.6).

---

## Rule 9.5 — Classification durations are never shared

**Rule.** `duration_for("BETUAL_NBA") -> (10, 40)`; `duration_for("CYBER_2K26") ->
(12, 48)`; unknown → (10, 40). Every consumer goes through
`duration_for(classification)`.

**Source.** `projection.CLASSIFICATION_DURATION`; `DECISIONS.md` §14.K.

**Why.** The same wall-clock minute is a different percent in each classification.

**Valid.** CYBER 50% = 24 elapsed minutes; BETUAL 50% = 20.

**Invalid.** Hardcoding 10 minutes for CYBER.

**Forbidden.** Sharing a quarter length across classifications.

**Verify.** `duration_for(cls)`.

---

## Rule 9.6 — The fingerprint layer is CONTEXT, never a gate

**Rule.** The historical UNDER fingerprint layer (C1, C3, C5, R2) is recorded
context on every UNDER evaluation. `fingerprint_count` counts only TRUE
fingerprints; it is NOT a threshold, gates nothing, and creates no alert.

**Source.** `live_analytics/under_fingerprints.py`; `10_TRAP_METER.md`.

**Why.** Fingerprints enrich the alert; they must never loosen or widen it.

**Valid.** "HISTORICAL FINGERPRINTS: 2 — ✓C3 ✓R2."

**Invalid.** "The alert fired because a fingerprint matched."

**Forbidden.** Making a fingerprint a gate or a second alert source.

**Verify.** `evaluate_fingerprints(...)` output; `active` unchanged by fingerprints.

---

## Rule 9.7 — The win rate is `under_pct`, from the platform's own function

**Rule.** The canonical win rate comes from
`live_analytics.fingerprint_stats.compute_stats(prod, clean)`. Win = final UNDER the
frozen line, so `under_pct` IS the win rate. Report BOTH the `cohort` rate (trigger
condition alone) and the `actionable` rate (+ a LIVE market).

**Source.** `fingerprint_stats.py::compute_stats`; skill
`references/alert-performance-and-win-rate.md`.

**Why.** One canonical answer; a bare headline overstates what you can bet (~4.7 pp).

**Valid.** cohort 63.87% (n=1146) vs actionable 59.17% (n=823), each with a Wilson
95% interval.

**Invalid.** A second hand-rolled win-rate definition.

**Edge.** The pooled rate is NOT stable (weekly 52–79%); quote the series, not one
point estimate.

**Forbidden.** Presenting the 90% "late" timing band as the headline.

**Verify.** `n` from `compute_stats` equals the platform headline `n`.
