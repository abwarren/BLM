# Agent Workflow: TC Cohort Statistics and Dashboard UI Rework

**Status:** Mandatory implementation directive for the TC cohort analytics and UI work.
**Repository:** `abwarren/BLM`
**Work branch:** `feature/tc-cohort-refactor-test`
**Scope:** Analytics, read-only API/read models, dashboard statistics, filters, tabs, layout, scrolling, and test evidence.
**Environment rule:** Test-first on the isolated EC2 parallel instance. Production promotion is not authorized.

## 0. Read these documents first

Before editing code or running commands, read in full:

1. `docs/ENVIRONMENT_BOUNDARIES.md`
2. `docs/TC_REFACTOR_TEST_ENV_REPORT.md`
3. `docs/TRADE_COHORT_REFACTOR_IMPLEMENTATION_STRATEGY.md`
4. `docs/AGENT_DIRECTIVE_TC_COHORT_REFACTOR.md`
5. `docs/PLAN-005-RESEARCH-GATES-AND-PROSPECTIVE-CONFIRMATION.md` (or the exact existing path referenced by the strategy)

This document adds an explicit step-by-step execution order and UI acceptance criteria. If it conflicts with the frozen cohort specification, research plan, or environment boundary, stop and report the conflict; do not silently choose a new rule.

## 1. Hard environment and safety boundary

- Production is the host serving `https://blm.haaats.xyz/`.
- The designated EC2 test entry point is `http://13.247.58.81/login?next=/`; its identity and isolation must be re-verified before any mutating command.
- The previously recorded isolated parallel test instance is `127.0.0.1:2263`, reached through the approved SSH tunnel. The existing `:2262`/`:80` staging mirror must not be replaced.
- Confirm hostname, AWS instance/account, working tree, git remote/branch, database paths, mounts, services, listeners, environment-variable names (never values), and all write targets before running a rescore, migration, service change, or other mutating action.
- If identity/isolation cannot be proved, STOP. Do not point test jobs at production DBs, credentials, queues, browser sessions, services, or endpoints.
- Do not deploy, restart, migrate, backfill, rescore-write, or otherwise alter production. Do not change Auto-Bet, the selected cohort, enabled state, unit/stake, caps, kill switch, dry-run, browser-submit authorization, credentials, or bet execution. Do not launch a betting browser or submit/retry bets.
- The Auto-Bet UI remains strictly read-only. Do not commit secrets, cookies, tokens, private keys, production data, or database files.
- Keep all work on the dedicated test branch and produce a reviewable diff. No auto-deploy and no production promotion.

### Mandatory commit-and-test-deploy cadence

**Always commit and deploy validated work to the isolated EC2 TEST environment. Never deploy to production.**

1. Work only in the verified EC2 test checkout on `feature/tc-cohort-refactor-test` (or a clearly documented child branch if needed for review). Do not make implementation edits on the production host.
2. Commit every coherent, validated work unit—not just at the end. Use small, descriptive commits for evaluator/tests, data/read-model work, API work, UI work, and fixes. Never commit secrets, production data, generated DBs, credentials, or unrelated changes.
3. Before each commit, inspect `git diff`, run the relevant tests, and confirm no production configuration, Auto-Bet behavior, or frozen research rules changed. Record the commit SHA and test result.
4. After each meaningful milestone is committed and its relevant tests pass, deploy that exact commit to the verified parallel EC2 test instance at `127.0.0.1:2263` (through the approved tunnel/deployment path), then run health/API/UI smoke checks. This is a test deployment only.
5. For changes that are not yet safe to run, commit them if coherent but do not deploy a broken or unvalidated build; fix and test first, then deploy. Do not leave a validated milestone undeployed.
6. Re-verify instance identity, branch, commit SHA, target service, database path, and collector-disabled/test-only settings before every deployment. Preserve the existing `:2262`/`:80` staging mirror.
7. If a commit or test deployment fails, capture the logs, stop promotion, repair or roll back the test deployment, and report the failure. Do not hide failures or deploy an unverified revision.
8. A GitHub commit is not permission to deploy to production. Production deployment, merge-triggered release, production restart, migration, or promotion remains prohibited until separate explicit operator approval.

## 2. Canonical cohort rules

Use the frozen server-side cohort evaluator/specification as the only source of truth. Do not recreate cohort thresholds in JavaScript.

Common gates:
- Progress is inclusively 75%–92%, normalized once at the input boundary.
- Scored points >= 70.
- Exclude the Cyber 2K26 simulated feed.
- Line-movement leg is OFF.
- Required pace and league-average pace must be finite, valid, and positive.
- Under price must be finite, valid, and meet the minimum.
- Missing/invalid operands produce UNKNOWN with reason codes, never a pass.
- `pace_ratio = required_pts_per_min / league_average_pts_per_min`.
- `shortfall = required_pts_per_min - actual_pts_per_min`.
- Ratio uses strict `>`; shortfall and price use inclusive `>=`.
- Compare full precision; round only for display.

| Cohort | Label | Pace ratio | Shortfall | Minimum under price |
|---|---|---:|---:|---:|
| TC-X | ELITE | > 1.16 | >= -0.50 | >= 1.60 |
| TC-E | EDGE | > 1.12 | >= -0.50 | >= 1.60 |
| TC-S | SHARP | > 1.08 | >= -0.50 | >= 1.60 |
| TC-C | CORE | > 1.04 | >= -0.50 | >= 1.60 |
| TC-W | WIDE | > 1.00 | >= -0.50 | >= 1.60 |
| TC-D | DEEP | > 1.00 | >= +0.50 | >= 1.60 |
| TC-C+ | CORE+ | > 1.04 | >= +0.25 | >= 1.80 |

Primary-label reporting precedence: **TC-X → TC-E → TC-S → TC-C+ → TC-C → TC-D → TC-W**. Return and preserve every qualifying cohort as well as the primary/tightest reporting label. TC-D and TC-C+ are alternate qualification paths, not simple rungs in one nested ladder. Keep `UNKNOWN` distinct from `NOT_QUALIFIED`.

## 3. Required execution workflow

### Phase 1 — Verify repository and test environment

1. Confirm the current branch and worktree. Preserve unrelated changes.
2. Re-verify the EC2 identity and isolation evidence described in `docs/TC_REFACTOR_TEST_ENV_REPORT.md`; update the report if the actual environment differs.
3. Verify the parallel test instance is isolated and configured with test-only data/output paths, collector disabled, and read-only access to source data.
4. Confirm the existing `:2262`/`:80` mirror is untouched and the test target is the parallel `:2263` instance.
5. Save the command output and redacted evidence needed for the final report.
6. If any target or isolation fact is ambiguous, stop before any write or restart.

**Gate 1:** no implementation or data mutation until environment identity and isolation are confirmed.

### Phase 2 — Inventory existing implementation and define field mapping

1. Inspect actual current code and schemas before assuming names or semantics. Start with `blm_v4/api.py`, `blm_v4/scorecard.py`, the existing cohort modules, DB helpers, and `blm_v4/dashboard/static/index.html`, `styles.css`, and `dashboard.js`.
2. Inventory the existing tables, columns, indexes, timestamps, source IDs, alert firing evidence, snapshot records, settlement sources, existing endpoints, dashboard controls, filters, and scroll containers.
3. Produce a field-mapping table covering source field, units, timestamp semantics, nullability, authoritative source, and destination read model.
4. Distinguish an actual fired alert from a checkpoint row that only would have qualified, and from prospective observations.
5. Document stable game/event identity and the deduplication rule before aggregating results.
6. Identify legacy metric definitions and consumers. Preserve legacy outputs under a clearly labelled collapsed Legacy Diagnostics section; do not relabel legacy C1/C2/C3/C5/R2 data as TC cohorts.

**Gate 2:** review field mapping, event identity, cohort spec version, and actual-firing classification before building aggregates.

### Phase 3 — Implement/verify the canonical evaluator

1. Keep the cohort thresholds and evaluator in one pure backend module with deterministic output and no DB/network/UI side effects.
2. Return all qualifying cohorts, the primary/tightest cohort, rule/evaluator version, evaluation timestamp, status (`QUALIFIED`, `NOT_QUALIFIED`, or `UNKNOWN`), and explicit reason codes.
3. Normalize progress units at the input boundary; reject inconsistent, missing, NaN, infinite, negative, or malformed values.
4. Implement explicit, documented precedence in one function. Never rely on dictionary/list iteration order.
5. Add boundary tests for every cohort: exact ratio threshold fails; just above passes; exact shortfall/price floor passes; just below fails; progress 75 and 92 passes; values outside fail; points 70 passes and 69 fails; simulated feed excluded; line-movement OFF required; missing inputs UNKNOWN.
6. Test overlap, TC-C+ / TC-D precedence, and determinism across repeated runs.
7. Do not change live alert triggering, collector cadence/persistence, settlement behavior, or betting execution.

**Gate 3:** evaluator tests pass and spec/version semantics are documented before rescore or UI work depends on it.

### Phase 4 — Build a firing-time-safe historical read model

1. Inventory source observations, real alert firings, market snapshots, and authoritative settlements. Preserve source rows and IDs.
2. Evaluate each actual firing using only information recorded at or before the firing timestamp. Never use future snapshots, closing price, final total, final game state, or later line movements as features.
3. Freeze trigger-time under price and trigger line from the actual firing evidence. Never replace them with current/closing values.
4. Join settlement only after firing-time evaluation is fixed, using stable event identity.
5. Keep actual firings, hypothetical checkpoint-only rows, and prospective observations separately labelled and filterable.
6. Preserve all genuine firings for audit/drill-down. For headline game-level metrics, use a deterministic first eligible actual firing per game per cohort/rule version (or an already proven authoritative event identity); report firing-level metrics separately.
7. Make the rescore idempotent, repeatable, and test-only. Never overwrite original historical records or write to production.
8. Record reconciliation counts at each stage: source rows, actual firings, evaluable, UNKNOWN/incomplete, each cohort membership, distinct games, settled, UNDER, OVER, PUSH, VOID, unresolved, duplicates, and excluded/mismatched rows.
9. Report any source fields that cannot be reconstructed; do not fabricate missing timestamps, prices, lines, or inputs.

**Gate 4:** source DB is unchanged, reruns are idempotent, temporal firewall tests pass, and all reconciliation differences are explained.

### Phase 5 — Implement auditable cohort statistics and API

For each cohort, report:
- Unique eligible games (N) and actual firing count separately.
- Settled UNDER, OVER, PUSH, VOID and unresolved counts.
- UNDER hit rate with its exact denominator.
- 95% Wilson confidence interval for settled UNDER vs OVER, with push treatment documented and identical across cohorts.
- Trigger-time market-price/trigger-line coverage and coverage denominator.
- UNKNOWN/incomplete counts with reason codes.
- Rule version, source type, date range, and exclusion counts.

Rules:
1. Never count unresolved, void, unknown, or mismatched results as losses.
2. Never silently treat pushes as wins/losses. Show push count and state the rate convention.
3. Show sample size and denominator in every KPI, chart, and comparison.
4. Display price-derived break-even context where valid; at decimal odds 1.60, the simple break-even rate is 62.5% before costs. Do not imply profitability from hit rate alone.
5. Provide comparisons by competition, trigger-time price band, pace ratio, shortfall, progress band, and elapsed game/time band only where fields are valid. Label small samples and missing coverage.
6. Keep historical rescore labelled retrospective; it is not prospective confirmation.
7. Use the existing API/read-model conventions. The strategy lists the intended read-only surfaces: `/api/v4/cohorts/spec`, `/api/v4/cohorts/summary`, `/api/v4/cohorts/ledger`, `/api/v4/cohorts/comparisons`, and `/api/v4/cohorts/quality`. Verify actual route conventions and implement or adapt these without breaking existing consumers.
8. API responses must include source scope, denominators, rule version, date range, pagination metadata, and quality status. Use deterministic ordering, bounded filters, UTC timestamps, safe null handling, and read-only DB connections.

**Gate 5:** API contract tests and aggregate-to-ledger reconciliation pass.

### Phase 6 — Redesign the dashboard information architecture

The dashboard must make TC cohort statistics the primary analytics view, not put new labels on old statistics.

#### 6.1 Navigation and tabs

- Keep global navigation for destinations only: Overview, Live Games, Alerts, Trade Cohorts, History & Results, Model Accuracy, Data Health, Settings.
- Inside Live Games, use tabs only for dataset segments such as All, Under, Over, Watch. Do not duplicate the same state in both tabs and dropdowns.
- Use dropdowns for league, period, signal, and sorting. Put rarely used criteria in an Advanced Filters popover.
- Display active filters as removable chips and provide a Reset Filters action that does not reset application/model configuration.
- Preserve filter state when opening a game and returning to the list.
- Counts must reflect the actual filtered dataset, not stale values.
- Keep classification, data-quality status, source scope (actual firing/checkpoint/prospective), and settlement status as separate concepts/filters.

#### 6.2 Trade Cohorts page

Build a dedicated Trade Cohorts page with:
- Cohort selector/cards for TC-X ELITE, TC-E EDGE, TC-S SHARP, TC-C CORE, TC-W WIDE, TC-D DEEP, TC-C+ CORE+.
- Active rule version and source scope shown prominently.
- Summary cards for unique games, actual firings, settled N, UNDER/OVER/PUSH, hit rate + denominator, Wilson interval, market coverage, settlement completeness, and UNKNOWN count.
- A comparison table with consistent columns and denominator definitions across all seven cohorts.
- Trend charts and breakdowns with sample sizes visible.
- An alert ledger with firing time, league/game, primary cohort, all qualifying cohorts, trigger-time inputs, frozen trigger line/price, rule version, settlement, source type, and data-quality reason.
- Separate views/filters for actual firings, hypothetical checkpoints, and prospective observations.
- A Data Quality panel for UNKNOWN reasons, missing fields, duplicates, settlement mismatches, and coverage gaps.
- Legacy fingerprint output moved to a collapsed, explicitly labelled Legacy Diagnostics section. Preserve data and behavior until dependencies are understood.
- Auto-Bet configuration is display-only; no controls may mutate it.

#### 6.3 Spacing, density and typography

Use a consistent spacing scale; suggested starting values:
- Main content/card gap: 16–20 px.
- Card padding: 14–18 px.
- Section spacing: 16–24 px.
- Small control gap: 8 px.
- Table row height: 40–46 px for desktop.
- Keep table numerals tabular and numeric columns right-aligned.
- Prioritize market total, model total, edge, cohort and outcome. Move infrequently used fields into expanded row details.
- Reduce repeated headings, redundant labels, excessive borders, oversized icons, decorative glow, and empty padding.
- Use color consistently: UNDER/positive, OVER/negative, WATCH/warning, UNKNOWN/data-quality, and neutral/pending. Never use color as the only signal; retain text labels and accessible contrast.
- Treat the dashboard as an analytics terminal: dense but legible, with consistent card and table patterns.

#### 6.4 Scrolling and responsive behavior

- Avoid nested scroll containers wherever possible.
- Use one primary page scroll; use a dedicated table viewport only where necessary.
- Keep table headers sticky while scrolling through long tables.
- Use a compact sticky filter toolbar only where it materially helps; avoid stacking oversized sticky header + tabs + filters.
- For large history/ledger datasets, use server-side pagination or row virtualization, stable sorting, and explicit loading/empty/error states.
- Do not reorder live rows on every data tick. Sorting should be stable; refresh values in place and move rows only when the user changes sorting or a clearly indicated refresh action is taken.
- On laptop widths, prioritize the primary table and stack secondary panels. On mobile, use compact cards or a selected-column view rather than shrinking text to illegibility.
- Ensure keyboard navigation, visible focus, accessible labels, and clear status text.

#### 6.5 Charts

- Default to the smallest useful set of series. For pace-vs-market, show market total and model total first; pace projection and secondary averages should be optional toggles.
- Label units, time range, source scope, and sample size.
- Tooltips must distinguish trigger-time price/line from current/closing values.
- Do not use invented or hard-coded values to fill missing chart data.

**Gate 6:** frontend uses backend cohort APIs only for cohort logic; UI filters/tabs/scrolling work without changing model or alert semantics.

### Phase 7 — Test, reconcile and inspect

Run and record exact commands, exit codes, pass/fail totals and environment:
1. Cohort evaluator unit and boundary tests.
2. Schema/field mapping tests.
3. Rescore no-lookahead, idempotency, source immutability and duplicate-handling tests.
4. Settlement joins and reconciliation tests.
5. API contract, pagination, filtering, null/UNKNOWN and read-only tests.
6. Frontend lint/build/syntax tests and relevant UI tests.
7. Filter combination/reset/persistence tests; counts match rows.
8. Stable sorting and live-update tests; rows do not jump unexpectedly.
9. Sticky headers, scroll behavior, long-table pagination/virtualization, responsive breakpoints, keyboard navigation and accessible labels.
10. Visual acceptance at desktop and laptop widths; capture screenshots with secrets and personal data removed.
11. Confirm Auto-Bet configuration/state is unchanged and read-only.
12. Confirm source database size/mtime or checksum unchanged as appropriate; confirm no production service, DB, or endpoint was accessed or changed.
13. Compare API aggregates to ledger-level recomputation; every difference must be explained.

Do not report tests as passing unless they were actually executed. Distinguish unrun tests, known failures, skipped tests, and environment blockers.

**Gate 7:** no unresolved critical data-lineage, denominator, isolation, UI integrity, or safety failure.

### Phase 8 — Deploy only to the isolated parallel test instance

1. Re-verify environment identity and isolation immediately before deployment.
2. Confirm the exact branch/commit SHA and clean/understood worktree.
3. Deploy only to the approved parallel instance at `127.0.0.1:2263`, using the test-only database/read model and collector disabled.
4. Do not replace or restart the existing `:2262`/`:80` mirror. Do not restart production.
5. Run health checks, API acceptance, UI acceptance, reconciliation, and read-only boundary checks after deployment.
6. Document rollback commands/procedure and the exact files/services affected.
7. If deployment requires a different target, production credentials, a production write, Auto-Bet action, or an uncertain restart, STOP and ask for operator review.

**Gate 8:** the parallel test instance is validated, rollback is documented, and the existing mirror/production remain untouched.

### Phase 9 — Final report and handoff

Report:
1. Environment identity and isolation evidence (redacted).
2. Branch and exact commit SHA.
3. Files changed and architecture decisions.
4. Cohort spec/rule version and precedence.
5. Field mapping and actual-firing/checkpoint/prospective classification.
6. Stage-by-stage rescore counts, cohort denominators, settlement outcomes, Wilson intervals, price/line coverage, UNKNOWN reasons, exclusions, and unresolved reconciliation differences.
7. Exact test commands and results, including failures and tests not run.
8. API examples and screenshots/UI evidence with secrets removed.
9. Scroll/filter/tab behavior checked and responsive widths tested.
10. Confirmation that production, production data/services, and Auto-Bet were untouched.
11. Rollback instructions and known risks.
12. A separate proposed production promotion plan, not executed.

## Definition of done

The work is complete only when cohort statistics are generated from the canonical evaluator and auditable read model; actual firings are separated from hypothetical checkpoints and prospective data; all denominators and exclusions are visible; aggregates reconcile to ledger rows; the UI has coherent navigation, filters, spacing and scrolling; relevant tests and test-environment validation are evidenced; rollback is documented; and production/Auto-Bet boundaries remain intact.

**Stop after test validation. Production promotion requires separate, explicit operator approval.**
