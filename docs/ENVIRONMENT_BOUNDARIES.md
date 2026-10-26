# Environment Boundaries and Promotion Policy

**Status: mandatory deployment boundary. Read before any BLM refactor, test, migration, or release.**

## Canonical environments

### Production

- **Canonical public URL:** https://blm.haaats.xyz/
- **Identity:** the existing production box that currently serves the public BLM dashboard at the URL above.
- **Purpose:** live production service and its production data.
- **Rule:** production is not a development or acceptance-test target. Do not use it to implement, experiment with, replay, rescore, migrate, or validate the Trade Cohort refactor.

Treat the host serving blm.haaats.xyz as production even if an IP address, SSH alias, hostname, or service name differs. Never infer that a machine is a test host merely because it is accessed by IP.

### Test / staging

- **User-designated EC2 test entry point:** http://13.247.58.81/login?next=/
- **Purpose:** implement and validate the Trade Cohort refactor before any production promotion.
- **Status:** designated by the operator as the test environment; its actual application version, database, services, credentials, and isolation have **not yet been independently verified**.
- **Rule:** all refactor implementation, historical rescore experiments, UI/API testing, test migrations, acceptance checks, and performance testing must happen here first, after verifying this EC2 host is isolated from production and uses test data/storage.

The EC2 URL uses plain HTTP. Do not transmit production credentials or secrets through it. Verify transport security, access controls, and database/service isolation before login or running commands. Do not store credentials, tokens, private keys, or session cookies in this repository.

## Non-negotiable test-first workflow

1. **Identify and verify the target.** Confirm the EC2 instance identity and environment markers. Confirm its DB paths, environment variables, services, reverse proxies, and write targets. Prove that its collector/API cannot write to production databases or control production services.
2. **Create an isolated work branch.** Keep changes reviewable and do not commit secrets or production database contents.
3. **Implement on EC2 test only.** Make all code, UI, API, rescore, schema, and migration changes in the test environment. Use a test database or a controlled copy/snapshot with sensitive data handled appropriately. Do not point test jobs at production DBs.
4. **Run verification on test.** Execute unit/integration tests, dashboard syntax checks, API contract checks, historical reconciliation, cohort boundary tests, data-quality checks, and manual UI acceptance tests. Record exact commit SHA, test results, DB identity, and environment identity.
5. **Review before promotion.** Produce a diff and evidence. Confirm cohort totals reconcile and safety invariants hold. Obtain explicit operator approval for a separate production release.
6. **Promote deliberately.** Production deployment is a separate, later action with its own reviewed commit, backup/rollback plan, maintenance decision, and explicit authorization. Do not auto-deploy on merge or as a side effect of testing.
7. **Verify production only after approval.** If approved later, perform the minimum release verification against production. Never use production to test unfinished behavior.

## Production safety prohibitions

Until a separately approved release:

- No production code edits, deployments, restarts, migrations, backfills, or rescoring writes.
- No production collector or API changes.
- No Auto-Bet changes: do not enable it, change its selected cohort, alter stakes/limits, modify kill switches, authorize browser submission, or submit bets.
- No copying test configuration over production configuration.
- No test process may point at production databases, queues, browser sessions, credentials, or service endpoints.
- Do not treat a successful test on EC2 as authorization to release to production.

## Trade Cohort refactor scope

The TC-X, TC-E, TC-S, TC-C, TC-W, TC-D, and TC-C+ refactor is **test-first on EC2**. Historical rescore, cohort analytics, UI changes, API changes, schema changes, and any data migration must be exercised and reconciled in test before production is considered.

The currently selected Auto-Bet cohort is reported as TC-X by the operator. Displaying or calculating cohort statistics must not mutate that setting or enable execution.

Historical eligibility must be computed from firing-time information only; final settlement is joined only for scoring. Keep the repository's frozen research and prospective-confirmation boundaries intact.

## Required deployment evidence

Before requesting production approval, provide:

- Test EC2 instance identity and confirmation of production isolation.
- Git branch and exact commit SHA.
- Files and schema changes.
- Test commands and pass/fail totals.
- Historical reconciliation: actual firings, distinct games, per-cohort membership counts, scored/unscored outcomes, missing market rates, exclusions, and duplicate-handling policy.
- UI/API acceptance results and screenshots or logs with secrets removed.
- Confirmation that Auto-Bet settings and production services were not changed.
- Backup and rollback plan for the proposed production release.

## Ownership and ambiguity rule

The operator's environment declaration is authoritative:

- https://blm.haaats.xyz/ = **PRODUCTION**.
- http://13.247.58.81/login?next=/ = **DESIGNATED EC2 TEST ENTRY POINT**, pending isolation verification.

If a tool, agent, shell session, environment variable, hostname, database path, or deployment script makes the target ambiguous, **stop and verify**. Do not guess and do not proceed against production.
