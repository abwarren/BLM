# 15 — Edge findings & gate optimisation (2026-10-12)

Retrieval keywords: win rate, edge, gate optimisation, stricter gate, fingerprints,
C2, pacegap, pace_difference, required vs actual pace, under_price floor, EV,
premature settlement, R1 retired, don't resurrect, class change.

**What this pack answers:** whether adding fingerprint/derived conditions to the
Auto-Bet gate raises the win rate, which candidates were measured, and which were
REJECTED. Every number is measured read-only on the CORRECTED final (see §15.1).

> ⚠️ **READ §15.1 FIRST.** All the analysis below is on `max(game's own feed end,
> settled OK total)` — NOT the settled final. A study on settled finals is
> contaminated and reads ~5 points too high.

---

## 15.1 The prerequisite: corrected finals, or the study is wrong

The settle worker sometimes writes a PREMATURE final (VERIFIED while the game is
still live), so `game_results.final_total` can sit BELOW a total the game later
reached. Over 21 days, 1,856/15,577 OK finals (11.9%) were written before the
game's last observation; it only ever INFLATES the UNDER rate (a lower total
favours the UNDER). Any win-rate or fingerprint study MUST use the corrected
final, and MUST say the class (the gates re-define the cohort).

---

## 15.2 Candidates measured, and the verdict

Population: the tradeable cohort (first qualifying row). Outcome: corrected final.
Baseline over 14d = 64.4%; over the archive (~37d, n=4,870) = 68.0%.

| candidate | short window (14d) | archive / out-of-time | verdict |
|---|---|---|---|
| **C2** (`recent_pace_3m − actual <= −0.5`) | TRUE 63.6% vs base 64.4% | sign opposite to intuition | **NO EDGE — do not gate** |
| **`req_ratio >= 1.20`** | 88.9% (n=36) | 65.8% (n=453) — BELOW base | **FLUKE — matches retired R1** |
| **`pacegap < −1.0`** | 85.4% (n=41) | 69.4% (n=569) | **FLUKE — do not gate** |
| **`pacegap < 0`** (actual < required) | 68.5% (n=387) | 73.0% (n=2355) | **HOLDS — the signal** |
| `req_ratio >= 1.10` | 70.3% | 72.6% (n=1272) | holds, weaker |
| lateness (`progress >= 85`) | 73.3% | 77.1% (n=946) | holds but largely UNPLACEABLE |

**The one coherent mechanism:** the UNDER wins when the game is UNDER-shooting the
required rate. `pacegap = actual_pts_per_min − required_pts_per_min` (see
`clean_metrics.py`: `pace_difference = actual - required`) is monotone across five
buckets over 14d: `<−1.0 85.4% · −1..0 66.8% · 0..0.5 66.0% · 0.5..1.0 60.1% ·
>1.0 56.6%`. C2 (momentum) is the same idea on the wrong input and does NOT work.

---

## 15.3 The decision implemented (operator directive 2026-10-12)

| rule | action |
|---|---|
| `pacegap < 0` AND `under_price >= 1.85` | **ADDED — the primary UNDER eligibility rule** |
| `pacegap < 0` AND `progress >= 82` | secondary candidate — evaluated, NOT enforced |
| `pacegap < 0` AND `progress >= 85` | separate, more restrictive — evaluated, NOT enforced |
| `req_ratio >= 1.20` | NOT used (reproduces the retired R1 result) |
| `pacegap < -1.0` | NOT used (a short-window fluke) |
| C2 | stays retired |

Implementation — TWO execution legs, ONE served verdict:
- LEG 1 (signal) `trade_window.exec_shortfall_reason`: the game must be at or BELOW
  its required rate (`actual_pts_per_min < required_pts_per_min`).
- LEG 2 (price) `trade_window.EXEC_MIN_UNDER_PRICE = 1.85`, applied via `max()` in
  `min_price_for` — the HIGHER of the operator minimum and the per-band break-even,
  never the looser of the two.
- Both fold into `under_alert_exec_armed` in `api.py` (the side that holds the
  league reference and the projector row); the executor CONSUMES the served boolean
  and re-derives nothing.

**CONTRACT CHANGE (deliberate; the fixtures moved in the SAME change):** the price
floor was the per-band break-even (1.48 / 1.39 / 1.28) and is now the flat 1.85
minimum, because 1.85 dominates every band. `tests/test_execution_window_2026_10_07.py`
asserted the per-band numbers and was updated deliberately — as the repo-change
invariants require when the guarded population changes.

**Not enforced (evaluated only):** the lateness variants (progress >= 82 / >= 85);
the enforced window stays [75, 92].

## 15.4 DO NOT resurrect

`req_ratio >= 1.20` / the `[1.20,1.35)` band (retired as R1); C4/C6; the trap
signals as gates (`10_TRAP_METER.md` §C). The 88.9% seen in one 14-day window is
exactly the trap that got R1 through the first time.

## 15.5 Caveats carried with this decision

1. Out-of-time evidence is a 30% time-slice (n≈40–150 for the tight combos); the
   ranking is stronger than the exact EV.
2. Absolute EV figures are NOT P&L — entry prices, no execution slippage, which
   historically halved this system's realised edge.
3. The price regime is not constant: avg win price ran 2.06–2.17 in W36–37 and
   1.75–1.79 from W38 on. Archive EV is inflated by the early weeks.
4. Changing the gate is a CLASS CHANGE — every earlier comparison is invalidated
   (`14_TRADED_COHORT.md` §14.5).

---

## 15.6 The LIVE TRADE COMBO — second revision (operator directive 2026-10-12)

**Supersedes §15.3 for the TRADE gate** (the alert gate is untouched — see below).
After §15.3 was landed, a corrected-cohort sweep over ~4,500 candidate combos
found a stricter footprint that lifts the win rate materially. The Auto-Bet gate
now requires ALL of:

| leg | value | constant |
|---|---|---|
| progress band | `78% <= progress <= 90%` (both inclusive) | `TRADE_MIN_PROGRESS_PCT` / `TRADE_MAX_PROGRESS_PCT` |
| pace ratio | `required_pts_per_min / competition_avg_pace` **> 1.12** (STRICT) | `TRADE_MIN_PACE_RATIO` |
| scored points | `current_total_points >= 90` | `TRADE_MIN_SCORED_POINTS` |
| shortfall margin | `required_pts_per_min - actual_pts_per_min >= 0.25` | `SHORTFALL_MARGIN` |
| under price | `under_price >= 1.85` | `EXEC_MIN_UNDER_PRICE` (via `min_price_for`) |

### The evidence (corrected cohort, 40 days, in-sample)

| combo | W/L | win % | n | bets/day |
|---|---|---|---|---|
| **price>=1.85, pace>1.12, pts>=90, shortfall>=0.25, band 78-90** — the gate | 782/193 | **80.2%** | 975 | 24.4 |
| price>=2.0, pace>1.08, pts>=70, shortfall>=0, band 78-90 | — | 80.0% | — | 24.4 |
| price>=1.85, pace>1.12, shortfall>=0.5, band 78-90 | — | 81.2% | — | 22.4 |

**The progress BAND narrowing 75-92 -> 78-90 is the dominant lever.**  Tightening
the shortfall margin 0.25 -> 0.5 buys +1.0 pp for ~2 fewer bets/day.

### Caveats (carry these with the number)

1. It is an **in-sample maximum over ~4,500 combos** — a real overfitting risk;
   the ranking is stronger evidence than the exact rate.
2. **95% CI ~= 77.7-82.5%** at n=975.
3. **Modelled entry prices, no slippage** — not P&L (see §15.5.2).
4. Changing the gate is a **CLASS CHANGE** (`14_TRADED_COHORT.md` §14.5).

### Implementation — the ONE definition, and what did NOT move

- `blm_v4/trade_window.py` gains the `TRADE_*` constants plus `SHORTFALL_MARGIN`;
  the trade verdicts (`execution_window_reason`, `score_reason`,
  `exec_alert_reason`, `exec_shortfall_reason`) now enforce them.
- `blm_v4/api.py` folds the pace bar, the shortfall, the band and the score
  floor into the SERVED `under_alert_exec_armed` (the side that holds the league
  reference and the projector row) and reports the trade values in
  `under_alert_execution_window`.  The executor CONSUMES the served boolean.
- **THE ALERT IS UNCHANGED.**  The alert's own class bar is
  `under_alert.REQUIRED_MARGIN = 1.04` and its floor
  `under_alert.ALERT_PROGRESS_PCT = 75.0` — byte-identical.  The `EXEC_*`
  constants (75 / 92 / 70 / 1.04) are retained VERBATIM as the alert-era /
  frozen-analysis class; `live_analytics.fingerprint_stats` (the frozen
  traded-cohort analysis) reads them, never the `TRADE_*` ones.

Tests: `tests/test_trade_combo_2026_10_12.py` (one named test per leg plus the
ALERT-unchanged proof); `tests/test_execution_window_2026_10_07.py` and
`tests/test_exec_shortfall_2026_10_12.py` updated deliberately for the new
band / score / pace (the guarded population moved — see §15.3's contract-change
note and `repo-change-invariants.md`).

---

## 15.7 The LIVE TRADE COMBO — third revision (operator directive 2026-10-12)

**Supersedes §15.6 for the TRADE gate** (the alert gate is untouched — see below).
The third revision re-measured the frontier on the corrected cohort (40 days,
in-sample) and moved the gate to the footprint that keeps the widest defensible
volume. The Auto-Bet gate now requires ALL of:

| leg | value | constant |
|---|---|---|
| progress band | `75% <= progress <= 92%` (both inclusive) | `TRADE_MIN_PROGRESS_PCT` / `TRADE_MAX_PROGRESS_PCT` |
| pace ratio | `required_pts_per_min / competition_avg_pace` **> 1.04** (STRICT) | `TRADE_MIN_PACE_RATIO` |
| scored points | `current_total_points >= 70` | `TRADE_MIN_SCORED_POINTS` |
| shortfall margin | `required_pts_per_min - actual_pts_per_min >= 0.0` | `SHORTFALL_MARGIN` |
| under price | `under_price >= 1.60` (**FLAT**) | `TRADE_MIN_UNDER_PRICE` (via `trade_price_reason`) |

The band, the score floor and the shortfall margin now COINCIDE with the
alert-era class; the pace bar (strict, 1.04) and the flat 1.60 price floor are
what pinch the volume.

### The evidence (corrected cohort, 40 days, in-sample)

| combo | win % | bets/day |
|---|---|---|
| **price>=1.60, pace>1.04, shortfall>=0, band 75-92** — the gate | **72.9%** | 61.9 |
| price>=1.60, pace>1.00 | 71.2% | 70.7 |
| no pace filter | 67.5% | 91.9 |
| price>=1.60, pace>1.08 | 74.6% | 51.4 |
| price>=1.90, pace>1.08, shortfall>=0.25, band 78-90 — the §15.6 gate | 78.1% | 27.8 |

The gate is a deliberate trade-off on the frontier: it gives up ~5 pp of win rate
versus the §15.6 gate to roughly double the number of bets (27.8 -> 61.9/day).
The pace bar does the real filtering — adding it (price>=1.60, pace>1.04 vs
pace>1.00) buys ~1.7 pp while cutting ~9 bets/day, and pace>1.08 buys another
~3.4 pp while cutting ~10 more.  (n=2,476 for the gate.)

**The FLAT 1.60 price floor is new in this revision.**  The trade path no longer
reads the per-band break-evens or the flat 1.85 `EXEC_MIN_UNDER_PRICE`; it reads
a flat, trade-only `TRADE_MIN_UNDER_PRICE = 1.60` via `trade_price_reason`.  The
`min_price_for` / `price_reason` break-evens are retained VERBATIM as the
published / frozen-analysis floor.

### Caveats (carry these with the number)

1. It is an **in-sample maximum over ~240 combos** — a real overfitting risk;
   the ranking is stronger evidence than the exact rate.
2. **Corrected finals** — the cohort's prerequisite (see §15.3).
3. **Modelled entry prices, no slippage** — not P&L; and the EV depends on the
   price ACTUALLY taken, not on the floor (see §15.5.2).
4. Changing the gate is a **CLASS CHANGE** (`14_TRADED_COHORT.md` §14.5).

### Implementation — the ONE definition, and what did NOT move

- `blm_v4/trade_window.py` sets the `TRADE_*` constants (75 / 92 / 70 / 1.04 /
  0.0) and adds the flat `TRADE_MIN_UNDER_PRICE = 1.60` plus the trade-only
  `trade_price_reason`; the trade verdicts (`execution_window_reason`,
  `score_reason`, `exec_alert_reason`, `exec_shortfall_reason`) enforce them.
- `blm_v4/api.py` folds all FIVE legs — the pace bar, the shortfall, the band,
  the score floor AND the under price — into the SERVED
  `under_alert_exec_armed` (the side that holds the league reference, the
  projector row and the market row) and reports the trade values — including the
  flat `min_price` 1.60 — in `under_alert_execution_window`.  The executor
  CONSUMES the served boolean.
- `blm_v4/betting/executor.py`'s price gate uses the flat `trade_price_reason`
  floor (1.60), NOT `min_price_for`'s 1.85.
- **THE ALERT IS UNCHANGED.**  The alert's own class bar is
  `under_alert.REQUIRED_MARGIN = 1.04` and its floor
  `under_alert.ALERT_PROGRESS_PCT = 75.0` — byte-identical.  The `EXEC_*`
  constants (75 / 92 / 70 / 1.04 / 1.85) and `min_price_for` / `price_reason`
  are retained VERBATIM as the alert-era / frozen-analysis class;
  `live_analytics.fingerprint_stats` (the frozen traded-cohort analysis) reads
  them, never the `TRADE_*` ones.

Tests: `tests/test_trade_combo_2026_10_12.py` (one named test per leg plus the
ALERT-unchanged and frozen-constant proofs); `tests/test_execution_window_2026_10_07.py`
and `tests/test_exec_shortfall_2026_10_12.py` updated deliberately for the new
band / score / pace / shortfall / price (the guarded population moved — see
§15.3's contract-change note and `repo-change-invariants.md`).

---

## 15.8 The LINE-MOVEMENT VOLATILITY filter — a new trade leg
## (operator-approved, 2026-10-12)

**ADDITIVE to §15.7** — the alert gate is untouched (see below). The Auto-Bet
gate gains ONE more leg: a game trades only when its TOTAL line is provably
**FLAT** relative to where it was FIRST CAPTURED. A line that has drifted is a
market-disagreement signal that materially lowers the UNDER win rate.

### The rule

| leg | value | constant |
|---|---|---|
| line movement | `|entry_line - open_line| / league_avg_line` **< 2%** (STRICT) | `TRADE_MOVE_MAX = 0.02` |

* `open_line` — the FIRST non-null `live_total_line` **captured** for the game
  (from `clean_observations`). This is the first **CAPTURED** line, **NOT** the
  true market open (`market.opening_line` from the snapshot stream is a
  different quantity); the distinction is deliberate and the study used the
  captured definition.
* `league_avg_line` — the mean of the per-game first-captured lines across the
  game's `competition_slug` (study population: NBA 226.6 · EuroLeague 172.7 ·
  TBSL 167.7 · CBA 178.2 · KBL 156.9 · cyber 218.5).
* The gate **FAILS CLOSED**: if the open line OR the league average is unknown /
  unprovable, the trade is **REFUSED** (`MOVE_UNPROVABLE`). For this leg an
  unprovable move must not license a trade — the safe direction is refusal.

### The evidence (out-of-sample walk-forward)

38 clean days, **entry-date attribution** (the game is scored in the bucket its
ENTRY fell in), corrected finals:

| cohort | UNDER | 95% CI | n | EV |
|---|---|---|---|---|
| **flat** `|move| < 2%` | **78.8%** | [76.5, 80.9] | 1,363 | **+0.672** |
| **moved** `|move| >= 2%` | **64.7%** | [61.6, 67.7] | 949 | +0.228 |

**Gap +14.1 pp, p ≈ 6e-14.** Both cohorts are large and the separation is not a
small-sample artefact.

* **Half-split.** The 38-day OOS window split in halves reproduces the gap in
  BOTH halves (direction and magnitude consistent) — reported by the study; not
  re-derived in this write-up.
* **Threshold plateau.** The separation is FLAT across a 1–4% cut, so 2% is a
  centre, not a knife-edge — carving the cohort anywhere in that plateau yields
  the same split.
* **League / price-band breakdown.** The gap HOLDS in all six leagues (NBA,
  EuroLeague, TBSL, CBA, KBL, cyber) and in every price band — it is not one
  league or one price regime carrying the result.

### The stricter history variant (NOT enabled)

`TRADE_MOVE_MIN_HISTORY_S` (env, default **0.0**) — 0 is the exact
configuration the study validated (the variant is EXCLUDED). Setting it (e.g.
`900` for ≥ 15 minutes of captured history) enables a stricter variant WITHOUT a
code change: its flat bucket is ~38% the size of the 0-second bucket but carries
a **+12.3 pp** gap (smaller sample, same direction). It is documented here so a
later operator can turn it on knowingly; it is OFF today.

### The toggle

`TRADE_MOVE_FILTER_ENABLED` (env, **default ON** — the operator approved the
leg). `false` / `0` / `no` / `off` disables it entirely: the leg then imposes NO
requirement and a volatile move no longer disarms a trade.

### Caveats (carry these with the number)

1. `open_line` is the first **CAPTURED** line, not the market's own open; a game
   whose first captured tick already lags the market contributes to noisier
   `move_norm`.
2. **Short-capture-window confound.** "Flat" partly measures how briefly the
   game was under capture: a game captured for only a few minutes has few
   chances to move. The min-history variant exists precisely to test this,
   which is why it is shipped OFF but wired.
3. **Regime change is untestable** from the same window — the flat cohort's edge
   is measured under one price/pace regime and could decay.
4. Modelled entry prices, no slippage — not P&L (§15.5.2).
5. Changing the gate is a **CLASS CHANGE** (`14_TRADED_COHORT.md` §14.5).

### Implementation — the ONE definition, and what did NOT move

- `blm_v4/trade_window.py` gains `TRADE_MOVE_MAX = 0.02`, the reason vocabulary
  (`MOVE_VOLATILE` / `MOVE_UNPROVABLE`), the toggle + min-history env constants,
  and `trade_move_reason(entry_line, open_line, league_avg_line, history_s=None)`.
  They are **APPENDED**: every frozen line above the new block (the `EXEC_*`
  class, `MIN_PRICE_BANDS`, `min_price_for`, `price_reason`, the trade verdicts)
  is **byte-identical** — `tests/test_trade_move_filter_2026_10_12.py` asserts
  the file still BEGINS with `git show HEAD:blm_v4/trade_window.py`.
- `blm_v4/live_analytics/line_reference.py` (+ `line_reference_worker.py`) — the
  ONE definition of `open_line` and `league_avg_line`; the worker precomputes
  them off the request path (mirroring `pace_reference_worker`).
- `blm_v4/api.py` folds the leg into the SERVED `under_alert_exec_armed` (the
  side that holds the league reference / projector / market rows) and carries
  `entry_line` / `open_line` / `league_avg_line` / `move_norm` / `reason` in the
  new `under_alert_line_move` block. The executor CONSUMES the served boolean.
- **THE ALERT IS UNCHANGED.** `under_alert.REQUIRED_MARGIN = 1.04` and
  `ALERT_PROGRESS_PCT = 75.0` are byte-identical (asserted against HEAD), and the
  `EXEC_*` frozen class is unchanged. The new leg reads neither.

Tests: `tests/test_trade_move_filter_2026_10_12.py` (boundary at exactly 2.0%,
fail-closed, toggle on/off, min-history excluded at 0, the FROZEN/alert
byte-unchanged proofs, and an end-to-end proof the SERVED boolean disarms a
volatile move so the executor refuses it).
