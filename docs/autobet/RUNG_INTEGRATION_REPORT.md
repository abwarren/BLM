# RUNG INTEGRATION REPORT

Retrieval keywords: rung integration report, canonical function, production call path,
files changed, tests executed, fail-closed, manual autonomous equivalence,
trigger immutability, wired_into_production.

Date: 2026-10-03 · Branch: `handoff-2026-09-07` · Environment: READ-ONLY production

---

## 1. Production call path

```
PokerBet live market
  → observed trigger line        under_alert.trigger_line  (FROZEN; from
                                  under_outcome.trigger_observation — ADR-001)
  → current line                 market.total_line         (fresh live read)
  → observed rung size           market.observed_lines → rung.infer_rung_size
  → canonical rung calculation   rung.rungs_moved / rung.under_rung_decision
  → UNDER eligibility            validate_execution (identity + market + selection)
  → command generation           manual_execution_command | autonomous_execution_command
  → execution validation         validate_execution (the ONE shared validator)
  → execute / reject
```

**Architecture (operator correction).** The BLM Alert Monitor is the ONLY
opportunity source. Auto-Bet CONSUMES an existing alert; it never discovers an
opportunity, never establishes a new baseline, and never creates a new trigger
line. The alert's frozen `trigger_line` is the reference for BOTH manual and
autonomous betting. There is no separate manual reference line.

## 2. Canonical function used

`blm_v4/betting/rung.py` — the single implementation:
`rungs_moved`, `under_rung_decision`, `infer_rung_size`, `validate_execution`,
`manual_execution_command`, `autonomous_execution_command`.
Stake authority: `blm_v4/betting/stake.py` (three modes + unit size + R2.00).

## 3. Files changed

| File | Change |
|---|---|
| `blm_v4/betting/rung.py` | NEW — canonical rung calculation + shared validator |
| `blm_v4/betting/stake.py` | NEW — canonical stake/mode authority (ZW-01..) |
| `tests/test_autobet_rung_wiring_2026_10_03.py` | NEW — PW-01..13, PW-15 |
| `tests/test_autobet_stake_and_unit_size_2026_10_03.py` | NEW — R2.00 + unit size + modes |
| `docs/autobet/AUTOBET_SAFETY_CONTRACT.md` | NEW — R2.00 + unit size + modes (prominent) |
| `docs/autobet/RUNG_PRODUCTION_WIRING_GATE.md` | NEW — PW gate status + evidence |
| `docs/autobet/RUNG_INTEGRATION_REPORT.md` | NEW — this report |
| `docs/rag/13_AUTOBET_RUNG.md` | updated — canonical module + architecture correction |
| `docs/rag/05_PRODUCTION_RULES.yaml` | updated — AUTOBET_STAKE block; rung module ref |
| `docs/rag/README.md` | updated — pack index |

No alert, settlement, reconciliation, result-authority or fingerprint logic was
modified. No alert-monitor eligibility/fingerprint/trigger/settlement rule changed.

## 4. Tests executed

```
BLM_ALLOW_HEAVY_TESTS=1 python3 -m pytest \
  tests/test_autobet_rung_rule_2026_10_03.py \
  tests/test_autobet_stake_and_unit_size_2026_10_03.py \
  tests/test_autobet_rung_wiring_2026_10_03.py -q
→ 91 passed
```

## 5. Results — gate by gate

See `RUNG_PRODUCTION_WIRING_GATE.md`. Summary: **12 PASS, 3 PARTIAL**
(PW-02, PW-03, PW-14 blocked by the absent browser extension / live engine
window). PW-15 (real-money separation) PASS; the R2.00 test was NOT performed.

## 6. Fail-closed cases (all PASS)

rung size missing / zero / negative / non-finite / inconsistent increments;
current line unavailable; trigger line unavailable; game identity ambiguous;
market identity ambiguous; selection ambiguous; movement not representable as
valid rungs. Each returns `REJECT` with a stable reason
(`rung_market_missing` / `rung_size_ambiguous` / `rung_identity_unverified` /
`more_than_one_rung_down`).

## 7. Evidence — manual & autonomous share ONE calculation

`manual_execution_command` and `autonomous_execution_command` both delegate to
`validate_execution`, which calls `under_rung_decision`. `test_pw13_*` asserts
`manual == autonomous == validate_execution(game)`; `test_pw02/03_*` monkey-patch
`under_rung_decision` and assert exactly one invocation per command. The ONLY
difference between the paths is the command producer (UI vs engine).

## 8. Evidence — trigger line immutable

`test_pw05_*` mutate the live market (line and full observed series) and assert
the trigger used stays equal to the frozen alert line. The validator reads
`under_alert.trigger_line`, never a recomputed DOM value; a re-render does not
reset it, and a reacquisition reuses the original trigger (PW-11).

## 9. Confirmation — trigger line remains immutable

CONFIRMED (in-process): PW-05 and PW-11 pass. The frozen alert trigger is the
sole reference line for both producers.

## 10. Real-money separation

The R2.00 ZAR real-money test was NOT performed. That remains a separate,
explicitly-authorized gate (see `AUTOBET_SAFETY_CONTRACT.md` §5). No provider was
constructed; no submission occurred.

## 11. Not-wired status

`wired_into_production` remains **false**. Every gate must pass; PW-02, PW-03 and
PW-14 cannot be fully proven in this environment (no loadable browser extension;
no authenticated live PokerBet engine window). The exact failed gates are named in
`RUNG_PRODUCTION_WIRING_GATE.md`.
