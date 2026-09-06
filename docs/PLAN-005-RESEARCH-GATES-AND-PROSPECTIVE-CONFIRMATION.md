# BLM Research Plan — Gates, Validation, and Prospective Confirmation

## Status

**Current phase: PROSPECTIVE CONFIRMATION — FROZEN / AWAITING DATA**

**Confirmation freeze:** `2026-09-06T21:00:00Z`

The research specification is frozen. No historical result may be used to retune the confirmation criteria.

## Research sequence

```text
Clean data integrity
        ↓
Observational pace / market layer
        ↓
Market ↔ trajectory residual measurement
        ↓
Historical forensic validation
        ↓
Calibration audit
        ↓
Market-age / residual interaction discovery
        ↓
Historical replication
        ↓
🔒 PRE-REGISTERED PROSPECTIVE CONFIRMATION  ← CURRENT
        ↓
Independent population / strong replication
        ↓
Only then: predictive model research
        ↓
Only after separate validation: production decision logic
```

## Frozen principles

1. **Historical data is evidence, not a source for prospective retuning.**
2. **Contemporaneous variables may use only information available at checkpoint T.**
3. **Final settlement enters only after the checkpoint for retrospective scoring.**
4. **100% terminal checkpoints are excluded from confirmation because of the previously identified terminal tautology.**
5. **`pace_gap` is observational and is not mapped directly to O/U decisions.**
6. **`market_trajectory_residual` is descriptive market-vs-trajectory divergence.**
7. **`market_forecast_error` is retrospective: `final_settled_total - live_line_at_checkpoint`.**
8. **No threshold, EV, staking, probability, or betting rule is created before the research gates are passed.**

## Prospective confirmation protocol

### Frozen residual bands

- `0–2.5`
- `2.5–5`
- `5–10`
- `10–20`
- `>20`

### Frozen market-age bands

- `0–5s`
- `5–10s`
- `10–20s`
- `20–30s`
- `30–60s`
- `60–120s`
- `120–300s`
- `>300s`

### Frozen nominated confirmation cells

| Side | Residual magnitude | Market age |
|---|---:|---:|
| OVER | 5–10 | >300s |
| OVER | 10–20 | >300s |
| OVER | >20 | >300s |
| UNDER | 10–20 | 0–5s |

These cells were nominated from the historical freshness/interaction research and are immutable for the prospective test.

### Frozen pass criterion

A nominated cell passes only when all three conditions hold:

- `N ≥ 30`
- win rate `≥ 0.60`
- Wilson 95% lower bound `> 0.50`

Overall confirmation requires:

- all three stale-OVER cells to pass; and
- at least one fresh-UNDER cell to pass.

If insufficient data exists: `INSUFFICIENT_PROSPECTIVE_N`.

If no post-freeze observations exist: `AWAITING_PROSPECTIVE_DATA`.

## Temporal firewall

A game can enter prospective population C only when:

```text
checkpoint_timestamp > CONFIRMATION_FREEZE_TIMESTAMP
AND
first_checkpoint_of_game > CONFIRMATION_FREEZE_TIMESTAMP
```

A game with any checkpoint before the freeze remains historical in its entirety. This prevents a partly observed game from leaking later checkpoints into the prospective population.

The historical chronological walk-forward population is explicitly population B. It is **not prospective** and can never contribute to the confirmation verdict.

## Required confirmation controls

The prospective report must include:

- checkpoint-weighted performance
- game-weighted performance
- chronological blocks fixed before evaluation
- checkpoints 10–90%
- identical-row baselines
- Wilson confidence intervals
- residual × age interaction analysis
- post-settlement MFE diagnostic
- duplicate and ordering checks
- future-leakage checks
- terminal exclusion checks
- deterministic rerun verification

No 100% checkpoint is confirmation evidence.

## Research interpretation

The historical interaction result is classified:

**REPLICATED — NOT STRONG REPLICATION**

Historical findings included a stale-line OVER magnitude gradient and a separate fresh-line UNDER component. Game-level weighting attenuated the historical rates, but the relationships remained directionally positive. The secondary CYBER_2K26 population did not contain enough OVER observations to provide independent confirmation.

These findings are research relationships only. They are **not an edge and not a betting signal**.

## Production freeze

During prospective confirmation, the following remain frozen:

- `projection.py`
- `scorecard.py`
- `deviation.py`
- calibration logic
- collector behavior
- production direction logic
- betting/signal/staking logic

The confirmation harness is read-only with respect to production decision logic.

## Exit gates

### Gate 1 — Prospective confirmation

Must pass using post-freeze games only.

### Gate 2 — Independent replication / strong replication

Requires an independent population or sufficiently large independent future sample with robust interaction and no material degradation across chronological blocks.

### Gate 3 — Predictive research

Only after confirmation should predictive modeling be considered. Any model must use strict chronological/walk-forward evaluation and must remain separate from the frozen observational layer.

### Gate 4 — Production consideration

Only after predictive research demonstrates durable out-of-sample performance should production decision logic be considered. Betting thresholds, EV, probability, and staking remain prohibited until separately authorized and validated.

## Current state

```text
HISTORICAL DISCOVERY       COMPLETE
HISTORICAL VALIDATION      COMPLETE
INTERACTION REPLICATION    COMPLETE
PROSPECTIVE SPEC           FROZEN
PROSPECTIVE DATA           0 rows at freeze
CURRENT STATUS             AWAITING PROSPECTIVE DATA
```
