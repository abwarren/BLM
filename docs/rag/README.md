# BLM RAG Knowledge Base

This directory contains the canonical project and agent operating references.

## Read order
1. [Core project identity](00_CORE_PROJECT_IDENTITY.md)
2. [Environment boundaries](01_ENVIRONMENT_BOUNDARIES.md)
3. [Agent operating directive](02_AGENT_OPERATING_DIRECTIVE.md)
4. [Project architecture](03_PROJECT_ARCHITECTURE.md)
5. [Data collection](04_DATA_COLLECTION.md)
6. [Database integrity](05_DATABASE_REFERENCE.md)
7. [Analytics and models](06_ANALYTICS_AND_MODELS.md)
8. [Testing and acceptance](07_TESTING_AND_ACCEPTANCE.md)
9. [Git and change management](08_GIT_AND_CHANGE_MANAGEMENT.md)
10. [Production safety](09_PRODUCTION_SAFETY.md)
11. [Auto-Bet safety](10_AUTOBET_SAFETY.md)
12. [RAG retrieval rules](11_RAG_RETRIEVAL_RULES.md)

## Active research task
- [Existing-strategy OVER cohort database research](../OVER_COHORT_DATABASE_RESEARCH.md) — read-only TEST analysis; no strategy retuning or production changes.

## Authority
These documents define operating policy. They do not replace inspection of current code, configuration, schemas, runtime state or operator instructions. If a technical detail is unknown or may have changed, verify it in the repository and running environment.

**Environment invariant: LOCAL = PRODUCTION; EC2 = TEST/DEVELOPMENT.**
