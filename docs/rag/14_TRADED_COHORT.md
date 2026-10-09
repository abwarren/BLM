# 14 — The Traded Cohort

Retrieval keywords: traded cohort, auto-bet cohort, what auto-bet takes, bettable vs
traded, fired-alert cohort, alert population, execution gate, armed, exec eligible,
one bet per game, why a game was not bet, traded vs alerted.

**What this pack answers:** which games count as "the traded cohort", how it differs
from the alert population and from "bettable", and why that number is NOT the number
of alerts shown on the board.

---

## 14.1 Definition

**The traded cohort is ONLY the games the system auto-bets.** It is the subset of
fired alerts that clear the execution gates — not the alert population, and not
everything the board shows an alert for.

Source of truth: `blm_v4/betting/executor.py::evaluate` (the gate chain, in this
order); values from `blm_v4/trade_window.py`.

```
traded  =  auto-betting enabled
       AND  the strict alert is active        required > league_average * 1.04
                                              AND progress_pct >= 75.0
       AND  progress_pct <= 92.0              EXEC_MAX_PROGRESS_PCT
       AND  points already scored >= 70.0     MIN_SCORED_POINTS
       AND  under price >= the band floor     75-80 -> 1.48
                                              80-85 -> 1.39    >=85 -> 1.28
       AND  the alert is fresh                age <= 90 s
       AND  eligible, live, no bet held       state age <= 300 s
       AND  within the limits                 <= 1000/bet, <= 200 bets/day
```

The normative definition is `docs/BLM_MODEL_SPEC.md` **§1.12**; this pack is the
retrieval-facing summary of it.

## 14.2 The three populations — never conflate them

| Population | Definition | Where it lives |
|---|---|---|
| **Fired-alert cohort** | every alert that FIRED (model spec §1.11) | `clean_projections`; the board's result rows |
| **Bettable** | `signal IN ('UNDER_VALUE','OVER_VALUE') AND checkpoint_pct < 100` | `checkpoint_market` |
| **Traded cohort** | the gates in 14.1 — what auto-bet actually takes | `bet_executions` + the engine's verdict |

- **"Bettable" is BROADER than the traded cohort.** It includes OVER alerts that
  auto-bet never touches. Quoting a bettable rate as the traded rate is the classic
  error, and it is why the same question returns 66 % and 84 % depending on the
  population used.
- **The fired-alert cohort is not the traded cohort either.** A fired alert that sat
  outside the 75-92 % window, under the 70-point score floor, or under the price floor
  was never traded.

## 14.3 Rules the RAG must hold

- **At `EXEC_MIN_PACE_RATIO = 1.04` the traded cohort IS the alert cohort.** The
  execution bar equals the alert's own bar, so the armed flag and the alert agree by
  construction. They diverge only when the execution bar sits BELOW 1.04 — it did for
  one day (2026-10-08, at 0.95) and that is why the two were being confused.
- **A game is spent by the ATTEMPT, not by the outcome.** It is one bet per game, so a
  refused or failed attempt permanently loses that game's position. A pre-submit
  failure is lost VOLUME, never a LOSS.
- **The engine's verdict is the only authority.** Whether a game is in the traded
  cohort is decided by `evaluate`. A view, report or query must CONSUME that verdict —
  never re-derive the gates beside it.
- **Never answer "how many did we trade" from the alert count.** Count claims in
  `bet_executions`, or apply the gates explicitly. The board shows alerts, not trades.
- **A game that armed but was never claimed is neither a trade nor a loss** — it is
  lost volume. The worker polls the live payload, so a game armed for only seconds is
  routinely missed; report that as a throughput leak, not as a strategy result.

## 14.4 The measurement trap when settling the cohort

`bet_executions` records `triggered_line` and nothing else about the line, and the
trigger line is systematically the HARDER line for an UNDER. **Never settle a strike
rate or P/L at `triggered_line`** — it manufactures losses. Use the market line
(`clean_market_observations.line_value` at or just before `submitted_at_utc`) and say
plainly that it is a PROXY. Detail: `08_MARKET_LINE_SEMANTICS.md`.
