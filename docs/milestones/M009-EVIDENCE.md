# M009 — Market vs Fair Value: EVIDENCE TRAIL

Scorecard redesign milestone M009-M1 + M009-M1b.
Primary analytical question: MARKET LINE vs FAIR VALUE at every
checkpoint (10..100%), with the disparity (live - fair) retained
signed and stored immutably.

Evidence ladder: L1 CODE -> L2 UNIT (RED->GREEN) -> L3 FULL SUITE ->
L4 DEPLOYED -> L5 LIVE VERIFIED.

---

## M009 SECTION 18 — FIXTURE-INTEGRITY REGRESSION TEST (COMPLETE, 1b23253)

The Karsiyaka/Denizli-vs-Karsiyaka/Korfez incident is now a PERMANENT
regression test (`tests/test_fixture_integrity.py`, 4 tests).  It
encodes, with the incident's real game ids and incident-era values:

- 30741194 (ended, Pinar Karsiyaka vs DENIZLI) carries ONLY 157.5
- 30741844 (live, Pinar Karsiyaka vs KORFEZ) carries ONLY its own WS
  batch; the main line is the LOWEST (182.5, never 186.5)
- line helpers are scoped per source_game_id; _frozen_market_line honors
  at-or-before (an observation after the checkpoint is honestly None)
- the API detail serves each game's own line only

VERIFICATION:
- GREEN: 4/4 (one initial RED finding — my first expectation asserted
  frozen=157.5 at t=0; the code correctly returned None because the WS
  observation lands at +6min: honest at-or-before semantics.  Test
  fixed to encode the correct expectation.)
- MUTATION-PROVEN: neutering the per-fixture filter in the two line
  helpers (`source_game_id=?` -> tautology) makes 2 tests FAIL —
  the test genuinely catches the conflation class.  Restored.
- Full suite: 225 passed, 0 failed (221 + 4).
- Live anchor: the real pair still isolates in production (verified via
  /api/v4/game/30741194 = 157.5 ended; /api/v4/game/30741844 = own WS
  line, now 191.5 as the market moved — isolation holds).

---

## M009-M4 — CHECKPOINT MARKET-LINE ANALYTICS: MOMENTUM + TIME-OF-DAY + EDGE BUCKETS (COMPLETE, c0106a3)

Directive: strengthen market-line analytics; preserve M3 freshness
semantics; time-of-day segmentation; false-momentum analytics; large-
edge investigation; duplicate protection; contamination exclusion.

WHAT CHANGED:
- checkpoint_market gains momentum_state / momentum_strength /
  false_momentum / false_momentum_confidence — captured at record time
  from snapshots AT-OR-BEFORE the checkpoint (no look-ahead), using the
  API's single signal definition (scorecard imports _momentum /
  _detect_signals from blm_v4.api — no duplicated logic, no api.py
  behavior change).
- time_of_day aggregation (in market_vs_fair): per hour-of-day (24) +
  configurable bands (env BLM_TOD_BANDS, default 0-6,6-12,12-18,18-24)
  with N, over/under/push counts, BLM win rate, market win rate (the
  market's side of the line = BLM losses), avg BLM-market differential.
  Game start = earliest snapshot timestamp (first_seen_at is the
  collector's tracking time, not fixture start — found via debug).
- edge_buckets aggregation: |BLM-market| magnitude buckets (0-2 / 2-5 /
  5-10 / 10-15 / 15-20 / 20+) split by direction (BLM_OVER = fair >
  market, BLM_UNDER = fair < market), each with n / win / loss / push /
  win_rate / avg_age — large apparent edges stay attributable to
  freshness.
- api.py game detail: momentum fields exposed (column-guarded).
- tests/test_m009_m4_analytics.py (7); _build helper gains start=.

VERIFICATION:
- RED: 5 of 7 failed pre-implementation (momentum columns + tod +
  edge_buckets missing); duplicate + contamination tests passed (existing
  behavior).
- Findings: (1) upsert_game hardcodes first_seen_at = now → game start
  proxy = MIN(snapshot.captured_at) (deterministic, honors start=);
  (2) the 20+/BLM_UNDER bucket legitimately mixes fresh + stale rows —
  avg_age revealing that mix IS the large-edge investigation (test
  rewritten to assert it).
- Full suite: 246 passed, 0 failed (239 + 7).  M3 freshness suite green
  (regression confirmed).
- DEPLOYED: blm-server + blm-collector restarted.  LIVE VERIFIED (real
  production data): /api/v4/scorecard time_of_day hour 2 — n=443,
  over 133 / under 263, blm_win_rate 0.69, market 0.31, avg_diff -11.65;
  bands 0-6 BLM win 66% / 6-12 58% / 12-18 56% / 18-24 57% (measured,
  not hard-coded); edge_buckets 0-2 BLM_UNDER win 208/278 = 75% vs
  BLM_OVER 97/272 = 36%; 20+ BLM_UNDER n=834 vs BLM_OVER n=184.
  Game detail 30749637 serves momentum fields (NULL — pre-M4 frozen
  rows, honest; new completions populate).
- Commits: c0106a3 (code+tests) + docs commit (this).  Pushed.

NEXT: M009-M5 — disparity bands + O/U outcome analysis / frontend
consumption of the new sections — after explicit go.

---

## M009-M3 — MARKET FRESHNESS LAYER (COMPLETE, d656680)

Directive sections 2-5, 22, 24.  Every frozen market line now carries
its OBSERVATION TIMESTAMP so the system distinguishes LIVE / STALE /
MISSING and NEVER treats a stale differential as a live edge.

WHAT CHANGED:
- checkpoint_market gains `market_timestamp` (frozen line's observation
  time — last carrying snapshot's captured_at, or WS captured_at).
  Idempotent ALTER migration (_ensure_cm_market_timestamp); old rows
  keep NULL = honest missing, never fabricated.
- Helpers: `_frozen_market_obs` (line, ts) refactor (preserves frozen-
  line semantics — M1 regression suite green), MARKET_STALE_SECONDS =
  existing dashboard definition (300s, configurable via
  BLM_MARKET_STALE_SECONDS), `_market_age_seconds` (checkpoint_ts -
  market_ts, clamped >=0), `_market_status` (LIVE <= 300s | STALE |
  MISSING), `_freshness_bucket` (0-10/10-30/30-60/60-120/120-300/300+s),
  `_edge_class` (LIVE_EDGE only when LIVE; STALE -> STALE_DIFFERENTIAL).
- `blm_market_diff` = BLM - market (positive = BLM higher; the exact
  negation of M009's market_vs_fair — both exposed, never merged).
- Aggregation: per-checkpoint n_live/n_stale/n_missing, live vs stale
  outcome counts, avg_market_age; `market_freshness` age-bucket x
  outcome section.
- API game detail: rows carry market_age_seconds / market_status /
  freshness_bucket / edge_class / blm_market_diff (lazy import, guarded
  on the new column for pre-migration DBs).

VERIFICATION:
- RED: 6 tests failed on collection (imports missing) — RED confirmed.
- One real finding: my first _frozen_market_obs returned the FIRST line
  at-or-before (refactor bug); fixed to LAST (the original semantic).
  Boundary: ==300s is LIVE (matches the existing `age <= 300`
  dashboard definition).
- MUTATION-PROVEN: neutering the staleness guard (`LIVE if True`)
  fails 2 tests (stale counted as live).  Restored (note: git checkout
  reverted the uncommitted M3 patches too — re-applied, verified).
- Full suite: 239 passed, 0 failed (233 + 6).
- Ad-hoc hermes-verify-m3.py (deleted): G-MIX pct10 LIVE age 0
  LIVE_EDGE diff +10.4 (BLM higher); G-STALE pct20 STALE age 690s
  300s+ STALE_DIFFERENTIAL; scorecard pct50 n=2 live=1 stale=1
  missing=1.  NOT the L5 basis.
- DEPLOYED: blm-server + blm-collector restarted; LIVE VERIFIED on
  real production data: /api/v4/game/30749637 all 10 rows
  market_status=MISSING (pre-M3 frozen rows — honest, never
  fabricated); /api/v4/scorecard pct50 n=445 n_live=0 n_stale=0
  n_missing=467; market_freshness buckets present.  New-game LIVE/
  STALE rows populate as the 60s loop records future completions.
- Commits: d656680 (code+tests) + docs commit (this).  Pushed.

NEXT: M009-M4 — frontend consumption (dashboard MARKET FRESHNESS /
LIVE EDGE STATUS per §17, §20) — after explicit go.

---

## M009-M2 (REFINED) — MARKET VS FAIR PRIMARY SCORECARD (COMPLETE, b6798f2)

The refinement directive ("Market vs Fair is the primary scorecard")
IS the M2 spec; it superseded the earlier M2 hold.  Built on the
immutable checkpoint_market rows (M1).

WHAT CHANGED:
- scorecard.py: `_market_vs_fair_sql(conn)` + `Scorecard.market_vs_fair()`.
  Per-checkpoint (10..100%): n (games with BOTH market+fair), n_fair,
  avg_market, avg_fair, avg_mf (SIGNED mean market-fair), median_mf,
  abs_mf, over/under/push value counts + pct (of market-bearing rows),
  over_win/over_loss/under_win/under_loss/push_outcome, position_win_rate
  (pushes excluded from denominator), avg_olv_to_clv, move_toward/
  move_away/move_unchanged.  games[]: per clean game — id, teams, OLV,
  CLV, final, outcome vs OLV, outcome vs CLV, progressive rows[]
  (checkpoint_pct, market, fair, mf, signal, actual, outcome).
- api.py: /api/v4/scorecard gains `market_vs_fair`.
- dashboard.js: MARKET VS FAIR VALUE is now the FIRST/PRIMARY block
  (per-checkpoint table with signed Avg M-F + Under/Over Value %;
  position-outcome table; market-movement table; GAME-LEVEL SCORECARD
  with <details> progressive tables).  Model MAE / Market MAE /
  model-beat-market demoted to labelled DIAGNOSTIC (population:
  prediction_scores fragment=0; line: checkpoint_market).
- tests: test_m009_mvf_aggregation.py (6, RED confirmed), 
  test_m009_mvf_frontend.py (2, RED confirmed).

RED findings: (1) my hand-computed fair values were wrong — fair
depends on the market via the 0.7*pace+0.3*line blend (G-PUSH market
143 -> fair 137.3, not 148.4); the aggregation was correct; tests
rewritten to compute expected stats from the raw checkpoint_market
rows + model-independent invariants (signs, counts, pushes, moves).
(2) frontend test asserted runtime-interpolated text; fixed to
source-level markers.

FULL SUITE: 233 passed, 0 failed (225 + 8).

DEPLOYED + LIVE VERIFIED (2026-08-31):
- /api/v4/scorecard market_vs_fair pct50 (real production data):
  n=440, avg_market 191.13, avg_fair 183.2, avg_mf +8.62 (signed),
  median_mf 7.5, abs_mf 14.32, over_value 147 (33%), under_value 293
  (67%), over_win 75 / over_loss 72, under_win 171 / under_loss 122,
  position_win_rate 56%, avg_olv_to_clv +3.09, move_toward 223 /
  move_away 195.  4615 checkpoint rows served.
- Served dashboard.js carries: MARKET VS FAIR VALUE (1), GAME-LEVEL
  SCORECARD (1), MODEL vs MARKET — DIAGNOSTIC (1).

The scorecard now answers the primary question from real data: at 50%
BLM fair averaged 183.2 vs bookmaker 191.13 (avg +8.6 UNDER value),
Under value flagged 67% of the time, and the UNDER position won 171/293
(58%) — the market moved TOWARD BLM 223 vs AWAY 195.

NEXT: M009-M3 — OLV/CLV relationship analysis / convergence (explicit go only).

---

## L1 CODE — implementation exists

Commits (all pushed to github.com:abwarren/BLM, SSH):
- a891104 — feat(scorecard): immutable per-checkpoint Market-vs-Fair
  history — checkpoint_market table (M009-M1)
- 61929bb — test(scorecard): drop dead vars in market_move_toward_blm
  RED test (M009-M1)
- b8df068 — feat(api): expose immutable Market-vs-Fair checkpoint
  history in game detail (M009-M1b)
- a3c0881 — docs(milestones): M009-M1b complete
- ffa5f87 — docs(milestones): M009-M1/M1b DEPLOYED + live evidence

Files changed:
- blm_v4/scorecard.py   — checkpoint_market schema (immutable, UNIQUE
  per game+checkpoint, INSERT OR IGNORE = frozen at first write);
  record_checkpoint_market() + helpers (_first_verified_line,
  _last_verified_line, _market_vs_fair_signal, _checkpoint_outcome,
  _market_move_toward_blm); run() returns checkpoint_market stats.
- blm_v4/api.py         — _game_checkpoint_market(); /api/v4/game/{id}
  returns market_vs_fair[] alongside checkpoints[] (detail route only;
  /live and /games stay lean).
- tests/test_m009_checkpoint_market.py — 10 RED tests (M009-M1).
- tests/test_m009_mvf_api.py           — 4 RED tests (M009-M1b).
- docs/milestones/CURRENT.md           — milestone records.

## L2 UNIT — RED observed first, then GREEN

RED (implementation reverted to pre-M009, test files present):
    git checkout e858abd -- blm_v4/scorecard.py
    ./venv/bin/python -m pytest tests/test_m009_checkpoint_market.py -q
    -> 10 failed in 6.00s
       all: AttributeError: 'Scorecard' object has no attribute
            'record_checkpoint_market'
    ./venv/bin/python -m pytest tests/test_m009_mvf_api.py -q
    -> 4 failed (missing 'market_vs_fair' in detail payload)

GREEN (implementation restored):
    ./venv/bin/python -m pytest tests/test_m009_checkpoint_market.py -q
    -> 10 passed in 6.00s
    ./venv/bin/python -m pytest tests/test_m009_mvf_api.py -q
    -> 4 passed

Coverage: rows for clean game / signed disparity + signal / outcome
incl. push / OLV-CLV-actual linkage / market_move_toward_blm /
immutability on re-run / ineligible games excluded (live, INVALID,
fragment) / honest NULLs on missing market / WS-fallback lines /
terminal fair floor / API exposure + honest NULLs + live-empty + lean.

## L3 FULL SUITE — no regression

    ./venv/bin/python -m pytest -q
    -> 221 passed, 0 failed   (was 207 before M009-M1)

## L4 DEPLOYED — running services carry the code

    systemctl restart blm-server blm-collector   (~19:47 SAST)
    systemctl status -> both active (running)
    WorkingDirectory=/home/gdi/BLM (the git tree) -> deploy == tree

Scorecard loop (server.py) runs sc.run() every 60s: creates +
populates checkpoint_market automatically.  No manual DB writes.

## L5 LIVE VERIFIED — real production data via the running service

    curl -s http://127.0.0.1:2262/api/v4/game/30749637
    (Betual NBA, ended: Tianjin Pioneers Virtual vs Zhejiang Lions
     Virtual, actual total 165)

    "market_vs_fair": [10 rows, checkpoint_pct 10..100]
      pct10  opening 172.5  live NULL   fair 100.0  closing 172.5
             mvf NULL  signal NULL  outcome NULL        (honest NULL:
             no market observed at-or-before that checkpoint)
      pct20  opening 172.5  live 172.5  fair 182.1  closing 172.5
             mvf -9.6  signal OVER_VALUE  blm_vs_olv 9.6
      olv_to_clv 0.0 (line never moved: 172.5 -> 172.5)

    Notes:
    - pct10 fair 100.0 = the KNOWN pace-unavailable sentinel (M004:
      quarter=NULL event-view rows -> pace None -> 100 fallback),
      recorded as-is, honest.
    - blm_vs_olv -72.5 at pct10 is the same sentinel arithmetic.

## VERIFICATION STATUS

- M009-M1  : COMPLETE, DEPLOYED, LIVE VERIFIED  (commits a891104..ffa5f87)
- M009-M1b : COMPLETE, DEPLOYED, LIVE VERIFIED  (commit b8df068)
- M009-M2 (REFINED): COMPLETE, DEPLOYED, LIVE VERIFIED  (commit b6798f2)
- M009-M3  : COMPLETE, DEPLOYED, LIVE VERIFIED  (commit d656680)
- M009-M4  : COMPLETE, DEPLOYED, LIVE VERIFIED  (commit c0106a3)
- Full suite 246 passed, 0 failed at L3 (fresh).
- Ad-hoc synthetic verifications (hermes-verify-* scripts, /tmp,
  deleted after run) used during development; NOT the evidence basis
  for L4/L5 — those are the running service + real DB.

## NEXT

M009-M5 — disparity bands + O/U outcome analysis / frontend
consumption (sections 9, 13, 20 of the directives).  After explicit go.
