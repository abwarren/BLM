# BETUAL-ONLY QUARTERLY LINE + SCORE COLLECTION — DESIGN & FINAL REPORT

Directive date: 2026-09-22 · Status: IMPLEMENTED (data collection only) ·
Nothing in this work touches alerts, fingerprints, betting logic,
thresholds, or any production decision path (§19).

---

## A. Current Betual data source / collector architecture

Single resilient collector (`blm_v4/collector.py`) against PokerBet
(BetConstruct SPA), which hosts Betual NBA as its own competition
(`Classification.BETUAL_NBA`, URL taxonomy `Virtual Matches /
betual-nba`).  Identity is durable: `source='PokerBet'` +
`source_game_id` (the BetConstruct event ID).  Two paths, ONE polling
loop each:

- **FAST path (5s monotonic deadline grid):** lobby panel parse (score /
  period / clock), list snapshots, WS subscription maintenance,
  persistence, heartbeat.
- **SLOW path (own worker thread + own browser):** event-view navigation
  (verified scoreboard incl. per-quarter breakdown + Total Points
  ladder), identity resolution, market rotation (~3 games/round).
- **eu-swarm WebSocket (authenticated per page):** pushes each
  subscribed game's full market tree — `MatchTotal` (game total) plus
  quarter/team totals — independently of the DOM.

Prior directive work earlier today (2026-09-22) added quarter-score
observations (`quarter_score_observations` from the verified event view)
and quarter-line observations (`quarter_market_observations` from the WS
feed) with raw-frame retention and chronological validation.  This work
reuses that infrastructure unchanged.

## B. Existing reusable schema

| Table | Reuse in the Betual dataset |
|---|---|
| `games` | registry + FK target; `classification='BETUAL_NBA'` is the §1 gate key |
| `snapshots` (+ q1..q4 columns) | verified scoreboard source; disappeared-end last-known scores |
| `market_observations` | historical `MatchTotal` path — **byte-identical, unchanged** |
| `quarter_market_observations` | quarter lines (raw market entry preserved) |
| `quarter_score_observations` + `_anomalies` | quarter scores exactly as presented |
| `ws_raw_frames` | verbatim frame retention (parse failures never throttled) |
| `betual_game_timers` (new) | §13 restart anchors |

## C. New fields / tables required (all ADDITIVE, backward compatible)

New tables (created by `PokerBetStore._init` on next collector start; no
migration of existing rows):

- **`betual_time_observations`** — one row per score observation with the
  §1 field set: `source_start_time`, `internal_game_time`,
  `internal_elapsed_seconds`, `quarter` (internal),
  `quarter_remaining_seconds`, `q1..q4 home/away`, `home_score`,
  `away_score`, `total_score`, `betual_displayed_clock`,
  `clock_difference`, `source_quarter`, `raw_json`.
  UNIQUE(source_game_id, captured_at).
- **`betual_line_observations`** — full-game + Q1..Q4 lines with
  §6/§7 movement fields: `line_previous`, `line_change`,
  `seconds_since_previous_line`, `line_velocity`,
  `score_at_observation`, internal-timer fields, prices, raw.
  UNIQUE(source_game_id, market_id, line, captured_at) — §10 dedup.
- **`betual_transitions`** — §11 snapshots (Q1→Q2, Q2→Q3, **Q3→Q4**):
  prev/new quarter, prev-quarter final score, new cumulative, internal +
  displayed timers, `clock_difference`, full-game line, quarter line,
  `line_previous`, `line_change`.  UNIQUE(source_game_id, transition).
- **`betual_game_ends`** — §12: final scores/total, final quarter
  scores, full-game line, `settlement_state`, and **`end_evidence`**
  (`observed_final` | `disappeared`) — the NO-FINAL diagnosis data.
- **`betual_clock_diagnostics`** — §14 flags (see D/L).
- **`betual_parse_failures`** — §9: raw retained, parsed NULL, error.
- **`betual_game_timers`** — §13 persisted anchors + last caches.

`ws_market._extract_game` now passes through `start_ts` / `match_length`
(additive keys; the normalized `MatchTotal` observation shape is
unchanged and pinned by test).

## D. Internal timer design

`blm_v4/betual_timer.py` — pure, no I/O:

    game_start_wall   = Betual start evidence (WS start_ts, §2)
    monotonic_anchor  = time.monotonic() at adoption (+ elapsed offset)
    elapsed           = monotonic_now - monotonic_anchor + offset
    quarter model     = [(k-1)(q+b), k(q+b))  per quarter k

- Live object runs on `time.monotonic()` (§3: NTP/wall adjustments
  cannot move the game timer); restart path derives the same elapsed
  wall-to-wall (both terms move together under NTP — cannot go backwards).
- `TimerAnchors.model` honestly reports `'default'` (4×600 s, only until
  calibrated) vs `'calibrated'` (§4: lengths are never blindly assumed).
- Post-Q4 elapsed is capped at the model's full-game length — no
  invented game time.
- Q(k+1) derivation from cumulatives happens ONLY from two consecutive
  observed cumulative pairs (§5) — a hole leaves the leg NULL.

## E. How Betual start time is obtained

The swarm feed's game object carries `start_ts` (epoch seconds) — the
subscription request already asked for it; nothing stored it.  Now:
`_extract_game` returns it; the WS frame handler adopts it per game
(`_betual_maybe_start_ts`) — FIRST evidence wins (idempotent), the wall
pair `(start_ts, first_seen_wall)` is persisted to `betual_game_timers`.
A game joined mid-stream anchors with
`elapsed_at_adoption = seen_wall - start_ts`, so the timer never
restarts from zero.  Until the feed exposes it, rows carry
`source_start_time = NULL` (never fabricated, §9).

## F. How the internal timer survives restart

1. Every dataset write persists the anchor + latest caches
   (`_betual_persist_timer` → `betual_game_timers`).
2. On `start()`, `_betual_restore_state()` reads all anchor rows and
   rebuilds each game's `BetualGameRecord`:
   calibrated model restored as-is; wall anchor re-adopted with
   `elapsed_at_adoption = now - start_ts`; previous score/line caches
   re-seeded so the FIRST post-restart row still carries movement
   deltas (pinned by `test_timer_survives_collector_restart`).
3. Verified: elapsed ≥ 900 s after a restart with a 900 s-old anchor —
   never reset to zero.

## G. Quarter-score collection

Verified event-view parse → `_betual_record_score` (called in
`_capture_event_state`, same capture instant as the snapshot) →
`betual_time_observations`.  Per-quarter values derive from the
scoreboard's quarter breakdown via point-in-time-valid cumulative
subtraction only.  The pre-existing `quarter_score_observations` path
(exact-as-presented values + anomaly flags) is untouched and runs on the
same parse.

## H. Quarter-line collection

Every non-game-total O/U market the swarm pushes (quarter totals etc.)
routes through `_ingest_quarter_market_observation`, which now ALSO
feeds `betual_line_observations` (period inferred from the market NAME
only — `Q1..Q4`, `other`, or `quarter_unknown`; never guessed).
Full-game lines (`MatchTotal`) enter as `period='full_game'`.  Quarter
lines also land in `betual_transitions.quarter_line` at transitions.

## I. Line-movement calculation

Per line observation vs the game's PREVIOUS line observation:

    line_change              = line - line_previous
    seconds_since_previous   = captured_wall - previous captured_wall
    line_velocity            = line_change / seconds (pts/s; ×60 = /min)

NULL-when-unavailable (first observation, NULL line, restart with no
cache) — never zero, never guessed.  §10: dedup collapses only
same (game, market, line, timestamp); change-and-return across distinct
timestamps is retained (pinned by test).

## J. Score-vs-line timing data

Every line row carries `internal_game_time`, `internal_elapsed_seconds`,
`quarter`, `quarter_remaining_seconds`, the absolute score at the
observation, and `score_at_observation` (the PREVIOUS observation's
total) — so Δline vs Δscore vs Δinternal-time is directly queryable
per market.  Score rows carry the same internal fields plus
`clock_difference`, enabling the directive's research questions
(reaction lag, movement before/after scores, acceleration, quarter-end
behaviour, post-Q3→Q4 behaviour) without joins or reconstruction.

## K. Q3 → Q4 transition capture

`_betual_record_score` compares the verified parse's quarter against the
game's last observed quarter (`_betual_last_quarter`); an exactly-one-
step forward move fires `betual.maybe_transition` → `betual_transitions`
with the full §11 snapshot.  A restart-cleared cache invents nothing
(prev = None ⇒ no transition), and non-adjacent jumps are ignored.
Q3→Q4 is a first-class `transition` value and counted in the metrics.

## L. Game-end / final capture

Two §12 paths, both writing `betual_game_ends`:
- Verified final via event view (`_end_game`) → `end_evidence=
  'observed_final'` + `settlement_state` from the source label.
- Panel disappearance at grace expiry (`_mark_ended`) →
  `end_evidence='disappeared'` with last-known scores (never a final).
The internal timer expiring NEVER records a final.  Complements the
existing NO-FINAL fix (final-capture window + verified-DOM tails) with
per-game evidence data for the diagnosis.

## M. NO-FINAL implications

The dataset now measures the defect instead of just patching it:
`betual_collection_metrics()` reports `finalization_success` vs
`no_final_disappeared`; `end_evidence` on every game-end row shows
exactly which games ended unseen.  Cross-referencing `disappeared` rows
against the collector's final-capture-window attempts will show whether
tails are degenerate SPA rows vs genuine early exits — data the
existing fix's tuning can then use.  Nothing here changes settle/alert
behaviour.

## N. Tests

- `tests/test_betual_dataset.py` (32 tests): timer init/progression/
  mid-game join/calibration (median-robust, no-anchor)/default honesty;
  wall-path NTP-safety; post-Q4 cap; §14 diagnostics (backward clock,
  duplicates, impossible elapsed, transition gap, calibrated-only
  mismatch); score obs + point-in-time derivation; missing data; line
  movement; change-and-return retention; dedup; transitions (Q3→Q4,
  non-adjacent refusals, once-per-game); game ends (observed vs
  disappeared); restart recovery; §1 gate; §9 failures; §18 metrics.
- `tests/test_betual_collector_integration.py` (12 tests): WS start
  evidence adoption + persistence + first-wins; full-game + quarter
  line routing; movement across frames; event-view score + Q3→Q4
  transition; no-transition-without-previous; observed-final vs
  disappeared ends; restart restore (timer not reset, caches reseeded,
  calibrated model honoured); untracked rows skipped; historical
  `MatchTotal` shape unchanged; Cyber games refused.
- Full suite: **1355 passed**; the 4 failures in
  `test_forensic_relative_pace_freeze.py` / `test_prospective_freeze.py`
  were **verified pre-existing** by stashing this work and re-running
  (identical failures on the baseline).

## O. Coverage metrics (§18)

`store.betual_collection_metrics()` (also in the collector state file
under `betual_dataset`): betual games; games with score data;
games with Q1/Q2/Q3/Q4; games anchored to a start time; games with line
data / quarter lines / full-game lines; line observations (+ with-line);
line movements; transitions (+ Q3→Q4); game ends; finalization success;
NO-FINAL (disappeared) count; parse failures; clock diagnostics (+ large
clock discrepancy ≥ 90 s).

## P. Files changed

| File | Change |
|---|---|
| `blm_v4/betual_timer.py` | NEW — internal monotonic timer, calibration, diagnostics (pure) |
| `blm_v4/betual_dataset.py` | NEW — Betual-only recorder: observations, movement fields, transitions, ends, restart restore |
| `blm_v4/storage.py` | ADDITIVE — 7 `betual_*` tables + indexes, insert/lookup/metrics methods |
| `blm_v4/ws_market.py` | ADDITIVE — `start_ts`/`match_length` passthrough on game payloads |
| `blm_v4/collector.py` | Wire-in only — dataset hooks in WS frame handler, `_capture_event_state`, `_end_game`, `_mark_ended`, `start()` restore, state payload; NO new polling loops, NO decision-path changes |
| `tests/test_betual_dataset.py` | NEW — 32 tests |
| `tests/test_betual_collector_integration.py` | NEW — 12 tests |

Not committed / not pushed (per directive).  Production alert,
fingerprint, betting, threshold and settle logic untouched — the dataset
tables feed nothing but analysis.
