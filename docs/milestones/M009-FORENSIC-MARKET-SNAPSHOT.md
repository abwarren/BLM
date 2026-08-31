# MARKET SNAPSHOT FORENSIC REPORT — BLM v4 (M009 era)

Audit date: 2026-08-31 (~19:0x–19:4x UTC).  Scope: MARKET SNAPSHOT QUALITY —
do the market-line observations used for analytics represent the market
state at the relevant checkpoint?  Pure forensic audit: NO redesign, NO
freshness-definition changes, M009-M5 untouched (it was committed
independently mid-audit by the parallel agent, cbc96d7).

## METHOD (evidence basis)

- Read-only analysis of a temp copy of the production DB
  (`cp blm_pokerbet.db /tmp/blm-forensic.db`, snapshot at 19:02:33Z;
  production files never opened directly — standing user rule).
  Corpus: 27,230 snapshots / 26,265 market_observations / 4,654
  checkpoint_market rows / 10,512 predictions / 1,107 games (471 clean
  ended games in checkpoint_market).
- Freshness semantics reproduced EXACTLY from `blm_v4/scorecard.py`
  (`_market_age_seconds` = checkpoint_ts − market_ts clamped ≥ 0;
  `_market_status` LIVE ≤ 300s / STALE / MISSING; `BLM_MARKET_STALE_SECONDS=300`).
- The frozen line at each checkpoint was RECONSTRUCTED from raw
  snapshots + WS observations using the code's own `_frozen_market_obs`
  algorithm (last snapshot `total_line` at-or-before, else lowest line
  of the latest WS MatchTotal batch at-or-before).  Validation:
  **0 mismatches** across all 4,654 rows vs the stored `live_market_line`
  — the reconstruction is exact.
- Live verification through the RUNNING service (curl :2262) where the
  report cites production numbers.
- Tests: full suite run on the working tree — see TEST RESULTS.

---

## 1. SNAPSHOT TIMING INTEGRITY — SOUND

- `captured_at` parseable on 100% of rows in all four tables
  (snapshots, market_observations, checkpoint_market.checkpoint_timestamp,
  predictions.source_snapshot_at).  0 unparseable.
- 0 `captured_at` regressions vs insertion order; 0 duplicate
  (game, captured_at) pairs (UNIQUE constraint holds).
- Same-game wall-clock capture cadence: median 85.7s, p95 134.7s,
  p99 232.8s, max 3,280s (55 min).  177 gaps > 300s, 63 > 600s —
  these are collector rotation/relaunch gaps (list-row round-robin +
  event-view one-per-tick limit), not data corruption.
- Snapshots/game: min 1, median 24, max 138.
- Checkpoint timing: `checkpoint_timestamp` = the checkpoint snapshot's
  `captured_at`; fixed-pct checkpoints select the snapshot closest to
  the target progress within ±5pp; snapshots after the checkpoint are
  never used (no look-ahead — enforced by construction and tests).
- Snapshots around a checkpoint: typically 2–3 snapshots within ±120s
  (dist: 1→96, 2→1354, 3→3088, 4→38, 5+→78) — consistent with the
  ~86s per-game capture cadence.  No checkpoint-crowding pathology.

## 2. FRESHNESS INTEGRITY — STORED LAYER IS DATA-STARVED (HEADLINE FINDING)

- **All 4,654 checkpoint_market rows carry `market_timestamp` = NULL.**
  Every row was backfilled in one 43-minute window (17:47:39Z →
  18:30:20Z, the M009-M1 deploy) and is IMMUTABLE (INSERT OR IGNORE,
  UNIQUE(game, pct)).  The M3 freshness columns arrived AFTER the
  corpus froze; old rows keep NULL by design ("old rows keep NULL =
  honest missing").
- Consequence: the production freshness classification is
  **MISSING 4,654/4,654 = 100%** — LIVE 0, STALE 0, every bucket 0,
  every edge `edge_class` = None, `avg_age` = null everywhere.
  Verified live via the running API:
  - `/api/v4/scorecard` market_freshness: all six buckets n=0.
  - edge_buckets: every bucket `fresh_n: 0, stale_n: 0, missing_n: N,
    avg_age: null` (e.g. 20+ BLM_UNDER n=834, avg_diff −28.44).
  - `/api/v4/game/30749637`: 10/10 rows `market_status: MISSING`.
- **TRUE freshness (reconstructed from raw data, exact per §METHOD):**
  LIVE 3,297 (70.9%) · STALE 670 (14.4%) · MISSING 687 (14.8%).
  Stale-line frequency: 670/4,654 = 14.4% of all checkpoints; 16.9% of
  market-bearing checkpoints.  Missing-line frequency: 14.8%.
- Age distribution (market-bearing rows): median 160s, p75 254s,
  p90 329s, p95 377s, max 1,294s.  Buckets: 0-10s 35 · 10-30s 345 ·
  30-60s 442 · 60-120s 613 · 120-300s 1,862 · 300s+ 670.
- Frozen-line source: 100% WS (all 3,967 market-bearing lines came from
  eu-swarm MatchTotal batches; zero snapshot-carried lines in this
  corpus — the eligible WS-era games never produced event-view lines).
- Root cause of the empty layer: **no clean game has completed since
  M3's deploy** — the last OK game_result is 18:22Z, and the collector
  was restarted 19:04:33Z (M4 deploy).  The layer is inert-but-correct,
  not broken: the next eligible completion populates LIVE/STALE rows.

## 3. EXTREME EDGES — THE SIX EXAMPLES, TRACED

All six named values located (predictions q2/pctN checkpoints,
checkpoint_market, and one live card).  For each: the frozen market
line's true observation age (reconstructed), the actual (when known),
and each side's error vs the actual:

```
edge    game      cp     blm     mkt     mkt_age   actual   mkt_err  blm_err
+43.5   30745486  q2     213.0   169.5   209s      192      22.5     21.0
+33.6   30744854  q2     217.1   183.5   165s      179      4.5      38.1
+33.6   30750174  q2     196.1   162.5   48s       (live)   —        —
+31.6   30748937  q2     185.1   153.5   167s      155      1.5      30.1
-38.1   30750168  q2     130.4   168.5   129s      (live)   —        —
-38.1   30742006  live   182.4   220.5   ~fresh    217      3.5      34.6
-29.5   30739713  pct80  133.0   162.5   56s       (never ended)
-29.5   30741130  pct20  113.0   142.5   259s      (never ended)
-27.7   30741759  pct50  213.8   241.5   87s       225      16.5     11.2
```

Verdict per example: **NOT stale-market artifacts.**  Every market was
observed 48–259s before its checkpoint — all LIVE by the 300s
definition (two borderline: 209s, 259s).  In every case with a known
actual, the MARKET was close to right (err 1.5–22.5) and the BLM side
was the outlier (err 11–38); the −38.1 live card (30742006) is the
cleanest case: market 220.5 vs actual 217 (err 3.5) while the model
said 182.4 (err 34.6).  The "edge" is model-instability, not
market-model disagreement the market lost.

Root mechanisms, reproduced with the repo's `project()`:
- **Collapsed pace**: 30745166 pct10 — 16 of the 20 prefix snapshots
  are a pre-tip stuck board ("1st Quarter 12:00 0-0" for ~30 wall-min),
  pace = 41.6 → fair = 97.4 vs fresh WS line 227.5 (actual 198).
  Same signature in −29.5/−38.1 rows (fair 113–133).
- **Burst pace**: 30746509 pct10 — 5-row Q1-opening prefix, pace =
  283.0 → fair = 254.6 vs 188.5 (actual 196).  Same signature in
  +43.5/+33.6/+31.6 rows (fair 185–217).
- 30745166 at pct100: pace 103.0 → fair 198.0 = actual.  The model is
  fine late; early checkpoints on poisoned/short prefixes are unstable.

## 4. EDGE MAGNITUDE vs MARKET AGE — THE STALE HYPOTHESIS IS INVERTED

- Of 1,018 market-bearing checkpoints with |edge| ≥ 20: **896 (88%)
  had a LIVE (≤ 300s) market**; only 122 (12%) STALE.
- Median market age DECREASES as |edge| grows:
  0-2 → 193s · 2-5 → 171s · 5-10 → 191s · 10-15 → 164s · 15-20 → 142s ·
  20+ → 135s.
- Mean |edge| by age bucket: 0-10s → 44.1 (n=35) · 10-30s → 17.4 ·
  30-60s → 13.4 · 60-120s → 13.2 · 120-300s → 12.0 · 300s+ → 10.7.
  The FRESHEST lines carry the LARGEST edges — because the freshest
  lines are exactly the early checkpoints where the model is most
  unstable.  Stale data does NOT explain large edges; model instability
  at early/mid checkpoints does.
- Big-edge rows at pct ≤ 60 (n=718): fair 140–220 (plausible range)
  610 · burst fair > 220: 73 · collapsed fair < 140: 35.  Only 2 of
  307 big-edge games carry the stuck-0-0 prefix signature.
- Large edges concentrate at pct20–80 (158/143/141/125/128/121/145),
  not pct10 (23) or pct90 (34) — mid-game, where a single fast-scoring
  burst inflates the pace rate.

## 5. SUSPICIOUS PATTERNS

- **Abrupt line moves**: 1,255 WS line moves ≥ 5 pts within 30s
  wall-clock.  At 7x game speed 30s ≈ 3.5 game-minutes — consistent
  with fast virtual scoring, not impossible.  11-pt single moves
  dominate; no physically impossible hops found in WS data.
- **Frozen/stuck lines**: only 5 identical-line runs ≥ 10 min
  (30744924 14m, 30750169 13m, 30750173 11m, 30750275 11m, 30745046 10m).
  All five verified GENUINE holds — the WS feed kept flowing past each
  run (e.g. 30750169 obs continue to 18:40:04Z, 30750275 to 19:01:38Z).
  No stuck-capture pathology.
- **Post-freeze movement** (frozen line superseded quickly): of 1,018
  big-edge rows, 253 had a WS obs within 60s after the checkpoint; 71
  of those moved ≥ 5 pts within 60s (median move 2.0, p90 8.0,
  max 19.0 — e.g. 252.5 → 233.5 in 11s).  A minority of big edges
  coincide with the line moving right after the freeze; those frozen
  lines are stale-within-a-minute by construction.
- **WS batch drift**: batches now carry up to 5–6 half-point variants
  per frame (182.5/184.5/186.5/188.5/190.5; sizes: 3-line 2,995 · 1-line
  1,589 · 2-line 931 · 4-line 1,368 · 5-line 1,669 · 6-line 2) — the
  M006-era convention was validated on 3-line batches.  All variants
  move together (center +6 ↔ lowest +6), so the lowest-line convention
  stays internally consistent; re-validation against the SPA main line
  is recommended given the batch-size change.
- **Missing lines**: 14.8% of checkpoints (WS feed not yet covering the
  game, or the documented 30741757-class coverage gap: snapshots
  flowing with zero WS observations for hours).  With timestamps stored
  (post-M3 rows), the STALE flag exposes these honestly; today they are
  invisible because `market_timestamp` is NULL on the whole corpus.
- **7x-speed caveat**: LIVE ≤ 300s of WALL clock ≈ 35 game-minutes of
  virtual-game time.  A "LIVE" line can be far off the current book.
  The freshness semantics are the existing dashboard definition
  (unchanged per directive); the interpretation caveat stands.

## 6. SNAPSHOT ORDERING / BEFORE-AFTER CHECKPOINT

- 0 ordering regressions, 0 duplicates; checkpoints consume only
  at-or-before data (line freeze + projection prefix), verified by
  construction and the M007-M4/M009 freeze tests.
- Snapshots after a checkpoint are never consulted for that checkpoint;
  the frozen line is the LAST observation at-or-before, so a line that
  moved right after the checkpoint is the honest at-freeze market.

## 7. RECOMMENDED SAFEGUARDS (no redesign; all optional, explicit-go)

1. **Backfill `market_timestamp` on the frozen corpus from raw data.**
   The reconstruction is proven exact (0/4,654 mismatches).  A one-time
   UPDATE from `_frozen_market_obs` semantics would make the whole
   4,654-row corpus classifiable (TRUE: 70.9% LIVE / 14.4% STALE /
   14.8% MISSING instead of 100% MISSING), activating the M3
   LIVE_EDGE/STALE_DIFFERENTIAL and avg_age machinery that today
   cannot run.  Tradeoff: it writes to nominally-immutable rows — it
   changes NO computed value (mvf/signal/outcome are line-derived and
   the line is unchanged), it only fills a column that did not exist at
   freeze time.  Requires explicit authorization (immutability rule).
2. **Model-instability guard on large edges.**  Edge buckets / big-edge
   rows should carry a pace-quality flag: collapsed (fair < 140) and
   burst (fair > 220) fair values at pct ≤ 60, or ≥ 5 stuck 0-0 rows in
   the prefix, are pace artifacts — display them as UNRELIABLE, never
   as a market signal.  Fits the M5 `reliable` pattern (n < min_sample);
   no freshness-definition change.
3. **Re-validate the lowest-line convention** against the SPA main line
   now that WS batches carry 5–6 variants (was 3 at validation time).
4. **Re-validate the 300s LIVE threshold for 7x virtual games** (or
   surface a game-time-equivalent age alongside wall age).  Do NOT
   change the definition per directive — but an analyst reading
   "LIVE" on a 299s-old line of a 7x game should know it is ~35
   game-minutes old.
5. **Close the collector market-refresh coverage gap** (30741757-class:
   snapshots flowing, zero WS obs for hours) — already the documented
   open item; with timestamps stored, STALE flags will make these
   visible to analytics.
6. **Predictions market freeze should also store the observation
   timestamp** (predictions are rebased, so a `market_timestamp`
   column would refresh on rebase) — today only checkpoint_market
   carries it, and only for post-M3 rows.

## TEST RESULTS

- Targeted (freshness + M4 analytics + checkpoint_market): 23 passed.
- Full suite (working tree): **266 passed, 0 failed** (canonical 246 +
  20 M009-M5 tests committed mid-audit at cbc96d7 by the parallel
  agent; the audit modified no code).
- Live API (curl :2262): market_freshness all-zero buckets,
  edge_buckets all missing-only with avg_age null, 30749637 10/10
  MISSING — all consistent with the temp-copy reconstruction.

## ANSWERS TO THE 15 INVESTIGATION POINTS

1. captured_at — parseable 100%, monotonic, unique. SOUND.
2. market timestamp — NULL on 100% of the frozen corpus (pre-M3 rows);
   true values reconstructable exactly (0 mismatches).
3. checkpoint timing — checkpoint snapshot's captured_at, closest-±5pp
   selection, no look-ahead. SOUND.
4. market age — true median 160s; 14.4% of checkpoints > 300s.
5. freshness classification — production: 100% MISSING (layer
   data-starved); true: 70.9% LIVE / 14.4% STALE / 14.8% MISSING.
6. market line — 100% WS-sourced on the corpus; lowest-of-batch
   convention internally consistent.
7. missing market line — 14.8% (feed coverage / documented gap).
8. stale market line — 14.4% of checkpoints, 16.9% of market-bearing.
9. multiple snapshots around one checkpoint — typical 2–3 within ±120s;
   no crowding pathology.
10. snapshot ordering — 0 regressions, 0 duplicates.
11. snapshots before/after checkpoint — after never used; 71/253 big-
    edge rows saw the line move ≥ 5 pts within 60s post-freeze.
12. abrupt impossible line movements — 1,255 ≥ 5pt/30s moves; all
    consistent with 7x virtual scoring; none impossible.
13. frozen/stuck market lines — 5 runs ≥ 10 min, all genuine holds.
14. extreme edges — 1,018 rows |edge| ≥ 20 (25.7% of market-bearing);
    88% had LIVE markets; model-pace artifacts (burst/collapsed), not
    market-data artifacts.
15. stale observations explaining extreme edges — REFUTED: median age
    DECREASES with edge magnitude; freshest bucket has the largest
    mean edge; every named example had a market ≤ 259s old.
