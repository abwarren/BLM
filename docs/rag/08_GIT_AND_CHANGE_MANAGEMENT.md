# Git and Change Management

## Required repository delivery
- Inspect working tree, branch, remotes and commit before editing.
- Preserve unrelated user changes.
- Use a dedicated development branch for isolated work.
- Keep commits focused and descriptive.
- Review the diff and run required tests before committing.
- Commit completed, tested changes and push them to the designated GitHub development branch. No additional permission is required.
- Report the branch, commit SHA and GitHub link.
- Do not leave completed work only in an uncommitted local tree.
- Do not force-push, rewrite shared history, overwrite remote work or discard unrelated changes.
- Do not commit secrets, credentials, private datasets, production data or generated database files.
- Record changed files, tests, known issues and outstanding work.

## Automatic EC2 TEST deployment
- EC2 TEST is the automatic deployment target for runnable application-code, configuration and asset changes.
- After tests pass and test-environment isolation is verified, automatically deploy the exact tested commit to EC2 TEST. No additional user permission or confirmation is required.
- Verify the deployment with health checks, relevant API/UI checks and logs.
- Never deploy an uncommitted tree or a different SHA from the one tested.
- Documentation-only changes require commit and push but no runtime deployment.
- A failed test or unresolved regression blocks deployment.
- If the EC2 target, branch, deployment method or isolation cannot be verified, stop and report the blocker; never guess.
- Do not configure automatic deployment on merge unless separately authorized.

## Absolute production release boundary
LOCAL = PRODUCTION. EC2 = TEST.

- Never edit, write to, restart, reconfigure, migrate, backfill or otherwise change LOCAL PRODUCTION without Warren's explicit confirmation for that specific action.
- Never deploy to LOCAL PRODUCTION without explicit confirmation naming the approved commit/release.
- A general implementation request, "commit", "push", "deploy", successful EC2 deployment, passing tests or GitHub merge is not production authorization.
- EC2 deployment must remain isolated and must not change LOCAL PRODUCTION as a side effect.
- If completing a task requires a production-side action, stop and ask before that action.

A GitHub commit is a repository change; it does not mean the EC2 runtime or local production has been updated. Report repository delivery, EC2 TEST deployment and LOCAL PRODUCTION release as three separate states.
