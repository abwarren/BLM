# Production Safety

LOCAL is production. Protect it by default.

The agent has no implied authority to:
- Deploy code to local production.
- Restart or stop production services.
- Change production configuration or secrets.
- Modify, migrate, truncate or replace production databases.
- Change firewall, DNS, proxy or network exposure.
- Change scheduled jobs or collector operation.
- Disable monitoring, backups or safety controls.

Production operations require explicit operator authorization for the specific action and scope. A general request to develop, fix, test, commit or update documentation is not authorization to deploy or restart production.

Before any authorized production action, state the target, exact change, expected impact, backup/rollback plan and verification steps. If the requested scope is ambiguous, stop and ask.
