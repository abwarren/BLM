# Live trading parameters — the gate that fires auto-bet

**Status:** current as of 2026-11-02 08:25 SAST · **Locked by:** `tests/test_live_parameters_locked_2026_11_02.py` · **Inspect with:** `scripts/blm_live_params.py`

This file is the single written record of what the DEPLOYED engine is allowed to
trade on. It is not a proposal and not a default: it is what the running
`blm-server` has in its environment. Any change here needs the matching drop-in
change on the box, and the test will fail until the two agree.

## The parameters in force

| parameter | value | leg it implements |
|---|---|---|
| `TRADE_MIN_PACE_RATIO` | **1.04** | required pace ÷ league average pace must be **strictly above** 1.04 |
| `TRADE_SHORTFALL_MARGIN` | **0.50** | required − actual pace must be **≥ +0.50** (game is half a point/min under its required rate) |
| `TRADE_MIN_PROGRESS_PCT` / `TRADE_MAX_PROGRESS_PCT` | **75 / 92** | game-progress band (code constant, not env) |
| `TRADE_MIN_SCORED_POINTS` | **70** | points already on the board (code constant) |
| `TRADE_EXEC_MIN_PRICE` | **1.60** | execution floor on the UNDER price — equals the signal floor, so the seam is not stricter than the alert |
| `TRADE_MOVE_FILTER_ENABLED` | **0** | line-movement leg off |
| `BET_REVALIDATE` / `BET_REVALIDATE_MAX_AGE_S` | **1 / 120** | the gate is re-run at the submission boundary against a capture no older than 120 s; fails closed |
| excluded competitions | `cyber-basketball-2k26-matches` | never traded (code constant) |
| `BETTING_DRY_RUN` / `BETTING_BROWSER_SUBMIT` | **false / true** | real submission through the PokerBet DOM transport |
| `BLM_EXPECTED_ACCOUNT_ID` | **233997789** | account-identity guard, checked at the submission boundary |

These values are exactly `TC-C` in `blm_v4/trade_cohorts.py`:

```
TC-C  CORE   pace 1.04 · shortfall -0.50 · price 1.60
```

so the live gate is a **named cohort**, not an ad-hoc number. The test asserts
that agreement, which is what stops the env and the registry drifting apart.

## Where the values live (and the precedence trap)

`~/.config/systemd/user/blm-server.service.d/`, loaded in filename order, **last
writer wins per key**:

| file | sets |
|---|---|
| `96-trade-move-off.conf` | `TRADE_MOVE_FILTER_ENABLED=0` |
| `97-trade-pace-tcw.conf` | `TRADE_MIN_PACE_RATIO=1.00` ← **shadowed, dead** |
| `98-trade-cohort-tc25.conf` | `TRADE_MIN_PACE_RATIO=1.04`, `TRADE_SHORTFALL_MARGIN=0.50`, `BET_REVALIDATE=1`, `BET_REVALIDATE_MAX_AGE_S=120` |
| `98-trade-exec-price-floor.conf` | `TRADE_EXEC_MIN_PRICE=1.60` |

`97-...` still contains a live-looking `1.00` that nothing overrides except
`98-...` winning on filename order. That is a trap: deleting or renaming
`98-trade-cohort-tc25.conf` silently widens the gate to `TC-W` (pace > 1.00,
+8 bets/12 days at a lower win rate). Do not "clean up" either file without
running `scripts/blm_live_params.py` afterwards.

## Change log — 2026-11-01 → 2026-11-02

Read from the journal (`pace_rung` is logged by `fast_lane_ready` on every
start-up) and from `blm_betting.db`, not from memory.

- **2026-10-30 03:55** — `98-trade-cohort-tc25.conf` created: the live gate
  became `TC-C` (pace > 1.04, shortfall ≥ +0.50, revalidate on).
- **2026-11-01 22:50Z → 2026-11-02 06:14Z** — every restart logged
  `pace_rung: 1.04`. These are the restarts the last ten bets were placed
  under.
- **2026-11-02 00:39** — `TRADE_EXEC_MIN_PRICE=1.70` added (operator: "only
  place bets of 1.7 or larger"). It was live for exactly one of those ten bets
  (3681, price 1.70); the other nine were priced 1.60–1.85 and six of them are
  below 1.70, so under that floor they could not have fired.
- **2026-11-02 08:14** — that floor reverted to **1.60** on the operator's
  instruction ("the parameters for auto bet to fire … change it back"), which
  restores the price leg the ten bets actually ran on.
- **2026-11-02 06:23Z (13 minutes)** — a `TC-S` experiment (`pace 1.08`) was
  applied and reverted at the operator's "Nop"; no bet was placed in that
  window. The measured comparison is kept in the drop-in's comment.
- **2026-11-02** — `bet: persist the odds the bet was TAKEN at` (`12db2cc`) and
  `live: run the first-line scan OFF the request path` (`2c42c11`) are both
  deployed. The second one matters here: the line-reference scan (93.5 s over
  5.2M `clean_observations` rows) used to run inside the payload build the
  betting worker waits on, which left the lane blind after a restart.

## How to change one (and how to revert)

```bash
# 1. edit the value in the owning drop-in (never in two files)
sudoedit ~/.config/systemd/user/blm-server.service.d/98-trade-cohort-tc25.conf

# 2. make systemd and the process agree
systemctl --user daemon-reload && systemctl --user restart blm-server

# 3. prove it took, from the process, not the file
tr '\0' '\n' < /proc/$(systemctl --user show blm-server -p MainPID --value)/environ \
  | grep -E 'TRADE_|BET_REVALIDATE'
python3 scripts/blm_live_params.py     # declared vs effective, exit 1 on drift

# 4. the journal must agree too — this is the line the bets run under
journalctl --user -u blm-server --since "-5 min" | grep -o '"pace_rung": [0-9.]*'
```

Revert is the reverse edit plus the same reload/restart. Both the 1.70 floor and
the TC-S experiment were reverted this way today; the journal is the record.

## Why this file exists

Three changes reached this gate in nineteen hours, one of them silently
widening it (the 1.70 floor), one narrowing it (TC-S), and one of them
mis-attributed to the operator. Nothing about the parameters is expensive to
change — the cost is in not knowing which numbers are live. So: the values are
written here, pinned by a test against the registry, and diffable against the
running process with one command.
