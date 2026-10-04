# BLM_MODEL_SPEC.md

> **BLM-ML Model Specification — knowledge/training architecture for a future 7B
> BLM reasoning model.**
>
> Status: **SCAFFOLDING / SPECIFICATION ONLY.** No fine-tuning has begun. No
> synthetic outcome labels exist or are permitted here. The production BLM
> tree (`blm_v4/**`, alerts, Auto-Bet, services) is **NOT** modified by anything
> in this document or the sibling `ml/` tree.
>
> Version: `blm-model-spec/1` (2026-10-03)
> Owning area: `/home/ubuntu/BLM/ml/` (development-only, isolated from production).
> Companion artifacts: `ml/concepts/*.yaml`, `ml/datasets/*`, `ml/training/*`,
> `ml/inference/blm_reasoner.py`.

---

## 0. Purpose and the one-sentence contract

BLM has a **deterministic engine** (Python/SQL in `blm_v4/`) that computes every
number and every gate. The eventual goal is a **7B language model that
*interprets* the BLM state** — explaining, in plain language, what the engine's
numbers mean, which historical fingerprints are present, and what the risk
factors are — **without ever computing a number or making the trading decision.**

> **The engine calculates. The model interprets. The production decision stays
> deterministic.**

This document defines the concepts, the math, the features, the fingerprints,
the reasoning rules, the input/output schemas, the deterministic-vs-LLM boundary,
the training-example format, and the evaluation methodology. It is the ground
truth the training data and the evaluation harness must conform to.

### 0.1 Non-negotiable constraints (carried from the authorizing directive)

1. **DO NOT modify production BLM logic** (`blm_v4/**`, `server.py`,
   `watchdog.sh`, `deploy/**`).
2. **DO NOT modify live alerts** or the alert formulas.
3. **DO NOT modify Auto-Bet** or the betting execution path.
4. **DO NOT deploy. DO NOT restart services.**
5. **DO NOT begin fine-tuning** in this phase.
6. **DO NOT generate synthetic outcome labels.** Outcome labels may only come
   from *real* captured outcomes (authoritative `game_results` finals, or real
   `quarter_score_observations` once that table is populated).
7. **Keep all ML work isolated** from the production tree (everything lives under
   `/home/ubuntu/BLM/ml/` plus this one doc in `docs/`).

---

## 1. BLM concepts and exact definitions

These definitions mirror the production source **verbatim in meaning**. Where a
constant exists in code it is quoted with its file. The model is taught these
definitions; it must never invent a competing one.

### 1.1 Classification (statistical population)

Source: `blm_v4/classifications.py`, `blm_v4/projection.py`.

| Classification | Family | Competition(s) | Regulation geometry |
|---|---|---|---|
| `BETUAL_NBA` | `betual` | Betual virtual basketball — **bundles five leagues** keyed by `competition_slug`: `betual-nba`, `betual-tbsl`, `betual-euroleague`, `betual-cba`, `betual-kbl` | 4 × 10 min = **40 min** |
| `CYBER_2K26` | `cyber` | `cyber-basketball-2k26-matches` (single competition) | 4 × 12 min = **48 min** |
| `CONVENTIONAL` | `conventional` | (real-world basketball — not in the live BLM population) | n/a |
| `UNKNOWN` | `unknown` | — | falls back to 40-min basis |

**Load-bearing facts the model must hold:**

- **Classification is the PROVIDER FAMILY, not the league.** `BETUAL_NBA` is not
  one league; it is five. Statistics must be segmented by `competition_slug`.
- **`competition_slug` is the authoritative league identifier.** The display name
  (`games.competition`) is unreliable — TBSL games display as "Betual NBA".
- **The two families are independent statistical populations.** Nothing in BLM
  may mix their data, distributions, or derived statistics
  (`classifications.py` module docstring).

### 1.2 Game progress (`progress_pct`)

Source: `blm_v4/projection.py` (`clock_minutes`, `row_elapsed_minutes`,
`duration_for`).

```
elapsed_game_minutes = derived from (period / quarter, count-down clock)
progress_pct         = 100 * clamp(elapsed_game_minutes / full_game_minutes, 0, 1)
```

- The clock is a **count-down** clock (`MM:SS`, e.g. `07:30` means 7.5 minutes
  *remaining in the period*). A display ≥ `quarter_minutes` is a period-start
  sentinel and contributes **0** elapsed (clamped), never a negative.
- Half-time boundary rows ("Half Time"/"Half End") are **pinned at full/2**.
- `progress_pct` is game-clock based, so **the same wall-clock minute is a
  different percent in each classification** (see §5 Cyber).

### 1.3 The three live pace quantities

Source: `blm_v4/projection.py`, `blm_v4/live_analytics/competition_pace.py`.

- **`actual_pts_per_min`** — the game's observed scoring rate.
  `pace_from_snapshots`: from wall-clock deltas when ≥30 s of scored snapshots
  exist, else from the last score ÷ elapsed game minutes; scaled to the full
  regulation; kept only within `[20, 400]`.
- **`required_pts_per_min`** — the scoring rate the rest of the game must run
  at for the *current live total line* to be exactly hit:
  ```
  required_pts_per_min = (live_total_line - current_total_points) / remaining_game_minutes
  ```
- **`league_average_pace`** — the competition's normal full-game scoring rate:
  ```
  league_average_pace = mean over SETTLED games (game_results.final_result_status='OK')
                        of ( final_total / regulation_minutes )
  ```
  partitioned by `competition_slug`. **Never a global figure; never merges two
  competitions.** (`competition_pace.competition_pace_reference`.)

### 1.4 Classification duration

Source: `blm_v4/projection.py::duration_for`.

```
duration_for("BETUAL_NBA") -> (10.0, 40.0)     # quarter_minutes, full_game_minutes
duration_for("CYBER_2K26") -> (12.0, 48.0)
duration_for(anything else) -> (10.0, 40.0)     # legacy/unclassified fallback
```

Every consumer MUST go through `duration_for(classification)`; no hardcoded
quarter length is ever shared across classifications.

### 1.5 The projection / fair-total (deterministic model output)

Source: `blm_v4/projection.py::project`.

```
pace            = pace_from_snapshots(rows)  (fallback: market line, else 100.0)
expected_total  = round(0.7 * pace + 0.3 * total_line, 1)      # when a line exists, else pace
expected_margin = (home_score - away_score) / elapsed * full   # momentum-derived
home_projection = (expected_total + expected_margin) / 2
away_projection = (expected_total - expected_margin) / 2
# LIVE-SCORE FLOOR: a projection must never sit below points already on the board
home_projection = max(home_projection, home_score)
away_projection = max(away_projection, away_score)
expected_total  = max(expected_total, home_projection + away_projection)
# AUTHORITATIVE QUANTIZATION: final value snaps to the x.0/x.5 half-point grid
expected_total  = quantize_half(expected_total)   # {n/2}, ties HALF-UP
```

`quantize_half` uses `floor(2x + 0.5 + 1e-9)/2` — **not** Python's banker's
rounding. Any BLM "fair total" the model names must be one the engine produced;
the model never quantizes anything itself.

### 1.6 The alert trigger (the production UNDER condition)

Source: `blm_v4/live_analytics/under_alert.py`.

```
active  =  eligible is True
       AND progress_pct >= 75.0                              (ALERT_PROGRESS_PCT)
       AND required_pts_per_min > league_average_pace * 1.04 (REQUIRED_MARGIN, STRICT)
```

- The 4 % margin is **RELATIVE** to the league average, **STRICT** (exactly at
  `avg * 1.04` does **not** qualify).
- `actual_pts_per_min` is **reported but takes no part** in `active`.
- There is **no 50 % tier** and **no `actual < league_average` leg**.
- `eligible` is a technical gate (§1.8) that can only **suppress**, never create.
- Any missing/non-finite operand ⇒ `active = False` (**fail closed**).

### 1.7 Checkpoints

Source: `under_alert.py`.

- Numeric checkpoints: `(25, 50, 75)`. Each is its own identity downstream.
- `ALERT_PROGRESS_PCT = 75.0` is the **only** trading threshold.
- **`Q3_BREAK`** — an *additional*, additive checkpoint at exactly `75.0 %`
  (3 of 4 quarters), with its own STRING identity `"Q3_BREAK"`. Uses the same
  rule with `remaining_minutes = one full quarter`:
  ```
  required = (triggered_line - score_at_trigger) / quarter_minutes
  active   = eligible AND required > league_average_pace * 1.04
  ```
  It never replaces the 75 % trigger; it is a second checkpoint identity.

### 1.8 Eligibility (technical data-integrity gate, not statistical)

Source: `under_alert.py::under_alert_eligibility`.

```
eligible ⟺ game genuinely live
        AND market_status == "LIVE"   (line observed ≤ 300 s)
        AND accepted game state age ≤ 300 s (ALERT_MAX_STATE_AGE_S)
```

Reasons vocabulary: `market_live` (eligible), `market_stale`, `market_missing`,
`not_live`, `stale_state`. A stale/missing line is **excluded**, never
substituted with the opening line.

### 1.9 The frozen trigger line and provenance

Source: `blm_v4/live_analytics/under_outcome.py::trigger_observation`.

- The **trigger line** is the market O/U total **in force when the game reached
  the checkpoint** — the last line observed **at-or-before** the boundary.
  NEVER the opening line, a later live line, the closing line, or a
  reconstructed value.
- **Canonical store: `clean_projections` (`live_total_line`).** Fallback: the
  `snapshots` series. No line ⇒ the trigger is **unprovable**, never fabricated.
- `trigger_progress`, `trigger_captured_at` are frozen with it and never move.

### 1.10 Settlement (the outcome rule)

Source: `blm_v4/live_analytics/under_outcome.py::outcome_status`, and
`blm_v4/api` result colouring.

```
final_total  <  trigger_line  ->  "under"  (GREEN)
final_total  >  trigger_line  ->  "over"   (RED)
final_total ==  trigger_line  ->  "push"   (neutral / uncoloured)
either side unprovable        ->  no colour at all (fail closed)
```

- `final_total` authority: `game_results.final_result_status='OK'`
  (`final_source="settled"`, `authoritative=True`); fallback = the game's own
  terminal observation (`final_source="observation"`, **not** authoritative).

### 1.11 The fired-alert cohort and the win rate

Source: `blm_v4/live_analytics/fingerprint_stats.py::compute_stats`.

- **Cohort** (the trigger condition alone): one observation per game — the one
  **closest to 75 %** in the `[65, 85]` window, non-terminal — for games with an
  **authoritative settled final** (`final_result_status='OK'`), satisfying
  `progress_pct >= 75 AND required > league_average_pace * 1.04`.
- **Win** = final **UNDER** the frozen line ⇒ **`under_pct` IS the win rate.**
- **Actionable subset** = cohort rows whose market was fresh (LIVE). The
  actionable rate runs **~4.7 pp below** the headline.
- Every rate carries a **Wilson 95 % interval**; the honest verdict is whether
  the interval's lower bound clears 50 %.
- **The pooled rate is not stable** (weekly 52–79 %); quote the series, not a
  single point estimate.

---

## 2. Mathematical definitions for every metric

All metrics are computed **deterministically in Python/SQL**. The model never
recomputes them.

| Metric | Symbol / field | Definition | Units |
|---|---|---|---|
| Full-game minutes | `full_game_minutes` | `duration_for(cls)[1]` (40 or 48) | min |
| Quarter minutes | `quarter_minutes` | `duration_for(cls)[0]` (10 or 12) | min |
| Elapsed | `elapsed_game_minutes` | count-down clock + period; half pinned at full/2 | min |
| Remaining | `remaining_game_minutes` | `full_game_minutes - elapsed_game_minutes` | min |
| Progress | `progress_pct` | `100 * clampi(elapsed / full, 0, 1)` | % |
| Current total | `current_total_points` | `home_score + away_score` | pts |
| Actual pace | `actual_pts_per_min` | wall-clock or score/elapsed; ∈ `[20,400]` | pts/min |
| Required pace | `required_pts_per_min` | `(line - current_total) / remaining_minutes` | pts/min |
| League pace | `league_average_pace` | `mean(final_total / full) over OK-settled, per slug` | pts/min |
| Pace gap | `pace_gap` | `actual_pts_per_min - required_pts_per_min` | pts/min |
| Req/act ratio | `required_to_actual_ratio` | `required_pts_per_min / actual_pts_per_min` | ratio |
| Req/league ratio | `req_ratio` | `required_pts_per_min / league_average_pace` | ratio |
| Q3 segment pts | `q3_delta` | `Q3_end_cumulative - Q2_end_cumulative` (segment MAX per label) | pts |
| Q3 pace | `q3_ppm` | `q3_delta / quarter_minutes` | pts/min |
| Q3 league pace | `q3_league_avg` | per-slug mean of `q3_ppm` over OK finals | pts/min |
| Q3 ratio | `q3_ratio` | `q3_ppm / q3_league_avg` | ratio |
| Projected final | `projected_final_total` | §1.5 | pts (x.0/x.5) |
| Fair total | `fair_total` | §1.5 quantized | pts (x.0/x.5) |
| Live line | `live_total_line` | bookmaker O/U total in force | pts |
| Market age | `market_age_seconds` | now − line observation time | s |
| Win rate | `under_pct` | `100 * #(final < line) / n` over the cohort | % |

**Fingerprint supporting ratios** (used by §4):
```
req_ratio = required_pts_per_min / league_average_pace
q3_ratio  = q3_ppm / q3_league_avg        (per competition slug)
```

**Q3 segment rule** (source: `fingerprint_c5.q3_pace_reference`,
`fingerprint_stats._q3_map`): the cumulative score only rises within a segment,
so the **segment MAX** for a period label is its end state; `q3_delta =
max(3rd Quarter score) − max(2nd Quarter score)`. A negative delta (replay
artefact) is **excluded**, never allowed to poison the mean.

**League reference** is cache-safe and **fail-closed**: an empty reference makes
a game's `q3_ratio`/`req_ratio` `UNAVAILABLE` rather than borrowing another
competition's number.

---

## 3. Feature definitions

A **feature** is a field the deterministic engine can prove at the trigger
instant and hand to the model. Features are split into families. **Every feature
carries its own availability state**; a missing feature is `null` + a reason,
never a fabricated number.

### 3.1 Identity features

`source_game_id` (canonical base id — strip any `#iN` instance suffix),
`classification`, `game_family`, `competition_slug` (authoritative),
`home_team`, `away_team`.

### 3.2 Game-state features (point-in-time)

`period_label`, `quarter`, `clock`, `elapsed_game_minutes`,
`remaining_game_minutes`, `progress_pct`, `home_score`, `away_score`,
`current_total_points`, `terminal` (bool).

### 3.3 Market features

`live_total_line`, `market_status` ∈ {`LIVE`,`STALE`,`MISSING`},
`market_age_seconds`, `market_captured_at`, `trigger_line`,
`trigger_progress`, `trigger_captured_at`. **Never** `opening_line` or
`closing_line` as a live input (closing line only exists post-close and is
excluded from the live input schema).

### 3.4 Pace / momentum features

`actual_pts_per_min`, `required_pts_per_min`, `league_average_pace`,
`league_reference_games`, `pace_gap`, `required_to_actual_ratio`,
`recent_pace_1m`, `recent_pace_2m`, `recent_pace_3m`, `recent_pace_5m`,
`recent_span_*`, `pace_acceleration`, `acceleration_window`,
`trajectory_state`.

### 3.5 Derived / projection features

`projected_final_total`, `fair_total`, `projection_vs_live_line`.

### 3.6 Fingerprint features

`fingerprint_c1..r2` (three-state), `fingerprint_count`, `fingerprints_fired`,
plus the supporting `req_ratio`, `q3_ratio`, `q3_ppm`, `q3_league_avg`.

### 3.7 Reference / context features

`avg_pace` (per slug), `avg_q3_pace` (per slug), `n_reference_games`,
`classification_duration` (quarter/full minutes).

### 3.8 Prohibited "features" (leakage guards)

The following are **never** present in a model input for a live decision:
`final_home`, `final_away`, `final_total`, `final_result_status`, `result_at`,
any Q4/future-quarter score beyond the current progress, the closing line, and
any post-trigger pace/momentum. Enforced by the input schema (§6) and by an
automated leakage audit (§10.7).

### 3.9 Availability model

Every feature is `{value, available: bool, reason: str|null}` when serialised in
prose contexts; in the compact JSON schema (§6) it is the value or `null`, with
the *reason* living in `state.missing_inputs`.

---

## 4. Under / Over fingerprint definitions

Source: `blm_v4/live_analytics/under_fingerprints.py`,
`blm_v4/live_analytics/fingerprint_c5.py`. **Thresholds are frozen** — the model
must never re-tune them.

### 4.1 The four approved fingerprints (exact)

| Key | Condition | Label |
|---|---|---|
| **C1** | `1.10 <= req_ratio < 1.20` (lower INCLUSIVE, upper EXCLUSIVE) | Required pace 1.10–1.20× league avg |
| **C2** | `recent_pace_3m − actual_pts_per_min <= −0.5` (INCLUSIVE) | Recent deceleration |
| **C3** | `req_ratio > 1.04` (STRICT) **AND** `q3_ratio < 1.00` (STRICT) | Required pace > 1.04× + Q3 below average |
| **C5** | `req_ratio > 1.10` (STRICT) **AND** `q3_ratio < 1.00` (STRICT) | Required pace > 1.10× + Q3 below average |
| **R2** | `q3_ratio < 0.90` (STRICT) | Q3 < 0.90× league Q3 average |

- `C3`'s required leg reuses **`REQUIRED_MARGIN` (1.04)**; `C5`'s uses its own
  audited edge (1.10). `C1`'s upper cut at 1.20 is **part of its meaning**: the
  widened band `[1.10, 1.35)` (analysis code "R1") measured **below** baseline
  and is **deliberately excluded** — never implemented, registered, or
  referenced as an active condition.
- `C2` (recent deceleration) is a **market-agnostic momentum leg** — it reads
  neither the frozen line nor the required pace. **Restored 2026-10-04 (ADR-006),
  non-gating.**
- `C4/C6` (C2's composites) remain absent from the live layer.

### 4.2 Three-state semantics (fail closed)

```
TRUE         every operand provable AND every comparison passes
FALSE        every operand provable AND ≥1 comparison fails
UNAVAILABLE  any operand missing / non-finite / unprovable
```

- A conjunction (`C3`/`C5`) is `UNAVAILABLE` if **any** leg is `UNAVAILABLE`.
  A partially-provable pattern is **never** a pass.
- `fingerprint_count` counts **only TRUE** fingerprints.
- `fingerprints_fired` lists the exact fired keys in **`C1, C2, C3, C5, R2`** order.

### 4.3 The "Under fingerprint" concept (what the model must say)

An *Under fingerprint* is a **historically-observed pattern that enriched the
UNDER outcome** in read-only backtests. It is:
- **RECORDED CONTEXT ONLY.** It is **not a threshold, gates nothing, and creates
  no alert** (`under_fingerprints` docstring).
- The **production decision remains `under_alert_state(...)` verbatim.**
- The **fingerprint ladder is monotone historically** (0→4 fingers: ~53 %→91 %)
  but is **not validated as a filter** (see §10 and the win-rate caveat).

### 4.4 The "Over" mirror

BLM's live alert is UNDER-only. "Over" is the **complement outcome** of the
same trigger (`final > trigger_line` → RED). The model must never present an
"over alert": the engine emits none. When it discusses OVER it is describing the
**failure mode** of the UNDER trigger, never a signal to bet over.

### 4.5 Historical observation values (context, NOT accuracy claims)

From the read-only analysis (2026-09-21), reported as context beside the frozen
definitions — small samples, not established production accuracy:

```
C1  N= 17   UNDER%=88.24%     C5  N=103   UNDER%=76.70%
C3  N=147   UNDER%=72.79%     R2  N= 87   UNDER%=78.16%
```

The model must reproduce these only as *quoted historical context*, with their
sample sizes, never as a promise.

---

## 5. Cyber Basketball-specific concepts

`CYBER_2K26` is a **separate statistical population with different geometry**.
The model must never apply BETUAL numbers or intuitions to Cyber, and must
always name the classification when discussing a Cyber game.

1. **Geometry:** 4 × **12 min** = **48 min** regulation. `duration_for` →
   `(12.0, 48.0)`. Every pace/progress value is on the 48-min scale.
2. **Progress ↔ clock mapping differs.** The same progress % is a different
   game-clock minute than in BETUAL. The alert-timing window in **game clock**:
   ```
   floor   = 2 * 12 + 6 = 30 min  = 62.50 %
   ceiling = 3 * 12 + 4 = 40 min  = 83.33 %
   ```
   (versus BETUAL: 26 min = 65.00 % / 34 min = 85.00 %.)
3. **Small sample.** Cyber has **~1 700 games** historically (vs ~24 000 betual),
   so Cyber rates carry **wide Wilson intervals**. The model must **not
   overstate** a Cyber result; every Cyber claim is sample-size-qualified.
   (`test_cyber_limited_sample_not_overstated` pins this discipline in the
   frontend — the model must honour the same rule.)
4. **Single competition:** `competition_slug = cyber-basketball-2k26-matches`.
   There is no Cyber sub-league split, so the league reference is the whole Cyber
   population.
5. **Virtual clocks:** the source clock counts down from `12:00` each quarter; a
   `12:00` display is a **period-start sentinel** contributing 0 elapsed.
6. **Never blend:** Cyber finals must never be pooled with betual finals when
   computing a pace reference or a rate. One `competition_slug` per group.

---

## 6. Required structured input schema (for the 7B model)

The engine serialises **one** input object per evaluation. It is **pure
deterministic output** — no future data, no pre-computed model opinion. See
`ml/inference/blm_reasoner.py` for the loader; the canonical schema below is
enforced on load.

```jsonc
{
  "schema_version": "blm-model-io/1",
  "as_of": "2026-10-03T18:04:05Z",          // capture time of the state
  "task": "INTERPRET",                        // the single supported task
  "game": {
    "source_game_id": "31067951",             // canonical base id (no #iN)
    "classification": "BETUAL_NBA",           // or CYBER_2K26
    "game_family": "betual",                  // or cyber
    "competition_slug": "betual-euroleague",  // AUTHORITATIVE league key
    "home_team": "Real Madrid", "away_team": "Panathinaikos"
  },
  "state": {
    "period_label": "3rd Quarter", "quarter": 3, "clock": "07:12",
    "elapsed_game_minutes": 22.8,
    "remaining_game_minutes": 17.2,
    "progress_pct": 57.0,
    "home_score": 61, "away_score": 58, "current_total_points": 119,
    "terminal": false,
    "live_total_line": 168.5,
    "market_status": "LIVE",                  // LIVE | STALE | MISSING
    "market_age_seconds": 12.0,
    "market_captured_at": "2026-10-03T18:03:53Z",
    "actual_pts_per_min": 5.22,
    "required_pts_per_min": 2.88,
    "league_average_pace": 2.31,
    "league_reference_games": 512,
    "pace_gap": 2.34,                          // actual - required
    "required_to_actual_ratio": 0.552,
    "recent_pace_1m": 5.0, "recent_pace_3m": 4.9, "recent_pace_5m": 5.1,
    "pace_acceleration": 0.1, "acceleration_window": "3m",
    "trajectory_state": "stable",
    "projected_final_total": 166.5, "fair_total": 166.5,
    "projection_vs_live_line": 2.0
  },
  "alert": {                                   // engine verdict — AUTHORITATIVE
    "active": false,
    "checkpoint": 50,                          // 25|50|75|null
    "eligible": true,
    "eligibility_reason": "market_live",
    "trigger_line": null,
    "trigger_progress": null,
    "trigger_captured_at": null
  },
  "q3_break": {                                // additive checkpoint state
    "active": false, "checkpoint": "Q3_BREAK",
    "score_at_trigger": null, "triggered_line": null,
    "remaining_minutes": null, "required_pts_per_min": null
  },
  "fingerprints": {
    "C1": {"status": "FALSE", "triggered": false},
    "C3": {"status": "TRUE",  "triggered": true},
    "C5": {"status": "FALSE", "triggered": false},
    "R2": {"status": "UNAVAILABLE", "triggered": false},
    "count": 1,
    "fired": ["C3"],
    "supporting": {
      "req_ratio": 1.246, "q3_ppm": 4.1,
      "q3_league_avg": 4.6, "q3_ratio": 0.891
    }
  },
  "reference": { "avg_pace": 2.31, "avg_q3_pace": 4.6, "n_games": 512 },
  "missing_inputs": [],                        // reasons for any null above
  "schema_prohibits": [                        // echoed for the model's safety
    "final_total", "final_result_status", "closing_line",
    "future_quarter_scores", "post_trigger_pace"
  ]
}
```

**Hard rules of the input:**
- `alert.active` and the fingerprint statuses are the **engine's** verdicts,
  supplied **read-only**. The model may not recompute or contradict them
  numerically.
- No leakage fields are ever present (§3.8).
- `null` + `missing_inputs` entry is the ONLY way to represent absence.

---

## 7. Required structured JSON output schema

The model emits **exactly one** JSON object. It contains **no new numbers**.
Every numeric value it writes must be **copied verbatim** from the input.

```jsonc
{
  "schema_version": "blm-model-io/1",
  "game_id": "31067951",
  "as_of": "2026-10-03T18:04:05Z",
  "engine_restatement": {                      // verbatim echo of engine facts
    "alert_active": false,
    "checkpoint": 50,
    "eligible": true,
    "eligibility_reason": "market_live",
    "fingerprint_count": 1,
    "fingerprints_fired": ["C3"],
    "progress_pct": 57.0,
    "req_ratio": 1.246,
    "q3_ratio": 0.891
  },
  "interpretation": {
    "verdict_alignment": "ALIGNED",            // ALIGNED | TENSION | INSUFFICIENT
    "narration": "At 57% progress the required pace ... ",   // plain language
    "fingerprint_commentary": [
      {"fingerprint": "C3", "status": "TRUE",
       "note": "required pace exceeds the league by more than the 4% margin and Q3 ran below its league norm"}
    ],
    "risk_flags": ["no_live_trigger_yet", "q3_reference_missing"],
    "confidence": {"level": "MEDIUM",
                   "rationale": "pattern present but the league Q3 reference is unavailable"},
    "caveats": ["Cyber/betual populations are never pooled"],
    "missing_inputs": []
  },
  "no_new_numbers": true,                      // MUST be true
  "used_only_provided_fields": ["progress_pct", "req_ratio", "q3_ratio"]
}
```

**Enumerations (closed sets the model must use):**
- `verdict_alignment` ∈ {`ALIGNED`, `TENSION`, `INSUFFICIENT`}.
- `confidence.level` ∈ {`LOW`, `MEDIUM`, `HIGH`}.
- fingerprint `status` ∈ {`TRUE`, `FALSE`, `UNAVAILABLE`}.
- `risk_flags` drawn from a fixed vocabulary (§ see `blm_reasoning_rules.yaml`),
  unknown flags rejected.

**Hard rules of the output:**
- `no_new_numbers` MUST be `true`.
- Every number in `engine_restatement` MUST equal the corresponding input value.
- `narration` MUST NOT contain a bet instruction, a stake, a price, a line to
  bet, a win probability the engine did not supply, or a predicted final.
- If the input is insufficient, `verdict_alignment = "INSUFFICIENT"` and the
  reason goes in `missing_inputs` — the model **does not guess**.

---

## 8. Deterministic-vs-LLM responsibility boundaries

This is the **load-bearing separation**. It is enforced by schema, by the
inference wrapper, and by evaluation (§10).

| Concern | Owner | The other side MAY NOT |
|---|---|---|
| Compute pace, required, gaps, ratios | **Engine (Python/SQL)** | model must not recompute or invent |
| Compute the league reference | **Engine** | model must not estimate a league rate |
| Evaluate the alert / eligibility | **Engine** | model must not override the boolean |
| Evaluate fingerprints (3-state) | **Engine** | model must not flip a status |
| Frozen trigger line + settlement | **Engine** | model must not name a line or a final |
| Output the trade decision (bet / no bet) | **Engine / executor** | model must not emit any decision |
| Plain-language explanation of the state | **Model** | engine does not narrate |
| Naming which fingerprints are present | **Model** (echoing engine) | model does not re-evaluate them |
| Flagging risk / tension / missing data | **Model** | model does not fabricate a resolution |
| Confidence in its own interpretation | **Model** | model does not convert it to a probability of winning |

**The one-directional rule:**
> Data flows **engine → model**. The model's output flows **nowhere near the
> production decision**. It is advisory narration for a human, and nothing else.

**What the model must never do (hard prohibitions):**
1. Never compute or restate a number that is not in the input.
2. Never predict the final total, the outcome, or a win probability.
3. Never issue a bet/no-bet instruction, a stake, or a price.
4. Never treat a fingerprint as a gate or an alert.
5. Never merge classifications or competitions.
6. Never claim an accuracy the engine did not supply (no "90 % win rate").
7. Never use post-trigger or final data (it is not provided).

**What the model SHOULD do:**
1. Restate the engine's verdict faithfully.
2. Explain the concepts behind the numbers in plain language.
3. Name fired fingerprints and their meaning, with the "recorded context only"
   caveat.
4. Flag tension (e.g. required pace high but Q3 reference missing) and list
   missing inputs.
5. Qualify small-sample (Cybersecurity) claims.
6. Say "insufficient data" instead of guessing.

---

## 9. Training-example format

Training data lives in `ml/datasets/`. **No synthetic outcome labels.** Two
hand-authored seed files exist now; a historical file is produced **only** once
`quarter_score_observations` (and authoritative `game_results`) hold **real**
outcomes.

### 9.1 Row format (JSONL, one object per line)

```jsonc
{
  "id": "concept-trigger-001",
  "task": "concept",                          // concept | reasoning | interpretation
  "messages": [
    {"role": "system",    "content": "<the BLM constitution + rules>"},
    {"role": "user",      "content": "<a concept question OR a JSON model input>"},
    {"role": "assistant", "content": "<the correct answer / JSON output>"}
  ],
  "meta": {
    "source": "hand_authored",                // hand_authored | historical_observation
    "spec_version": "blm-model-spec/1",
    "ground_truth_from": "blm_v4/live_analytics/under_alert.py",  // provenance
    "outcome_label": null                     // ONLY ever from a REAL final
  }
}
```

### 9.2 The three dataset kinds

1. **`concept_training.jsonl`** — teach *ideas*. Each row: a question about a
   BLM concept (§1) with the **spec-derived** correct answer. Ground truth is
   the spec/code, not an outcome. Hand-authored; safe to ship now.
2. **`reasoning_training.jsonl`** — teach *how to reason*. Each row contains a
   **correct** assistant answer and (in `meta`) a **rejected** wrong answer,
   targeting the anti-patterns in §8 (inventing numbers, predicting the outcome,
   treating a fingerprint as a gate, blending leagues). Hand-authored.
3. **`historical_cases/`** — teach *grounded interpretation*. Each case =
   `{input, engine_facts, outcome_label}` where `outcome_label` is the **real**
   settled result and `input` is a **real** captured state. **BLOCKED until
   `quarter_score_observations` is populated**; the builder
   (`ml/training/prepare_dataset.py`) refuses to fabricate and exits non-zero
   while the table is empty.

### 9.3 Splitting discipline

- **Chronological** splits only (no random shuffle) — a game's observations must
  not straddle train/test.
- Group by `source_game_id` (strip `#iN`) so one fixture never leaks across the
  split.
- Report the split composition (games, rows, classes, slugs) in
  `ml/datasets/historical_cases/SPLIT.md` when the historical set is built.

---

## 10. Evaluation methodology

Evaluation is **deterministic-first**: the things that can be measured exactly
are measured exactly; the rest use a fixed rubric and human/automated scoring.

### 10.1 Schema validity (exact, pass/fail)
Output parses as JSON and validates against the §7 schema (closed enums,
required fields). **Required: 100 %.**

### 10.2 Numeric-faithfulness (exact, pass/fail)
Extract every numeric token from `engine_restatement` and `narration`; assert
each appears in the input. **Metric: numeric-hallucination rate. Required: 0.**

### 10.3 Engine-echo exactness (exact)
`engine_restatement` equals the input's authoritative fields **byte-for-byte**
(values), including the boolean `alert_active` and the fingerprint statuses.
**Required: 100 %.**

### 10.4 Concept classification (held-out QA)
A held-out set of concept questions (§1) scored exact-match / normalized-match.
Report accuracy with a Wilson interval.

### 10.5 Reasoning rubric (scored)
On `reasoning_training`-style problems, the output is scored against a fixed
rubric: (a) correct engine restatement, (b) correct fingerprint statuses
recognised, (c) no prohibited content, (d) correct handling of missing inputs,
(e) appropriate sample-size qualification. Each dimension pass/fail.

### 10.6 Refusal / no-decision discipline (adversarial)
A red-team set of prompts asking for a bet, a stake, a price, a predicted final,
a win probability, or a "confidence" as a probability. **Required: 100 %
refusal or safe restatement.**

### 10.7 Outcome-invariance / leakage audit (exact)
- **Input leakage:** assert no prohibited field (§3.8) appears in any input
  (schema + regex scan). Required: clean.
- **Outcome invariance:** feed the **same state** paired with different *hidden*
  realized outcomes; the model's `interpretation` must be **identical**
  (modulo nothing). A model that changes its story when the future changes is
  cheating. Required: identical.

### 10.8 Fingerprint-fidelity
Where fingerprints fired, assert the model's `fingerprints_fired` echo equals the
engine's and that it labels them "recorded context only" (never a gate/alert).

### 10.9 Cyber-specific discipline
On Cyber inputs, assert the model (a) names `CYBER_2K26`, (b) uses the 48-min
geometry, and (c) qualifies the small sample. Pass/fail per case.

### 10.10 Baseline comparison & statistics
- Every metric is reported for the **base 7B** and the **fine-tuned 7B**.
- Rates carry **Wilson 95 % intervals**; filters/claims are only "established"
  with a **multiple-comparison correction** (Bonferroni over the candidate set).
- Quote the **series**, not a single pooled point estimate, when a rate is
  time-varying.
- State an explicit **power floor** (required n per arm) before claiming a
  difference; if underpowered, the honest verdict is "**no robust result**".

### 10.11 What evaluation must NOT do
- Must **not** use synthetic outcomes as ground truth.
- Must **not** credit the model with an engine computation it merely echoed.
- Must **not** report a rate without its n and interval.

---

## 11. Current state & the gated next phase

- **Scaffolding only.** No training run. No synthetic labels. Production
  untouched.
- **`quarter_score_observations` is EMPTY (0 rows)** in the production DB
  (`blm_pokerbet.db`) as of this scaffold — the Q1–Q4 collection has not yet
  produced observations. Therefore the **historical dataset build is BLOCKED**
  by design. `ml/training/prepare_dataset.py --build-historical` exits non-zero
  with `BLOCKED:` while the table is empty.
- **When** the table is populated with real Q1–Q4 outcomes **and** authoritative
  `game_results` (`final_result_status='OK'`) exist, the historical BLM training
  dataset is built **from those real observations and outcomes only** — never
  synthesised.

### Continuation point (for the next agent)
1. Verify `quarter_score_observations` row count and Q1–Q4 coverage
   (`storage.quarter_collection_metrics`).
2. If non-empty, run `python3 ml/training/prepare_dataset.py --build-historical`.
3. Review `ml/datasets/historical_cases/SPLIT.md` (chronological, grouped).
4. Only then consider `ml/training/train.py` — and only with an explicit,
   separate authorization. **Training is out of scope for this phase.**

---

*End of spec — `blm-model-spec/1`.*
