# BLM MILESTONE CHECKPOINT — M004 (2026-08-30)

## PROJECT
BLM v4 (Betting Line Model) — PokerBet virtual-basketball live-score pace
projections + accuracy scorecard + historical market analytics. Live on
`blm-server:2262` / `blm-collector` (systemd).

## CURRENT MILESTONE
M006 — First-class live PokerBet market data (capture frequency + prediction freeze).

## COMPLETED
- 79c4c0d: projection live-score floor at source + single-source api.py +
  collector honesty (no explosion-split, identity guard, audit table). 167 tests.
- db650d7 (DEPLOYED): market_history table + trends.py + /api/v4/trends + dashboard section.
- af3e4c2 (DEPLOYED): scorecard REBASE — recomputes checkpoints from current model. 174 tests.
- 7eed84a (DEPLOYED): quality-gate >50pt jump rule wall-clock-gap aware. 175 tests.
- 7d1bb8e (DEPLOYED): dashboard distinguishes RECORDED/COMPLETED/VALID/EXCLUDED.
- c598be5 + 5062a37 + 024c571 + f2b8584 (DEPLOYED): first-class market data —
  * collector captures event views with per-game freshness tracking
    (MARKET_BATCH=1, MARKET_REFRESH_S=480 → every game's line refreshed
    ~7 min vs ~33 min before; batch>1 broke the SPA, reverted).
  * predictions FREEZE the market total observed at/before each
    checkpoint (quarter + fixed 10-90%) — later movement never rewrites.
  * API exposes total_line_at + total_line_age_s (freshness).
  * dashboard Mkt Total shows live/stale by age.
  * event-view failure storm → browser rotation (self-healing).
  * f2b8584: fixed rotation page-propagation bug (rotation closed the old
    browser but _tick kept using it → TargetClosedError → infinite
    relaunch loop).  _capture_next_market now returns the live page.
  Tests: 177 pass (2 new freeze tests).

## CURRENT STATE — market data
- **ROOT CAUSE of MKT TOTAL = – (CONFIRMED by full trace)**: the PokerBet
  market total exists ONLY on the event-view page.  The collector captures
  it ONLY when it successfully opens an event view.  Since ~21:07Z the
  SPA's event-view route has stopped hydrating (row click no longer
  navigates; parse yields empty teams ''/'').  Panel/list capture keeps
  working (90 snaps/10min) but carries NO market.  So the market is NOT
  lost in the pipeline — it is NOT CAPTURED because the event-view route
  is a hard single dependency that PokerBet-side SPA changes broke.
- **PROOF the pipeline is correct when the SPA works**: 30740053 has 104
  market observations with REAL live movement (171.5→183.5→184.5 every
  ~20s); 30739886 shows the user's exact 164.5→162.5→172.5 series; 30739890
  has 66 obs.  The 183.5 one-liners on 8 games are each at DISTINCT
  timestamps (per-game discovery captures, NOT contamination).
- **User's named games (Panathinaikos 70-92, Maccabi 101-88)**: both
  correctly INVALID (score regression) — their snapshot series contain
  foreign snaps (81-89 Q1 then 60-77 Q4; 76-87 Q4 then 69-59 Q3, then an
  81-89 spike mid-game).  0 scored predictions.  The 198.4/76.9 projections
  are the model operating on contaminated series — the dashboard showing
  them as "completed games" is presentation, not a model bug.
- **Fix (deployed)**: per-game freshness tracking + API freshness fields
  + prediction market freeze + event-view failure rotation (self-healing).
  The freeze is PROVEN by tests (190.0 at Q1 never rewritten by 195.0).
- **LIVE BLOCKER (external)**: PokerBet event-view SPA route not hydrating.
  Collector rotates gracefully (verified stable), cycles games, but cannot
  capture new market lines until the route recovers.  Manual browser check
  confirms row click does not navigate (SPA change on PokerBet side).

## M004 (previous milestone) — audit answers retained

### 1. Why 0 valid scored games while Recent Predictions is populated
The dashboard "Valid scored games" = prediction_scores rows where
`fragment=0`. ALL 50 scored rows are `fragment=1` → headline 0.
`fragment=1` means the game did NOT start at Q1 with >=15 snaps — it was
tracked from mid-game (the old resolve-path hole: first snapshot was the
lobby "1st Quarter 23:00 15-2", then a jump to the real mid-game state).
This is the SAME contamination that the quality gate caught (65 score
regression + 20 impossible jump = 85 INVALID), correctly excluded. Recent
Predictions shows these games because prediction rows are stored for ALL
checkpoints (q3/q4/pctNN) regardless of final quality — those predictions
are real but from contaminated/incomplete histories, so they must not be
scored. **Not a bug — the gate is working.**

### 2. The 100-point predictions — root cause (TRACE, not guess)
Example: Houston/Lakers `30740069#i11`, checkpoint q3, projected 50.0/50.0
tot=100.0, live 80-82.
- q3's source snapshot has `quarter=NULL` and period_label='3rd Quarter'
  (event-view rows store only the label, no structured quarter).
- `clock_minutes(q=None, clock='00:15')` → **None** (needs quarter+clock).
- `_progress_of` fallback maps label→quarter, but `pace_from_snapshots`
  uses ONLY structured quarter+clock for elapsed → None.
- With 1 scored row and pace=None, the model's original fallback was
  expected_total=100 (50/50 split), which the floor then kept.
- So 100 = "pace unavailable at that checkpoint" sentinel, NOT a real
  projection and NOT a model trying to predict 100. The floor is masking
  nothing; the model cannot yet pace a single event-view snapshot.
- This also explains pct80 = 205.7 (two spaced rows → real pace 205) and
  pct90/final = 143/158 (pace collapses on short spans, pre-floor).

### 3. Invalid breakdown (the 85)
- 65 score regression (contamination) — lobby/foreign "15-2" snapshots
  mixed into real games (pre-a185333 resolve hole), or intra-game dips.
- 20 impossible score jump — big forward jumps from the same hole.
- All correctly excluded from scoring; not over-strict (legitimate quarter
  transitions, score-representation changes, benign jitter do NOT trigger
  these; both reasons are genuine contamination signatures).
- Note: game_results counts show UNKNOWN=127, INVALID=84, OK=10 (the
  "85" in the dashboard includes an UNKNOWN counted as invalid; OK=10 are
  all fragments too).

### 4. The real fix — ALREADY SHIPPED in 79c4c0d
The collector now starts clean: first snapshot is the real Q1 start of the
actual event (identity guard in `_capture_event_state` + `_restart_split_suffix`).
LIVE PROOF: games 30741202..30741207#i1 (started after deploy) have 16 snaps,
FIRST='1st Quarter', Q1-starting — clean full-history games. First clean OK
game expected ~00:12Z (02:12 SAST). Watcher `proc_a36aec40e075` armed.

## FILES CHANGED (this milestone)
- Audit itself: investigation only.
- af3e4c2: blm_v4/scorecard.py (rebase in record_predictions), tests.
- db650d7: market_history + trends.py + api + dashboard.
- docs/milestones/CURRENT.md (this checkpoint).

## COMMIT
- HEAD: 7d1bb8e (dashboard 4-concept distinction) — deployed
- 7eed84a (quality-gate gap-aware) — deployed
- af3e4c2 (rebase) — deployed 23:49:00Z
- db650d7 (trends) — deployed 23:47:40Z
- 79c4c0d (floor + collector) — deployed 23:32:48Z
- All services active.

## TEST RESULTS
- 175 passed / 0 failed (7d1bb8e tree).

## LIVE VERIFICATION
- blm-server + blm-collector: active. HEAD 7d1bb8e deployed.
- /api/v4/trends live: analytics_tz Africa/Johannesburg, grouped periods
  configured, empty clean-game base (correct).
- /api/v4/scorecard: FOUR concepts live — recorded_predictions 1460,
  completed_games 10, valid_scored_games 0, invalid 86 (20 impossible
  jump + 66 score regression), excluded 211.  RECENT = 25 rows
  (fragment rows now FRAGMENT-badged, diagnostics only).
- Live games 3074120x#i1: began 1st Quarter (clean start), now mid-game.

## KNOWN ISSUES
- 30740069#i11 q3 pace unavailable (single event-view row, quarter=NULL):
  a future improvement is to compute elapsed from period-label when
  quarter is missing (label→quarter map exists for progress). NOT required
  for M005; the gate + collector fix make it moot for future games.
- market_history table will populate from clean games only; existing
  fragments stay excluded by design.

## NEXT MILESTONE
M005 — First clean full-history game end-to-end: one real game captured
Q1→final, all predictions scored, market_history recorded, dashboard shows
real "Valid scored games" > 0.

## NEXT ACTION (ONE)
**Deploy db650d7** (restart blm-server + blm-collector), then watch
`proc_a36aec40e075` for the first clean OK game (expected ~00:12Z) and
confirm: predictions scored with fragment=0, market_history row recorded,
/trends shows real data.

## IMPORTANT DECISIONS
- 100 = pace-unavailable sentinel (expected_total=100 fallback), NOT a
  prediction. Keep the floor; fix the model's pace input (label→quarter),
  not the floor.
- Quality gate stays strict; fragments never scored; INVALID games kept
  for diagnostics (game_quality table).
- No UI change to hide the 0-valid discrepancy — it is correct until clean
  games accumulate.

## DATA/SCHEMA CHANGES
- db650d7 adds market_history table + indexes (auto-created on server start).
- No destructive changes.

## CONFIGURATION
- BLM_ANALYTICS_TZ default Africa/Johannesburg; BLM_TREND_GROUPS
  configurable. No changes needed now.

## DO NOT REGRESS
1. Floor: CURRENT + REMAINING = FINAL (projection never below live score).
2. api.py single-source via project().
3. Identity guard in _capture_event_state + _restart_split_suffix.
4. Explosion-split removed; verified final ends the game.
5. market_history: OLVC never overwritten by CLV; only OK + non-fragment.
6. Headline accuracy = fragment=0 only; no hard-coded time-of-day rules.
