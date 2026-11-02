# Project Architecture Reference

This is a verification guide, not an assertion that every component or path currently exists. The agent must inspect the live repository before relying on component names, ports, service names or deployment topology.

## Architectural areas to map
- PokerBet source access and collection.
- Browser/session automation and collector lifecycle.
- Parsing, normalization and event/market identity.
- Persistence and historical databases.
- Settlement and actual-result ingestion.
- Analytics, prediction and alert generation.
- Backend APIs and service processes.
- Dashboard/frontend presentation.
- Operational monitoring, logging and deployment.

## Required architecture record
For each confirmed component, document:
- Responsibility and owner module.
- Entry point and dependencies.
- Inputs and outputs.
- Database/table ownership.
- Runtime process/service and verified host.
- Configuration source (never publish secrets).
- Health checks and failure modes.
- Tests and operational dependencies.

## Architecture safety
Do not assume similarly named databases are interchangeable. Do not assume a dashboard value is authoritative when it is derived from backend state. Do not change ports, service bindings, process ownership or data flows without tracing their consumers and validating the result in EC2.
