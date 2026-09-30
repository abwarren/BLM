# Capture-efficiency baseline — 2026-09-30 (Phase 1, read-only)

Directive: "FIX CAPTURE EFFICIENCY, DO NOT ALTER ALERT LOGIC" — TEST ENVIRONMENT ONLY.
This file is evidence recorded BEFORE any code change. No production process, DB, or
unit was touched during its production. DB access was `mode=ro` + `PRAGMA query_only=ON`.

## Git

- HEAD: `d50b230` "Add a Show-last filter (10/20/50/100/200/ALL) to the RESULTS panel"
- branch: `handoff-2026-09-07` (== origin at time of check)
- working tree: 63 dirty paths (owner work: `blm_v4/scorecard.py`, `blm_v1/collector.py`,
  `watchdog.sh` modified; ~50 untracked). `blm_v4/collector.py` CLEAN == HEAD.

## Process topology — CHANGED since 04:30 diagnosis

- System units `blm-collector.service` / `blm-server.service` are **inactive/dead** and
  `systemctl is-enabled` → **not-found**. Operator migrated to **systemd --user** units
  (parent of both PIDs = `systemd --user`, PID 9503).
- `blm-collector.service` (--user): MainPID 105593, started **13:20:35 UTC**,
  WatchdogUSec 1min30s, **NRestarts=3**.
- `blm-server.service` (--user): MainPID 9520, started 02:54:49 UTC, NRestarts=0.
- Both processes cwd `/home/ubuntu/BLM` → production **runs the working tree**.

## NRestarts increasing? YES

Journal (--user) shows watchdog kills today:
- 13:00:40 Main process exited code=dumped status=6/ABRT → `Failed with result 'watchdog'`
- 13:01:07 Started (new PID 102480)
- 13:20:24 Main process exited code=dumped status=6/ABRT → `Failed with result 'watchdog'`
- 13:20:35 Started (new PID 105593, current)
In-process watchdog near-misses (STOPPING WATCHDOG=1 → recovered) at 13:01:46,
13:05:11, 13:18:56, 14:20:51. So ≥2 hard watchdog ABRT restarts in ~2.5 h.

## Tick durations (PID 105593, 13:20→15:20, n=695 `fast tick N done in X s` lines)

| min | P50 | P90 | P95 | P99 | MAX | mean | >10s |
|-----|-----|-----|-----|-----|-----|------|------|
| 0.05 | 1.68 | 6.76 | 8.64 | 13.72 | 50.54 | 2.82 | 3% |

Much healthier than the 04:30 window (then: P50 4.5 / P95 15.8 / max 110.2). Still:
- **184 `page.content() OVERRAN its 3.0s budget` events** in the same window
  (e.g. 3.7–7.7 s overruns, repeatedly on game 31072240).
- 0 in-process watchdog stop events since 13:20:35 (current process clean so far),
  1 near-miss at 14:20:51 (recovered 25 s later).
- Last successful tick at artifact time: tick 692+ completing ~1.4 s, errors=0.

## NULL-score snapshots (snapshots table, `captured_at >= '2026-09-30 13:20'`)

- total 23,910; NULL home/away score **3,104 = 13.0%** (higher than the 9.5% class-A
  audit window of 04:26–04:33).
- Note: `captured_at` is ISO-8601 with `T`/`Z`; earlier SQLite `datetime('now',…)`
  window comparisons silently matched 0 rows — use string compare against ISO stamps.

## Ended-game capture waste (Phase 3 target CONFIRMED still live)

- Game **31061785** (games.status='ended', ended 03:37): **48 new snapshots since
  13:20** — dead game still in fast-path rotation, each visit burns page-capture
  budget (~3 s+ per attempt).
- Game name appears 0 times in collector log since 13:20 → capture is silent
  (no per-game log line), i.e. invisible waste unless counted from the DB.

## Host pressure (15:15 UTC)

- load avg 11.31 / 9.52 / 9.08 (up 12:32)
- PSI io: some avg10=37.08 full avg10=21.56 → I/O still the dominant pressure
- PSI cpu: some avg10=12.16 full=0 ; PSI mem: some avg10=19.93 full avg10=11.41
- **QEMU/KVM: no processes found** (CPU/RAM/IO: N/A at this time)

## Preliminary component attribution (to be replaced by Phase 2 instrumentation)

Log evidence points at DOM `page.content()` capture as the overrunning component
(184 overruns, all tagged `[tick]`, budget 3.0 s, actuals 3.7–7.7 s), with repeats
concentrated on individual slow games (31072240). But per the directive: Phase 2 must
MEASURE tick_total vs game_discovery / page_content / score_parse / board_parse /
line_parse / db_write / retry / ended_game — not assume.

## Implications for the phases

- Phase 2: instrument in a TEST ENV checkout — production runs this working tree, so
  editing `blm_v4/collector.py` in place would go live on next unit restart.
- Phase 3: lifecycle LIVE→FINALIZING→DONE; 31061785 is the canonical repro case.
- Phase 4: 13.0% NULL baseline; bounded recovery must never let one game monopolize
  the tick (31072240 overrun clusters are the suspect pattern).
- Phase 5/6: unchanged; preserve same-thread Playwright contract.
- Watchdog restarts (2 ABRTs today) predate any of my changes — pre-existing.
