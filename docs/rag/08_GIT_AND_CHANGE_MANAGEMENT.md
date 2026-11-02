# Git and Change Management

## Required repository delivery
- Inspect working tree, branch, remotes and commit before editing.
- Preserve unrelated user changes.
- Use a dedicated development branch for isolated work.
- Keep commits focused and descriptive.
- Review the diff and run the required tests before committing.
- Commit completed, tested changes and push them to the designated GitHub development branch.
- Report the branch, commit SHA and GitHub link.
- Do not leave completed work only in an uncommitted local tree.
- Do not force-push, rewrite shared history, overwrite remote work or discard unrelated changes.
- Do not commit secrets, credentials, private datasets, production data or generated database files.
- Record changed files, tests, known issues and outstanding work.

## Deployment workflow
- Application/code/configuration/assets changes: after tests pass and isolation is verified, deploy the exact committed SHA to EC2 TEST and perform health/API/UI/log verification.
- Documentation-only changes: commit and push; no runtime deployment is required.
- Never deploy an uncommitted tree or a different SHA from the one tested.
- A failed test or unresolved regression blocks deployment.
- Do not configure automatic deployment on merge unless separately authorized.

## Production release
LOCAL = PRODUCTION. EC2 = TEST.

EC2 is the default deployment target. Local production deployment is never automatic. It requires a separate explicit operator authorization naming the approved commit SHA, scope, backup/rollback plan and release window. A commit, push, PR merge, successful EC2 deployment or generic request to "deploy" is not production authorization.

A GitHub commit is a repository change; it does not mean the EC2 runtime or local production has been updated. Report repository, test deployment and production release as three separate states.
