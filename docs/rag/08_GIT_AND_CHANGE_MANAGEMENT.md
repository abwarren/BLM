# Git and Change Management

> **ALL ENGINEERING CHANGES MUST BE MADE IN TEST. PRODUCTION MUST NEVER BE MODIFIED.**

## Environment boundary
- LOCAL = PRODUCTION: protected, unchanged, and read-only for this workflow.
- EC2 = TEST: exclusive runtime target for engineering changes, debugging, testing and deployment.
- Use an isolated development workspace to prepare changes; validate and deploy runnable changes only to verified, isolated EC2 TEST.
- Verify host, branch, service, database and environment identity before any write. A label or assumption is not proof of isolation.

## Required repository delivery
- Inspect working tree, branch, remotes and commit before editing.
- Preserve unrelated user changes.
- Use a dedicated development branch for isolated work.
- Keep commits focused and descriptive.
- Review the diff and run required tests before committing.
- Commit completed, tested changes and push them to the designated GitHub development branch. No additional permission is required for the requested repository work.
- Report the branch, commit SHA and GitHub link.
- Do not leave completed work only in an uncommitted local tree.
- Do not force-push, rewrite shared history, overwrite remote work or discard unrelated changes.
- Do not commit secrets, credentials, private datasets, production data or generated database files.
- Record changed files, tests, known issues and outstanding work.

## Automatic EC2 TEST deployment
- EC2 TEST is the only runtime deployment target for this workflow.
- After tests pass and TEST isolation is verified, automatically deploy the exact tested commit to EC2 TEST. No additional user permission or confirmation is required.
- Verify the deployment with health checks, relevant API/UI checks and logs.
- Never deploy an uncommitted tree or a different SHA from the one tested.
- Documentation-only changes require commit and push but no runtime deployment.
- A failed test or unresolved regression blocks deployment.
- If the EC2 target, branch, deployment method or isolation cannot be verified, stop and report the blocker; never guess.
- Do not configure automatic deployment on merge unless separately authorized.

## ABSOLUTE PRODUCTION PROHIBITION
**NO PRODUCTION CHANGES — EVER AS PART OF THIS WORKFLOW.**

Neither agents nor operators acting under these directives may modify LOCAL PRODUCTION. Prohibited actions include file/code/configuration writes, package changes, database writes or migrations, backfills, service restarts, collector changes, network changes, Auto-Bet changes, deployments, promotions and any operation with uncertain side effects.

Production may be inspected only through necessary, non-mutating read-only checks. Do not run tests, reproduce bugs, debug, or apply fixes in production.

A general request to fix, implement, commit, push, deploy, merge, or a passing QA result does not authorize a production action. The required response to a production issue is to reproduce and resolve it in TEST while leaving production untouched.

If an operation could affect production, stop before executing it. If environment identity or isolation cannot be proven, do not proceed.

A GitHub commit is a repository change; it does not mean the EC2 runtime has been updated. Report repository delivery and EC2 TEST deployment as separate states. LOCAL PRODUCTION remains untouched.
