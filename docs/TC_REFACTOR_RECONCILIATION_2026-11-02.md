# TC refactor — reconciliation, 2026-11-02

This branch (`review/tc-refactor-reconciled-2026-11-02`) exists so a reviewer can
see the two TC-refactor lines side by side without either being deployed. Nothing
here touched production, and nothing in the production prohibition section of
`TRADE_COHORT_REFACTOR_IMPLEMENTATION_STRATEGY.md` was relaxed.

## What the two lines actually are

Both started from `03b242e` and both created `blm_v4/cohorts/`:

| | origin `feature/tc-cohort-refactor-test` | EC2 line `stage/tc-ui-2026-11-02` |
|---|---|---|
| cohorts package | spec · evaluator · rescore · metrics · api | the SAME package, extended |
| `spec.py`, `metrics.py` | — | **byte-identical** to origin's |
| `evaluator.py` | 240 lines | 282 (`+46 / -4`) |
| `rescore.py` | 370 lines | 438 (`+80 / -12`) |
| `api.py` | 312 lines | 325 (`+26 / -13`) |
| dashboard | a 141-line panel (+59 html, +42 css) | full redesign: `tc.js` 509, `styles.css` +1725, `nav.js`, `login.js`, `stats.js` |
| docs / plan | the plan, directive, implementation + test-env reports | none of these |
| tests | `test_tc_cohort_{api,spec}` 126 + 280 | 149 + 325 |

So this is ONE lineage, not two competing implementations: the EC2 line's cohort
package is a strict superset of the origin one (identical where it did not extend),
and it carries the UI work; the origin line carries the plan documents.

## What this merge resolves

- `blm_v4/cohorts/*`, `blm_v4/dashboard/static/*`, `server.py`,
  `tests/test_tc_cohort_*` → **the EC2 line** (the superset, and what actually
  runs on the test box).
- `docs/AGENT_DIRECTIVE_TC_COHORT_REFACTOR.md`,
  `docs/AGENT_NEXT_STEPS_TC_COHORT_TEST_DEPLOYMENT.md`,
  `docs/TC_REFACTOR_IMPLEMENTATION_REPORT.md` → **origin** (the newer plan).

## Three deltas that are NOT settled here — review before promoting

These exist on the origin line and are absent from the EC2 line's rewrite. They
are *recorded*, not restored, because restoring them blindly would overwrite a
rewrite someone made deliberately.

1. **`evaluator.py` — scored-points validation.** Origin reads
   `inputs["scored_points"]`, validates it with `_finite()`, and returns
   `R_MISSING_POINTS` / `R_INVALID_POINTS` when unusable. The EC2 rewrite handles
   its inputs differently; confirm that a missing/invalid point total still comes
   out UNKNOWN rather than passing.
2. **`rescore.py` — the PRIOR-ONLY league reference.** Origin's `_series()` +
   `_prior_mean()` build a league average from captures strictly at or before each
   row's own timestamp (`bisect_right`, `league_source="prior_only"`). That is the
   no-lookahead property the strategy document makes a release gate
   (§8.2 temporal-firewall tests). Confirm the EC2 rescore keeps it.
3. **`api.py` — per-cohort market coverage.** Origin computes
   `market_coverage` / `market_coverage_pct` per cohort (rows with both an under
   price and a trigger line, over all qualifying rows). Absent from the EC2 API.

## Evidence collected on this branch

```
cohort imports clean
tests/test_tc_cohort_api_2026_10_26.py + test_tc_cohort_spec_2026_10_26.py
    117 passed
tests/test_dashboard*.py + tests/test_execution_window*.py
    155 passed, 1 failed
    the failure (test_dashboard_audio_alert :: no_over_and_no_banned_vocabulary)
    fails IDENTICALLY on stage/tc without this merge — pre-existing on that line,
    captured as a baseline rather than counted as a regression
```

## What a reviewer has to decide

1. Which cohort package becomes canonical for promotion — the EC2 line's superset
   (recommended: it is what the test deployment runs and it is a superset), or the
   origin line (which has the three deltas above).
2. Whether the three deltas are regressions to restore or deliberate removals —
   item 2 in particular is a no-lookahead release gate, not a nicety.
3. The UI: the EC2 `tc.js` is the operator-facing work; it must be checked against
   whichever engine is chosen, because its API expectations were written against
   the superset.
