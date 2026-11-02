# Data Collection — Primary Engineering Priority

The first major objective is a collector that can repeatedly obtain a clean, correctly associated PokerBet dataset. A process that stays running but captures incomplete, stale or misidentified observations is not a successful collector.

## Required observation fields
Verify the actual source schema, then capture available fields such as:
- Stable event/game identity.
- League and participating teams.
- Market identity and market type.
- Total/line and associated prices/odds.
- Source observation timestamp and collection timestamp.
- Event status and source identifiers.
- Provenance and parsing/validation status.

Do not invent missing fields or substitute zero for NULL.

## Quality requirements
- Correct event and market association.
- Accurate line and odds extraction.
- Reliable timestamps and timezone handling.
- Explicit missing/invalid data detection.
- Duplicate detection and idempotent persistence.
- Raw-source preservation where feasible.
- Recovery from transient source/network/browser failures.
- Clear logging, metrics and collection health state.

## Acceptance evidence
Demonstrate repeated collection runs; compare source values with persisted rows; report coverage, missing fields, duplicates, parse failures, event identity errors and persistence counts. Include representative raw-to-normalized examples and exact time windows.

Do not declare the collector reliable based solely on process uptime, a single successful scrape or synthetic fixtures. Synthetic tests are useful but do not replace source-backed verification.
