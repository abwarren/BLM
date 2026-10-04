# BLM — Controlled Codebase Cleanup: STEP 1 INVENTORY

> **Status:** INVENTORY ONLY. Nothing deleted, nothing moved, nothing modified.
> **Date:** 2026-10-03 (UTC)
> **Repo:** `/home/ubuntu/BLM`
> **Method:** AST import-graph reachability from the real entry points
> (`server.py`, `blm_v4/collector.py`, `blm_v4/api.py`, `blm_v4/test_stack.py`,
> `run_test_stack.py`, `app.py`, `deploy.py`) + targeted greps for business-rule
> duplication. Reproducible with `/tmp/blm_cleanup_inventory.py` (read-only, opens
> no `.db`).

---

## §0 — NO-DELETION CHECKPOINT (explicit gate)

**This document is the checkpoint. No file may be deleted, moved, renamed, or
have its content changed based on it until the operator signs off.**

Rules binding the whole cleanup:

1. STEP 1 (this file) classifies only. It authorises **zero** mutations.
2. A candidate may move to DELETE only when ALL of the following are recorded in
   `docs/CODEBASE_CLEANUP_REPORT.md`:
   - the exact path(s) and LOC,
   - a reference/import/test search proving nothing live imports it,
   - the owning agent (or "unowned" with evidence),
   - a focused test run proving production behaviour is unchanged after removal.
3. **`UNKNOWN` is never deleted.** Ambiguous → left in place.
4. **No file that is dirty in the working tree (`git status`) is deleted or
   overwritten at all** — that is another agent's or a prior session's
   uncommitted work. This repo currently carries 25 modified + 56 untracked paths.
5. Deletions happen on a **dedicated branch in an isolated `git worktree`**, never
   on `handoff-2026-09-07` in the live tree, and never as `git rm` of a path that
   currently holds uncommitted changes.
6. STEP 4 (removal) is a SEPARATE, explicitly-authorised step. It does not begin
   from this document alone.

---

## §1 — PRODUCTION-PROTECTION SNAPSHOT (STEP 2)

### Git

| Item | Value |
|---|---|
| Branch | `handoff-2026-09-07` |
| HEAD (at session start) | `a2e9514` — "Add the BLM RAG knowledge base under docs/rag/" |
| HEAD (during session) | **`08895e5`** — "Add the canonical PokerBet rung rule to the Auto-Bet RAG docs + tests" (concurrent agent `abwarren`, 22:12:34) |
| Remote | `git@github.com:abwarren/BLM.git` (SSH) |
| Ahead/behind origin | **3 ahead / 0 behind** |
| Working tree | **25 modified tracked + 56 untracked** (81 `git status --short` lines) |
| Worktrees | main tree + `blm-dev/` (branch `dev/local-engineering-2026-09-27`), `/home/ubuntu/blm-test`, + ~16 prunable `/tmp/*` worktrees |

**Do NOT reset, clean, checkout, or stash.** The dirty tree is live production
state (services run from the working tree, not HEAD) *and* other agents' work.

### Services (read-only observation)

| Unit | State | MainPID | Since | NRestarts |
|---|---|---|---|---|
| `blm-server.service` | `active` | 1656 | 2026-10-03 20:52:33 | 0 |
| `blm-collector.service` | `activating` | 26000 | 2026-10-03 22:11:01 | **22** |

> **Finding (report, do not fix):** the collector is currently **flapping**
> (state `activating`, `NRestarts=22` and climbing, fresh MainPID). This matches
> the known self-watchdog restart behaviour. It is recorded here as a service
> observation; **no restart, timeout raise, or intervention is performed.**

### Databases (sizes only; never opened)

| DB | Size | Notes |
|---|---|---|
| `blm_pokerbet.db` | **19.9 GB** | live pipeline (games/snapshots/market_observations/game_results) |
| `blm_metrics_clean.db` | 2.26 GB | clean analytic layer |
| `blm_historical.db` | 140 KB | |
| `blm_betting.db` | 1 MB | essentially empty — no production bets have ever run |
| `blm_auth.db` | 1 MB | dashboard auth |

### Concurrent-agent observation (report, do not absorb)

During this read-only inventory, files I never opened were rewritten on disk
(mtime 22:10–22:12 vs a session start ~22:11): `docs/rag/13_AUTOBET_RUNG.md`,
`docs/rag/05_PRODUCTION_RULES.yaml`, `docs/rag/README.md`, `docs/rag/CONTRADICTIONS.md`,
`tests/test_autobet_rung_rule_2026_10_03.py`, `blm_v4/settle_worker.py`, and `.pytest_cache/*`.

Content reconciliation (so this is not mistaken for my work):
- **ROOT CAUSE FOUND:** HEAD advanced `a2e9514 → 08895e5` mid-session. Commit `08895e5`
  ("Add the canonical PokerBet rung rule to the Auto-Bet RAG docs + tests", agent
  `abwarren`, 22:12:34) is what rewrote `docs/rag/13_AUTOBET_RUNG.md` (and the RAG
  packs) and `tests/test_autobet_rung_rule_2026_10_03.py`. Those mtimes are a **commit**,
  not a stray edit.
- `git status --short docs/rag/` is empty → those files match the new HEAD (no residue).
- Tracked-modified set is **identical** to the pre-session baseline (same 25 names).
- **No staged changes.** Untracked collapsed count moved 56 → 55 (my +1 file accounts
  for it). **I ran no `rm` and made exactly one repo write**
  (`docs/CODEBASE_CLEANUP_INVENTORY.md`, new, still untracked — not swept into 08895e5).

**Consequence for STEP 3:** the rung rule now has an **owner (`abwarren`) and a fresh
commit** on HEAD. It is no longer uncommitted work, but per the CRITICAL OWNERSHIP RULE
this cleanup must still **coordinate before editing `docs/rag/13_AUTOBET_RUNG.md` or
`tests/test_autobet_rung_rule_2026_10_03.py`** — a concurrent agent is actively moving
in exactly that area.

### Test baseline

**Could not be captured on this host.** `tests/conftest.py` hard-refuses a pytest
run while a production pipeline is live (`rc=4`, verbatim: *"REFUSED: a production
BLM pipeline is running on this host: pid 1656 server.py / pid 27056 collector"*).
This is by design (the 2026-09-29 restart-storm guard).

- Last recorded full-suite baseline: **2026-09-27 — 55 failed / 1524 passed /
  3 skipped / 177 warnings (304 s)** (from `docs/milestones/STATUS_2026-09-27_pre_autobet_subsystem.md`).
- A fresh baseline requires either an **isolated scratch checkout** or an explicit
  `BLM_ALLOW_HEAVY_TESTS=1` acceptance of production impact. **Not run** — needs
  its own authorisation (STEP 5 decision point).

---

## §2 — SAFE SERVICE-STATE BOUNDARIES

What this cleanup MAY touch, and what is fenced off.

**GREEN — safe, no production effect**
- Reading any source file.
- Creating NEW documentation under `docs/` (e.g. this file).
- Running read-only analysis scripts against a scratch DB **copy**, never the live DB.
- Working inside a NEW isolated `git worktree` (e.g. `/tmp/blm-cleanup-<date>`).

**AMBER — needs explicit authorisation per action**
- Running focused pytest files (the conftest guard blocks by default).
- Any code edit in `blm_v4/**` that production imports.
- Creating a branch/commit.

**RED — forbidden during cleanup**
- Restarting/stopping `blm-collector`, `blm-server`, or the roulette units.
- Touching the live databases (`blm_pokerbet.db` etc.) in any way.
- `git reset` / `git clean` / `git checkout --` / `git restore` / force-push.
- Deleting or overwriting any **dirty** (modified/untracked) path.
- `git rm` of anything, `git add .`, or committing another agent's work.
- Any Auto-Bet execution, real-money bet, or provider call.

---

## §3 — INVENTORY BY CATEGORY

Legend: **KEEP** = production, keep. **REFACTOR** = keep but consolidate.
**DEPRECATE** = keep on disk, mark abandoned, do not extend. **DELETE** = candidate
for removal once the §0 gate is satisfied. **UNKNOWN** = decide later.

### 3.1 Production modules — KEEP

Reachable from the entry points: **102 of 398 modules.** The live core:

| Module | LOC | Role |
|---|---|---|
| `blm_v4/collector.py` | 4679 | live PokerBet collector (systemd) |
| `blm_v4/scorecard.py` | 3441 | scorecard / frozen-line settlement |
| `blm_v4/api.py` | 3278 | v4 API (alerts, live payload) |
| `blm_v4/storage.py` | 1706 | SQLite storage layer |
| `blm_v4/result_reconciler.py` | 1438 | finals recovery |
| `blm_v4/clean_metrics.py` | 1107 | clean analytic layer |
| `blm_v4/live_analytics/*` | — | alert/pace/fingerprint analytics |
| `blm_v4/auth/*` | — | dashboard auth |
| `blm_v4/dashboard/static/*` | — | frontend |
| `server.py` | 600 | FastAPI entry |
| `blm_v4/betting/*` | ~5.1k | **production Auto-Bet engine (DRY_RUN)** — wired into `server.py:242-249` |

### 3.2 Legacy version trees `blm_v1/` `blm_v2/` `blm_v3/` — SPLIT (corrected)

Reference-proof changed this classification. **`blm_v2` is NOT dead — `server.py::main()`
imports ~15 `blm_v2` modules** (`blm_v2.telemetry.logging`, `timeseries.sqlite_fallback`,
`storage.sqlite`, `events.bus`, `engine.blm_engine`, `engine.adapter`,
`collector.v1_adapter`, `collector.scheduler`, `alerts.manager`, `analytics.*`,
`api.v2_fastapi::create_v2_app`, `dashboard.server::create_dashboard_app`,
`api.dependencies::wire_dependencies`) and **runs them** (creates the v2 app, mounts
`/dashboard`, starts the scheduler thread). The v4 router is mounted *alongside* it.

| Tree | Files | Status | Verdict |
|---|---|---|---|
| `blm_v1/` | 9 | 1 reachable; `blm_v1/collector.py` is **dirty (modified)** | DEPRECATE (do not touch the dirty file) |
| `blm_v2/` | 112 | **imported by `server.py::main()`** + tested | **KEEP the imported shell**; the rest UNKNOWN |
| `blm_v3/` | 33 | only `tests/verify_phase2_collector.py` | DEPRECATE |

Rationale: v4 is the analytics; `blm_v2` is the **server shell / dashboard scaffolding
that is still live**. `blm_v1/collector.py` is modified; `tests/test_v1_*.py` /
`test_v2_*.py` still exercise the legacy trees. **None of the three may be
blanket-deleted.** Blanket "blm_v2 is dead" would break `server.py` startup.

### 3.3 Auto-Bet code already present — REFACTOR / clarify ownership

**Two betting subsystems coexist. Only ONE is production.**

| Subsystem | Modules | Wired to `server.py`? | Verdict |
|---|---|---|---|
| `blm_v4/betting/` | api, config, store, worker, **executor**, engine, machine, precheck, session, provider, fake_provider, adapters, replay, account_guard | **YES** (`server.py:242-249`) | **KEEP** (production, DRY_RUN default, kill-switch OFF) |
| `blm_v4/execution/` | total_executor, execution_queue, selection_model, selection_resolver, adapter, store, audit, betslip_verifier, parlay_matrix, config, **pokerbet/{browser,session,dom}** | **NO importers outside `execution/`** — but **3 test files DO import it**: `tests/test_parlay_execution.py`, `tests/test_pokerbet_live_adapter.py`, `tests/test_execution_game_binding.py` (+ `tests/fake_browser_adapter.py`) | **UNKNOWN** — NOT "dead code": it is an isolated, *tested* subsystem with owner-history (`a4a29d6` added it, `2766883` last touched it). Reconcile, do not blind-delete |

Facts:
- `blm_v4/betting/provider.py::PokerBetProvider.submit` is an **intentional fail-safe
  stub** (raises `ProviderUnavailable`). `DryRunProvider` is the default. `blm_betting.db`
  is empty ⇒ no bet has ever executed.
- `blm_v4/execution/pokerbet/dom.py` (510 LOC) is the **CDP/Playwright DOM adapter**
  with **guessed selectors** (`"button"`, `[class*='market']`) — exactly the
  anti-pattern the Auto-Bet directive forbids. Its CDP endpoint (`127.0.0.1:9222`)
  is currently **closed**, so it is inert.
- The two subsystems are **not** an execution-pipeline duplication yet (only
  `betting/` is live), but they ARE competing implementations of "the PokerBet
  execution layer" — must be reconciled before Auto-Bet work (STEP 3/§3.7).

### 3.4 Duplicate implementations — REFACTOR

Verified duplicate/competing definitions:

| Name | Locations | Verdict |
|---|---|---|
| `pace_gap()` | `blm_v4/live_analytics/pace.py:60` **and** `blm_v4/pace_projector.py:69` | **REFACTOR** — same `required − actual`, None-safe; two rounding helpers. **Caveat:** `pace_gap` is ALSO a persisted SQLite column and a frontend JS field — consolidate the *function*, never rename the identifier. |
| `MarketObservation` | `blm_v4/models.py` (production) **and** `blm_v4/execution/adapter.py` (dead) | dies with the execution subsystem |
| `ReplayEngine` | `blm_v2/replay/engine.py` (legacy) **and** `blm_v4/betting/replay.py` | legacy one DEPRECATEs with blm_v2 |
| `benchmark_key()` | `blm_v4/deviation.py` **and** `blm_v4/live_analytics/pace.py` | verify equivalence before consolidating |
| `checkpoint_for()` | `blm_v4/live_analytics/under_alert.py` **and** `scripts/backtest_under_alert_settlement_basis_2026-09-13.py` | script copy is throwaway |
| `_load_env_file` | `blm_v4/betting/config.py` (canonical) re-used by `execution/config.py` | fine — already shares |

> The AST scan found ~90 same-name defs, but the **only production business-rule
> duplicate is `pace_gap`**. The rest are test/script-local helpers
> (`binom_tail_ge`, `epoch_of`, `fin`, `main`, `client`, …) — not competing logic.

### 3.5 C2 fingerprint — RESTORED 2026-10-04 as a non-gating recorded fingerprint

- `blm_v4/live_analytics/under_fingerprints.py`: **`FINGERPRINT_KEYS =
  ("C1","C2","C3","C5","R2")`**. C2 = `recent_pace_3m - actual_pts_per_min <= -0.5`
  (recent deceleration). **Restored 2026-10-04 (ADR-006), non-gating** — gates
  nothing, creates no alert. **C4/C6 remain RETIRED**, **R1 EXCLUDED**.
- `actual_pts_per_min`, `recent_pace_3m`, `recent_span_2m/3m` are surfaced in
  `blm_v4/api.py` (`_v4_live_uncached` field lists) and `pace_projector.py`; the
  api now ALSO feeds `recent_pace_3m` + `actual_pts_per_min` into
  `evaluate_fingerprints` so C2 is recorded live. The alert verdict
  (`under_alert_state`) is unchanged — the fingerprint layer is not a gate.
- `blm_v4/betting/executor.py:12-13` docstring: fingerprints create no bet.
- C4/C6 evaluation code remains gone (2026-09-27 sync merge). Only C2 was
  re-instated.

### 3.6 Competing market-line / rung calculations — REFACTOR (the RAG/code gap)

This is the highest-priority correctness item for Auto-Bet.

| Concept | Authoritative implementation today | Gap |
|---|---|---|
| market line | `event_parser.parse_event_view` → `total.ladder[].line` / `select_total_market` | single, OK |
| trigger line | `live_analytics/under_outcome.py::trigger_observation` (75 % crossing) | single, OK |
| final result | `game_results.final_result_status='OK'` + RESULTS_PAGE authority | single, OK |
| **rung size / rung rule** | **`docs/rag/13_AUTOBET_RUNG.md` (spec)** + **`tests/test_autobet_rung_rule_2026_10_03.py::under_rung_decision` (reference impl ONLY)** | ⚠️ **NOT wired into production.** `calculate_rungs()`/`should_take_under()` **do not exist** anywhere outside that test. The RAG document describes a rule the code does not enforce. |

> **RAG ↔ CODE DIVERGENCE (the directive's exact warning).** `13_AUTOBET_RUNG.md`
> defines `under_rung_decision(current_line, trigger_line, rung_size)` with the
> `rungs_moved >= -1 → ALLOW / < -1 → REJECT` boundary. That function exists in a
> **test file only**. No production module imports it. Runtime behaviour today has
> NO rung gate at all. This must be resolved in STEP 3, not papered over.

### 3.7 Dead / unreferenced modules — UNKNOWN (verify before any DELETE)

- `blm_v4/execution/*` (17 modules, ~3.0k LOC) — no production importer, **but 3 test
  files cover it** (see §3.3) → UNKNOWN, not dead.
- `blm_v4/calibration.py` (1000 LOC, 5 importers but none from an entry) — **UNKNOWN**.
- `blm_v4/freshness_audit.py`, `blm_v4/interaction_validation.py`,
  `blm_v4/deviation_analysis.py`, `blm_v4/confirmation.py`,
  `blm_v4/live_analytics/maturation_audit.py`, `prospective_health.py`,
  `historical_context.py` — reachable-check needed per module (some are API-adjacent).
- `blm_v2/replay/engine.py`, `blm_v2/analytics/*`, `blm_v3/engine/*` — legacy leaves.
- `rag/build_context.py`, `rag/query_context.py`, `ml/**` — separate tooling (see §3.11).

### 3.8 Temporary scripts — DEPRECATE

`scripts/` = **104 files**, ~90 of them date-stamped one-off
`*_2026-09-*` diagnostics (`analysis_*`, `audit_*`, `backtest_*`, `forensic_*`,
`shadow_*`, `oos_*`, `q1_vs_q2_*`, …). Zero importers. These are the analysis
artefacts behind closed investigations. Some are cited in RAG/STATUS as evidence.

**Verdict:** DEPRECATE (move to `scripts/archive/` or mark with a header), do NOT
delete until each is checked against RAG/STATUS citations. A few are still live
tooling (`scripts/backfill_resulted_finals_2026-10-03.py`, `quarterly-audit.py`,
`collect_pokerbet_quarter_dom.py`) — keep those.

### 3.9 Diagnostics / root artefacts — DEPRECATE

**~40 files at the repo root**: `analysis_*.txt` (28), `forensic_*.txt` (3),
`shadow_*.jsonl` (2), `prospective_health_history.jsonl`, `*.svg` (2),
`point_timeline_*.txt`, `activations_since_2026-09-16.csv`, plus safety scripts
(`blm_*.sh`, `blm_*.py` on the host home dir — outside the repo). These are
session evidence dumps living at the repo root. DEPRECATE → move under
`docs/analysis/` or `analysis/` once referenced paths are updated.

### 3.10 Stale documentation — REFACTOR

| Path | Verdict |
|---|---|
| `docs/rag/13_AUTOBET_RUNG.md` | **REFACTOR** — reconcile with code (§3.6) |
| `docs/rag/*` (13 packs + DECISIONS + EXAMPLES + CONTRADICTIONS.md) | KEEP; `CONTRADICTIONS.md` already tracks doc↔code drift |
| `docs/BETTING_API_CONTRACT.md` | KEEP — describes the live `blm_v4/betting` API |
| `docs/milestones/*` (11) | DEPRECATE the M009/M010 point-in-time audits → `docs/archive/` |
| `docs/milestones/STATUS_2026-09-27_pre_autobet_subsystem.md` | REFACTOR — merge into canonical `STATUS.md` (per AGENTS.md §2.8, avoid duplicate state files) |
| `docs/AGENT_HANDOFF.md` | KEEP as pointer (already a pointer) |
| `docs/AGENT_CONTEXT.md`, `DECISIONS.md`, `PLAN.md`, `ROADMAP.md`, `TODO.md` | KEEP (some may be stale; verify in STEP 4) |
| `DECISIONS.md` §16 fingerprints | **REFACTOR** — §16 describes 7 fingerprints (C1..C6,R2); code has 4 (C1,C3,C5,R2). Stale. |

### 3.11 Experimental areas `ml/`, `rag/`, `docker/`, `deploy/`

- `ml/` (16 files) — isolated ML dev area (`concepts/`, `datasets/`, `training/`,
  `inference/`) for a future model. **KEEP, isolated** (must not affect `blm_v4/**`).
- `rag/` (4 files) — retrieval tooling (`build_context.py`, `query_context.py`).
  **KEEP, isolated.**
- `docker/`, `deploy/` — `deploy/*.service` are **STALE** (live units are under
  `~/.config/systemd/user/`). DEPRECATE.
- `blm-dev/` — a **separate git worktree** (branch `dev/local-engineering-2026-09-27`).
  Out of scope for the main-tree cleanup; do not touch from here.

### 3.12 Obsolete / legacy tests — REFACTOR (needs a test-strategy decision)

`tests/` = 302 files.

- **22 legacy-version tests**: `test_v1_*` (5), `test_v2_*` (5), `test_m009_*` (12).
  These pin `blm_v1/v2` and the M009 milestone. DEPRECATE with their target trees.
- Test-category separation is currently by filename convention only. STEP 5 should
  introduce explicit markers/markers-file so `production / regression / integration
  / Auto-Bet / zero-stake / R2` are distinguishable (directive requirement).
- **`tests/conftest.py` blocks all runs on the production host** — any STEP 5 run
  must use an isolated checkout or explicit override.

### 3.13 Just landed (2026-10-03) — KEEP

`scripts/backfill_resulted_finals_2026-10-03.py`, `tests/test_authoritative_source_contract_2026_10_03.py`,
`tests/test_resulted_backfill_2026_10_03.py`, `tests/test_resulted_alert_settlement_2026_10_03.py`,
`tests/test_panel_discovery_coverage_2026_10_03.py`, `tests/test_quarter_score_capture_identity_2026_10_03.py`,
`tests/test_autobet_rung_rule_2026_10_03.py` — all KEEP.

---

## §4 — CLASSIFICATION SUMMARY

| Bucket | Count (approx) | Examples |
|---|---|---|
| **KEEP** | 102 reachable modules + live tooling + docs/rag | `blm_v4/*` core, `server.py`, `blm_v4/betting/*`, `blm_v2` shell, `ml/`, `rag/` |
| **REFACTOR** | 6 | `pace_gap` (x2), `MarketObservation` (x2), rung RAG↔code, `DECISIONS.md §16`, milestones/STATUS duplication |
| **DEPRECATE** | ~150 | `blm_v1/`+`blm_v3/` (42 files), ~90 date-stamped scripts, ~40 root artefacts, 22 legacy tests, stale `deploy/*.service` (note: `blm_v2` is NOT here — it is live shell) |
| **DELETE (pending gate)** | 0 today | nothing cleared yet — `UNKNOWN` must be resolved first |
| **UNKNOWN** | ~40 | `blm_v4/execution/*` (17, tested-not-wired), `blm_v4/calibration.py`, `freshness_audit.py`, `interaction_validation.py`, non-imported `blm_v2/*` |

---

## §5 — WHAT THIS STEP DID NOT DO

- Did **not** delete, move, rename, or edit any existing file.
- Did **not** open any `.db`.
- Did **not** restart, stop, or reconfigure any service.
- Did **not** run the test suite (blocked by the production-host guard).
- Did **not** create a branch, commit, or push.
- Only new artefact: this file (`docs/CODEBASE_CLEANUP_INVENTORY.md`).

---

## §6 — PROPOSED SEQUENCE (each step gated)

- **STEP 3 — Consolidate business rules.** Create a single canonical
  `calculate_rungs()` + `should_take_under()` in `blm_v4/betting/` (or
  `live_analytics/`), promote `under_rung_decision` out of the test into it, wire
  the gate into `executor.evaluate`, and make `pace_gap` one function. **Behaviour
  change → needs its own authorisation.**
- **STEP 4 — Remove obsolete code.** In an isolated worktree, delete only
  KEEP-cleared candidates (start with the `blm_v4/execution/*` subsystem once
  reconciled). (C2 is no longer a removal candidate — restored 2026-10-04.)
- **STEP 5 — Tests.** Establish a fresh baseline (isolated checkout), add explicit
  test categories, re-run focused suites per removal.
- **STEP 6 — Document.** Reconcile RAG↔code (rung, trigger line, current line,
  market identity, alert lifecycle, settlement authority, Auto-Bet command path).

**Recommended next cleanup (STEP 4 first candidate):** reconcile the
`blm_v4/execution/*` subsystem with `blm_v4/betting/*` — it is the largest coherent
block of Auto-Bet-adjacent code and directly blocks a clean Auto-Bet boundary. Because
it is *tested* (3 suites) and *owned* (a4a29d6/2766883), it must be **reconciled, not
deleted** — hence UNKNOWN, and hence a design decision, not a cleanup sweep.

---

## §7 — AUTO-BET READINESS ASSESSMENT

**NOT READY to build the Auto-Bet command pipeline on today's tree.** Blockers, in order:

1. **Two competing execution layers** (`blm_v4/betting/` live vs `blm_v4/execution/`
   tested-but-not-wired) — must be reconciled to ONE before new work (§3.3).
2. **RAG↔code rung divergence** — the rule the directive mandates is documented
   but **not enforced** (§3.6). Building Auto-Bet on top would create a third
   source of truth.
3. **No live controllable authenticated browser** — the CDP adapter is inert
   (port 9222 closed) and the extension does not exist yet; real DOM discovery
   (Agent 1) still needs live access (paths a/b/c from the Auto-Bet job).
4. **No rung/`should_take_under` production function** to reuse for both the manual
   and autonomous paths (§3.6).

Clean state after STEP 3-6 would give: one betting subsystem, one rung rule
enforced in code AND documented, and a clear boundary for the extension to plug
into — which is exactly the foundation the Auto-Bet directive requires.
