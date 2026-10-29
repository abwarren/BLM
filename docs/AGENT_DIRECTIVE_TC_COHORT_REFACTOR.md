# Agent Directive: Trade Cohort Refactor (EC2 Test First)

**MANDATORY: read docs/ENVIRONMENT_BOUNDARIES.md before doing any work.**

**Step-by-step execution plan:** [docs/AGENT_WORKFLOW_TC_COHORT_STATS_AND_UI.md](AGENT_WORKFLOW_TC_COHORT_STATS_AND_UI.md). This workflow is mandatory for the cohort-statistics rebuild and dashboard cleanup; it defines phase gates, data lineage, denominator rules, UI filter/tab/spacing/scroll requirements, tests, and test-only deployment evidence.

**MANDATORY commit/deploy cadence:** work in the verified EC2 test checkout only. Commit each coherent work unit after relevant tests pass, then deploy that exact validated commit to the isolated parallel test instance at `127.0.0.1:2263` and run smoke checks. Re-verify target identity before every deploy. Never deploy to production; do not replace the existing `:2262`/`:80` mirror. If a build is broken or unvalidated, fix/test before deploying it. Follow the detailed cadence in the workflow.

## Target environment order

1. Implement and test this refactor on the designated EC2 test environment: http://13.247.58.81/login?next=/
2. Verify EC2 identity and prove database, service, credential, and write-target isolation before running mutations. The supplied URL is plain HTTP; do not send production secrets through it.
3. The production environment is the box serving https://blm.haaats.xyz/. Do not use production for development, historical rescoring, UI acceptance tests, migrations, backfills, or experiments.
4. Do not promote or deploy to production without a separate explicit operator approval after review of test evidence.

If isolation cannot be proved, stop and ask for clarification. Do not assume the EC2 host is isolated just because it has a different IP.

## Work scope

Refactor the primary alert analytics and dashboard from legacy fingerprints to named trade cohorts:

- TC-X / ELITE: pace ratio > 1.16, shortfall >= -0.50, UNDER price >= 1.60
- TC-E / EDGE: pace ratio > 1.12, shortfall >= -0.50, UNDER price >= 1.60
- TC-S / SHARP: pace ratio > 1.08, shortfall >= -0.50, UNDER price >= 1.60
- TC-C / CORE: pace ratio > 1.04, shortfall >= -0.50, UNDER price >= 1.60
- TC-W / WIDE: pace ratio > 1.00, shortfall >= -0.50, UNDER price >= 1.60
- TC-D / DEEP: pace ratio > 1.00, shortfall >= 0.50, UNDER price >= 1.60
- TC-C+ / CORE+: pace ratio > 1.04, shortfall >= 0.25, UNDER price >= 1.80

Common gates: progress 75–92% inclusive; scored points >= 70; exclude the Cyber 2K26 simulated feed; line-movement leg OFF. Pace ratio = required points/min divided by league-average points/min. Shortfall = required points/min minus actual points/min. Verify source field units and semantics before coding. Missing operands must produce UNKNOWN/unscorable, never TRUE.

Calculate all cohort memberships at the recorded firing time using only data available then. Preserve the triggering line and price. Join settlement only for retrospective scoring. Keep actual firing history distinct from retrospectively qualifying checkpoints. Headline statistics must use a documented one-game-one-observation rule, with every firing retained for audit/drill-down. Define a transparent tightest-cohort ranking for TC-D and TC-C+; they are not simply rungs in the standard nested pace ladder.

## UI and API requirements

- Add a Trade Cohorts overview with sample size, UNDER/OVER/PUSH, UNDER rate, Wilson 95% interval, market coverage and exclusions.
- Add a fired-alert ledger showing firing-time inputs, tightest cohort, all qualifying cohorts, triggering line/price and final outcome.
- Add cohort comparisons by actual competition, price, pace ratio, shortfall and progress.
- Move C1/C2/C3/C5/R2 fingerprint output to collapsed Legacy Diagnostics; preserve legacy data.
- Show Auto-Bet configuration read-only, including persisted selected cohort (currently reported as TC-X), enabled/dry-run/submission authorization state and missing risk limits. Do not mutate any of these values.
- Keep business logic in the backend; the frontend renders API output only.
- Preserve the repository's frozen research and prospective-confirmation firewall.

## Hard prohibitions

No production changes, deployment, service restart, migration, backfill, rescore writes, Auto-Bet changes, stake changes, kill-switch changes, browser submission, or live bets. Do not point test jobs at production DBs or services. Do not put secrets or production data into Git.

## Required completion evidence

Report the branch and commit SHA, test environment identity, proof of isolation, files changed, tests and results, UI/API acceptance, per-cohort reconciliation totals, distinct-game counts, unsettled/invalid results, missing-market coverage, exclusions, and rollback considerations. Submit a reviewable diff/PR. Stop after test validation and request explicit production-release approval; do not deploy automatically.
