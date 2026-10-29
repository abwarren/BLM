# Trade Cohort refactor + Live Games table — implementation report (EC2 test only)

**Status:** implemented and verified in the designated EC2 **test** environment.
**Not promoted.** No production deployment, no merge-triggered release.
Last updated 2026-10-29 against commit `66707a6`.

- **Branch:** `feature/tc-cohort-refactor-test`
- **Base:** `992cd21` — "snapshot: as-deployed production working tree 2026-10-26
  (52c879d + 27 uncommitted paths)"
- **Session commits:** `b5a536d`, `b7aaa79`, `1289c7b`, `55e58e3`, `462ba6d`, `66707a6`
- **Backup ref:** `backup/tc-local-tip-20261029` → `55e58e3` (taken before any
  further work, so the pre-Live-Games tip is recoverable)
- **Test instance:** `127.0.0.1:2263` on `blm-stage-cpt`
- **Production:** untouched. Auto-Bet: untouched, disarmed, zero executions.

---

## 1. Environment identity and isolation (re-verified 2026-10-29)

| Check | Result |
|---|---|
| Hostname | `blm-stage-cpt` |
| AWS instance | `i-0d6c4d01114f52bf4`, `af-south-1`, account `243047261878` |
| OS / kernel | Ubuntu 24.04.5 LTS, `7.0.0-1014-aws` |
| Instance role | `blm-stage-s3-role` — `iam:ListAttachedRolePolicies` / `iam:ListRolePolicies` / `s3:ListAllMyBuckets` all **denied** (cannot escalate, cannot enumerate) |
| Production reachability | `192.168.1.100:22`, `:2262`, `:443` → **unreachable** from this host |
| Outbound credentials | `~/.ssh/` contains only `authorized_keys`; no private keys |
| Env var names | no `AWS_*`, token, secret, password, `POKERBET_*`, `BETTING_*` or `BLM_*` names in the login shell |
| Collector | no `blm_v4.collector` process |
| `:2262` mirror | untouched — `server.py` `3e56c9fd5dc8` and `blm_v4/api.py` `72fb7185515f` still match the 2026-10-26 baseline |
| `:80` | nginx → 2262, untouched |
| `:2263` | bound to `127.0.0.1` only |

### 1.1 The test instance now has its own data

Two isolation defects were found and fixed during this session:

1. **The `:2263` server wrote into the shared test database.** Its settle /
   line-reference workers were opening `/srv/blm/data/blm_pokerbet.db` — the same
   file the `:2262` mirror serves — so the mirror's data changed because of the
   test instance, and no DB-level count was stable evidence. The instance now
   uses a **disposable clone**
   (`/srv/blm/data/derived/test-2263/blm_pokerbet.test.db`, made with
   `sqlite3 .backup`), and its betting config lives in
   `…/test-2263/blm_betting.db`. The shared file no longer receives test-instance
   writes from 2263.
2. **`/api/auth/me` returned 404** when `BLM_AUTH_ENABLED=0` (the auth router is
   not mounted), which the dashboard probes on every load. It now answers
   `200 {"auth_disabled": true}` on the disabled path only; the enabled
   (production) path is unchanged.

**Residual:** the `:2262` mirror still runs its own workers against the shared
file. That is pre-existing mirror behaviour, outside this task's scope, and it is
why every immutability claim below is made on a **frozen copy**, never on the
live shared file.

---

## 2. Branch authority (resolved, not acted on)

The remote and local branches have **diverged**, and neither remote ref is the
live code line:

| Ref | Relationship to the deployed tree |
|---|---|
| `origin/feature/tc-cohort-refactor-test` (`d962d5d`) | based on `origin/main` `03b242e` (Sept line). Against the deployed snapshot it removes **44,333 lines across 120 files**. Merging it into current code would regress the tree. It is a docs branch carrying one implementation commit on a stale base. |
| `origin/fix/price-mapping-2026-10-06` (`fc2f75d`) | **42 commits behind** the production working tree; 22 files / −2,743 lines vs the deployed snapshot. |
| **Production working tree** (`/home/ubuntu/blm-fix-price-mapping`, HEAD `6a342b9`) | **Authoritative.** Contains `52c879d`; has **25 commits and 13 code files** (`server.py`, `blm_v4/execution/pokerbet/dom.py`, `live_analytics/benchmark.py`, …) that our base `992cd21` does not. |

**Conclusion:** authority is the production working-tree lineage. Our branch sits
on the 2026-10-26 snapshot of that line, so it is **25 commits behind the live
tree** in code. A promotion branch must be ported (rebased or re-applied) onto
`6a342b9` or its successor, resolving conflicts in `server.py` and the execution
modules, before any production review is meaningful.

**No history rewrite was performed.** No force-push, no rebase. The local tip is
preserved on `feature/tc-cohort-refactor-test` and on
`backup/tc-local-tip-20261029`.

---

## 3. What was built in this session

| Commit | Change |
|---|---|
| `b5a536d` | **Auto-Bet cohort selector, backend.** `blm_v4/betting/cohort_selection.py` + two endpoints. One config key (`autobet_selected_cohort`), strict allow-list over the seven frozen codes, one audit row per change, `POST` rejects any field other than `cohort`/`actor`, invalid values → 422 with nothing written. Not consulted by `executor.evaluate` / `command`. |
| `b7aaa79` | **Destinations + Trade Cohorts page.** Eight destinations with one switcher (`nav.js`); `stats.js`/`explorer.js` reduced to polling lifecycles reacting to `blm:view`; Live Games gets dataset tabs, dropdowns, chips, RESET, stable sort; Trade Cohorts becomes its own page (7 cards, 12 KPIs with denominators, comparison, breakdowns, paged ledger, data-quality panel, collapsed legacy diagnostics); Settings carries the cohort selector; `data-quality`/`legacy` labelling. |
| `1289c7b` | `/api/auth/me` answers when auth is disabled (see §1.1). |
| `55e58e3` | Empty state keyed off rendered rows (was keyed off the whole payload), destination gutters, legible empty state. |
| `462ba6d` | **Live Games compact table** as the default surface (13 columns, 34px rows, in-place row updates keyed by `game_id`, sticky header, TABLE/CARDS toggle); two real defects fixed: RESET cleared the widgets but not the filter state, and the GAME column's team order disagreed with the SCORE column. |
| `66707a6` | Two static-contract tests re-pointed at the new markup, preserving their intent (cache-bust mechanism; dropdown option ids). |

### 3.1 The Auto-Bet selector is a LABEL, not a gate

`blm_v4.betting.cohort_selection` persists which cohort Auto-Bet is *configured*
for and reports the fail-closed lock reasons. **Nothing in the execution path
reads it.** Selecting TC-X does not make the executor act only on TC-X alerts.
Wiring the selection into execution is a separate behavioural change requiring
its own specification, authorization, risk controls and tests. It was not done,
and it must not be inferred from the UI.

---

## 4. Cohort specification in force

`rule_version = tc-1.0.0`, effective `2026-10-26T00:00:00Z`.

Common gates: progress 75–92 % inclusive; scored points ≥ 70 (integer-validated,
no silent truncation); Cyber 2K26 excluded by slug; line-move leg OFF (False
passes, True fails, unknown → UNKNOWN); all operands finite and positive.

| Code | Pace ratio (strict >) | Shortfall (≥) | UNDER price (≥) |
|---|---:|---:|---:|
| TC-X ELITE | 1.16 | −0.50 | 1.60 |
| TC-E EDGE | 1.12 | −0.50 | 1.60 |
| TC-S SHARP | 1.08 | −0.50 | 1.60 |
| TC-C CORE | 1.04 | −0.50 | 1.60 |
| TC-W WIDE | 1.00 | −0.50 | 1.60 |
| TC-D DEEP | 1.00 | +0.50 | 1.60 |
| TC-C+ CORE+ | 1.04 | +0.25 | 1.80 |

**Reporting precedence** (named, tested, not iteration order):
`TC-X, TC-E, TC-S, TC-C+, TC-C, TC-D, TC-W`. `qualifying_cohorts` keeps every
match and remains authoritative; the precedence is a label only.

**Cross-check against the live naming path** (`blm_v4.trade_cohorts.resolve_cohort`,
which picks the MAXIMUM cohort by legs): **0 disagreements across the 52
cohort-qualified ledger rows.** Caveat: this sample contains no TC-C+ and no TC-D
row, so the check exercises the pace ladder only — it does not prove parity on
the two alternate routes.

---

## 5. Firing-time lineage and classification

This system persists **no alert ledger**; alerts are derived live. So:

- **A firing is reconstructed**: the first observation of a game where the
  production alert condition held (progress ≥ 75, required > league × 1.04, live
  line and price present). `source_type = 'reconstructed_firing'`.
- Every other qualifying observation is `checkpoint` and is never counted as a
  fired alert (33 qualified checkpoint rows; 1,047 UNKNOWN rows are also stored
  in the alert band with a reason code, for data-quality evidence).
- UNKNOWN ≠ NOT_QUALIFIED, and neither is a pass.
- Settlement is joined **after** evaluation and scored against the frozen
  trigger line captured on the firing row.
- League reference is **strictly prior** (`result_at < observed_at`); the
  `full_sample` mode exists only as a labelled sensitivity run.

### 5.1 Stage counts — measured on the frozen clone (no concurrent writer)

Source: `/srv/blm/data/derived/test-2263/blm_pokerbet.test.db` (clone of the
2026-10-22T13:42 → 2026-10-23T20:34 slice; 163,500 snapshots, 1,118 games).

| Stage | Count |
|---|---:|
| observations evaluated | 134,730 |
| qualified | 52 |
| NOT_QUALIFIED (valid, out of gate) | 13,959 |
| UNKNOWN (reason-coded) | 120,719 |
| reconstructed firings | 75 |
| qualified checkpoints | 33 |
| ledger rows stored | 1,155 |
| games scanned (non-Cyber) | 1,084 |
| Cyber games excluded | 31 |
| settled firings | 73 (55 UNDER / 18 OVER / 0 PUSH / 2 pending) |
| unique qualified games | 19 |

**Read-only proof (frozen copy):** size, mtime and SHA-256
(`1fdfa976…` / `c94c6ab6…`) identical before and after a full rescore;
`quick_check` OK. Re-running the rescore produced a byte-identical ledger
digest over 1,155 rows (`27e13ec4…`), i.e. idempotent.

### 5.2 Per-cohort scorecards (prior_only, actual firings, first firing per game)

| Cohort | games | firings | settled | U | O | P | pending | UNDER rate | Wilson 95 % | firing coverage | break-even |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|---|---:|
| TC-X ELITE | 1 | 1 | 1 | 1 | 0 | 0 | 0 | 100.0 % | 20.7–100 % | 1/1 (100 %) | 62.5 % |
| TC-E EDGE | 6 | 6 | 6 | 5 | 1 | 0 | 0 | 83.3 % | 43.6–97.0 % | 6/6 (100 %) | 55.8 % |
| TC-S SHARP | 9 | 9 | 9 | 8 | 1 | 0 | 0 | 88.9 % | 56.5–98.0 % | 9/9 (100 %) | 53.9 % |
| TC-C+ CORE+ | **0** | 0 | 0 | – | – | – | – | – | – | – | – |
| TC-C CORE | 19 | 19 | 19 | 13 | 6 | 0 | 0 | 68.4 % | 46.0–84.6 % | 19/19 (100 %) | 54.7 % |
| TC-D DEEP | **0** | 0 | 0 | – | – | – | – | – | – | – | – |
| TC-W WIDE | 19 | 19 | 19 | 13 | 6 | 0 | 0 | 68.4 % | 46.0–84.6 % | 19/19 (100 %) | 54.7 % |

Sensitivity (`full_sample` league reference): 72 firings, 69 settled, 51 UNDER /
18 OVER, 3 pending, 16 unique qualified games.

**Limitations, stated plainly:** 31-hour window; n ≤ 19 per cohort; cohorts
overlap (TC-C and TC-W are identical in this sample) so rows are not independent
samples; **TC-C+ and TC-D have zero memberships and PUSH is zero everywhere** —
no evidence exists for three of the seven cohorts; firings are reconstructed,
not recorded; the rescore is **descriptive, not prospective confirmation**.

---

## 6. Tests

Interpreter `/home/ubuntu/blm/.venv/bin/python`, `BLM_ALLOW_HEAVY_TESTS=1`,
`pytest -q -p no:randomly --ignore=blm-dev`, in **separate clean worktrees**.

### 6.1 Full suite, base vs branch

| Run | Commit | Collected | Passed | Failed | Skipped | Wall |
|---|---|---:|---:|---:|---:|---:|
| base | `06c5ca4` | 2,741 | 2,484 | **111** | 146 | 546 s |
| tip | `55e58e3` | 2,756 | 2,497 | **113** | 146 | 663 s |
| final tip | `66707a6` | — | 2,499 | **111** | 146 | 486 s |

Base vs `55e58e3` delta: **111 inherited**, **2 new**, 0 fixed. Both new failures
were static-contract tests pinned to the old markup
(`dashboard.js?v=6855974.11`, `data-filter=""`); neither was a behavioural
regression, and both were re-pointed at the new markup in `66707a6` with their
intent preserved. **Final tip vs base: 111 vs 111 failures — 0 new, 0 fixed, and
15 more passing tests** (the new selector suite).

### 6.2 Targeted sets

| Set | Result |
|---|---|
| TC cohort suite (spec / firewall / API / prior-semantics) | 149 passed |
| Auto-Bet cohort selector | 14 passed |
| 9-file frontend set | 75 passed, 5 failed — the same 5 fail at base (verified in a pristine worktree) |
| `test_execution_window_2026_10_07.py` + `test_v4_api.py` after `66707a6` | 128 passed |
| 4 betting tests (`test_autobet_command_api`, `test_betting_manual_contract`) | 4 failed — identical at base, pre-existing |

### 6.3 UI acceptance (headless Chromium on `127.0.0.1:2263`)

- **Cohorts/destinations run:** 37/37 checks — one visible view per destination
  at 1440×900 and 1280×800, seven cohort cards, 12 KPI cards, 7-row comparison,
  paged ledger with firing-time price/line, data-quality cards, legacy
  diagnostics collapsed, no console/page errors, no failed requests.
- **Live Games run:** 25/26 on the first pass, **26/26 on the deployed build**
  (`389c742`, i.e. `462ba6d` + test/docs commits) after the RESET-state fix — filters,
  chips, individual removal, empty state, sort stability across a poll
  (identical first-12 ids), scroll retention (600 → 600), no duplicate rows or
  poll loop after navigation, CARDS toggle + persistence, three viewports with
  no page-level overflow, no console errors.
- Baseline for comparison: 100 cards (no table), 540 px-tall cards, horizontal
  page overflow at 1152 px.
- Evidence paths (test host): `/tmp/tc-evidence/value/` —
  `ui-acceptance.json`, `live-final/`, `live-baseline/`, `suite2-*.{summary,failures}`.

---

## 7. Production and Auto-Bet boundary

- No production file, service, DB, migration, backfill, rescore write or restart.
- No Auto-Bet change: no enable, no cohort change to production config, no stake,
  limit, kill-switch, dry-run or browser-submit change, no bet placed or retried.
- The test instance's betting DB shows `bet_executions = 0` and audit rows whose
  only event is `autobet_cohort_selection`.
- `:2262` and `:80` untouched; the parallel `:2263` instance is the only thing
  restarted.
- The `:2263` betting worker logs `betting_pass … enabled=False … executed=0`.

**Rollback:** stop the transient unit
(`systemctl --user stop blm-tc2263.service`) — nothing else changed on the host.
The shared test DB was never written by this work; the clone and derived DBs are
disposable files under `/srv/blm/data/derived/`. The branch tip is recoverable
from `backup/tc-local-tip-20261029`.

---

## 8. Outstanding issues and blockers

1. **Broader historical snapshot — BLOCKED.** The directive asks for a rebuild on
   a broader test-only snapshot. Only the 31-hour slice exists on this host; the
   collector is disabled, the mirror sync is paused, and the instance role cannot
   enumerate buckets. No broader data is reachable without operator action.
2. **Branch is 25 commits behind the live server tree** (§2). Promotion requires
   a port onto `6a342b9` or its successor.
3. **Not pushed.** The local branch cannot fast-forward the remote branch, and a
   force-push is prohibited. Pushing under a new branch name is a decision for
   the operator.
4. **Selector is not wired to execution** (§3.1) — deliberate.
5. **Three cohorts have no evidence** (TC-C+, TC-D, PUSH) in this sample.
6. **MODEL ACCURACY has no live data on this host**: the server logs
   `stats_run_failed` / `line_ref_scan_failed` → `unable to open database file`
   because the clean-metrics database does not exist here. Pre-existing
   environment gap, not caused by this work.
7. **The `:2262` mirror still writes the shared test DB** with its own workers
   (pre-existing).
8. **UI acceptance runs are timing-dependent** (they wait for a 20 s poll); they
   passed on the final build but are not part of the pytest suite.

## 9. Proposed promotion plan (NOT executed)

1. Port the branch onto the live server tree (`6a342b9` or successor) in an
   isolated worktree; resolve `server.py` / execution-module conflicts there.
2. Re-run the full suite on the ported branch and compare against a same-day base
   captured on that tree.
3. Rebuild the read model against production **data** (read-only replica or a
   copied snapshot), both league-reference modes, and reconcile counts.
4. Decide the league-reference policy (`prior_only` is causally strict but yields
   far fewer evaluable rows; `full_sample` matches the cached production reference).
5. Review the diff for hidden writes, secrets, deployment hooks and Auto-Bet
   changes.
6. Promote only with separate, explicit authorization naming the commit SHA,
   migration/backfill policy, backup/rollback, maintenance window and stop
   criteria.
