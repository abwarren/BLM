# Environment Boundaries — Critical

## LOCAL = PRODUCTION
The local machine and its BLM installation are the authoritative production environment. It may contain live services, production configuration and authoritative historical databases.

Do not experiment, refactor, run speculative migrations, overwrite data, replace application files, change production configuration or restart production services without explicit operator authorization.

## EC2 = TEST / DEVELOPMENT
The EC2 environment is for development, investigation, implementation, refactoring and acceptance testing. All experimental changes must be made and validated here first.

EC2 is not production. A passing EC2 test does not authorize deployment to local.

## Mandatory preflight
Before any write or operational action, verify:
1. Host identity and environment.
2. Repository path and remote.
3. Git branch and current commit.
4. Relevant service/process identity.
5. Database paths and whether they are production data.
6. Whether any storage, database, credentials or services are shared across hosts.

Do not infer environment from a hostname, directory name, shell prompt or stale documentation alone. If identity is ambiguous, stop before modifying anything.

## Hard boundary
Never copy test data over production data. Never point EC2 experiments at production databases. Never deploy, merge, restart or migrate production as an assumed next step.
