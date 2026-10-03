# Auto-Bet Safety Contract — R2.00, Unit Size, and the Three Execution Modes

Retrieval keywords: R2.00, R2, real-money test, unit size, production stake, ZERO_STAKE,
REAL_MONEY_TEST, PRODUCTION_AUTO_BET, execution mode, authorized test stake, currency ZAR,
stake invariant, fail closed, no unit size, maximum vs exact, authorization proof.

> ## ⚠️ R2.00 REAL-MONEY TEST — HARD SAFETY REQUIREMENT
>
> The only authorized real-money test stake is **exactly ZAR 2.00 (R2.00)**.
> No other real-money test stake is authorized.
>
> **R2.00 is the authorized real-money TEST stake ONLY. It is NOT the production
> stake.** The production stake is the user-configured **BLM Unit Size**.

This is a first-class repository requirement, enforced at the execution boundary
(not only in the UI).

---

## 1. Three execution modes — exactly one stake each

| Mode | Permitted stake | Notes |
|---|---|---|
| `ZERO_STAKE` | **R0.00** | simulation only; never real money |
| `REAL_MONEY_TEST` | **exactly R2.00 ZAR** | the ONLY authorized real-money test; a separate, explicitly-authorized gate |
| `PRODUCTION_AUTO_BET` | **exactly the user-configured BLM Unit Size** | never R2.00; never derived from bankroll |

Source of truth: `blm_v4/betting/stake.py` (`MODES`, `resolve_stake`,
`is_authorized_test_stake`, `validate_unit_size`). This module is the SINGLE
authority for what stake a mode permits; nothing else may re-derive it.

**Maximum vs exact (separately checkable).**
`REAL_MONEY_TEST_STAKE == REAL_MONEY_TEST_MAX_STAKE == 2.00`. An amount **above**
the maximum is rejected, and an amount **below** it is ALSO rejected — the test
stake is EXACT, not a ceiling. `is_authorized_test_stake(amount, currency)` is
True ONLY for exactly `2.00` ZAR.

**Explicit authorization proof.** `authorization_proof(mode, authorized,
authorized_by)` — both real-money modes require `authorized is True` AND a
non-empty actor. `ZERO_STAKE` needs none. A command carries this proof so an
audit can reconstruct WHY it was allowed.

---

## 2. Production unit size

The production stake is a **Unit Size** the operator saves in the BLM UI:

```
Unit Size: [ R________ ]   [Save]      (currency ZAR, active value shown)
```

Rules the execution layer enforces:

- Production stake = the configured unit size, used **verbatim** (no silent rounding).
- **Never** hard-coded; **never** R2.00; **never** derived from bankroll; **never**
  auto-changed; **never** a hidden default when none is configured.

If there is no valid unit size → **FAIL CLOSED → DO NOT BET**:

```
no unit size / zero / negative / non-finite / wrong currency  ->  NO BET
```

Persistence (minimum): `unit_size`, `currency`, `updated_at`, `updated_by`.
Changing the unit size must **not** silently alter an already-issued command —
each command carries the resolved stake it used, while the configured value
remains the source of truth for new production bets.

---

## 3. Execution boundary validation

```
alert → eligibility → frozen trigger line → current market validation
      → rung validation → retrieve configured unit size → validate unit size
      → execute EXACTLY that stake
```

The execution layer must independently verify that the stake sent to PokerBet
equals the configured production unit size (in `REAL_MONEY_TEST`, exactly R2.00).
The UI is not the only protection.

---

## 4. Audit record (every production bet)

`execution_id · alert_id · game_id · timestamp · unit_size · currency ·
trigger_checkpoint_percent · trigger_line · current_line · rung_size ·
rungs_moved · execution_decision · PokerBet reference/bet ID · final status`

This reconstructs: which alert caused the bet, when it triggered, the line that
triggered it, the fingerprints present, the line at execution, how many rungs it
moved, and why it was accepted or rejected.

---

## 5. The R2.00 test gate is separate

The R2.00 real-money test is **not** performed automatically as part of — and
**not** implied by — code cleanup, rung integration, production wiring, zero-stake
testing, reconciliation, or deployment. It requires its own explicit
authorization.

---

## 6. Priority hierarchy (unambiguous)

1. Safety / execution authorization
2. R2.00 exact real-money test constraint
3. Alert Monitor as the betting-opportunity source
4. Frozen alert trigger line
5. Rung validation
6. Execution

---

## 7. Tests

`tests/test_autobet_stake_and_unit_size_2026_10_03.py` pins: R2.00 ZAR ALLOW (when
authorized); R1.00 / R2.01 / R5.00 / R10.00 / wrong-currency / missing → REJECT;
missing / zero / negative / non-finite unit size → REJECT; PRODUCTION does not
default to R2.00; unknown mode → REJECT; authorization-proof semantics.
