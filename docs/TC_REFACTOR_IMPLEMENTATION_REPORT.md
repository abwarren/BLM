# Trade Cohort refactor — implementation report (EC2 test only)

**Status:** implementation complete in the designated EC2 test environment. **Not deployed,
not merged, not promoted.** Awaiting operator review (strategy doc §9 Phase 6/§11).

- **Branch:** `feature/tc-cohort-refactor-test` (base `docs/environment-boundaries-test-first` @ `5988e19`)
- **Commits:** `a91a1fe` (implementation) · second commit (this report) — see `git log`
- **Environment:** `blm-stage-cpt` (EC2 `i-0d6c4d01114f52bf4`, 13.247.58.81) — identity and
  isolation evidence in `docs/TC_REFACTOR_TEST_ENV_REPORT.md`
- **Production:** untouched (`portable-ubuntu` / `blm.haaats.xyz`); Auto-Bet untouched.

## 1. Files changed

| file | what |
|---|---|
| `blm_v4/cohorts/__init__.py` | public surface of the package |
| `blm_v4/cohorts/spec.py` | frozen spec: 7 cohorts, common gates, `rule_version = tc-1.0.0`, **named** precedence |
| `blm_v4/cohorts/evaluator.py` | ONE pure evaluator; UNKNOWN vs NOT_QUALIFIED; per-cohort leg verdicts |
| `blm_v4/cohorts/metrics.py` | Wilson 95 %, break-even, PUSH-aware outcome counting, UNDER settlement |
| `blm_v4/cohorts/rescore.py` | test-only derived ledger: temporal firewall, idempotent, reconciliation |
| `blm_v4/cohorts/api.py` | read-only `/api/v4/cohorts/{spec,summary,ledger,comparisons,quality}` |
| `blm_v4/dashboard/static/{index.html,styles.css,dashboard.js}` | Trade Cohorts panel + collapsed Legacy Diagnostics |
| `server.py` | mounts the cohorts router (2 lines) |
| `tests/test_tc_cohort_spec_2026_10_26.py` | 86 boundary/unknown/precedence tests |
| `tests/test_tc_rescore_firewall_2026_10_26.py` | 10 firewall/idempotency/settlement tests |
| `tests/test_tc_cohort_api_2026_10_26.py` | 9 API contract + read-only tests |
| `docs/TC_REFACTOR_TEST_ENV_REPORT.md` | Phase 0 environment/isolation evidence |

No schema change to any existing table. The only new storage is the derived
`/srv/blm/data/derived/cohort_rescore.db` (droppable at will).

## 2. Cohort spec (frozen, versioned)

`rule_version = tc-1.0.0`, effective `2026-10-26T00:00:00Z`. Common gates: progress
75–92 % inclusive, scored points ≥ 70, Cyber 2K26 excluded by slug, line-move leg OFF
(False passes, True fails, unknown → UNKNOWN), all operands finite and positive.

| code | pace ratio (strict >) | shortfall (>=) | UNDER price (>=) |
|---|---:|---:|---:|
| TC-X ELITE | 1.16 | −0.50 | 1.60 |
| TC-E EDGE | 1.12 | −0.50 | 1.60 |
| TC-S SHARP | 1.08 | −0.50 | 1.60 |
| TC-C CORE | 1.04 | −0.50 | 1.60 |
| TC-W WIDE | 1.00 | −0.50 | 1.60 |
| TC-D DEEP | 1.00 | +0.50 | 1.60 |
| TC-C+ CORE+ | 1.04 | +0.25 | 1.80 |

**Precedence (primary label):** `TC-X, TC-E, TC-S, TC-C+, TC-C, TC-D, TC-W` — a named,
tested policy (`spec.PRECEDENCE`), never iteration or hash order. Rationale: pace ladder
first, then the two thinner side routes (C+ adds price+shortfall on top of C's pace;
D trades pace for a large shortfall), then WIDE. `qualifying_cohorts` keeps **every**
match and remains authoritative; the precedence is a reporting label only.

Both are pinned by tests (including a 5-seed mapping-shuffle test and a C+ > C test).

## 3. Data lineage: actual firing vs checkpoint

This system **persists no alert ledger** — alerts are derived live from observations.
A "firing" in the rescore is therefore **reconstructed**: the first observation of a game
where the production alert condition held (progress ≥ 75, required > league × 1.04, live
line **and** price present). Everything else that satisfies a cohort is stored as
`source_type = 'checkpoint'` and is never counted as a fired alert. UNKNOWN rows carry a
reason code and never pass.

**Temporal firewall** (all enforced in code, all tested):
- progress from the observation's own quarter/clock (classification-aware quarter length);
- required/actual pace from that row's score and its own live line;
- league reference from games with `result_at <= observation` (`prior_only`, default);
- settlement joined **after** evaluation, scored against the FROZEN trigger line;
- no closing line, final total, or later snapshot can change a firing evaluation
  (test: two runs with different closing lines → identical firing rows).

**Known deviation:** the EC2 slice has no `blm_metrics_clean.db` (the collector's clean
pipeline never ran here), so firing-time inputs are reconstructed from `snapshots` rather
than read from `clean_projections`. This is deterministic and leakage-free, but it is not
byte-identical to the production projector (which also consumes a WS market override).

## 4. Stage counts and reconciliation (EC2 slice 2026-10-22T13:50 → 10-23T20:34Z)

| stage | count |
|---|---:|
| snapshots in source DB | 163,500 (1,114 games) |
| snapshots carrying line **and** price | 37,816 (23.1 % market coverage) |
| games scanned (non-Cyber) | 1,084 |
| Cyber games excluded | 31 |
| observations evaluated | 134,730 |
| … qualified | 52 |
| … NOT_QUALIFIED (valid, out of gate) | 13,959 |
| … UNKNOWN (reason-coded) | 120,719 |
| reconstructed firings | 75 |
| qualifying checkpoints (stored) | 33 |
| ledger rows stored | 1,155 |
| settled firings | 73 (55 UNDER / 18 OVER / 0 PUSH / 2 pending) |
| unique qualified games | 19 |

Largest UNKNOWN driver: missing market price/line in the observation (76 % of snapshots
carry no market) — a property of this test slice, not of the evaluator.

### Per-cohort scorecards (unique games, prior-only league reference)

| cohort | games | UNDER | OVER | PUSH | UNDER rate | Wilson 95 % | break-even @1.60 |
|---|---:|---:|---:|---:|---:|---|---:|
| TC-X ELITE | 1 | 1 | 0 | 0 | 100.0 % | 20.7–100 % | 62.5 % |
| TC-E EDGE | 6 | 5 | 1 | 0 | 83.3 % | 43.6–97.0 % | 62.5 % |
| TC-S SHARP | 9 | 8 | 1 | 0 | 88.9 % | 56.5–98.0 % | 62.5 % |
| TC-C CORE | 19 | 13 | 6 | 0 | 68.4 % | 46.0–84.6 % | 62.5 % |
| TC-W WIDE | 19 | 13 | 6 | 0 | 68.4 % | 46.0–84.6 % | 62.5 % |
| TC-C+ CORE+ | 0 | – | – | – | – | – | 62.5 % |
| TC-D DEEP | 0 | – | – | – | – | – | 62.5 % |

Sensitivity run (`--league-reference=full`, production-parity reference): 72 firings,
51 UNDER / 18 OVER, 16 unique qualified games. Both runs are retained
(`cohort_rescore.db` = prior-only, `cohort_rescore_full.db` = full-sample).

**Limitations, stated plainly:** 31-hour window; n ≤ 19 per cohort; overlapping cohorts
(TC-C = TC-W here) are not independent samples; TC-C+/TC-D have zero observations in this
slice; firings are reconstructed, not recorded; historical rescore is **descriptive, not
prospective confirmation**; nothing here disputes or replaces the legacy 613-game /
58.73 % figures — those are legacy-rule numbers and were not reused.

## 5. Tests and commands (all on EC2 test)

```
python -m pytest tests/test_tc_cohort_spec_2026_10_26.py \
                  tests/test_tc_rescore_firewall_2026_10_26.py \
                  tests/test_tc_cohort_api_2026_10_26.py -q
→ 105 passed

python -m pytest tests/ -q                      # whole suite on this branch
→ 54 failed, 451 passed

baseline: git worktree at 5988e19 (unmodified)
python -m pytest tests/ -q
→ 54 failed, 346 passed
```

The 54 failures are **pre-existing and identical** at the base commit — all in
`tests/test_v2_*.py`, caused by `pytest-asyncio` not being installed in this venv
("async def functions are not natively supported"). No new failures were introduced;
the branch adds 105 passing tests. Nothing was hidden or waived.

**Read-only proof:** re-running the rescore leaves the source DB byte-identical
(`272490496 1793048878` before → after), and the API test pins the derived DB's size and
mtime across all five endpoints.

## 6. UI evidence

Trade Cohorts panel rendered from the live API on the test instance (port 2263; the
designated :80 entry point still serves the untouched staging mirror — see §8):

- `/tmp/tc/cohorts-panel.png` on the test host (element-scoped capture, no secrets)
- 7 cohort cards with games/UNDER/OVER/PUSH/pending/rate/Wilson/coverage/avg-price/break-even
- rule + window + league-reference pills and the "firings are reconstructed" caveat
- fired-alert ledger with firing-time inputs, all memberships, frozen trigger line, result
- collapsed **Legacy diagnostics** section (labelled legacy rules, nothing deleted)
- console errors during capture: none

API examples (`curl http://127.0.0.1:2263/api/v4/cohorts/...`):

```
/spec          → {"rule_version":"tc-1.0.0","cohorts":[7],"precedence":[...]}
/summary       → totals {ledger_rows 1155, firings 75, checkpoints 1080, unknown 1047}
/ledger?cohort=TC-X&source_type=reconstructed_firing&limit=1
               → 2026-10-22T16:37 betual-nba 31361686 TC-X prog 87.5 pts 166
                 req 8.27 act 6.24 league 3.94 ratio 2.102 shortfall 2.03
                 price 1.8 line 168.5 UNDER (133) QUALIFIED
/quality       → unknown_reasons + caveats + market-row counts
/comparisons?by=price → bucketed UNDER rates with denominators
```

## 7. Production / Auto-Bet boundary

- No production file, service, DB, migration, backfill or rescore write.
- No Auto-Bet change: no enabled/disabled toggle, no cohort change, no stake/limit/kill
  switch change, no browser submission, no bets. The panel is display-only and the stage
  unit carries no `BETTING_*` variables (executor fails closed).
- The deployed staging service (`:2262` behind `:80`) was **not** replaced: this branch is
  based on the docs branch (`5988e19`), which predates the live server branch — deploying
  it there would regress stage. Acceptance therefore runs as a parallel instance on 2263.

## 8. Open items / proposed promotion checklist (NOT executed)

1. **Rebase** `feature/tc-cohort-refactor-test` onto `fix/price-mapping-2026-10-06` (or
   current main) so the refactor sits on the live server code before any promotion.
2. Re-run the 105 tests plus the full suite on the rebased branch.
3. Build the read-model against production **data** (read-only replica or a copied
   snapshot), re-run both league-reference modes, and reconcile counts; expect materially
   different numbers from this 31-hour slice.
4. Decide the league-reference policy for production (`prior_only` is causally strict but
   yields far fewer evaluable rows; `full_sample` matches the cached production reference).
5. Review the diff for hidden writes/secrets: `git diff 5988e19..HEAD`.
6. Promote only with a separate, explicit operator approval that names the commit SHA,
   migration/backfill policy, backup/rollback, maintenance window and stop criteria.
