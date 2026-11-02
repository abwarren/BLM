# Git and Change Management

- Inspect working tree, branch, remotes and commit before editing.
- Preserve unrelated user changes.
- Use a dedicated development branch for isolated work.
- Keep commits focused and descriptive.
- Do not force-push or rewrite shared history without explicit authorization.
- Do not commit secrets, credentials, private datasets or generated database files.
- Review the diff before committing.
- Record commit SHA, changed files, tests and known issues.
- Do not merge or deploy automatically.

A GitHub commit is a repository change; it does not mean the EC2 runtime or local production has been updated. Keep source control, test deployment and production deployment as separate stages.
