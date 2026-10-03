# 07 — Collector Architecture

Retrieval keywords: collector, Playwright, browser, WS, WebSocket, market observation,
snapshot, tick cadence, watchdog, timeout budget, thread affinity, page.content,
heartbeat, collector OFFLINE, event view, panel discovery, slow worker, resolve queue.

**What this pack answers:** how live data enters BLM, and how to diagnose a
collector failure instead of blindly restarting services.

---

## The path

```
PokerBet live panel (SPA)
   ↓  Playwright (self-healing browser rotation)
   ↓  page capture: lobby panel parse + event-view capture
   ↓  parser (blm_v4/event_parser.py) → teams / score / period / clock / quarters
   ↓  snapshot  (blm_v4/storage.py → snapshots; UNIQUE(game_id, captured_at))
   ↓  market observation (event-view DOM and/or eu-swarm WS → snapshots.total_line / market_observations)
   ↓  database (blm_pokerbet.db; WAL hygiene via wal_hygiene.py)
   ↓  clean pipeline (clean_* tables) → pace projector → clean_projections
```

The eu-swarm WebSocket (`wss://eu-swarm-newm.pokerbet.co.za/`) pushes the full
live market tree with NO event-view DOM dependency — the primary alternative when
the event-view route stops hydrating.

---

## Rule 7.1 — The collector runs from the WORKING TREE

**Rule.** A dirty tree goes live. The unit runs `python3 -m blm_v4.collector`; modules
load at process START. `deploy/*.service` files are STALE; the live units are
`~/.config/systemd/user/blm-collector.service` (+ drop-ins).

**Source.** live `~/.config/systemd/user/blm-collector.service`.

**Why.** A fix on disk is not live until the process restarts.

**Valid.** Prove the running process holds the fix (Rule 0.8).

**Invalid.** "I edited the file, so the collector is fixed."

**Forbidden.** Assuming `deploy/blm-collector.service` reflects production.

**Verify.** `systemctl --user cat blm-collector`; `ExecMainStartTimestamp` vs module
mtime.

---

## Rule 7.2 — Cadence and the tick budget

**Rule.** Fast loop targets `FAST_TICK_S` (default 5.0 s; the running unit uses
`--tick 10`). Live-market freshness target is 30 s (`LIVE_MARKET_FRESH_TARGET_S`).
The slow worker does event-view captures with `EVENT_VIEW_MIN_INTERVAL_S=10`,
`MARKET_REFRESH_S=240`, `RESOLVE_BATCH=2` per wake, `RESOLVE_RETRY_S=30`.

**Source.** `blm_v4/collector.py`.

**Why.** The 2026-09-30 capture-efficiency phase brought tick P50 2.47→1.44 s and
line-present ~27→100% by tightening this scheduling without touching alert logic.

**Valid.** A fast cycle that completes inside the grid.

**Invalid.** Judging health by uptime alone — check that a recent tick wrote a
snapshot.

**Forbidden.** Raising timeouts to mask an overrun (report the flap instead).

**Verify.** Journal shows a recent tick with `errors=0`; newest
`snapshots.captured_at` is seconds old; `collector_state.json` `last_tick_at`
advances.

---

## Rule 7.3 — Playwright thread affinity: `page.content()` must stay on the owning thread

**Rule.** All Playwright calls — including `page.content()` — must run on the
thread that owns the browser/page. Do NOT move a Playwright call to a worker thread
that did not create it.

**Source.** `blm_v4/collector.py` (fast path owns the page; the selected-section
read uses a single-element DOM call).

**Why.** Violating affinity raises "Cannot switch to a different thread".

**Valid.** Read the selected sidebar section on the owning thread via
`_selected_section_html` (`page.locator('[class*="market-game-section"][class*="active"]').first.evaluate("el => el.outerHTML")`).

**Invalid.** Calling `page.content()` from a background thread.

**Edge.** Prefer a single-element DOM call over the whole-page `page.content()`
round-trip — the latter overruns the tick budget (FM-09).

**Forbidden.** Using `page.content()` where a single-element locator would do.

**Verify.** No thread-affinity errors in the journal; the capture completes within
budget.

---

## Rule 7.4 — The self-watchdog is the kernel + PID 1, not hope

**Rule.** `Type=notify` + `WatchdogSec=90`. The collector pets systemd's watchdog
from the FAST path only while fast cycles complete. A wedged process (blocked
Playwright/SPA call) stops pinging and systemd restarts the whole unit.

**Source.** `blm-collector.service`; `collector._watchdog_loop`.

**Why.** "The collector must never be down" is enforced mechanically.

**Valid.** A watchdog-triggered restart appears in the journal with no agent action.

**Invalid.** Attributing a watchdog restart to yourself.

**Forbidden.** Disabling the watchdog to stop flaps.

**Verify.** `journalctl --user -u blm-collector | grep -iE "watchdog|Stopped|Started"`.

---

## Rule 7.5 — Collector OFFLINE / heartbeat semantics

**Rule.** Liveness is reported via the heartbeat payload and `WATCHDOG=1`. The API
must NOT render an API failure as "COLLECTOR OFFLINE".

**Source.** commit `d864988` ("Stop rendering API failures as COLLECTOR OFFLINE").

**Why.** Conflating an API error with a dead collector misdiagnoses the fault.

**Valid.** Distinguish "collector not writing snapshots" (collector fault) from
"the API call failed" (server fault).

**Forbidden.** Reporting a collector outage from an API timeout.

**Verify.** Check `snapshots.captured_at` freshness independently of the API.

---

## Rule 7.6 — Discovery coverage is a COUNTER, not a guess

**Rule.** Tracked coverage is bounded by the panel and the resolve queue. Diagnose a
coverage problem by comparing a live external truth (fetch the panel, count rows)
against the collector's own counters (`games_tracked`, `pending_resolve`,
`rows_seen`).

**Source.** `collector._discover_panel_rows`; FM-08.

**Why.** The `0561ea2` re-indentation bug showed as `games_tracked=6` with a 40-row
panel.

**Valid.** `rows_seen` ~40 paired with `pending_resolve` 1 = the regression
signature.

**Invalid.** Concluding coverage is fine from a healthy-looking process.

**Forbidden.** Raising a batch/timeout to "fix" a coverage symptom.

**Verify.** Live-panel row count vs `state/collector_state.json` counters.

---

## Rule 7.7 — A game in the WS feed but absent from `games` is a DISCOVERY symptom

**Rule.** If a game is visible in `ws_raw_frames` but absent from `games` /
`quarter_score_observations`, it is a discovery gap, not a capture bug.

**Source.** STATUS 2026-10-03 (e.g. `31103377`).

**Why.** The panel bounds discovery; the WS feed does not.

**Valid.** Check `games_tracked` vs the live panel row count first.

**Forbidden.** Blaming the parser before checking discovery.

**Verify.** Presence of the id in `ws_raw_frames` vs `games`.

---

## Rule 7.8 — WS frames DO carry per-quarter scores (a coverage lever)

**Rule.** A live `ws_raw_frames` payload holds
`info.additional_data.quarterScores = [{quarterNumber, score:{team1,team2}}...]`
for every listed game — a panel-independent way to capture quarters for ALL listed
games.

**Source.** STATUS 2026-10-03; contradicts the `_record_quarter_scores` docstring
("WS frames carry no quarter scores — audited 2026-09-22"). VERIFY the docstring
before trusting it.

**Why.** Quarter-score coverage via the DOM path is bounded by games the collector
resolves and visits; the WS path is not.

**Valid.** Confirm the field in a live frame before wiring the path.

**Forbidden.** Trusting a docstring that a live frame contradicts.

**Verify.** Inspect a `ws_raw_frames` payload for `quarterScores`.
