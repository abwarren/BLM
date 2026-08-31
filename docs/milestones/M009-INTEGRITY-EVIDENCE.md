# M009 INTEGRITY EVIDENCE — RETROSPECTIVE CONTAMINATION + FRONTEND INTEGRITY (2026-08-31)

Applies the consolidated M009 directive: dataset-integrity /
retrospective-contamination hardening + the authorized frontend
data-integrity slice.  No schema changes.  No freshness-semantics
changes.  Frozen rows stay immutable.

## 1. LIFECYCLE TRACE (verified in code, not assumed)

- `game_quality` (schema `scorecard.py:77`) — written ONLY by
  `capture_results()` (the 60s scorecard loop, M007-M8 re-verification):
  `status='INVALID'` + `reason` + `checked_at`.  Absent row = eligible.
  INVALID is final/idempotent.
- `record_checkpoint_market()` — INSERT-time eligibility: OK result,
  ≥15 snaps, starts Q1, `NOT EXISTS game_quality INVALID`; rows frozen
  via `INSERT OR IGNORE` + `UNIQUE(source_game_id, checkpoint_pct)`.
- The re-verification ALSO flips `game_results.final_result_status` to
  'INVALID' (verified: contamination-integrity + legacy tests assert it).
- Headline readers BEFORE the fix: `_market_vs_fair_sql` and
  `/api/v4/scorecard/events` selected checkpoint_market with NO
  eligibility predicate → a clean-at-record game re-verified INVALID
  later kept feeding headline analytics forever.  THE DEFECT.
- `game detail` (`/api/v4/game/{id}`) is per-game DIAGNOSTIC — it
  intentionally keeps serving the frozen rows (audit trail).

## 2. FIX — LOGICAL EXCLUSION, single definition

`scorecard.py`:
```sql
_CM_ELIGIBLE_SQL =
  JOIN game_results r ON r.source_game_id = cm.source_game_id
  WHERE r.final_result_status = 'OK'
    AND NOT EXISTS (SELECT 1 FROM game_quality q
                    WHERE q.source_game_id = cm.source_game_id
                      AND q.status = 'INVALID')
```
Applied in BOTH headline readers: `_market_vs_fair_sql` (scorecard) and
`/api/v4/scorecard/events` (api.py imports the same fragment — one
definition).  Game detail untouched (diagnostic).

Behavior preserved: rows REMAIN in checkpoint_market (immutable,
auditable); per-checkpoint skeleton (10..100%) is still returned with
honest N=0 when every game is excluded (the early-return on empty rows
was removed so the shape never vanishes); freshness classification
(LIVE/STALE/MISSING, M3 helpers) untouched; UNIQUE + INSERT OR IGNORE
untouched.

## 3. COMPLETION-STANDARD ANSWER (directive §13)

Q: Can a game that was VALID when checkpoint rows were frozen later
become INVALID and still contribute to headline market-vs-fair
statistics?
A: **NO.**  Code path: re-verification writes game_quality INVALID +
game_results INVALID → `_CM_ELIGIBLE_SQL`'s WHERE excludes the game's
rows from `market_vs_fair()` (checkpoints/games/freshness/TOD/edge
buckets all derive from the same filtered `rows`) and from
`/scorecard/events`.  Frozen rows remain in checkpoint_market and stay
visible in game detail for audit.

## 4. TEST EVIDENCE

- RED: marker-free `test_m009_contamination_integrity.py` run against
  HEAD code in a clean worktree (`git worktree add /tmp/blm-head HEAD`) →
  **5 failed, 6 passed** (the 5 exclusion tests fail without the fix).
- GREEN (working tree with fix): contamination-integrity 11/11 passed;
  legacy-contamination 3/3 passed.
- Frontend integrity (new): `tests/test_m009_m5_frontend_integrity.py`
  11/11 passed.
- M3/M4/M5 targeted (checkpoint_market, market_freshness, mvf
  aggregation/api/frontend, m4 analytics, m5 disparity + frontend):
  all green.
- Full canonical suite: **300 passed, 0 failed**.

## 5. FRONTEND INTEGRITY SLICE (message-2 scope, completed)

- `/api/v4/live` + `/api/v4/game/{id}` now carry the AUTHORITATIVE
  `quality_status` / `quality_reason` from `game_quality` (backend
  single source; the browser never re-derives validity).
- INVALID game card: EXCLUDED chip (`.chip-excluded`), gated-note
  banner `INVALID — EXCLUDED FROM ANALYTICS` + reason; model panel
  (momentum/signals/projections) replaced by the gated note; model
  total/edges suppressed; scoreboard, lines, sparkline, raw history
  remain as diagnostics.
- Modal: same banner; charts suppressed; Model/Momentum panels gated;
  NEW frozen per-checkpoint MARKET VS FAIR table exposes recorded
  momentum + false_momentum (distinct from the live-window chip, which
  is now labelled "False Mom (live)" with an explanatory tooltip).
- Labels: "Market-implied win prob" (no 50/50 fallback — "— (no odds
  captured)"), "Model data confidence", "BLM position win rate",
  "mkt proximity" (tooltip), "SNAPSHOTS (window) — served games only".
- Market age: exact age rendered on card + modal (`fmtAgeExact`:
  `LIVE · 18s` / `STALE · 4m 21s` / `MISSING · —`); M3 threshold
  (300s) and LIVE/STALE/MISSING semantics unchanged.
- Time-of-day labels: both blocks now say "first-observed hour, local"
  (game_start = MIN(snapshot.captured_at) is a first-observed proxy;
  no fixture-start timestamp is manufactured).

## 6. DEPLOYMENT VERIFICATION

NOT YET POSSIBLE — HTTP verification against :2262 was denied this
session; the scorecard loop auto-creates/backfills tables, so a
`sudo systemctl restart blm-server blm-collector` + `/api/v4/scorecard`
+ `/api/v4/scorecard/events` + `/api/v4/live` check is the remaining
step, pending operator authorization.

## 7. REMAINING / OUT OF SCOPE (documented, untouched)

- 12:00 period-sentinel checkpoint misposition (59 rows; separate
  position-integrity task — do not rewrite frozen rows).
- `market_timestamp`/momentum NULLs on all pre-M3/M4 rows (data-age
  gap, not a defect; no backfill without separate authorization).
- `market_history`/trends aggregation does NOT yet apply the same
  retrospective exclusion (separate dataset; follow-up candidate).
- Production deploy + live verification (needs operator restart).
