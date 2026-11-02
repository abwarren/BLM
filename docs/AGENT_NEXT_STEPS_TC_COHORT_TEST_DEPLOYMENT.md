# Agent Directive — TC Cohort Refactor Next Steps (Test Only)

**Owner / reviewer:** Warren  
**Date:** 2026-10-27  
**Implementation branch:** `feature/tc-cohort-refactor-test`  
**Scope:** implement the review follow-ups, rebase safely, validate, and deploy only to the isolated EC2 test instance.  
**Authorization:** Test deployment to the parallel `127.0.0.1:2263` instance is authorized. Production promotion is NOT authorized.

Read before acting:
- `docs/TRADE_COHORT_REFACTOR_IMPLEMENTATION_STRATEGY.md`
- `docs/TC_REFACTOR_TEST_ENV_REPORT.md`
- `docs/TC_REFACTOR_IMPLEMENTATION_REPORT.md`
- `docs/ENVIRONMENT_BOUNDARIES.md`
- `docs/PLAN-005-RESEARCH-GATES-AND-PROSPECTIVE-CONFIRMATION.md`

## Non-negotiable boundaries

1. Work in the isolated EC2 test checkout only. Re-verify hostname, instance/account, branch, working tree, process/service targets, data paths, credentials/secret *names*, network reachability, and deployment target before making changes.
2. Production is the host serving `https://blm.haaats.xyz/`. Do not connect to, copy from, or write to production during this task.
3. Preserve the existing `:2262` service and `:80` entry point that serve the staging mirror. Do not replace, restart, or reconfigure them for this refactor.
4. Only the parallel test instance `127.0.0.1:2263` may be deployed/restarted for this task, after preflight and code validation.
5. No production merge/deploy/restart/migration/backfill/rescore, DNS/proxy/firewall changes, live betting, real submissions, Auto-Bet settings changes, browser-submit authorization, or bet execution.
6. Do not enable CI/CD auto-deploy. Do not expose secrets or transmit credentials over plain HTTP.
7. If environment identity/isolation is uncertain, source DB metadata changes, or the target would touch `:2262`/`:80` or production, stop and report. Do not improvise.

## Work plan

### Step 1 — Establish exact baseline
- Record current branch, HEAD SHA, working-tree state, remotes, and existing uncommitted changes. Preserve unrelated work; do not reset or discard it.
- Verify the documented test environment against the current host and service state.
- Capture before-state for source DB size/mtime and relevant service/unit configuration; record hashes/checksums where appropriate without exposing data.
- Confirm the cohort derived DB path is under the test data root and source DB connections use read-only mode.
- Report the baseline before continuing if any detail differs materially from the existing isolation report.

### Step 2 — Fix evaluator input validation
- In `blm_v4/cohorts/evaluator.py`, validate `scored_points` as an integer-valued, non-negative numeric input before converting or returning it.
- Reject Boolean values, fractional values (for example `10.9`), negative values, malformed values, NaN, and positive/negative infinity as invalid/UNKNOWN with stable reason codes.
- Do not silently truncate fractional input.
- Preserve existing spec semantics for valid integer scores and the minimum score gate.
- Add tests for valid integer values, fractional, negative, Boolean, numeric strings if accepted by the current input contract, malformed strings, None, NaN, and infinities. Document the chosen string-handling contract.

### Step 3 — Enforce strictly prior league-reference semantics
- Inspect `blm_v4/cohorts/rescore.py` and all timestamp normalization paths.
- A settled game with `result_at == observed_at` must not contribute to the league reference unless a reliable source proves the result was available before evaluation. Default behavior must be strict-before.
- Avoid lexical timestamp comparisons unless all timestamps are normalized to a canonical sortable UTC representation.
- Add regression tests for result timestamp before, exactly equal to, and after the observation; include timezone/format variants if the schema permits them.
- Ensure settlement remains joined only after the firing-time evaluation and cannot alter cohort qualification.

### Step 4 — Align market-coverage denominators
- Inspect API aggregation, comparison endpoints, and dashboard labels.
- The headline outcome cohort is first reconstructed firing per unique game per cohort; checkpoint rows must not silently inflate its market-coverage denominator.
- Either compute coverage over the same population as the displayed outcome rate, or report distinct metrics with explicit names and denominators (for example, firing-level coverage and checkpoint-observation coverage).
- Ensure every displayed percentage has a clear numerator and denominator, and that filters do not silently mix `reconstructed_firing` with `checkpoint`.
- Add API tests for denominators, source-type filters, and market data present/missing combinations; update UI labels/tooltips accordingly.

### Step 5 — Rebase/port onto current server code in test only
- Fetch the latest remote refs and identify the current successor of `fix/price-mapping-2026-10-06`; record the chosen base SHA and reason.
- Rebase or port the cohort changes onto that current server code line in the isolated test checkout only. Do not force-push over collaborators' work; if a force-push is required, stop and request direction.
- Resolve conflicts by preserving live-server fixes and keeping this refactor analytics/read-only only. Do not change live alert behavior, collector cadence, betting execution, or production schema.
- Review the complete diff against the chosen base for unexpected changes, secret material, deployment hooks, migrations, and writes.
- If conflicts are ambiguous or cannot be resolved safely, stop and provide a conflict report rather than guessing.

### Step 6 — Test and data validation after fixes/rebase
Run and record exact commands, exit codes, commit SHA, counts and warnings for:
- focused evaluator/spec/rescore temporal-firewall tests;
- focused API and dashboard tests;
- full relevant test suite;
- lint/type/build checks available in the repo;
- rescore idempotence and reconciliation;
- API read-only behavior and source DB immutability;
- test environment isolation checks;
- manual UI/API smoke tests on the parallel instance.

The last recorded baseline was 105 new cohort tests passing and 54 full-suite failures also present on the unmodified base in that test venv (reported as related to missing `pytest-asyncio`). Re-establish this baseline after rebase; do not assume failures are pre-existing if dependencies or code changed. Report new failures separately and do not hide or casually waive failures.

### Step 7 — Rebuild the test-only derived read model
- Use the larger historical snapshot available in the isolated test environment; do not connect to production to obtain it.
- Keep source databases read-only. Record source identity, time range, row counts, coverage and source/derived DB metadata before and after.
- Run `prior_only` and `full_sample` as separate labelled sensitivity analyses. Use `prior_only` as the causal/predictive default; `full_sample` is sensitivity/parity only and must not be presented as no-lookahead evidence.
- Reconcile raw observations, unknown/incomplete rows, reconstructed firings, checkpoints, qualified cohort memberships, unique games, settlements, outcomes, and exclusions.
- Keep the short 31-hour slice results labelled exploratory. Do not reuse legacy cohort statistics or claim profitability/prospective confirmation.

### Step 8 — Deploy only to parallel test instance
Preconditions: steps 1–7 complete; isolation proven; tests and build results documented; no unresolved critical blocker.

- Confirm the deployment target is exactly `127.0.0.1:2263` and collector is disabled for this test instance.
- Record current test-unit configuration and rollback procedure before changing anything.
- Deploy/restart only the dedicated parallel test unit (document its exact name and command). Never restart `blm-server.service`, alter nginx, or replace the existing `:2262`/`:80` mirror for this task.
- Validate health endpoint, `/api/v4/cohorts/spec`, `summary`, `ledger`, `comparisons`, and `quality`; verify source-type filters and denominator labels; inspect dashboard rendering and browser console/API errors.
- Verify source DB size/mtime/hash and Auto-Bet configuration/execution state remain unchanged.
- If validation fails, roll back only the parallel test unit to the last known-good test build, verify it is stopped/restored as appropriate, and report the failure. Do not broaden rollback to other services.

## Acceptance criteria

The agent's final report must include:
1. Test host identity/isolation evidence, redacted.
2. Base SHA, implementation HEAD SHA, branch and working-tree status.
3. Files changed and summary of each code fix.
4. Exact test/build commands, exit statuses, pass/fail counts, and baseline comparison.
5. Prior-only timestamp tests and market-coverage denominator semantics.
6. Source/derived DB paths, before/after metadata, reconciliation and idempotence evidence.
7. Historical time range, cohort denominators, settled outcomes, confidence intervals, market coverage and limitations.
8. Deployment unit/target, exact commands, health/API/UI validation and rollback steps.
9. Explicit confirmation that `:2262`/`:80`, production, production data, and Auto-Bet state/execution were untouched.
10. Remaining blockers and recommended follow-up.

## Stop conditions and authorization status

- Test deployment to the isolated parallel `127.0.0.1:2263` instance is authorized once preflight and validation pass.
- Production deployment/promotion is NOT authorized by this directive.
- No production database access or writes, no production service changes, and no Auto-Bet or real-betting changes are permitted.
- Do not merge this branch if merge could trigger deployment.
- If any requirement conflicts with repository policy or actual environment state, stop and ask for operator direction.
