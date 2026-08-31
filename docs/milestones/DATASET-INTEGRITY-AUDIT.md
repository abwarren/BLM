# DATASET INTEGRITY AUDIT — checkpoint_market (M009-M4 baseline)

Date: 2026-08-31 ~19:10Z
Auditor: independent (not the M5 implementation agent)
Scope: `checkpoint_market` and its data dependencies (`games`, `snapshots`,
`market_observations`, `game_results`, `game_quality`, `predictions`,
`market_history`).

## Baseline

- Baseline commit (as authorized): `e42b7bc` (docs) / `c0106a3` (code) — M009-M4.
- Working tree at audit start: clean except untracked `tests/test_m009_m5_disparity.py`
  (the M5 agent's file — not touched).
- DURING the audit (21:04 SAST) the parallel M5 agent committed `36f09c0` +
  `c842d87` and deployed (blm-server + blm-collector restarted 21:04:33 SAST).
  Working tree now also carries their uncommitted edits to
  `blm_v4/scorecard.py` and `tests/test_m009_m5_disparity.py`.  None of this
  audit's findings depend on M5; the M5 diff is additive (edge_buckets
  extension + /scorecard/events endpoint) and does not touch the
  checkpoint_market record path.

## Method

- Read-only SQL over a COPY of the production DB (`/tmp/blm_audit.db`, copied
  19:03:54Z from `/home/gdi/BLM/blm_pokerbet.db`; production file never opened
  directly) + the running service's HTTP API (curl :2262) for cross-checks.
- The checkpoint freeze logic (`_frozen_market_obs`: last snapshot
  `total_line` at-or-before, else lowest line of latest WS MatchTotal batch
  at-or-before) was re-implemented standalone in the audit script and
  re-derived for ALL 4,654 rows, then diffed against stored values.
- Freshness semantics treated as authoritative: LIVE = market age ≤ 300s
  (`BLM_MARKET_STALE_SECONDS`), STALE > 300, MISSING = no observation;
  STALE_DIFFERENTIAL never a live edge.
- No code changes were made.  No M009-M5 files were modified.

## Checks performed (15 categories)

| # | Category | Result |
|---|----------|--------|
| 1  | Duplicate checkpoint rows | PASS — 0 duplicates; UNIQUE(source_game_id, checkpoint_pct) holds |
| 2  | Duplicate game/checkpoint fragments | PASS — 0 dup source_game_id in games; 0 dup (game_id,captured_at) in snapshots; every pct has its own snapshot (0 collapses) |
| 3  | Missing checkpoint observations | PASS — all 471 eligible games have rows (0 missing); 42 games have <10 pcts, all verified as honest capture-gap / ±5pp-tolerance skips |
| 4  | Impossible checkpoint ordering | FAIL — 1 game (`30745013`): pct20 ts 16:26:19Z after pct30 ts 16:17:29Z |
| 5  | Impossible timestamps | PASS — 0 future / NULL / unparseable / pre-2026; recorded_at ≥ checkpoint_ts; 0 checkpoint_ts < first_seen_at |
| 6  | Market ts after checkpoint (look-ahead) | PASS — 0 rows with market_timestamp > checkpoint_timestamp |
| 7  | Stale classified as live/fresh | PASS (vacuous) — NO row has a market_timestamp, so the freshness classifier returns MISSING for all 4,654; nothing is mislabeled live (see #15) |
| 8  | Missing market lines | PASS — live NULL 687 (14%), opening NULL 205, closing NULL 205; pct100 opening⇒live consistency holds; per-pct missing matches WS-feed-era history |
| 9  | Missing BLM predictions | PASS — blm_fair_value NULL = 0; fair-vs-predictions divergence on pct10..90 = 0 |
| 10 | Invalid/contaminated games in headline | PASS — 0 INVALID / <15-snap / not-Q1 / fragment / not-OK games in checkpoint_market or market_history |
| 11 | Inconsistent final results | PASS — 0 cm-vs-game_results mismatches; 0 market_history-vs-game_results; 0 OK games with |result − last-snapshot| > 4 |
| 12 | Checkpoint pct outside 10–90/final structure | FAIL (bounded) — pct VALUES are exactly {10..100}, but 59 rows' checkpoint POSITION is mislabeled by the `12:00` sentinel (details below) |
| 13 | Multiple observations same game/checkpoint | PASS — identical to #1 (UNIQUE enforces one) |
| 14 | Silent rewrites of historical rows | PASS — id order == pct order for every game; frozen=1 on all rows; single-batch recording per game; INSERT OR IGNORE semantics; fair==rebase predictions ⇒ no rewritten values |
| 15 | Freshness metadata vs underlying timestamps | FAIL — market_timestamp NULL on ALL 4,654 rows while re-derivation yields a timestamp for 3,967 (headline finding below) |

## FAILURE DETAILS

### F1 (category 15 + explains 7) — M3/M4 freshness + momentum metadata is entirely absent in production

- Affected: `checkpoint_market.market_timestamp`, `.momentum_state`,
  `.momentum_strength`, `.false_momentum`, `.false_momentum_confidence`,
  `.quarter`, `.progress`, `.elapsed_minutes` — all NULL on 4,654/4,654 rows.
- Example: game `30745166` (recorded 18:30:20Z, last batch in the table) —
  9 rows with frozen WS lines (227.5/215.5/219.5/220.5/224.5/207.5/206.5/203.5/203.5)
  and 234 raw `market_observations` behind them, yet `market_timestamp` NULL
  on every row.
- Why: ZERO checkpoint_market rows have been recorded since the M3/M4 code
  deployed.  Last `recorded_at` in the table = 2026-08-31T18:30:20Z; M3 was
  committed 18:32:45Z, M4 18:49:55Z; no game has completed since.  The
  deployed record path (`_write_checkpoint_row`) DOES write these columns —
  verified in code (M5 diff is additive; the INSERT is unchanged).  This is a
  data-age gap, not a code defect.
- Consequence: the running API's freshness split is `n_live=0, n_stale=0,
  n_missing=n_fair` at every checkpoint and `avg_market_age=None`; every
  freshness/momentum-derived metric in the M3/M4/M5 layers (market_freshness,
  LIVE_EDGE gating, momentum capture, M5 fresh_n/stale_n, /events
  market_status) is empty/vacuous on production data.  Re-derivation from the
  underlying snapshots/observations shows the data WOULD split
  LIVE 3,297 / STALE 670 / MISSING 687 (ages: p25 73s, median 160s, p75 254s,
  max 1,294s) — a healthy mix that the stored metadata currently cannot
  express.
- Headline contamination: NONE in the misclassification sense (nothing is
  wrongfully presented as fresh — everything is honestly MISSING).  The
  freshness layer simply has no production sample yet.
- Remediation: no code fix required.  First post-M4 completions (games in
  flight ~19:30Z) will populate the columns.  Optional, on user decision: a
  one-time metadata backfill of `market_timestamp` from the raw sources is
  FEASIBLE and safe (the re-derivation is at-or-before by construction and
  line values already match 0/4,654 mismatches) — but it rewrites frozen
  rows' metadata and must be a deliberate, tested decision, not a silent fix.

### F2 (categories 12 + 4) — `12:00` period-boundary sentinel misplaces 59 checkpoint positions

- Affected: `checkpoint_market.checkpoint_pct` vs the snapshot's true game
  position.  `clock_minutes(q, '12:00')` computes `(q−1)·10 + (10−12)`, i.e.
  treats the boundary sentinel as "12 min remaining" instead of period start
  / boundary.
- 59/4,654 rows (1.27%):
  - 44 rows pct70 ← `4th Quarter 12:00` (true position 75%): e.g.
    `30741395` pct70 (ts 22:50:29Z) — the selection chose this row (formula
    progress 0.7, dist 0) over `3rd Quarter 01:30` (true 71.25%, formula
    0.7125, dist 1.25pp), which is genuinely closer to 70%.
  - 14 rows pct20 ← `2nd Quarter 12:00` (true 25%).
  - 1 row pct20 ← `Half End 12:00` (true 50%): `30745013` pct20
    (ts 16:26:19Z, fair 165.7 vs actual final 205) — a half-time projection
    labeled and aggregated as a 20% checkpoint.
- Why it matters: per-checkpoint aggregation at pct20/pct70 mixes projections
  frozen 5pp late (58 rows) or 30pp late (1 row) into the checkpoint's
  averages and position win rates; the game-detail timeline lists the
  half-time row at pct20 ahead of the pct30 row that is genuinely earlier in
  the game.  This is also the root cause of the category-4 ordering FAIL
  (30745013: pct20 ts 16:26:19Z > pct30 ts 16:17:29Z).
- Headline contamination: bounded.  59/4,654 rows; the 44 pct70 rows are
  within the ±5pp tolerance band of the label (75 vs 70) but displaced a
  genuinely closer selection; the Half End row is the only egregious case
  (30pp).
- Remediation (code, for FUTURE rows only): special-case the sentinel in
  `clock_minutes`/`_progress_of` — `12:00` at a period start/boundary means
  the period clock has not ticked: elapsed = `(q−1)·10`; `Half End` = 20 min.
  This is an isolated, clearly-safe fix, but the task says document rather
  than implement — NOT implemented.  The 59 frozen rows stay frozen (design);
  recommend documenting them (this report) and optionally excluding the
  Half End row's pct from position stats on user decision.  M5's
  /scorecard/events will expose the mislabeled positions as-is until then.
- Related metadata gap: `checkpoint_market.quarter/progress/elapsed_minutes`
  are NULL on all rows because `project()` derives them from
  `clock_minutes(quarter, clock)` WITHOUT the period-label fallback that
  `_progress_of` uses for selection — so the table stores no usable position
  evidence, and the sentinel distortion is invisible in stored data.

### F3 (category 3, benign — documented, not a failure)

42/471 eligible games have <10 distinct pcts (28×9, 14×8).  Missing-pct
patterns are varied ((10,)×18, (30,)×5, (80,90)×7, (50,)×4, (20,30)×3,
(10,30)×4, (60,)×1) and each is explained by capture timing: e.g. `30741372`
missing pct30 because its snapshots jump from `1st Quarter 01:45`
(progress 0.206) to `2nd Quarter 04:45` (0.381) across a 7-minute wall-clock
capture gap (22:13→22:20Z) — nothing falls in [0.25, 0.35], so the ±5pp
tolerance correctly skips.  No data loss, no corruption; honest N by design.

## Supporting evidence

- Duplicates: 0 (UNIQUE enforced); games table 0 duplicate source_game_id;
  snapshots 0 duplicate (game_id, captured_at).
- Look-ahead: 0 rows market_ts > checkpoint_ts; re-derived (line, ts) pairs
  are at-or-before by construction and match stored LINES exactly (0/4,654
  line mismatches).
- Final results: cm.actual == game_results.final_total (0 mismatches);
  market_history == game_results (0); OK games' last-snapshot sum within 4
  pts of the recorded final (0 exceed).
- Headline population: 471 eligible games (OK + ≥15 snaps + Q1 start +
  not-INVALID) = the 471 games present in checkpoint_market; market_history
  (471 rows) draws from the same eligible set.
- Rewrites: per-game id order == pct order (0 anomalies); frozen=1 on all;
  recorded_at clusters per game (single batch); fair-vs-predictions
  divergence 0 on pct10..90 (also confirms model math unchanged since the
  rows were frozen).
- API cross-check: /api/v4/scorecard market_vs_fair (running server):
  n_live=0/n_stale=0/n_missing=n_fair at every pct, avg_market_age None;
  /api/v4/game/30745166 serves 9 mvf rows matching the copy;
  /api/v4/game/30745013 serves the Half End row at pct20 (fair 165.7) before
  pct30 (fair 152.9) — the mislabel is visible through the API.

## Tests

Full suite: **266 passed / 0 failed** (18.7s) — the 246 canonical M009-M4
tests plus the M5 agent's 20.  No regressions.

## Bottom line

The checkpoint_market dataset is structurally sound and trustworthy for the
core Market-vs-Fair analytics: no duplicates, no look-ahead, no contaminated
games, no inconsistent finals, no rewrites, frozen lines correct 100% of the
time.  Two bounded caveats: (1) the M3/M4 freshness/momentum columns have
zero production rows so far (first completions will populate them — expect
the freshness split to appear from ~19:30Z); (2) 59/4,654 rows (1.27%) carry
a checkpoint-pct label 5pp (58 rows) or 30pp (1 row) off the snapshot's true
position due to the `12:00` sentinel mapping — bounded, frozen, and only
fixable for future rows in code.

No code changes were made by this audit.  Report file: this document
(untracked, not committed).
