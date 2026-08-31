# BLM MILESTONE CHECKPOINT — M009-M2 (REFINED) (2026-08-31)

## M009 — SCORECARD REDESIGN: MARKET VS FAIR VALUE

M009 supersedes M008-SCORE-M2 as the active scorecard milestone (same
theme — OLV/CLV/checkpoint disparity — at higher fidelity: per-checkpoint
Market-vs-Fair as the PRIMARY metric, not a generic model-vs-market
block).  The M008-SCORE-M2 declaration below is retained as context.

CURRENT STATE: M009-M1 + M009-M1b + M009-M2 (REFINED) COMPLETE, DEPLOYED,
LIVE VERIFIED.  checkpoint_market immutable history; /api/v4/game/{id}
market_vs_fair; /api/v4/scorecard market_vs_fair aggregation (per-
checkpoint avg/median signed M-F, value %, outcomes, position win rate,
OLV->CLV, market movement) + game-level scorecard; dashboard MARKET VS
FAIR VALUE primary (Model MAE demoted to labelled DIAGNOSTIC).  §18
fixture-integrity regression in place.  Full suite 233 pass.  Evidence
trail docs/milestones/M009-EVIDENCE.md.

NEXT: **M009-M3 — OLV/CLV relationship analysis / market-convergence
stats.  Do NOT start without explicit authorization.**

## M009-M2 (REFINED) (COMPLETE, DEPLOYED, LIVE VERIFIED) — MARKET VS FAIR PRIMARY SCORECARD

MILESTONE: M009-M2 (REFINED) — the refinement directive ("Market vs Fair
is the primary scorecard") IS the M2 spec; it superseded the earlier M2
hold.  Commit b6798f2 + docs commit.

OBJECTIVE: per-checkpoint (10..100%) aggregation over checkpoint_market:
N, avg/median signed M-F (sign retained), abs M-F, OVER/UNDER/PUSH value
% (with N), OVER/UNDER WIN/LOSS, position win rate (pushes excluded),
avg OLV->CLV, market moved TOWARD/AWAY/UNCHANGED.  Game-level scorecard:
per clean game OLV/CLV/final + outcome vs OLV/CLV + progressive table.
Headline redesign: MARKET VS FAIR primary; Model MAE / Market MAE /
model-beat-market demoted to labelled DIAGNOSTIC (population:
prediction_scores fragment=0; line: checkpoint_market).

WHAT WAS TRACED: real checkpoint_market rows -> _market_vs_fair_sql ->
/api/v4/scorecard market_vs_fair -> dashboard MARKET VS FAIR VALUE block.

WHAT CHANGED: scorecard.py (_market_vs_fair_sql + market_vs_fair()),
api.py (route), dashboard.js (primary block + game-level + DIAGNOSTIC
relabels), tests/test_m009_mvf_aggregation.py (6), tests/
test_m009_mvf_frontend.py (2).

TESTS: RED confirmed each (6 + 2); one RED finding was my test
expectation (fair depends on market via 70/30 blend) — aggregation was
correct, tests rewritten against raw rows + invariants.  Full suite 233
passed, 0 failed (225 + 8).

LIVE VERIFIED (real production data, /api/v4/scorecard): pct50 — n=440,
avg_market 191.13, avg_fair 183.2, avg_mf +8.62 (signed), median 7.5,
under_value 293/440 = 67%, under_win 171 / under_loss 122, over_win 75 /
over_loss 72, position_win_rate 56%, avg_olv_to_clv +3.09, move_toward
223 / move_away 195.  4615 checkpoint rows served.  Served dashboard.js
carries MARKET VS FAIR VALUE / GAME-LEVEL SCORECARD / MODEL vs MARKET —
DIAGNOSTIC markers.

COMMIT: b6798f2 (code + tests) + docs commit (this).  Pushed.

KNOWN LIMITATION: position win rate excludes pushes by design (reported
separately); value % denominators = market-bearing rows only (honest N).

NEXT MILESTONE: M009-M3 — OLV/CLV relationship analysis / convergence
(awaiting explicit go).

## M009-M1b (COMPLETE, DEPLOYED, LIVE VERIFIED) — Market-vs-Fair exposed through game detail API

MILESTONE: M009-M1b — smallest vertical slice completion: the immutable
per-checkpoint Market-vs-Fair history is now observable end-to-end
(STORAGE -> API) through the existing game-detail route.

WHAT CHANGED:
- `blm_v4/api.py`: `_game_checkpoint_market(conn, source_game_id)` reads
  checkpoint_market rows (ordered by checkpoint_pct); `/api/v4/game/{id}`
  now returns `market_vs_fair[]` alongside `checkpoints[]` (only on the
  detail route — /live and /games stay lean).  Each row: checkpoint_pct,
  checkpoint_timestamp, quarter, opening_line, live_market_line,
  blm_fair_value, closing_line, actual_final_total, market_vs_fair
  (signed), signal, blm_vs_olv, blm_vs_clv, olv_to_clv,
  market_move_toward_blm, outcome.  Empty list when no rows; NULLs
  preserved (never fabricated).
- `tests/test_m009_mvf_api.py` (4 tests, RED confirmed): detail exposes
  rows + both disparity directions / honest NULLs on no-market / live
  game -> empty / lean payload without table.

TESTS: full suite 221 passed, 0 failed (was 217).

COMMIT: b8df068 (api.py + test).  Pushed with a2fdb38..61929bb earlier.

DEPLOYED: YES (2026-08-31 ~19:47 SAST) — blm-server + blm-collector
restarted on HEAD a3c0881; scorecard loop creates + populates
checkpoint_market within 60s.

LIVE EVIDENCE: /api/v4/game/30749637 (Betual NBA, ended, Tianjin vs
Zhejiang) returns market_vs_fair with 10 rows (pct10..100): opening
172.5, closing 172.5, actual 165; pct20 live 172.5 / fair 182.1 /
M-F -9.6 (OVER_VALUE); honest NULLs where market absent (pct10
live_market_line null -> mvf/signal/outcome null).  Real production
data, served by the running service.

NEXT MILESTONE: M009-M2 — scorecard aggregation (per-checkpoint Avg
Market / Avg Fair / Avg M-F table + Under/Over value % + outcome
analysis, honest N per checkpoint).

## M009-M1 (COMPLETE, DEPLOYED, LIVE VERIFIED) — immutable per-checkpoint Market-vs-Fair history

MILESTONE: M009-M1 — data schema + checkpoint Market-vs-Fair
calculations.

OBJECTIVE: new `checkpoint_market` table — ONE immutable row per (clean
completed game, checkpoint 10..100%) freezing what was actually
available at that point: checkpoint_pct/timestamp, opening_line (OLV),
live_market_line (frozen at-or-before the checkpoint), blm_fair_value
(project() recompute from snapshots up to the checkpoint), closing_line
(CLV), actual_final_total, signed market_vs_fair = live - fair,
signal (UNDER_VALUE/OVER_VALUE/PUSH), blm_vs_olv / blm_vs_clv /
olv_to_clv, market_move_toward_blm (TOWARD/AWAY/UNCHANGED per M009 §10),
outcome (UNDER_WIN/OVER_WIN/UNDER_LOSS/OVER_LOSS/PUSH per M009 §5),
model_version, recorded_at, frozen=1.

WHAT WAS TRACED: synthetic clean 20-snapshot game (fast-early/slow-late
scoring → both disparity directions in one game) → record_checkpoint_market
→ 10 rows (pct10..90 + pct100); pct50: fair 148 < market 180 →
UNDER_VALUE, actual 143 < 180 → UNDER_WIN; pct10: fair > market →
negative disparity retained, OVER_VALUE, OVER_LOSS; second run → byte-
identical (immutability).

WHAT CHANGED:
- `checkpoint_market` table + indexes in SCORECARD_SCHEMA.  INSERT OR
  IGNORE + UNIQUE(source_game_id, checkpoint_pct) = frozen at first
  write; NEVER rebased (unlike predictions, which are current-code-wins
  — the M009 §3 rule: no recalculating old checkpoints with a later
  model prediction).
- `Scorecard.record_checkpoint_market()` — eligibility mirrors the
  historical base (OK result + >=15 snaps + starts Q1 + not INVALID);
  pct10..90 via the existing closest-snapshot ±5pp selection, pct100 =
  terminal snapshot.
- Helpers: `_first_verified_line` / `_last_verified_line` (OLV/CLV:
  snapshot lines primary, eu-swarm WS MatchTotal fallback — WS-only
  games get full rows), `_market_vs_fair_signal`, `_checkpoint_outcome`
  (pushes explicit), `_market_move_toward_blm` (|CLV-fair| vs |OLV-fair|).
- `run()` now returns `checkpoint_market` stats.

FILES: blm_v4/scorecard.py, tests/test_m009_checkpoint_market.py,
docs/milestones/CURRENT.md.

TESTS: 10 new (RED confirmed: all failed on missing method before
implementation).  RED→GREEN: rows for clean game / signed disparity +
signal / outcome classification incl. push / OLV-CLV-actual linkage /
market_move_toward_blm / immutability on re-run / ineligible games
excluded (live, INVALID, fragment) / honest NULLs on missing market /
WS fallback lines / terminal fair floor.  Full suite: 217 passed,
0 failed (was 207).

LIVE EVIDENCE: NONE YET — user denied DB access this session (read-only
diagnostics and copy-tracer both blocked).  No production writes
happened; the running blm-server does not have this code.

COMMIT: a891104 (scorecard.py + tests + this record)

DEPLOYED: YES (2026-08-31 ~19:47 SAST) — shipped with the M1b deploy;
checkpoint_market table created + populated by the running scorecard
loop (verified live via /api/v4/game/30749637, 10 rows).

ACCEPTANCE: PASS (code+tests) / LIVE VERIFICATION BLOCKED (user).

KNOWN LIMITATION: market_history OLV/CLV remain snapshot-only (WS-era
games get NULL there) while checkpoint_market is WS-aware — deliberate,
documented divergence; a future milestone should unify.  pct100 fair is
the model's final projection (floored >= actual), not a post-hoc
prediction.  Fixed-checkpoint tolerance (±5pp) means not every game has
every pct — honest N is handled at aggregation (M2).

NEXT MILESTONE: M009-M2 — scorecard aggregation (per-checkpoint
Avg Market / Avg Fair / Avg M-F table + Under/Over value % + outcome
analysis, honest N per checkpoint).

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

## M007-M6 (COMPLETE, deployed 74adfdf) — stale/ended visibility filter

Presentation-only dashboard control: [SHOW ALL] / [SHOWING LIVE] toggle in
the header filters.  Default SHOW ALL; toggling hides games with
status='ended' OR not live (existing API freshness: live = latest
snapshot <= 15 min) — rendered from the same /api/v4/live payload;
nothing is deleted/mutated/excluded in the backend; hidden cards restore
on toggle.  Counts derived from the rendered dataset (showing X live ·
hidden Y).  Session-local state.

CONTEXT — market-integrity investigation (PROVEN): the visible "157.5
stale market" was NOT a capture failure.  30741194 (Karsiyaka vs Denizli,
previous cycle) is ENDED with its single line 157.5 frozen as
opening/current/closing — an honest ended card.  The CURRENT Karsiyaka
game is 30741844 (vs Korfez), live, whose WS batches contain
182.5/184.5/186.5 etc.; API serves 182.5 (lowest of latest batch, M006
convention) @ age < 3 min, model 180.5, edge -2.0.  The directive's
"157.0 - 186.5 = -29.5" conflated two different fixtures.  The grid mixed
the ended card in with live games, making it read as stale "current
market".  This filter makes ended/stale cards hideable; the API was
already honest.

VERIFIED LIVE (browser): SHOW ALL = 100 cards (33 live/27 ended/40
stale); HIDE = 31 live, 0 ended/stale, count "showing 31 live · hidden
69"; ended Karsiyaka 30741194 hidden while live 30741844 visible; toggle
restore = 100 cards back.  197 tests (1 new).

## M007-M7 (COMPLETE, deployed 599e2d7 + df7998b) — non-live market presentation

Frontend-only.  A non-live game's historical market line is NEVER
presented as the current live line and NEVER produces a live edge.

- Card (divergenceHTML): six distinct concepts — opening / CURRENT LIVE
  LINE (only when live AND <= 300s fresh) / LAST OBSERVED (with timestamp
  + ENDED|STALE) / BLM (historical) / closing.  liveEdge only vs the
  current live line; ended/stale cards show Edge —.
- Modal (renderModal): same guard — "Last observed @ ts · ENDED|STALE",
  "Model total (historical)", "Total edge —" when not live+fresh.
- No backend/API/storage/model/quality-gate change.  Historical
  checkpoints and observations immutable.

ROOT CAUSE (proven in audit): the "157.5 vs 186.5" report conflated two
fixtures — 30741194 (Karsiyaka vs DENIZLI, ENDED, single frozen line
157.5, model 157.0) and 30741844 (Karsiyaka vs KORFEZ, WS lines incl.
186.5).  No capture failure: 186.5 was captured 3x for 30741844.  The
defect was presentation: an ended card rendered "Mkt Total: 157.5 ·
stale / Edge: -0.5", implying a current market.  Now it shows
"Last observed 157.5 @ 21:25:21Z · ENDED / BLM (historical) 157.0 /
Edge —".  30741194 verified unchanged: opening 157.5, closing 157.5,
model 157.0, live false, age 12958s.

LIVE VERIFIED (browser): ended card 30741376 → Last observed —, BLM
(historical) 179.6, Edge —; its modal → "Last observed –", "Model total
(historical) 179.6", "Total edge –", Status ended.  Live card 30742006 →
Current Live Line 220.5 · ws, Edge -38.1 (sign preserved); its modal →
"Market total 220.5 (ws)", "Model total 182.4", "Total edge -38.1",
Status live.  Just-ended-in-window game 30741757 → market shown as
"Last observed 239.5 @ 00:47:12Z · ENDED" with edge suppressed (handles
ENDED+FRESH-snapshot edge case).  199 tests (3 new: card + modal
regressions, RED confirmed each).

OBSERVATION (not fixed, out of scope): 30741757 had NO WS market
observations after 00:47:12Z while snapshots continued to 02:48Z —
market-capture refresh gap for that game.  Candidate for a future
milestone (collector market-refresh coverage audit).

NEXT: M007-M8 — collector market-refresh coverage audit (games with
snapshots but no recent market observations).

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
NEXT: M008-SCORE-M2 — BLM vs OLV and BLM vs CLV scorecard sections
(needs OLV/CLV timestamp columns in market_history).

## M008-SCORE-M2 (IN PROGRESS) — three distinct market benchmarks

MILESTONE: Rework MODEL vs MARKET so every comparison names its line
type (OLV / CLV / live-checkpoint) and the scorecard answers the
permanent acceptance question.

OBJECTIVE: market_compare returns THREE benchmark sections (olv / clv /
checkpoint) + per-checkpoint 10-90% table; every record carries
market_line_type; MAE/RMSE/median/bias per benchmark; beat/ties with
denominators; O/U split per line type; disparity min/max/absmax +
checkpoint + progress retained.  Frontend renders the three sections.

CURRENT STATE: M008-SCORE-M1 (e858abd) fixed MAE/bias/denominators but
market_compare still uses ONE line type (checkpoint_market) via
prediction_scores.market_total; market_history has opening_total/
closing_total but NO timestamp columns; no per-line-type comparisons;
frontend shows a single MODEL vs MARKET block.

GAP: (a) no BLM-vs-OLV and BLM-vs-CLV aggregates; (b) no OLV/CLV
timestamps in market_history; (c) no per-checkpoint (10-90%) market
performance; (d) disparity not tied to progress/checkpoint/OLV/CLV.

ACCEPTANCE (final): scorecard answers "at each point was BLM closer to
the eventual total than the market, what was the disparity, where in the
game, did the market move toward BLM, what were OLV and CLV, did BLM
beat OLV/live/CLV" from clean fixture-verified data.

TRACER BULLET: one valid completed game with OLV + checkpoint lines +
CLV + actual → record carries market_line_type per comparison →
market_compare emits olv/clv/checkpoint sections with MAE/bias/beat/O-U
→ frontend renders three blocks → browser-verified.

RED TEST (planned, tracer dataset): synthetic valid game with BLM pred,
OLV, checkpoint line, CLV, actual, +positive/negative disparity, push,
BLM win, market win, tie, missing market, invalid game — assert
market_compare returns per-line-type sections, each beat/tie/denominator
reconciles, O/U per line type, negative disparity retained.

NEXT SINGLE ACTION: (authorized) write RED tests for _market_compare_sql
3-benchmark output + OLV/CLV timestamp columns, then implement.

## M008-SCORE-M1 (COMPLETE, deployed 46adb70) — forensic metric accounting

ROOT CAUSE: _market_compare_sql computed model_mae as mean(SIGNED
total_error) = -8.37 — that is BIAS mislabeled MAE. O/U 227/365/0 mixed
BLM vs an unnamed checkpoint line; beat-market was a bare rate without
denominator or market-beats-BLM / ties.

FIX (smallest slice, data untouched):
- model_mae = mean(abs) >= 0; model_bias = signed mean (separate).
- market_mae (abs) + market_bias (signed) explicit.
- BLM beat / Market beat / Ties from abs errors; counts + denominator,
  reconcile to n (688 = 220 + 467 + 1).
- O/U names line type (checkpoint_market); over/under/push + hits with
  visible denominator (450 / 688).
- signed disparity (BLM - market line) min/max/absmax retained
  (-97.2 .. +53.1, abs 97.2).
- OLV/CLV already separate (market_history opening_total/closing_total);
  _market_history_sql exposes them; missing = NULL.
- Frontend: MODEL vs MARKET + O/U PERFORMANCE blocks now show
  MAE/Bias/beat/ties with numerators and denominators.

LIVE VERIFIED (browser): "MODEL vs MARKET (line: checkpoint_market) |
Valid comparisons 688 | BLM MAE 14.34 | BLM Bias -5.95 | Market MAE
7.76 | Market Bias 2.33 | BLM beat 220/688 = 32% | Market beat 467/688 |
Ties 1/688" + "O/U PERFORMANCE (checkpoint_market) | BLM Over 254 |
Under 434 | Push 0 | Hit rate 450/688 = 65.4%".  206 tests (6 new).

REMAINING (later milestones): BLM vs OLV / BLM vs CLV sections,
checkpoint-by-checkpoint market performance (10-90%), disparity
pattern analysis, per-game forensic records in the frontend.

NEXT: M008-SCORE-M2 — BLM vs OLV and BLM vs CLV scorecard sections
(needs OLV/CLV timestamp columns in market_history).
