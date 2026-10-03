# Auto-Bet Rung — Production Wiring Gate (PW-01 .. PW-16)

Retrieval keywords: production wiring gate, PWG, PW-01, PW-02, canonical implementation,
manual path, autonomous path, pre-execution validation, immutable trigger, live rung size,
fail closed, fresh market state, movement rule, adversarial, DOM reacquisition,
idempotency, equivalence, zero stake, real-money separation, evidence.

**Purpose.** Prove the verified rung rule actually **controls** production execution —
not merely that it exists somewhere in the codebase.

**Canonical implementation.** `blm_v4/betting/rung.py`
(`rungs_moved`, `under_rung_decision`, `infer_rung_size`, `validate_execution`,
`manual_execution_command`, `autonomous_execution_command`). One implementation;
no producer re-implements the formula (PW-01 is an AST/source assertion).

**Test suite.** `tests/test_autobet_rung_wiring_2026_10_03.py`
(+ `tests/test_autobet_rung_rule_2026_10_03.py`,
`tests/test_autobet_stake_and_unit_size_2026_10_03.py`).

---

## Status

| Gate | Requirement | Status | Evidence / blocker |
|---|---|---|---|
| PW-01 | exactly one canonical implementation + decision | **PASS** | `test_pw01_*` (only `rung.py` defines `rungs_moved`/`under_rung_decision`; no duplicated formula/boundary) |
| PW-02 | manual path calls the canonical function | **PARTIAL** | `test_pw02_manual_calls_canonical` proves `manual_execution_command` → `validate_execution` → `under_rung_decision`. The **UI → PokerBet-extension** hop is NOT provable here: no browser extension exists in-repo |
| PW-03 | autonomous path calls the same function | **PARTIAL** | `test_pw03_autonomous_calls_canonical` proves `autonomous_execution_command` shares the validator. The **live engine against a real PokerBet window** is NOT exercisable in this environment |
| PW-04 | independent pre-execution validation (identity/market/selection/trigger/current/rung/movement) | **PASS** | `test_pw04_*` |
| PW-05 | immutable trigger line (rerender/disappearance/reacquisition) | **PASS** | `test_pw05_*` |
| PW-06 | live rung size (never hard-coded 0.5) | **PASS** | `test_pw06_*` |
| PW-07 | fail closed (7 cases) | **PASS** | `test_pw07_*` |
| PW-08 | fresh market state before execution | **PASS** | `test_pw08_stale_current_line_rejects` |
| PW-09 | movement rule truth table | **PASS** | `test_pw09_movement_rule` |
| PW-10 | adversarial: fixed-point rule disagrees with rung rule | **PASS** | `test_pw10_*` |
| PW-11 | DOM reacquisition retains the trigger | **PASS** | `test_pw11_*` |
| PW-12 | idempotency (same command id once) | **PASS** | `test_pw12_idempotent_claim_once` |
| PW-13 | manual/autonomous equivalence | **PASS** | `test_pw13_manual_equals_autonomous` |
| PW-14 | zero-stake full production wiring path | **PARTIAL** | in-process BLM→command→validation→execution-attempt proven; the **extension** hop is blocked (same as PW-02) |
| PW-15 | real-money separation (no real bet) | **PASS** | `test_pw15_no_real_money_execution_in_this_suite`; no bet placed |
| PW-16 | evidence document | **PASS** | this file + `RUNG_INTEGRATION_REPORT.md` |

---

## Failed / unprovable gates (exact)

- **PW-02, PW-14** — the "BLM UI → PokerBet **extension**" hop. `find` for an
  extension/userscript directory returns **nothing**; the PokerBet execution
  surface is a Playwright adapter (`blm_v4/execution/pokerbet/`), not a loadable
  browser extension. The extension's own independent stake/rung validation
  therefore **cannot be executed or proven** in this environment.
- **PW-03** — the **live autonomous engine** against a real PokerBet window
  requires an authenticated headless PokerBet session that is unavailable to an
  agent session. The in-process path is proven; the live-market hop is not.

Because PW-02 / PW-03 / PW-14 are not fully proven, **`wired_into_production`
remains `false`** (see `docs/rag/05_PRODUCTION_RULES.yaml`).

---

## Evidence — manual/autonomous share the SAME calculation

Both producers are thin wrappers over the one validator:

```python
def manual_execution_command(game, *, market="TOTAL", selection="UNDER"):
    return validate_execution(game, market=market, selection=selection)

def autonomous_execution_command(game, *, market="TOTAL", selection="UNDER"):
    return validate_execution(game, market=market, selection=selection)
```

`test_pw13_manual_equals_autonomous` asserts `manual == autonomous ==
validate_execution(game)` for every probe. `test_pw02/03_*_calls_canonical` monkey-
patch `under_rung_decision` and assert it is invoked exactly once per command.

## Evidence — trigger line immutability (PW-05)

`test_pw05_trigger_line_immutable_across_market_moves` and
`test_pw05_rerender_does_not_reset_trigger` change `market.total_line` and
`market.observed_lines` (a fresh series) and assert the trigger used
(`validate_execution(...)["trigger_line"]`) stays `48.5`. The validator reads the
FROZEN alert line (`under_alert.trigger_line`), never a re-derived DOM value.

## Evidence — no real money (PW-15)

No provider is constructed and no submission occurs in the suite. The production
mode with no configured unit size resolves to `REJECT` (never R2.00). The R2.00
real-money test was **not** performed.
