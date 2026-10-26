# Trade Cohort Refactor — Full Implementation Strategy

**Status:** Test-environment deployment authorized by Warren on 2026-10-27. This authorizes deployment and validation of this refactor only in the verified isolated EC2 test environment. It does NOT authorize production deployment/promotion, live betting, real submissions, Auto-Bet changes, or any production data/service changes.

**Repository:** `abwarren/BLM`  
**Plan owner/reviewer:** Warren / designated operator  
**Production:** the existing host serving `https://blm.haaats.xyz/`  
**Test environment:** EC2 `blm-stage-cpt`, instance `i-0d6c4d01114f52bf4`, account `243047261878`; refactor parallel instance `127.0.0.1:2263` (access by SSH tunnel). Treat the recorded isolation report as the baseline and re-verify identity/isolation before each deployment.

Related documents:
- [Environment boundaries](ENVIRONMENT_BOUNDARIES.md)
- [Agent directive](AGENT_DIRECTIVE_TC_COHORT_REFACTOR.md)
- [Research gates and prospective confirmation](PLAN-005-RESEARCH-GATES-AND-PROSPECTIVE-CONFIRMATION.md)

---

## 1. Mission and completion definition

Refactor the BLM analytics/dashboard around explicit named trade cohorts (TC-*), backed by a deterministic, testable cohort evaluator and an auditable historical alert ledger. Keep the work research/analytics-only. The dashboard must distinguish actual alerts that fired from historical records that merely would have satisfied a rule.

The agent must deliver:
1. A verified test-environment identity/isolation report before touching application data or services.
2. A frozen, versioned cohort specification and deterministic evaluator.
3. A firing-time historical rescore pipeline that prevents lookahead and preserves the original alert evidence.
4. API/read-model changes that do not write to production databases or mutate betting settings.
5. A dashboard with cohort scorecards, comparisons, alert ledger, data-quality status, and collapsed legacy diagnostics.
6. Unit, integration, API, frontend, historical-reconciliation, and isolation tests with reproducible results.
7. A final implementation report: files changed, migrations (if any), tests, historical coverage and exclusions, known gaps, screenshots or UI evidence, and a separate proposed production promotion plan.

Do not declare success based only on unit tests or on a dashboard that renders. The historical data lineage, no-lookahead properties, data reconciliation, environment isolation, and read-only betting boundary are release gates.

## 2. Non-negotiable environment and safety boundary

### 2.1 Environment definitions

- **Production** is the existing host serving `https://blm.haaats.xyz/`.
- **Test** is intended to be the EC2 environment reachable at `http://13.247.58.81/login?next=/`.
- The URL alone does not prove host identity or isolation. Treat test as **unverified** until checks below pass.

### 2.2 Mandatory preflight — stop if any check fails

Before running migrations, collectors, rescore jobs, services, browser automation, or any command that can write data:

1. Confirm hostname, instance identity, account/region where available, OS, logged-in user, current working directory, repository remote and branch.
2. Inventory running services, process arguments, service unit files, containers, reverse proxies, bound ports, environment-variable *names* (do not print secret values), scheduled jobs, and deployment hooks.
3. Identify all database paths and connection targets from configuration without exposing credentials. Record canonical paths, mounts, filesystem/device, and whether each target is local or remote.
4. Prove the test process cannot reach or write to production databases, production service endpoints, production credentials, or production submission endpoints. Use explicit test-only configuration and network restrictions where possible.
5. Confirm no production DB file, production volume, production secret, or production endpoint is mounted, referenced, or writable from the test runtime.
6. Record the evidence and checksums/config identifiers in a test-environment report. Redact secrets and personal data.
7. If the EC2 endpoint is still plain HTTP, do not transmit production credentials, tokens, cookies, private keys, or secrets through it. Prefer a verified secure administrative channel. Do not use production credentials to log into test.

**Stop and report a blocker** if identity/isolation cannot be proven. Do not improvise by connecting test code to production data. Do not claim to have inspected a server unless the agent actually performed and recorded the checks.

### 2.3 Production prohibition

Until Warren gives separate, explicit production-promotion approval:
- No production file edits, deploys, merges that trigger deployment, restarts, migrations, backfills, data repair, rescores, collector changes, DNS/proxy/firewall changes, or production DB writes.
- No changes to Auto-Bet enabled state, selected cohort, unit/stake size, daily caps, kill switch, dry-run, browser submission authorization, credentials, or bet execution.
- No live betting, real submissions, automatic retries, or browser relaunch for betting.
- Do not enable CI/CD auto-deploy for this work.
- A merge to the repository is not permission to deploy to production. Test deployment is explicitly authorized only within the isolated EC2 test environment identified in `docs/TC_REFACTOR_TEST_ENV_REPORT.md`. Keep the implementation on its dedicated branch. Do not replace the existing `:2262`/`:80` staging mirror; use the parallel `127.0.0.1:2263` instance unless the operator explicitly approves a different test-only target.

## 3. Cohort specification

### 3.1 Common gates

Every cohort requires all of these:
- Progress is inclusively within **75%–92%**.
- Scored points are **>= 70**.
- Competition/feed is not **Cyber 2K26 simulated**.
- The line-movement leg is **OFF** for this cohort model.
- Required pace and league-average pace are valid, finite, positive values.
- Under price is valid, finite, and meets the cohort's inclusive minimum.
- Outcome and input status are represented explicitly; missing values are UNKNOWN, never silently treated as passing.

Definitions:
- `pace_ratio = required_pts_per_min / league_average_pts_per_min`
- `shortfall = required_pts_per_min - actual_pts_per_min`
- `progress_pct` uses the existing canonical progress field and units. Normalize it once at the input boundary; do not mix 0–1 fractions and 0–100 percentages.
- Comparisons are performed with full stored precision; rounding is for display only.
- Ratio comparison is strict `>`; shortfall and price comparisons are inclusive `>=`.

### 3.2 Named cohort rules

| Code | Name | Pace ratio | Shortfall | Minimum under price |
|---|---|---:|---:|---:|
| TC-X | ELITE | > 1.16 | >= -0.50 | >= 1.60 |
| TC-E | EDGE | > 1.12 | >= -0.50 | >= 1.60 |
| TC-S | SHARP | > 1.08 | >= -0.50 | >= 1.60 |
| TC-C | CORE | > 1.04 | >= -0.50 | >= 1.60 |
| TC-W | WIDE | > 1.00 | >= -0.50 | >= 1.60 |
| TC-D | DEEP | > 1.00 | >= +0.50 | >= 1.60 |
| TC-C+ | CORE+ | > 1.04 | >= +0.25 | >= 1.80 |

All seven rules share the common gates above. The line-movement leg must not be used to qualify or disqualify a cohort beyond the explicit common configuration that it is OFF.

### 3.3 Cohort membership and primary/tightest cohort

Return both:
- `qualifying_cohorts`: every cohort whose full rule passes, in a stable documented order.
- `tightest_cohort`: a deterministic primary label used for headline counts and filtering.

Do not assume the cohorts form one perfectly nested ladder: TC-D and TC-C+ are alternate paths, and price/shortfall dimensions make them partially incomparable. Implement a named, versioned precedence policy in one central function, with tests, rather than relying on iteration order. Proposed default precedence for the primary label: **TC-X, TC-E, TC-S, TC-C+, TC-C, TC-D, TC-W**; the full qualifying list remains authoritative and preserves all matches. Document that this is a reporting precedence, not a claim that every earlier item mathematically dominates every later one. If an existing product requirement or stored cohort policy conflicts, stop and surface the conflict for operator decision rather than silently changing semantics.

If inputs are incomplete or invalid, record `UNKNOWN`/data-quality reasons for the affected evaluation. Never turn missing price, pace, progress, feed identity, or line-movement state into a passing boolean. Keep `NOT_QUALIFIED` distinct from `UNKNOWN`.

### 3.4 Rule versioning

Give every evaluation a stable `rule_version` and effective timestamp. Preserve the exact rules/config used by historical and prospective observations. Never retroactively relabel old fingerprint results as TC-* results without rerunning the evaluator against valid firing-time inputs.

## 4. Data model and lineage

First inventory the existing schema and current source-of-truth fields; do not assume column names. Produce a field mapping before implementation. Reuse existing authoritative tables where suitable and avoid unnecessary duplication.

Every real alert firing / ledger observation should retain or derive from immutable evidence:
- unique firing/alert ID and stable game/event ID;
- event/competition/league/feed identity;
- actual alert-fired timestamp in UTC and original timezone/source timestamp if applicable;
- rule version and evaluator version;
- observed progress and scored points at firing;
- required points/minute, actual points/minute, league-average pace, pace ratio, shortfall;
- under price at trigger time and its source/observation timestamp;
- frozen `trigger_line` / market line at trigger time, never replaced by later live or closing lines;
- line-movement-leg state and exclusion/gate results;
- all qualifying cohort codes and primary/tightest cohort;
- settlement status, settlement timestamp/source, and final outcome (UNDER/OVER/PUSH/VOID/UNKNOWN as supported by the authoritative settlement source);
- data-quality flags, missing-field reasons, source provenance, and deduplication/grouping keys.

Do not fabricate missing firing timestamps, prices, lines, or cohort inputs from later observations. Keep a distinction between:
1. **Actual firing**: evidence that the alert really fired at a recorded time.
2. **Historical eligibility/checkpoint**: a historical row that satisfies a condition but does not prove an alert fired.
3. **Prospective observation**: data collected under the frozen spec after the defined start time.

Use separate source/status labels and filters for these. Never mix them into one performance denominator without explicit labeling.

### 4.1 Historical rescore procedure

1. Inventory historical alerts, raw observations, market snapshots, settlement records, and schema versions; document join keys and timestamp semantics.
2. Identify genuine firing events and their firing-time inputs. Preserve source rows and IDs.
3. Reconstruct only from data that was available at or before firing time. Apply a strict temporal firewall; settlement may be joined only after the firing-time evaluation is fixed.
4. Freeze the trigger price and trigger line as recorded at firing. Do not use closing prices, final totals, later line movements, or final game state as input features.
5. Evaluate every rule against the firing-time snapshot. Store all memberships, the primary cohort, rule version, and missing-input status in a derived/read-model dataset. Never overwrite the original alert.
6. Join settlement separately using authoritative stable event identity. Unsettled, void, ambiguous, or mismatched results are excluded from settled hit-rate denominators and counted separately.
7. Validate duplicate alerts and repeated snapshots. Retain all real firings for drill-down. For game-level headline statistics, use a deterministic, documented **first eligible actual firing per game per cohort/rule version** (or a better existing authoritative event identity if it is demonstrably correct). Do not count repeated firings as independent games. Show firing-level metrics separately from game-level metrics.
8. Keep checkpoint-only rows in a separate hypothetical analysis; never present them as alerts that fired.
9. Record reconciliation counts at every stage: raw rows, actual firings, evaluable, unknown/incomplete, qualified by cohort, unique games, settled, valid UNDER/OVER/PUSH/VOID, and excluded/mismatched rows.
10. Make the rescore repeatable and idempotent in test. No writes to production; do not modify source historical records.

The previously reported 613 settled/scorable games and 58.73% UNDER rate are legacy-rule figures only. They are not evidence for the new TC-* cohorts. Do not reuse or relabel them as cohort results. Recompute from the actual firing-time evidence and report any inability to reconstruct it.

## 5. Performance metrics and statistical presentation

For each cohort, report at minimum:
- unique eligible games (N);
- settled UNDER, OVER, PUSH, VOID and unresolved counts;
- UNDER hit rate with denominator clearly defined;
- 95% Wilson confidence interval for settled binary UNDER vs OVER, with the treatment of PUSH explicitly stated and consistent across all cohorts;
- market-data coverage: eligible observations with valid trigger-time price and trigger line / total eligible;
- firing-level count separately from unique-game count;
- UNKNOWN / incomplete count and reasons;
- rule version, source type (actual firing vs checkpoint vs prospective), and date range.

Do not combine pushes with wins or losses without a documented metric definition. Do not use unsettled/void/unknown outcomes as losses. Always show denominators and exclusions. Avoid implying statistical significance from overlapping cohorts or repeated alerts.

Price context:
- At decimal odds **1.60**, the simple break-even win rate is **62.5%** before fees, stake sizing, voids, and other costs.
- Display the applicable break-even rate from each actual trigger price where useful; never claim profitability based only on a hit rate above 50%.
- Historical cohort results are descriptive unless a valid prospective protocol and sample-size/decision rule establish stronger evidence.

Provide breakdowns by competition, price band, pace ratio, shortfall, progress band, and elapsed game/time band where inputs are valid and sample sizes are shown. Use consistent bins and disclose small/empty groups. Avoid cherry-picked thresholds or post-hoc claims.

## 6. API and backend design

Relevant current areas to inspect first include `blm_v4/api.py`, `blm_v4/scorecard.py`, dashboard routes/data providers, and database access helpers. Confirm actual current structure before coding.

Implementation expectations:
1. Put cohort thresholds and evaluation logic in a single pure module with no DB/network/UI side effects.
2. Add schema-validated data-transfer/read-model structures for evaluation, scorecards, ledger rows, and quality diagnostics.
3. Add versioned read-only API endpoints or extend existing endpoints using current project conventions. Keep legacy consumers backward-compatible where practical.
4. All analytics reads must use read-only DB connections or a derived test-only dataset. Any derived materialization must have an explicit test-only output path, idempotent keying, and clear provenance.
5. Add bounds/allowlists to filters, stable pagination, explicit UTC timestamps, deterministic ordering, and safe handling of missing values.
6. Return metric denominators, source type, rule version, and data-quality status with aggregates so the UI cannot silently mislabel partial data.
7. Never expose credentials, session tokens, private configuration, or personal data in API output/logs.
8. Do not change collector cadence, collector persistence, settlement behavior, live alert triggering, bet execution, or production schemas unless the task is separately scoped and approved. This project is an analytics/display refactor, not an alert-engine redesign.

## 7. Dashboard requirements

Inspect `blm_v4/dashboard/static/index.html`, `styles.css`, `dashboard.js`, and the actual current API contract before changing the frontend.

### 7.1 Trade Cohorts overview

Create a dedicated **Trade Cohorts** panel with a selectable cohort view:
- TC-X ELITE, TC-E EDGE, TC-S SHARP, TC-C CORE, TC-W WIDE, TC-D DEEP, TC-C+ CORE+;
- explicit indication of the active rule version and source scope (actual firings, checkpoint-only, or prospective);
- scorecards for N, UNDER/OVER/PUSH, hit rate, Wilson CI, market coverage, unknowns, and settlement completeness;
- clear empty/error/loading states and accessible labels.

### 7.2 Cohort comparisons

Provide filters and grouped summaries for:
- competition/league;
- under-price bands and price-derived break-even;
- pace-ratio bands;
- shortfall bands;
- progress bands;
- game-time/progress timing;
- actual firing vs hypothetical checkpoint vs prospective data source.

Show sample sizes and denominators in every chart/table. Avoid misleading comparisons where data coverage differs.

### 7.3 Alert ledger

Add a paginated, filterable ledger that includes:
- firing timestamp;
- league/competition and game/event;
- primary cohort plus all qualifying cohorts;
- progress and points;
- required pace, actual pace, league average, pace ratio, shortfall;
- trigger-time under price and frozen trigger line;
- settlement result/status;
- data status and reason codes;
- rule version and source type.

Show the trigger line and trigger timestamp from firing-time evidence, not current or closing market values. Make UNKNOWN and checkpoint-only rows visually unmistakable. Do not imply a checkpoint is a fired alert.

### 7.4 Legacy diagnostics

Move old C1/C2/C3/C5/R2 fingerprint tables and other legacy research tables into a collapsed **Legacy Diagnostics** section. Label them as legacy-rule results and preserve access for comparison. Do not delete or rename them as TC-* cohorts. Do not remove the legacy data until its source, use, and dependencies are inventoried.

### 7.5 Auto-Bet panel: strictly read-only

The Auto-Bet panel may display persisted configuration and status, including selected cohort (currently reported as TC-X), enabled state, dry-run, browser-submit authorization, kill switch, unit price/size, daily limits and missing-limit warnings. It must not write configuration.

- Do not change the selected cohort merely because the dashboard defaults to TC-X.
- Do not toggle enabled/disabled, change stake/unit size, set caps, change kill switch, authorize browser submission, launch a browser, submit a bet, or retry a submission.
- If a setting is missing or invalid, show a fail-closed warning; do not invent defaults.
- Production stake remains the user's persisted UI Unit Size. R2.00 is only an explicitly authorized REAL_MONEY_TEST value, never a production default.
- No Auto-Bet behavior or execution changes are in scope for this analytics refactor.

## 8. Testing and validation gates

Create or update tests using the repository's existing test conventions. Include:

### 8.1 Rule boundary tests
For every cohort:
- pace ratio exactly at threshold fails; just above passes (when other gates pass);
- shortfall exactly at minimum passes; just below fails;
- price exactly at floor passes; just below fails;
- progress 75 and 92 pass; values just outside fail;
- points exactly 70 pass; 69 fail;
- simulated Cyber 2K26 feed is excluded;
- line-movement leg OFF passes the common configuration; ON/unknown must not silently pass;
- missing, null, NaN, infinite, negative, malformed, and inconsistent values become UNKNOWN/invalid with reason codes;
- overlapping cohorts return all memberships and deterministic primary cohort;
- rule ordering does not depend on DB or hash iteration order.

### 8.2 Historical temporal-firewall tests
- later snapshots, closing price/line, and settlement data cannot change firing-time qualification;
- trigger line/price are immutable in derived output;
- checkpoint-only records cannot be counted as actual firings;
- unsettled, void, duplicate, mismatched, and unknown records are handled as specified;
- one game with repeated alerts is counted once in game-level metrics and retained in the ledger;
- rerunning the rescore is idempotent and reconciles to the same counts.

### 8.3 Backend/API tests
- correct filtering, pagination, UTC ordering, stable identifiers and version fields;
- denominator, Wilson interval, price coverage and unknown counts are correct;
- read-only endpoints do not mutate DB rows, alert state, betting configuration, or production resources;
- API outputs distinguish source types and missing-data states;
- malformed filters and unavailable data fail safely.

### 8.4 Frontend tests
- all seven cohorts render correctly and filters work;
- ledger shows trigger-time line/price/time and all memberships;
- empty, loading, error, UNKNOWN, VOID, PUSH, and unsettled states are clear;
- legacy diagnostics are collapsed by default and remain accessible;
- Auto-Bet controls are not interactive/writable as part of this panel;
- accessibility, responsive layout, and no console/API errors.

### 8.5 Test-environment isolation and regression
- capture a before/after snapshot of test DB/config metadata and service state;
- prove no production endpoints, DBs, secrets, or write targets were used;
- run focused tests, full relevant test suite, lint/type checks where present, and build/UI checks;
- record exact commands, exit statuses, commit SHA, test counts, warnings/failures, and known flakes;
- run a historical reconciliation report and manually inspect representative boundary and UNKNOWN cases;
- compare existing dashboard/API behavior for regressions.

Do not hide, waive, or casually fix unrelated failures without documenting them. Do not claim a test passed if it was not executed.

## 9. Implementation sequence / required checkpoints

### Phase 0 — Read-only reconnaissance
- Read environment-boundary docs and PLAN-005.
- Inspect repository tree, current API, scorecard, DB schema, alert provenance, dashboard and tests.
- Produce a field/source mapping, dependency inventory, data-flow diagram, and change plan.
- No data writes or service changes.

**Gate 0 deliverable:** reconnaissance report and verified test isolation evidence. Stop if test isolation is not proven.

### Phase 1 — Specification and fixture set
- Encode the cohort rules and common gates in a versioned spec.
- Build small synthetic fixtures for boundary, overlap, missing data, exclusion, repeated alerts, and temporal leakage.
- Resolve any conflict with current authoritative field semantics before implementation.

**Gate 1:** operator-reviewable spec and passing pure evaluator tests before historical backfill.

### Phase 2 — Pure evaluator and data contracts
- Implement the pure cohort evaluator, reason codes, all-membership output, primary precedence, and version tagging.
- No writes to production, no live alert changes, no Auto-Bet writes.

**Gate 2:** complete boundary tests pass.

### Phase 3 — Test-only historical rescore
- Build an idempotent test-only rescore/read model from real firing-time records.
- Keep original alert data immutable.
- Join settlement only after firing-time evaluation; reconcile counts and report gaps.

**Gate 3:** temporal-firewall tests pass and reconciliation is reviewed. If firing-time inputs cannot be reconstructed, report limitations; do not substitute later values.

### Phase 4 — Read-only API
- Add or adapt cohort scorecard, comparison, ledger and data-quality endpoints.
- Verify read-only DB behavior, stable filtering/pagination and regression compatibility.

**Gate 4:** API and integration tests pass.

### Phase 5 — Dashboard
- Add Trade Cohorts panel, comparisons, ledger, source/data-quality labels, collapsed Legacy Diagnostics and read-only Auto-Bet status.
- No settings mutations or execution controls.

**Gate 5:** frontend and end-to-end test checks pass.

### Reviewer follow-up actions — required before test acceptance

The following code-review actions are part of the authorized test-environment work. Complete them on this dedicated branch and validate in isolation:

1. **Validate scored points as an integer.** Reject Boolean, fractional, negative, malformed, NaN and infinite values as invalid/UNKNOWN rather than silently truncating a numeric value such as 10.9. Add focused evaluator boundary tests.
2. **Make the prior-only league reference strictly prior.** A settled result with `result_at == observed_at` must not influence that observation's league reference unless a trustworthy causal ordering proves the result was already available. Prefer strict-before semantics and add an exact-timestamp regression test.
3. **Align market-coverage denominators.** Do not present checkpoint-inclusive coverage as though it were measured on the first-firing-per-game outcome cohort. Either calculate coverage on the same population or clearly label the distinct observation-level and firing-level denominators in the API and UI; test the chosen contract.
4. **Rebase in test only.** Rebase/port the refactor onto the current live-server code line (`fix/price-mapping-2026-10-06`, or verify and use its current successor) in the isolated EC2 test checkout. Resolve conflicts there, preserve the existing `:2262`/`:80` mirror, and do not merge or deploy to production.
5. **Re-run validation after the fixes and rebase.** Run focused cohort tests, the full relevant suite, lint/type/build checks where available, API/UI checks, isolation checks, and source-DB immutability/reconciliation checks. Record the existing 54 baseline failures separately; investigate any new failures rather than assuming they are pre-existing.
6. **Rebuild the derived read model on a broader test-only historical snapshot.** Keep source data read-only and isolated. Compare `prior_only` with `full_sample` as clearly separated sensitivity analyses; use `prior_only` for causal/predictive evaluation unless an explicit, documented parity question requires the alternative. Do not describe this retrospective sample as prospective confirmation.
7. **Deploy and validate only the parallel test instance.** After the above checks and test-environment identity/isolation preflight pass, deployment to `127.0.0.1:2263` is authorized. Capture the exact commit SHA, service changes, health checks, API/UI results, rollback command/procedure, and confirmation that production and Auto-Bet remain untouched.

**Stop conditions:** if rebase conflicts cannot be resolved safely, environment isolation is uncertain, source DB metadata changes, the deployment would replace `:2262`/`:80`, or any step requires production access or Auto-Bet changes, stop and report the blocker instead of improvising.

### Phase 6 — Full test and review package
- Run the complete relevant test suite, lint/build, reconciliation and manual UI checks in EC2 test.
- Produce screenshots or captured UI evidence without secrets, plus exact commands/results.
- Review diff for hidden production writes, schema changes, secret exposure, deployment hooks, Auto-Bet changes, and research-freeze violations.

**Gate 6:** once the test identity/isolation preflight passes, deploy the branch to the isolated parallel test instance (`127.0.0.1:2263`) and validate it there. This test-only deployment is authorized. Preserve the existing `:2262`/`:80` staging mirror and all production boundaries. Report exact commit SHA, commands, service/unit changes, health checks, API/UI validation, rollback procedure, and test results.

### Phase 7 — Separate production decision
Production promotion remains NOT AUTHORIZED. Only after Warren reviews the test evidence and explicitly authorizes production promotion in a separate instruction may a production release plan be prepared. That approval must specify scope, approved commit SHA, migration/backfill policy, backup/rollback, maintenance window, validation, and stop/rollback criteria. This authorization covers the isolated EC2 test deployment only and must never be interpreted as production approval.

## 10. Research freeze and prospective confirmation

Read and preserve `docs/PLAN-005-RESEARCH-GATES-AND-PROSPECTIVE-CONFIRMATION.md` and related repo rules. This refactor must not:
- unfreeze or alter a frozen research hypothesis/specification;
- use future data to qualify past alerts;
- claim prospective confirmation from retrospective rescore;
- silently change live alert behavior;
- declare production betting authorized;
- tune thresholds against the same historical sample and present that as independent confirmation.

Historical rescore is descriptive and must be labeled retrospective. Prospective confirmation requires its own frozen start time, collection protocol, sample-size/decision criteria, and separate review.

## 11. Required agent final report

At completion in test, report:
1. Environment identity and isolation evidence (redacted).
2. Branch and exact commit SHA(s).
3. Files changed and architecture decisions.
4. Cohort spec/version and precedence behavior.
5. Data sources, actual-firing vs checkpoint classification, field coverage and missing-data handling.
6. Historical stage counts and reconciliation, cohort denominators, outcomes, Wilson intervals, market coverage, exclusions, and limitations.
7. Exact test commands, exit statuses, pass/fail counts and remaining failures.
8. UI evidence and API examples with secrets removed.
9. Confirmation that production was untouched and Auto-Bet configuration/execution was unchanged.
10. Outstanding risks, blockers, and a proposed production promotion checklist (not executed).

**Definition of done:** test-only implementation demonstrably satisfies the spec, historical lineage is auditable, all required tests and reconciliation are reported, the authorized isolated EC2 test deployment is validated and rollback is documented, research/Auto-Bet/production boundaries remain intact, and Warren has a reviewable diff. No production deployment is part of this task.
