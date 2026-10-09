# BLM RAG Knowledge Base

> ## ⚠️ AUTO-BET SAFETY (read before any betting work)
> **R2.00 (ZAR 2.00) is the ONLY authorized real-money TEST stake — it is NOT the
> production stake.** The production stake is the user-configured **BLM Unit
> Size**. Three modes: `ZERO_STAKE` (R0.00) · `REAL_MONEY_TEST` (exactly R2.00) ·
> `PRODUCTION_AUTO_BET` (exactly the configured unit size). No valid unit size →
> FAIL CLOSED → NO BET. The R2.00 real-money test is a separate, explicitly-
> authorized gate. Authoritative: `docs/autobet/AUTOBET_SAFETY_CONTRACT.md`.

Operational knowledge packs for future BLM agents. This is **not** a copy of the
repository. It is a retrieval-oriented operating system: for each rule it records
the exact rule, its source of truth in the live code, why it exists, a valid
example, an invalid example, an edge case, forbidden behaviour and a verification
method.

- `Retrieval keywords:` — every major document carries a keyword line so a
  BM25/FTS retriever (see `rag/build_context.py`) can surface it.
- Rule blocks use a fixed shape: **Rule · Source · Why · Valid · Invalid · Edge ·
  Forbidden · Verify**.
- The RAG **finds** the truth; it never defines it. The repository code, tests
  and live production state remain the authority (`DECISIONS.md` §AUTHORITY).

## Authority order (highest → lowest)

```
current source code + tests   ← source of truth
        ↓
production state (DB / services)
        ↓
DECISIONS.md                  (durable decisions)
        ↓
docs/rag/**                   (this knowledge base — retrieval layer)
        ↓
knowledge store / RAG index   (agent convenience)
```

Lower levels never override higher levels. On conflict: report it, trust the
higher level, then correct the lower one.

## Packs

| File | Purpose |
|---|---|
| `00_AGENT_RULES.md` | Behaviour rules for a BLM agent + the mandatory handoff format |
| `01_AUTHORITATIVE_SOURCE_CONTRACT.md` | What data is trusted, for what |
| `02_ALERT_LIFECYCLE.md` | Trigger → freeze → settlement, with valid/invalid transitions |
| `03_SETTLEMENT_RECONCILIATION.md` | Fixing PENDING / unresulted games |
| `04_DATABASE_SCHEMA.md` | Semantic meaning of every important field |
| `05_PRODUCTION_RULES.yaml` | Machine-readable rules registry (thresholds, gates) |
| `06_KNOWN_FAILURE_MODES.md` | Past defects and their signatures |
| `07_COLLECTOR_ARCHITECTURE.md` | How live data enters BLM |
| `08_MARKET_LINE_SEMANTICS.md` | Opening / live / closing / fair / trigger lines |
| `09_BLM_MODEL.md` | Pace, projection, OLV-vs-BLM, fingerprint context |
| `10_TRAP_METER.md` | Trap/line-vs-score signals + retired logic (never resurrect) |
| `11_TESTING_CONTRACT.md` | What counts as proof |
| `12_PRODUCTION_RUNBOOK.md` | Inspect / change / deploy / rollback |
| `13_AUTOBET_RUNG.md` | The PokerBet RUNG rule (line-increment guard for Auto-Bet) |
| `14_TRADED_COHORT.md` | What "the traded cohort" means — the games the system auto-bets, vs the alert population and vs "bettable" |
| `DECISIONS/` | Architectural Decision Records (permanent) |
| `EXAMPLES/` | Real positive / negative / edge-case cases |

## Auto-Bet companion docs (outside `docs/rag/`)

| File | Purpose |
|---|---|
| `docs/autobet/AUTOBET_SAFETY_CONTRACT.md` | R2.00 test-stake invariant, Unit Size, the three execution modes |
| `docs/autobet/RUNG_PRODUCTION_WIRING_GATE.md` | PW-01..16 production wiring gate status + evidence |
| `docs/autobet/RUNG_INTEGRATION_REPORT.md` | Rung integration report (call path, files, tests, evidence) |

## Verified against

- Repo: `/home/ubuntu/BLM`, branch `handoff-2026-09-07`, HEAD `d864988`.
- Source read for this build: `blm_v4/under_alert.py`,
  `blm_v4/live_analytics/under_outcome.py`, `blm_v4/result_policy.py`,
  `blm_v4/result_reconciler.py`, `blm_v4/settle_worker.py`, `blm_v4/scorecard.py`,
  `blm_v4/projection.py`, `blm_v4/terminal_eligibility.py`,
  `blm_v4/live_analytics/under_fingerprints.py`,
  `blm_v4/live_analytics/fingerprint_c5.py`, `blm_v4/betting/executor.py`,
  `blm_v4/betting/config.py`, `blm_v4/collector.py`, `blm_v4/api.py`,
  `blm_v4/pace_projector.py`, live SQLite schemas, live systemd `--user` units.

## Contradictions found while building this KB

See `CONTRADICTIONS.md` in this directory. Every contradiction between these
packs and the current production code is recorded there explicitly — none was
silently resolved.
