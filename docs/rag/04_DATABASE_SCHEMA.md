# 04 — Database Schema Dictionary (semantic)

Retrieval keywords: schema, database dictionary, field meaning, game_results,
snapshots, market_observations, checkpoint_market, clean_projections,
clean_observations, clean_games, result_reconciliation, result_reconciliation_state,
result_conflicts, predictions, prediction_scores, reconciliation, quarter_score_observations,
authority, never use, column meaning, which table.

**What this pack answers:** what every important field MEANS, which authority it
carries, and — critically — what it must NEVER be used for. It prevents confusing
similarly-named fields across tables.

Schemas below are the LIVE DDL read read-only from the running DBs
(2026-10-03). Where a table has migrated columns, the migration-added columns are
shown.

---

## Databases

| DB | Role | Notable tables |
|---|---|---|
| `blm_pokerbet.db` | production live pipeline | games, snapshots, market_observations, checkpoint_market, game_results, result_reconciliation, result_reconciliation_state, result_conflicts, predictions, prediction_scores, reconciliation, quarter_score_observations, game_quality |
| `blm_metrics_clean.db` | clean / analytic | clean_games, clean_snapshots, clean_observations, clean_projections, clean_market_observations, clean_meta, deviation_residuals |
| `blm_metrics_clean.db.live_analytics.db` | live-analytics cache | competition_ledger, historical_context_cache, pace_benchmark_cache |
| `blm.db`, `blm_v2.db` | V1/V2 legacy | `alerts` tables are EMPTY (v4 alerts are derived) |
| `blm_historical.db`, `blm_ts.db` | V3 research / V2 TS | research only |

---

## `games`

Identity: `UNIQUE(source, source_game_id)`. `source_game_id` (TEXT) is the
canonical identity every other table joins by convention.

| Field | Meaning | Authority |
|---|---|---|
| `source_game_id` | canonical game identity (may carry an `#iN` instance suffix) | identity key |
| `classification` | statistical population: `BETUAL_NBA` / `CYBER_2K26` / … | population key |
| `competition_slug` | authoritative LEAGUE identifier | league key |
| `competition` | display name — **UNRELIABLE** (TBSL games display "Betual NBA") | display only |
| `status` | `live` / `ended` — a STATE FLAG, never a result | state only |
| `first_seen_at` / `last_seen_at` | discovery/freshness timestamps | freshness |

**Never use** `status='ended'` as evidence of a final. **Never use** `competition`
(display name) for statistical grouping — use `competition_slug`.

---

## `snapshots` (append-only; immutable)

`UNIQUE(game_id, captured_at)`. Fields: `home_score`, `away_score`,
`period_label`, `quarter`, `clock`, `game_status`, `total_line`,
`total_over_odds`, `total_under_odds`, `q1..q4_home/away_score`, `markets_json`,
`raw_json`.

| Field | Meaning | Authority |
|---|---|---|
| `captured_at` | capture instant | ordering key |
| `total_line` | observed bookmaker O/U total (NULL on panel-only ticks) | market observation |
| `q1..q4_home/away_score` | per-quarter scores (migration-added) | data collection |
| `clock` | count-down clock `MM:SS` or `M'` | game time |

**Never use** a snapshot as a game final unless it is the terminal observation AND
no OK `game_results` row exists (then it is non-authoritative,
`final_source='observation'`). **Never** treat a `total_line`-NULL stub as "no
market" — the last non-null line persists.

---

## `market_observations`

`UNIQUE(source_game_id, market_type, line_value, captured_at)`. The market tree:
`market_type` (MatchTotal | MatchHomeTeamTotal2 | …), `line_value`, `over_price`,
`under_price`. **Line selection is by PRICE/type, never array position.**

---

## `checkpoint_market` (immutable analytical history)

`UNIQUE(source_game_id, checkpoint_pct)`. One row per (game, checkpoint).

| Field | Meaning | Settlement? |
|---|---|---|
| `opening_line` | OLV — first verified line | ❌ |
| `live_market_line` | frozen at-or-before the checkpoint | ❌ (analytical) |
| `market_timestamp` | when the frozen line was observed; NULL = never observed | freshness |
| `blm_fair_value` | `project()` recompute, frozen at first write | ❌ |
| `closing_line` | CLV — last verified line | ❌ |
| `actual_final_total` | actual final from `game_results` | ✅ |
| `market_vs_fair` | live − fair (signed, never discarded) | ❌ |
| `signal` | UNDER_VALUE / OVER_VALUE / NO_EDGE (was PUSH) | ❌ |
| `outcome` | UNDER_WIN / OVER_WIN / UNDER_LOSS / OVER_LOSS / PUSH | ✅ |
| `frozen` | 1 (row immutable) | — |
| `terminal` / `predictive_eligible` / `exclusion_reason` | terminal-checkpoint stamps | eligibility |

**Never use** `live_market_line` as the alert trigger line — the trigger line comes
from `trigger_observation`; `checkpoint_market` is an analytical/OLV/CLV store.

---

## `game_results` (the authoritative final)

`UNIQUE(source_game_id)`. `final_home`, `final_away`, `final_total`, `result_at`,
`final_result_status`, `result_source` (migration-added).

| Field | Meaning | Authority |
|---|---|---|
| `final_total` | authoritative final combined score | ✅ when `final_result_status='OK'` |
| `final_result_status` | production values seen: `OK`, `UNKNOWN`, `NEEDS_RECONCILIATION`, `INVALID` | gate |
| `result_source` | `RESULTS_PAGE` (immutable page verdict) / `swarm_feed` / NULL | provenance |

**Meaning of `final_total`.** Authoritative final combined game score.
**Authority.** Only authoritative when `final_result_status='OK'`.
**Never use** for live scoring or trigger detection. **Treat as immutable** when
`result_source='RESULTS_PAGE'` AND OK.

> ⚠️ The DDL comment on `final_result_status` reads `OK | UNKNOWN` but production
> values also include `NEEDS_RECONCILIATION` and `INVALID`. See CONTRADICTIONS.md.

---

## `result_reconciliation` (per-attempt audit) & `result_reconciliation_state` (latest)

`result_reconciliation`: `UNIQUE(source_game_id, attempt)`; `outcome` ∈
{VERIFIED, REJECTED, FAILED_ATTEMPT, TEMPLATE_FAILED, CONFLICT};
`rendered_home/away`, `final_home/away/total`, `quarter_scores` (JSON),
`match_score` (identity proof), `checks_json`, `fetched_at`.

`result_reconciliation_state`: `PRIMARY KEY(source_game_id)`; `outcome`,
`attempt`, `final_*`, `source` (`RESULTS_PAGE` | `LIVE_DOM` | `DB_EXISTING` |
`MANUAL_REVIEW`), `updated_at`.

| Field | Meaning |
|---|---|
| `outcome` | per-attempt verdict; drives the candidate re-arm |
| `attempt` | attempt count; `attempt >= 4` is impossible under the old cap |
| `checks_json` | the `validate_page_result` checks that were run |

**Never** treat `VERIFIED` state as permanent if the OK row is gone (it re-candidates).

---

## `result_conflicts` (flagged disagreements)

`UNIQUE(source_game_id)`. `live_dom_total` (stored verdict's total),
`results_total` (page-rendered total), `detail`, `flagged_at`. A flag is a job for
a human; it never overwrites a verdict. **Never auto-delete** a resolved conflict.

---

## `predictions` / `prediction_scores`

`predictions`: `UNIQUE(source_game_id, checkpoint, model_version)`; `checkpoint`
∈ `q1|q2|q3|q4|final|pctNN`; `projected_*`, `market_total`, `progress`,
`terminal`/`predictive_eligible`. `prediction_scores`: FK
`prediction_id → predictions.id`; error metrics + `ou_correct`,
`model_beat_market`, `fragment`.

**Never** score a prediction after the game is final (post-final rejected,
`DECISIONS.md` §14.J). **Never** include a terminal row in headline accuracy.

---

## `clean_games` / `clean_snapshots` / `clean_observations` / `clean_projections`

`clean_games.source_game_id` is the instance id (incl. `#iN`); `base_game_id`
strips the suffix to the fixture id. `clean_projections` carries the CANONICAL
trigger-line field `live_total_line`, `progress_pct`, `market_status`
(LIVE/STALE/MISSING), `market_captured_at`, `market_age_seconds`,
`actual_pts_per_min`, `required_pts_per_min`, `projected_final_total`,
`fair_total`, recent-pace windows, `terminal`/`predictive_eligible`.

**Meaning of `clean_projections.live_total_line`.** The canonical market total used
for the frozen trigger line (ADR-001 / Rule 1.3).

**Never use** `clean_observations`/`clean_projections` z-score or "satellite"
fields as production decisions while they are NULL (statistics are NULL until a
clean reference population is authorized).

---

## `quarter_score_observations` (data collection only)

Per-quadrant scores for a game; `source_path` ∈ `event_view | ws`. Feeds NO
alert/bet. See `06_KNOWN_FAILURE_MODES.md` (was 0 rows until 2026-10-03).

---

## `reconciliation` (BetConstruct match)

`UNIQUE(source, source_game_id)`; `checks_json`, `result` (`matched`, …). Records
the BetConstruct event correspondence, distinct from `result_reconciliation*`.

---

## Field-confusion cheat sheet (the four easy mistakes)

| Confusable pair | Use | Do NOT use |
|---|---|---|
| `games.status='ended'` vs `game_results.final_result_status='OK'` | the latter for a result | the former as a final |
| `snapshots.total_line` vs `clean_projections.live_total_line` | `clean_projections` for the frozen trigger | snapshots for the trigger (fallback only) |
| `checkpoint_market.live_market_line` vs `trigger_observation` | `trigger_observation` for the trigger | `checkpoint_market` for the trigger |
| `games.competition` vs `games.competition_slug` | `competition_slug` for grouping | display name for statistics |
