# BLM MILESTONE CHECKPOINT — M007-M4 (2026-08-31)

## PROJECT
BLM v4 (Betting Line Model) — PokerBet virtual-basketball live-score pace
projections + accuracy scorecard + historical market analytics. Live on
`blm-server:2262` / `blm-collector` (systemd).

## CURRENT MILESTONE
M007 — Game detail window: live market + prematch + live model.
M007-M1 (COMPLETE, deployed 3c8183c): backend exposes the four line
concepts through the existing game-detail API.
- OPENING LINE: market.opening_line + opening_line_at (first verified
  total_line via opening_snapshot — never moves with the market).
- CURRENT LIVE LINE: market.total_line + total_line_at + total_line_age_s
  + market_source (existing).
- LIVE BLM PREDICTION: model.expected_total (existing, pure function).
- PREMATCH PREDICTION: NOT STORED — genuine gap. No prematch checkpoint
  exists; games are discovered live (period labels are only quarter states
  + Finished); no pre-game prediction is ever recorded.  API returns honest
  null; dashboard must show PREMATCH BLM = – for these games.  Capturing
  prematch requires a new collector slice (prematch tab scan + fixture
  correspondence) — decision pending.
- Verified live: 30740053 opening=171.5@20:30:52Z live=181.5@21:07:44Z
  (stale, honest age 8545s); live 30741613 opening=null total=null (event
  view starved — honest), live prediction 165.7 computed.
- Tests: 185 pass (new test_opening_snapshot_returns_first_line).
M007-M2 (COMPLETE, deployed 6a40efc): game-detail pane shows the four
concepts.  divergenceHTML now leads with a line-grid: Prematch BLM (–,
honest), Opening Line (immutable first line + ts), Current Live Line
(latest + live/stale + source), Live BLM Prediction (model.expected_total).
Same poll loop (refresh → loadModalDetail → renderModal → divergenceHTML)
re-renders every tick — no page refresh.  Verified live on running server:
deployed JS/CSS carry the header; API 30740053 opening=171.5 live=181.5
expected_total=181.5.  node --check OK; 185 tests green.
M007-M3 (COMPLETE, deployed 0960cbe): closing line.
- CLOSING CONDITION: market closes = game reaches terminal state (game
  status 'ended').  While live → closing_line = null (latest live line is
  NOT closing).  Once ended → last verified PokerBet total, immutable.
- closing_snapshot(rows, ended) in projection.py; API exposes
  market.closing_line + closing_line_at; pane shows Closing Line (– null).
- Verified live: 30741197 (ended, 1 line) closing=161.5@21:25:41Z;
  30741518 (ended, no lines) closing=None (honest);
  30741613 (live, line 145.5) closing=None — latest live line NOT closing.
- opening_line untouched; historical checkpoints untouched; no provider
  substitution.  186 tests green (new test_closing_snapshot_only_when_ended).
M007-M4 (COMPLETE, deployed 8ed6164): historical checkpoint market values
in the game-detail table.
- API /api/v4/game/{id} now exposes `checkpoints[]`: per checkpoint —
  check/label, checkpoint_percent, quarter, predicted_at,
  source_snapshot_at, blm_prediction (projected_total), market_at_checkpoint
  (stored frozen predictions.market_total), edge (blm - market), actual_final
  (game_results, only when verified), error (blm - actual).  Missing market
  or result = NULL, never fabricated.  /live and /games lists stay lean
  (checkpoints only on the detail route).
- Detail modal renders a CHECKPOINTS table (Check/BLM Pred/Market @CP/Edge/
  Actual/Error) with prediction timestamps + a frozen-semantics footnote.
- FIXED a real freeze bug the new tests exposed: _record_game only updated
  its frozen line on checkpoint snapshots (lines observed on non-checkpoint
  snapshots were invisible) and the WS fallback froze the FIRST observed
  line forever.  Both quarter and fixed-% paths now share one at-or-before
  scan `_frozen_market_line`: last snapshot total_line at-or-before T,
  else LOWEST line of the latest WS MatchTotal batch at-or-before T
  (event-view parity, same rule as storage.market_observations_before).
  Later observations never rewrite a checkpoint.
- Verified live: 30741197 (ended) 11 checkpoints all market=161.5 frozen,
  actual=182, errors -9.0..-51.9; 30741613 (live) 14 checkpoints, 8 with
  WS frozen markets that match the raw market_observations batches
  timestamp-for-timestamp (148.5/146.5/147.5/146.5/142.5/145.5), 6 honest
  NULL (feed started after those checkpoints), actual/error NULL while live.
- Tests: 196 pass (10 new: shaped/ordered checkpoints, snapshot-line freeze,
  WS freeze at-or-before, multi-line batch lowest-line tie-break, rebase
  immutability, ended actual+error, live nulls, empty game, lean /live,
  modal markup).
NEXT: M007-M5 — NOT STARTED, scope to be defined (candidate: checkpoint
market source/freshness badge in the table, or prematch collector slice).

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
- **RESOLVED — eu-swarm WebSocket feed (7748193 + 42a7c61, DEPLOYED)**:
  the BetConstruct SPA pushes the FULL live market tree (game total +
  team/half/quarter totals + O/U lines + Over/Under prices) over
  `wss://eu-swarm-newm.pokerbet.co.za/` with NO event-view DOM dependency
  (proven by live frame capture: MatchTotal base=204.5, O 1.94/U 1.87).
  BLM now captures it: ws_market.py parser → market_observations table →
  API fallback (market_source='ws') → scorecard freeze fallback →
  dashboard '· ws' marker.  The event-view path is UNCHANGED (primary
  when it works); the model never fabricates a line.
- **LIVE VERIFICATION (2026-08-31)**: game 30741613 — 3 lines captured
  (148.5/150.5/152.5), API total_line=148.5, market_source='ws',
  age 0.66s; model expected_total 160.5.  Game 30741576 — 8 timestamped
  line batches over 3 min (204.5→205.5→209.5→204.5→203.5→202.5→201.5),
  70 observations across 13 games, zero collector errors.  Movement
  history preserved; predictions freeze the line at-or-before checkpoint
  (market_observations_before, tested).
- **User's named games (Panathinaikos 70-92, Maccabi 101-88)**: both
  correctly INVALID (score regression) — their snapshot series contain
  foreign snaps (81-89 Q1 then 60-77 Q4; 76-87 Q4 then 69-59 Q3, then an
  81-89 spike mid-game).  0 scored predictions.  The 198.4/76.9 projections
  are the model operating on contaminated series — the dashboard showing
  them as "completed games" is presentation, not a model bug.

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
