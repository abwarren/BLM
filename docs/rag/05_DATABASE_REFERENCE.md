# Database and Data Integrity Reference

Databases are critical assets. Identify the exact host, path, database engine, schema and environment before reading or writing.

## Data categories
Keep these conceptually distinct:
- Raw source observations.
- Parsed/normalized observations.
- Historical market snapshots.
- Event and team metadata.
- Settled results.
- Derived metrics and analytics.
- Model predictions and alerts.
- Betting execution records, if applicable.

## Rules
- Never assume database files with similar names are equivalent.
- Never assume missing values are zero.
- Never fabricate observations or silently backfill unknown values.
- Preserve source identifiers and timestamps.
- Preserve historical records and provenance.
- Maintain event/market relationships and referential integrity.
- Validate row counts and representative records before and after significant operations.
- Use backups and tested rollback procedures for authorized schema changes.
- Never run destructive SQL or experimental migrations against production.

Database writes belong in the authorized test environment unless the operator explicitly authorizes a specific production operation. Never point EC2 tests at production databases.
