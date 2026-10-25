# 14 — The Traded Cohort

Retrieval keywords: traded cohort, tradeable cohort, auto-bet cohort, what auto-bet
takes, bettable vs traded, fired-alert cohort, alert population, execution gate, armed,
exec eligible, one bet per game, why a game was not bet, traded vs alerted, cohort pull,
"my results", cohort list, cohort format, win-rate list.

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

## 14.5 Data validity — a class change INVALIDATES every earlier comparison

**A change to the execution class makes all prior data a DIFFERENT population.**
Never pool, trend or compare across a class change, and never quote a rate from
before it as if it described the current configuration.

- The class is set by the gates in 14.1 — principally `EXEC_MIN_PACE_RATIO`, with the
  window, the score floor and the price floors. Changing ANY of them re-defines who is
  in the traded cohort, so rows either side of the change are not one population.
- **Flag any dataset that spans a class change as UNRELIABLE for judging the current
  class.** Say which side of the change a number comes from; a pooled figure is not
  evidence about either.
- **Measured example (2026-10-09).** While the relaxed 0.95 execution bar was live on
  08 Oct the traded cohort ran **53.3 %**, against **75.9 / 83.8 / 73.8 %** on the
  adjacent days. Pooling the window would have averaged that collapse away and hidden
  precisely the finding that the relaxation was the problem.
- **The valid baseline is the rows AFTER the change took effect** — the moment the new
  class went LIVE (the service restart that picked it up), never the commit time and
  never the start of the next session. For the 2026-10-09 revert to 1.04 that is
  **02:43 SAST 2026-10-09** (committed 02:42, server restarted 02:43:02). An
  operator's recollection of the hour is not the cut — read it off the deploy.
- Whenever a rate is quoted, state the window, the class, and the time that class took
  effect. "Last week" is not a window unless the class held for the whole of it.

## 14.6 The TRADEABLE cohort — the pull (definition and mandated delivery format)

`14.1` defines the **traded** cohort — what auto-bet actually TAKES (the engine's
verdict, recorded in `bet_executions`). The **tradeable** cohort is a different set: the
RULE-TRIGGERED cohort — every game the theory says to trade, reconstructed from the live
observation series. When the operator says "pull my tradeable cohort" for a window, this
section IS the set and the format.

### The definition (one row per game)

A game enters **once**, at the **FIRST** observation where EVERY execution gate holds:

```
progress_pct in [75, 92]                             EXEC_MIN/MAX_PROGRESS_PCT
AND total_points >= 70                               MIN_SCORED_POINTS
AND required_pts_per_min / league_avg_pace > 1.04    EXEC_MIN_PACE_RATIO
AND under_price >= min_price_for(progress)           bands: <80 -> 1.48,
                                                            80-85 -> 1.39,
                                                            85-92 -> 1.28
```

- **Settlement is NOT an entry criterion.** A triggered game whose final is not yet
  written BELONGS in the set, marked `PENDING`. Never hand a settled-only list as "the
  cohort" — that silently drops the newest trades, which are exactly the ones that just
  fired, and the operator reads it as a data gap.
- **Dedupe — one row per game, most recent first.** This is the "watch and take" rule
  the live engine runs. It is NOT the older "single row closest to 75%, gate only that
  row" construction, which UNDER-counts (measured 57-58 vs the engine's 139).

### The four populations — pick the right one

| Set | What it is | Source |
|---|---|---|
| **Tradeable cohort** (this pull) | the gates SELECT (settled + PENDING) — a reconstruction | `clean_observations` (+ `clean_games`, `game_results`) |
| Traded cohort (`14.1`) | what auto-bet TAKES / PLACED | `bet_executions` + the engine verdict |
| Fired-alert cohort (§1.11) | every alert that FIRED | `clean_projections` |
| Bettable | `signal IN ('UNDER_VALUE','OVER_VALUE') AND checkpoint_pct < 100` (broader) | `checkpoint_market` |

The tradeable cohort is a THEORETICAL set. Pair it with the realised ledger; never
present the cohort rate as P&L, and never present failed attempts as positions.

### Where each column lives (do not guess)

- `clean_observations` (metrics): `progress_pct`, `total_points`, `required_pts_per_min`,
  `under_price`, `live_total_line`, `home_score`, `away_score`. Use
  `required_pts_per_min / avg` — it matches the module; do not recompute `(line-tot)/rem`.
- `clean_games` (metrics): `home_team`, `away_team`. **`final_home`/`final_away` are
  mostly NULL — never gate on them.**
- `game_results` (pokerbet): `final_total`, `final_result_status` (`'OK'`) — the
  **SETTLED** record. It is **NOT** authoritative for the final score: the settle
  worker sometimes writes a PREMATURE final (VERIFIED while the game was still
  live), so `final_total` can sit BELOW a total the game later reached. See the
  final-score rule in the format section below.
- `competition_pace_reference` (pokerbet): `{slug: avg_pace}` for the pace gate.
- `games` (pokerbet): `source_game_id -> competition_slug`. Build the slug map ONCE.
- `clean_observations` is keyed by the BASE id — use `str(gid).split('#')[0]`. Read-only
  throughout (`mode=ro`).

### The mandated delivery format (operator's standing preference)

Full-row list, **most recent first**, then one summary line. **The score shown is
the game's FINAL score — never the trigger score.** Per row:

```
<marker> MM-DD HH:MM  home <final_home> @ away <final_away>   line=X up=Y  total=Z  VERDICT  [source_game_id] [final_status]
```

**The final-score rule (do NOT read the trigger score; do NOT blindly trust
`game_results`):**
- The score is **the game's own feed end** — the LAST `clean_observations` row for
  the game (`home_score` + `away_score`). The verdict follows it (`final_total < line`
  = WON). For `31202604` the trigger row was 57 @ 91 but the feed ended 80 @ 121, so
  the row shows 80 @ 121 and the settled `177` is ignored as premature.
- The settled `game_results.final_total` is used ONLY when it is HIGHER than the feed
  end, because the collector sometimes loses the feed early (the last observation is
  mid-game — e.g. a game whose feed stopped at 49.5% / 63 @ 52 but really ended
  111 @ 92). Two failure modes, one rule: **final = the higher of the feed end and
  the settled total** (they are equal on the sound games).
- Print `feed=<X> settled=<Y>` on any row where the two disagree, so a premature
  settlement is visible rather than silent. In one 10 h window: 17 games were
  premature (feed end higher) and 7 had the feed cut early (settled higher); the
  other 50 agreed.
- A row whose game has no settled OK final is `PENDING` and is still shown with its
  feed-end score.

- `<marker>` = ✅ WON · ❌ LOST · ⏳ PENDING · ➖ PUSH. The leading marker is the ONLY
  highlight that survives the chat channel (ANSI colour does not) — keep the VERDICT word
  too, so a long list is scannable down the left edge.
- `final=Z` and `WON/LOST` are OMITTED for PENDING rows (verdict word `PENDING` only).
- `won` = the UNDER came in, i.e. `final_total < line`; `final_total > line` = LOST;
  equal = PUSH. Times are **SAST (UTC+2)**, formatted `M-D HH:MM`.

Summary line:

```
summary: N trades, WW/LL[/PP], K pending (SAST)
  modelled rate = R%   ROI@R100 flat = R<amt> on R<stake> (+X%)  [modelled: entry prices, no slippage]
```

- **ALWAYS CLEAN + COMPLETE, and INCLUDE PENDING/unsettled rows.** Apply the settled-only
  filter ONLY when the ask is explicitly for RESULTS.
- Label the R100-flat ROI **modelled** (entry prices, no execution slippage) and state the
  window (`last Nh` trailing, or the SAST calendar day).
- Print the **completeness census** with the pull: games seen -> in-band
  (`progress in [75,92]` AND `total_points>=70`) -> cohort (cleared pace + price), plus the
  rejects by reason. `pace_fail` is the dominant filter (expected); `price_fail` and
  `price_missing` should both be 0 — a non-zero `price_missing` means the fail-closed price
  gate is clipping the set, so say the pull is INCOMPLETE.
- Colour requests render in a FILE, never inline (INLINE ANSI prints literal `^[[32m`
  noise or is stripped): write a `colorize_cohort.py` copy (WON bold green, LOST red,
  PENDING yellow, PUSH cyan) plus its `.html` twin and hand over the paths.

### Window semantics (state the window you used)

- **"Today" is a CALENDAR day, not `now - 24h`.** `scripts/cohort_day.py <YYYY-MM-DD>`
  does the SAST day; a trailing `Nh` pull is a different slice. Asked for "today" within
  minutes after local midnight, "today" means the day that just CLOSED.
- **A naive trailing window distorts the edge.** A game that triggered BEFORE the boundary
  is reported at its first IN-window row (wrong line/price). Fix: scan a lookback with a
  buffer, keep each game's genuine FIRST qualifying row, and include only games whose
  trigger falls inside the window.

### PENDING is a settle-worker RESOLUTION failure, not lag

Keep every PENDING game in the list. Its `game_results` row usually carries a non-OK
verdict (`NEEDS_RECONCILIATION`, `INVALID`) the worker wrote but could not trust, so the
row never leaves PENDING; a few of the newest just-fired games have NO `game_results` row
at all. Confirm with `journalctl --user -u blm-server | grep settle_worker_result`.

**Verify.** Re-run the pull and confirm the cohort `n` and the WON/LOST/PENDING counts
match the row list, and that the census reads `price_missing = 0`.

