# Testing and Acceptance

Every change requires acceptance criteria proportionate to its risk.

## Test sequence
1. Run focused tests for the changed behaviour.
2. Run relevant integration tests.
3. Run the broader suite when appropriate.
4. Validate API and frontend behaviour when affected.
5. Check database integrity and persistence.
6. Inspect logs and runtime health.
7. Check for regressions and unintended file changes.

## Report exact outcomes
Distinguish passed, failed, skipped and not-run tests. Identify pre-existing failures separately from newly introduced failures. Include command, environment, test count and failure details where useful.

Never claim all tests pass if any failed or were not run. Never weaken or delete tests merely to produce a green result. A test passing in EC2 validates the test environment only; it is not production deployment approval.

## Acceptance
A task is complete only when the agreed behaviour is demonstrated with evidence and known limitations are disclosed.
