# BLM — Betting Logic Model

A production-grade quantitative sports analytics platform for live basketball market analysis. BLM captures timestamped live-game observations, market state, pace/trajectory measurements, research diagnostics, and reproducible validation results.

**Current research state: PROSPECTIVE CONFIRMATION — FROZEN / AWAITING DATA**

**Confirmation freeze:** `2026-09-06T21:00:00Z`

> The current research relationship is **REPLICATED**, not yet an edge or betting signal. The prospective confirmation specification is frozen and cannot be retuned using future results.

## Research Status

```text
Clean-data integrity                 COMPLETE
Observational pace / market layer    COMPLETE
Market ↔ trajectory residual         COMPLETE
Historical forensic validation       COMPLETE
Calibration audit                    COMPLETE
Residual × market-age discovery      COMPLETE
Historical replication               COMPLETE
Prospective specification            FROZEN
Prospective confirmation             AWAITING DATA
Predictive model                     NOT STARTED
Production betting logic             FROZEN / NOT AUTHORIZED
```

### Frozen prospective protocol

The prospective test uses only genuinely unseen post-freeze games. A game enters the prospective population only when both its checkpoint timestamp and its first checkpoint are strictly after the confirmation freeze. A game with any pre-freeze checkpoint remains historical in its entirety.

**Residual bands:** `0–2.5`, `2.5–5`, `5–10`, `10–20`, `>20`

**Market-age bands:** `0–5s`, `5–10s`, `10–20s`, `20–30s`, `30–60s`, `60–120s`, `120–300s`, `>300s`

**Frozen nominated cells:**

| Side | Residual magnitude | Market age |
|---|---:|---:|
| OVER | 5–10 | >300s |
| OVER | 10–20 | >300s |
| OVER | >20 | >300s |
| UNDER | 10–20 | 0–5s |

A cell passes only if `N ≥ 30`, win rate `≥ 60%`, and Wilson 95% lower bound `> 50%`. Overall confirmation requires all three stale-OVER cells and at least one fresh-UNDER cell to pass.

**100% terminal checkpoints are excluded** from confirmation because the historical audit identified a terminal tautology.

See [`docs/PLAN-005-RESEARCH-GATES-AND-PROSPECTIVE-CONFIRMATION.md`](docs/PLAN-005-RESEARCH-GATES-AND-PROSPECTIVE-CONFIRMATION.md) for the complete research gate and confirmation protocol.

## Historical Research Finding

The historical BETUAL_NBA analysis identified a residual × market-age relationship:

- Stale-line OVER performance increased monotonically with residual magnitude: approximately `.44 → .78` across the frozen magnitude bands.
- Stale-OVER residual magnitude `≥5` was `63.7%` checkpoint-weighted and `59.8%` game-weighted.
- Fresh-line UNDER was `67.0%` checkpoint-weighted and `62.2%` game-weighted.
- The relationship survived chronological, checkpoint, half-split, and game-level controls.
- CYBER_2K26 did not contain enough OVER observations for independent confirmation.

These are historical research results. They are **not production betting performance** and do not authorize thresholds, EV, probability, or staking.

## V4 — PokerBet Live Basketball Pipeline

BLM's production game source is PokerBet.co.za (BetConstruct). The V4 collector discovers live basketball games from the live panel, classifies each into an isolated statistical population, snapshots market state, and reconciles against the underlying event.

```bash
# One-shot live capture
python -m blm_v4.collector --once --ticks 1

# Continuous collector — systemd: blm-collector.service
# API server — systemd: blm-server.service
```

### Classifications

| Classification | Population |
|---|---|
| `CYBER_2K26` | Cyber Basketball 2K26 |
| `BETUAL_NBA` | Betual NBA |

Historical/statistical processing is scoped per classification; populations are never mixed.

## Core Measurement Vocabulary

- `pace_gap` — required pace minus actual pace; observational only.
- `market_trajectory_residual` — live market line minus projected trajectory; descriptive market-vs-trajectory divergence.
- `market_forecast_error` — final settled total minus live market line at checkpoint; retrospective bookmaker forecast-error measurement.
- Final outcomes are attached only after settlement.
- Contemporaneous classification uses only information available at checkpoint T.

## Validation and Safety Gates

The project follows a strict research sequence:

```text
Observation
  ↓
Measurement
  ↓
Forensic audit
  ↓
Historical validation
  ↓
Calibration
  ↓
Interaction discovery
  ↓
Replication
  ↓
Prospective confirmation
  ↓
Independent replication
  ↓
Predictive research
  ↓
Production consideration
```

No stage may silently promote an observational relationship into production decision logic.

## API / Dashboard Research Surface

Current research surfaces include deviation analysis, calibration, freshness analysis, interaction validation, and the frozen prospective-confirmation report.

The prospective confirmation endpoint is:

`/api/v4/scorecard/prospective-confirmation`

The dashboard section is:

`PROSPECTIVE CONFIRMATION — FROZEN SPEC`

## Tests

The latest freeze audit reported:

- **400 tests passed**
- `dashboard.js` syntax check passed
- confirmation harness temporal firewall verified
- specification SHA256 tamper evidence verified
- terminal exclusion verified
- historical/prospective separation verified
- deterministic confirmation report verified

## Repository Documentation

- [`docs/PLAN-005-RESEARCH-GATES-AND-PROSPECTIVE-CONFIRMATION.md`](docs/PLAN-005-RESEARCH-GATES-AND-PROSPECTIVE-CONFIRMATION.md) — current phase and frozen prospective protocol
- [`docs/`](docs/) — architecture, API, schema, research directives, and implementation plans

## Engineering Principles

- Historical data is the primary source of truth.
- Every observation is timestamped and reproducible.
- No future information may enter contemporaneous decisions.
- Presentation never contains business logic.
- Business logic never contains scraping logic.
- Statistical populations remain isolated.
- Research specifications are frozen before prospective confirmation.
- Production changes require explicit validation gates.

## License

Proprietary — Red Cape Technologies (Pty) Ltd
