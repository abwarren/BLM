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

## 5. WAL — bounded by a free-then-forced fold, and the real pin is a SCAN

Resolved 2026-11-02.  The measured facts, in order:

- the merge had silently dropped the janitor's TRUNCATE escalation (PASSIVE-only);
  restored.
- the biggest pin was `settle_worker._chain_snapshots` planning as
  `SCAN snapshots` + temp b-tree PER GAME (no index through the OR); split into two
  index seeks — 0.001 s for 5 rows where it had run for minutes.
- even then, TRUNCATE cannot win while a READER holds a snapshot: the pace/stats
  scans run 282-579 s out of every 1800 s.  Stopping the collector does not help
  (three attempts, `busy=1 copied=198 held_back=21231`, identical each time).
- but with nothing stopped it succeeds instantly when no scan is in flight
  (measured: 109 MB -> 0 MB in ~1 s).

So `scripts/blm-wal-fold.sh` runs every 10 min from `blm-wal-fold.timer`:
level 1 is a FREE single truncate attempt (no capture gap) and wins whenever the
box is between scans; level 2 — stop the collector, retry 3x, restart, then prove
a fresh fast cycle — only runs above `BLM_WAL_FOLD_FORCE_MB` (320 MB).

The durable fix is still the scans: bounding them (snapshot to a temp table, raise
the cadence further) is what stops the file growing at all.

## 5b. (superseded) WAL — watch the first automatic truncate

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

COLLECTOR tree (`/home/ubuntu/BLM`, branch `handoff-2026-09-07`) — RECONCILED
2026-11-02 on a review branch, production untouched.  It was 17 ahead / 10 behind;
six of the "local" commits were already on origin under rewritten SHAs, and 11 were
genuinely unique.  Method that worked (and is worth reusing): probe each local-only
commit by grepping a handful of its ADDED lines against the origin tree — commits
whose probes are present have been rewritten, the rest are real.  The 11 real ones
cherry-picked onto origin's tip with ZERO conflicts:
  d864988 a2e9514 08895e5 9432aa6 f6a0c38 cfcd460 3f466cd eaaa73f bff3ae4
  e65871b 4abbd1f
pushed as `review/handoff-reconciled-2026-11-02`.  It carries the origin line's
result-reconciler starvation fix + deterministic-test work AND this line's ladder
split, backfill pacing, clean_metrics index, WAL housekeeping, liveness deadline,
C2 fingerprint, multi-row panel discovery, rung wiring and RAG packs.

STILL OPEN: that review branch is not the deployed branch.  The live collector
still runs the old line, and moving it means a collector restart — deferred while
production is frozen.

## 7. Skill index is at its size limit

`blm-live-analytics-platform/SKILL.md` is 99,776 chars against a 100,000 hard
limit, so a new reference (`references/total-line-and-price-floors.md`) could be
written but not indexed in the body. It needs a curation pass (move topic depth
out of the always-loaded body) before any further index line can be added.
