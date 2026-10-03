# 13 — Auto-Bet: the PokerBet RUNG rule (line-increment guard)

Retrieval keywords: rung, rung size, rungs moved, line increment, line movement,
tick size, auto-bet gate, under execution, manual vs autonomous, trigger line immutable,
DOM re-render, fail closed, current line vs trigger line, -1 rung, execution rule,
PokerBet market increment, 0.5 not universal.

**What this pack answers:** when the live market line has moved away from the frozen
trigger line, may an UNDER bet still execute? The answer is expressed in **rungs**,
never in raw points.

---

## Rule 13.1 — RUNG

**Rule.** A **rung** is ONE valid line increment in the currently observed PokerBet
market. The rung size must be established from the actual market; it must **not** be
globally hard-coded.

**Source.** Operator directive (2026-10-03). No `rung` concept exists anywhere in the
current codebase (verified by a full-tree search); this is a NEW canonical rule, and
this pack is its authoritative definition. The integration point in production is
`blm_v4/betting/executor.py::evaluate` (see Rule 13.7).

**Why.** PokerBet line ticks differ by market (e.g. 0.5, 1.0, 0.25). A rule written
against a fixed point offset (`-1.0`) is wrong for any other tick.

**Valid.** Rung = 0.5 for a market whose observed lines step 205.5 → 205.0 → 204.5.

**Invalid.** Assuming every market uses 0.5.

**Edge.** A market observed at fewer than two distinct increments is **ambiguous** and
must fail closed (Rule 13.4).

**Forbidden.** Hard-coding a global rung size; expressing the rule as a point
difference.

**Verify.** `infer_rung_size(observed_lines)`; see Rule 13.4.

---

## Rule 13.2 — CALCULATION

```
rungs_moved = (current_line - trigger_line) / rung_size
```

- `current_line` — the CURRENT observed live market total
  (`blm_v4/betting/executor.py`: `market["total_line"]`).
- `trigger_line` — the FROZEN, immutable trigger line
  (`blm_v4/betting/executor.py`: `ua["trigger_line"]`, sourced from
  `blm_v4/live_analytics/under_outcome.py::trigger_observation`).
- `rung_size` — the observed market increment (Rule 13.1/13.4).

`rungs_moved` is a SIGNED COUNT OF RUNGS (down = negative). It is **not** a point
difference.

**Valid.** current 204.5, trigger 205.5, rung 0.5 → `(204.5-205.5)/0.5 = -2` rungs.

**Invalid.** `current_line - trigger_line = -1.0` used as the threshold.

**Forbidden.** Any comparison in raw points.

---

## Rule 13.3 — UNDER EXECUTION RULE

```
rungs_moved >= -1  →  ALLOW
rungs_moved <  -1  →  REJECT
```

| Condition | rungs_moved | Decision |
|---|---|---|
| Upward movement | any `> 0` | **ALLOW** (unrestricted) |
| Zero movement | `0` | **ALLOW** |
| One rung down | `-1` (exactly) | **ALLOW** |
| More than one rung down | `< -1` | **REJECT** |

The boundary is INCLUSIVE at `-1`: exactly one rung downward is allowed; more than one
is rejected.

**Why.** For an UNDER bet a higher line is favourable and a lower line is
unfavourable; a large downward move means the entry the alert identified is gone.

**Valid.** `rungs_moved = -1` → ALLOW. `rungs_moved = -1.0001` → REJECT.

**Edge.** A float tolerance must be applied so an intended exact `-1` is not rejected
by binary rounding (use a small epsilon, e.g. `1e-9`).

**Forbidden.** Rejecting at exactly `-1`; allowing at `< -1`.

**Verify.** `under_rung_decision(current_line, trigger_line, rung_size)` (Rule 13.5).

---

## Rule 13.4 — Rung size is DERIVED from the live market, and fails closed

**Rule.** The extension must establish the actual rung size from the live PokerBet
market — never a global constant. When the size cannot be proven, the decision is
**REJECT** (fail closed).

**Derivation (canonical).** From the observed line series:

```
distinct = sorted(set(observed_lines))            # drop None / duplicates
if len(distinct) < 3:            return None       # <2 increments → AMBIGUOUS
steps    = [distinct[i+1]-distinct[i] for i]      # positive increments only
if len(steps) < 2:               return None       # AMBIGUOUS
tick     = min(steps)
if tick <= 0:                    return None
for s in steps:                                    # every increment must be an
    if s is not (integer multiple of tick):        # exact multiple of the tick
        return None                                # INCONSISTENT → AMBIGUOUS
return tick
```

**Source.** Operator directive; reference implementation in
`tests/test_autobet_rung_rule_2026_10_03.py::infer_rung_size`.

**Why.** A market's tick is the smallest consistent step its lines take; if the
observed steps are not a consistent multiple series, no single rung size is provable.

**Valid.** `{205.5, 205.0, 204.5}` → tick 0.5. `{206, 205, 204, 202}` → tick 1.0.

**Invalid / ambiguous (→ None → REJECT).** `{205.5, 208.0}` (one increment);
`{205.5, 208.0, 210.0}` (steps 2.5 and 2.0 — not consistent multiples); `{}`.

**Edge.** A rung_size of `None`, `0`, negative or non-finite from ANY source
(config, DOM, feed) is unprovable → REJECT.

**Forbidden.** Defaulting a missing rung size to 0.5 (or any constant); deriving a
rung from a single increment.

**Verify.** `infer_rung_size(...)` returns `None` for every ambiguous case and the
correct tick for a consistent series.

---

## Rule 13.5 — The decision function (canonical, pure)

**Rule.** The canonical, pure decision is:

```
under_rung_decision(current_line, trigger_line, rung_size) -> {decision, reason, rungs_moved}

  cur, trig, rung must all be finite; rung must be > 0
  else                              -> REJECT, reason "rung_ambiguous"
  moved = (cur - trig) / rung
  if not finite(moved)              -> REJECT, reason "rung_ambiguous"
  if moved >= -1 - EPS              -> ALLOW,  reason "within_one_rung_down"
  else                              -> REJECT, reason "more_than_one_rung_down"
```

**Source.** Reference implementation in
`tests/test_autobet_rung_rule_2026_10_03.py`.

**Why.** One pure function, one decision — the same calculation everywhere.

**Forbidden.** A second, divergent rung calculation in any surface.

**Verify.** The automated cases in Rule 13.6.

---

## Rule 13.6 — Automated test cases (required)

**Rule.** The following must be automated and passing (one named test per case):

| Case | current vs trigger | rung | rungs_moved | Expected |
|---|---|---|---|---|
| 0 rungs | equal | any valid | `0` | **ALLOW** |
| +1 rung | `+1 × rung` | valid | `+1` | **ALLOW** |
| +multiple rungs | `+3 × rung` | valid | `+3` | **ALLOW** |
| −1 rung | `-1 × rung` | valid | `-1` | **ALLOW** |
| −2 rungs | `-2 × rung` | valid | `-2` | **REJECT** |
| invalid/ambiguous rung size | any | `None`/`0`/`<0`/`inf`/non-consistent | `None` | **REJECT** |

Plus two requirement-pins:
- **expressed in rungs, not points** — the SAME point difference yields DIFFERENT
  decisions under different rung sizes (proves no fixed-point threshold).
- **manual == autonomous** — the manual and autonomous wrappers delegate to the ONE
  canonical calculation and return identical decisions for identical inputs.

**Source.** `tests/test_autobet_rung_rule_2026_10_03.py`.

---

## Rule 13.7 — Integration point (documented; NOT yet wired)

**Rule.** The rule belongs as a gate in `blm_v4/betting/executor.py::evaluate`, in the
market-information block. The two operands already exist there:

```
line     = _finite(market.get("total_line"))    # executor.py:111  (current line)
trig_line = _finite(ua.get("trigger_line"))     # executor.py:112  (frozen trigger)
```

`trig_line` is currently computed but **used only in the recorded candidate** (as
`triggered_line`), NOT in any gate. A rung gate would compare `line` vs `trig_line`
via `under_rung_decision(...)` and refuse (`NO_BET`, reason `rung_moved_too_far`) when
the decision is REJECT — placed AFTER `market_missing` and BEFORE the stake/limit
math, preserving the executor's cheap-first ordering.

**Status.** NOT implemented. Wiring is a PRODUCTION BEHAVIOUR CHANGE (it can newly
block bets) and requires its own explicit authorization. This pack and its tests only
specify and pin the rule.

**Forbidden.** Wiring the gate into the executor as part of a documentation task.

---

## Rule 13.8 — Explicit statements the RAG must hold

The RAG must explicitly state, and never soften, all of the following:

1. `0.5` is an **example**, NOT a universal rung size.
2. The extension **must establish the actual rung size from the live PokerBet
   market** (Rule 13.4).
3. The **trigger line is immutable** (ADR-001; `under_outcome.trigger_observation`).
4. **DOM re-render / reacquisition must not reset the trigger line** — the trigger
   line is read from the frozen alert value (`ua["trigger_line"]`), never recomputed
   from a fresh DOM render of the current market.
5. **Ambiguous or unprovable rung size must fail closed** → REJECT (Rule 13.4).
6. The **same rung calculation must be used for manual and autonomous execution**
   (one canonical function, Rule 13.5).

**Source.** Operator directive (2026-10-03); ADR-001 (frozen trigger line).

**Forbidden.** Re-anchoring the trigger line to the current DOM; a second rung
implementation for the manual path.

---

## Worked examples

Rung = 0.5 (example only), trigger = 205.5:

```
current 205.5 → (205.5-205.5)/0.5 =  0     → ALLOW   (zero movement)
current 206.0 → +0.5/0.5          = +1     → ALLOW   (one rung up)
current 207.0 → +1.5/0.5          = +3     → ALLOW   (three rungs up; unrestricted)
current 205.0 → -0.5/0.5          = -1     → ALLOW   (exactly one rung down)
current 204.5 → -1.0/0.5          = -2     → REJECT  (two rungs down)
rung ambiguous (None)                       → REJECT  (fail closed)
```

Same POINT move, different rung → different decision (proves rungs, not points):

```
diff = -1.5 points
  rung 0.5 → -3.0 rungs → REJECT
  rung 2.0 → -0.75 rungs → ALLOW
```
