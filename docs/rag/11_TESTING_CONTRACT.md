# 11 — Testing Contract

Retrieval keywords: testing, pytest, proof, verification, conftest, heavy tests,
BLM_ALLOW_HEAVY_TESTS, focused tests, mutation proof, red first, no fabricated value,
production verify, api cross check, reconciliation test, alert lifecycle test.

**What this pack answers:** what constitutes proof in BLM, and why "pytest passed"
does not automatically mean "production behaviour verified".

---

## Rule 11.1 — "pytest passed" ≠ "production verified"

**Rule.** A green targeted suite proves the code path, not live behaviour. Production
verification requires live evidence (API response, DB query, journal line, counter)
separately.

**Source.** `AGENTS.md` §2.6/§2.7; the user's directive (97/97 API validation is the
model of the difference).

**Why.** Tests run against fixtures; production runs against the live feed.

**Valid.** "22 tests pass (code) AND game 31093644 now OK/RESULTS_PAGE (live)."

**Invalid.** "Tests pass, therefore production is fixed."

**Forbidden.** Reporting a test result as a production verification.

**Verify.** The handoff §5 has BOTH a tests block and a production-verification
table.

---

## Rule 11.2 — Do not run the full suite on this host; never block on a long run

**Rule.** Two independent reasons: (1) `tests/conftest.py` REFUSES to start when a
production BLM pipeline is live on the box (it names the PIDs; requires
`BLM_ALLOW_HEAVY_TESTS=1` to override) — because pytest competes for the CPU/RAM
that caused the 2026-09-29 restart storm; (2) a bare `pytest` dies in COLLECTION on
`blm-dev/tests/conftest.py` (`ImportPathMismatchError` — it shadows
`tests/conftest.py`), yielding zero tests. Run NAMED targeted files.

**Source.** `tests/conftest.py` (`_refuse_production_host_run`,
`_production_pipeline_pids`); skill notes.

**Why.** A heavy run on the production host can restart-storm the pipeline.

**Valid.** `python3 -m pytest tests/test_resulted_alert_settlement_2026_10_03.py -q`.

**Invalid.** `python3 -m pytest` (bare) on the production host.

**Edge.** An interrupted run is NOT evidence; never cite it. `--ignore=blm-dev`
fixes collection but not the conftest guard.

**Forbidden.** Waiting indefinitely on a long run — kill it and run targeted files.

**Verify.** `tests/conftest.py` guard; the specific named files that ran.

---

## Rule 11.3 — Frozen env defaults under test

**Rule.** `conftest.py` sets `BLM_PREDICTION_FREEZE=0` (unfrozen) and
`BLM_LIVE_CACHE_TTL_S=0` (no live cache) for the whole suite; dedicated tests
re-enable the frozen/cached behaviour explicitly.

**Source.** `tests/conftest.py` docstring.

**Why.** The suite exercises prediction/cache machinery as a legacy path; production
runs frozen/cached.

**Valid.** A freeze test sets `BLM_PREDICTION_FREEZE=1`.

**Invalid.** Asserting frozen behaviour under the default suite env.

**Forbidden.** Hardcoding frozen behaviour into a general test.

**Verify.** The env value the test sets.

---

## Rule 11.4 — RED-first and mutation proof

**Rule.** Prefer writing the failing test first, confirm RED, then implement.
Where a fix is correctness-critical, mutate the fix back and confirm the test goes
RED (mutation proof).

**Source.** `DECISIONS.md` §15.C; e.g. the 2026-10-03 reconciler fixes
"mutation-proven RED".

**Why.** A test that passes both before and after proves nothing.

**Valid.** Reverting the retry clause fails 3 re-arm tests.

**Invalid.** A test that is green on the broken code.

**Forbidden.** Deleting/weakening a test to make a change pass.

**Verify.** The mutation run's before/after counts.

---

## Rule 11.5 — The "no fabricated value" invariant

**Rule.** Missing inputs must return `NULL`/fail-closed, never a borrowed or
made-up value. This is a recurring test invariant.

**Source.** `DECISIONS.md` §15.H.

**Valid.** A missing market → honest NULL, never the opening line.

**Forbidden.** A test that asserts a fallback value where the contract is NULL.

**Verify.** Grep the test tree for the invariant; run the fail-closed tests.

---

## Rule 11.6 — Targeted verification is on-disk; post-activation re-run is required

**Rule.** After activating a fix (restart), re-run the focused tests POST-activation;
pre-activation green does not carry over, and the strongest proof is a live counter
unreachable under the old code.

**Source.** skill `references/activating-a-verified-fix.md`.

**Valid.** `COUNT(*) ... attempt >= 4` went 0 → 6 after the restart.

**Forbidden.** Citing a pre-restart green as proof the running process is fixed.

**Verify.** Behavioural counter before/after + module sha256.

---

## Rule 11.7 — Distinguish new failures from pre-existing

**Rule.** Capture a baseline of pre-existing failures; never credit a fix with an
unrelated incident, and never call a pre-existing failure a regression.

**Source.** `DECISIONS.md` §13.E/§15.F; `AGENTS.md` §2.7.

**Valid.** "20 failures on the branch fail identically on the base — 0 new."

**Forbidden.** Reporting a pre-existing red as "caused by my change".

**Verify.** Run the failing tests on the base commit / a scratch worktree.

---

## Rule 11.8 — Independent ad-hoc verifiers outrank suite-green alone

**Rule.** For correctness-critical changes, an independent verifier (AST proofs,
real SQL on a tempfile DB, hand-derived accept/reject truth) is stronger than a
green suite. Document it with a path and an exit code.

**Source.** The `hermes-verify-*` pattern; `docs/rag/` examples.

**Valid.** `/tmp/hermes-verify-panel-discovery.py` 18/18 PASS, exit 0.

**Edge.** These are NOT suite-green; report them as such.

**Verify.** The verifier's exit code + the claims it proves.

---

## Rule 11.9 — Documented pinned tests (by behaviour)

| Behaviour | Pinned test file |
|---|---|
| Alert lifecycle | `tests/test_under_alert_lifecycle.py` |
| Live-market gate | `tests/test_live_market_gate.py` |
| Triggered/frozen line | `tests/test_active_alert_triggered_line.py` |
| Settlement semantics | `tests/test_settlement_semantics.py` |
| Final colour | `tests/test_final_result_color.py` |
| Resulted-alert settlement (2026-10-03) | `tests/test_resulted_alert_settlement_2026_10_03.py` |
| Backfill | `tests/test_resulted_backfill_2026_10_03.py` |
| Quarter-score identity | `tests/test_quarter_score_capture_identity_2026_10_03.py` |
| Panel discovery coverage | `tests/test_panel_discovery_coverage_2026_10_03.py` |
| Fingerprints | `tests/test_under_fingerprints.py` |
| Classification duration | `tests/test_classification_duration.py` |

> The suite size/counts recorded in `DECISIONS.md` §15.A/§15.F are STALE vs the
> current tree (which carries the 2026-10-03 test files). See CONTRADICTIONS.md.
