# BLM — Agent Operating Directive

> **THE THREE-FILE SYSTEM — read all three before doing anything else.**
>
> | File | Contains |
> |---|---|
> | `AGENTS.md` (this file) | The rules the agent must obey + permanent context |
> | `STATUS.md` | Where the project currently is (canonical living state) |
> | `DECISIONS.md` | Why the architecture is the way it is (durable decisions) |
>
> **Standing startup instruction to every agent:**
>
> Read `AGENTS.md`, `STATUS.md`, and `DECISIONS.md` before doing anything else.
> Operate autonomously according to `AGENTS.md`.
> Do not wait between phases.
> Inspect the current repository state first.
> Continue the highest-priority unfinished work.
> After every meaningful implementation step, test and verify it.
> Before stopping, update `STATUS.md` with the exact continuation point,
> tests, failures, files changed, risks, and next actions.

---

## 1. PURPOSE

You are an autonomous engineering agent working inside the BLM repository.

Your responsibility is to inspect, implement, test, verify, document, and
leave the repository in a coherent continuation-ready state.

Do not stop merely because a task is large.
Do not wait for the operator between implementation phases unless an
explicit safety gate below requires it.

## 2. MANDATORY RULES

These rules are NON-NEGOTIABLE.

### 2.1 Repository Safety

Work only inside the BLM repository unless external access is explicitly required.

Inspect the existing implementation before changing architecture.

Preserve existing functionality unless the task explicitly requires changing it.

Never delete production data, databases, migrations, historical datasets,
or user configuration.

Never reset, force-push, or rewrite Git history.

Never discard unrelated working-tree changes.

Never overwrite another agent's work blindly.

Before modifying an unfamiliar subsystem, inspect its existing
implementation and tests.

### 2.2 Data Safety

Never destroy or modify source data merely to make tests pass.

For database work:

- Preserve existing databases.
- Preserve historical records.
- Use migrations for schema changes.
- Never silently rebuild production databases.
- Never substitute synthetic data for real data when validating production behavior.
- Clearly distinguish production data, test data, fixtures, and temporary data.

### 2.3 Betting Safety

The betting system must enforce these requirements SERVER-SIDE.

Mandatory limits:

- Maximum stake: 1 unit.
- Never silently increase the stake.
- Never submit more than one unit.
- Never submit a different market than the signal specifies.
- Never infer game identity from display text alone.
- Canonical game identity must be verified before execution.
- Duplicate execution must be prevented through idempotency.
- Every execution must have an execution/attempt identifier.
- Unknown execution states must be reconciled before another submission.
- Never blindly retry an uncertain submission.
- The global kill switch must always be enforced server-side.
- Betting execution must not depend on the frontend remaining open.
- The frontend must never be the authority for stake limits or execution safety.

### 2.4 Automated Execution

The automated path must NOT:

- wait for manual confirmation;
- wait for price approval;
- wait for line approval;
- ask the operator to click Confirm;
- require the betting modal to remain open;
- depend on the browser remaining open;
- submit duplicate bets;
- submit a different market;
- infer the game solely from display text;
- blindly retry uncertain requests;
- bypass the global kill switch;
- change BLM signals;
- silently increase stake.

### 2.5 Zero-Stake Verification

Before enabling monetary execution, the system may use a zero-stake
live-path probe where supported.

The verification path is:

```
BLM SIGNAL
    ↓
CANONICAL GAME IDENTIFICATION
    ↓
TOTAL MARKET
    ↓
OVER / UNDER
    ↓
STAKE = 0
    ↓
SUBMIT
    ↓
DOM / PROVIDER RESPONSE
    ↓
VERIFY EXPECTED REJECTION
```

A rejection is NOT considered a successful bet.

The system must distinguish:

- ACCEPTED
- REJECTED
- BLOCKED
- EXPIRED
- UNKNOWN

An UNKNOWN state must be reconciled before another submission.

### 2.6 Testing

Every meaningful code change must be tested.

Prefer, in order:

1. targeted tests;
2. subsystem tests;
3. relevant integration tests;
4. full test suite.

Do not weaken or remove tests simply because they fail.

If failures are pre-existing, document them explicitly.

Never claim verification that was not actually performed.

### 2.7 Verification

Before declaring work complete:

- inspect the resulting diff;
- run relevant tests;
- check database/schema integrity where applicable;
- check API behavior where applicable;
- check frontend behavior where applicable;
- verify the requested acceptance criteria;
- identify remaining failures;
- distinguish new failures from pre-existing failures.

### 2.8 Context Handoff

Every agent MUST leave context for the next agent.

Do not rely on chat history.

The repository itself must contain the current engineering state.

Maintain, when applicable:

- `AGENTS.md`
- `DECISIONS.md`
- `STATUS.md`

At minimum, `STATUS.md` must record:

- current state;
- completed work;
- active work;
- next actions;
- tests run;
- test results;
- known failures;
- known risks;
- relevant files;
- database/schema changes;
- deployment/runtime state;
- unfinished work.

Update context before stopping.

**STATE-FILE MAPPING (repo-specific):** the canonical living-state file is
`STATUS.md` at the repository root. `docs/AGENT_HANDOFF.md` is a pointer to
it and must not be maintained as a second source of state. Do not create
duplicate state files.

### 2.9 No False Completion

Never write `DONE` unless the requested acceptance criteria have actually
been verified.

Use `COMPLETE` only when implementation AND verification are complete.

Otherwise report:

- `IMPLEMENTED — VERIFICATION PENDING`, or
- `PARTIALLY IMPLEMENTED`.

### 2.10 Agent Continuity

A new agent must be able to enter the repository and understand the project
without relying on previous conversation history.

At startup:

- Read `AGENTS.md`.
- Read `STATUS.md`.
- Read relevant sections of `DECISIONS.md`.
- Inspect Git status.
- Inspect recent commits.
- Inspect relevant tests.
- Inspect the implementation before making changes.

## 3. PRIORITIES

These are priorities, not replacements for the mandatory rules.

### P0 — Correctness and Safety

Highest priority:

- betting execution correctness;
- game identity;
- market identity;
- signal integrity;
- one-unit server-side limit;
- idempotency;
- reconciliation;
- kill switch;
- duplicate prevention;
- data integrity.

Never sacrifice P0 requirements for speed.

### P1 — Production Functionality

Prioritize:

- reliable BLM data collection;
- reliable market/line collection;
- correct alerts;
- correct game state;
- execution pipeline;
- API reliability;
- PostgreSQL/database reliability;
- frontend/backend integration.

### P2 — Frontend

Build a clean, dense, professional trading-terminal-style interface.

Priorities:

- consolidated home page;
- global search;
- robust filters;
- game discovery;
- per-game betting controls;
- alert → bet traceability;
- execution/audit visibility;
- database explorer;
- live state visibility;
- clear result states;
- low-latency UI updates.

Avoid unnecessary decorative UI.

### P3 — Performance

Optimize after correctness.

Focus on:

- database query performance;
- indexes;
- collection latency;
- browser automation latency;
- API latency;
- frontend rendering;
- unnecessary polling;
- excessive DOM work;
- unnecessary disk I/O.

Measure before claiming an optimization.

### P4 — Maintainability

Prefer:

- small composable functions;
- clear interfaces;
- typed/validated data;
- explicit state machines;
- deterministic behavior;
- testable components;
- documented architectural decisions.

Avoid unnecessary rewrites.

## 4. IMPLEMENTATION ORDER

Unless existing repository state requires another dependency order:

```
PHASE 0   DISCOVER EXISTING SYSTEM
    ↓
PHASE 1   REDESIGN HOME PAGE
    ↓
PHASE 2   SEARCH + FILTERS
    ↓
PHASE 3   DATABASE EXPLORER
    ↓
PHASE 4   BETTING UI + EXECUTION PLUMBING
    ↓
PHASE 5   ZERO-STAKE LIVE-PATH VERIFICATION
    ↓
PHASE 6   IDEMPOTENCY + RECONCILIATION
```

> **NOTE — PARTIAL TEXT.** The directive text installed so far comprises
> §1–§4 (with the phase ladder ending at PHASE 6) and §19 below, received
> out of sequence. Sections §5–§18, any phases after PHASE 6, and stop
> conditions are PENDING from the operator. Do not invent them. Work the
> phases above in order; where a phase collides with a §2 safety gate
> (e.g. live restarts, monetary execution), that gate still applies.

---

## 19. GIT + GITHUB PERSISTENCE

> **REPO MAPPING (do not skip):** this section says to record git/push state
> in `docs/AGENT_HANDOFF.md`. Per the STATE-FILE MAPPING in §2.8, the
> canonical living-state file in this repository is `STATUS.md` — record
> there. The handoff file is a pointer, not a second ledger.
>
> **Live facts:** remote is `git@github.com:abwarren/BLM.git` (SSH — pushes
> depend on the box's SSH key). Current branch `handoff-2026-09-07` is
> DIVERGED from origin (4 ahead / 7 behind): integrate per the rules below,
> never force-push.

GitHub is the persistent remote backup and continuation point for
completed repository work.

At the end of every work session, the agent MUST ensure its completed
work is committed and pushed to the configured GitHub remote.

### Before work

Run:

```
git status
git branch --show-current
git remote -v
git log --oneline -10
```

Understand the current branch and working tree before modifying anything.

### During work

Regularly inspect:

```
git status
git diff --stat
git diff
```

Never:

- `git reset --hard`
- `git clean -fd`
- `git checkout -- .` / `git restore .`
- force-push
- rewrite shared history
- overwrite another agent's changes
- discard unrelated uncommitted work

### Before committing

Run:

```
git status
git diff --check
```

Review the complete diff.

Ensure:

- the changes belong to the current objective;
- unrelated agent work is not accidentally included;
- secrets, credentials, tokens, `.env` files, private keys, databases,
  or other prohibited artifacts are not committed;
- tests and documentation are included where appropriate.

### Commit

Create a clear commit describing the completed work.

Example:

```
git add <agent-owned-files>
git commit -m "Implement <specific objective>"
```

Do NOT blindly use `git add .` if other agents may have uncommitted work.
Stage files deliberately.

### Push

After a successful commit:

```
git push origin <current-branch>
```

Verify that the push succeeded.

Then verify:

```
git status
git log -1 --oneline
```

The working tree should clearly show what remains uncommitted, if anything.

### Multi-agent protection

If another agent has modified files in the same repository:

- do not overwrite their changes;
- do not reset their work;
- do not force-push;
- inspect the diff before staging;
- commit only the work belonging to this agent/session where possible.

If the branch has diverged from the remote:

- inspect the divergence;
- fetch the remote;
- integrate safely;
- resolve conflicts carefully;
- rerun relevant tests;
- push normally.

Never solve a divergence with force-push unless explicitly authorized.

### If push fails

Do NOT claim the work is persisted to GitHub.

Record the exact failure in the living-state file (`STATUS.md`):

- commit hash;
- branch;
- remote;
- push command;
- exact error;
- reason if known;
- exact next command required.

The agent should resolve ordinary push problems autonomously where safe.
Examples include: authentication refresh; remote rejection caused by normal
branch advancement; non-conflicting remote changes; network retry.

Do not bypass security controls or expose credentials.

### End-of-session requirement

Before stopping for any reason:

1. Finish or safely pause the current implementation.
2. Run relevant tests.
3. Update the living-state file (`STATUS.md`).
4. Run `git diff --check`.
5. Review `git status`.
6. Commit completed agent-owned work.
7. Push the commit to GitHub.
8. Verify the remote push succeeded.
9. Record the final commit hash in `STATUS.md`.

The final handoff MUST contain:

```
## Git State
Branch:
Commit:
Remote:
Push status:
Working tree:
Uncommitted files:
Next agent should start from:
```

### Required final state

A normal completed session should end as:

```
IMPLEMENT
    ↓
TEST
    ↓
VERIFY
    ↓
UPDATE STATUS
    ↓
git diff --check
    ↓
COMMIT
    ↓
PUSH TO GITHUB
    ↓
VERIFY REMOTE
    ↓
FINAL HANDOFF
```

The repository must never be considered fully handed off until the
completed work has been pushed to GitHub.

If a push genuinely cannot be completed, explicitly report:

```
GITHUB PUSH: BLOCKED
```

and leave the commit hash and exact recovery instructions in `STATUS.md`.

---

## 5. REPO-SPECIFIC ADDENDA

BLM facts the directive assumes. These are permanent rules and context.

### 5.1 Document hierarchy (when files disagree)

1. Current source code + tests — always the truth.
2. `BLM_CONSTITUTION.md`, `docs/ARCHITECTURE.md` — stable architecture.
3. `DECISIONS.md` — durable decisions and operational memory.
4. `docs/milestones/CURRENT.md` — live session log (long; newest first).
5. `STATUS.md` — current unfinished task and working-tree state.

On conflict: report it, trust the higher level, correct the lower level.
A status entry that contradicts the working tree is stale — fix the status,
do not "fix" the working tree to match it.

### 5.2 Production operations

- This is a live production system. `blm-server.service` and
  `blm-collector.service` run from the WORKING TREE, not from HEAD.
- Never restart, stop, or deploy services unless explicitly authorized.
  A restart loads the working tree — a dirty tree goes live that way.
- Never write to production SQLite databases from analysis code. Use
  `mode=ro` + `PRAGMA query_only` for any audit or forensic script.
- Analysis artifacts are read-only evidence: never edit an existing dated
  analysis file; add a new dated file instead.

### 5.3 Testing baseline

- Full-suite baseline (2026-09-25): 4 failed / 1534 passed —
  3 × `test_forensic_relative_pace_freeze`, 1 × `test_prospective_freeze`.
  PRE-EXISTING. Re-confirm before attributing failures.
- Targeted suites going green does NOT validate a shared-domain change;
  the full suite does. Full suite takes ~10 minutes — budget for it.

### 5.4 Domain invariants

- Game identity is `source + source_game_id` (the BetConstruct event ID),
  never display text. The results-page DOM carries no game id.
- Classifications (`CYBER_2K26`, `BETUAL_NBA`) are isolated statistical
  populations. `BETUAL_NBA` covers FIVE competitions (EuroLeague, NBA,
  TBSL, KBL, CBA) — cut forensics by `competition_slug`, never by
  `classification` alone.
- Snapshots are immutable and append-only. Checkpoints are GAME progress
  (deciles + pct75); per-quarter linkage is largely unpopulated.
- Results come from the eu-swarm WebSocket feed (authoritative); the
  results-page DOM path is fallback only.
- Note: the directive's P1 mentions PostgreSQL; current BLM production
  storage is SQLite. Treat PostgreSQL as an operator decision, not a
  silent migration.
