# BLM — Decisions & Memory

Persistent operational memory for the BLM repository. This file is the
single source of truth for what has been decided, what tooling exists, and
what must be remembered across sessions.

Last updated: 2026-09-20 19:14 UTC
HEAD at last update: `76dba4a` — "Render exception tracebacks so a crash can name its own cause"

---

## AUTHORITY (highest → lowest)

```
┌───────────────────────┐
│ Current source        │
│ code + tests          │  ← source of truth
└─────────┬─────────────┘
          │
┌─────────▼─────────┐
│ Production state  │
│ DB / services     │
└─────────┬─────────┘
          │
┌─────────▼─────────┐
│ DECISIONS.md      │
│ durable decisions │
└─────────┬─────────┘
          │
┌─────────▼─────────┐
│ knowledge.db      │
└─────────┬─────────┘
          │
┌─────────▼─────────┐
│ rag/blm_context.db │
│ RAG index         │
│ retrieval layer   │
└─────────┬─────────┘
          │
┌─────────▼─────────┐
│ ~/.clai KB        │
│ agent convenience │
└───────────────────┘
```

**Rule:** lower levels never override higher levels. On conflict, report it,
trust the higher level, and correct the lower level.

**Role of the RAG:** The RAG should not become a second source of truth. It
should help the agent **find** the truth — by retrieving the authoritative
source code, tests, production state, and recorded decisions — not define
the truth itself. Verification always goes back to the source levels above.

---

## 0. How BLM works (read this first)

A single-page mental model so no one has to re-explain the project.

**Repository code is authoritative over this memory.** This section is a
convenience summary, not a substitute for the source. If anything here
conflicts with the code, the code wins — verify before relying on it.

**What BLM is:** a quantitative sports-analytics decision-support system for
live basketball betting markets. It does **not** place bets; it measures
whether a sportsbook's live market has diverged from historically expected
behaviour and identifies favourable **UNDER** entry timing.

**Target:** BetConstruct Cyber Basketball 2K26 (and Betual NBA) on
PokerBet.co.za.

**Pipeline (V4 production):**

```
PokerBet live panel
  → V4 collector (20s cadence, systemd blm-collector)
      → snapshots (append-only, source_game_id identity)
      → SQLite blm_pokerbet.db
          → projection.project(snapshots) → pace, expected_total, margin
          → scorecard checkpoints (10..90 + terminal 100)
          → Under Alert: progress>=75 AND required > league_avg*1.04
      → FastAPI server (port 2262, systemd blm-server)
          → dashboard (live, alerts, settlement)
```

**Key layers:**

- **V1** — legacy Playwright scraper + SQLite + Flask research console.
- **V2** — platform layer: BLM engine, event bus, InfluxDB/SQLite TS, FastAPI.
- **V3** — parallel historical research pipeline (250ms collector, 7-table DB).
- **V4** — current production: PokerBet live pipeline + reconciliation.

**Two populations, never mixed:** `CYBER_2K26` (4×12 min) and `BETUAL_NBA`
(4×10 min). Every game carries its own `classification`; statistical
processing is scoped per classification.

**Core math (single source of truth: `blm_v4/projection.py`):**

```
pace           = points / elapsed * full_game_minutes
expected_total = 0.7*pace + 0.3*live_total_line   (x.0/x.5 grid)
expected_margin= (home-away)/elapsed * full
home/away      = (total ± margin)/2, floored at live score
```

**The only production alert rule (`under_alert.py`):**

```
active = eligible AND progress >= 75%
         AND required_pts_per_min > league_average_pace * 1.04  (strict)
```

**Primary databases:** `blm_pokerbet.db` (live, 12 tables),
`blm_metrics_clean.db` (clean metrics, 7), `blm_historical.db` (V3, 7),
plus V1/V2/TS smaller DBs.

**Non-negotiables:** historical data immutable; derived values reproducible;
classification isolation; market line by price not position; point-in-time
checkpoints; no terminal-score leakage; fail closed on missing inputs.

**Memory protocol:** search both stores before changes (§6a), verify against
the repo (§6b), propose durable facts (§6c), update on change (§6d), never
store secrets (§6e), follow startup protocol (§6f).

---

## 1. Persistent context / RAG index

**Decision:** BLM has a dependency-free, persistent retrieval index backed by
SQLite FTS5. No vector DB or embeddings are used.

**Location:** `rag/blm_context.db` (generated; do not edit by hand)

**Tooling:**

```bash
# Rebuild from the current repo state
python3 rag/build_context.py

# Query (BM25-ranked, supports filters + JSON)
python3 rag/query_context.py "under alert trigger condition"
python3 rag/query_context.py --kind py "freeze detector"
python3 rag/query_context.py --path scorecard "settlement"
python3 rag/query_context.py --schemas "checkpoint"
python3 rag/query_context.py --commits "temporal freshness"
python3 rag/query_context.py --json "query"

# Retrieve AND persist every hit to the knowledge store
python3 rag/query_context.py "query" --store
```

**Index contents (as built):**

- 356 text files → 8,599 chunks
- 36 SQLite table schemas across 7 databases
- 106 git commits
- Production logs outside the repo (indexed as `external/*`)

**Index design rules:**

- `blm_context.db` uses FTS5 `porter unicode61` tokenizer and BM25 ranking.
- Python chunks carry `symbol` (function/class), `line_start`, `line_end`,
  and `content_hash` (SHA-256 of full chunk content).
- Rebuild is idempotent: `files`/`chunks`/`chunks_fts` are dropped and rebuilt.
- Always rebuild after material repo changes.

---

## 2. Persistent local knowledge store

**Decision:** A separate, session-surviving knowledge store holds retrieved
chunks and general facts, independent of the BLM RAG index.

**Location:** `/home/ubuntu/.clai/knowledge.db` (SQLite + FTS5)
**CLI:** `/home/ubuntu/.clai/kb.py`

```bash
python3 ~/.clai/kb.py add "Title" "Content" --tags blm --source cliagent
python3 ~/.clai/kb.py search "BLM" --json
python3 ~/.clai/kb.py list --tag blm
python3 ~/.clai/kb.py get 1
python3 ~/.clai/kb.py update 1 --content "new"
python3 ~/.clai/kb.py delete 1
python3 ~/.clai/kb.py export > backup.jsonl
```

**Stored chunk schema (every `--store` hit):**

| Field | Meaning |
|---|---|
| `title` | `rel_path:start-end :: symbol title` |
| `content` | highlighted snippet |
| `tags` | `blm,rag,<kind>` |
| `source` | repo-relative path |
| `file_path` | absolute filesystem path |
| `line_start` / `line_end` | 1-based line range |
| `symbol` | function/class name (code only) |
| `content_hash` | SHA-256 of full chunk content |
| `commit_hash` / `commit_author` / `commit_date` / `commit_subject` | git provenance |

**Dedup:** key = `<rel_path>#<chunk_id>`, upserted — re-storing never duplicates.

**Durability:** SQLite WAL, committed on every write; survives CLI close and
machine restart.

---

## 3. Exclusion & redaction rules (non-negotiable)

The RAG indexer **must never index**:

- Directories: `.git`, `venv`, `.venv`, `env`, `.env`, `virtualenv`,
  `.virtualenvs`, `__pycache__`, `.pytest_cache`, `node_modules`,
  `.mypy_cache`, `.ruff_cache`, `.tox`, `.coverage`, `.cache`,
  `.ipynb_checkpoints`, `.pyre`, `dist`, `build`, `target`, `backups`, `rag`
- Files matching: `.env*`, `.aws-*`, SSH private keys, `.npmrc`, `.pypirc`,
  `credentials`, `.netrc`
- Binary/compiled extensions: `.pyc`, `.pyo`, `.so`, `.o`, `.a`, `.class`,
  `.jar`, `.war`, `.whl`, `.parquet`, `.arrow`, `.feather`, `.pkl`,
  `.pickle`, `.pt`, `.onnx`, `.bin`, `.iso`, `.zip`, `.tar`, `.gz`, `.bz2`,
  `.xz`, `.7z`, `.exe`, `.dll`, `.dylib`
- SQLite DB binaries and their WAL/SHM sidecars (schemas are indexed instead)
- Files over 8 MB

The indexer **redacts** secret-looking values before storage:
AWS keys, Google API keys, GitHub PATs, Slack tokens, Stripe live keys,
Bearer tokens, private-key blocks, and `password/secret/api-key/token=value`
patterns. Redaction marker: `[REDACTED]`.

---

## 4. What is intentionally kept (historical value)

Despite being "generated," the following remain indexed because they are
valuable context:

- Root analysis reports (`analysis_*.txt`)
- Production logs (`external/blm_report.out`, `external/blm_under_backtest.out`,
  `external/blm_under_backtest.json`, `external/oos_run.log`)
- SVG point/quarter timelines
- JSONL histories (`prospective_health_history.jsonl`,
  `shadow_momentum_decisions_*.jsonl`, `shadow_p80_3d_triggers.jsonl`)
- Signal CSVs

---

## 5. Repo facts

- **Root:** `/home/ubuntu/BLM`
- **Purpose:** quantitative sports analytics platform for live basketball
  betting market analysis; target is BetConstruct Cyber Basketball 2K26 on
  PokerBet.co.za.
- **Layers:** V1 legacy research console, V2 platform, V3 historical engine,
  V4 PokerBet live pipeline (current production).
- **DBs:** `blm_pokerbet.db` (12 tables), `blm_historical.db` (7),
  `blm_metrics_clean.db` (7), `blm_metrics_clean.db.live_analytics.db` (3),
  `blm.db` (3), `blm_ts.db` (2), `blm_v2.db` (2).
- **Current branch:** `handoff-2026-09-07`.

---

## 6. Retrieval-to-store workflow

Whenever BLM context is retrieved for use:

```bash
cd /home/ubuntu/BLM
python3 rag/query_context.py "<query>" --store
```

This persists every hit with full provenance (path, lines, symbol, content
hash, git commit) into the knowledge store, so future sessions can recall it
without re-indexing.

### 6a. Before making a change, search BOTH

Before any code, config, schema, or workflow change, retrieve context from
**both** stores to avoid duplicating past decisions or reintroducing
rejected approaches:

```bash
# 1. BLM repository index (code, docs, schemas, commits, logs)
python3 /home/ubuntu/BLM/rag/query_context.py "<query>" --json

# 2. Persistent knowledge store (retrieved chunks + durable facts)
python3 ~/.clai/kb.py search "<query>" --json

# 3. Durable decisions/memory file (authoritative prose)
python3 /home/ubuntu/BLM/rag/query_context.py --path DECISIONS.md "<query>"
```

Rules:

- Check **both** stores — they are complementary: the RAG index holds the
  full repo; the knowledge store holds previously retrieved/stored chunks.
- If relevant chunks are found, store them first (`--store`) so the
  knowledge store stays current.
- Consult `DECISIONS.md` for already-made architecture, alert, checkpoint,
  formula, bug, rejected-approach, test, and discovery decisions.
- Do not proceed if the change contradicts a recorded **rejected/superseded
  approach** (§14) or an **explicit prohibition** (§12.H) without explicit
  re-authorization.

### 6b. Memory is a guide, not ground truth

- **Do not assume something is correct merely because it appears in
  memory** (`DECISIONS.md` or the knowledge store).
- Memory records prior decisions and discoveries, but it can be stale,
  incomplete, or wrong. The authoritative source is the **current
  repository state**: code, tests, and live data.
- Before relying on a memory item, **verify it against the source** — read
  the referenced file, run the relevant test, or query the live DB.
- If memory contradicts the repository, **trust the repository** and update
  memory to match.
- **If memory conflicts with current code, report the conflict and treat
  current verified code as authoritative.** Do not silently pick memory, and
  do not silently patch memory without saying a conflict was found — surface
  it, then update memory to the verified code.
- **Update memory only when appropriate.** A conflict does not automatically
  require an edit: confirm the change is durable and material (a real
  architecture/behavior difference, a correction, or a new fact), not a
  transient state or a trivial wording difference. If no memory update is
  warranted, report the conflict and stop — do not churn memory.

### 6c. Propose durable facts for memory

- **When you discover a durable architectural fact or decision, propose
  adding it to the persistent memory** — do not silently keep it in the
  current conversation.
- A fact is worth recording if it will still matter in future sessions:
  a design invariant, a non-obvious constraint, a formula, a rejected
  approach, a root cause, a deploy/runtime requirement, or a decision with
  long-term consequences.
- Propose the exact section and wording, then append it to `DECISIONS.md`
  (and, if it was retrieved from the repo, store the source chunk via
  `--store`).
- Keep memory accurate: if verification shows a recorded fact is wrong,
  correct it in the same step.

### 6d. Update memory when code changes

- **When code changes, update the relevant memory/documentation if the
  architecture or behavior changed.**
- After a code change, review whether any recorded fact in `DECISIONS.md`
  (formulas, constants, schemas, constraints, workflows, rejected
  approaches) is now stale or contradicted by the new code.
- If architecture or behavior changed, update the corresponding section in
  the same change — do not leave memory describing the old behavior.
- If only an implementation detail changed with no architectural/behavioral
  effect, leave the durable memory unchanged.
- Prefer updating memory alongside the code change, and note the revision in
  the commit or memory header so future sessions can see what changed.

### 6e. NEVER store secrets in memory

- **NEVER store secrets in memory** — not in `DECISIONS.md`, the knowledge
  store, or any indexed file.
- Secrets include: passwords, API keys, access tokens, cookies, private
  keys, session tokens, credentials, and `.env` values.
- Memory may record **where** secrets live and **how** they are handled
  (e.g. "read from env var `BLM_POKERBET_DB`"), but never the secret value.
- The RAG indexer redacts secret-looking values before storage
  (`build_context.py::redact_secrets`), and secret-bearing files are
  excluded by `SKIP_PATTERNS`.
- If a secret is ever found in memory or the index, remove it immediately
  and treat it as compromised (rotate if appropriate).

### 6f. Session startup protocol

At the beginning of every new session:

1. **cd /home/ubuntu/BLM** — begin in the BLM repository root before any
   git, index, or memory operation.
2. **Load `DECISIONS.md`** — read the full memory file, then confirm the
   knowledge store (`~/.clai/knowledge.db`) and RAG index
   (`rag/blm_context.db`) exist and are current.
3. **Identify HEAD and git status** — run `git rev-parse --short HEAD`,
   `git branch --show-current`, and `git status --short`. Verify the repo
   root, current branch, HEAD commit, tracked modifications, and untracked
   files so memory provenance matches reality.
4. **Check RAG/index freshness** — compare the RAG index `built_at` meta
   value against the newest indexed file mtimes. If source/docs/schemas
   changed after the last build, rebuild with `python3 rag/build_context.py`
   before querying, so retrieval reflects the current repository.
5. **Verify memory against current code** — memory is a guide, not ground
   truth (§6b). For any fact you are about to rely on, verify it against the
   current repository (read the file, run the test, query the DB). If memory
   conflicts with code, report the conflict and treat current verified code
   as authoritative (§6b).
6. **Check for drift** — if code, schemas, or docs changed since the last
   memory update, flag the affected sections for refresh (§6d).
7. **Retrieve context relevant to the current task** — before acting, search
   both stores for the specific task (§6a): the RAG index
   (`python3 rag/query_context.py "<query>"`) and the knowledge store
   (`python3 ~/.clai/kb.py search "<query>"`). Store relevant hits with
   `--store` so the knowledge store stays current.
8. **Never load secrets** — treat memory/index as untrusted for credentials;
   secrets are never stored and never assumed present (§6e).
9. **Only then begin analysis** — do not start task work until steps 1–8 are
   complete. If any step fails or surfaces a conflict/drift, report it and
   resolve before proceeding.

### 6g. Standing directives (from this point forward)

1. **DO NOT redesign the RAG architecture unless there is a demonstrated
   defect.** The current foundation (SQLite FTS5 index + knowledge store +
   `DECISIONS.md` + `FOUNDATION.md`) is frozen. Propose changes only when a
   real defect is demonstrated (e.g. wrong results, data loss, or a
   correctness/durability failure), not for speculative improvement.

2. **DO NOT continually add redundant memory entries.** Before adding to
   memory, check whether the fact is already recorded (search both stores and
   `DECISIONS.md`). Do not restate the same decision, rule, or discovery in
   multiple places or in slightly different words. Add memory only when it is
   new, corrects an error, or reflects an actual change. If a fact already
   exists, leave it alone.

3. **DO NOT modify `DECISIONS.md` merely to restate information already
   present.** The file is the durable memory, not a log of confirmations.
   Editing it to rephrase an existing rule, re-record the same fact, or
   acknowledge a directive without adding new substance is redundant memory
   churn. Modify it only for: (a) a new durable fact/decision, (b) a
   correction, or (c) an actual architecture/behavior change.

4. **DO NOT index secrets, credentials, `.env` files, virtual environments,
   caches, compiled artifacts, or generated build artifacts.** The exclusion
   and redaction rules in §3 and the no-secrets rule in §6e are
   non-negotiable. They already cover: secrets/credentials/`.env*`/`.aws-*`/
   SSH keys/`.npmrc`/`.pypirc`/`credentials`/`.netrc`, virtual environments,
   caches, compiled/binary artifacts, generated/build artifacts
   (`dist`/`build`/`target`), and DB binaries (schemas only). Do not weaken
   or bypass them.

5. **Current production state remains authoritative over historical
   documentation.** When a historical doc, milestone note, or recorded
   decision conflicts with the current running code/schema/configuration,
   treat the current production state as authoritative. Historical docs
   describe what was true at a point in time; verify against the live
   repository and runtime before relying on them.

6. **Before changing BLM behavior, retrieve the relevant source code.**
   Read the authoritative module(s) and the functions/classes that own the
   behavior being changed — not just memory or docs. Confirm the current
   implementation, constants, and call sites before proposing or making the
   change.

7. **Before changing BLM behavior, retrieve the relevant tests.** Find the
   test files that pin the behavior being changed (and run them if
   practical). Understand what the tests assert and which contracts they
   protect before altering the implementation, so a change does not silently
   break a pinned invariant.

8. **Before changing BLM behavior, retrieve the relevant `DECISIONS.md`
   sections.** Check the recorded architecture, alert logic, checkpoint,
   formula, bug, rejected-approach, test-requirement, and discovery sections
   that concern the behavior. Confirm whether the change is consistent with a
   prior decision, contradicts a rejected/superseded approach, or is
   prohibited by a production constraint — and surface any such finding
   before proceeding.

9. **Before changing BLM behavior, retrieve relevant historical/backtest
   evidence.** If the behavior affects a statistical rule, alert condition,
   checkpoint, or projection, consult the analysis reports, backtest scripts,
   and out-of-sample studies that originally justified or rejected it. A
   change should not contradict quantitative evidence without an explicit,
   recorded reason.

10. **Before changing BLM behavior, retrieve the relevant DB schema.** If
    the change touches persistence, queries, or derived metrics, inspect the
    live table DDL (`--schemas` query or `sqlite_master`) and the recorded
    relationships in §11. Confirm column names, constraints, join keys, and
    immutability expectations before modifying data or schema.

11. **Before changing BLM behavior, retrieve relevant production evidence
    when applicable.** If the change affects a live path, consult production
    logs, run outputs, reconciliation results, or live DB state (read-only)
    to understand actual observed behavior — not just the designed behavior.
    Use it to confirm the defect or expected effect before acting.

12. **Do not make a change simply because a historical document suggests
    it.** A doc, milestone note, or old decision is context, not a mandate.
    Verify the suggestion against current code, tests, production state, and
    recorded decisions. Make the change only if current evidence supports it;
    otherwise report why it should not be done.

13. **When answering a BLM question, show the evidence used.** Every claim
    must be traceable to its source: cite the file, function/class, line
    range, test, schema, log, or DB result that supports it. Do not answer
    from memory alone — retrieve and display the evidence, or say explicitly
    that no evidence was found.

14. **When proposing a code change, identify:**
    - the authoritative module/function/class being changed;
    - the exact source location (file path and line range);
    - the current behavior and the exact proposed change;
    - the evidence justifying the change (test, backtest, log, schema, or
      production observation);
    - affected tests and pinned contracts;
    - regression risk (which existing behavior/tests/contracts could break);
    - any recorded decision, rejected approach, or production constraint the
      change touches (§6g.6–12);
    - risks and, where applicable, a rollback plan;
    - validation required (which tests/backtests/checks must pass before the
      change is acceptable).
    Do not propose a change from memory or historical docs alone.

15. **Do not make production changes unless explicitly instructed.** A
    proposal, analysis, or draft is not authorization to modify production
    code, DB state, collector behavior, scorecard, Market/Fair, projection
    methodology, or deployment config. Make production changes only after an
    explicit instruction to do so, and only to the scope named in that
    instruction.

16. **Do not restart services unless explicitly instructed.** Deployed
    systemd services (`blm-collector`, `blm-server`) and any other running
    BLM process must not be restarted, reloaded, or stopped on the basis of
    a code change or proposal alone. Restart only after an explicit
    instruction, and only the named service(s).

17. **Do not push commits unless explicitly instructed.** Committing locally
    may be done when asked; pushing to a remote (including `origin`) must
    never happen automatically or on the basis of a code change alone. Push
    only after an explicit instruction, and only the named branch/remote.

18. **Do not alter production database contents unless explicitly
    instructed.** Production DBs (`blm_pokerbet.db`, `blm_metrics_clean.db`,
    `blm_ts.db`, `blm.db`, etc.) are append-only/immutable by design. Do not
    INSERT, UPDATE, DELETE, backfill, rewrite, or trim rows in a production
    DB without an explicit instruction. Read-only queries and schema
    inspection are permitted; mutations are not.







---

## 7. Architecture decisions (durable)

### A. Layered evolution, not replacement

- **V1** (legacy research console) is preserved as-is.
- **V2** (platform) adds enrichment, eventing, and APIs on top of V1.
- **V3** (historical engine) runs as a **parallel pipeline** for research —
  it does not replace V1/V2.
- **V4** (PokerBet live pipeline) is the current production source and
  reconciles against BetConstruct events.

**Rationale:** preserve working code; each layer has a distinct cadence,
schema, and purpose.

### B. Historical data is the primary source of truth

- Snapshots are immutable and append-only.
- Everything is reproducible from stored data; nothing relies on memory.
- Derived values are computed, never manually entered.
- Terminal-score leakage into earlier checkpoint calculations is forbidden.

### C. League/classification isolation (zero statistical leakage)

- Every game/snapshot carries `classification`, `game_family`, `competition`,
  and `region`.
- Identity is `source + source_game_id`.
- `BETUAL_NBA` (4×10 min) and `CYBER_2K26` (4×12 min) are **never mixed**.
- Historical/statistical processing is scoped per classification.

### D. Point-in-time checkpoint methodology

- Checkpoints use **prefix-only** Market, Fair, and Momentum.
- Checkpoint timestamp identifies the source snapshot.
- Frozen checkpoint rows are immutable; the trigger line is carried on the
  alert, not re-derived later.

### E. Market line selection by price, never position

- The Total Points market line is selected by **price**, not array position.
- Opening line (OLV) = first verified line; closing line (CLV) = last
  verified line.
- Live market line is frozen at-or-before the checkpoint.

### F. Storage choices

- **SQLite (WAL)** for portability and zero-infrastructure operation.
- **Denormalised analytical tables** for flat ML training rows.
- **InfluxDB primary / SQLite fallback** for V2 time series.
- Raw snapshots preserve full market JSON for reproducibility.

### G. Pure-function compute pipeline

- All metric computations are **pure functions** (no state, no I/O) in
  `blm_v3/engine/`.
- Presentation never contains business logic.
- Business logic never contains scraping logic.
- Scraping never performs analytics.

### H. Collector principles

- V1: Playwright scraping, ~1s.
- V2: scheduler polls V1, ~20s cadence.
- V3: configurable 250ms with geometric rate-limit degradation and frozen
  market detection.
- V4: one-shot or continuous 20s cadence with BetConstruct reconciliation.

### I. Decision-support, not a betting bot

- BLM is a **statistical decision-support system**, not an automated bettor.
- Recommendations must be statistically explainable and traceable to data.
- The primary objective is identifying favourable UNDER entry timing by
  detecting when the live market diverges from historical expectations and
  begins to regress.

### J. Explicitly deferred work

- Cyber 2K26 game-structure research is **deferred** until the stability gate
  in `PLAN.md` is explicitly satisfied.
- No team/operator identity features, collector changes, DB changes, or
  projection changes are authorized before that gate.

---

## 8. Alert logic decisions (durable)

Source of truth: `blm_v4/live_analytics/under_alert.py`.

### A. The production UNDER trigger (directive 2026-09-14)

```
progress_pct >= 75
AND
required_pts_per_min > league_average_pace * 1.04   (STRICT, RELATIVE)
```

- This is the **whole** statistical rule — no other threshold participates.
- The 4% margin is **relative** to the league average, never an absolute
  pts/min offset.
- The comparison is **strict**: required pace exactly at `avg * 1.04` does
  **not** qualify.
- `league_average_pace` is the game's **own** competition reference
  (`competition_pace`), never a global rate.

### B. The mid-tier rule was removed

- The 50% progress tier was removed: historically a coin flip (48.51%
  UNDER) and not part of the ~69.89% UNDER cohort.
- `actual_pace` is **reported but takes no part** in the decision. The old
  `actual < league_average` leg is gone.

### C. Fail closed

- Any missing/non-finite input, or missing `eligible`, yields
  `active=False`.
- Missing league reference → `active=False`; no other competition's rate is
  borrowed.

### D. Eligibility gates are technical only (LIVE MARKETS ONLY, 2026-09-12)

The only gates besides the statistical rule are data-integrity/live-market
gates:

- Game must be genuinely live.
- Market line must be `LIVE`.
- `STALE` → ineligible `market_stale`; missing/unrecognised → ineligible
  `market_missing` (fail closed).
- Stale game state is a live-gate failure carrying its own reason
  (`stale_state`).
- The opening line is **never** substituted for a stale/missing live line.
- `eligible` can only **suppress**, never create.

### E. Single authoritative definition

- `under_alert.py` is the **only** definition of the condition.
- The dashboard renders the boolean and the numbers it is built from; it
  never re-derives them.
- The displayed gap and the displayed verdict can never disagree.

### F. Frozen trigger line

- The served alert block carries the **frozen** `trigger_line` plus
  `trigger_progress` and `trigger_captured_at`.
- It is the market total in force when the alert's checkpoint was reached.
- It never moves when the market does later.
- Trigger provenance comes from `under_outcome.trigger_observation` — the
  same authority settlement reads.

### G. Progress checkpoints

- Numeric checkpoints: `25`, `50`, `75`.
- Each is its own identity downstream; a later checkpoint never suppresses
  an earlier one.

### H. Q3 BREAK (directive 2026-09-18) — additive, not replacement

- Additional checkpoint identity: string `"Q3_BREAK"`, deliberately not in
  the numeric `CHECKPOINTS` tuple.
- Sits at exactly **75.0%** progress (3 of 4 regulation quarters).
- Boundary = **first** observation at/after that progress: the Q3-end
  sentinel or first instants of Q4, never mid-Q3.
- Same condition, applied with the break's own geometry:
  `remaining_minutes = quarter_minutes` (one full quarter left);
  `required_pts_per_min = (triggered_line - score_at_trigger) / remaining_minutes`;
  `active = eligible AND required > league_average_pace * 1.04` (strict).
- `quarter_minutes` is classification-specific: 10 min `BETUAL_NBA`, 12 min
  `CYBER_2K26` — never hardcoded or shared across classifications.

---

## 9. Checkpoint definitions (durable)

Source of truth: `blm_v4/scorecard.py`, `blm_v4/projection.py`,
`blm_v4/calibration.py`, `PLAN.md`.

### A. Classification-specific game time

- `BETUAL_NBA` = 4 × 10 minutes = 40 regulation minutes.
- `CYBER_2K26` = 4 × 12 minutes = 48 regulation minutes.
- Unknown/None classification falls back to `BETUAL_NBA` default (10/40).
- All progress, elapsed, remaining, and pace use the game's **own**
  classification duration via `duration_for(classification)`.

### B. Primary percentage checkpoints

- `checkpoint_market` stores one row per `(source_game_id, checkpoint_pct)`.
- Primary predictive checkpoints: **10, 20, 30, 40, 50, 60, 70, 80, 90**
  (`FIXED_CHECKPOINT_PCTS`, `PRIMARY_PCTS`).
- `pct100` is the **terminal state** — diagnostic only, excluded from
  primary calibration.
- Half-time boundary rows are pinned at half the full game duration.

### C. Fixed percentage checkpoints vs quarter checkpoints

- Predictions use `checkpoint` identity strings: `q1|q2|q3|q4|final|pct10..pct90`.
- `checkpoint_percent` is the fixed-checkpoint target (0.10..0.90).
- `distance_pct` = `|selected progress - target|` in percentage points.
- `_CHECKPOINTS = ("q1", "q2", "q3", "q4", "final")` are the fixed quarter
  identities.

### D. Point-in-time selection (anti-leakage)

- Checkpoint selection is **point-in-time**: prefix-only Market, Fair, and
  Momentum.
- The checkpoint timestamp identifies the source snapshot.
- No terminal-score leakage into earlier checkpoint calculations.
- Historical checkpoint rows remain **immutable** — never rewritten by a
  later model build.

### E. Elapsed-time mapping

- 50% BETUAL checkpoint = 20 elapsed minutes.
- 50% CYBER checkpoint = 24 elapsed minutes.
- 75% BETUAL checkpoint = 30 elapsed minutes.
- 75% CYBER checkpoint = 36 elapsed minutes.
- Count-down clock sentinel (`12:00`) at a period start clamps the
  contribution at 0 — fixes the 2-minute undercount that mislabeled
  checkpoint positions.

### F. Terminality (bucket-independence)

- Terminality is derived **only** from the row's own game-time evidence:
  `elapsed >= full duration`, `progress >= 1.0`, or the source snapshot's
  finished label / Q4 period-over sentinel.
- The `pct100` bucket name is **not** terminal evidence. A 98.1% snapshot
  frozen into the pct100 bucket is non-terminal.
- Terminal rows are stamped `terminal=1, predictive_eligible=0` with an
  explicit `exclusion_reason`; rows are never deleted.
- Eligibility stamps are backfilled idempotently — existing rows keep their
  recorded values.

### G. Checkpoint API surface

Checkpoint API data exposes:

- `elapsed_minutes`, `progress`, `remaining_minutes`
- `home_score_at_checkpoint`, `away_score_at_checkpoint`
- `clock_at_checkpoint`
- `current_pace`, `required_pace`
- Market/Fair/result fields

### H. Q3 BREAK checkpoint

- Identity: string `"Q3_BREAK"` — additive to numeric checkpoints, never a
  replacement (see §8.H).
- Boundary = first observation at/after 75.0% progress (Q3-end sentinel or
  first instants of Q4).

---

## 10. Formulas (durable)

Source of truth: `blm_v4/projection.py` (single source of truth for
projection math), `blm_v4/scorecard.py`.

### A. Regulation duration

```
BETUAL_NBA:  quarter = 10 min,  full game = 40 min
CYBER_2K26: quarter = 12 min,  full game = 48 min
unknown/None → BETUAL_NBA default (10/40)
```

### B. Clock → elapsed minutes (count-down)

```
elapsed = (quarter - 1) * quarter_minutes + max(0, quarter_minutes - MM - SS/60)
```

- Clock format `MM:SS` or `M'`.
- Period-start sentinel (`12:00`) clamps the contribution at 0 — fixes the
  2-minute undercount that mislabeled checkpoint positions.
- Half-time boundary rows are pinned at `full / 2`.

### C. Progress

```
progress = clamp(elapsed_minutes / full_game_minutes, 0.0, 1.0)   (4 d.p.)
```

### D. Pace (points per full game)

Wall-clock delta (preferred, requires ≥2 scored snapshots ≥30s apart):

```
pace = (points_last - points_first) / span_minutes * full_game_minutes
     = ((H_last + A_last) - (H_first + A_first)) / span_minutes * full
```

Fallback (game clock):

```
pace = (H + A) / elapsed_minutes * full_game_minutes
```

Valid range: `20 <= pace <= 400`; otherwise `None`.

### E. Expected total (projection blend)

```
expected_total = 0.7 * pace + 0.3 * total_line     (when total_line exists)
               = pace                              (when no market line)
```

### F. Expected margin

Game-clock basis (when `elapsed > 1`):

```
expected_margin = (home_score - away_score) / elapsed_minutes * full_game_minutes
```

Fallback (≥2 scored snapshots ≥60s apart):

```
expected_margin = (current_margin - first_margin) / span_minutes * full_game_minutes
```

### G. Team projections

```
home_projection = (expected_total + expected_margin) / 2
away_projection = (expected_total - expected_margin) / 2
```

### H. Live-score floor

```
home_projection = max(home_projection, home_score)
away_projection = max(away_projection, away_score)
expected_total  = max(expected_total, home_projection + away_projection)
expected_margin = home_projection - away_projection
```

### I. Authoritative x.0/x.5 quantization

- Authoritative BLM prediction/fair total must lie on the half-point grid
  `{n/2}`.
- Uses `floor(2x + 0.5 + EPS) / 2` (half-up, **not** banker's rounding).
- Quantize **after** the live-score floor, so the floor can never
  reintroduce an arbitrary decimal.
- Away split is re-derived as `et_q - home` so
  `home + away == expected_total` holds exactly.

### J. Market / Fair definitions

```
market_vs_fair = live_market_line - blm_fair_value   (signed, never discarded)
```

- `blm_fair_value` = `project(snapshots_up_to_checkpoint)` recompute,
  frozen at first write.
- Market line is always **observed** PokerBet data — the model never
  fabricates a line.

Signal labels (RM-3 alignment):

```
market > fair → UNDER_VALUE
market < fair → OVER_VALUE
market == fair → NO_EDGE   (position no-bet, never 'PUSH')
```

`PUSH` is reserved for settlement outcome `actual == market`.

### K. Market line snapshot selection

- `market_snapshot` = most recent snapshot carrying a total line (panel
  ticks without a market payload never count as "no market").
- `opening_snapshot` (OLV) = first verified line; immutable.
- `closing_snapshot` (CLV) = last verified line at-or-before terminal; for
  a live game it is `None` — the latest live line is NOT the closing line.

### L. Model version

```
MODEL_VERSION = "v4-pace-1"
```

Accuracy aggregates are always split by model version — never mixed.

---

## 11. Database relationships (durable)

### A. `blm_pokerbet.db` (production, 12 tables)

**Physical FKs:**

- `snapshots.game_id → games.id`
- `market_observations.game_id → games.id`
- `prediction_scores.prediction_id → predictions.id`

**Logical join keys (no physical FK, but the join path is by convention):**

- `games.source_game_id` is the canonical identity.
- `snapshots.source_game_id` joins `games.source_game_id`.
- `market_observations.source_game_id` joins `games.source_game_id`.
- `checkpoint_market.source_game_id` joins `games.source_game_id`
  (one row per `(source_game_id, checkpoint_pct)`).
- `game_results.source_game_id` joins `games.source_game_id` (one row per game).
- `game_quality.source_game_id` joins `games.source_game_id`.
- `predictions.source_game_id` joins `games.source_game_id`.
- `prediction_scores.prediction_id → predictions.id`, plus
  `prediction_scores.source_game_id` joins `games.source_game_id`.
- `market_history.source_game_id` joins `games.source_game_id`.
- `reconciliation.source_game_id` joins `games.source_game_id`
  (`UNIQUE(source, source_game_id)`).
- `instance_splits.base_id/old_id/new_id` reference game IDs, not `games.id`.

**Chain for predictions → scores → checkpoints:**

```
games(source_game_id)
  └─ predictions(source_game_id, checkpoint, model_version)
       └─ prediction_scores(prediction_id → predictions.id)
  └─ checkpoint_market(source_game_id, checkpoint_pct)
  └─ snapshots(source_game_id, captured_at)   # prefix-only projections
```

### B. `blm_metrics_clean.db` (clean metrics, 7 tables)

**Physical FKs:**

- `clean_projections.observation_id → clean_observations.id`

**Logical joins (by `source_game_id`):**

- `clean_games.source_game_id` = canonical identity;
  `base_game_id` strips the `#iN` instance suffix to the fixture ID.
- `clean_snapshots.source_game_id` joins `clean_games.source_game_id`.
- `clean_observations.source_game_id` joins `clean_games.source_game_id`.
- `clean_projections.observation_id → clean_observations.id`, plus
  `clean_projections.source_game_id` joins `clean_games.source_game_id`.
- `clean_market_observations.source_game_id` joins `clean_games.source_game_id`.
- `deviation_residuals.observation_id → clean_observations.id`, plus
  `deviation_residuals.source_game_id` joins `clean_games.source_game_id`.

**Clean pipeline chain:**

```
clean_games(source_game_id)
  └─ clean_snapshots(source_game_id)
  └─ clean_observations(source_game_id)
       └─ clean_projections(observation_id)
       └─ deviation_residuals(observation_id)
  └─ clean_market_observations(source_game_id)
```

### C. `blm_historical.db` (V3 research, 7 tables)

**Physical FKs:**

- `snapshots.game_id → games.id`
- `market_events.game_id → games.id`
- `market_events.snapshot_id → snapshots.id`
- `predictions.game_id → games.id`
- `predictions.snapshot_id → snapshots.id`
- `signals.game_id → games.id`
- `signals.snapshot_id → snapshots.id`

**Join chain:**

```
games(id)
  └─ snapshots(game_id → games.id)
       └─ market_events(snapshot_id → snapshots.id)
       └─ predictions(snapshot_id → snapshots.id)
       └─ signals(snapshot_id → snapshots.id)
  └─ predictions(game_id → games.id)
  └─ signals(game_id → games.id)
```

### D. Other DBs (smaller)

- `blm.db` (V1): `snapshots.game_id → games(game_id)` (logical, TEXT key).
- `blm_ts.db` (V2 TS fallback): `snapshots_v2.game_id` and
  `line_analysis.game_id` are TEXT game keys (no physical FK).
- `blm_v2.db` (V2): `games.game_id` TEXT unique; `alerts.game_id` is a
  TEXT game key (no physical FK).

---

## 12. Production constraints (durable)

### A. Performance targets (README)

| Metric | Target |
|---|---|
| Snapshot write | <50ms |
| Dashboard refresh | <200ms |
| Replay | 60 FPS |
| Concurrent games | 10,000 |
| Snapshot loss | Zero |

### B. Deployment (systemd)

- Collector: `deploy/blm-collector.service`
  - `ExecStart=... python -m blm_v4.collector --tick 20`
  - `Restart=on-failure`, `RestartSec=10`
  - User `gdi`, working dir `/home/gdi/BLM`
- Server: `deploy/blm-server.service`
  - `PORT=2262`, `HOST=0.0.0.0`, `BLM_ENV=production`
  - `Restart=on-failure`, `RestartSec=5`
  - User `gdi`, working dir `/home/gdi/BLM`
- Live push cadence: 20s (WebSocket `/ws` and collector tick).

### C. Freshness bounds

- `ALERT_MAX_STATE_AGE_S = 300.0` (5 minutes).
- Any game state older than 300s fails closed with reason `stale_state`.
- Market freshness: `LIVE`/`STALE`/`MISSING`; `STALE` or missing → alert
  ineligible (see §8.D).
- `ALERT_MAX_OBS_AGE_S` stays the projector-observation bound.

### D. Stability gate (PLAN.md)

Before deferred Cyber 2K26 research begins, ALL must be stable and reviewed:

- Core data integrity closed.
- Point-in-time checkpoint methodology stable.
- Market/Fair calculations stable.
- Classification-specific timing stable.
- Momentum integrity stable.
- 90-minute live measurement reviewed.
- Live product verified.
- Current scorecard stable.
- No unresolved critical production defects.

### E. 90-minute live measurement window

- Measurement is isolated.
- **No code or DB changes during the measurement window.**
- Collector and measurement data are not altered by subsequent audits.

### F. Historical data immutability (non-negotiable)

- Never modify historical data.
- No backfill, rewriting, or deletion of raw snapshots.
- Historical checkpoint rows remain immutable.
- Derived values must be reproducible from stored data.

### G. Read-only audit discipline

- Database investigation must use SQLite read-only mode where possible.
- Audits recompute from primitives; they do not mutate production state.
- Prospective/confirmation harness is read-only with respect to production
  decision logic.

### H. Explicit prohibition before stability (Cyber-structure research)

Until the stability gate is satisfied:

- Do not perform Cyber-structure research.
- Do not modify production code for Cyber structure.
- Do not alter the collector.
- Do not alter the database.
- Do not alter the scorecard.
- Do not alter Market/Fair.
- Do not alter projection methodology.
- Do not add team/operator features.
- Do not infer developer/operator identity.
- Do not change the current Cyber 48-minute model.

### I. Non-negotiable architectural principles (Constitution)

- BLM is not a betting bot; it is a statistical decision-support system.
- Recommendations must be evidence-based, not heuristic alone.
- Every output must be traceable back to historical and live data.
- Architecture must remain modular, explainable, and extensible.
- Primary objective: identify statistically favourable UNDER entry timing
  by detecting when the live market diverges from historical expectations
  and begins to regress.

---

## 13. Known bugs & historical defects (durable)

### A. Stale/finished games shown as LIVE (fixed 2026-09-16)

**Root cause (three facts combined):**

1. `upsert_game` refreshed `last_seen_at` even on the `status='ended'`
   transition — a finished game got a fresh timestamp.
2. The API's `live` flag was age-only (`LIVE_AGE_S = 15 * 60`); it never
   read `status` or terminality.
3. The dashboard never filtered by status on the backend; alert paths did
   not re-read it.

**Fix:** temporal-freshness fix (2026-09-16) — `ALERT_MAX_STATE_AGE_S = 300s`,
fail-closed `stale_state`, independent 60s game-finished reconciler thread.
Result: 1053 passed / 3 failed, all 3 pre-existing (stash-verified).
Services were NOT restarted (awaiting explicit authorization).

### B. Test fixture collision in `test_historical_context.py` (2026-09-11)

**Root cause:** `_seed_archive` generated game IDs without the competition
dimension (`gid = f"A{i // 2:04d}"`). Seeding two competitions
(`betual-nba`, `betual-kbl`) produced the same IDs and `INSERT OR REPLACE`
clobbered the NBA ledger mapping, so the NBA benchmark key never existed.

**Resolution:** fixture construction was at fault; the application code path
behaved correctly (no fallback for unresolved competition).

### C. EuroLeague alert autopsy (2026-09-20) — below breakeven

EuroLeague since 09-16 ran **52.7% UNDER (below breakeven)** while other
leagues performed better. Documented in
`analysis_filter_backtest_euro_autopsy_2026-09-20.txt`. Investigated as a
margin-mix/league-concentration question, not a code defect.

### D. Pre-existing immutable anomalies (out of scope)

- 12 pre-existing mislabeled `pct20` rows in settlement-repair evidence —
  immutable by design.
- `30741757` had NO WS market observation (documented, not fixed, out of
  scope).
- Live reference rounds `avg_pace` to 4dp — pre-existing, not introduced.

### E. Pre-existing test failures (stash-verified)

At the temporal-freshness fix: **3 pre-existing test failures** unrelated to
the change. Confirm their current status before treating them as regressions.

### F. Known scope boundaries (NOT bugs — deferred/excluded)

- Cyber 2K26 game-structure research is deferred until the stability gate.
- ML / automatic prediction is explicitly deferred.
- Quarter/half-specific scorecard presentation is deferred; current focus is
  time-based projections.
- Unresolved competition → `competition_unresolved` / no context, **no
  fallback of any kind** (by design).
- Unresolved alert verdict → `PENDING` / no colour class (by design).

---

## 14. Rejected / superseded approaches (durable)

### A. Superseded UNDER alert condition: `actual < BOTH` (commit 73b7277)

The earliest production rule (2026-09-13) required **both**:

```
active = actual_pace < required_pace
         AND actual_pace < league_average_pace   (both strict)
```

This was **rejected** by the 2026-09-14 directive and superseded. It lives
on only as an audited superseded record. Reason: the `actual_pace` leg was
statistically unnecessary and the rule was replaced by the single-operator
relative-margin rule:

```
progress >= 75 AND required_pace > league_average_pace * 1.04
```

### B. Removed 50% progress tier (coin flip)

The 50% tier was evaluated and **removed**:

- 50% tier historical UNDER rate: **48.51%** (a coin flip).
- Not part of the ~69.89% cohort.
- Below 75% there is now **no trading alert of any kind**.

### C. Removed `actual < league_average` leg

The `actual_pace < league_average_pace` leg was **removed**. `actual_pace`
is still reported (displayed beside required pace) but takes **no part** in
the active decision.

### D. Absolute pts/min margin rejected

The 4% margin is **relative** to the league average. An absolute pts/min
offset was considered and **rejected** — different leagues/classifications
have different scales.

### E. Global (cross-league) pace reference rejected

`league_average_pace` must be the game's **own** competition reference.
A global rate is never used; missing league reference fails closed.

### F. Opening line as fallback for stale/missing live line rejected

For alert eligibility, the opening line is **never** substituted for a
stale or missing live line. A market we cannot prove is live is treated as
not live (`market_missing`/`market_stale`).

### G. Bucket-name-based terminality (superseded)

An earlier rule stamped a game's final snapshot as terminal because it was
forced into the `pct100` bucket. **Superseded** by bucket-independence:
terminality is derived only from the row's own game-time evidence. A 98.1%
snapshot in the pct100 bucket is **non-terminal**.

### H. `PUSH (equal)` signal label rejected

The old `'PUSH (equal)'` signal label conflated a no-value position with a
line landing. **Rejected** and replaced:

```
market > fair → UNDER_VALUE
market < fair → OVER_VALUE
market == fair → NO_EDGE   (no bet; never 'PUSH')
```

`PUSH` is now reserved for the settlement outcome `actual == market`.

### I. V3 as replacement for V1/V2 rejected

V3 was deliberately built as a **parallel** research pipeline, not a
replacement. V1→V2 remains the live path; V3 is research-oriented with a
different interval, schema, and purpose.

### J. Post-final predictions rejected

Predictions made after the final result are **rejected** (scorecard stat
`rejected >= 1`). No prediction can be scored after the game is final.

### K. Cross-classification quarter lengths rejected

`quarter_minutes` is classification-specific (10 min BETUAL_NBA, 12 min
CYBER_2K26). Hardcoding or sharing across classifications is rejected; all
consumers go through `duration_for(classification)`.

---

## 15. Test requirements (durable)

### A. Suite size and status

- `tests/` contains **88 `test_*.py` files**; current suite collects
  **1178 tests**.
- Verification command:

```bash
cd /home/ubuntu/BLM
python3 -m pytest tests/ -q
```

### B. Pytest configuration (`tests/conftest.py`)

- Asyncio mode: **auto** — async tests need no explicit `@pytest.mark.asyncio`.
- **Prediction-generation freeze:** default for the whole suite is
  `BLM_PREDICTION_FREEZE=0` (unfrozen), so machinery tests can exercise the
  prediction path.
- Dedicated freeze tests re-enable `BLM_PREDICTION_FREEZE=1` and prove the
  frozen production behavior.

### C. Evidence-first / RED-first discipline

- Many milestones use **RED-first**: write the failing test, confirm RED,
  then implement.
- Audits must provide measurements, row counts, source paths, formulas, and
  test results — not "looks good."

### D. Read-only audit exception

- `docs/AUDIT_GAME_TIME_PACE_LIVE_LINE.md`: **Do not add tests during this
  audit.** Audit scope is investigation; tests come after the directive
  authorizes implementation.

### E. Regression coverage for every directive

Each implementation directive carries directed tests. Examples:

- Temporal-freshness fix → `tests/test_temporal_freshness_fix_2026_09_16.py`
  (22 directed tests) plus canaries in 6 existing test files.
- Market line selection → `tests/test_market_line_selection_2026_09_16.py`.
- Alert condition → `tests/test_under_alert_lifecycle.py`,
  `tests/test_live_market_gate.py`, `tests/test_active_alert_triggered_line.py`.
- Settlement → `tests/test_settlement_semantics.py`,
  `tests/test_final_result_color.py`, `tests/test_resulted_alerts_*.py`.
- Checkpoint market → `tests/test_v4_checkpoint_market.py`,
  `tests/test_m009_checkpoint_market.py`.
- Classification duration → `tests/test_classification_duration.py`.

### F. Known-failure baseline

At the temporal-freshness fix: **1053 passed / 3 failed, all 3
pre-existing (stash-verified).** Pre-existing failures must be verified as
such before treating any red test as a regression.

### G. Fixture integrity

- Replayable fixtures exist for both classifications
  (`tests/test_blm_v4_pipeline.py`) proving discovery, classification,
  source, dedup, snapshot persistence, and statistical separation.
- Fixture integrity tests (`tests/test_fixture_integrity.py`) guard against
  fixture drift.
- A historical fixture collision was found and fixed in
  `tests/test_historical_context.py` (see §13.B).

### H. Test naming / scope conventions

- `test_z_*` files run last (frontend migration checks) and guard against
  stale terminology, duplicate alert tiers, and retired numbers.
- "No fabricated value" is a recurring test invariant: missing inputs must
  return `NULL`/fail-closed, never a borrowed or made-up value.

### I. Representative verified subset (this session)

```
python3 -m pytest tests/test_classification_duration.py \
                 tests/test_under_alert_lifecycle.py -q
→ 42 passed, 1 warning in 24.92s
```

---

## 16. Important discoveries (durable)

### A. 100-point predictions were a sentinel, not a prediction

- Example: `30740069#i11` checkpoint q3 projected 50/50 = 100 while live was
  80-82.
- Root cause: q3 snapshot had `quarter=NULL` (event-view rows carry only
  period label). `clock_minutes(q=None)` → `None`; the model's fallback
  produced `expected_total=100` when pace was unavailable.
- **Not a bug**: 100 = "pace unavailable at that checkpoint" sentinel, not a
  real projection. The live-score floor masked nothing.
- Real fix already shipped in `79c4c0d`: collector starts clean with
  identity guard + `_restart_split_suffix`.

### B. Contamination signature: score regression + impossible jumps

- 65 score-regression rows and 20 impossible-jump rows = 85 INVALID, all
  correctly excluded by the quality gate.
- Origin: lobby "1st Quarter 23:00 15-2" snapshots mixed into real games
  (old resolve-path hole), then jumping to the true mid-game state.
- Legitimate quarter transitions / jitter do **not** trigger these.
- Prediction rows may still exist for contaminated games (stored for all
  checkpoints), but they are excluded from scoring. **The gate works.**

### C. Production UNDER edge decays out-of-sample (OOS 1.85 study)

- Production rule (A): historically 56.69% UNDER, +4.88% ROI, but CI
  `[51.68, 61.58]` includes break-even (p=0.163).
- Latest OOS period: **45.74%**, −15.37% ROI, stability classified
  **Decaying**.
- Trailing 3-day P80/P85 percentile thresholds (B, C, D) **fail outright**
  (51.4–52.1%, negative ROI).
- Only candidate **E1** (`actual < required AND actual < league_avg`) stayed
  above 55% in all three chronological partitions — but it was selected post
  hoc and its latest-OOS CI still includes break-even. Verdict: *promising,
  not established*.
- High-win-rate low-coverage cells (F/G/H: 63–69% on 1.4–5.1% coverage) are
  small-sample artefacts, not candidates.

### D. No sustainable >55% candidate at 1.85 (as of 2026-09-15)

The decisive evidence — remaining above 1.85 break-even in genuinely unseen
data with adequate sample and no leakage — was **not present for any
candidate**. No production threshold or code was changed.

### E. Test-fixture competition collision (2026-09-11)

`_seed_archive` generated game IDs without the competition dimension, so
two competition seeds collided and `INSERT OR REPLACE` clobbered the ledger
mapping. Application code path behaved correctly (no fabricated fallback).
See §13.B.

### F. Stale-live root cause was three independent facts (2026-09-11/16)

See §13.A: (1) `upsert_game` refreshed `last_seen_at` on ended transitions;
(2) API `live` flag was age-only; (3) dashboard didn't filter status. All
three had to combine to produce up to 15-minute stale live presentation.

### G. Freshness bound discovery (2026-09-16)

A single `300s` state-age bound (`ALERT_MAX_STATE_AGE_S`) plus fail-closed
`stale_state` and an independent 60s game-finished reconciler thread
resolved the stale-alert class without touching any betting threshold or
model math (all constants bit-identical).

### H. EuroLeague below break-even (2026-09-20)

EuroLeague since 09-16 ran 52.7% UNDER (below break-even) while other
leagues performed better — documented in the EuroLeague autopsy. Investigated
as a margin-mix/league-concentration question.

### I. 30741757-class WS coverage gap

Some games have no WS market observation (`30741757` documented). This is a
known collector market-refresh coverage gap, not a scoring defect.

### J. Shadow fingerprint log + weekly OOS machinery (2026-09-21)

READ-ONLY observation layer (no production change).  Files:

- `scripts/shadow_fingerprint_collector_2026-09-21.py` +
  `scripts/shadow_fingerprint_daily.sh` (cron 15:45 UTC, install-cron
  subcommand) append one fingerprint per 75%-boundary occurrence —
  triggers AND non-triggers — to `shadow_fingerprints_75.jsonl`.
  Append-only, dedup on (game_id, captured_at), settlement updates as
  `_update` lines, mode=ro + query_only on both DBs.
- `docs/SHADOW_FINGERPRINT_LOG_2026-09-21.md` — the frozen design:
  field authorities, parity-check contract, explicit non-goals.
- `scripts/oos_weekly_rolling_2026-09-21.py` merges the discovery CSV
  with the forward log (JSONL supersedes CSV per key; CSV rows without a
  join key are dropped loudly) and re-evaluates PRE-REGISTERED rules
  C1..C6 + R1..R8 over fixed ISO-week partitions, appending to
  `analysis/oos_weekly_history.jsonl`.

Durable facts recorded at first evaluation (2026-09-21, N=281 settled
CLEAN triggers, baseline 59.71% UNDER): C1 88.2% (N=17), C2 74.8%
(N=111), C3 72.8% (N=147), C5 76.7% (N=103), C6 76.0% (N=75), R2
(q3_ratio < 0.90) 78.2% (N=87) — all "ON TRACK" (>= baseline) across
closed weekly partitions, but N per rule is still far below the shadow
bar; R1 (widened req band [1.10,1.35)) is BELOW baseline (56.5%) —
the [1.20,1.35) band drags.  These are observations, NOT promotions;
no alert change is authorized from this data alone.

### K. Historical UNDER fingerprint LAYER in production (2026-09-21)

AUTHORIZATION EXECUTED.  The approved historical fingerprints C1..C6 + R2
were implemented as an ENRICHMENT/CONTEXT LAYER on the existing production
UNDER alert — NOT new alerts, NOT a loosening of the alert condition
(which remains `progress>=75 AND required > league_avg*1.04` verbatim in
`under_alert.py`):

- `blm_v4/live_analytics/under_fingerprints.py` — the layer: seven
  fingerprints (C1 req_ratio in [1.10,1.20); C2 recent3_minus_act<=-0.5;
  C3 req>1.04x AND q3<avg; C4 C1 AND C2; C5 req>1.10x AND q3<avg; C6 C2
  AND q3<avg; R2 q3_ratio<0.90), each TRUE / FALSE / UNAVAILABLE (missing
  data is NEVER TRUE — fail closed), plus `fingerprint_count` (TRUE only,
  RECORDED FOR ANALYSIS, never a threshold) and `fingerprints_fired`.
  C5 thresholds reuse `fingerprint_c5.py` constants (one authority).
- `blm_v4/api.py` — `g["under_alert_fingerprint"]` now serves the full
  layer (was C5-only).  Operands are the SAME point-in-time values the
  alert consumed (projector paces, competition_pace league average,
  trailing recent3, Q3 pace vs competition Q3 archive via the existing
  `q3_pace_reference`).
- `dashboard.js` — active + history rows render `HISTORICAL
  FINGERPRINTS: N` with one ✓ line per fired fingerprint and an explicit
  UNAVAILABLE line (missing operands reported, never absorbed).
- `tests/test_under_fingerprints.py` — 23 tests: directive boundary
  values per fingerprint, UNAVAILABLE semantics, conjunction propagation,
  count/fired invariants, alert-contract untouched, R1 exclusion
  (AST-based: no R1 key/name/1.35 constant; a req_ratio of 1.30 fires
  nothing), and a no-leakage scan (the layer imports no DB/os/json and
  is pure arithmetic over its operands).
- `scripts/fingerprint_parity_2026-09-21.py` + report — the historical
  dataset (CSV merged with the forward shadow JSONL, the same cohort as
  the weekly OOS) run through the PRODUCTION functions: FULL PARITY —
  C1 N=17 88.24%, C2 N=111 74.77%, C3 N=147 72.79%, C4 N=7 100.00%,
  C5 N=103 76.70%, C6 N=75 76.00%, R2 N=87 78.16%, all matching the
  directive's historical observations exactly; ratio-definition drift 0;
  R1 absent.  (Parity note: the dataset stores `recent3_minus_act` at
  6-dp while its pace fields are coarser, so `evaluate_fingerprints`
  accepts an optional authoritative offset; the live API path never
  passes it and always derives C2 from its own full-precision paces.)
- R1 EXCLUDED per directive: not implemented, not registered, not used
  in combinations, not referenced as an active condition; it remains a
  rejected historical finding (56.47% UNDER, below the 59.71% baseline).

Small-sample caveats carry over unchanged: C1 (N=17) and C4 (N=7) are
NOT established production accuracies.  No alert volume, threshold,
margin or pace range was changed; no fingerprint fires an alert.
