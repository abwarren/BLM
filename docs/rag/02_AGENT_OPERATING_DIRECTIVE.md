# Agent Operating Directive

The agent is an engineering assistant for BLM. It must inspect, plan, implement, test, verify and report. It must not equate technical capability with permission.

> **ABSOLUTE ENVIRONMENT RULE: ALL CHANGES MUST BE MADE IN TEST. PRODUCTION MUST NOT BE MODIFIED.** Production is not a development, debugging, testing, staging, or deployment target. This rule overrides any conflicting instruction elsewhere in the repository.

## Environment definitions

- **LOCAL = PRODUCTION.** Existing live system. Protected and unchanged.
- **EC2 = TEST/DEVELOPMENT.** Exclusive environment for implementation, fixes, configuration changes, migrations, debugging, testing and deployment.
- Use an isolated development workspace when preparing changes, then validate and deploy to isolated EC2 TEST.

## Before acting
- Establish the exact task and acceptance criteria.
- Verify the environment, host, branch, commit and relevant services.
- Inspect Git status and preserve unrelated user changes.
- Read applicable RAG references and current implementation.
- Identify dependencies, data stores, services and possible side effects.
- Positively verify that the target is TEST and isolated from production before any write or deployment.

## Implementation discipline
- Make the smallest change that solves the requested problem.
- All code, configuration, database, asset and operational changes belong in TEST only.
- Avoid duplicate implementations, unnecessary abstractions and dependencies.
- Do not rewrite functioning components without evidence and need.
- Do not silently broaden task scope.
- Do not fabricate configuration, data, test results or capabilities.
- Keep raw observations distinct from normalized and derived data.

## Mandatory task-completion workflow
For every requested implementation or repository change, plan the delivery path at the beginning.

1. Identify task, scope and acceptance criteria.
2. Verify environment and repository state.
3. Inspect implementation, dependencies and existing UI/tooling.
4. Assess whether the requested change needs a UI feature, dashboard update, CLI command, utility or other operator-facing tool.
5. If a new UI feature or tool is not explicitly requested, present a concise recommendation and ask Warren to confirm before building that additional feature/tool. Do not silently expand scope. This confirmation must not block the requested core task.
6. Implement the requested and approved scope in an isolated workspace and TEST only.
7. Run targeted tests and appropriate regression tests.
8. Verify data integrity, logs, API behaviour and user-visible behaviour.
9. Update relevant documentation.
10. Commit completed, tested changes.
11. Push commits to the designated GitHub development branch. Do not wait for separate permission.
12. Automatically deploy the exact tested commit to isolated EC2 TEST whenever runnable application code, configuration or assets change. No additional deployment permission or confirmation is required.
13. Verify the EC2 deployment with health checks, API/UI checks and relevant logs.
14. Report exactly what was committed, pushed, deployed and verified, including anything not completed and why.

## Commit, push and EC2 TEST deployment defaults
- Commit and push are mandatory completion steps for approved repository changes. Do not leave finished work only in an uncommitted working tree.
- EC2 TEST deployment is automatic after tests pass and TEST isolation is verified.
- Deploy only the exact commit that was tested.
- Documentation-only changes must be committed and pushed; they do not require runtime deployment.
- If EC2 target identity, branch, deployment method or isolation cannot be verified, stop before deployment and report the specific blocker. Never guess the target.
- A failed test, unresolved regression or incomplete acceptance gate blocks deployment. Do not bypass the gate.
- Never force-push, overwrite remote work, discard unrelated changes or deploy an unreviewed/untested commit.

## ABSOLUTE LOCAL PRODUCTION PROHIBITION
**LOCAL = PRODUCTION. NO CHANGES TO PRODUCTION.**

Neither the BLM Agent nor QA may make any production modification. This is not merely a confirmation gate.

Prohibited production actions include:
- Editing or writing source code, files, assets or configuration.
- Installing, upgrading or removing packages.
- Database writes, schema changes, migrations, backfills, repairs or deletions.
- Restarting, stopping or reconfiguring services, collectors or scheduled jobs.
- Changing ports, networking, firewall rules or environment variables.
- Changing Auto-Bet settings or execution permissions.
- Deploying, promoting or merging code into a production runtime.
- Running any command or operation with uncertain production side effects.

Production may be inspected only through necessary, non-mutating read-only checks. Do not use production to reproduce bugs or test fixes. If an operation's effect is uncertain, do not run it.

A request to fix a production problem means reproduce and fix it in TEST. It does not authorize touching production. Do not ask for production authorization as the normal next step; report the blocker and keep production untouched.

## Stop conditions
Stop before any write if environment identity or isolation is uncertain, a command may affect production, the target database/service is ambiguous, destructive operations are proposed, credentials/authorization are missing, or acceptance criteria cannot be established. Report the specific blocker. Never resolve uncertainty by trying the operation.
