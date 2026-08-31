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
- Full suite 221 passed, 0 failed at L3 (fresh).
- Ad-hoc synthetic verifications (hermes-verify-* scripts, /tmp,
  deleted after run) used during development; NOT the evidence basis
  for L4/L5 — those are the running service + real DB.

## NEXT

M009-M2 — scorecard aggregation: per-checkpoint Avg Market / Avg Fair /
Avg M-F table + Under/Over value % + outcome analysis, honest N per
checkpoint (sections 6-7 of the directive).
