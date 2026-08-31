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
- Full suite 233 passed, 0 failed at L3 (fresh).
- Ad-hoc synthetic verifications (hermes-verify-* scripts, /tmp,
  deleted after run) used during development; NOT the evidence basis
  for L4/L5 — those are the running service + real DB.

## NEXT

M009-M3 — OLV/CLV relationship analysis / market-convergence stats
(after explicit go).  M009-M2 was the refinement directive's scope.
