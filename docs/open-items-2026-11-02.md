# Open items — 2026-11-02

Recorded at the end of the session that landed: the total-ladder split guard, the
two-tier price floor (signal 1.60 / execution 1.70), the WAL truncate-cap retune,
and the dashboard's `tradable` + `bet placed` audio cues.

Nothing here blocks trading. Each is either a decision owed by the operator or a
guard that has not YET met its condition in production.

## 1. "Only place bets of 1.7 or larger" — RESOLVED as a flat floor

Landed: the SIGNAL floor stays 1.60, every arm/place/re-validate seam requires
1.70 (`TRADE_EXEC_MIN_PRICE`), and the default is now 1.70 itself so a deploy
without the env var cannot trade lower.  The alternative reading ("take another
rung at >= 1.70") was NOT built — it would change which line is bet.

## 1b. "There will normally be more than 1 rung" — original note

Implemented literally: a game priced 1.60-1.69 still ALERTS and shows on the
board, and every execution seam (executor, fast lane, submission revalidation,
served exec-armed verdict) refuses it.

The operator's full sentence also said "there will normally be more than 1 rung
on the market". If the intent was "when the selected rung is under 1.70, take a
DIFFERENT rung that is at or above 1.70", that is NOT what was built — it changes
WHICH line is bet, and is a separate change to line selection. Ask before
building it.

## 2. Three stale in-flight ledger rows from 2026-10-28

`blm_betting.db` · `bet_executions`:

```
3193  SUBMITTING   2026-10-28
3197  PENDING      2026-10-28
3198  PENDING      2026-10-28
```

They are four days old and terminal in practice. Each one burns its game's single
claim forever and makes any naive "is a bet in flight?" check return true, so a
restart approval has to reason around them every time. They should be reconciled
(or annotated as abandoned) in a quiet window — do not delete rows blindly; check
each game's settlement first.

## 3. Guards landed but not yet observed firing in production

Both are proven by tests and by in-process checks on the live tree; neither has
met its real condition yet:

- `LADDER_MAX_ROW_GAP` — no split ladder has occurred since the restart. The
  first real one will leave
  `total ladder SPLIT for <game>: rows N pts apart` in the blm-collector journal
  (throttled to one per game per 5 min).
- `TRADE_EXEC_MIN_PRICE=1.70` — the next sub-1.70 rung that otherwise qualifies
  will be refused with `price_below_break_even` at every seam. Confirm it once in
  the ledger rather than assuming.

## 4. Audio cues need one click per browser

Every browser blocks audio until a user gesture. The dashboard's AUDIO toggle
(=the confirmation tone) is the unlock, and the preference persists in
localStorage. A fresh browser/profile will be silent until it is clicked — this
is not a fault, and there is no server-side way around it.

Cues: alert rises (1/2/3/4-tone by level) · `tradable` (high two-note, on entry
into the tradable set) · `bet placed` (low rising triad).

## 5. WAL — watch the first automatic truncate

`96-wal-hygiene-tuning.conf` now truncates at 128 MB (gate 64 MB, 60 s). The
manual fold measured 157 MB → 0 MB in 0.0 s with `log=0 copied=0`. The automatic
path has not yet been observed crossing 128 MB since the restart; when it does,
confirm the file falls rather than assuming the new cap works.

## 6. Trunk reconciled — collector tree still diverged

2026-11-02 `6a52c1c` merges production into `fix/price-mapping-2026-10-06` and is
deployed: the trunk now carries the four re-landed legs (shortfall, competition
exclusion, cohort naming, submission revalidation), the two-tier price floor and
the dashboard cues, alongside the newer transport/board work.  The banded
`min_price_for` is back to the measured table (1.48/1.39/1.28) and the price the
trade uses is the operator's flat floor, not the band.

STILL OPEN: the COLLECTOR tree (`/home/ubuntu/BLM`, branch `handoff-2026-09-07`)
is a separate lineage and was 17 ahead / 10 behind at the same moment.  Its work
is pushed to `ladder-split-2026-11-02` and running in production, but it has not
been reconciled with `origin/handoff-2026-09-07`.  Reconcile it the same way
before the next deploy of that tree.

## 7. Skill index is at its size limit

`blm-live-analytics-platform/SKILL.md` is 99,776 chars against a 100,000 hard
limit, so a new reference (`references/total-line-and-price-floors.md`) could be
written but not indexed in the body. It needs a curation pass (move topic depth
out of the always-loaded body) before any further index line can be added.
