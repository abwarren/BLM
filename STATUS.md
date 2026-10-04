# BLM — STATUS

## 2026-10-04 — C2 RESTORED to the fingerprint layer (non-gating) + doc reconciliation + focused regression. NOT COMMITTED, NOT DEPLOYED, NOT RESTARTED.

Directive: LOCUS A only — restore C2 as a NON-GATING recorded fingerprint;
reconcile docs to code; run the complete focused regression; stop before
commit/restart/deploy.  No gate, no alert-behaviour change, no Auto-Bet.

### CODE CHANGES (2 modules + 2 tests)
- `blm_v4/live_analytics/under_fingerprints.py` — `FINGERPRINT_KEYS =
  ("C1","C2","C3","C5","R2")`; `C2_MOMENTUM_MAX = -0.5`; C2 computed in
  `evaluate_fingerprints` as `recent_pace_3m - actual_pts_per_min <= -0.5`
  (inclusive; explicit `recent3_minus_act` wins; missing operands → UNAVAILABLE);
  label + supporting values (`recent3_pace`/`actual_pace`/`recent3_minus_act`).
- `blm_v4/api.py` — feeds `recent_pace_3m` + `actual_pts_per_min` into
  `evaluate_fingerprints` (2 lines, localised; other uncommitted api.py edits preserved).
- `tests/test_under_fingerprints.py`, `tests/test_v4_api.py` — ONLY the
  C2-ABSENCE assertions updated; the "fingerprints cannot activate alerts" test
  is PRESERVED.

### INVARIANT (proven)
- `under_alert_state` UNCHANGED (`git diff` empty; sha256
  `4643ec1552bdc7a00170b1933b67fb72b93b12887044a4579e2fe3c904f4c6ba`): the rule
  remains `eligible AND progress>=75 AND required > league*1.04`.  It takes no
  fingerprint argument; fingerprints are computed AFTER the verdict and gate nothing.
- Alert universe recomputed from the DB: unchanged (1318 alerts / 1250 settled).

### FOCUSED REGRESSION
- **235 passed, 0 failed** (80 s): test_under_fingerprints, test_v4_api,
  test_betting_execution, test_autobet_command_api_2026_10_03,
  test_trigger_percent_contract_2026_10_03, test_active_alert_card,
  test_clean_metrics, test_under_alert_lifecycle.
- Full `tests/` suite did NOT finish in-window (box under memory pressure; dirty
  tree carries 100+ other-agent edits).  Partial ~51% showed 8 F, all OUTSIDE the
  changed modules — not attributable to this change; re-run when the box is quiet.

### C2 HISTORICAL (frozen cohort; settled OK finals; odds 1.85, BE 54.05%)
- C2 TRUE 656 (67.23%, Wilson [63.5,70.7], +0.244u, +159.8u)
- C2-only 281 (58.01%, +0.073u, +20.6u, Wilson [52.2,63.6] — LB below BE)
- C2 overlap 375 (74.13%) · C2 FALSE 589 (58.91%) · C2 UNAVAILABLE 5
- CURRENT {C1,C3,C5,R2} 650 (72.77%, +225.0u); NEW {+C2} 931 (68.31%, +245.6u)

### DOCS RECONCILED (code = FIVE keys)
- NEW `docs/rag/DECISIONS/ADR-006-c2-restored.md`; `ADR-005-c2-retired.md` marked
  SUPERSEDED.  Also `docs/rag/05_PRODUCTION_RULES.yaml`, `docs/rag/CONTRADICTIONS.md`
  C-01, `docs/rag/10_TRAP_METER.md` (Part B + 10.C.1), `ml/concepts/under_fingerprints.yaml`,
  `docs/BLM_MODEL_SPEC.md`, `docs/CODEBASE_CLEANUP_INVENTORY.md §3.5`.

### STATE
- NOT committed, NOT deployed, NO restart.  The running `blm-server` still serves
  the pre-change fingerprint block until an authorized restart.

## 2026-10-03 23:47 UTC — AUTO-BET COMMAND API: manual + autonomous now share ONE validation path (gap G-07). NOT ARMED, NOT DEPLOYED. Isolated-worktree commit only.

Directive: complete the remaining Auto-Bet production wiring without touching
collector/discovery.  Repository verified FIRST: /home/ubuntu/BLM, origin
`git@github.com:abwarren/BLM.git`, branch `handoff-2026-09-07`, HEAD `f6a0c38`.
No service restarted; no bet placed; no real money.

### WHAT WAS MISSING (evidence)

The canonical rung rule (`blm_v4/betting/rung.py`) + the three stake modes
(`blm_v4/betting/stake.py`) existed but were UNREACHABLE from either production
producer — the manual endpoint (`blm_v4/betting/api.py::manual_bet`) and the
autonomous executor (`blm_v4/betting/executor.py::evaluate`) each ran their own
checks and NEITHER called `rung.validate_execution` / `stake.resolve_stake`
(gap G-07).  Full trace BLM UI → command → API → extension → PokerBet DOM →
confirmation → ledger:

    BLM UI (dashboard.js manual form + alert-card PLACE BET)   EXISTS
    → /api/v4/betting/manual                                   EXISTS (legacy, own checks)
    → command API (ONE shared validator)                       ADDED  (command.py)
    → browser EXTENSION                                        MISSING (no manifest/userscript anywhere in repo)
    → PokerBet DOM                                             BLOCKED (only a guessed-selector CDP adapter)
    → confirmation                                             MISSING
    → execution ledger (blm_betting.db)                        EXISTS (store.py, UNIQUE idempotency)
    → UI/audit (/status /history /game/{id}/state)             EXISTS

### CHANGES (5 paths)

    A  blm_v4/betting/command.py   (NEW) — the ONE shared command layer: command
       identity, the state machine (CREATED/VALIDATED/SENT/ACKNOWLEDGED/EXECUTED/
       REJECTED/FAILED/UNKNOWN) + LEDGER_STATUS mapping, build_command /
       manual_command / autonomous_command / validate_for_execution / audit_record.
       Composes rung.validate_execution + stake.resolve_stake; fail-closed.
    M  blm_v4/betting/executor.py  +20  autonomous path consults command.py (when armed)
    M  blm_v4/betting/api.py       +20  manual UNDER-against-alert path consults the SAME command.py (when armed)
    M  blm_v4/api.py               +6   market payload exposes `observed_lines` (additive) for infer_rung_size
    A  tests/test_autobet_command_api_2026_10_03.py  (NEW, 33 tests)

Everything is behind `command.WIRED_INTO_PRODUCTION = False` (the operator's own
PW gate).  While False, production behaviour is byte-for-byte unchanged; manual
and autonomous share the validator only once armed.

### NOT ARMED — why

PW-02 / PW-03 / PW-14 need a controllable, authenticated PokerBet browser.
Measured this session: NO extension / userscript / manifest in the repo; CDP
`127.0.0.1:9222` REFUSED; no 922x listener; the only PokerBet DOM surface is
`blm_v4/execution/pokerbet/dom.py` with GUESSED selectors (`"button"`,
`[class*="market"]`).  So `wired_into_production` stays `false` — ordering per
the operator's own Production Wiring Gate.

### COMMIT

Working tree: the 5 paths above (dirty 85 → 90); production HEAD `f6a0c38`
UNCHANGED.  My files only, landed in an ISOLATED worktree (never touched the
other owners' 85 dirty paths):

    `66d5283b9040a85bf4ae0170f6521b93498aac37`  (branch autobet-command-wiring-2026-10-03 @ /tmp/blm-autobet-wt)
    NOT pushed.

### TESTS (BLM_ALLOW_HEAVY_TESTS=1, named files)

    tests/test_autobet_command_api_2026_10_03.py          34 passed (new)
    betting/autobet suites (7 files, incl. rung rule/stake) 309 passed
    payload/frontend (5 files)                              87 passed
    test_dashboard_command_center + market_line_selection   36 passed
    test_v4_api                                             24 passed
    ad-hoc /tmp/hermes-verify-autobet-command-2026-10-03.py  PASS

### SCOPE / NOT DONE

No collector, event_parser, result_reconciler, scorecard, settle_worker,
wal_hygiene, server.py or watchdog touched.  No alert / settlement / trigger-%
logic.  No restart.  R2.00 real-money test NOT run.  Production NOT deployed.
BLOCKED at the browser/extension + provider-transport boundary — the remaining
links (HTTP command channel into the extension, the PokerBet DOM contract, the
real provider transport) cannot be built or proven without a controllable
authenticated PokerBet browser.

## 2026-10-03 06:35 UTC — UNRESULTED POPULATION AUDITED + BACKFILLED. fired-but-unresulted 154 → 63. NOT COMMITTED, NO RESTART.

Directive: find and eliminate every remaining unresulted game. Data-only repair;
no service restarted (blm-server PID 261630, start 06:02:11Z, NRestarts 0 — the
same process the previous fix activated).

### THE MODEL (why there is nothing to "un-PENDING" directly)

Alerts are DERIVED, not persisted. The legacy `alerts` tables (`blm.db`,
`blm_v2.db`) are EMPTY; v4 computes every alert's verdict on the fly via
`under_outcome.under_alert_outcome` from `snapshots`/`clean_projections` + the
settled `game_results` row, and serves it through `/live` and
`/api/v4/alert-outcomes`. `outcome_status(trigger, final)` is None if EITHER
operand is None. **A RESULT PENDING row is therefore always a MISSING FINAL.**
The repair can only be: recover the final into `game_results`.

### AUDIT (before) — 06:06Z

    game_results      : OK 18,642 | NEEDS_RECONCILIATION 6,109 | INVALID 1,099
                        | UNKNOWN 10
    unresolved (non-OK, non-INVALID)        : 6,119
    of those, completed-zone (reached >=75%) : 3,975
    with NULL final                          : 6,028
    with a derivable final (cosmetic only)   : 1
    no snapshots at all (orphan)             : 80
    FIRED the alert but had no result        : 154  (96 stranded >24h)

### FEED COVERAGE (the ceiling)

All 6,109 ids probed through ONE batched `SwarmResultsClient` session:
complete 3,473 | partial (in play) 13 | absent 2,623. So the authoritative
source could supply at most 3,473 finals; 2,623 are a real ABSENCE (never
fabricated).

### BACKFILL — `scripts/backfill_resulted_finals_2026-10-03.py`

Drives the reconciler's OWN `candidate_games` / `_attach_history_bounds` /
`_apply_swarm_result`; no second classification, no Playwright (swarm only).
10 rounds over ~6,900 candidates:

    ADDED to OK      : 2,065 rows (all result_source=RESULTS_PAGE)
    OK ids LOST      : 0
    rejected         : ~2,300 canonical refusals (see below)
    unresolved       : 6,119 -> 2,594

### WHY A REPLY IS REJECTED (correct fail-closed, not a defect)

`_apply_swarm_result` re-checks IDENTITY, not just the render: the feed answers
for the BASE id of a virtual fixture, but the game may be an `#iN` instance
(`instance_splits`: the same id replayed after a score drop). A base result
cannot be attributed to one instance, so it is refused. Those ids keep their
result_source NULL rather than take a foreign instance's score.

### AFTER (06:33Z)

    game_results : OK 22,258 | NEEDS_RECONCILIATION 2,092 | INVALID 1,075
                   | UNKNOWN 502
    unresolved   : 2,594  (was 6,119)
    FIRED but   result-less: 63 (was 154) — 45 BETUAL_NBA, 18 CYBER_2K26
    recovered verdicts (per checkpoint, canonical rule over the FULL stream):
        UNDER 2,641 | OVER 3,161 | PUSH 0
    still unresolved, by missing link:
        A  finished game, no provable final : 0   <-- the defect is GONE
        D  never reached the alert zone     : 2,590
        C  no snapshots (orphan)            : 19
    feed-absent among the remainder         : 1,435 of 2,609

### ROOT CAUSES OF THE REMAINING 63

1. FEED ABSENCE (dominant). The authoritative results feed holds no entry for
   the id — an observation of absence, not a failure. 1,435 of the remaining
   unresolved ids are feed-absent; unrecoverable from the only authoritative
   source. The live worker re-arms hourly, so a late publication is still caught.
2. NEVER IN THE ALERT ZONE (2,590). Never reached 75%; no alert could fire, so
   these are not user-visible PENDING alerts.
3. ORPHAN IDS (19). No snapshots at all.
4. Separately: the NO-LINE family (line unprovable at the boundary) is NOT
   resolved by design — the directive forbids resolving without a known line.

### CHURN (pre-existing; reported, NOT caused by the backfill)

`settle_worker.py:131` selects `final_result_status='NEEDS_RECONCILIATION'` and
its upsert has NO `!= 'OK'` guard, so the label cycles
NEEDS_RECONCILIATION <-> UNKNOWN. Reconciler-written OK rows carry
`result_source='RESULTS_PAGE'` and are excluded from settlement (line 137), so
no verified final is at risk from this path. Measured: the id-level OK set is
stable (0 ids left OK in a 150 s no-backfill control); OK TOTALS do get
corrected in place (~150 pairs/90 s burst, the documented M007-M8 path).
3 pre-existing OK rows moved to INVALID during the run with their totals KEPT
(223/236/179) — a status change, not a data loss.

SECOND CHURN PATH (found in the post-repair verification): of the ids the
backfill VERIFIED, 74 were later flipped OK -> INVALID by the settle worker's
`_snapshot_history_quality` gate (instance-history games). ALL 74 KEPT their
final_total (74/74 non-null) — the status changed, the DATA did not. This is
the same guard-vs-write race noted above: `_settle_game` reads provenance at
line 219 and only then may write INVALID, so a row the reconciler wrote
between the read and the write can be reclassified. It is PRE-EXISTING (the
live reconciler worker races the settle worker identically); the backfill only
raised the write rate. Net effect on the repair: the invariant "no final is
lost" HOLDS.

### TESTS

`tests/test_resulted_backfill_2026_10_03.py` — 13 new (the 7 required cases +
clobber safety + canonical-rule identity). Focused suite: **218 passed, 0 failed**.

### CAVEATS (ranked)

1. The feed answers a ROTATING subset per call; the 63 are unrecoverable NOW,
   not necessarily forever. More rounds converge (rounds 6-10 verified 0).
2. Counts drift: the pool moves between NEEDS_RECONCILIATION and UNKNOWN.
   Every number above is a timestamped snapshot.
3. `fired_but_unsettled` filters on NEEDS_RECONCILIATION only; at the time of
   the after-run that label held 99% of the pool, so 154 -> 63 is comparable
   (residual mismatch <= 25 rows).
4. My verdict distribution does not pass `projection_rows`, so the per-checkpoint
   NO-LINE counts are an UPPER bound; the API may prove some from
   clean_projections.

## 2026-10-03 06:02 UTC — RESULTED-ALERTS-STUCK-PENDING FIXED & ACTIVATED. blm-server RESTARTED (operator-authorized). NOT COMMITTED.

Q3 BREAK alerts triggered and ended correctly but stayed RESULT PENDING
indefinitely. Proven live on two games: BETUAL_NBA `31101100` and CYBER_2K26
`31093644`. TWO independent upstream causes — the verdict rule was never broken.

### THE BROKEN LINK

`alert -> game_id -> completed game -> final actual total -> triggered line ->
classification -> persistence`. Every link held except the FINAL.
`outcome_status(trigger, final)` returns None whenever EITHER operand is None,
so a NULL final is a permanent PENDING. The final was missing for two reasons.

### BUG 1 — `max_attempts` was a TERMINAL cap on a LIVE, IMPROVING source

`ResultReconciler.candidate_games` admitted a REJECTED/FAILED_ATTEMPT game only
while `attempt < 3`. The rejection reason is `render_complete=false` — the feed's
render AT THAT MOMENT (game still in play, 1–3 quarters), which
`result_policy.validate_page_result` correctly rejects. That is a TEMPORAL
statement, but the count cap made it PERMANENT: minutes later the same feed
publishes the complete four-quarter final, and it was never ingested. Scale:
**2 486 games permanently exhausted** (2 417 BETUAL_NBA / 69 CYBER_2K26);
7 233 ended games lack an OK final. (CORRECTION to the earlier note here: the
per-id path does NOT inherently return a partial render — re-measured, both
`fetch_results([id])` and `fetch_results_day` return COMPLETE finals for exactly
the rejected ids once the game has finished.)

Fix: `RETRY_COOLDOWN_S = 3600.0` (new module constant, threaded through
`ResultReconciler(retry_cooldown_s=...)`). The candidate SQL gains
`OR (st.outcome IN ('FAILED_ATTEMPT','REJECTED') AND st.updated_at <= ?)`;
`retry_cooldown_s=0` restores the old terminal cap. `TEMPLATE_FAILED` stays
terminal (SPA-cached render — a retry re-reads the same page). Rate-limited by
the pass interval × batch_limit; self-draining once the game settles OK.

### BUG 2 — the fixture-identity window used REGULATION as WALL CLOCK

`_fixture_start_consistent` bounded the page's fixture start as
`last_seen - regulation - grace <= page_start`. Regulation is PLAYING time; a
virtual fixture's wall-clock span additionally carries pre-roll, inter-quarter
breaks, stoppages and the scheduled slot. Measured live:

| game | observed span | regulation | page_start−last_seen | old window | old |
|---|---|---|---|---|---|
| 31101100 BETUAL_NBA | 2 915 s | 2 400 s | −2 930 s | −2 700 s | REJECT |
| 31093644 CYBER_2K26 | 4 940 s | 2 880 s | −4 323 s | −3 180 s | REJECT |

Both were our OWN fixture, declared "an earlier replay that had already
finished". Consequence: CYBER_2K26 final coverage **11.8 %** vs BETUAL_NBA
**80.3 %** (4/34 vs 151/188 on 2026-10-02).

Fix: the lower bound's duration is `max(regulation, observed span)` where
observed span = `last_seen − first_seen`. The anti-neighbour property is
preserved AND tightened — a same-teams replay that had already finished before
we last saw ours started before WE did, so it is still rejected. A game we
observed for less than its regulation keeps the regulation basis (no weakening).

### FILES

    M blm_v4/result_reconciler.py                        +75 / −4
    ? tests/test_resulted_alert_settlement_2026_10_03.py NEW, 22 tests

Untouched: `outcome_status` (the verdict rule), `result_policy`, alert logic,
BLM scoring, betting, `settle_worker`, quarter-market parsing.

### TESTS

    tests/test_resulted_alert_settlement_2026_10_03.py      22 pass
    alert/result suite (integrity, under_alert, settle,     208 pass
      replay, scorecard, quarter, status)
    result/alert remainder (reconciliation, page            193 pass
      authority, outcome, swarm, colour, correction)
    ────────────────────────────────────────────────────────────
    TOTAL (pre-activation)                                  423 pass, 0 fail
    POST-ACTIVATION focused re-run (new + integrity +        111 pass
      reconciliation + settlement_semantics + outcome)

Covers ACTIVE→ENDED→PENDING→UNDER **and** →OVER **and** →PUSH; NBA **and**
Cyber; a missing final stays PENDING (never invented); idempotency (5 extra
passes leave the row byte-identical). Mutation-proven: reverting the retry
clause fails 3 re-arm tests; reverting the identity bound fails the
wall-clock test.

### ACTIVATION (this restart)

    pre : blm-server PID 1653,   started 2026-10-02 17:07:11Z, NRestarts 0
    post: blm-server PID 261630, started 2026-10-03 06:02:11Z, NRestarts 0
          module mtime 2026-10-03 05:46:11Z   (process started AFTER the fix)
          sha256 blm_v4/result_reconciler.py =
            dd6cc70f352726e1acbd3928494ec81deab648d10de4c7e20060d2a5c810d268
          blm-collector NOT restarted (PID 249616 unchanged, directive §12)

THREE INDEPENDENT PROOFS THE RUNNING PROCESS HOLDS THE FIX:
1. **Direct frame inspection** (`py-spy dump --pid 261630`; ptrace_scope=1 so
   under sudo): thread `blm-result-reconciler` loop frame at
   `result_reconciler.py:1427`. Line 1427 exists ONLY in the fixed file —
   HEAD's file is 1 367 lines and that statement sits at line 1356 there.
2. **`/proc/261630/mem` string scan** (671 MB scanned): every fix-only needle
   FOUND (`st.updated_at <= ?`, `RETRY_COOLDOWN_S`, `_retry_cutoff`,
   `span_s = max(full_min * 60.0, obs or 0.0)`, the RE-ARM docstring); the
   OLD-only needle (`if -delta > full_min * 60.0 + _FIXTURE_GRACE_S:`) ABSENT.
3. **Behavioural** (below) — `attempt >= 4` is structurally unreachable
   pre-fix.

Health: active/running, `/healthz` 200 ×3, `NRestarts` still 0 after activation.

### PRODUCTION VERIFICATION (live, 18 s after restart)

    pass 1 @ 2026-10-03T06:02:29Z
    31093644 -> result_reconciliation_state = VERIFIED (attempt 0)
                game_results: final 217, status OK, source RESULTS_PAGE
                alert verdict: RESOLVED, Q3_BREAK line 199.5 -> OVER
    31101100 -> game_results: final 265, status OK
                alert verdict: RESOLVED, Q3_BREAK line 269.5 -> UNDER
                (already OK pre-activation — it was never actually stuck)

BEHAVIOURAL COUNTER: `COUNT(*) FROM result_reconciliation_state WHERE
attempt >= 4` went **0 → 6** in the first pass. Pre-fix the cap admits only
`attempt < 3`, so any row at `attempt >= 4` is impossible under the old code —
the strongest available proof the new query is executing.

COOLDOWN SEMANTICS (live):
    [9]  REJECTED/FAILED at attempt ≥ 3 INSIDE the 1 h cooldown: 56 rows —
         admitted by the candidate WHERE clause: **0** (no re-attempt inside
         the cooldown ⇒ no request storm).
    [9]  OUTSIDE the cooldown (re-armed): 3 576 rows.
    [10] rows at attempt ≥ 4 (an expired cooldown DID re-arm): 6.
    [11] all 6 re-armed rows carry a real observed first_seen<last_seen window;
         the two cited games' spans (2 915 s / 4 940 s) exceed their regulation
         (2 400 s / 2 880 s) and are ACCEPTED by the real helper where the old
         bound REJECTED.

Backlog: 4 173 old-clause candidates + 2 868 re-armed = 7 041. The live server
re-armed and cleared the cited CYBER game in its FIRST pass.

### RANKED CAVEATS

1. The re-armed set is now effectively UNBOUNDED IN TIME — it retries until a
   game settles OK. Bounded per pass (25) and by the cooldown, but a game whose
   feed never publishes a complete final is retried indefinitely at ~1
   attempt/hour. Intended (never a permanent NO FINAL), but it is a permanent
   low-rate load. Knobs: `BLM_RESULT_RECONCILE_INTERVAL_S` / `_BATCH`.
2. The 1 h cooldown is a heuristic: too short costs feed load, too long delays
   late finals. `retry_cooldown_s=0` disables re-arming entirely.
3. The identity fix assumes our observed window is a LOWER bound on the real
   fixture duration; a partially-observed game keeps the regulation basis —
   verified, no weakening.
4. `blm-collector` watchdog flap is UNFIXED and NOT from this change: it
   self-restarted at 05:35:25Z (NRestarts 0→1) on `page.content()` overruns of
   30–74 s on the CDBL event view — the same pre-existing defect recorded on
   2026-10-02.
5. Uncommitted: HEAD is unchanged; the fix is working-tree only (push remains
   the operator's separate decision).

### SCOPE

Daemon-only restart of `blm-server`. No alert threshold, gate, colour rule or
bet logic touched; an OK verdict remains immutable (idempotency pinned).

## 2026-10-03 05:15 UTC — QUARTER-SCORE CAPTURE FIXED + PANEL-DISCOVERY COVERAGE FIXED. COLLECTOR RESTARTED (operator-authorized). NOT COMMITTED.

Two independent root causes, both fixed and verified live. Production collector
restarted once at **05:10:16Z** to load them; `blm-server` UNTOUCHED (PID 1653,
started 2026-10-02 17:07:11Z).

### BUG 1 — `quarter_score_observations` was 0 rows for the table's whole life

Root cause was NOT the INSERT (`storage.insert_quarter_score_observation`) nor
its caller (`collector._capture_event_state` → `_record_quarter_scores`) — both
intact. It died at the **identity guard**. Commit `36d7538` (2026-09-30) made
`parse_event_view` take teams/score/quarters from the SELECTED sidebar section,
identified by **HTML class attributes** (`market-game-section active`). Every
caller feeds `page.inner_text("body")` — rendered text with NO markup — so the
section regex could never match, teams parsed `""/""`, and `_verified_event_view`
rejected every capture. The quarter writer sits BELOW that guard ⇒ never reached.
Snapshots kept flowing via the WS→snapshot bridge, so the pipeline looked healthy
while the table stayed empty.

Fix (smallest): `parse_event_view(text, identity_html=None)` — identity from the
selected section's MARKUP, market token stream still from `text`. Collector reads
it with a single-element DOM call (`_selected_section_html`:
`page.locator('[class*="market-game-section"][class*="active"]').first.evaluate("el => el.outerHTML")`),
deliberately NOT `page.content()` (the whole-page round-trip that overruns its
tick budget). Fail-closed when a section is present but has no scoreboard.
Pins: `tests/test_quarter_score_capture_identity_2026_10_03.py` (17 tests).

### BUG 2 — panel discovery processed ONE ROW PER COMPETITION (coverage cap)

Commit `0561ea2` (2026-09-27) **re-indented** the per-row discovery block OUT of
`for row in comp.games:` up to the `for comp` level, so canonicalisation /
`seen_keys` / `_queue_resolve` / snapshot-queue ran once per competition on the
leaked last `row`. Capped tracked coverage at the competition count and made
`_mark_ended` close the other ~34 panel rows as "vanished" every tick.
Proof: `git show 0561ea2^:blm_v4/collector.py` → block at indent 24 (inner loop);
`git show 0561ea2:...` → indent 20 (comp loop).
Live signature: panel had **40 relevant rows / 6 competitions** while the running
collector reported `games_tracked=6`, `pending_resolve=1`.

Fix: extracted `_discover_panel_rows(comps) -> (seen_keys, to_snapshot)` with the
block correctly nested in the inner loop; `_tick_body` consumes both return
values. Pins: `tests/test_panel_discovery_coverage_2026_10_03.py` (7 tests),
mutation-proven (fixed 7/7 pass → reintroduced bug 4 fail → restored).

### ACTIVATION (operator-authorized; `systemctl --user restart blm-collector.service`)

| | before (old module) | after (verified module) |
|---|---|---|
| ExecMainStartTimestamp | 03:41:26Z | **05:10:16Z** |
| MainPID | 198770 | **237737** |
| games_tracked | 6 (hours) | **13 and climbing** |
| pending_resolve | 0–1 | **23–30 (drains as resolved)** |
| games_resolved since start | — | **10 in ~5 min** |
| quarter score_observations | 378 (hours) | **21 in ~5 min** |
| qso rows / distinct games (DB) | 395 / 13 | **428 / 32** |
| ticks with errors>0 | 0 | **0** |

`slow_worker` healthy (resolve_ok 10, resolve_failures 0). Module sha256[:16]
`bd86a42af9bcafc0` unchanged by activation. Runtime proof the running process has
the fix: `pending_resolve` in the 20s–30s range is UNREACHABLE under the regressed
code (cap = competition count ≈ 6).

### VERIFICATION

- Targeted pytest pre-activation: **250 passed** (0 failed).
- Targeted pytest POST-activation: **100 passed** (0 failed).
- Ad-hoc independent check (not suite green): `/tmp/hermes-verify-panel-discovery.py`
  **18/18 PASS**, exit 0 — AST-proves the per-row call nesting, compares behaviour
  against a hand-written reference, and asserts the regression shape is absent.
  (Cleanup denied by the approval layer; script left in /tmp.)
- Live-panel dry run: 40 panel rows → 40 resolve requests (vs 6 regressed).
- Full suite NOT run — the conftest guard refuses heavy workloads on this host and
  `blm-dev/` has a pre-existing ImportPathMismatchError; per directive it was out
  of scope. Any "full suite" artifact from this session is INVALID (interrupted).

### Files (this session; NOT committed, NOT pushed)

```
 M blm_v4/collector.py                                   (both fixes)
 M blm_v4/event_parser.py                                (BUG 1)
 M tests/test_m009_market_capture.py                     (parser signature)
?? tests/test_quarter_score_capture_identity_2026_10_03.py   (new, BUG 1)
?? tests/test_panel_discovery_coverage_2026_10_03.py         (new, BUG 2)
```

### NOT DONE / RISKS

1. **Nothing committed or pushed.** Dirty tree (73 entries incl. other owners'
   work) preserved untouched — no reset/stash/clean/checkout at any point.
2. **Resolution throughput unproven at full panel size.** RESOLVE_BATCH=2 per
   worker wake against ~40 rows; watch whether `pending_resolve` drains or stalls.
3. **Panel-bounded coverage remains.** Games deep in-play that rotate off the
   panel are still undiscoverable — yet they remain in the WS feed. The WS frames
   DO carry `info.additional_data.quarterScores` (`[{quarterNumber, score:{team1,team2}}]`)
   for EVERY listed game, contradicting the `_record_quarter_scores` docstring
   ("WS frames carry no quarter scores — audited 2026-09-22"). Wiring that path is
   the next coverage lever.
4. **Watchdog flapping.** `page.content()` overruns its 3.0s budget (observed
   3.9s–18.1s); the self-watchdog fired repeatedly before this restart
   (NRestarts reached 6). Documented, NOT fixed, NOT worked around — do not raise
   timeouts to hide it.
5. **`blm-dev/blm_v4/collector.py` carries the identical latent indentation bug.**
   Separate dev copy; deliberately untouched (out of scope).
6. `quarter_validation.check_quarter_regression` flags the IN-PROGRESS quarter
   (~7 flags in 5 min). Flag-only, nothing rewritten; pre-existing validator
   semantics, newly visible now that rows exist.

**Next agent**: watch `games_tracked` settle toward the panel size (~40),
`pending_resolve` drain, and `quarter_score_observations` accumulation. Then
consider the WS `quarterScores` path for panel-independent coverage.

## 2026-09-30 19:50 UTC — PR #3 DEPLOYED + T+30 VALIDATED (capture-efficiency phase COMPLETE)

Operator authorized the merge + restart. Executed as one transaction:

1. **Pre-flight PASS**: prod HEAD `d50b230`, 63 dirty owner paths intact,
   merge-base == `d50b230`, PR tree clean, file set == reviewed 6 files
   (+1327/−2), zero overlap with dirty paths, both units healthy.
2. **Merge**: `git merge --ff-only capture-eff-20260930` on
   `handoff-2026-09-07` → production HEAD now **`f174ade`** (== PR #3 head).
   Dirty count unchanged (63). Owner work untouched.
3. **Restart** 19:16:24Z: `systemctl --user restart blm-collector blm-server`
   (systemd --user). New PIDs: collector 206959, server 207231. NRestarts=0
   both. Old-process shutdown noise (blm_v1 TargetClosedError during teardown)
   expected; 0 errors from new processes.
4. **T+30 AFTER vs BEFORE** (n=206 ticks, window 19:16:38→19:48; full detail in
   `analysis/deploy_validation_2026-09-30.md` on `review-evidence-20260930`):
   - tick P50 2.47s → **1.44s**; P95 9.54s → **4.65s**; MAX 54.9s → **28.1s**
   - ticks > 3.0s page budget 42.1% → **16.5%** (−61%)
   - line-present ~27% → **100%** (1077/1077, all MatchTotal)
   - post-final snaps ≤24/day → **~2 real** (4–5 rows incl. boundary rows;
     the raw 198 was a join fan-out — see artifact note)
   - NULL-score 13.3% (held); checkpoint pipeline writing again (22 rows);
     780 snapshots / 9 games; healthz ok; heartbeat fresh.
5. **Honest caveats**: `null_score_recovery` attempts=0 and
   `ws_lifecycle_suppressed`=0 in window (nothing eligible; all 47 endings
   took the disappearance path, which now writes 0 post-final snaps);
   slow-path `slow_event_view_ms` P95 6.7s / max 35.3s and one 28s tick /
   24.9s persistence stall remain — overruns down but not zero.
6. **Alert integrity**: no alert-logic file modified at any point; post-merge
   tree == reviewed head; no alert-formula changes during validation.

**Next agent**: monitor capture metrics (next-day line-present%, NULL-score%,
tick P95, counter activity when WS-DONE-before-board cases occur); do NOT
modify alert formulas during the validation period. Legacy `blm_v1`
post-final writes are pre-existing and out of scope.

### Git State
Branch: `handoff-2026-09-07` (production working tree) @ `f174ade`
Commit: `f174adeb15723ae924953c26c9f24d0cec6ad724` (== PR #3 head)
Remote: `git@github.com:abwarren/BLM.git` (SSH)
Push status: PUSHED OK (2026-09-30 ~19:55 UTC) — `d50b230..f174ade` fast-forward
accepted by origin; branch now IN SYNC with remote. Evidence branch
`review-evidence-20260930` pushed at `425f4c0` (deploy-validation artifact).
Working tree: 63 dirty owner paths preserved, untouched.
Uncommitted: owner work + untracked STATUS.md/AGENTS.md (by convention).
Next agent should start from: post-deploy monitoring; PR #3 deployed+validated.

## 2026-09-30 (night) — MERGE-READINESS VALIDATION COMPLETE: MERGE: READY (recommendation only — authorization still with operator).

Six-task validation of PR #3 before authorization. NO merge/deploy/restart/DB
write; alert logic untouched throughout.
1. **capture-efficiency suite**: 23/23 passed (re-run).
2. **Capture-efficiency metrics (BEFORE — PR not deployed, no 'after' exists)**:
   `analysis/data_quality_baseline_2026-09-30.md` — per-day: NULL-score 56%
   (09-24/25) → ~13% (09-27→30); line-present% COLLAPSED 82% → ~27% (09-28+);
   post-final waste up to 24 snaps/day; ticks P50 2.47s / P95 9.54s / MAX 54.9s,
   **42.1% of ticks exceed the 3.0s page-capture budget**. These are the numbers
   the post-deploy re-run must beat.
3. **Historical accuracy-vs-capture-health**: over 14,614 settled triggers —
   LIVE 88.1% UNDER vs STALE 79.3%; market age MONOTONIC: <30s 88.7% /
   30-120s 87.5% / >120s 79.7%. Degraded accuracy DOES correlate with stale
   captures → the PR targets the right defect. Alert math untouched.
4. **Full regression suite, branch AND base** (3-chunk, ~12 min each):
   branch 1904 passed/20 failed/4 skipped; base d50b230 (fresh scratch
   worktree) fails the IDENTICAL 20 by name. **New failures from the PR: 0**;
   branch additionally passes 23 new tests. All 20 documented as
   pre-existing/environmental in STATUS (below + dossier).
5. **Four new collector methods audited**: NONE touches store/clean_metrics
   writes (only _progress_tier reads, one indexed SELECT); they feed scheduling
   order and logs only — alert inputs, required-rate, fingerprints, checkpoint
   semantics, final-result calc all unreachable from them (consistent with the
   blob-hash + AST proofs of 2026-09-30 evening review).
6. **Evidence persisted WITHOUT touching the reviewed head**: branch
   `review-evidence-20260930` (f174ade + ffbc64a, pushed) carries the AST tool,
   the baseline script and the baseline artifact; **capture-eff-20260930 stays
   frozen at f174ade** == PR #3 head (mergeable/clean re-confirmed after the
   work).

**MERGE: READY** (agent recommendation; explicit operator authorization still
required — production runs FROM the working tree, merge must be paired with a
restart window). Post-merge verification checklist: re-run
scripts/review_data_quality_baseline_2026-09-30.py → 'after' table A must show
post-final snaps → ~0, line-present% recovering, and tick P95 down; ws_
lifecycle_suppressed / null_score_recovery counters visible in state.

## 2026-09-30 (evening, 2nd) — PR #3 FINAL REVIEW COMPLETE. ALL SECTIONS PASS. MERGE AUTHORIZATION: WAITING.

Directive: review and validate PR #3 (abwarren/BLM/pull/3,
capture-eff-20260930 ← handoff-2026-09-07) before any production merge. No
merge, no deploy performed.

**1. Repository state (verified twice, start + end of review)**
- production HEAD d50b230 (unchanged; branch reflog shows no reset; stash empty)
- production working tree: same 63 dirty paths, same 17 tracked-modified owner
  files — untouched
- branch/origin/PR head = f174ade (all three equal); merge-base = d50b230 =
  production HEAD → PR based directly on the recorded production HEAD (no STOP)
- PR #3 via GitHub API: state=open, **mergeable=true, mergeable_state=clean**,
  draft, 2 commits, 6 files, +1327/−2
- no reset/clean/restore/stash used anywhere

**2. Complete PR diff reviewed** (6 files incl. docs/PR dossier; API file list
matches local `git diff --name-status` exactly). Sensitive-token search over
all ADDED lines, every hit classified — all are prose/imports/test
assertions: REQUIRED_MARGIN (imports + analysis formula + dossier prose,
NEVER re-declared), ALERT_PROGRESS_PCT (test assertion only), duration_for
(import + read-only call + test assertions), fingerprint/under_alert (prose
+ imports), final_result (one SQL WHERE literal `final_result_status='OK'`),
systemd/watchdog/deploy (analysis prose only). ZERO hits: CREATE/ALTER/DROP
TABLE, no config changes. No unrelated modifications.

**3. Alert logic proven identical — two independent methods**
- blob-hash identity base d50b230 vs head f174ade: under_alert.py,
  under_fingerprints.py, fingerprint_stats.py, benchmark.py, projection.py,
  pace_projector.py, ws_market.py, clean_metrics.py, scorecard.py,
  storage.py, terminal_eligibility.py — **ALL IDENTICAL** (byte-level)
- AST function-level diff of blm_v4/collector.py
  (scripts/review_collector_ast_diff_2026-09-30.py, left untracked so the
  reviewed head stays exactly f174ade): 99→103 functions, **90 identical,
  0 removed**, 9 changed = the 9 documented insertion points (init, tick_body,
  write_state_impl, ingest_ws_observation, mark_ended, end_game,
  store_list_snapshot_impl, capture_slow_market, module _tick_timing_summary),
  4 new = the four directive methods (_recover_null_scores, _progress_tier,
  _full_game_minutes, _sort_market_queue_checkpoint_aware).
  Alert-adjacent functions (_capture_event_state, _is_final_state,
  _reconcile, _detect_event_reset, _detect_instance_reset,
  _verified_event_view, _infer_status): **ALL SOURCE-IDENTICAL**.

**Tests**: unchanged from the Phase 4 gate (458 passed / 2 skipped / 4 failed;
all 4 failures independently reproduced at base d50b230 — 3 vocabulary scans
+ 1 worktree-missing-DB OperationalError that passes 31/31 with
BLM_POKERBET_DB set).

**MERGE AUTHORIZATION: WAITING.** No merge, no deploy, no restart, no DB
write. Production untouched throughout.

## 2026-09-30 (evening) — PR REVIEW OF capture-eff-20260930 COMPLETE. MERGE GATE: WAITING. NO PRODUCTION ACTION.

Directive: review commit f86e0a5 against the production branch, create the PR,
review the actual diff, run the test gate, record owner-work safety — then STOP
at the merge gate. All phases done.

- **PR**: GitHub PRs cannot be created from this host (no gh CLI, no API
  token/credential helper — verified). Delivered as the tracked compare URL
  (base handoff-2026-09-07 ← head capture-eff-20260930; the page carries the
  prefilled "Create pull request" action) plus a committed PR dossier on the
  branch: `docs/PR_capture_eff_2026-09-30.md` (dossier commit **f174ade**,
  branch pushed, origin == local verified).
- **Phase 2 audit**: 5 files (+1240/−2 then +87 dossier); ONLY removed lines in
  the whole diff are the two timing-tuple reformats; every collector.py hunk is
  a pure insertion except those. No alert formulas, REQUIRED_MARGIN,
  ALERT_PROGRESS_PCT, duration_for, fingerprints, market-line/result logic, DB
  schema, deploy/*.service or watchdog constants appear as modifications
  (sensitive-token grep over the full diff: only imports + test assertions +
  prose).
- **Phase 3**: DONE-writer audit (exactly 2: `_end_game` verified-final,
  `_mark_ended` disappeared — no live path can mark DONE); tier-1 freshness
  exemption is scheduling-only; `_page_content_timed` untouched (def 2613,
  no hunk intersects 2613–2700).
- **Phase 4 test gate (branch worktree, BLM_ALLOW_HEAVY_TESTS=1)**: 28 suites —
  **458 passed / 2 skipped / 4 failed**; all 4 failures independently verified
  at BASE d50b230 in a fresh scratch worktree: the 3 vocabulary scans
  (identical failure set incl. audio/frontend-migration/historical-alert) AND
  test_live_alert_eligibility::test_live_payload_exposes_alert_gate_for_every_game
  = `sqlite3.OperationalError: unable to open database file` (api.py:204) —
  environmental (scratch worktrees have no blm_pokerbet.db); WITH
  BLM_POKERBET_DB set the branch passes **31/31**.
- **Phase 5 recorded**: production HEAD d50b230 (working tree: 63 dirty paths —
  17 tracked-modified owner files incl. scorecard.py/v1 collector.py/
  watchdog.sh + untracked; UNTOUCHED by this session), branch HEAD f174ade,
  merge-base = d50b230 (fast-forward-able, no conflicts). No reset/clean/
  restore/stash used anywhere.
- **MERGE AUTHORIZATION: WAITING** — do not merge/deploy/restart without the
  operator's explicit go (production runs FROM the working tree; merge must be
  coordinated with a restart window).

## 2026-09-30 (PM) — CAPTURE-EFFICIENCY DIRECTIVE: PHASES 1–7 IMPLEMENTED IN TEST WORKTREE. NOT DEPLOYED. PRODUCTION UNTOUCHED.

Directive: "FIX CAPTURE EFFICIENCY, DO NOT ALTER ALERT LOGIC" — TEST ENVIRONMENT
ONLY. All code lives on branch **`capture-eff-20260930`** in worktree
**`/home/ubuntu/blm-test`** (HEAD d50b230 + 1 commit). The production working
tree at `/home/ubuntu/BLM` was NOT modified by this work (owner dirty files
untouched; no restart, no deploy, no DB writes anywhere).

### PHASE 1 — BASELINE (read-only; `analysis/capture_baseline_2026-09-30.md`)
- HEAD d50b230; **production topology CHANGED ~13:20 UTC**: units moved from
  system → **systemd --user**. `blm-collector@--user`: PID 105593 from 13:20:35,
  WatchdogUSec 90s, **NRestarts=3 — IS increasing** (watchdog ABRT kills 13:00:40
  + 13:20:24 today; pre-existing, none since). blm-server@--user PID 9520,
  NRestarts=0. No QEMU running. load ~11, PSI io some 37% — I/O-bound.
- Ticks (695, 13:20→15:20): P50 1.68s / P90 6.76 / P95 8.64 / P99 13.72 /
  MAX 50.5; 184× `page.content() OVERRAN` 3.0s budget in-window (clusters on
  single games, e.g. 31072240).
- NULL-score snapshots **13:20→15:20: 15.1%** (358/2369). CAUTION: snapshots.
  captured_at is ISO-T format — SQLite `datetime('now')` comparisons silently
  match nothing; use ISO string compare.
- Ended-game waste ROOT-CAUSED (Phase 3 evidence): 31075046 took **294
  snapshots in the ~31 min AFTER its result_at**; the WS→snapshot bridge had NO
  terminal-status guard (the DOM paths already did).

### PHASES 2–6 — CODE (worktree branch `capture-eff-20260930`, 1 commit)
`blm_v4/collector.py` (+308/−2) + `tests/test_capture_efficiency.py` (23 tests):
- **P2 instrumentation**: 9 new tick timing buckets (tick_total/game_discovery/
  page_content/score_parse/board_parse/line_parse/db_write/retry/ended_game,
  ms deques) recorded in `_tick_body`/`_store_list_snapshot_impl`/
  `_capture_slow_market`; `_tick_timing_summary` exposes P50/P95/P99/MAX per
  bucket in the state payload (n/means preserved for continuity).
- **P3 lifecycle LIVE→FINALIZING→DONE**: `_end_state_terminal` cache populated
  by `_end_game` (verified final) and `_mark_ended` (disappeared);
  `_ingest_ws_observation` suppresses DONE games (FINALIZING = final-capture
  window still accepts frames); counter `ws_lifecycle_suppressed` in state.
  DOM paths unchanged (already guarded).
- **P4 bounded NULL-score recovery**: `_recover_null_scores` — max 2 re-reads/
  tick (NULL_SCORE_RECOVER_MAX_PER_TICK), 1 per game, budget-aware (skips when
  page-capture budget ≤0), backoff 30 ticks + INCOMPLETE marker after 3 failed
  episodes; recovered rows replace NULL rows pre-persist (semantics unchanged);
  `null_score_recovery` counters in state.
- **P5 checkpoint-aware scheduling**: `_progress_tier` (1 checkpoint-critical
  within 2 game-min of the NEXT alert boundary ≥50%, 2 active-valid <40s,
  3 normal, 4 stale >40s, 5 finalizing, 6 DONE; defaults 3 on error) orders the
  market-rotation queue (`_sort_market_queue_checkpoint_aware`, never-line
  tier 0 preserved); tier-1 games exempt from the freshness gate.
  NO checkpoint definition touched (ALERT_PROGRESS_PCT/REQUIRED_MARGIN/
  duration_for pinned by test).
- **P6 Playwright safety**: `_page_content_timed` untouched; 4 regression
  tests pin same-thread execution, no worker threads, overrun-flags-never-
  raises, and the source-level no-threading/SIGALRM contract.

### PHASE 7 — ACCURACY (read-only; alert algorithm untouched)
`scripts/analysis_market_status_by_competition_2026-09-30.py` →
`analysis/market_status_by_competition_2026-09-30.md`. Validated trigger
reconstruction (same cohort as c2_forensic_backtest_2026-09-23; REQUIRED_MARGIN
IMPORTED = 1.04): **14,570 settled triggers**.
**HEADLINE: within EVERY competition, LIVE UNDER% > STALE UNDER% by 7–11pp**
(NBA 90.7 vs 81.8; EuroLeague 87.7 vs 79.1; TBSL 86.1 vs 77.9; KBL 84.7 vs
73.4; CBA 88.6 vs 80.8; Cyber 86.5 vs 79.4). The earlier aggregate
"STALE>LIVE" gradient was a COMPOSITION effect (heavy STALE weight in W36–W37,
the low-UNDER weeks). By checkpoint: 75-80 86.1%, 80-90 92.0%, 90-100 95.8%
(UNDER rises toward the buzzer). Gap gradient persists >60s 90.0% vs 30-60s
85.1% (provider re-pricing at the Q3 boundary remains the structural story).

### VERIFICATION (all in the worktree, BLM_ALLOW_HEAVY_TESTS=1)
- tests/test_capture_efficiency.py **23 passed** (new)
- 9 collector-touching suites (betual_collector_integration, crash_recovery,
  market_freshness, scheduler_step2, timeout_budget, final_result_recovery,
  m009_market_capture, status_liveness, ws_instance_market): **115 passed**
- scorecard + deviation_dirty_gating + under_alert_lifecycle: **97 passed,
  2 skipped, 3 FAILED — the SAME 3 vocabulary-scan failures proven
  pre-existing at clean HEAD d50b230 earlier today** (dashboard.js untouched
  in this branch). Not attributable.
- `git diff --check` clean; py_compile clean. Full suite NOT run (production
  host guard; ~10 min; unchanged policy).

### GIT STATE
  Branch: handoff-2026-09-07 (production tree, clean of my edits) + worktree
  branch `capture-eff-20260930` at /home/ubuntu/blm-test
  Commit: **f86e0a5** "Fix capture efficiency without touching alert logic
  (2026-09-30 directive)" — 5 files, PUSHED to origin (new remote branch
  capture-eff-20260930, verified: push output + clean worktree)
  Working tree (main): owner dirty files untouched (scorecard.py, v1
  collector.py, watchdog.sh + ~50 untracked); STATUS.md updated (untracked by
  convention)
  Next agent: integration decision needed — merging capture-eff-20260930 into
  the production working tree REQUIRES an operator-authorized restart window
  (production runs FROM the working tree). Do not merge+restart without
  authorization. NULL-recovery tuning knobs are class constants
  (NULL_SCORE_*); P2 percentiles appear in collector_state.json →
  tick_timing once deployed.

## 2026-09-30 — RESULTS PANEL: 'SHOW LAST N' CAP ADDED (10/20/50/100/200/ALL). COMMITTED + PUSHED. NO RESTART, NO DEPLOY.

result: the RESULTS (resulted alerts) tab gains a display-only "Show last" cap —
10 / 20 / 50 / 100 / 200 / ALL (default ALL). It composes with the existing
league/date/time/result filters: they narrow, the cap then keeps the newest N OF
THAT SET. Pure display state: stores, settlement and the alert condition are
untouched; Clear restores every row; a reload resets to ALL (module state, not
localStorage, same as the other filters).

### FILES (this session's complete diff — both committed)
  blm_v4/dashboard/static/dashboard.js
    - RESULT_LAST_N_CHOICES = [10,20,50,100,200]; RESULT_LAST_N_ALL = 0;
      module state `resultLimit = { last: 0 }`
    - `limitResultedRows(rows, n)` — pure cap of an already-newest-first list
    - `anyResultLimitActive` / `clearResultLimit`; `clearResultFilters()` now
      also clears the cap; `anyResultFilterActive` includes it (Clear button
      visibility + "filters active" copy follow automatically)
    - the newest-first merge of UNDER_ALERTS + Q3B_ALERTS history EXTRACTED
      verbatim into `mergedNewestFirst()` (same tie-break: equal/absent
      timestamps fall back to newest-INSERTED by seq — the settle-worker
      colour contract is untouched) so the panel render and the header count
      walk the SAME list
    - historyAlertsHTML: filterResultedRows -> limitResultedRows -> render
      (order preserved; the pinned source contract "renders THROUGH the
      applier" still holds and is now asserted to include the cap)
    - bar control `rfLast` ("Show last") + binding; header count states
      "N of M shown · last N" while the cap is active (both paint paths)
  tests/test_resulted_panel_filters.py
    - 4 new contract tests: caps to the NEWEST N (oldest hidden, stores keep
      everything), composes with the league filter (newest N OF THE SET),
      display-only (a hidden record is STILL SETTLED while capped; clear
      restores), bar markup + header copy + RESULT_LAST_N_CHOICES grid
    - source pins extended: limitResultedRows inside historyAlertsHTML,
      clearResultFilters calls clearResultLimit

### VERIFICATION (all with BLM_ALLOW_HEAVY_TESTS=1 on the production host —
### the targeted sets only, per the conftest guard's own carve-out; NOT the
### full suite — that must go to a scratch tree per the 2026-09-30 directive)
  tests/test_resulted_panel_filters.py            15 passed (13 pre-existing + 4 new = 17
                                                    collected minus 2 superseded? NO: 15 total,
                                                    all green)
  accordion/review/color-coverage/unprovable/     67 passed
  out-of-window/settle-worker (6 suites)
  test_under_alert_lifecycle.py (FULL suite)      34 passed
  node --check dashboard.js                       OK
  PRE-EXISTING FAILURES (fail IDENTICALLY at clean HEAD e8975f7, proven in an
  isolated /tmp worktree — not attributable to this change):
    test_dashboard_audio_alert ::test_audio_introduces_no_over_and_no_banned_vocabulary
    test_z_frontend_migration  ::test_no_model_series_anywhere
    test_z_historical_alert_frontend ::test_alert_terminology_descriptive_only
  (documented bare-substring vocabulary-scan class; this change adds none of
  the banned substrings)

### GIT STATE
  Branch: handoff-2026-09-07
  Commit: d50b230 "Add a Show-last filter (10/20/50/100/200/ALL) to the RESULTS
          panel" — 2 files: dashboard.js + test_resulted_panel_filters.py,
          staged pathspec-only; all other owners' dirty paths untouched
  Parent: e8975f7 (sibling WAL-checkpoint commit; the branch was ahead 2/
          behind 0 — the two sibling commits rode the push per §19)
  Remote: git@github.com:abwarren/BLM.git
  Push status: PUSHED — `9f0c3bf..d50b230  handoff-2026-09-07 -> handoff-2026-09-07`;
          origin/handoff-2026-09-07 == HEAD == d50b230, ahead/behind 0/0
  Working tree: all other dirty paths are OTHER streams' work (DECISIONS.md,
  blm_v1/collector.py, blm_v4/scorecard.py, deploy/*.service, watchdog.sh,
  m009/clean-metrics tests, docs, scripts) — deliberately not touched
  Next agent should start from: the pushed tip of handoff-2026-09-07

### NOT DONE / RISKS
  - NOT deployed: blm-server still runs the pre-change dashboard.js from the
    working tree — but since the unit serves static files FROM THE TREE, the
    new control is effectively live on next browser load WITHOUT a restart
    (uvicorn serves /static from disk per request). Verify in the browser.
  - Full suite NOT run (production-host guard). If desired, run it in a
    scratch checkout per tests/conftest.py.
  - test_under_alert_lifecycle.py still carries the OTHER stream's uncommitted
    3-hunk realignment (untouched here; see the 2026-09-29 handoff entries).

## 2026-09-29 (owner-proxy handoff) — 2 UNSETTLED TEST FILES RECORDED AGAINST THEIR OWNERS. NO FILE EDITED, NO COMMIT, NO STAGING, NO RESET/STASH/CLEAN, NO RESTART.

result: the two test files whose failures the verification run isolated (isolated clean checkout,
  14 failed / 1847 passed / 1 skipped) are handed to the owners this file itself names (see
  "### Owners (identified from this file's own records)" below). Recorded here because AGENTS.md
  §2.8 makes STATUS.md the canonical handoff channel. Both files keep their working-tree edits
  EXACTLY as found; nothing staged; index empty for both.

### BASELINE NOTE — HEAD moved during the handoff (flag, not acted on)
  Requested baseline: "HEAD = d0f59cf = origin/handoff-2026-09-07".
  Actual now: HEAD = 59f6791 (ahead 1) — a sibling session committed on top of d0f59cf:
    59f6791 "Stop the capture deadline from injecting TimeoutError into SQLite commits; ping the
    watchdog only on a completed fast cycle"   (blm_v4/collector.py + tests/test_status_liveness.py)
  origin/handoff-2026-09-07 has ALSO advanced to 59f6791 (a sibling pushed it during this handoff),
  so now HEAD = origin = 59f6791, ahead 0 / behind 0. Nothing was reset to restore the older HEAD
  (AGENTS.md §2.1/§19 forbid it). Both handoff items are identical at 59f6791 — 59f6791 touches
  neither file.

### HANDOFF 1 → FRONTEND OWNER (owner map §2)
  file: tests/test_under_alert_lifecycle.py   (MODIFIED, 3 hunks, nothing staged)
    working-tree sha256 f3d392cb…   vs   committed-at-HEAD sha256 b420e7d5…
    failing in a CLEAN checkout: test_two_alert_sections_exist_in_the_required_order
  ISSUE: the COMMITTED test asserts the PRE-redesign dashboard; the COMMITTED frontend is already
    the redesign, so this is a stale TEST, not a frontend defect — no production change is needed.
      committed test asserts : "ACTIVE UNDER ALERTS" , "RESULTED ALERTS" , order grid < active < hist
      committed index.html   : "ACTIVE UNDER ALERTS" 0 matches ; "🔴 LIVE ALERTS" x2 ; "🟢 RESULTS" x2 ;
                               byte order activeAlerts(6103) < alertHistory(7253) < grid(8018)
  REVIEW: land the working-tree realignment (hunk @134: "🔴 LIVE ALERTS" / "🟢 RESULTS" /
    active < hist < grid — frontend redesign directive 2026-09-28) — pathspec-only, with the
    frontend suite.
  CAVEAT — SHARED FILE: the same working copy also holds 2 HELD corrections prepared by the
    collector/P4 session (see its entry below: "Authorization was granted for A only; B is NOT
    committed"):
      hunk @395  test_pace_gap_is_served_for_every_block  assert defined -> pytest.skip on an empty live pair
      hunk @731  test_no_alert_vocabulary_regression      drops "signal"/"betting" from the ban list
    Land all 3 hunks or none — do not split the file.
  NOT this owner's: test_every_game_carries_its_own_verdict fails only in FULL-SUITE order — a
    documented flake owned by the test-stack/collector stream (blm_v4/test_stack.py::
    apply_test_environment leaks os.environ["BLM_POKERBET_DB"] with no teardown; see
    "### FLAKE CHARACTERISED" below).

### HANDOFF 2 → COLLECTOR / STORAGE OWNER (owner map §3, "m009_*")
  file: tests/test_m009_m4_analytics.py   (MODIFIED, 1 hunk, nothing staged)
    working-tree sha256 8f7197fb…   vs   committed-at-HEAD sha256 41812e0a…
    failing in a CLEAN checkout: test_time_of_day_segmentation   (assert 0 >= 9)
  ISSUE: the COMMITTED fixture builds G-AM / G-PM at 2026-01-01, BEFORE the clean-data epoch
    CLEAN_DATA_EPOCH = "2026-09-05T05:40:41.782315Z", so scorecard's CLEAN partition
    (first_seen_at >= CLEAN_DATA_EPOCH) excludes them -> band n = 0 -> the assert fails.
  REVIEW: land the working-tree hunk (@57 fixtures + @150 expected hours: start 2026-01-01 ->
    2026-09-22, same UTC hours) so the fixtures land in the CLEAN partition — "the segmentation
    contract is hour-of-day, not calendar date". pathspec-only, with the scorecard/m009 stream.

### PRESERVED EXACTLY (no write to either file)
  tests/test_under_alert_lifecycle.py   31 insertions / 11 deletions   mtime 2026-09-29 05:47:31Z
  tests/test_m009_m4_analytics.py        6 insertions /  4 deletions   mtime 2026-09-28 19:35:07Z
  0 stashes; index empty for both; no reset/checkout/clean/restore of any path; no ref written;
  no restart, no deploy. This section is the only thing this session wrote.

## 2026-09-29 (P4 blockers) — SIGALRM persist-failure class ROOT-CAUSED + FIXED; watchdog verified.
##   COMMIT A LANDED (59f6791) AND PUSHED (d0f59cf..59f6791).
##   test_under_alert_lifecycle.py deliberately left UNCOMMITTED. NOT DEPLOYED.

### 7. WATCHDOG MARGIN — measured, not nominal; then PUSH
  Push target: origin git@github.com:abwarren/BLM.git branch handoff-2026-09-07.
  Result: `d0f59cf..59f6791  HEAD -> handoff-2026-09-07`; origin == HEAD == 59f6791;
  ahead/behind 0/0. Commit B left dirty, nothing else rode along (index asserted empty;
  dry-run then real push; 1 commit, 2 files).
  NO DEPLOY. Collector untouched: MainPID 106791, NRestarts=6 across the whole operation.

  CORRECTION to the nominal arithmetic: the live fast-liveness deadline is
  `FAST_TICK_S * FAST_LIVENESS_FACTOR` = 5.0 * 6.0 = **30 s** (collector.py:2009), i.e. the
  multiplier is the CONSTANT FAST_TICK_S, NOT the `--tick` interval. The "FACTOR x tick = 6 x 10
  = 60 s" form is wrong for this config; the dog is TWICE as strict as that suggests. systemd
  WatchdogUSec = 1 min 30 s = 90 s. Because the dog pings only while a cycle completed within
  the deadline, systemd's timer expires at last_completion + 30 + 90 = **+120 s**, so a restart
  needs an inter-completion gap >= 120 s. Silent period = gap - 30.

  MEASURED (journal; the running process IS commit A -- collector.py mtime 05:53:12 < process
  start 06:12:35, no later writes):
    steady state (load ~5; the collector's own chrome-headless ~91% CPU + BLM server.py ~60%):
      last 5-min buckets max inter-completion gap 12-15 s -> margin ~75-105 s. 0 dog-stops,
      0 persist failures in the final 6 min (45 cycles).
    worst transient observed (06:29-06:32, load ~12): gaps 92 s and 93 s -> silent 62-63 s vs 90 s
      -> margin **27 s**; the unit did NOT restart. NOTE the 06:02:25-06:12:21 restart cluster (5
      restarts) ran under load 12-14 = my own two concurrent full verification suites + the
      `qemu-system-x86_64 win11-nvme-install` VM; every restart was systemd's Watchdog timeout on
      a STARTING process, self-healing via Restart=always (StartLimitIntervalSec=0), and 0 persist
      failures. The fix does NOT change steady-state restart behaviour: the removed grace ping
      only ever affected the first 60 s after READY=1 (it delayed a doomed restart by 60 s, it did
      not prevent it) and data corruption is eliminated (post-fix persist failures = 0 vs 268
      pre-fix today).
  VERDICT: margin adequate in steady state with a large cushion; held at 2x load; a restart
  requires a >120 s stall that was not observed. Push authorised on that basis.
  RISK TO REVISIT: the 30 s deadline is half the documented "FACTOR x tick_s" intent, so under a
  sustained >120 s stall the unit restarts ~60 s sooner than the docs imply. Owner may want
  FAST_LIVENESS_FACTOR reviewed (its own commit) if more headroom is desired.

tip: started at d0f59cf (= origin/handoff-2026-09-07, pushed by its owner); now HEAD = 59f6791,
  ahead 1 / behind 0. Origin unchanged.

### 1. P4 collector/storage stream — inspected (NOT committed)
  uncommitted P4 production files: blm_v4/collector.py, blm_v1/collector.py, blm_v4/scorecard.py,
  watchdog.sh, deploy/blm-collector.service. NOTE deploy/*.service targets User=gdi /
  /home/gdi/BLM = worker-01, a DIFFERENT host — it must not overwrite this box's user unit.
  The live user unit ALREADY carries the P4 unit settings (Type=notify, WatchdogSec=90,
  Restart=always, StartLimitIntervalSec=0, MemoryHigh=2500M); the only deltas are
  User/WorkingDirectory/BLM_POKERBET_DB/ExecStart(--tick 20 vs 10)/WantedBy. Uncommitted P4
  tests: test_collector_thread_affinity.py, test_status_liveness.py, test_performance_metrics.py,
  test_storage_betual_metrics.py (all untracked) + test_clean_metrics.py, test_m009_* (6),
  test_scorecard_incremental_gate.py, test_v1_collector_staleness.py (tracked, modified).

### 2. SIGALRM persist-failure class — ROOT CAUSE (proven) + FIX
  Traceback (live): `_betual_persist_timer` -> storage.py:1453 `conn.commit()` -> collector.py
  `_on_alarm` `raise TimeoutError()`. py-spy on the live PID showed BOTH MainThread and
  blm-slow-worker in `on_frame -> _ingest_quarter_market_observation -> _betual_record_line ->
  _betual_persist_timer -> upsert_betual_timer`.
  MECHANISM: `signal.setitimer(ITIMER_REAL, tl)` arms ONE process-wide timer whose handler
  RAISES. Playwright dispatches WebSocket frame callbacks NESTED on the capturing thread while
  page.content() is in flight, and those callbacks write to SQLite. The raise therefore landed
  INSIDE a commit: the write was aborted mid-transaction AND the alarm was consumed, so the
  deadline silently stopped being enforced (3.0s budget measured 5.921s) and a late raise could
  land in the tick's own persistence phase.
  SCALE (journal, 2026-09-29 00:00->06:00): 138 `betual timer persist failed` + 119 `quarter
  market observation persist failed`, ALL TimeoutError; 425 bare `TimeoutError` tracebacks; the
  only other class is 7 x `sqlite3.IntegrityError: NOT NULL ... market_type` (separate,
  pre-existing, NOT touched).
  FIX in blm_v4/collector.py: the handler FLAGS the overrun and NEVER RAISES; an overrun is
  logged and the capture is KEPT (discarding it would route every over-budget capture through
  `_fresh_context` — a context recycle + discovery navigation, ~3-5% of ticks, and exactly the
  path that hung 85s before the 03:04:06Z restart). SIGALRM is blocked via the new
  `_block_deadline_signal()` in all three worker threads (watchdog, slow worker, deviation
  backfill) so the process-wide timer can only reach the thread that armed it. A genuinely
  wedged page is now covered by the documented path: self-watchdog stops pinging -> systemd
  restarts the unit (see the WATCHDOG_* note).
  PROOF (reproducer, /tmp/hermes-verify-sigalrm-behaviour.py): control (old raising handler)
  ABORTS a nested SQLite commit (0 rows); the shipped handler keeps the capture and the commit
  SURVIVES (1 row). 5/5 checks pass; the change is behaviour-neutral for within-budget captures.

### 3. Collector watchdog-restart — verified against the P4 changes
  03:04:06Z restart = systemd's own `Watchdog timeout (limit 1min 30s)` after the dog starved
  (61 `no completed fast cycle within 30s` lines from 03:02:18). Cause chain: tick 609
  page.content() START 03:01:52.767 -> the armed alarm fired 03:01:55.771 -> recovery then hung
  >=85s -> no completed fast cycle -> restart. So the watchdog worked as designed; the upstream
  defect is item 2. NRestarts=1 (collector), 0 (server); both units active.
  Unit change is NOT the fix here: the box already runs the P4 unit settings; deploy/ is the
  worker-01 variant.

### 4. test_status_liveness.py + the required collector.py change
  `test_watchdog_pets_only_when_fast_cycle_fresh` asserted NO ping when no fast cycle has ever
  completed; collector.py pinged unconditionally for the first 60s ("STATUS=startup grace
  period"). The implementation contradicted its OWN documented contract — the comment at the
  READY=1 site says liveness is "reported exclusively via WATCHDOG=1 pings ... gated on fresh
  fast cycles", and with Type=notify systemd arms WatchdogSec only AFTER READY=1, so a
  grace-window ping only blindfolds the dog for its most fragile minute. VERDICT: genuine
  collector.py defect -> the unconditional grace ping (and the now-unused `startup_deadline`
  parameter) is REMOVED; the test stays as the contract. Commit test WITH the code.

### 5. test_under_alert_lifecycle.py — 2 failures classified
  (a) `test_no_alert_vocabulary_regression` — INCORRECT TEST. It is a bare-substring scan over
      JS source, so it bans identifiers, not displayed vocabulary: `signal` is shipped audit-trace
      vocabulary (index.html:198 renders ALERT -> GAME -> SIGNAL -> EXECUTION -> RESULT;
      styles.css:821 documents the "BLM SIGNAL / DIRECTION ROW") and `ctrl.signal` is the Fetch
      API's own AbortController property; `betting` occurs 32x as `state.betting` / `betting:
      null` and the AUTO BETTING panel is the exclusion the test itself already makes. Both
      removed from the ban list; the remaining 9 words are the actual model-jargon rule and all
      pass. (b) `test_pace_gap_is_served_for_every_block` — data-luck assertion (`assert defined`
      requires the LIVE population to carry a defined pair). Replaced with the file's existing
      `pytest.skip` idiom when the live data has no such game; the defined case is already pinned
      deterministically by `test_pace_gap_is_consistent_for_qualifying_and_non_qualifying`.
      Production code (under_alert.py `pace_gap`, api.py) is ALREADY committed, so this is a
      stale-test correction, not a test riding ahead of its implementation.
  NOT in scope, still red at HEAD (same bare-substring class): test_dashboard_audio_alert
      ::test_audio_introduces_no_over_and_no_banned_vocabulary, test_z_frontend_migration
      ::test_no_model_series_anywhere, test_z_historical_alert_frontend
      ::test_alert_terminology_descriptive_only, test_m009_m5_frontend_integrity
      ::test_metric_labels_explicit — pre-existing, untouched.

### Verification (isolated /tmp worktrees; the production tree was never executed)
  static contract 13/13 PASS | behaviour probe 5/5 PASS | targeted: thread_affinity+timeout_budget
  +status_liveness 21 PASSED; under_alert_lifecycle 33 PASSED / 1 skipped.
  FULL-SUITE DIFFERENTIAL (pristine HEAD vs HEAD+the 3 files, no DB in either tree so both sides
  are symmetric; no test can reach a live DB — CleanMetricsStore resolves relative to its source
  file): BASE 22 failed / 1857 passed; CANDIDATE 18 failed / 1861 passed; **NEW FAILURES = 0**.
  Fixed: test_watchdog_pets_only_when_fast_cycle_fresh, test_no_alert_vocabulary_regression,
  test_page_content_timed_no_longer_spawns_threads (its subject is the P4 collector.py), and
  test_two_alert_sections_exist_in_the_required_order (attributable to the pre-existing dirty
  test reorder, NOT to this change — the base tree carried HEAD's committed test file).

### 6. LIVE STATE — the fix went live by itself, and the collector restarted 5x
  NRestarts went 1 -> 6; current MainPID 106791, ExecMainStartTimestamp 06:12:35
  (this session never restarted anything). Cause of every restart is systemd's
  `Watchdog timeout (limit 1min 30s)`: the dog starved because the host is
  saturated — `qemu-system-x86_64 -name win11-nvme-install` at 159% CPU / 6 GB for
  48 min, plus the desktop Chrome/GNOME and the collector's own chrome-headless.
  Fast ticks degraded to 31s and 89s. NOT attributable to this change: the process
  killed at 06:02:25 was running the OLD (unmodified) collector.py, and the load is
  still present with no test process running.
  CONSEQUENCE: because the unit's WorkingDirectory is the working tree, each
  systemd restart re-executed the tree as-is — so the UNCOMMITTED collector.py
  change is LIVE as of 06:12:35.
  Live validation of the fix (since 06:12:35): `betual timer persist failed` = 0,
  `quarter market observation persist failed` = 0 (was 138 + 119 earlier today);
  34 `page.content() OVERRAN its 3.0s budget ... capture kept` lines. Those 34
  overruns in ~20 min are the load, and they are the direct evidence that
  discarding-on-overrun would have forced ~34 `_fresh_context` recycles into a
  starved, watchdog-timed process. Under-current-load the dog keeps stopping pings,
  so further restarts should be expected until the host load clears.
  RISK TO FLAG: with the grace ping gone, a first fast cycle that exceeds
  WatchdogSec (90s) will now cycle restarts; FAST_LIVENESS_FACTOR x --tick = 60s vs
  WatchdogSec 90s is a tight margin under this load. Owner decision.

### Commits
  A) blm_v4/collector.py + tests/test_status_liveness.py
       "Stop the capture deadline from injecting TimeoutError into SQLite commits; ping the
        watchdog only on a completed fast cycle"
       -> LANDED as 59f6791 (2 files, index verified == authorized set, nothing else staged).
  B) tests/test_under_alert_lifecycle.py
       "Correct two stale assertions in the under-alert lifecycle suite"
       -> AUTHORIZATION WAS GRANTED FOR A ONLY; B is NOT committed and remains modified in the
          working tree (its 2 corrections are verified but unlanded). Its production code
          (under_alert.py / api.py) is already committed, so nothing rides ahead of its impl.
  Nothing else. All other dirty paths are other owners' work and are excluded.
  Post-commit tree: HEAD 59f6791, 17 tracked-modified + 46 untracked paths remain.

### STILL BLOCKING DEPLOY (unchanged by this session)
  the tree is NOT the verified commit: 16 other tracked-modified files + untracked P4 tests and
  docs remain (scorecard.py, blm_v1/collector.py, watchdog.sh, deploy/blm-collector.service,
  test_clean_metrics/m009_*/scorecard_incremental_gate/v1_collector_staleness, DECISIONS.md,
  docs/milestones/CURRENT.md, prospective_health_history.jsonl, scripts/*, AGENTS.md, STATUS.md).
  Not restarted, not deployed, not pushed.

## 2026-09-29 (push) — handoff-2026-09-07 PUSHED to origin (ed2772f..d0f59cf). NO RESTART, NO DEPLOY.

result: fast-forward push of 8 commits under explicit authorization. The authorized C2 test
  realignment (3aa8f2b "Align the live fingerprint API test with the canonical key set") had
  ALREADY been committed by its owner while this session held, so the push also published the
  owners' committed work that was sitting ahead of origin on the same branch.

  commits published (ed2772f..d0f59cf):
    c05c6ae  auth layer            f1e28bf  login + frontend assets
    a4a29d6  pokerbet execution    bd866d1  TEST stack harness
    14e959c  conftest fixtures     2766883  execution core game-identity + PokerBet DOM sink (Blocker 1)
    3aa8f2b  C2 test realignment  <- the authorized change        d0f59cf  frontend alert/results suites
  verified: origin/handoff-2026-09-07 = d0f59cf = local HEAD.

  gate frozen at push time (the "relevant verification" the directive named):
    pytest tests/test_v4_api.py -> 24 passed
    C2 / under-fingerprint regression (under_fingerprints, betting_execution, live_market_gate,
      execution_game_binding, pokerbet_live_adapter, pokerbet_quarter_dom) -> 134 passed
    whitelist fix confirmed: the canonical test now validates by SUBJECT TOKEN
      (fingerprint_<SUBJECT>_... must be C1/C3/C5/R2), so fingerprint_c5_req_ratio /
      fingerprint_c5_q3_ratio pass while any C2 field still fails.
  Services untouched: blm-server active NR=0; blm-collector active NR=1 (systemd's own 03:04
  watchdog restart, not this session). No restart, no deploy. Real tree never written by this
  session; no reset/stash/rebase of any owner's work.

## 2026-09-29 (owner-proxy commits) — BLOCKERS 1 + 2 RESOLVED AND COMMITTED; BLOCKER 3 PART-LANDED (7 of 9). NOT PUSHED, NOT DEPLOYED.

result: three pathspec-scoped commits made under explicit owner-proxy authorization.
  Blocker 1 is green from a pristine checkout (42 passed), the stale C2 test block is
  realigned to the canonical contract (24 passed), 7 of 9 frontend suites landed (71 passed).
  No restart, no deploy, no push. Nothing reset/stashed/cleaned; sibling work untouched.

### COMMITS (local; branch handoff-2026-09-07, ahead of origin by 8)
  2766883  Bind game identity through the execution core and add the PokerBet DOM observation sink
           blm_v4/execution/{store,total_executor,betslip_verifier,selection_model}.py + blm_v4/storage.py
           (BLOCKER 1 + the storage.py sink it calls; no test weakened, no C2 field reintroduced)
  3aa8f2b  Align the live fingerprint API test with the canonical key set (C1, C3, C5, R2)
           tests/test_v4_api.py  (BLOCKER 2 — C2 is asserted ABSENT, not restored)
  d0f59cf  Commit the frontend alert and results test suites that pass on the current dashboard
           tests/{test_active_alert_card,test_dashboard_command_center,test_resulted_panel_accordion,
           test_final_result_color,test_resulted_alerts_review,test_resulted_alerts_unprovable_states,
           test_resulted_panel_filters}.py   (BLOCKER 3, 7 of 9 files)
  staging was verified to equal the authorized set before each commit; index left empty; 0 stashes.

### BLOCKER 2 RESOLUTION — how tests/test_v4_api.py now matches the contract
  The old block asserted fingerprint_c2 / c2_reason / c2_operand_source and a per-game
  c2_evaluation DEBUG record.  Replaced by test_live_fingerprint_block_serves_the_canonical_key_set:
    - C1, C3, C5, R2 each present with a boolean `_triggered` verdict
    - EVERY `fingerprint_*` field must be ABOUT one of those four keys — the subject is the
      first token after the prefix, so per-key operand detail (fingerprint_c5_req_ratio) is
      allowed while any C2/C4/C6 field FAILS the test
    - c2_reason / c2_operand_source must be absent; `fingerprints_fired` ⊆ canonical set;
      fingerprint_count == len(fired)
  The guard was proven to bite: against a synthetic block it flags fingerprint_c2_triggered
  and fingerprint_c2, and passes the canonical block.
  The C2-only debug-log test was dropped — the committed blm_v4/api.py emits no c2_evaluation
  (0 C2 references), so there is no canonical analogue.  Nothing else in the file depended on C2.

### VERIFICATION (all in isolated /tmp worktrees; the production tree was never run)
  candidate @14e959c + the authorized set, and again at the committed tip d0f59cf (pristine, 0 dirty):
    PokerBet/execution (execution_game_binding + pokerbet_live_adapter + pokerbet_quarter_dom)
        before: 13 failed / 29 passed  ->  after: 42 passed / 0 failed
    tests/test_v4_api.py                 before: 2 failed / 23 passed -> after: 24 passed
    Blocker-3 frontend set (7 files)     71 passed
    regression (frozen common set + Auto-Bet + fingerprints, 9 files)
        1 failed / 238 passed — the single failure is the known DATA-DEPENDENT
        test_live_market_gate::test_api_active_flag_follows_the_gate (fails identically at
        0a9a7cc / 7921954 / ed2772f / 14e959c with the stale-DB symlink => pre-existing)
  INVARIANTS at the committed tip: FINGERPRINT_KEYS = ("C1","C3","C5","R2");
    fingerprint_c2_triggered refs = 0; C2_MOMENTUM_MAX = 0; blm_v4/betting 15 modules;
    blm_v4/auth 9 modules; 0566f5a/7921954/0a9a7cc/ed2772f/14e959c ALL ancestors of HEAD;
    8/8 boot imports OK (server, blm_v4.api, blm_v4.auth, blm_v4.auth.api,
    blm_v4.execution.pokerbet.dom, blm_v4.betting.api, blm_v4.betting.account_guard, blm_v4.test_stack).
    TRAP (recorded): `python3 -I -c "import blm_v4..."` returns ModuleNotFoundError for
    EVERYTHING because -I strips the cwd from sys.path — proven empirically
    (plain -c => sys.path[0]='' ; -I => no cwd entry).  Pin PYTHONPATH instead.

### BLOCKER 3 — the 2 files deliberately NOT committed (evidence re-validated)
  test_status_liveness.py — its subject is the collector self-watchdog, which lives in the
    DIRTY collector.py (it drives c._watchdog_thread / _watchdog_stop and WATCHDOG_USEC wiring);
    committing it would put a RED test in its own commit.  Held with the P4 stream.
  test_under_alert_lifecycle.py — 2 failures, both independent of these commits:
    test_no_alert_vocabulary_regression: the test bans the word "signal", and the COMMITTED
      dashboard.js contains class="blm-signal-row"/<span class="blm-sig-label">SIGNAL =>
      pre-existing contract mismatch owned by the frontend owner.
    test_pace_gap_is_served_for_every_block: "no game in the payload carried both operands" =>
      DATA-dependent (same class as the live_market_gate failure), environmental under the
      stale-DB symlink.

### SIGALRM PERSIST-FAILURE CLASS — bounded investigation (scope respected)
  The "betual timer persist failed: TimeoutError" tracebacks are raised by the SIGALRM handler
  at collector.py:2429 (`_on_alarm`) firing INSIDE storage commits.  That handler/setitimer code
  exists ONLY in the uncommitted collector.py redesign — the committed collector.py uses a side
  thread and contains 0 occurrences of setitimer/_on_alarm.  Checked the committed set:
  blm_v4/storage.py has NO signal/timer code (0 matches), so none of the three commits can
  introduce this class, and it cannot affect test verification (tests never enter the collector
  fast path, and the collector writes the repo-root DB while test worktrees use their own).
  Separately, the pre-existing data class (sqlite3.IntegrityError: NOT NULL constraint failed:
  quarter_market_observations.market_type) is unrelated to the SIGALRM change.

### STATE
  HEAD d0f59cf ; origin/handoff-2026-09-07 = ed2772f (ahead 8, behind 0) ; index empty ;
  0 stashes ; dirty 65 paths (18 modified / 47 untracked — was 78/28/50).
  Services NOT restarted: blm-server active (NRestarts=0), blm-collector active (NRestarts=1,
  the systemd watchdog restart at 03:04:06Z — not this session).  /healthz 200 auth:true users:2.
  My scratch worktrees removed; 0 of mine remain.

### STILL BLOCKING DEPLOYMENT (unchanged in kind)
  1. P4 collector/storage stream uncommitted (collector.py, scorecard.py, blm_v1/collector.py,
     watchdog.sh, deploy/blm-collector.service [targets worker-01, not this host], + tests) —
     owner must decide production-readiness; the SIGALRM class and the 03:04 watchdog kill live there.
  2. test_status_liveness.py + test_under_alert_lifecycle.py unresolved (above).
  3. The working tree must equal the verified commit before any restart (production runs the
     working tree).  PUSH not authorized/performed — local ahead by 8.

## 2026-09-29 (integration gate, after owner commits) — OWNERS COMMITTED P1/P2/P3 → tip 14e959c. CLEAN-CHECKOUT GATE PASSES FULLY. NEW FAILURES = 2, both a stale C2-forensic block in ONE test file. NOT PUSHED, NOT DEPLOYED.

result: the owner-commit phase HAPPENED while this was running. P1 (auth), P2 (frontend assets)
  and P3 (pokerbet) are now COMMITTED by abwarren, and the clean-checkout gate that FAILED on
  ed2772f now PASSES on 14e959c. The remaining gap to NEW FAILURES = 0 is a single 63-line
  C2-forensic block in tests/test_v4_api.py (obsolete under the canonical REMOVE-C2 direction).
  Nothing pushed, nothing restarted, nothing deployed. Real tree never written by me.

### IDENTITY
  owner tip (branch handoff-2026-09-07) = 14e959c  (local ahead of origin by 5)
  origin/handoff-2026-09-07             = ed2772f  (NOT yet pushed)
  verified base                         = ed2772f  (unchanged; 0a9a7cc/0566f5a/7921954 unmoved)
  New owner commits (all by abwarren, all children of ed2772f):
    c05c6ae Commit the BLM login and session authentication layer      15 files (+3193)
            blm_v4/auth/{__init__,api,config,middleware,passwords,ratelimit,seed,service,store}.py,
            requirements.txt (+bcrypt>=3.2.0), tests/test_auth_{login,authorization,security,seed,ui}.py
    f1e28bf Commit the login page and dashboard operational view assets 7 files (+1868)
            dashboard/static/{login.html,login.css,login.js,blm-login-arena.jpg,stats.js,explorer.js},
            tests/test_frontend_operational_views.py
    a4a29d6 Commit the PokerBet live execution adapter and its tests    8 files (+1934)
            blm_v4/execution/pokerbet/{__init__,browser,dom,session}.py,
            scripts/collect_pokerbet_quarter_dom.py, tests/test_{execution_game_binding,
            pokerbet_live_adapter,pokerbet_quarter_dom}.py
    bd866d1 Commit the isolated TEST stack harness                      3 files (+543)
            blm_v4/test_stack.py, run_test_stack.py, tests/test_test_stack.py
    14e959c Commit the authentication test-harness fixtures             1 file (+110)
            tests/conftest.py (auth_root/auth_stack/login/signed_in + pytest_configure)

### CLEAN-CHECKOUT GATE at 14e959c — ALL PASS (isolated worktree /tmp/hv-14e959c, 0 dirty)
  import gate     server, blm_v4.api, blm_v4.auth, blm_v4.execution.pokerbet, blm_v4.betting.api,
                  blm_v4.betting.account_guard, blm_v4.test_stack — ALL PASS
                  `from blm_v4.auth import install` NOW RESOLVES FROM THE COMMIT
                  (this is the exact import that failed on ed2772f alone -> the blocker is CLOSED)
  static refs     index.html {styles.css,dashboard.js,stats.js,explorer.js} missing=NONE
                  login.html {login.css,blm-login-arena.jpg,login.js}        missing=NONE
  boot+serve      /healthz 200 {"auth":true,"users":2}; / 303; /login 200;
                  /static/{login.css,login.js,stats.js,explorer.js,dashboard.js} all 200;
                  /api/v4/live 401; /api/auth/me 401   (auth gate live)
                  ModuleNotFound/ImportError in boot log: 0
  content         FINGERPRINT_KEYS = ("C1","C3","C5","R2"); C2_MOMENTUM_MAX 0;
                  blm_v4/betting 15 modules (Auto-Bet intact, 0 diff vs 7921954);
                  fingerprint layer 0 diff vs 0a9a7cc; blm_v4/auth 9 modules

### TEST GATE — NEW FAILURES = 2 (both C2), against the ed2772f baseline
  Same suite set run on BOTH sides (109 common test files), plus the 17 new suites.
    baseline ed2772f : 23 failed / 1632 passed / 1 skipped
    candidate (tree) : 15 failed / 1648 passed / 1 skipped
    FIXED by the owner commits  : 10 (all frontend vocabulary: resulted-alerts unprovable states,
                                    panel filters, review labelling, under_alert_lifecycle, m009 m4)
    NEW                         : 2  — tests/test_v4_api.py::test_live_fingerprint_block_serves_c2_provenance
                                       tests/test_v4_api.py::test_live_c2_evaluation_logged_at_debug
  Proof it is ONLY those 2: candidate-failures-minus-C2 (13) is byte-identical to
  baseline-failures-minus-fixed (13). Every other candidate failure is PRE-EXISTING.
  New suites (17 files): 1 failed / 223 passed — the 1 failure
  (test_status_liveness::test_watchdog_pets_only_when_fast_cycle_fresh) ALSO FAILS IN THE REAL
  PRODUCTION TREE => pre-existing, not an integration regression. Auth suites: 119 passed / 1 skipped.

### THE 2 NEW FAILURES — root cause + owner + fix (ONE file, ONE block)
  tests/test_v4_api.py is DIRTY (+63/-0 vs ed2772f). The added block is headed
  "# C2 forensic observability (2026-09-23)" (lines 429-469) and asserts C2 fields
  (fingerprint_c2, c2_reason, c2_operand_source, fingerprint_c2_triggered) plus a per-game
  `c2_evaluation` DEBUG record. The canonical C2 REMOVAL deleted exactly those fields.
  => Same obsolete C2-forensic layer already dropped TWICE (tests/test_betting_execution.py,
     tests/test_under_fingerprints.py). This is the THIRD file carrying it.
  owner: the C2-forensic / analytics owner (it is not in the C2 artifact's 6 landed files).
  fix: align/drop that block to the REMOVE-C2 direction — nothing else in the file depends on it.
  preserved first (non-destructive, round-trippable):
    /tmp/backend-agent-preserved/c2-forensic/c2-forensic-test_v4_api.py.patch  (71 lines)
  With that one edit, NEW FAILURES = 0.

### RESIDUAL DEPLOY BLOCKER — 78 dirty paths (down from 101); NONE boot-critical anymore
  28 modified: blm_v4/collector.py, blm_v4/storage.py, blm_v4/scorecard.py, blm_v1/collector.py,
      watchdog.sh, deploy/blm-collector.service, prospective_health_history.jsonl, DECISIONS.md,
      docs/milestones/CURRENT.md, blm_v4/execution/{store,total_executor,betslip_verifier,
      selection_model}.py (P3 leftovers — pokerbet/ was committed, these were not), + 15 tests
      (incl. test_v4_api.py above)
  50 untracked: 27 scripts/, 7 tests/, 5 docs/, 3 analysis/, 5 analysis_*.txt, STATUS.md,
      AGENTS.md, blm-dev/ (nested worktree — must stay excluded from any pytest run)
  Still owner-gated: P4 collector/storage production-readiness (unchanged from the previous
  section) and the execution/*.py P3 leftovers.
  NOTE: the tree is now 14e959c + P4/P3-leftover/tests/docs — i.e. the boot-critical half of
  the "partial commit" is FIXED; what remains is collector/storage + test alignment.

### PUSH + DEPLOY — NOT PERFORMED
  Gate is "NEW FAILURES = 0"; it is 2. So push/land/deploy are NOT yet authorized by the gate,
  and pushing remains explicit-authorization territory. Local ahead of origin by 5 commits.
  Services blm-server/blm-collector untouched: active, NRestarts=0, since 01:19:02.

### SCRATCH ARTIFACTS (additive handles; safe to delete)
  branch integrated-candidate-20260929 -> 2be2d04  (SUPERSEDED by the owner commits; delete with
    `git branch -D integrated-candidate-20260929`)
  worktrees: /tmp/hermes-integrate-20260929, /tmp/hermes-integrate2-20260929, /tmp/hv-14e959c,
    /tmp/hermes-verify-recon-{base,tgt}   (all /tmp; real refs and the real tree untouched)
  Evidence: /tmp/hermes-int-{build,gate,tests,gate4}-out.txt, /tmp/hermes-14e-gate-out.txt

## 2026-09-29 (three-blocker verification) — EVIDENCE GATHERED. NO COMMIT MADE (owner action). **BLOCKER 2 IS NOT PRODUCTION-READY AS-IS: its stream re-asserts C2.**

result: ran the directive's mandated pre-commit verification for all three blockers, read-only +
  isolated worktrees. Findings below. Nothing committed, nothing restarted, nothing deployed.
  Branch/refs unchanged by this session (HEAD 14e959c, origin ed2772f, 0 stash, tree still dirty).

### BLOCKER 1 — EXECUTION CORE (4 files) — READY, but COUPLED to Blocker 2
  All 4 files are modified-tracked and uncommitted:
    blm_v4/execution/store.py, total_executor.py, betslip_verifier.py, selection_model.py
  Isolated proof (worktree @14e959c + the 4 files; /tmp/hvb1_*.raw):
      committhe 4 alone        -> tests/test_{execution_game_binding,pokerbet_live_adapter,pokerbet_quarter_dom}.py
                                  = 1 failed, 41 passed   (12 of the 13 clean-checkout failures fixed)
      + also storage.py        -> 42 passed, 0 failed
  residual (exec-only) = tests/test_pokerbet_quarter_dom.py::test_storage_rejects_cross_game_primary_key_linkage
    needs PokerBetStore.insert_pokerbet_dom_market_observation, which lives ONLY in the dirty
    blm_v4/storage.py:932 (0 occurrences at HEAD) — i.e. a BLOCKER-2 file.
  => Blocker 1 cannot be cleared alone; it needs storage.py (Blocker 2) too. Nothing needed here
     contradicts the C2-Removed invariant, and no test was patched.

### BLOCKER 2 — COLLECTOR / STORAGE — **NOT READY: contains a file that re-asserts C2**
  (a) HARD CONFLICT with the preserved invariant. The dirty tests/test_v4_api.py re-adds the
      C2-FORENSIC layer that was already dropped elsewhere:
        line 429  "# C2 forensic observability (2026-09-23) — the served fingerprint block"
        line 435  def test_live_fingerprint_block_serves_c2_provenance(client):
        line 445  assert "c2_reason" in fpb and fpb["c2_reason"]
        line 446  assert "c2_operand_source" in fpb
        line 451  assert fpb["fingerprint_c2_triggered"] is False      <-- asserts the C2 key EXISTS
        line 476  "...fingerprint block (C2 included)..."
      HEAD tests/test_v4_api.py has 0 C2 occurrences; the dirty file has 7. Isolated run
      (worktree @14e959c + the stream; /tmp/hvb2_all.raw) = 2 failed, 122 passed, the 2 being
        test_v4_api.py::test_live_fingerprint_block_serves_c2_provenance
        test_v4_api.py::test_live_c2_evaluation_logged_at_debug
      ⇒ committing this file re-introduces C2 assertions that CONTRADICT
        FINGERPRINT_KEYS = ("C1","C3","C5","R2") and 0 dangling fingerprint_c2_triggered reads.
        It must be reconciled (C2-forensic additions dropped) BEFORE commit — exactly the same
        disposition the backend agent already applied to under_fingerprints.py.
      No other file in the 21-path stream references fingerprint_c2_triggered.
  (b) DB location — CORRECT (verified): server resolves repo-root/blm_pokerbet.db (api.py:116);
      collector unit sets BLM_POKERBET_DB=/home/ubuntu/BLM/blm_pokerbet.db; live file 13.83 GB,
      growing, on /dev/sdb3. Open handles of both pids are repo-root only.
  (c) /mnt/blm-nvme — NO LIVE ASSUMPTION (verified): 0 references in blm_v4/, blm_v1/, server.py,
      watchdog.sh, and 0 in the dirty storage.py / collector.py / scorecard.py. /mnt holds STALE
      copies (pokerbet 00:55, metrics_clean 00:55, ts 00:55, blm.db 00:46) held by no process.
      Caveat (unchanged): the read-only audit scripts under scripts/ still read the stale copy.
  (d) Recurring persist errors — TWO DISTINCT CLASSES, BOTH LONG-STANDING in the dirty stream:
      - NEW-ish class: TimeoutError raised by the SIGALRM handler at collector.py:2429
        (`_on_alarm`) firing INSIDE storage commits (upsert_betual_timer / insert_betual_line_
        observation / upsert_market_observation) -> "betual timer persist failed".
        Counts: 159 'persist failed' since 00:00 (80 betual timer, 70 quarter market observation,
        9 market observation), 245 TimeoutError, hourly peak 02:00 (59). Present at least since
        2026-09-28 00:06 (same line 2429) => not new, but PERSISTENT.
        NOTE: the SIGALRM design exists ONLY in the dirty collector.py. HEAD's _page_content_timed
        uses a side THREAD (t.join(timeout=tl)), 0 occurrences of _on_alarm/setitimer. So this
        error class is introduced by the uncommitted design that replaces it.
      - PRE-EXISTING data class: sqlite3.IntegrityError: NOT NULL constraint failed:
        quarter_market_observations.market_type (sample 2026-09-28 12:00; 243 occurrences in the
        09-27..09-28 19:35 window). Independent of the dirty SIGALRM change.
  (e) Collector watchdog — MARGINALLY UNSTABLE: WatchdogSec=90 + Type=notify; 36 "STOPPING
      WATCHDOG=1" stalls and 1 systemd "Watchdog timeout (limit 1min 30s)" -> 1 automatic restart
      of blm-collector at 03:04:06Z (NRestarts=1, NOT performed by this session; it recovered and
      is ticking: tick 364 done in 0.67s, errors=0).
  (f) deploy/blm-collector.service (dirty) targets User=gdi / WorkingDirectory=/home/gdi/BLM /
      venv — a DIFFERENT host (worker-01). The running unit on this box is
      /home/ubuntu/.config/systemd/user/blm-collector.service (ubuntu, /usr/bin/python3, --tick 10).
      Committing the deploy file must NOT be used to overwrite the running user unit.

### BLOCKER 3 — FRONTEND TESTS — 7 of 9 files CLEAN, 2 with failures
  Isolated run (worktree @14e959c + the 9 files; /tmp/hvb3_all.raw) = combined 3 failed, 113 passed.
    test_active_alert_card.py                    9 passed     COMPLETE
    test_dashboard_command_center.py             4 passed     COMPLETE
    test_resulted_panel_accordion.py             9 passed     COMPLETE
    test_final_result_color.py                  14 passed     COMPLETE
    test_resulted_alerts_review.py              15 passed     COMPLETE
    test_resulted_alerts_unprovable_states.py    9 passed     COMPLETE
    test_resulted_panel_filters.py              11 passed     COMPLETE
    test_status_liveness.py                  1 failed / 10 passed   NOT clean
    test_under_alert_lifecycle.py            2 failed / 32 passed   NOT clean
  ⇒ owner must decide on the two failing files (or commit the 7 clean ones only).

### SAFETY
  No commit. No reset/stash/checkout. No ref write. Production tree untouched (78 dirty paths).
  No restart. Only throwaway /tmp worktrees were used and they have been removed.
  NOTE: sibling agents are ACTIVE (a full-suite run in /tmp/blm-integrated and 2be2d04 verification
  in /tmp/hermes-integrate-20260929) — the base may move again. Heavy concurrent suites also compete
  with the live collector, which is watchdog-timed.

## 2026-09-29 (integration re-check) — OWNER ADVANCED THE BRANCH. AUTH/FRONTEND/POKERBET-ADAPTER COMMITTED. **COLLECTOR/STORAGE + EXECUTION-CORE STILL OUTSTANDING → STOPPED, NO DEPLOY.**

result: re-checked owner state (directive step 1) and found HEAD had MOVED by 5 commits since
  ed2772f. Classified every remaining path against the 4 required owner streams (steps 2–3).
  Two streams are still uncommitted, so the integration cannot complete → STOPPED and reported
  instead of forcing it. No commit, no reset, no stash, no ref write of any kind by this session.
  No restart, no deploy. Only throwaway worktrees under /tmp were used.

### 1. OWNER STATE RE-CHECK (sibling agents are active — this moved DURING the session)
  - BEFORE (05:xx this session): HEAD = ed2772f ; origin = 0a9a7cc → I landed + pushed ed2772f.
  - NOW:    HEAD = **14e959c** ("Commit the authentication test-harness fixtures", 03:54:28Z)
            handoff-2026-09-07 [ahead 5] ; origin/handoff-2026-09-07 = **ed2772f** (my push).
  - The 5 owner commits are LINEAR children of ed2772f (ed2772f is an ancestor of 14e959c):
        c05c6ae login + session authentication layer (blm_v4/auth/* 9 modules, 5 auth tests, +bcrypt, test_stack)
        f1e28bf login page + dashboard operational view assets (login.html/js/css, blm-login-arena.jpg, stats.js, explorer.js)
        a4a29d6 PokerBet live execution adapter + its tests (blm_v4/execution/pokerbet/*, 3 tests)
        bd866d1 isolated TEST stack harness (blm_v4/test_stack.py, run_test_stack.py, tests/test_test_stack.py)
        14e959c authentication test-harness fixtures (tests/conftest.py)
  - sibling artifacts present and LEFT ALONE: branch `integrated-candidate-20260929` = 2be2d04
    (= ed2772f + the ENTIRE dirty tree, 111 files / 34914 insertions, built 03:52 by another
    session in /tmp/hermes-integrate-20260929). NOT adopted, NOT modified, NOT judged here — it
    commits uncommitted owner work, which is the owner's call, not mine.

### 2. REQUIRED-STREAM CLASSIFICATION (step 2/3) — evidence /tmp/hermes-classify-streams.sh
  [AUTH (P1)]             23 paths — **23 committed-clean / 0 outstanding**  ⇒ DONE
  [EXECUTION / POKERBET]  12 paths — 8 committed-clean / **4 OUTSTANDING**   ⇒ PARTIAL
      committed: blm_v4/execution/pokerbet/{__init__,browser,dom,session}.py + scripts/collect_pokerbet_quarter_dom.py
                 + tests/test_execution_game_binding.py, test_pokerbet_live_adapter.py, test_pokerbet_quarter_dom.py
      OUTSTANDING (modified, uncommitted, mtime 2026-09-28 19:35:07): blm_v4/execution/store.py,
                 total_executor.py, betslip_verifier.py, selection_model.py
  [COLLECTOR / STORAGE]   21 paths — 0 committed / **21 OUTSTANDING**        ⇒ NOT STARTED
      blm_v4/collector.py, blm_v4/storage.py, blm_v4/scorecard.py, blm_v1/collector.py, watchdog.sh,
      deploy/blm-collector.service, prospective_health_history.jsonl, docs/milestones/CURRENT.md,
      tests/test_{v1_collector_staleness,collector_thread_affinity,storage_betual_metrics,clean_metrics,
      scorecard_incremental_gate,m009_checkpoint_market,m009_contamination_integrity,
      m009_legacy_contamination,m009_m4_analytics,m009_mvf_aggregation,m009_mvf_api,v4_api,
      performance_metrics}.py
  [FRONTEND (P2)]         12 paths — 3 committed-clean / **9 OUTSTANDING (tests only)**
      committed: stats.js, explorer.js, test_frontend_operational_views.py  (the LOAD-BEARING assets ARE in)
      OUTSTANDING: test_active_alert_card.py*, test_dashboard_command_center.py*, test_resulted_panel_accordion.py*,
      test_status_liveness.py* (*untracked) + test_final_result_color.py, test_resulted_alerts_review.py,
      test_resulted_alerts_unprovable_states.py, test_resulted_panel_filters.py, test_under_alert_lifecycle.py
  [ANALYTICS/DOCS]        scripts/* (29), analysis/*, docs/*, AGENTS.md, STATUS.md, blm-dev/ — NOT load-bearing.
  census: 28 modified tracked / 50 untracked (was 30/71 = 101 before the owner's 23-path commits).

### 3. THE PREVIOUS ENTRY'S P1 BOOT BLOCKERS ARE **CLEARED**
  clean checkout of 14e959c: 7/7 imports OK (blm_v4.auth, .api, .middleware, .passwords,
  blm_v4.betting.api, blm_v4.execution.pokerbet.dom, blm_v4.test_stack); server.py:255
  `from blm_v4.auth import install` resolves; stats.js/explorer.js committed; requirements.txt
  clean with 2 bcrypt pins. ⇒ the server CAN now boot from a clean checkout.

### 4. VERIFICATION of the committed tip (isolated worktrees ONLY; production tree never run)
  (A) VALID base comparison — suites that EXIST at both tips (/tmp/hermes-verify-dependency.out):
        tip14e959c  1 failed, 140 passed  |  base_ed2772f  1 failed, 140 passed
        *** NEW FAILURES from the committed tip = 0 ***
        same single failure in both: tests/test_live_market_gate.py::test_api_active_flag_follows_the_gate
        (data-dependent). NOTE: a first comparison was INVALID (base collected 0 tests because the
        new suites don't exist at ed2772f → "file or directory not found"); re-run with existing-only suites.
  (B) the committed PokerBet stream is NOT self-contained — 13 committed tests FAIL in a clean
      checkout of 14e959c (/tmp/hv2_tip14e959c.raw, /tmp/hv3_tip_only.raw):
        test_execution_game_binding 9 failed — TypeError: Selection.__init__() got an unexpected keyword argument 'game_id'
        test_pokerbet_live_adapter  3 failed — AttributeError: 'PokerBetStore' object has no attribute 'insert_pokerbet_dom_market_observation'
        test_pokerbet_quarter_dom   1 failed — sqlite3.OperationalError: no such column: game_id
      DEPENDENCY PROOF (same commit; only difference = the 4 uncommitted exec-core files copied in):
        tip alone                     13 failed, 29 passed
        tip + uncommitted exec-core     1 failed, 41 passed   (12 of 13 fixed)
      the residual needs blm_v4/storage.py's NEW method insert_pokerbet_dom_market_observation
      (blm_v4/storage.py:932) → the PokerBet stream also depends on the UNCOMMITTED Collector/Storage stream.
  ⇒ **14e959c is internally incoherent: git holds the PokerBet tests but not the code they assert.**

### 5. STOP DECISION (directive step 3 + its explicit STOP clause)
  Collector/Storage (21 paths) is NOT committed, and the execution-core half of PokerBet/execution
  is NOT committed ⇒ report, do not integrate and do not deploy. I did NOT manufacture a clean
  tree, did NOT touch 2be2d04, did NOT commit for any owner.

### 6. LIVE PRODUCTION OBSERVED (read-only; I restarted NOTHING)
  - blm-server: active, pid 23686, NRestarts=0, up since 01:19:02Z. The C2-removal files were
    written to disk at 01:41:44 ⇒ the running server PREDATES the ff land ⇒ **the LIVE server is
    still executing the C2-PRESENT fingerprint layer (7 keys) while the on-disk tree is
    C2-REMOVED (4 keys)**. A restart therefore CHANGES live fingerprint behaviour. (/healthz
    reports auth:true users:2, which is consistent with the newer auth-gated tree, so treat the
    layer mismatch as inference from start-time vs file mtime — not yet measured at runtime.)
  - blm-collector: **restarted by systemd at 03:04:06Z (NRestarts=1) — NOT by me.** Cause:
    "no completed fast cycle within 30s" from 03:02:18, then systemd "Watchdog timeout (limit
    1min 30s)" at 03:03:43. Recovered: ticks 334/335/336 (14.98s/38.76s/16.28s), tracked=6,
    snapshots=2184. 3 further watchdog stalls logged since 03:04.
  - recurring dirty-code errors: "betual timer persist failed: TimeoutError" + "quarter market
    observation persist failed" (collector.py:2429 SIGALRM inside storage upsert/commit) —
    41 events since 03:04, non-fatal, pre-existing.
  ⇒ production is running UNCOMMITTED, UNVERIFIED code and is demonstrably unstable (watchdog kill).

### NEXT ACTIONS (owner commits, then re-run directive steps 3→11)
  1. EXECUTION-CORE owner commits blm_v4/execution/{store,total_executor,betslip_verifier,selection_model}.py
     (pathspec only) so the already-committed PokerBet tests pass.
  2. COLLECTOR/STORAGE owner commits or moves out the 21 paths — and first decides production-readiness
     given the watchdog kill + persist-failure signature. NOTE: deploy/blm-collector.service targets
     User=gdi / WorkingDirectory=/home/gdi/BLM (worker-01), NOT this host — must not overwrite the
     running user units.
  3. FRONTEND owner commits the 9 remaining test files (assets already in).
  4. Then: verify the new integrated commit in an isolated worktree → NEW FAILURES = 0 → make the
     production tree correspond to it → push → restart both units → full health check.

## 2026-09-29 (deployment gate / owner-commit phase) — ed2772f IS NOT A RUNNABLE RELEASE. OWNER COMMITS REQUIRED. I DID NOT COMMIT FOR ANY OWNER.

result: root-caused WHY the tree cannot be cleared to ed2772f. It is not "ed2772f + other
  agents' WIP": ed2772f's OWN COMMITTED FILES import/reference artifacts that are UNTRACKED.
  The release is therefore incomplete in git — a partial commit, not a dirty tree.
  Storage/DB audit (the §STORAGE REQUIREMENT) COMPLETED: PASS. See below.
  Nothing committed, nothing restarted, nothing deployed. Services healthy and untouched.

### THE PROOF (this is why ed2772f is not runnable)
  server.py            COMMITTED at ed2772f, CLEAN (0 dirty lines)  — but contains:
                         from blm_v4.auth import install as install_auth   (line 255, inside main())
      blm_v4/auth/     UNTRACKED  -> clean checkout: ModuleNotFoundError  -> blm-server CANNOT BOOT
  index.html           COMMITTED at ed2772f, CLEAN — but references:
                         /static/dashboard.js  /static/stats.js  /static/explorer.js
      stats.js, explorer.js  UNTRACKED -> clean checkout: 404 on the committed page
  dashboard.js         COMMITTED at ed2772f, CLEAN (so the C2 removal landed; the rest is untracked)
  => The auth feature was committed on the SERVER side (server.py) and left uncommitted on the
     MODULE side (blm_v4/auth/). Same for the frontend assets. Git holds half of each feature.

### STORAGE REQUIREMENT — VERIFIED (read-only; /tmp/hermes-verify-db{audit,2,3}-out)
  Live DB location   /home/ubuntu/BLM/blm_pokerbet.db  on /dev/sdb3  (233G, 78G free, 66% used)
  Server resolution  no BLM_POKERBET_DB env -> DEFAULT_DB = repo-root/blm_pokerbet.db (api.py:116,199)
  Collector env      BLM_POKERBET_DB=/home/ubuntu/BLM/blm_pokerbet.db   (explicit, repo-root)
  Open handles (pid 23686): blm_pokerbet.db, blm.db, blm_ts.db, blm_historical.db — ALL repo-root
  /mnt/blm-nvme      /dev/nvme0n1p1 mounted (469G, 4% used) but holds STALE COPIES only:
                       blm_pokerbet.db 00:55 (13.63GB vs live 13.74GB), blm.db 00:46,
                       blm_metrics_clean.db 00:55, blm_ts.db 00:55. NO process holds them.
  STALE /mnt ASSUMPTIONS IN LIVE CODE: 0  (blm_v4/, blm_v1/, blm_v2/, server.py all clean)
  Remaining stale refs are non-live ONLY: docs/perf/BASELINE_2026-09-26.md, 12 read-only
  audit scripts under scripts/, and older STATUS.md entries. NOTE: those audit scripts read
  /mnt/blm-nvme/blm_pokerbet.db -> they would analyse the 00:55 STALE copy, not live data.
  Guard already in tree: tests/test_test_stack.py:68 asserts the TEST stack never points at
  /mnt/blm-nvme/blm_pokerbet.db.
  VERDICT: storage path is correct and intentional (repo-root/sdb3); no stale /mnt assumption
  in any live path. Deploy is NOT blocked by DB location. Caveat (ranked, for the analytics
  owner): audit scripts still read the stale nvme copy.

### EXACT PER-OWNER COMMIT RECIPES (pathspec only — never `git add .`; AGENTS.md §19)
  Verified by classifying all 101 dirty paths (/tmp/hermes-verify-owners.sh).

  PRIORITY 1 — AUTH OWNER  (unblocks BOOT)
      git add blm_v4/auth blm_v4/dashboard/static/login.html \
              blm_v4/dashboard/static/login.css blm_v4/dashboard/static/login.js \
              blm_v4/dashboard/static/blm-login-arena.jpg \
              tests/test_auth_login.py tests/test_auth_authorization.py \
              tests/test_auth_security.py tests/test_auth_seed.py tests/test_auth_ui.py \
              blm_v4/test_stack.py run_test_stack.py tests/test_test_stack.py \
              requirements.txt DECISIONS.md
      git commit -m "Commit the authentication layer (modules + login assets + tests) so production can boot"
    required requirements.txt change already present: +bcrypt>=3.2.0 (blm_v4/auth/passwords.py)

  PRIORITY 2 — FRONTEND OWNER  (unblocks the committed index.html)
      git add blm_v4/dashboard/static/stats.js blm_v4/dashboard/static/explorer.js \
              tests/test_active_alert_card.py tests/test_dashboard_command_center.py \
              tests/test_final_result_color.py tests/test_frontend_operational_views.py \
              tests/test_resulted_alerts_review.py tests/test_resulted_alerts_unprovable_states.py \
              tests/test_resulted_panel_accordion.py tests/test_resulted_panel_filters.py \
              tests/test_status_liveness.py tests/test_under_alert_lifecycle.py
      git commit -m "Commit frontend assets and tests referenced by the committed dashboard"

  PRIORITY 3 — EXECUTION / POKERBET OWNER  (do NOT touch 7921954 / Auto-Bet)
      git add blm_v4/execution/pokerbet blm_v4/execution/store.py \
              blm_v4/execution/total_executor.py blm_v4/execution/betslip_verifier.py \
              blm_v4/execution/selection_model.py scripts/collect_pokerbet_quarter_dom.py \
              tests/test_execution_game_binding.py tests/test_pokerbet_live_adapter.py \
              tests/test_pokerbet_quarter_dom.py
      git commit -m "Commit the PokerBet execution integration and its tests"

  PRIORITY 4 — COLLECTOR / STORAGE OWNER  (must DECIDE production-readiness first)
      blm_v4/collector.py  blm_v4/storage.py  blm_v4/scorecard.py  blm_v1/collector.py
      watchdog.sh  deploy/blm-collector.service  prospective_health_history.jsonl
      tests/conftest.py  tests/test_{v1_collector_staleness,collector_thread_affinity,
      storage_betual_metrics,clean_metrics,scorecard_incremental_gate,m009_*}.py
      production-ready -> commit; not ready -> the owner moves it out. NOT committed by me.
      Context for the decision: this set carries the watchdog redesign (Type=notify +
      WatchdogSec=90, Restart=always), and a KNOWN pre-existing collector error in the dirty
      code — "betual timer persist failed: TimeoutError" (collector.py:2429, 63 events since
      01:19, same signature in the prior process; non-fatal, ticks keep completing).
      deploy/blm-collector.service targets User=gdi / WorkingDirectory=/home/gdi/BLM (worker-01),
      NOT this host — do not let it overwrite the running user units.

  ANALYTICS/AUDIT + DOCS (45 paths, NOT load-bearing): scripts/*, analysis/*, docs/*, AGENTS.md,
      STATUS.md, blm-dev/. Safe to leave; no effect on boot.

### NOTE ON THE NEXT INTEGRATED TARGET
  After the owners commit, the target is a NEW commit (ed2772f + owner commits), NOT ed2772f.
  Gate then: clean checkout -> boot + import blm_v4.auth + serve login/stats/explorer +
  execution/pokerbet import + Auto-Bet intact + C2 removed + NEW FAILURES = 0.
  Landing note: 0566f5a/7921954 are already ancestors of ed2772f, so only the NEW owner
  commits need merging; do it in an isolated worktree and keep the 6 landed files intact.

### HEALTH (untouched; read-only baseline)
  blm-server + blm-collector active, NRestarts=0, started 01:19:02/01:19:22.
  / 303 ; /healthz 200 {"auth":true,"users":2} ; /api/* 401 (auth by design) ;
  DB reachable; collector ticking. No restart performed.

## 2026-09-29 (deployment gate) — DEPLOY **BLOCKED, AND THE STATED GATE IS UNSATISFIABLE AS WRITTEN**. STOPPED. No restart, no deploy.

result: I inventoried the production tree to clear owner WIP so the tree would equal
  ed2772f. It cannot: the tree is NOT "ed2772f + disposable WIP". A subset of the
  UNTRACKED paths is LOAD-BEARING LIVE CODE that exists in NO commit on ANY ref, and
  production cannot start without it. Reaching tree == ed2772f would therefore break
  production, not clean it. Stopped and reported instead of clearing or deploying.

### Decisive proof (ad-hoc verification; /tmp/hermes-verify-{classify,loadbearing}-out.txt)
  server.py:255-256 (TRACKED; byte-identical at ed2772f):
      def main() -> None:                       # line 102 — the ExecStart entrypoint
          from blm_v4.auth import install as install_auth   # line 255
          _auth_state = install_auth(app, root, logger=logger)  # line 256
  In a clean checkout of ed2772f  -> ModuleNotFoundError: No module named 'blm_v4.auth'
  In the production tree          -> imports OK (auth layer live)
  Live proof: GET /healthz -> {"status":"ok","service":"blm","auth":true,"users":2}
  => blm-server CANNOT START on a tree without blm_v4/auth. The untracked auth layer is
     load-bearing, and it is what auth-gates every /api/* route today (the 401s).

### Load-bearing paths that exist in NO commit (not on ed2772f, not on blm-dev either)
  blm_v4/auth/                       9 modules — required at STARTUP by server.py:255
  blm_v4/execution/pokerbet/         live execution path
  blm_v4/dashboard/static/login.html|login.css|login.js|blm-login-arena.jpg  the login page
  blm_v4/dashboard/static/stats.js|explorer.js   referenced by the COMMITTED index.html (404 without)
  blm_v4/test_stack.py, run_test_stack.py, tests/test_test_stack.py
  requirements.txt (+bcrypt)         blm_v4/auth/passwords.py hashes with bcrypt
  Verified: git cat-file -e dev/local-engineering-2026-09-27:<path> -> absent for all of them.

### Owners (identified from this file's own records — no ownership registry exists in docs)
  1. AUTH owner — "## 2026-09-28 (auth) — LOGIN + AUTHENTICATION LAYER" (§ WHAT WAS BUILT):
     blm_v4/auth/*, dashboard/static/login.*, blm-login-arena.jpg, tests/test_auth_*.py (119 tests),
     requirements.txt(+bcrypt), DECISIONS.md, server.py(+1 call), blm_v4/test_stack.py, tests/test_test_stack.py
  2. FRONTEND owner — line 1318 "OTHER AGENT owns the 3 frontend files (+ untracked frontend
     assets login.*/explorer.js/stats.js)": dashboard.js, styles.css, index.html, stats.js,
     explorer.js + frontend test suites (test_active_alert_card, test_dashboard_command_center,
     test_frontend_operational_views, test_resulted_panel_accordion, test_status_liveness,
     test_final_result_color, test_under_alert_lifecycle…)
  3. COLLECTOR/STORAGE owner: blm_v4/collector.py, storage.py, scorecard.py, blm_v1/collector.py,
     watchdog.sh, deploy/blm-collector.service, tests/test_{v1_collector_staleness,
     collector_thread_affinity,storage_betual_metrics,clean_metrics,m009_*}.py
  4. EXECUTION/POKERBET owner: blm_v4/execution/{store,total_executor,betslip_verifier,
     selection_model}.py, blm_v4/execution/pokerbet/, tests/test_pokerbet_{live_adapter,
     quarter_dom}.py, tests/test_execution_game_binding.py
  5. ANALYTICS/AUDIT owner: scripts/* (28), analysis/*, most remaining untracked tests
  6. HANDOFF/DOCS (shared): STATUS.md, AGENTS.md, docs/*

### Tree census (ed2772f + this)
  30 modified tracked / 71 untracked. Of the modified: 11 are LIVE code (collector, storage,
  scorecard, execution/*, blm_v1/collector, watchdog.sh, requirements.txt, collector.service);
  16 are tests; 3 are docs/data. None of the 6 landed C2/Auto-Bet files is dirty.
  The dirty set also carries the collector watchdog redesign (Type=notify + WatchdogSec=90,
  Restart=always) and the auth decision record.

### Consequence of forcing tree == ed2772f (why this is a STOP, not a cleanup)
  - blm-server fails to start (server.py:255 ImportError) — the auth layer is untracked
  - the login page and all /api/auth/* routes vanish; /api/* silently loses its 401 gate
  - /static/stats.js + /static/explorer.js 404 (index.html references them)
  - bcrypt drops out of requirements.txt (auth hashing breaks on a rebuilt env)
  - the collector watchdog hardening is lost
  Nothing here is disposable WIP; it is the live system, uncommitted.

### NOTE — even after owner commits, the target is NOT ed2772f
  Owners committing their work produces a NEW commit (ed2772f + their commits). The deploy
  gate must then read "tree == that new integrated commit", and it must be verified
  (NEW FAILURES = 0) before any restart. Deploying "tree == ed2772f" was never safe.

### Also noted (not acted on)
  deploy/blm-collector.service targets User=gdi / WorkingDirectory=/home/gdi/BLM — the
  deploy artifacts describe a different host (worker-01); this box runs user units. Not deployed.

### DECISION REQUIRED (owner actions; I must not commit another agent's work)
  The auth + frontend + collector/storage + execution owners must commit their completed work
  (or move it out) — PRIORITY: blm_v4/auth/, the login+stats/explorer assets, requirements.txt,
  and the live modified py files, because production is currently running code that exists in
  no commit. Then: new integrated commit -> verify NEW FAILURES = 0 -> restart -> health check.
  If instead the intent is to ship the tree as-is, that needs explicit authorization accepting
  uncommitted live code — but note the server will come back up, so the "unverified" concern is
  about the collector/storage/scorecard diffs, not about startup.

Pre-deploy health baseline (read-only): both units active, NRestarts=0; / 303, /healthz 200
  auth:true users:2, /api/* 401 (auth by design); DB reachable (79 objects); collector ticking.

## 2026-09-29 (backend agent, independent re-verification) — RECONCILIATION CONFIRMED CLEAN; LANDED + PUSHED (by owner session); DEPLOY BLOCKED. STOPPED, NOT FORCED.

Scope: read-only + isolated worktrees. No restart, no deploy, no rewrite, no stash/reset.
This section independently reproduces the "LAND → VERIFY → PUSH DONE. DEPLOY = HARD STOP"
section below, with a DIFFERENT (larger) suite set — same verdict.

  owner HEAD / branch          ed2772f2d3394ab2a1e37c01159db23a7e54a69d
  integrated commit            ed2772f = merge(0a9a7cc ^1 + 7921954 ^2); 0566f5a ancestor
  origin/handoff-2026-09-07    ed2772f (ls-remote) => LAND + PUSH both already done
  inputs unmoved               0a9a7cc / 0566f5a / 7921954 all resolve to their hashes

Files changed by each owner commit
  0566f5a  tests/test_betting_execution.py                       +384/-4    1 file
  7921954  blm_v4/betting/{8 new + api,executor,provider,store,worker}.py
           + 3 Auto-Bet tests/*.py                              +3988/-39  16 files
  0a9a7cc  blm_v4/live_analytics/fingerprint_stats.py             +8/-9     1 file
  C2 artifact vs 436e73e: 6 files (api.py, dashboard.js, fingerprint_stats.py,
           under_fingerprints.py, test_betting_execution.py, test_under_fingerprints.py)

Reconciliation: CLEAN — one file / one hunk (tests/test_betting_execution.py: the owner's
  auto_betting_enabled insertion). Kept the owner side; that file is a strict superset
  (+380/-0) of the C2 version => nothing lost. No force.

Independent verification (isolated clean checkouts; /tmp/hermes-verify-recon-out.txt,
-newfail-out.txt). Suite set: betting_execution, under_fingerprints, v4_api, live_market_gate,
under_alert_lifecycle, parlay_execution — run on BOTH sides.
  C2 intact        FINGERPRINT_KEYS=("C1","C3","C5","R2"), C2_MOMENTUM_MAX 0, 0 dangling
                   readers, fingerprint layer byte-identical to 0a9a7cc              PASS
  Auto-Bet intact  13/13 blm_v4.betting modules present + import; account_guard OK    PASS
  API imports      import blm_v4.api + import server                                  PASS
  Dashboard        node --check dashboard.js OK; pre-existing gap: index.html refs
                   /static/{stats,explorer}.js which are UNTRACKED => missing in a
                   clean checkout (self-containment gap, pre-existing)                 PASS(syntax)
  regression       base 0a9a7cc 4 failed/181 passed; target ed2772f 4 failed/199 passed;
                   failure SETS IDENTICAL => NEW FAILURES = 0                          PASS
                   pre-existing 4 = live_market_gate::test_api_active_flag_follows_the_gate
                   (data-dependent) + 3x under_alert_lifecycle (frontend vocabulary)
  Auto-Bet suites  120 passed / 0 (subsystem + pre_runtime + manual_contract)          PASS
  owner work       diff ed2772f vs 7921954 = ONLY the 5 C2 files (0 Auto-Bet files);
                   working-tree deletions 0; api.py keeps the 2026-09-25 empty-live-data
                   diagnostic + PaceRef plumbing; dashboard.js owner work committed at
                   5c4c89d => nothing discarded

DEPLOY — BLOCKED (the STOP). Production == the working tree. Tree is ed2772f + other agents'
  30 modified / 71 untracked (+1698/-219). None of the landed files are dirty, so this is a
  MIXED tree, not the verified state. Directive gate "production tree must correspond to the
  verified landed commit" is NOT met => no restart. Also: live procs started 01:19:02/01:19:22
  while the landed C2 files were written at 01:41:44 => running image ≠ tree ≠ commit.
  A restart IS required to make C2 removal live; that restart is what is blocked.

Pre-deploy health baseline (read-only): both units active, NRestarts=0; / 303, /healthz 200,
  /api/* 401 (auth by design); DB reachable (79 objects); collector ticking, settle_worker up.

DECISION REQUIRED: (a) the dirty owners commit/move their WIP, then restart both units; or
  (b) explicit authorization to restart with the tree as-is (ships their uncommitted
  collector/storage/scorecard/execution changes). No C2 redesign. Nothing forced.

## 2026-09-29 (C2 FINAL: LAND → VERIFY → PUSH DONE. DEPLOY = HARD STOP, MIXED TREE.)

result: owner tip re-checked (unchanged) → ed2772f LANDED (ff, no-op: already ff'd by the
  prior session) → FINAL VERIFICATION NEW FAILURES = 0 → PUSHED (0a9a7cc..ed2772f) →
  PRODUCTION TREE INSPECTED → **DEPLOY REFUSED BY THE DIRECTIVE'S OWN GATE** (tree is a
  live mixed tree, 101 dirty paths). No restart, no deploy. Nothing rewritten, nothing
  force-moved, no owner work stashed/reset/discarded.

### 1. OWNER-STATE RE-CHECK (before any write)
  - `git log -1` = ed2772f2d3394ab2a1e37c01159db23a7e54a69d "Reconcile owner Auto-Bet
    execution subsystem (7921954) onto verified C2 artifact (0a9a7cc)".
  - handoff-2026-09-07 == ed2772f ; origin/handoff-2026-09-07 == 0a9a7cc (unchanged).
  - NO commit on ANY ref newer than ed2772f (01:10:26). Owner has NOT advanced the branch.
    `git for-each-ref --sort=-committerdate` : newest = ed2772f (x2 refs).
  - artifact identity re-proved: 0a9a7cc / 7921954 / 0566f5a / ed2772f all resolve to their
    documented SHAs (UNCHANGED); ed2772f parents = 0a9a7cc + 7921954; superseded 0dac405 is
    NOT an ancestor of HEAD (absent — not used).
  => tip unchanged, ed2772f still the correct reconciled artifact. Land proceeded.

### 2. LAND (`/tmp/hermes-land-blm.sh`, rc=0)
      git checkout handoff-2026-09-07   → "Already on 'handoff-2026-09-07'"
      git merge --ff-only ed2772f       → "Already up to date."  (HEAD was already ed2772f)
  dirty fingerprint BEFORE = AFTER = 188cb3300716a41365290fb08199701b (101 paths) → the land
  wrote nothing and touched nothing. Services not restarted by it.

### 3. FINAL VERIFICATION (`/tmp/hermes-verify-blm-land.sh` → /tmp/hermes-verify-blm-land.out)
  Isolated worktrees ONLY (production tree never exercised; live services + other agents' WIP):
  /tmp/hv-landed-ed2772f (ed2772f, 0 dirty) · /tmp/hv-base-0a9a7cc · /tmp/hv-base-7921954
  common suite set (7 files) :
      landed      1 failed, 140 passed   |  base_c2  1 failed, 122 passed  |  base_owner 1 failed, 147 passed
      the ONE failure is the SAME in all three: tests/test_live_market_gate.py::
      test_api_active_flag_follows_the_gate (data-dependent: needs a qualifying game)
  *** (3) NEW FAILURES INTRODUCED BY THE LANDING = 0 ***  (acceptance met); (4) fixed = 0
  owner-only Auto-Bet set: landed 120 passed / base_owner 120 passed
  content checks at the landed commit:
    - C2 cleanup present: FINGERPRINT_KEYS = ("C1","C3","C5","R2") ; 0 refs to
      fingerprint_c2_triggered anywhere in blm_v4+tests ; fingerprint_stats.py no longer reads it
    - Auto-Bet present: 15 modules in blm_v4/betting/ ; 4 Auto-Bet/execution test files present
    - clean-checkout deps: 13/13 blm_v4.betting modules import from a pristine checkout
    - no unintended files: diff ed2772f vs 7921954 = EXACTLY the 5 C2 files
      (blm_v4/api.py, dashboard/static/dashboard.js, live_analytics/fingerprint_stats.py,
      live_analytics/under_fingerprints.py, tests/test_under_fingerprints.py);
      diff ed2772f vs 0a9a7cc = EXACTLY the 17 owner Auto-Bet files (blm_v4/betting/* + 4 tests);
      0 protected paths (execution/collector/scorecard/storage/server/under_alert) touched.
      (The script's "C2-side files touched = 1" line is a regex false positive:
       it matched blm_v4/betting/api.py, not the C2 file blm_v4/api.py.)

### 4. PUSH (`/tmp/hermes-push-blm.sh`, rc=0 — explicitly authorized)
      0a9a7cc..ed2772f  handoff-2026-09-07 -> handoff-2026-09-07   (fast-forward)
  remote SHA == landed SHA == ed2772f ✔ ; local == remote ; ahead/behind = 0/0.
  working tree + services untouched (dirty fp unchanged, NRestarts=0, timestamps unchanged).

### 5. DEPLOY — STOPPED (directive hard stop: "production tree is dirty")
  Production == /home/ubuntu/BLM (WorkingDirectory of both units) ⇒ deploy == the working tree
  goes live. Gate: "The production tree must correspond to the verified landed commit."
      tree clean? NO — 101 dirty paths (30 modified tracked / 71 untracked);
      tracked diff vs HEAD = 30 files, 1698 insertions / 219 deletions.
  Files that a restart WOULD execute differently (24 .py): blm_v1/collector.py, blm_v4/collector.py,
    blm_v4/storage.py, blm_v4/scorecard.py, blm_v4/execution/{store,total_executor,
    betslip_verifier,selection_model}.py + 15 tests/*.py + non-code (DECISIONS.md,
    docs/milestones/CURRENT.md, requirements.txt, watchdog.sh, deploy/blm-collector.service).
  NONE of the 6 C2-landing files is dirty (they now equal ed2772f), so the tree is ed2772f +
  OTHER AGENTS' unverified WIP — a MIXED tree. Also: the live processes (started 01:19:02 /
  01:19:22) predate the ff land that wrote the C2 files at 01:41:44 ⇒ running image ≠ on-disk
  tree ≠ landed commit (three different states).
  => NO restart, NO deploy. Deploy remains blocked until the tree can hold the verified code.

### 6. PRODUCTION SNAPSHOT (read-only; NOT a post-deploy health check — deploy did not happen)
  units: blm-server active/running (pid 23686, since 01:19:02Z), blm-collector active/running
    (pid 23688, since 01:19:22Z), NRestarts=0.
  http: / = 303 ; /healthz = 200 {"status":"ok","service":"blm","auth":true,"users":2} ;
    /api/health = 401 ; /api/fingerprint_stats = 401 (auth-gated expected).
  collector cadence: ticks 181/182/183 completing (0.27s / 0.76s / 6.79s), tracked=5,
    snapshots=495, errors=0 per tick.
  fingerprint stats: server `stats_run` ok (cohort_n=1075, 22–33s, reason=settled_changed).
  db: /mnt/blm-nvme/blm_pokerbet.db opens READ-ONLY (29 tables).
  PRE-EXISTING ERROR (dirty running code, NOT caused by this session): "betual timer persist
    failed: TimeoutError" — _betual_persist_timer → storage.upsert_betual_timer → conn.commit()
    killed by the SIGALRM handler (collector.py:2429). 63 events since 01:19; the SAME signature
    is present in the previous process (10 in 00:00–00:55). Non-fatal (ticks keep completing).

### NEXT ACTIONS (owner actions, then re-run items 5–6)
  1. The owners must commit (or remove) their 30 dirty tracked files — the deploy gate is the
     only remaining blocker. Do NOT stash/reset another owner's work to force it.
  2. Then: inspect the tree = the landed commit → `systemctl --user restart blm-server blm-collector`
     → post-deploy health check.
  3. Consider the pre-existing betual-timer TimeoutError (dirty collector/storage) — it will ship
     with any working-tree deploy.

## 2026-09-29 (C2 FINAL: RECONCILE 0566f5a/7921954 + 0a9a7cc) — ARTIFACTS COMPUTED + VERIFIED IN ISOLATION. NOT LANDED, NOT PUSHED, NOT DEPLOYED.

result: TWO reconciliation artifacts computed and verified in detached throwaway
  worktrees. Shared branch (7921954) and remote (0a9a7cc) were NOT moved.
  NEW FAILURES = 0 for both. Owner work preserved in full. No write to the real
  working tree; no service restarted; nothing deployed.

### Artifacts (additive handles; delete with `git branch -D <name>`)
  1. `c2-recon-owner-tip-20260929`  -> ed2772f  (RECOMMENDED — built on the CURRENT owner tip)
       parents: 0a9a7cc + 7921954   (0566f5a is therefore also preserved as an ancestor)
  2. `c2-recon-owner-0566f5a-20260929` -> 0dac405  (the LITERAL directive target, now superseded)
       parents: 0a9a7cc + 0566f5a
  Both are merge commits; all three inputs still resolve to their original hashes
  (0a9a7cc175c4569f0b0feb59299aa53a92d001a4, 7921954466a4d473d55bf9f9c101bc58537f47cd,
  0566f5a9544d8332668cb20383a6bb83eb5e4ba8). Nothing rewritten, nothing force-moved.

### Conflict + resolution (identical in both artifacts)
  ONE conflicting file: `tests/test_betting_execution.py`, ONE hunk:
        <<<<<<< HEAD
        =======
            store.set_config("auto_betting_enabled", "true")
        >>>>>>> <owner>
  Resolution = keep the owner's line (take-theirs for that file), PROVEN not guessed:
    - resolved file is byte-identical to the owner's version (cmp -> IDENTICAL)
    - diff of the C2-artifact version -> resolved = 380 additions / 0 deletions
      (the owner's version is a strict SUPERSET of the C2 version)
  Both sides had already made the same C2 edit (fingerprints_fired C2,C6 -> C3,C5),
  so the auto-merge took it and only the owner's inserted line conflicted.

### Why ed2772f and not 0dac405
  The owner session kept committing while this ran:
    0566f5a (00:09:34) "Pin the server-side betting safety contract…"  — tests only
    7921954 (00:53:41) "Commit the Auto-Bet execution subsystem…"     — the implementation
  `0dac405` (0566f5a + 0a9a7cc) was INCOMPLETE: 5 of the owner's own tests failed
  because the implementation they assert was still uncommitted
  (`blm_v4/betting/api.py` had no `is_simulated` at 0566f5a; the real tree did).
  7921954 committed that implementation, which is why **ed2772f is green**.

### Verification (ed2772f; common suite set, `blm_pokerbet.db` symlink supplied)
                                 c2only    ownertip   recon    realtree
                                 0a9a7cc   7921954    ed2772f  7921954+dirty
    failures                       1         1          1         1
  - failure set is IDENTICAL in c2only / ownertip / recon  => NEW FAILURES = 0
  - the single failure `test_live_market_gate.py::test_api_active_flag_follows_the_gate`
    is DATA-DEPENDENT ("no quantitatively-qualifying game"): it needs a game with
    checkpoint>=75 AND required > league_avg*1.04 in the CURRENT DB. It passes in the
    real tree (252 s, live data) and fails in a fresh worktree (8 s, no qualifier).
    PRE-EXISTING at both clean bases => not caused by the reconciliation.
  - fresh-worktree environment failures (all reproduced at the clean bases):
    `sqlite3.OperationalError: unable to open database file` (gitignored
    `blm_pokerbet.db -> /mnt/blm-nvme/…` — 3 of the 4 cleared once symlinked);
    untracked runtime state `blm_v4/state/` (gitignored, collector_state.json).
  - realtree-only failure: `test_v4_api.py::test_live_fingerprint_block_serves_c2_provenance`
    — asserts the C2 FORENSIC provenance fields, which exist only in the dirty tree.
    Correctly gone from the artifact: C2 is removed there.

### Owner work preserved (verified in ed2772f)
  - 120/120 passed: test_autobet_execution_subsystem.py + test_autobet_pre_runtime.py
    + test_betting_manual_contract.py
  - 185 passed incl. tests/test_betting_execution.py
  - all blm_v4/betting/* modules (+ account_guard, adapters, engine, fake_provider,
    machine, precheck, replay, session) present in the artifact tree
  - diff ed2772f vs 7921954 = ONLY the 5 C2 files (api.py, dashboard.js,
    fingerprint_stats.py, under_fingerprints.py, tests/test_under_fingerprints.py)
    => NO Auto-Bet file is touched by the C2 side
  - diff ed2772f vs 0a9a7cc = ONLY the owner's additions

### Canonical C2 state in the artifact (unchanged from the accepted artifact)
  FINGERPRINT_KEYS = ("C1","C3","C5","R2"); 0 dangling `fingerprint_c2_triggered`
  reads. The C2/C4/C6 definition labels remain untouched, as instructed.

### Repository / production state (unchanged)
  branch handoff-2026-09-07 = 7921954 ; origin/handoff-2026-09-07 = 0a9a7cc
  divergence origin 10 / local 2 (both directions are FAST-FORWARDS once ed2772f
  lands: 0a9a7cc is a parent of ed2772f, and 7921954 is a parent of ed2772f)
  Working tree: 35 modified / 82 untracked, untouched by this session.
  blm-server / blm-collector: running, NRestarts=0, NOT restarted. No deploy.
  NOTE: other sessions are live in this repo (`blm-dev` worktree, /tmp/autobet-clean,
  /tmp/autobet-sim, /tmp/blm-backend-verify) — the base may move again.

### Next step (NOT performed — needs explicit authorization)
  land:  git checkout handoff-2026-09-07 && git merge --ff-only ed2772f
  push:  git push origin handoff-2026-09-07     # ff from 0a9a7cc
  deploy: ONLY once the production working tree can hold the verified code
          (production runs the working tree: WorkingDirectory=/home/ubuntu/BLM).

## 2026-09-29 (auto-bet owner) — AUTO-BET SUBSYSTEM COMMITTED (7921954). CLEAN CHECKOUT GREEN. NOT PUSHED, NOT DEPLOYED.

AUTO-BET COMMIT: 7921954466a4d473d55bf9f9c101bc58537f47cd
parent: 0566f5a
files (16): blm_v4/betting/{account_guard,adapters,engine,fake_provider,machine,precheck,
  replay,session}.py (new) + blm_v4/betting/{api,executor,provider,store,worker}.py (modified)
  + tests/{test_autobet_execution_subsystem,test_autobet_pre_runtime,test_betting_manual_contract}.py
  3988 insertions / 39 deletions. Nothing else staged; other agents' dirty files untouched.
NOT pushed. NOT deployed. No merge. No reset/stash. 0566f5a and 0a9a7cc byte-identical after.

### CLEAN-CHECKOUT VERIFICATION (isolated worktree at 7921954, 0 dirty paths — /tmp/autobet-verify-out.txt)
1. account_guard imports; all 13 committed blm_v4.betting modules import         PASS
2. the 5 previously-red tests (the reported blocker)                            5 passed
3. backend agent's committed file: test_betting_execution + test_under_fingerprints  88 passed / 0
4. Auto-Bet sweep (betting_execution, betting_manual_contract, autobet_execution_subsystem,
   autobet_pre_runtime, parlay_execution)                                        240 passed / 0
5. fingerprint layer unchanged by this commit (0 files); no C2/C4/C6 logic added —
   the only match is the pre-existing docstring "fingerprint (C1..C6/R2) ...", byte-identical
   to 0566f5a (a range notation, not a C2 fingerprint)
6. 0566f5a = 0566f5a9544d8332668cb20383a6bb83eb5e4ba8 ; 0a9a7cc = 0a9a7cc175c4569f0b0feb59299aa53a92d001a4
=> THE CLEAN-CHECKOUT BLOCKER (ModuleNotFoundError: blm_v4.betting.account_guard) IS CLOSED.

### SCOPE BOUNDARY (deliberately NOT committed)
tests/{test_execution_game_binding,test_pokerbet_live_adapter,test_pokerbet_quarter_dom}.py stay
uncommitted: they import blm_v4/execution/* (and scripts/collect_pokerbet_quarter_dom), a different
subsystem whose own implementation is uncommitted — committing them would only move the same
clean-checkout break there. tests/conftest.py is another agent's dirty file and is NOT needed by the
suites above (they pass with HEAD's conftest).

### STILL BLOCKED FOR HERMES
blm_v4/api.py and blm_v4/dashboard/static/dashboard.js remain dirty -> a plain `git merge` on
handoff-2026-09-07 is still refused by git for api.py. Branch is now 2 ahead / 10 behind origin.

## 2026-09-29 (backend-agent) — OWNED WORK COMMITTED (0566f5a). C2 DIRECTION CONFIRMED = REMOVED. 1 OWNERSHIP DEPENDENCY REPORTED (not forced).

commit: 0566f5a9544d8332668cb20383a6bb83eb5e4ba8
subject: Pin the server-side betting safety contract with live-mode, cap and exposure tests
parent: 436e73e
files (1): tests/test_betting_execution.py   (384 insertions / 4 deletions)
All three previously-blocking owned files are now clean:
  blm_v4/live_analytics/under_fingerprints.py  — restored to HEAD (no diff)
  tests/test_under_fingerprints.py             — restored to HEAD (no diff)
  tests/test_betting_execution.py              — COMMITTED (0566f5a)
NOT pushed. NOT deployed. No reset/stash/force. Hermes' artifact 0a9a7cc untouched.

### C2 DIRECTION — CONFIRMED (evidence, not preference) = REMOVED
- The working-tree changes in the two fingerprint files were the C2-FORENSIC layer
  (39 lines in under_fingerprints.py: dead re-derivation removal + c2_reason /
  c2_operand_source diagnostics; 98 lines / 6 tests in test_under_fingerprints.py).
  It presupposes C2 and is obsolete under the canonical REMOVE (this file §1.d), so it was
  dropped, not committed. Preserved round-trippable as:
      /tmp/backend-agent-preserved/c2-forensic/c2-forensic-additions.patch   (git apply)
- C2 reference census in blm_v4/live_analytics/under_fingerprints.py:
      HEAD 19 lines  ->  committed line 19 (unchanged; the file is at HEAD)  ->  Hermes artifact 2
  (the "20 C2 refs" in the directive measures the pre-drop worktree: 44 lines; 5 of them the
  `"C2"` string literal). FINGERPRINT_KEYS after the artifact: ("C1","C3","C5","R2").
- The 4 stale C2/C4/C6 fingerprint expectations in tests/test_betting_execution.py were aligned
  to the canonical set (C1/C3/C5/R2) — the committed file carries ZERO C2 references.
- The C2 removal itself is Hermes' artifact; it was neither performed nor modified here.

### VERIFICATION (numbered, real output — logs under /tmp)
1. baseline, dirty tree : test_betting_execution + test_under_fingerprints = 94 passed / 0 failed
      (/tmp/blm_backend_baseline.txt)
2. committed state     : same two suites at 0566f5a = 88 passed / 0 failed (/tmp/blm_backend_after.txt)
3. betting/execution/Auto-Bet sweep (7 suites) at 0566f5a = 271 passed / 0 failed (/tmp/blm_backend_sweep.txt)
4. 0566f5a + artifact 0a9a7cc, isolated worktree /tmp/blm-backend-verify =
      101 passed / 5 failed (/tmp/blm_backend_merge_verify.txt) — the 5 are NOT C2-caused, see below
5. merge shape vs 0a9a7cc for the committed file: +380 / -0 (purely additive). ONE trivial
   conflict — the auto_betting_enabled insertion sits on the line Hermes rewrites; resolution is
   "take the committed side" (proven: committed file == artifact file + 380 lines, so nothing is lost).
6. ad-hoc re-verification (all PASS) — /tmp/hermes-verify-backend2-out.txt: commit = 1 file 384+/4-;
   committed file 0 C2 refs; artifact keys ("C1","C3","C5","R2"); committed file = artifact +380/-0;
   working tree 88/0; pristine 0566f5a 5F/83P with ZERO fingerprint mentions in those failures;
   same 5 with the uncommitted impl 5P; refs unmoved. Also proven: the impl cannot even be imported
   without its UNTRACKED siblings (ModuleNotFoundError: blm_v4.betting.account_guard) — so the
   modified 5 alone are not a commit-able unit.

### OWNERSHIP DEPENDENCY — REPORTED, NOT FORCED  (5 of my tests need uncommitted code)
Green only where the uncommitted betting implementation is present:
  test_bet_count_additional_execution_blocked_after_max
  test_api_dry_run_amount_is_simulated_not_real
  test_api_provider_ref_in_recent_executions
  test_recent_executions_populated_after_dry_run
  test_exposure_blocks_on_cumulative_overflow
Proof: at 0566f5a ALONE in a clean worktree -> 5 failed / 83 passed (/tmp/blm_dependency_map.txt).
They pass in the working tree (88/0) because the implementation is uncommitted there.
Required implementation — NOT in this agent's authorized set, NOT committed here:
  5 modified   : blm_v4/betting/{api,executor,provider,store,worker}.py     (+499 lines, mtime 09-28 19:35)
  >=1 untracked: blm_v4/betting/{machine,session,adapters,precheck,engine,replay,account_guard,fake_provider}.py
  The 8 untracked modules are the AUTOBET EXECUTION SUBSYSTEM's own files (STATUS 2026-09-28 section,
  "Commit NOT performed ... Ready to commit ... on request"), and §5 item 2 of this file assigns the
  +380 tests to the "Auto-Bet owner". Committing another owner's uncommitted subsystem to make my
  tests green is forbidden by the standing multi-agent rule -> STOP-and-report instead.
=> OPERATOR DECISION: (a) that owner commits the implementation (branch green in a clean checkout),
   or (b) keep test-only (green in the working tree, which is the /home/ubuntu/BLM deploy target).

### REMAINING BLOCKERS FOR HERMES (not this agent's files, unchanged)
- blm_v4/api.py and blm_v4/dashboard/static/dashboard.js are still dirty -> a plain `git merge` on
  handoff-2026-09-07 is still REFUSED by git for api.py.
- `git merge --ff-only 0a9a7cc` is no longer possible: 0566f5a is a CHILD of 436e73e, not an ancestor
  of 0a9a7cc. Use a normal merge — the artifact touches no line of the committed file except the single
  trivial conflict above.

## 2026-09-29 (C2 FINAL: PUSH AUTHORIZED) — 0a9a7cc PUSHED TO origin/handoff-2026-09-07. NOT LANDED. NOT DEPLOYED.

result: C2 ARTIFACT PRESERVED ON REMOTE. Push = fast-forward 0209585 → 0a9a7cc, done
  with an EXPLICIT SHA SPEC (`git push origin 0a9a7cc:refs/heads/handoff-2026-09-07`)
  so the local branch ref was NOT moved and the working tree was NOT touched.
  remote tip now: 0a9a7cc175c4569f0b0feb59299aa53a92d001a4 (ls-remote verified).
scope: no commit, no merge, no stash, no reset, no checkout, no restart, no deploy.

### CONCURRENT-SESSION CHANGE OBSERVED (not caused by this session)
While this directive ran, the OWNER session (abwarren) moved the branch:
  - `0566f5a` "Pin the server-side betting safety contract with live-mode, cap and
    exposure tests" (2026-09-29 00:09:34) — commits tests/test_betting_execution.py
    (+384/−4) on top of 436e73e. Local branch is now 0566f5a, NOT 436e73e.
  - It also REVERTED the uncommitted C2-forensic edits in under_fingerprints.py
    and tests/test_under_fingerprints.py (C2 refs 20 → 16 = HEAD's count); both
    files are now IDENTICAL TO HEAD. Tree is 35 modified / 79→80 untracked.
  ⇒ the 3 files that blocked the landing earlier are now CLEAN. The landing is
    therefore plausibly unblocked now, but it was NOT attempted (directive: do NOT
    merge locally). The tree is being actively edited right now (files touched at
    00:10).
  ⇒ CONSEQUENCE: remote 0a9a7cc does NOT contain 0566f5a (0a9a7cc predates it).
    The owner's next push of 0566f5a will be NON-fast-forward and needs a merge.
    Nothing was lost: 0566f5a exists locally and 0a9a7cc contains no owner work.
    Divergence after push: origin 10 / local 1.

### Push verification (all read-only)
  1. remote contains 0a9a7cc                    YES (ls-remote + fetch + is-ancestor)
  2. local real HEAD                           0566f5a (moved by the OWNER's commit,
     not by this session; this session never ran a local ref write)
  3. owner files untouched                      md5 stable across the push;
     35 modified / 80 untracked (was 35/79 — +1 = scripts/q1_vs_q2_frequency_2026-09-29.py,
     written 00:10:33 by another session, not this one)
  4. services                                   blm-server / blm-collector NOT restarted
     (NRestarts=0, ActiveEnterTimestamp unchanged 2026-09-29 00:00:22/00:00:42 UTC)
  5. deployment                                 NOT performed (working tree deploy forbidden)

### STILL BLOCKED
  Deployment remains blocked: production runs from the working tree
  (WorkingDirectory=/home/ubuntu/BLM), and the tree is a live mixed tree being edited
  by other sessions. Deploy only once the tree can contain the verified code.

## 2026-09-29 (C2 FINAL: LAND→PUSH→DEPLOY) — LANDING REFUSED BY GIT (OWNERSHIP). NOT PUSHED. NOT DEPLOYED.

result: STOPPED at step 1 of the directive, as the directive requires ("if Git
  reports a genuine ownership conflict, STOP and report it rather than forcing
  the merge"). The refusal was reproduced empirically, not asserted.
scope: READ-ONLY against the repo + a throwaway worktree. No commit, no stash,
  no reset, no ref move, no push, no restart, no deploy, no service touched.
  Main tree untouched: 38 modified / 79 untracked, branch ref still 436e73e.

artifact: 0a9a7cc (detached; ddc4674 → 0a9a7cc = fingerprint_stats.py only, +8/−9)
  ancestry check: 436e73e AND origin/handoff-2026-09-07 (0209585) are BOTH
  ancestors of 0a9a7cc ⇒ landing is a pure FAST-FORWARD, and `git merge-tree
  --write-tree HEAD 0a9a7cc` = rc 0 (CLEAN, zero commit-level conflicts).
  The landing is blocked by the WORKING TREE ONLY.

### The refusal (evidence — throwaway worktree /tmp/blm-land-proof, since removed)
    git merge --ff-only 0a9a7cc   → rc=1
    git merge 0a9a7cc             → rc=1
    error: Your local changes to the following files would be overwritten by merge:
            blm_v4/live_analytics/under_fingerprints.py
            tests/test_betting_execution.py
            tests/test_under_fingerprints.py
  The dirty files were md5-compared before/after the refusal: BYTE-IDENTICAL
  (nothing was written, nothing was lost). Branch ref verified still 436e73e.

### Why the tree blocks it (write set ∩ dirty set)
  landing write set (6): api.py, dashboard/static/dashboard.js,
    live_analytics/fingerprint_stats.py, live_analytics/under_fingerprints.py,
    tests/test_betting_execution.py, tests/test_under_fingerprints.py
  dirty overlap (3):       under_fingerprints.py, tests/test_betting_execution.py,
                           tests/test_under_fingerprints.py
  Owner work held in those files (dirty vs 436e73e):
    - under_fingerprints.py        +39/−8   C2 forensic layer (c2_reason,
      c2_operand_source, dead re-derivation cleanup) — "C2 forensic audit 2026-09-23"
    - tests/test_under_fingerprints.py +98  6 C2-forensic tests
    - tests/test_betting_execution.py +380  18 Auto-Bet/execution tests
      (live-mode gating, bet_count max, cumulative exposure, browser stake injection)
  NONE of them were authored by this session; §5 of the 2026-09-28 entry assigns
  them to their owners. Committing or stashing them is forbidden.

### Artifact re-check (read-only, artifact-only)
  - final delta = ONE file (fingerprint_stats.py, +8/−9) ✔
  - removes the live `c2 = {… fingerprint_c2_triggered …}` read; `inter` becomes
    `c1 & r2`; collapse definition string updated to "C1 AND R2" ✔
  - `git grep fingerprint_c2_triggered 0a9a7cc -- blm_v4 tests` → NONE ✔
  - canonical keys intact: FINGERPRINT_KEYS == ("C1","C3","C5","R2") (line 114) ✔
  - remaining C2/C4/C6 definition labels deliberately KEPT (fingerprint_stats.py
    740–749 legend entries) — as the directive instructs, NOT touched ✔
  - protected paths / Auto-Bet untouched: diff 436e73e→0a9a7cc touches NO file
    under blm_v4/execution, blm_v4/betting, blm_v4/collector.py, blm_v1/,
    scorecard.py, storage.py, server.py, under_alert.py, pace_projector.py ✔
  - seq-sort fix present (dashboard.js 1790–1812: tie-break `b.seq - a.seq`) ✔

### Direction parity (why the deploy would matter)
  running tree under_fingerprints.py: FINGERPRINT_KEYS = 7 keys incl. C2 (C2 is
  LIVE in production today; 20 C2 refs) vs artifact 4 keys / 2 refs.
  ⇒ landing+deploy CHANGES live behaviour. A working-tree deploy would ship the
  OPPOSITE of the verified artifact, and is explicitly forbidden by item 4.

### Deployment topology (unchanged)
  blm-server.service (pid 1842, /usr/bin/python3 /home/ubuntu/BLM/server.py) and
  blm-collector.service (pid 1840, python3 -m blm_v4.collector --tick 10) both run
  with WorkingDirectory=/home/ubuntu/BLM ⇒ deploy == the WORKING TREE goes live.

### PRE-DEPLOY BASELINE (read-only; NOT a post-deploy health check)
  units: blm-server active, blm-collector active, NRestarts=0, both up since 00:00:22Z
  collector: tick 21 done in 1.17s, tracked=3 snapshots=48 errors=0
  server: scorecard_run ok; stats_run ok (cohort_n=1074, 34.9s); settle_worker ok
  http: / = 303, /healthz = 200, /api/health = 401, /api/fingerprint_stats = 401 (auth-gated)
  db: /mnt/blm-nvme/blm_pokerbet.db reachable (79 objects)

### UNBLOCK SEQUENCE (owner actions; then re-run the directive)
  1. owner commits dashboard.js-era work? (already landed in 436e73e) — the only
     outstanding owner files are the 3 above:
       under_fingerprints.py + tests/test_under_fingerprints.py — commit WITHOUT the
         obsolete C2-forensic additions (canonical C2 = removed), or with them if
         the owner insists (then reconcile again);
       tests/test_betting_execution.py — Auto-Bet owner commits the +380 tests.
  2. then:  git checkout handoff-2026-09-07
            git merge --ff-only 0a9a7cc        # pure fast-forward, no conflicts
  3. re-run the required suites → NEW FAILURES = 0
  4. only then: push origin handoff-2026-09-07 (ff from 0209585) → restart both
     units (working-tree deploy) → health check.
  NOTE: the push itself is NOT blocked (ff from 0209585, artifact contains no
  owner work) — but it was NOT attempted, because the directive orders
  LAND → PUSH and the artifact must be the LANDED branch state.
  Rollback state: nothing to roll back — no write was made anywhere.

## 2026-09-28 (C2 resolution) — CANONICAL C2 = REMOVED. LANDING BLOCKED BY OWNERSHIP. NOT PUSHED.

result: C2 DIRECTION RESOLVED (evidence-based) = REMOVED (C1, C3, C5, R2).
  The verified candidate 84df0db encodes that exactly. Landing is BLOCKED by the
  takeover's own hard stops — reported, not forced.
scope: READ-ONLY. No source/test/config/DB/service touched. Main tree untouched
  (still cdd6f08, 45 modified / 75 untracked). No push. No deploy. No restart.

### 1. CANONICAL C2 — EVIDENCE (C2 is REMOVED from the live fingerprint layer)
a. `origin/handoff-2026-09-07` carries 7 owner-pushed commits (abwarren,
   2026-09-22 20:38) that remove C2/C4/C6 coherently across implementation,
   tests, dashboard display, betting expectations and the API input set.
b. `docs/milestones/STATUS_2026-09-27_pre_autobet_subsystem.md` states the
   resolution verbatim: the missing 7 "removed C2 (and C4/C6) from fingerprint
   evaluation and dashboard display"; "fingerprint set remains `C1, C3, C5, R2`";
   "C2 in fingerprint eval: 0". A prior agent merged it as 60c88cb (never landed).
c. `analysis/c2_forensic_backtest_2026-09-23.md`: on all 15790 boundary
   candidates C2 TRUE → 53.90% UNDER vs the 67.20% trigger baseline — C2 does
   NOT hold up, which is consistent with its removal.
d. The uncommitted C2 work is a *forensic layer* (diagnostics only: 39 lines in
   under_fingerprints.py + 98 in tests), not new C2 logic — it presupposes C2
   and is obsolete once C2 is removed.
=> CANONICAL: FINGERPRINT_KEYS == ("C1", "C3", "C5", "R2"). No C2/C4/C6.

### 2. CANDIDATE 84df0db VERIFIED (c2-removal-merge-candidate)
- Merge of the 7 remote commits into cdd6f08; 0 conflicts.
- Scope: exactly 5 files (api.py 9, dashboard.js 3, under_fingerprints.py 85,
  test_betting_execution.py 8, test_under_fingerprints.py 358).
- PROTECTED AREAS UNCHANGED: under_alert.py (alert condition + REQUIRED_MARGIN
  = 1.04), collector.py, pace_projector.py, execution/*, scorecard.py,
  storage.py, server.py. No threshold/gate/pace change. C2_MOMENTUM_MAX absent.

### 3. FAILURE CLASSIFICATION (3-way; clean-HEAD vs candidate vs dirty tree)
    clean-HEAD   11 failed / 140 passed
    candidate    11 failed / 133 passed   <-- IDENTICAL failure set to HEAD
    dirty-tree    2 failed / 173 passed
- The C2 merge introduces ZERO new failures (candidate-only set is EMPTY).
- 11 failures are PRE-EXISTING at HEAD (resulted-alert frontend contract + 2
  unrelated: test_metric_labels_explicit, test_no_model_series_anywhere).
- 9 of them are fixed ONLY by the uncommitted frontend work.

### 4. WHY LANDING IS BLOCKED (two independent hard stops)
A. OWNERSHIP: the merge rewrites exactly the 5 files that carry OTHER AGENTS'
   uncommitted work (1272 insertions across auth/execution/frontend/pace-ref/
   C2-forensic). A plain merge on the branch is REFUSED by git ("local changes
   would be overwritten"). Landing needs those files committed or stashed —
   forbidden by §5 ("the owner must commit them") and the hard stop.
B. FRONTEND REGRESSION: landing the candidate ALONE drops the uncommitted
   frontend resulted-alert fix (dashboard.js), regressing 9 tests — the hard
   stop "frontend changes disappear". Verify: test_settle_worker::
   test_no_final_remains_no_final_and_never_coloured passes in the dirty tree,
   fails in clean HEAD and in the candidate.

### 5. RECONCILIATION REQUIRED (owner actions, in order) — NOT performed here
1. The frontend agent commits dashboard.js (the resulted-alert NO FINAL fix),
   preserving it — do NOT reset/clean/stash.
2. The owners of api.py / tests/test_betting_execution.py /
   tests/test_under_fingerprints.py / under_fingerprints.py commit their work
   with the obsolete C2-forensic additions dropped (canonical C2 = removed).
3. Then land the verified candidate:
       git checkout handoff-2026-09-07
       git merge --no-ff c2-removal-merge-candidate
       git push origin handoff-2026-09-07        # only after re-running the suites
   (Alternatively the owners commit first and a normal merge lands, since the
   candidate merge is conflict-free at the commit level.)

## Git State
Branch: handoff-2026-09-07   (UNCHANGED — cdd6f08)
Candidate: 84df0db on `c2-removal-merge-candidate` (verified, unlanded)
Remote: git@github.com:abwarren/BLM.git (SSH)
Push status: NOT ATTEMPTED (hard stop). behind=7 ahead=8 until landed.
Working tree: UNCHANGED — 45 modified / 75 untracked (other sessions').
Next agent should start from: item 5 above (owner commits), then land + push.

## 2026-09-28 (integration) — SAFE C2-REMOVAL INTEGRATION — VERIFIED, NOT LANDED, NOT PUSHED

result: INTEGRATION COMPUTED + VERIFIED IN ISOLATION. The main branch and the
  dirty working tree were NOT modified. Awaiting an operator decision (below).
scope: git-only. No source file, no test, no config, no DB, no service touched.
  The C2/fingerprint/alert/betting/API/collector/frontend logic is unchanged.

### WHAT WAS INTEGRATED
- Origin's 7 C2-removal commits (820b912…0209585) merged into the local line
  (cdd6f08) in an ISOLATED worktree, never in the production working tree.

### VERIFIED CANDIDATE
- Merge commit : 84df0dbcc93fdcf2141da0ef5638b7e4a7c478f3
- Parents      : cdd6f08 (local) + 0209585 (origin)
- Handle       : branch `c2-removal-merge-candidate` (additive; delete with
                 `git branch -D c2-removal-merge-candidate`)
- Merge-base   : 53c5556 — the local 8-commit line and the remote 7-commit
                 line are SIBLINGS, not ancestor/descendant.
- cdd6f08 reachable from the candidate : YES
- conflicts (commit level)             : NONE — `git merge-tree --write-tree`
                 returned rc=0 (clean); `git merge` in the worktree auto-merged
                 blm_v4/api.py + dashboard.js via the 'ort' strategy.

### THE BLOCKER (why the candidate is NOT landed on the branch)
The merge rewrites exactly 5 files, and ALL 5 carry UNCOMMITTED work from other
sessions:
    blm_v4/api.py · blm_v4/dashboard/static/dashboard.js ·
    blm_v4/live_analytics/under_fingerprints.py ·
    tests/test_betting_execution.py · tests/test_under_fingerprints.py
A plain `git merge` on the branch is therefore REFUSED by git (verified on a
throwaway copy, dirty files intact afterwards):
    error: Your local changes to the following files would be overwritten by
    merge: … Please commit your changes or stash them before you merge.
    Aborting.  Merge with strategy ort failed.   (rc=2)
Landing it therefore requires either committing or stashing ANOTHER agent's
uncommitted work — both forbidden by the takeover directive. Moving the branch
ref by plumbing was rejected as "overwriting the dirty tree's meaning" (TASK 2).

### NOTE — SEMANTIC TENSION (operator's call, NOT resolved here)
The remote stream REMOVES C2; the uncommitted working-tree edits EXPAND C2
(under_fingerprints.py C2 lines: base 28 → HEAD 28 → remote 2 → worktree 44).
Each side's tests are internally consistent and green, but they encode OPPOSITE
fingerprint sets. Resolving that is a fingerprint/alert decision and is on the
do-not-touch list — deliberately left to the operator.

### TEST EVIDENCE
- INTEGRATED candidate (isolated worktree): test_under_fingerprints +
  test_betting_execution + test_live_cold_cache_latency → 66 passed.
  FINGERPRINT_KEYS == ('C1','C3','C5','R2')  (C2 removed).
- CURRENT dirty tree: same C2/fingerprint suites → 94 passed (C2 kept).
- Cold-cache latency guard: passes in both.

## Git State
Branch: handoff-2026-09-07   (UNCHANGED — still at cdd6f08)
Commit: cdd6f08 — local; candidate integration = 84df0db on `c2-removal-merge-candidate`
Remote: git@github.com:abwarren/BLM.git (SSH)
Push status: NOT ATTEMPTED (TASK 2 STOP). Same non-fast-forward blocker as before:
  the branch is ahead 8 / behind 7 until the candidate is landed.
Working tree: UNCHANGED by this task — 45 modified + 75 untracked (other sessions').
Uncommitted files: untouched.
Next agent should start from: obtain the operator decision on the 5 dirty files
  (they must be committed/stashed by their owner, or the C2 direction resolved),
  then land 84df0db and push. Recommended land path when the tree is clear:
      git checkout handoff-2026-09-07
      git merge --no-ff c2-removal-merge-candidate      # == the verified merge
      git push origin handoff-2026-09-07

## 2026-09-28 (profiling) — /api/v4/live COLD-PATH LATENCY — MEASURED + VERIFIED, NO FUNCTIONAL CHANGE

result: INVESTIGATION COMPLETE — nothing functional to deploy.
scope: READ-ONLY profiling of the /live request path against the PRODUCTION DBs
  (`mode=ro`; all sidecar I/O redirected to a /tmp copy). NO production code was
  changed by this session. The only new repository artifact is a test:
  `tests/test_live_cold_cache_latency.py` (commit cdd6f08). No alert, betting,
  execution, collector, API or frontend logic was modified.

### MEASURED (prod payload: 100 games, 7 live, 10 carrying historical context)
- cold `v4_live()` 9 490 ms · warm 2 052 ms
- `_historical_context_for` (x10) 7 353 ms cold / 0.1 ms warm
    · `hc.refresh_stats` (once per process) 4 617 ms — whole-archive SCAN, 1.34M rows
    · `benchmark._stats` (once per game, x9) 2 702 ms — 213-300 ms/game
- Root cause = two SQLite scans. The population query ALREADY uses
  `idx_clean_proj_bench` (index seek 8 ms) but pays ~142 ms of per-row table
  lookups (`status`, `actual_pts_per_min` absent from the index) + ~63 ms
  cross-DB ledger join. Population cells hold 6 083-19 136 rows.
- Ruled out BY MEASUREMENT: Python math (~1 ms), SQL-side AVG/COUNT (no gain),
  connection/ATTACH (<1 ms), duplicate scans (exactly one per game).
  `_HCTX_CACHE` keying + 120 s TTL verified correct; misses are not serialised.

### VERIFICATION (evidence)
- DATA EQUIVALENCE: 400 games / 54 benchmark keys — byte-identical JSON with the
  candidate covering index ON vs OFF (frozen inputs, fresh sidecar per run).
- FOCUSED (22 suites: /live + historical-context + alert + market + Auto-Bet):
  372 passed, 1 failed — the PRE-EXISTING
  `test_under_alert_lifecycle::test_no_alert_vocabulary_regression`.
- FULL SUITE: 13 failed / 1 888 passed / 1 skipped — a strict SUBSET of the
  pre-existing baseline; ZERO new failures. The new guard passes.
- The new guard is proven NON-VACUOUS by mutation (`_HCTX_TTL_S=0` ⇒ the warm
  poll re-scans 6x, so the zero-rescan assertion is load-bearing).

### NOT DONE (deliberate, per the DO-NOT list)
- Covering index `idx_clean_proj_bench_cover` MEASURED (per-game cell 213 → 86 ms;
  the 120 s spike would fall ~4.3 s → ~2.8 s) but NOT applied: it is a schema
  change, and the collector's writable `ensure_schema` would materialise it in
  production. `refresh_stats` is unaffected (its scan needs columns the index
  does not carry).
- `hc.refresh_stats` NOT moved off the request path (display-only hindsight, but
  not provably output-identical — directive §3 forbids).

## Git State
Branch: handoff-2026-09-07
Commit: cdd6f08 — "Add a cold-cache /live latency regression guard for the historical-context path"
Remote: git@github.com:abwarren/BLM.git (SSH)
Push status: BLOCKED — non-fast-forward. `git push origin handoff-2026-09-07`
  rejected: the branch is ahead 8 / behind 7. The remote carries 7 commits this
  box does not have (820b912…0209585, all "Remove C2 momentum fingerprint" work,
  i.e. another session's). Integrating them needs a merge/rebase, which was NOT
  attempted: the tree is dirty (44 modified + many untracked) and §19
  multi-agent protection forbids risking other agents' uncommitted work.
Working tree: DIRTY — 44 modified + ~80 untracked files, carried by other
  sessions. This session modified NO functional file.
Uncommitted files: everything EXCEPT tests/test_live_cold_cache_latency.py.
  Notably the carried dashboard/alert-frontend edits
  (dashboard.js / index.html / styles.css), the betting/execution/auth work,
  and STATUS.md itself (untracked — never committed to the repo).
Next agent should start from: `git fetch origin`, then integrate the 7 C2-removal
  commits per §19 (integrate safely, no force-push), re-run the relevant suites,
  then `git push origin handoff-2026-09-07` to publish cdd6f08. Then decide
  (a) whether to apply the covering index, (b) commit scope for the carried work.

### Recovery command for the blocked push
    git fetch origin
    git merge origin/handoff-2026-09-07       # rebase if the operator prefers
    # resolve any conflict WITHOUT discarding other agents' changes (§19)
    /usr/bin/python3 -m pytest -q tests/      # re-confirm the baseline
    git push origin handoff-2026-09-07

### DEPLOYMENT HANDOVER
- NOTHING to deploy from this session — no functional change. Production
  `blm-server` (port 2262) is untouched; it keeps running the pre-session tree.
- IF the covering index is approved: add it to `blm_v4/clean_metrics.py` beside
  `idx_clean_proj_bench` (idempotent `CREATE INDEX IF NOT EXISTS`). The collector
  materialises it on its next writable connect; the running `blm-server` sees it
  on its next query. No restart is strictly required for the index itself.
- Restarting `blm-server` / `blm-collector` stays OPERATOR-GATED (§5.2). Note the
  auth work (top entry) still needs a restart to go live.

## 2026-09-28 (auth) — LOGIN + AUTHENTICATION LAYER — IMPLEMENTED, STAGING-VERIFIED (directive §1–§14)

result: IMPLEMENTED — VERIFICATION GREEN ON STAGING / PRODUCTION DEPLOYMENT PENDING
  (production has NOT been restarted, so production is NOT yet protected —
   see DEPLOYMENT/RUNTIME STATE below; do not read this as "live")
scope: ADDITIVE. 9 new modules + 3 new static assets + 5 new test suites.
  Existing alerts, collection, analytics, settlement, WebSocket and AUTO-BET
  logic were NOT modified. The only edits to pre-existing tracked files are:
  `server.py` (+1 install call, +1 public /healthz), `blm_v4/test_stack.py`
  (auth wired into the TEST stack + /healthz), `dashboard.js` (one fetch
  wrapper + one bootstrap IIFE), `dashboard/static/index.html` (user pill +
  SIGN OUT button), `tests/test_test_stack.py` (fixture signs in),
  `requirements.txt` (+bcrypt), `DECISIONS.md` (decision record).

### WHAT WAS BUILT
- `blm_v4/auth/config.py`     env-sourced config; PUBLIC path allow-list; safe defaults
- `blm_v4/auth/passwords.py`  bcrypt (SHA-256 pre-hash, `bcrypt_sha256$…`), constant-shape verify
- `blm_v4/auth/store.py`      blm_auth.db — users / sessions / auth_audit; hashes only, never plaintext
- `blm_v4/auth/service.py`    login / validate / logout / CSRF; the server-side authority
- `blm_v4/auth/ratelimit.py`  per-identity + per-IP sliding window with lockout
- `blm_v4/auth/middleware.py` pure-ASGI guard: pages redirect, APIs 401, WS closed 1008, CSRF on mutating
- `blm_v4/auth/api.py`        /login page, /api/auth/{login,logout,me,admin/probe}, role dependencies
- `blm_v4/auth/seed.py`       idempotent, env-driven seeder (never prints/overwrites silently)
- `blm_v4/auth/__init__.py`   ONE call: `install(app, root)`
- `dashboard/static/login.html|login.css|login.js|blm-login-arena.jpg` — the BLM login page
- `tests/test_auth_{login,authorization,security,seed,ui}.py` — 119 tests

### DATABASE / SCHEMA
- NEW database `blm_auth.db` (repo root; gitignored via `*.db`) + `test_env/blm_auth_test.db` for the TEST stack.
- NEW tables: `users`, `sessions`, `auth_audit` (created idempotently; additive migrations only).
- NO existing database, table, column or row was created, altered or deleted.
- PROVISIONED 2026-09-28: `admin` (role=admin) and `bradblm` (role=user), bcrypt cost 12,
  plaintext-absent from the database file, both verified end-to-end. Seed re-run = unchanged (idempotent).

### ROUTES
PUBLIC: `/login`, `/api/auth/login`, `/static/*`, `/dashboard/static/*`, `/healthz`, favicons.
PROTECTED (were open before this change): `/`, `/dashboard`, `/dashboard/*`, `/api/v2/*`, `/api/v4/*`,
`/metrics`, `/docs`, `/openapi.json`, `/redoc`, and the `/ws` handshake.

### TEST EVIDENCE
- NEW auth suites: 119 passed, 1 skipped (`test_auth_login` 25, `test_auth_authorization` 30,
  `test_auth_security` 33, `test_auth_seed` 12, `test_auth_ui` 19).
- Auth + test-stack together: 128 passed, 1 skipped.
- FULL SUITE after change: **19 failed, 1879 passed, 1 skipped** in 588s.
- PRE-CHANGE BASELINE (same tree, this session): **17 failed, 1759 passed, 3 errors** in 541s.
- DELTA: +119 passing (the new suites); **ZERO new failures**. The 19 are exactly the pre-existing
  classes already documented: frontend-vocabulary/dirty-tree (7), freeze/temporal (6), latency+liveness
  guard (6). The 3 baseline ERRORS (`test_live_route_latency_guard`) now collect and FAIL — same
  pre-existing class, not a regression; one latency test that failed at baseline now passes.
  Attribution proven, not assumed: `blm-signal-row` (the string that trips the vocabulary tests) is
  present at HEAD, and the new auth JS blocks contain none of the banned vocabulary.
- CREDENTIAL LEAK SCAN: operator-supplied credentials absent from all 434 tracked + 86 untracked-present
  files (opt-in test `test_no_plaintext_credentials_in_tracked_source` passes with `BLM_LEAK_CHECK_STRINGS`).

### LIVE STAGING SMOKE TEST (real HTTP, TEST stack on 127.0.0.1:2264)
1 anon `/` → 303 `/login?next=/` · 2 `/login` → 200 (BLM design + TEST banner) · 3 anon `/api/v4/live` → 401 ·
4 bad password → 401 generic · 5 empty fields → 400 validation · 6 admin login → 200 + HttpOnly cookie ·
7 dashboard → 200 (AUTO BETTING panel intact, SIGN OUT present) · 8 `/api/v4/live` → 200 ·
9 `/api/auth/me` → identity + CSRF · 10 POST without CSRF → 403 · 11 POST with CSRF → 200 ·
12 `/api/auth/admin/probe` admin → 200 · 13 logout → session revoked, `/` → 303 ·
14 user login → 200 · 15 user → admin probe → 403 · 16 user → dashboard + live API → 200 ·
17 anon WebSocket → rejected (HTTP 403 before accept) · 18 `/metrics` `/docs` `/openapi.json` anon → 303 ·
19 no-JS form login → 303 `/` + cookie; bad form login → 303 `/login?error=invalid`.
All 24 checks PASS.

### DEPLOYMENT / RUNTIME STATE
- PRODUCTION (blm-server, PID 301286, port 2262, started 2026-09-28T07:23Z) is running code that
  PREDATES this work. `blm_auth.db` is provisioned but the guard is not loaded in that process,
  so **production is still unauthenticated until it is restarted**. Restart is OPERATOR-GATED.
- The stale TEST stack on port 2263 (PID 240068, started 04:58) is also pre-change; leave or restart
  as the operator prefers.
- Production env for the restart: `BLM_ENV=production` (already set in `deploy/blm-server.service`)
  ⇒ Secure cookie + X-Forwarded-For trust on automatically. No new environment variable is required.

### NEXT ACTIONS
1. OPERATOR: authorise `systemctl restart blm-server` (and `blm-collector` if the tree is reloaded),
   then run the production smoke: anon `/` → 303 `/login`, both seeded accounts → 200, `/api/v4/live`
   → 401 anon / 200 authed, AUTO BETTING panel + live data + `/ws` unchanged.
2. OPERATOR: decide on the login-page affordances. "Forgot password?" and "Continue with Google"
   render DISABLED (inert, with tooltips) because no reset flow and no SSO provider exist. Silence
   them by editing `login.html`, or keep them as a design placeholder.
3. Commit scope: NONE committed. The tree carries other sessions' uncommitted work; the auth-scoped
   files are ready to stage as a single commit on request (per AGENTS.md §19 the branch is diverged
   from origin, so a push needs the divergence integrated first).
4. `/metrics` is now authenticated — set `BLM_AUTH_PUBLIC_PATHS=/metrics` if a Prometheus scrape is
   ever wired up.

### KNOWN RISKS
- Production is NOT protected until restarted (highest-priority residual risk).
- Login throttle is in-memory: a restart clears counters (the durable trail is `auth_audit`).
- Sessions are stored in SQLite and validated per request; at the dashboard's 5-second poll cadence
  this is one indexed read per poll — measured negligible, but a future multi-process deployment
  would need the session store shared (it already is, via the file) and a purge cadence.
- `blm_auth.db` is gitignored, so a disaster-recovery restore must re-run the seeder.


## 2026-09-28 (later) — AUTOBET FIX/VERIFY/DEPLOY — DEPLOYED + HEALTH GATE PASSED (directive §1–§8)

deployment: blm-server restarted 2026-09-28T07:23:37Z, PID 301286, from the tested working tree
root_cause: RUNNING PROCESS WAS STALE — server started 2026-09-27 22:20:52 but the betting layer
(GATE-8 account guard, executor, provider, store, worker, api, dashboard AUTO BETTING panel) was
edited 21:53–04:33; production had been executing pre-GATE-8 code for 6 hours (inert: kill switch OFF,
no executions — verified ledger empty). No code defect found; the defect was deployment lag.
test_gate: 194/194 PASS on the exact tree deployed (5 betting suites)
health_gate: ALL §6 CHECKS PASS — services active, NRestarts=0, no error-level logs since restart,
API 200 (betting/status, v4/status, v4/live), frontend 200 with AUTO BETTING panel, DB serving,
collector heartbeat fresh (tick 2636, 0 errors, 6 games), memory pressure RELIEVED by restart
(13.2 GB available; was 392 KB — the restart's allocator release was a side benefit, not the goal)
safety: kill switch OFF · DRY_RUN=true · live_money=false · creds present in env (booleans served) ·
manual endpoint fail-closed 409 without configured unit · game-state gate BLOCKED/GLOBAL_KILL_SWITCH ·
ledger untouched by smoke probes (0 executions, 0 audit rows)
chain_smoke (§7): frontend → auth layer → betting API → validation gate → DRY_RUN execution →
response → audit → frontend status: ALL VERIFIED without a real-money bet (dry-run path per §7)
remaining: (1) live transport is an INTENTIONAL stub — live money requires code, operator-gated;
(2) production betting limits + unit price NOT CONFIGURED (fail-closed: betting blocked until armed);
(3) 19 pre-existing non-betting test failures (unchanged); (4) tree uncommitted (40+ modified files);
(5) §1–§7 execution subsystem (engine/adapters/machine/precheck/replay) tested but NOT yet wired into
the server runtime — additive only, integration is a separate authorized step.

## 2026-09-28 — AUTO-BET EXECUTION SUBSYSTEM (test-first directive §1–§7) — IMPLEMENTED, VERIFICATION GREEN

result: IMPLEMENTED — the §1–§7 subsystem is code-complete with a 63-test directive suite; 0 new failures introduced
footprint: 8 NEW files only — zero tracked files modified (verified: git status)
targeted: 280 passed across 8 betting/auto-bet suites (incl. the new 63)
full_suite: 1759 passed / 19 failed (579s) — all 19 PRE-EXISTING (see classification)

### WHAT WAS BUILT (all in `blm_v4/betting/`, additive — nothing existing was modified)

- `machine.py` — §3 state machine: explicit matrix (SIGNAL→ELIGIBLE→PRECHECK→SUBMITTING→ACCEPTED→CONFIRMED→SETTLED + every directive failure edge), `validate_transition` raises `InvalidTransition` on anything else, terminal detection, reachability. No SIGNAL→CONFIRMED edge exists.
- `session.py` — §2 backend session layer: CONNECTED/DISCONNECTED/SESSION_EXPIRED/AUTHENTICATION_ERROR, login/logout/expire/touch/reconnect, `assert_submittable()` fail-closed gate, credential-free publications (`status`/`snapshot`/`resume`), `redact()` scrub. Credentials held in memory only, never logged/served/persisted.
- `adapters.py` — §1+§6 adapter boundary: `BookmakerAdapter` protocol; `TestBookmakerAdapter` deterministic simulator with all 13 directive scenarios + `queue_response` scripting + adapter-level duplicate protection; `LiveBookmakerAdapter` fail-safe stub (construct-only with explicit opt-in; every call raises `LIVE_NOT_IMPLEMENTED`); `adapter_from_environment()` — TEST adapter is the DEFAULT unless BLM_ADAPTER_MODE=production AND BLM_ALLOW_LIVE_ADAPTER=1 together.
- `precheck.py` — §4 pre-bet gate: 16 ordered checks (event/market/selection existence, current line/odds, line+odds tolerance, event/market status, session, balance, stake+exposure limits, duplicate protection, game-level limits, global switch), exact machine rejection reasons (STAKE_LIMIT_EXCEEDED, LINE_TOLERANCE_EXCEEDED, ...), fail-closed on unverifiable limits.
- `engine.py` — BetEngine: matrix-validated pipeline; §5 idempotency via `ClaimStore` (UNIQUE-key SQLite claim, atomic, survives restarts) claimed BEFORE any state move; UNKNOWN→RECONCILING→CONFIRMED/REJECTED via adapter truth lookup (never blind retry); duplicates learn the ORIGINAL's disposition (`duplicate_of_original`), never a second order.
- `replay.py` — §7 replay engine: historical signals through the SAME engine hard-wired to the TEST adapter (type-enforced — a live adapter raises TypeError); per-signal scenario scripting; deterministic; summary counts claims_won/duplicates/outcomes separately.
- `tests/test_autobet_execution_subsystem.py` — 63 tests across §1/§2/§3/§4/§5/§6/§7 incl. 8-thread concurrent single-submission, worker-restart dedup, timeout-reconcile, every precheck reason.

### TEST EVIDENCE

- Baseline (pre-change): 186 passed across 5 betting suites — green before work started.
- New directive suite: 63/63 PASS (`tests/test_autobet_execution_subsystem.py`).
- Targeted (8 suites): 280 passed, 0 failed (`test_autobet_execution_subsystem`, `test_autobet_pre_runtime`, `test_betting_execution`, `test_test_stack`, `test_betting_manual_contract`, `test_parlay_execution`, `test_pokerbet_live_adapter`, `test_execution_game_binding`).
- Full suite: 1759 passed / 19 failed / 1 deselected (2 slow latency-guard tests excluded) in 579s.
- `git diff --check`: PASS. `py_compile` on all 6 new modules: PASS.

### FULL-SUITE FAILURE CLASSIFICATION (19 failed — NONE touch the betting layer)

Verified pre-existing before attribution: grep for betting-layer imports in all 13 failing test files = 0 hits; the dashboard.js `blm-signal-row` string predates this session (present at HEAD; this session modified 0 tracked files).
- Frontend vocabulary/dirty-tree (B4 pattern): test_dashboard_audio_alert, test_z_frontend_migration, test_z_historical_alert_frontend, test_m009_m5_frontend_integrity, test_under_alert_lifecycle (2), test_settle_worker — dashboard.js carries uncommitted prior-session edits these tests straddle.
- Freeze/temporal (known pre-existing): test_forensic_relative_pace_freeze (3), test_prospective_freeze, test_deviation, test_deviation_analysis — matches the standing 4-failure baseline class.
- Latency-guard class (pre-existing; 2 slow siblings deselected): test_live_route_latency_guard (4), test_status_liveness, test_live_market_gate — PaceRef plumbing sensitive to dirty-tree api.py.
- Unknown-vs-baseline delta: 19 now vs 46-55 in prior sessions' full runs — CLEANER than baseline; no regression from this work.

### NO-LIVE-MONEY CERTIFICATION (directive §0)

The system runs fully in TEST/PAPER mode: TestAdapter is the default adapter in every non-production environment; LiveBookmakerAdapter cannot submit (stub raises on every call); replay is type-enforced onto the test adapter; the pre-existing DRY_RUN/kill-switch/idempotency layer is untouched and its suites stay green. No live execution path exists until code is written — configuration alone cannot enable it.

### OPERATOR NOTES

- This session is ON TOP of the prior session's uncommitted sync-worktree state (STATUS.md + docs/milestones snapshot + DECISIONS.md 165 lines + dashboard.js NO FINAL fix all remain UNCOMMITTED — deliberately not staged; multi-agent protection per AGENTS.md §19).
- Pre-change STATUS.md preserved at `docs/milestones/STATUS_2026-09-27_pre_autobet_subsystem.md` (STATUS.md is untracked — no git history to fall back on).
- Commit NOT performed: the working tree mixes multiple agents' uncommitted work; staging only my 8 files is safe, but per §19 I did not commit without the operator confirming scope. Ready to commit `blm_v4/betting/{machine,session,adapters,precheck,engine,replay}.py` + `tests/test_autobet_execution_subsystem.py` on request.
- Push: NOT attempted (operator-gated; branch diverged 6/7).

### NEXT ACTIONS

1. Operator: authorize commit of the 8 new files (scope: execution subsystem only).
2. Wire the new engine behind the TEST stack as an integration surface (§ "TEST ENVIRONMENT becomes the integration environment") — engine not yet exposed via API endpoints; deliberate (no scope creep).
3. §7 historical-signal export from production DBs (read-only) to feed ReplayEngine in bulk.
4. Settlement engine (CONFIRMED→SETTLED path is machine-legal but has no settlement worker in the new subsystem yet — existing settle_worker unaffected).

## 2026-09-27 — Phase 5 + C2 removal merge + PaceReferenceWorker restoration + Resulted Alerts contract resolution

sync: completed (worktree `/tmp/blm-sync-worktree-20260927-095359`, commit 60c88cb)
pace_ref_restore: committed (ffd7ffc)
resulted_contract: resolved (NO FINAL)
full_suite: run (55 failed / 1524 passed / 3 skipped / 177 warnings, 304s); all 55 classified into 4 buckets — see below
resulted_alerts_suites: PASS (44/44 across color_coverage, unprovable_states, panel_filters, settle_worker)

## WHAT HAPPENED

Sync merge (60c88cb) + PaceReferenceWorker restoration (ffd7ffc) + Resulted Alerts contract fix (unstaged), all in isolated worktree `/tmp/blm-sync-worktree-20260927-095359`.

## SYNC MERGE

Orthogonal to the Phase 5 / C2 removal work: the `handoff-2026-09-07` branch was 7 commits behind `origin/handoff-2026-09-07`, the missing 7 being a clean upstream sweep (`0209585` "Remove C2 inputs from fingerprint evaluation" and 6 ancestors) that removed C2 (and C4/C6) from fingerprint evaluation and dashboard display. A controlled merge in an isolated worktree brought the branch current and removed C2 while preserving the 41 local files not present upstream.

Merge commit: 60c88cb, HEAD of `/tmp/blm-sync-worktree-20260927-095359`.

## PACEREFERENCEWORKER RESTORATION (COMMITTED: ffd7ffc)

The ort merge stripped the PaceReferenceWorker API plumbing that was added in the dirty tree for the empty-live-data diagnostic (2026-09-25). Three `test_live_route_latency_guard` tests regressed to collection errors until the plumbing was restored from the pre-sync dirty `api.py`. Commit ffd7ffc restores it.

Restored:
- `_PACE_REF_WORKER`, `_PACE_REF_LATEST`, `_PACE_REF_LOCK` (module-level state)
- `_publish_pace_references()`, `configure_pace_reference_worker()`, `pace_reference_status()`
- Worker-aware behavior in `_pace_reference()` and `_q3_pace_reference()`
- Inline fallback when no worker is wired

Preserved:
- C2 removal throughout (fingerprint set remains `C1, C3, C5, R2`; no `recent_pace_3m`, no `actual_pts_per_min`, no active C2/C4/C6 evaluation)
- Phase 5 collector timeout budgets (no change to `blm_v4/collector.py`)
- Resulted Alerts collapsible section (no change to dashboard.js expand/collapse)
- All 41 local files not upstream (execution files, scripts, docs, analysis, AGENTS.md, Status.md)

Commit: ffd7ffc, on detached HEAD at `/tmp/blm-sync-worktree-20260927-095359`.

## RESULTED ALERTS CONTRACT RESOLUTION (UNCOMMITTED: dashboard.js)

The implementation displayed the word "RESULT PENDING" for the resolved+null state (record resolved, backend proves neither final nor line), but that semantic is misleading — the record is already resolved, so "PENDING" (may still settle) is the wrong word. The contract is "NO FINAL": the docstring (test_resulted_alerts_unprovable_states.py:12), the CSS class (`.al-nofinal`), the `kind: "nofinal"` field, and the pre-sync dirty-tree test assertions (9 locations across 3 suites + settle_worker) all say "NO FINAL". The dirty tree had patched 3 test files to match the wrong implementation word; the merge reverted both the tests' patches and the implementation, re-exposing the mismatch.

Resolved by changing the displayed word from "RESULT PENDING" → "NO FINAL" in 3 functional locations in `blm_v4/dashboard/static/dashboard.js`:
- `ALERT_RESULT_WORDS.no_final` value (line 446)
- `onUnprovable()` diagnostic call (line 496)
- `alertVerdictStateFor()` return value (line 497)

Preserved:
- `ALERT_RESULT_WORDS.unknown` value stays "RESULT PENDING" (unknown ≠ nofinal — different concept)
- 4 documentary references to "RESULT PENDING" kept as historical context (lines 404, 468, 524, 1498)
- No other files touched: PaceRef plumbing, C2 removal, collector, execution files, Resulted Alerts test files all unchanged

## UNCOMMITTED CHANGES

`blm_v4/dashboard/static/dashboard.js` — Resulted Alerts contract fix (3 functional locations, NO FINAL word). Tracked, unstaged.

## TEST RESULTS

### PaceReferenceWorker restoration
Pre-commit: 3/3 PASS (test_wrappers_serve_published_payload_without_inline_scan, test_warmup_serves_empty_references_fail_closed, test_configure_publishes_and_reports).
Post-commit: 3/3 PASS — regressions fixed.

### Resulted Alerts contract (before fix)
- test_resulted_alerts_color_coverage.py: 8/9 PASS, 1 FAIL
- test_resulted_alerts_unprovable_states.py: 4/9 PASS, 5 FAIL
- test_resulted_panel_filters.py: 9/11 PASS, 2 FAIL
- test_settle_worker.py: 14/15 PASS, 1 FAIL  (test_no_final_remains_no_final_and_never_coloured)

### Resulted Alerts contract (after fix)
All 4 suites 100% PASS: 9 + 9 + 11 + 15 = 44/44 PASS.
Accounting: 9 contract failures → 0. UNKNOWN = 0.

### Full suite (after sync + PaceRef commit, before contract fix)
55 failed / 1524 passed / 3 skipped / 177 warnings, 304s.
4-bucket classification:
  B1 MERGE_REGRESSION: 0 (3 PaceRef regressions fixed, 0 unresolved)
  B2 PRE_EXISTING: 21 (same tests failed baseline and merged)
  B3 UPSTREAM_CHANGE: 0
  B4 RESTORED_DIRTY_WORK: 34 (25 module-dependency + 9 contract-held)
  UNKNOWN: 0

After contract fix: the 9 contract-held failures are resolved (44 Resulted Alerts tests now pass). The remaining 21 pre-existing and 25 non-contract dirty-tree failures are unchanged by the contract fix.

## SAFETY ARTIFACTS (DO NOT DELETE)

Pre-sync:
- `/tmp/blm-working-tree-before-sync.patch` (404 KB) — pre-sync tracked diff
- `/tmp/blm-untracked-before-sync-20260927-091307.tar.gz` (795 KB) — pre-sync untracked archive
- `/tmp/blm-working-tree-before-sync.binary.patch` (404 KB) — binary-equivalent snapshot
- `/tmp/blm-baseline-worktree-111358` — backup branch worktree (backup/handoff-2026-09-07-pre-sync-20260927-091318)

Post-Phase5 isolation:
- `/tmp/blm-pre-sync-after-phase5.patch` (295 KB) — post-Phase5 tracked diff
- `/tmp/blm-untracked-after-phase5.tar.gz` (795 KB) — post-Phase5 untracked archive
- `/tmp/blm-untracked-after-phase5.txt` (1.8 KB) — post-Phase5 untracked list

Sync worktree (active):
- `/tmp/blm-sync-worktree-20260927-095359` — isolated merge worktree (HEAD 60c88cb + ffd7ffc + uncommitted dashboard.js contract fix)

## VERIFICATION CHECKS

- node --check blm_v4/dashboard/static/dashboard.js: PASS
- git diff --check: PASS
- py_compile blm_v4/api.py: PASS
- 3 PaceRef regression tests: 3/3 PASS
- 4 Resulted Alerts suites: 44/44 PASS
- C2 in fingerprint eval: 0 (C1, C3, C5, R2 only)
- Only 1 tracked file modified (dashboard.js contract fix)

## VERIFICATION GATES

- W-1 (test_wrappers_serve_published_payload_without_inline_scan): PASS
- W-2 (test_warmup_serves_empty_references_fail_closed): PASS
- CONF-1 (test_configure_publishes_and_reports): PASS
- UNKNOWN classification bucket: 0

## CURRENT STATE

- Working tree: isolated `/tmp/blm-sync-worktree-20260927-095359`
- Branch: detached HEAD at ffd7ffc (after PaceRef commit) + uncommitted dashboard.js
- Committed: 60c88cb (sync merge), ffd7ffc (PaceRef restoration)
- Uncommitted: blm_v4/dashboard/static/dashboard.js (contract fix)
- Original `/home/ubuntu/BLM`: untouched, at 0561ea2, handoff-2026-09-07 branch
- Services: STOPPED — blm-server inactive, blm-collector inactive, 0 BLM processes
- Push: NOT performed
- Deploy: NOT done

## REMAINING ISSUES

- Uncommitted dashboard.js contract fix — decision needed whether to commit
- 21 PRE-EXISTING failures (B2) not addressed by this work
- 25 non-contract dirty-tree failures (B4) require dirty tracked modules not in merge
- Service restart authorization pending (operator-gated)
- Push authorization pending (operator-gated)

## NEXT ACTIONS

1. Review the dashboard.js contract fix (3 functional changes, NO FINAL word)
2. Decide whether to commit the contract fix
3. If commit: run full suite, re-verify UNKNOWN=0, then consider push
4. Service restart and push are operator-gated; do not proceed without explicit authorization

## TESTS RUN

- Phase 5 focused: 18/18 PASS (timeout budget + collector staleness + color coverage)
- PaceRef restoration: 3/3 PASS pre-commit, 3/3 PASS post-commit
- Resulted Alerts suites: 44/44 PASS (after contract fix)
- Full suite: 1524 passed, 55 failed, 3 skipped, 177 warnings, 304s

## TEST RESULTS

All verification gates green:
- W-1, W-2, CONF-1: PASS
- Resulted Alerts: 44/44 PASS (9 + 9 + 11 + 15)
- UNKNOWN: 0
- C2: removed from fingerprint eval

No new failures introduced.

## KNOWN FAILURES

See full-suite classification above:
- B2 PRE_EXISTING: 21 failures (pre-existing, not introduced by this work)
- B4 non-contract: 25 failures (dirty-tree module dependency)
- Total remaining failures after contract fix: 46 (was 55, 9 contract failures resolved)

## KNOWN RISKS

- Dirty production tree in `/home/ubuntu/BLM` (untouched): 40+ modified tracked + 47+ untracked files. Any restart after push puts ALL of it live.
- Diverged branch: integration is an operator decision.
- Box stability: 3 unexplained hard resets (2026-09-25); memory pressure is a standing risk.
- Reconcile backlog: ~7,100 unresolved games; worker throttle is the bottleneck.
- Service restart and push are operator-gated; do not proceed without explicit authorization.

## RELEVANT FILES

- `/tmp/blm-sync-worktree-20260927-095359/blm_v4/api.py` — committed PaceRef restoration (ffd7ffc)
- `/tmp/blm-sync-worktree-20260927-095359/blm_v4/dashboard/static/dashboard.js` — uncommitted contract fix
- `/tmp/blm-sync-worktree-20260927-095359/blm_v4/collector.py` — Phase 5 timeout budgets (intact)
- `/tmp/blm-sync-worktree-20260927-095359/blm_v4/execution/` — 4 preserved local files
- `/tmp/blm-final-classification-report.md` — full 4-bucket classification
- `/home/ubuntu/BLM/` — original dirty tree, untouched
- Safety artifacts in `/tmp/` — see list above

## Git State

Branch: `handoff-2026-09-07` (original `/home/ubuntu/BLM`); detached HEAD (sync worktree)
Commits: 0561ea2 (Phase 5, original HEAD), 0209585 (upstream HEAD), 60c88cb (sync merge), ffd7ffc (PaceRef restore)
Remote: `git@github.com:abwarren/BLM.git` (origin, SSH)
Push status: NOT ATTEMPTED — sync worktree is isolated; no remote operations performed
Working tree: `/tmp/blm-sync-worktree-20260927-095359` — 1 tracked modified (dashboard.js), 47 untracked
Next agent should start from: sync worktree `/tmp/blm-sync-worktree-20260927-095359`; read STATUS.md in that worktree for continuation point
  call paths; no unrelated fixes were made.
- Latest focused verification: performance, clean-metrics, pace-projector,
  deviation, and storage-index suites passed (75 passed in 15.71s), including
  the new individual COUNT-query timing assertion. `py_compile` passed and
  `git diff --check` passed.
- Live services remain active and unchanged (`blm-server` MainPID 1602,
  `blm-collector` MainPID 3406); no reload/restart was performed. Instrumented
  production timing remains unverified pending an authorized reload and
  end-to-end measurement.
- `git diff --check` passed. No commit or push. Services remain untouched.
- Production top-ten operation timings, SQL call rates, and
  observations/projections per accepted snapshot are not yet populated from
  the new instrumentation; collect them after an authorized process reload.

Next action: review instrumentation diff, then schedule an authorized
collector reload/measurement window. Do not optimize queries or alter
database settings before that measurement.

> Canonical living-state file (per `AGENTS.md` §2.8). Update before stopping.
> Last updated: 2026-09-26 — performance instrumentation added; live process not reloaded.
>
> 2026-09-27 — Phase 5 timeout budget + Resulted Alerts UI:
> - Added centralized timeout/budget constants to PokerBetCollector:
>   PAGE_CONTENT_TICK_TIMEOUT_S=3.0, PAGE_CONTENT_RETRY_TIMEOUT_S=5.0,
>   RECOVERY_TIMEOUT_S=5.0, PAGE_CAPTURE_BUDGET_S=12.0,
>   TICK_TARGET_S=10.0, TICK_HARD_CEILING_S=30.0.
> - _page_content_timed accepts an explicit timeout_s override; tick body
>   tracks page-capture budget remaining and forces fresh context on exhaust.
> - Resulted Alerts panel is now always rendered inside a <details> element,
>   collapsed by default, with a persistent expand/collapse toggle button in
>   the header (localStorage key pz.resultedAlertsCollapsed).
> - Added tests/test_collector_timeout_budget.py (5 tests, all passing).
> - Added docs/perf/collector-timeout-budget.md.
> - Verified: 18 tests pass (timeout budget + staleness + resulted alerts);
>   JS syntax OK; collector.py compiles. No existing tests regressed.

Agent: performance instrumentation step 1
Branch: `handoff-2026-09-07` — HEAD `153415f` "Quantify the unresolved backlog against the authoritative feed"
Branch is DIVERGED from origin: 4 ahead / 7 behind. Do not pull/push/rebase without explicit instruction.

## CURRENT STATE

Quarterly line/checkpoint identity work is implemented and targeted-tested,
but not ready for live collection until the direct PokerBet event-page route
and rendered market tabs are confirmed in a controlled browser session.
No collector was run and no production data was written by this task.

- Production: `blm-server.service` + `blm-collector.service` running from
  the WORKING TREE (not HEAD). Restarts require explicit authorization.
- Storage: SQLite (blm_pokerbet.db, blm_metrics_clean.db, blm_historical.db,
  blm_ts.db, blm_v2.db, blm_betting.db) — no PostgreSQL in production.
- Deployment/runtime: user-level systemd services; journal shows reconcile
  worker cycling but throttle-capped; last full-suite run 2026-09-25.

## COMPLETED WORK

- Quarter collection now uses the canonical numeric event ID embedded in
  each game's stored PokerBet `/event-view/.../{event_id}/...` URL. It opens
  that exact URL, checks the final page URL, and refuses rows unless the
  captured page URL and exact `source_game_id` agree. Suffixed BLM aliases
  such as `<event_id>#i1` are skipped because they cannot be distinguished
  as separate provider events.
- DOM market storage validates that `game_id` and `source_game_id` resolve
  to the same PokerBet game row before insertion.
- Quarterly audit now reports absent schema columns distinctly, includes
  the DOM-observation table, and labels checkpoint basis as whole-game
  progress percentage.
- Checkpoint semantics remain game-progress checkpoints; pct75 remains in
  the ladder. Added tests for event URL identity, repeated team names,
  missing/mismatched IDs, game-row binding, quarter identity, and 50/75
  checkpoint identity/progress.
- Read-only production audit: `checkpoint_market` 156,858 rows / 14,547
  games; pct50=14,334, pct75=14,039, both=13,924; duplicate checkpoint rows
  and game/result orphans=0; DOM game/source ID mismatch=0. DOM observation
  table is empty. 37,721 checkpoint
  rows have NULL outcomes; none are missing final totals. Quarter feed table
  row counts changed while the live collector continued operating.
- Agent continuity chain installed: `AGENTS.md` (directive + addenda),
  `STATUS.md` (this file), `docs/AGENT_CONTEXT.md` (stable architecture).
- `DECISIONS.md` pre-exists (1,742 lines, durable decisions, authority chain).
- Verified repo state for the seed below (git status, branch, log, docs).

## ACTIVE WORK

Controlled browser verification of one canonical event URL remains. Python
`urllib` received HTTP 403 from PokerBet in this environment; no claim is made
that Playwright page access, selected-period controls, or visible markets work.
Do not run collection against the production database without explicit
authorization; use an isolated test database for a live probe.

## PERFORMANCE ROADMAP STATE

**Phase 1** (Instrumentation): DONE — `blm_v4/performance.py` in working tree,
wired into collector.py, tests in `test_performance_metrics.py`. Not yet deployed
to running collector (PID 3406).

**Phase 2** (Baseline): DONE — `docs/perf/BASELINE_2026-09-26.md` captured
2026-09-26T22:30Z. Key: tick p50=21,313ms (2.1x target), 99.8% overruns,
persistence p50=5,575ms, slow_event_view p50=12,542ms.

**Phase 3 P1** (Observation Cache): DONE — observations_loaded/accepted_snapshot
51.2 → 0.0.

**Phase 3 P2** (Deviation Dirty Gating): DONE — commit 9d0582f, 15 tests passing,
persistence p50 6,449ms → 571ms (-91%), deviation.refresh_game live calls 22,421
→ ~82 (-99.6%). Benchmark: docs/perf/PHASE3_P2_BENCHMARK_2026-09-27.md.

**Phase 4** (Fast/Slow Worker Split): VERIFIED — already implemented in code.
Dedicated daemon thread (`blm-slow-worker`) with own browser, async queue,
no fast-path blocking. Confirmed by code audit.

**Phase 5a** (betual_line_count_distinct): DONE — implemented in storage.py (incremental counter + trigger). Tick 160: 28,238 ms/tick p50 (still dominates — first call pays full COUNT DISTINCT; subsequent calls O(1); counter needs priming at startup).

**Phase 5b** (get_game cache): DONE — implemented in collector.py (_game_id_cache).

**Phase 5c** (Background deviation backfill): DONE — implemented in collector.py (blm-deviation-backfill daemon thread).

**Phase 5d** (blm.db snapshots index): NOT APPLICABLE — all production queries use ORDER BY captured_at ASC. The UNIQUE(game_id, captured_at) autoindex + idx_snapshots_source_ts cover all query patterns. The schema comment referencing idx_snapshots_game_created (game_id, created_at) was erroneous (wrong column, wrong database). No index needed. See docs/perf/FINAL_REPORT.md §7.

**Phase 5e** (checkpoint_market source index): NOT APPLICABLE — idx_cm_game_pct(source_game_id, checkpoint_pct) covers production query (api.py:857-859, ORDER BY checkpoint_pct ASC). No DESC LIMIT 1 query exists in production code. No index needed. See docs/perf/FINAL_REPORT.md §7.

**Phase 6** (Persistence optimization): NOT APPLICABLE — connection overhead measured at 0.333ms p50, per-tick=4.66ms, tick p50=45,731ms → 0.010% of tick. Not worth connection reuse. No PRAGMA/WAL/VACUUM changes warranted. See docs/perf/FINAL_REPORT.md §1.

**Current benchmark (tick 160, 163 completed ticks):**
- tick p50: 45,731 ms | p95: 62,631 ms | max: 79,676 ms
- observations_per_snapshot: 0.0 (was 51.2)
- projections_per_snapshot: 0.113 (was 51.2)
- residuals_per_snapshot: 0.006 (was 0.64)
- persistence p50: 26.6 ms
- physical read rate: 70.3 MB/s (60s I/O delta, cold cache)
- Top bottleneck: betual_line_count_distinct 28,238 ms/tick (61% of tick)

**Phase 2 vs current:**
- tick p50: 31,971 → 45,731 ms (+43%) — includes Phase 5a first-call COUNT DISTINCT
- obs/snapshot: 51.2 → 0.0 (-100%)
- proj/snapshot: 51.2 → 0.113 (-99.8%)
- res/snapshot: 0.64 → 0.006 (-99.0%)
- persistence: 571 ms → 26.6 ms (different contexts — not directly comparable)
- physical read rate: 241 B/s → 70.3 MB/s (warm vs cold cache — not comparable)

Remaining bottleneck: betual_line_count_distinct 28,238 ms/tick (61% of tick) — Phase 5a counter needs priming at startup.

## NEXT ACTIONS

1. Use an isolated DB and one canonical event to confirm Playwright reaches
   the stored event URL, its final URL retains the same event ID, and Q1–Q4
   period selection yields visible market DOM.
2. If the page exposes no canonical route ID or market DOM, keep the collector
   fail-closed and investigate provider-owned event payloads/links.
3. After operator authorization, plan a controlled collection run; do not use
   the production DB until the browser probe and identity results are reviewed.

## TESTS RUN

Baseline before changes: requested four-file suite — 28 passed, 1 warning.
Final after changes: requested four-file suite — 36 passed, 1 warning in
24.85s. `git diff --check` passed. Python compilation passed for the collector,
quarterly audit, and storage module. Production audit used the script's
read-only connections; no collector run.

## TEST RESULTS

One intermediate run had a fixture failure because that fixture lacked the
newly required canonical event URL; the fixture was corrected. Final suite
has no failures. Starlette/httpx deprecation warning remains.

## KNOWN FAILURES

Full-suite baseline (2026-09-25, from docs/milestones/CURRENT.md):
4 failed / 1534 passed — 3 × `test_forensic_relative_pace_freeze`,
1 × `test_prospective_freeze`. PRE-EXISTING. Re-confirm before attributing
failures to new work.

## KNOWN RISKS

- **Dirty production tree:** 38 modified tracked files + untracked work
  (incl. `blm_v4/execution/pokerbet/`, `blm_v4/live_analytics/
  fingerprint_stats.py`, `pace_reference_worker.py`, 6 new test files,
  12+ scripts). Any service restart puts ALL of it live.
- **Diverged branch:** 4 ahead / 7 behind origin/handoff-2026-09-07.
  History rewrite forbidden; integration is an operator decision.
- **Box stability:** 3 unexplained hard resets in one hour (2026-09-25)
  during peak memory pressure; cause UNDIAGNOSED. Memory pressure is a
  standing risk; restarts are not a remedy.
- **Reconcile backlog:** ~7,100 unresolved games; worker throttle
  (BATCH=25 / INTERVAL=300s at server.py:58-62) is the bottleneck. Raising
  it requires a blm-server restart (operator-gated).
- **Checkpoint semantics decision:** checkpoints built as GAME progress
  (deciles + pct75); if the directive's "50%/75% of the quarter" meant
  WITHIN-QUARTER, §3/§5 need a per-quarter rebuild (operator-gated).
- **PokerBet page verification:** PokerBet returned HTTP 403 to a read-only
  urllib request. Direct canonical event-page navigation and live DOM capture
  are not integration-verified; collector must remain unrun until a controlled
  browser probe confirms the event route and period tabs.
- **Event aliases in production:** of 24,424 ended basketball rows with
  `source_url`, 22,263 `source_game_id` values exactly match the event ID in
  the URL; 2,161 suffix aliases share 969 event-URL groups and are skipped.
  The stored URL is the provider identity authority; names are not used.

## RELEVANT FILES

- `AGENTS.md` — directive + repo addenda (rules).
- `DECISIONS.md` — durable decisions + authority chain (why).
- `docs/AGENT_CONTEXT.md` — stable architecture (what).
- `docs/milestones/CURRENT.md` — long-form session log (evidence trail).
- `docs/AGENT_DIRECTIVES_SLICES_1-4.md` — standing no-improvisation rule.
- `server.py:58-62` — reconcile throttle envs.
- `blm_v4/execution/` — execution layer incl. untracked pokerbet/.
- Working tree: 38 modified + untracked as listed in git status.

## Git State

Branch: `handoff-2026-09-07`
Commit: `153415f` — "Quantify the unresolved backlog against the authoritative feed"
Remote: `git@github.com:abwarren/BLM.git` (origin, SSH)
Push status: NOT ATTEMPTED this session — see note below
Working tree: broad pre-existing dirty tree plus this task's edits; see
`git status --short`. No files staged, committed, or pushed.
Uncommitted files: includes this session's continuity files (AGENTS.md,
STATUS.md, docs/AGENT_HANDOFF.md, docs/AGENT_CONTEXT.md) atop the
pre-existing dirty production tree
Next agent should start from: `153415f` on `handoff-2026-09-07`; read
AGENTS.md → STATUS.md → DECISIONS.md first

Push note (per AGENTS.md §19): the branch is DIVERGED from origin
(4 ahead / 7 behind), so a push would be rejected until the divergence is
integrated. Integration means a merge/rebase over a tree carrying 38 files
of uncommitted production work — not safe to improvise, and commit/push of
the continuity files was deliberately deferred until the operator confirms
(the directive itself is still arriving piecemeal: §5–§18 pending).
First safe window: after the operator authorizes, stage ONLY the
continuity files, commit, integrate the divergence, re-run relevant tests,
push normally. Never force-push.

## DATABASE/SCHEMA CHANGES

No production database writes or schema changes by this task. Audit was
read-only. The live production services continued appending quarter-market
observations during the audit, so table counts varied between passes.

## UNFINISHED WORK

- Controlled browser verification of the quarter collector remains unfinished.
- PHASE 0..6 ladder (AGENTS.md §4) — not started; no phase after 6 exists
  in the installed text (truncation noted there).
- Operator-gated: reconcile-throttle change, restart authorization,
  checkpoint-semantics ruling, branch integration.

## CONTEXT FOR NEXT AGENT

- Docs that matter more than they look: `docs/milestones/CURRENT.md`
  carries a self-correcting EuroLeague header — read the CORRECTION at the
  top before trusting any cohort claim.
- `docs/AGENT_HANDOFF.md` is now a pointer to this file; do not maintain
  both. `docs/AGENT_CONTEXT.md` is stable architecture, not state.
- Betting identity is `source + source_game_id`, never display text.
- Update THIS file before stopping; leave the tree coherent.

---

## 2026-09-28 (regression-agent) — PARALLEL REGRESSION / PRODUCTION-SAFETY AUDIT (read-only)

role: second, independent agent (regression / performance / production-safety gatekeeper).
ownership: DID NOT modify dashboard.js / styles.css / index.html or any frontend test. NO backend code changed.
result: NO reproducible backend/API regression. The reported latency numbers do not reproduce on the stated fixture.
deployed: NO. production restart: NO. all DB access read-only (mode=ro); blm_v4/api.py has 0 INSERT/UPDATE/DELETE.

### BASELINE
- HEAD afcc27d3d1c1b27c1d53fd8c6f5f4f712102a8b1 ("Add bounded PERFORMANCE instrumentation…"), branch handoff-2026-09-07 (diverged 7/7 from origin).
- Working tree DIRTY: 45 tracked modified + 75 untracked (incl. nested worktree `blm-dev/` = branch dev/local-engineering-2026-09-27 @0561ea2).
- OTHER AGENT owns the 3 frontend files (+ untracked frontend assets login.*/explorer.js/stats.js). All uncommitted work left untouched. No git reset/clean/checkout/stash/rebase/force-push performed. Backend comparison used an isolated worktree `/tmp/blm-ra/head` @HEAD.

### CLAIMED REGRESSION — NOT REPRODUCED
Claim: /api/v4/live ≈ 6.7s, /api/v4/status ≈ 51.4s on a 2,000-game / 200,000-snapshot fixture.
Measured (same fixture, via TestClient): working tree /live 0.66s, /status 0.03s; HEAD /live 0.68s, /status 0.14s.
So the numbers do NOT reproduce on the stated fixture for EITHER tree. They correspond to the PRE-FIX PRODUCTION incident
(real DB: 13.4 GB, 2,056,695 snapshots, 3,504,282 market_observations) — already remediated in the working tree.

### PRODUCTION MEASUREMENTS (read-only, PID 301286 :2262)
- /api/v4/live : 5.37s first, 2.0–3.8s steady. Dominated by `_historical_context_for` (historical benchmark scans, 120s
  in-process cache) + per-game snapshot tail + under-alert evaluation. IDENTICAL at HEAD (7.05s cold / 1.86s warm,
  refs stubbed) ⇒ PRE-EXISTING, not a regression.
- /api/v4/status : 0.030–0.051s (x5). HEAD code on the same DB: 3.8s warm / ~16s+ cold (`_freshest_game_state` WS probe).
  Post-fix `_db_stats` = MAX(rowid) estimate + per-class index seek; `_freshest_game_state` = rowid-desc first-match walk.

### EXPLAIN QUERY PLAN / INDEX MISMATCH
- Production snapshots indexes: idx_snapshots_class_captured(classification,captured_at), idx_snapshots_game_ts,
  idx_snapshots_source_ts, UNIQUE(game_id,captured_at). All /status probes = index seeks (≤12ms) on 2.05M rows.
- Guard fixture LACKS idx_snapshots_class_captured → its per-class last-snapshot is a SCAN (26ms @200k, 235ms @2M);
  production has the index (0.0ms). Fixture is CONSERVATIVE, does not mask a regression.

### GUARD COVERAGE GAP (finding, not a regression)
The latency guard's `/status` branch cannot detect the incident it documents: pre-fix (HEAD) /status on the 200k fixture
= 0.144s vs the 5s threshold, and the fixture's `market_observations` table is EMPTY, so the WS freshness probe (the ~16s
cold cost on production) is never exercised. A re-introduction of the whole-table scans would pass the guard unless the
fixture were raised to production scale (≥2M rows) with a populated market_observations table.

### FULL-SUITE RESULT
14 failed / 1884 passed / 1 skipped (691s, `--ignore=blm-dev`).
- PRE-EXISTING (13) — fail identically at HEAD with the same test files: frontend-vocabulary/rendering 6
  (test_dashboard_audio_alert, test_m009_m5_frontend_integrity, test_z_frontend_migration,
  test_z_historical_alert_frontend, test_under_alert_lifecycle[2]); freeze/temporal 6 (test_deviation,
  test_deviation_analysis, test_forensic_relative_pace_freeze[3], test_prospective_freeze); liveness 1 (test_status_liveness).
- NEW vs HEAD (1) — FRONTEND-OWNED: test_settle_worker::test_valid_verdicts_reach_the_frontend_with_the_correct_colour
  PASSES @HEAD, FAILS only with the working-tree dashboard.js (new `historyAlertsHTML()` merged re-sort, dashboard.js:1794-1807).
  Reported only — NOT edited (ownership boundary).
- test_live_route_latency_guard: 11/11 GREEN (vs 4 failures + 3 collection errors in the documented prior baseline).

### BETTING / ALERT SAFETY
UNCHANGED by this agent. Working-tree diffs to blm_v4/betting/* + blm_v4/execution/* only TIGHTEN: stake capped at 1 unit
(`max_stake_units: 1.0`), idempotency_key required, kill switch server-side, UNKNOWN→RECONCILING. No change to
under_alert.py / under_outcome.py eligibility, market-freshness, live-state or stale-state gates. scorecard.py adds pct75 to
FIXED_CHECKPOINT_PCTS and bumps SCORECARD_LOGIC_REVISION 1→2 (deliberate, quarterly-collection directive; tested by
test_m009_checkpoint_market, green). Betting/Auto-Bet/Auth suites: 326 passed, 0 failed.

### ENVIRONMENT
`pytest` aborts collection with ImportPathMismatchError because the nested worktree `blm-dev/tests/conftest.py` collides with
`tests/conftest.py`. Run the full suite with `--ignore=blm-dev`.

### NEXT ACTIONS
1. Operator: decide whether to (a) accept the guard as-is, or (b) raise its scale to production-shape (≥2M rows + populated
   market_observations) so the /status branch is a real regression detector.
2. Frontend agent: reconcile `historyAlertsHTML()` row ordering with test_settle_worker's expected colour order.
3. No backend fix required — the two latency defects described in the guard docstring are already fixed in the working tree.

---

## 2026-09-28 (frontend-fix) — SETTLE-WORKER COLOUR REGRESSION FIXED (authorised frontend-only change)

scope: FRONTEND ONLY — 2 files. NO backend, betting, Auto-Bet or alert logic touched.
result: COMPLETE — regression fixed and verified; no new failures.

### ROOT CAUSE
`historyAlertsHTML()` (dashboard.js) merged the UNDER + Q3 history stores and re-sorted
newest-first by `triggered_at`. The comparator returned 0 for EQUAL timestamps, and Array.sort is
stable, so records sharing a trigger instant came out in OLDEST-FIRST store order — the inverse of
the settlement colour contract (the panel must render the newest record on top). The pre-existing
render was `UNDER_ALERTS.history.slice().reverse()` (newest-first). Records that share a trigger
instant therefore flipped from reverse-store-order to store-order.

The frontend work ALSO rewrote the existing contract test
`tests/test_resulted_alerts_color_coverage.py::test_add_remove_reorder_never_changes_any_rows_colour`
from the true contract (`[["al-over"],["al-push"],[],["al-unknown"]]` — reverse store order) to the
buggy order (`[[c] if c else [] for c in afterReorder]` — store order), masking the regression for
that suite while `test_settle_worker` still failed.

### FIX
- `blm_v4/dashboard/static/dashboard.js` — `historyAlertsHTML()`: each merged item now carries
  `seq` (its position in the append-ordered store) and the comparator tie-breaks EQUAL / ABSENT
  timestamps by `b.seq - a.seq` (newest-inserted first). Distinct timestamps still sort newest-first
  (the accordion directive — cross-family ordering preserved).
- `tests/test_resulted_alerts_color_coverage.py` — restored the assertion to its pre-regression
  contract (reverse store order). NOT a weakening: it re-tightens a test the regression had loosened.

### ORDERING CONTRACT NOW COHERENT (all four pass)
- distinct trigger timestamps → newest-first by ts (accordion directive) ✓
- record with NO trigger timestamp → sorts LAST ✓
- equal trigger timestamps → newest-inserted first = reverse store order (settlement colour) ✓
- sort is presentation-only; stores keep append order ✓

### TESTS
- tests/test_settle_worker.py::test_valid_verdicts_reach_the_frontend_with_the_correct_colour: PASS
- Required result/alert list (10 suites): 131 passed, 0 failed.
- Frontend integrity (13 suites): 163 passed, 5 failed — ALL 5 pre-existing (fail identically at HEAD;
  the `blm-signal-row` vocabulary class).
- Full suite: 13 failed / 1885 passed / 1 skipped (was 14/1884 before) — delta is exactly the fixed
  test; ZERO new failures. The 13 remaining are all pre-existing.
- `node --check` dashboard.js: PASS.

---

## 2026-09-28 (frontend-fix) — COMMIT HELD (operator decision) + fix preserved as patches

decision: DO NOT COMMIT. Operator: let the frontend agent own committing
  `blm_v4/dashboard/static/dashboard.js`; the one-hunk fix is re-applied AFTERWARD.
reason: the fix is layered on the frontend agent's uncommitted redesign of
  historyAlertsHTML() and cannot be isolated/applied at HEAD.
state: working tree STILL carries the fix (uncommitted). NO commit made by this agent.
preserved: /tmp/blm-ra/settle-colour-fix/
  - dashboard-historyAlertsHTML-sort.patch
  - test-color-coverage-assertion.patch
  - README.txt   (re-apply + verify steps)
  Both patches round-trip verified: apply -> byte-identical to the fixed files.
re-apply: git apply /tmp/blm-ra/settle-colour-fix/*.patch  (from the repo root)

---

## 2026-09-28 (regression-agent) — remaining verification + HEAD-advance note

HEAD ADVANCED mid-session: afcc27d -> cdd6f08 ("Add a cold-cache /live latency
regression guard for the historical-context path"); that commit adds ONLY
`tests/test_live_cold_cache_latency.py` (296 lines). Frontend files remain
uncommitted; the working tree still carries the settle-colour fix.

commit for the settle-colour fix: HELD (operator). Fix preserved as
round-trip-verified patches in /tmp/blm-ra/settle-colour-fix/. NO commit made
by this agent.

### VERIFICATION (fix applied, frozen)
- Required result/alert list + frontend integrity + latency guards (26 suites):
  326 passed / 6 failed. All 6 PRE-EXISTING: 5 frontend-vocabulary
  (test_dashboard_audio_alert, test_m009_m5_frontend_integrity,
  test_z_frontend_migration, test_z_historical_alert_frontend,
  test_under_alert_lifecycle::test_no_alert_vocabulary_regression) + 1 liveness
  (test_status_liveness::test_watchdog_pets_only_when_fast_cycle_fresh).
  test_live_route_latency_guard + test_live_market_gate PASS.
- New guard tests/test_live_cold_cache_latency.py + tests/test_settle_worker.py: 18 passed.
- Full suite (captured at afcc27d, fix applied): 13 failed / 1885 passed / 1 skipped
  (was 14/1884 pre-fix; delta = the fixed test only). cdd6f08 adds only the new
  (passing) guard, so the figure is unchanged in substance.

### FLAKE CHARACTERISED (environment, pre-existing, NOT the fix)
test_under_alert_lifecycle::test_every_game_carries_its_own_verdict passes
standalone but fails in FULL-SUITE order: `blm_v4/test_stack.py::
apply_test_environment()` writes os.environ["BLM_POKERBET_DB"] = <root>/test_env/
blm_pokerbet_test.db with NO teardown, so a later v4_live() reads an EMPTY test DB
-> payload["games"] == [] (repro: pytest tests/test_test_stack.py + that test ->
1 failed; either alone -> passes). Introduced by the untracked tests/test_test_stack.py.

### SCOPE
Changed by this agent: ONLY `blm_v4/dashboard/static/dashboard.js` (the sort) +
`tests/test_resulted_alerts_color_coverage.py` (restored assertion). 0 backend /
betting / Auto-Bet / alert / threshold / API-contract changes. Commit made: NONE.

---

## 2026-09-28 (frontend-fix) — COMMITTED (operator-authorised): 5c4c89d

commit: 5c4c89d57ebb755da02ff389f622778f5d338654
subject: Preserve resulted alert ordering and colour coverage
parent: cdd6f08
files (2):
  blm_v4/dashboard/static/dashboard.js          (complete frontend redesign + the
                                                 historyAlertsHTML() seq tie-break fix)
  tests/test_resulted_alerts_color_coverage.py  (frontend vocab changes + strict
                                                 rowClasses assertion preserved)
NOT pushed. Backend / betting / Auto-Bet / alerts / thresholds / API: untouched.
Other agents' work (index.html, styles.css, backend, etc.) left uncommitted.
verify: node --check OK; pytest test_settle_worker + test_resulted_alerts_color_coverage = 24 passed.

---

## 2026-09-28 (frontend-fix) — COMMITTED: 436e73e (remaining frontend-owned files)

commit: 436e73e47b332a8d76725177ea3decf62b3ecf5f  "Finalize LIVE ALERTS / RESULTS frontend handoff"
parent: 7f30f80
files (3):
  blm_v4/dashboard/static/index.html   (🔴 LIVE ALERTS / 🟢 RESULTS / 🎯 LIVE GAMES; user pill + SIGN OUT)
  blm_v4/dashboard/static/styles.css   (active-alert card state, RESULTS panel, responsive)
  tests/test_settle_worker.py          (requirement 6: NO FINAL -> RESULT PENDING)
NOT pushed. No backend/betting/execution/C2-forensic content.
Frontend HEAD is now 436e73e. dashboard.js unchanged vs 5c4c89d; sort fix present.
verify: pytest test_settle_worker + test_resulted_alerts_color_coverage = 24 passed.

GAP (flagged, NOT committed — outside the authorized set):
  index.html references /static/explorer.js and /static/stats.js, both UNTRACKED.
  Standalone login page assets also untracked: login.html, login.js, login.css, blm-login-arena.jpg.
  Frontend is not self-contained from a fresh clone until these are committed.

## 2026-09-29 (watchdog follow-up IMPLEMENTED) — bounded startup grace ping COMMITTED (ed6f1f0). NOT PUSHED, NOT RESTARTED, NOT DEPLOYED.

result: the forensic follow-up (§8 of the 2026-09-29 watchdog forensic report) was implemented
  under explicit authorization, tested scratch-first, and committed. HEAD = ed6f1f0
  (child of 59f6791); origin still 59f6791 (ahead 1). Nothing restarted/deployed/pushed.

### WHAT LANDED (commit ed6f6791-prefix ed6f1f0, 2 files, +196/−15)
  blm_v4/collector.py:
    - WATCHDOG_STARTUP_GRACE_S = 60.0 + _startup_grace_seconds() =
      min(60s, WatchdogSec/2) → 45s on this unit (WatchdogUSec=90s).
    - _watchdog_loop_body(): while NO fast cycle has EVER completed and
      start-up is inside the grace window, ping WATCHDOG=1 with
      STATUS=startup grace; window ends at first completed cycle.
      Steady-state liveness still strictly cycle-gated (wedge protection
      unchanged: kill = last completion + 30s deadline + 90s WatchdogSec).
    - starving-flag fix: one STOPPING warning per starvation episode
      (was 1 per 5s pass; 251 lines across the two storms) and the
      "fresh again — resuming" line is now reachable.
  tests/test_status_liveness.py:
    - test_watchdog_pets_only_when_fast_cycle_fresh now pins the
      POST-grace state (deliberate contract change); new tests pin the
      cap arithmetic, the grace→silence boundary, first-cycle recovery,
      and exactly-once warning/resume logging.

### VERIFICATION (scratch-first, per instruction)
  isolated scratch tree (git archive of 59f6791 + the untracked
  thread-affinity test) at /home/ubuntu/blm-scratch-grace — removed after
  landing: py_compile OK; targeted plan 19 passed (test_status_liveness 15
  + test_collector_thread_affinity 4; the TargetClosedError line in output
  is the affinity test's expected Playwright teardown noise). NO full
  suite, NO competing heavy tests. Real tree received the change only
  after green; commit made from it.

### WATCHDOG FORENSIC REPORT (2026-09-29) — condensed record
  All 10 journal kill/restart pairs decoded (NR=11 = 11 scheduled restarts
  from 10 kills; no kill since 07:35:29Z). Kill arithmetic verified to the
  second: kill = last fresh ping + WatchdogSec(90s); restart needs
  inter-completion gap ≥ ~120s (30s deadline + 90s dog). Root cause:
  legitimate slow-cycle protection + host-load starvation (06:02–06:13 two
  concurrent full pytest suites; 06:50–07:35 qemu win11 VM + desktop
  Chrome; 479 capture overruns ≥3s, six ≥30s, max 113.3s — all KEPT per
  59f6791, which is what let the process keep progressing); the removed
  grace ping amplified first-60s fragility into a self-sustaining restart
  chain (7 of 10 kills on a process <60s into life). NOT causes: ping
  suppression (disproven), READY/first-cycle bug (disproven), SIGALRM
  interaction (disproven: 0 TimeoutError since 06:02; all 36 persist
  failures since are the pre-existing NOT NULL market_type IntegrityError
  class). 59f6791 itself: correct, kept unchanged.

### SIGALRM/PERSISTENCE — STILL FIXED
  0 TimeoutError in the collector journal since 06:02 (was 425/day).
  Recurring class = pre-existing sqlite3.IntegrityError NOT NULL
  quarter_market_observations.market_type (collector.py:1059 →
  storage.py:908) — separate defect, unowned, still open.

### PRODUCTION DB INTEGRITY CHECK — NOT COMPLETED (safety abort)
  Authorized read-only quick_check (mode=ro + query_only + idle I/O)
  could not obtain even a metadata read: connect stalled >25s on the live
  WAL DB (expected holders only: server 23686 + collector 129311; header
  verified WAL, page size 4096). Aborted rather than compete with the
  watchdog-timed collector's lock choreography. No backup copy of
  blm_pokerbet.db found on-box this session. INTEGRITY QUESTION REMAINS
  OPEN — retry when the collector is next quiesced or from a snapshot.

### ACTIVATION CAVEAT (load-bearing)
  The unit runs the WORKING TREE and has NOT been restarted: the running
  collector (PID 129311, since 07:35:54Z) is still executing 59f6791 —
  WITHOUT the grace window. The fix takes effect only at the next unit
  restart, which (a) is NOT authorized from this session and (b) should
  not be done while the tree is dirty with other owners' work (17
  tracked-modified paths) — a restart would take ALL of it live. Until
  then, the known residual risk stands: a host-load spike that denies the
  collector its first completed cycle within ~120s of a restart (or a
  >120s stall in steady state) will still be killed by systemd.

### STATE AT STOP
  HEAD ed6f1f0 (= origin/handoff-2026-09-07 + 1, NOT pushed — push needs
  explicit authorization); index empty; 0 stashes; dirty tree = the
  63-path handoff baseline MINUS collector.py/test_status_liveness.py
  (now committed) = 17 tracked-modified + 46 untracked. Services
  untouched: blm-server NR=0 (up since 01:19:02Z), blm-collector NR=11
  (up since 07:35:54Z, healthy, 0 watchdog stalls since 07:39Z).
  /healthz 200 auth:true users:2 (port 2262).

### NEXT ACTIONS (owner decisions)
  1. Push ed6f1f0 to origin/handoff-2026-09-07 (explicit authorization).
  2. Decide restart timing for the grace-window fix: clean the 17 dirty
     paths by owner commits FIRST (deploy rule: the tree must equal the
     verified commit before a restart), then restart blm-collector once.
  3. Re-run the DB quick_check from a snapshot (see above).
  4. Owner stream: NOT NULL market_type IntegrityError defect.
  5. Consider FAST_LIVENESS_FACTOR review (6.0 → higher) as the separate
     owner-decision item already flagged in the forensic report.

### ADDENDUM 13:25Z — KILL #12 + UNPLANNED LIVE ACTIVATION OF THE GRACE WINDOW
  - 13:20:22Z: 12th watchdog kill (old code, PID 129311): ticks degraded
    41s → 54s → 110.1s (all completed, errors=0) under an unattributed
    load spike (15-min loadavg 15.26; no pytest running after; box load
    15 at 13:22). Kill = last fresh ping + 90s exactly (last completion
    13:20:17.8). Consistent with the day's starvation pattern.
  - The systemd respawn (13:20:46, PID 187480) picked up the new
    collector.py (mtime 13:20:31 < start): boot log carries
    "self-watchdog active: fast-liveness deadline 30s, startup grace
    until +45s" — ed6f1f0's grace window is LIVE on the running unit
    (activated by systemd's own kill cycle, no restart command from this
    session). Early evidence: first cycles completed inside the window
    under load 15; 2 one-line starvation episodes (new once-per-episode
    warning) with recovery, 0 kills since. WARNING: the same restart
    also loaded the rest of the dirty tree into the process (standing
    condition of this box — every restart runs the working tree).
  - NRestarts baseline for the next agent: 12.

### ADDENDUM 13:35Z — DIRECTIVE VERIFICATION COMPLETE; LIVE VALIDATION AWAITING AUTHORIZATION
  Full targeted collector/liveness set at ed6f1f0: pytest
  tests/test_status_liveness.py tests/test_collector_thread_affinity.py
  tests/test_collector_timeout_budget.py -> 24 passed (closes the
  timeout-budget gap from the earlier scratch run). Directive's hard
  rules re-verified: deploy/blm-collector.service byte-untouched by the
  commit (its M = owners' baseline dirt); SIGALRM persistence region
  byte-untouched (0 diff lines touching _on_alarm/setitimer/
  _capture_overran/ITIMER); market_type defect NOT mixed in. All
  directive TESTS items (1-9) green. NEXT: live validation (fresh start
  + synthetic load + wedge-kill audit) ONLY on explicit authorization;
  not restarted/deployed/pushed by this session beyond the 13:20 systemd
  self-activation already recorded.

### ADDENDUM 18:57Z — INCIDENT: FULL STACK OUTAGE/DEGRADATION → RECOVERED
  SYMPTOMS: server up-but-unreachable (healthz 000, 45s timeouts, accept
  queue backed up), collector at NR=13 for the day, box load 20-24, swap
  3/3 GB full, settle_worker/scorecard/game_finished_reconciler all
  failing with "database is locked", server restart-looping on startup
  WAL init.
  ROOT-CAUSE CHAIN: day of watchdog kills (load spikes + grace-less
  window pre-ed6f1f0) killed the collector mid-write repeatedly ->
  blm_pokerbet.db WAL ballooned to 7.1 GB (never checkpointed) -> every
  server startup crawled against the giant WAL and lost lock races ->
  "Application startup failed" loop; server event loop starved under
  load 20 -> port accepting but never answering.
  RECOVERY (authorized by operator "get blm up and running"):
    1. Removed session-clone Chrome (blm-clone-chrome unit + 5 MB
       profile) — no longer needed, was eating RAM during incident.
    2. Sequenced boots: collector stopped -> server restarted (booted
       clean in ~3 min, checkpointed the ENTIRE WAL: 7.1 GB -> 5.5 MB)
       -> collector started.
  FINAL STATE (18:55Z): blm-server PID 231960 ACTIVE, healthz 200
    {"status":"ok","auth":true,"users":2}; blm-collector PID 231281
    ACTIVE "fast cycle complete 8.1s ago", grace window live
    ("startup grace" observed in StatusText during boot); WAL 5.5 MB;
    load ~9 falling. NOTE: server NR=2, collector NR=0 (counters reset
    by the recovery restarts).
  OPEN ITEMS FOR OWNER: (1) WAL hygiene — 7 GB un-checkpointed WAL
    means wal_autocheckpoint is being starved by constant write
    pressure; consider periodic passive_checkpoint or investigating the
    long-lived read transaction holding it open. (2) ed6f1f0 still not
    pushed. (3) NOT NULL market_type ingest defect still firing. (4)
    Restart-cause load spikes (qemu/suites) recur — keep heavy jobs off
    this box or accept watchdog kills in steady state.

### ADDENDUM 01:52Z (2026-09-30) — PUSH AUTHORIZATION EXECUTED: ed6f1f0
  ALREADY ON ORIGIN — NO PUSH NEEDED
  Operator authorized: "Push ed6f1f0 to origin/handoff-2026-09-07".
  Verified BEFORE pushing (git status -b, git log, git ls-remote):
    - local tracking ref origin/handoff-2026-09-07 = 9f0c3bf, and the
      AUTHORITATIVE remote (git ls-remote) confirms 9f0c3bf — no
      divergence; tracking ref is accurate.
    - git merge-base --is-ancestor ed6f1f0 origin/handoff-2026-09-07 →
      YES: ed6f1f0 was already pushed (at some point after the 18:57Z
      addendum, along with e5a11dc, 16d3a83, f425fd6, 9f0c3bf).
  CONSEQUENCE: no push performed. Blind `git push` would additionally
    have published 36d7538 ("Take event-view identity from the selected
    sidebar section, not the whole page"), the ONLY unpushed commit,
    which is NOT covered by this authorization — deliberately not
    pushed.
  Git State: branch handoff-2026-09-07, HEAD 36d7538, origin head
    9f0c3bf, branch ahead 1 (36d7538) / behind 0. Nothing staged by this
    action; the 17 owner-dirty paths remain byte-untouched; STATUS.md
    itself remains untracked (content-only update). Open item (2) of the
    18:57Z addendum ("ed6f1f0 still not pushed") is now CLOSED — already
    on origin. Push of 36d7538 remains a separate decision for the
    operator.

### ADDENDUM 02:27Z (2026-09-30) — WAL HYGIENE IMPLEMENTED (INCIDENT OPEN
  ITEM 1): COMMIT e8975f7, PENDING AUTHORIZED RESTART TO ACTIVATE
  LIVE EVIDENCE THE PATHOLOGY IS RECURRING: at 02:00Z the production
  blm_pokerbet.db-wal measured 1,759,132,912 bytes (1.76 GB) — 7 h
  after the 18:55Z recovery checkpointed it to 5.5 MB (~250 MB/h of
  re-accumulation). wal_autocheckpoint is being starved continuously;
  the 2026-09-29 chain (ballooned WAL → restart loses lock races) is
  re-arming itself right now.
  IMPLEMENTATION (commit e8975f7, 3 files, +475/-0, NOT pushed):
    - blm_v4/wal_hygiene.py — stat-gated periodic
      PRAGMA wal_checkpoint(PASSIVE): threshold 256 MB
      (BLM_WAL_HYGIENE_THRESHOLD_MB), cadence 300 s
      (BLM_WAL_HYGIENE_INTERVAL_S, 0 disables), PASSIVE never blocks
      readers/writers so the pass cannot reintroduce the incident's
      lock races; result-row semantics pinned empirically
      (busy=was-blocked flag, log=total frames, checkpointed=copied;
      derived held_back = log - checkpointed).  Fail-closed; worker
      never dies.  NO-STRATEGY §19: touches only the WAL file's
      physical layout; scorecard (owner-dirty) not imported.
    - server.py — WAL_HYGIENE worker thread in the existing worker
      fleet (SettleWorker idiom: daemon start/stop, app.state,
      stopped in stop_pipeline), started after the settle worker.
      server.py is NOT owner-dirty.
    - tests/test_wal_hygiene.py — 12 tests, ALL on throwaway tmp_path
      DBs (production DB never opened): stat gate/boundaries, real
      checkpoint, open-reader pins tail while PASSIVE copies the
      prefix unblocked, corrupt-DB fail-closed, worker cadence via
      wait-loop, env knobs.  12 passed under the documented
      BLM_ALLOW_HEAVY_TESTS=1 override (single small file, no
      browsers, no full suite).
  ACTIVATION: requires an authorized blm-server restart (NOT performed
    — no restarts without explicit authorization).  Once live it will
    checkpoint at every 300 s tick while the WAL exceeds 256 MB, so
    the WAL can physically never re-reach the 1.76 GB → 7.1 GB regime
    between restarts.  Interim mitigation for the already-1.76 GB WAL:
    one authorized restart checkpoints it entirely (as at 18:55Z), and
    the worker then keeps it bounded for life.
  Git State: branch handoff-2026-09-07, HEAD e8975f7, origin head
    9f0c3bf, ahead 2 (36d7538, e8975f7) / behind 0.  Owner-dirty paths
    byte-untouched.  e8975f7 (and 36d7538) NOT pushed — push remains
    the operator's separate decision.

## 2026-10-01 (api-reliability directive) — /LIVE AMPLIFICATION + FALSE COLLECTOR-OFFLINE FIXED & DEPLOYED — COMMIT d864988

  AUTHORIZATION: explicit directive ("authorized to modify code, run
  tests, restart services, and deploy").  Scope held to the five named
  fixes; alert/capture logic untouched by construction (see FIX 9 below).

  BASELINE (recorded before touching anything): branch
  handoff-2026-09-07, HEAD f174ade, 63 owner-dirty paths (unchanged
  from the previously known state), blm-server pid 6151 up since
  02:36Z, blm-collector active.  Files edited (api.py, dashboard.js,
  conftest.py) were all CLEAN — no owner-dirty path was written; owner
  mtimes (2026-09-28/29) all predate this session.

  ROOT CAUSE (measured, not inferred): /api/v4/live is a SYNC route, so
  FastAPI runs its body on the AnyIO worker pool (40 tokens).  The
  per-game analysis costs 505 s COLD / 98.5 s WARM / 2.1 s fully-hot on
  the production DB (direct in-process profile).  It was requested every
  5 s by the dashboard, by the in-process betting worker (its own 4 s
  cache made that a full build every 4 s), and by the trace UI.  py-spy
  during the incident: 40/40 tokens inside v4_live (35 in _analyze_game,
  32 in the historical-context path).  FileResponse(/login) and
  StaticFiles(JS/CSS) use the SAME pool → the page could not load its
  assets.  Caddy access log: a /api/v4/live request that never completed
  (21,463 s).  Independent second bug: the dashboard painted a poll
  timeout OR a 401 as "COLLECTOR OFFLINE — data collection is DOWN".

  FIX 1 (backend): blm_v4/api.py — the existing route body preserved
  VERBATIM as _v4_live_uncached (sha256 cf83c847ac2d6db4…, 15,140 chars,
  IDENTICAL to HEAD's v4_live body).  A thin v4_live wrapper adds
  single-flight coalescing + 5 s TTL + stale-while-revalidate (a caller
  never holds a pool token waiting for a build).  A failed build is never
  cached, degrades to the last good payload, and arms a fail-fast
  cooldown.  Bounds env-tunable: BLM_LIVE_CACHE_TTL_S(5) /
  _MAX_STALE_S(300) / _SINGLEFLIGHT_WAIT_S(8) / _ERROR_COOLDOWN_S(5).
  _LIVE_CACHE bounded at 16 keys.
  FIX 1 (frontend): dashboard.js — /live poll is client-side
  single-flight on its own cadence (LIVE_POLL_MS 20 s, LIVE_TIMEOUT_MS
  45 s), documented as NOT cancelling server-side work.
  FIX 2/3: dashboard.js — collector pill now GREEN RUNNING / ORANGE
  STALLED / RED OFFLINE (ONLY from a heartbeat read older than
  COLLECTOR_STALE_AFTER_S=90) / AMBER UNVERIFIED (request failed or timed
  out) / SESSION EXPIRED (401).  renderStatus no longer writes the
  collector pill → the (now cached) /live payload can never be the
  authority on collector health.  On 401 every poller stops and the user
  goes to /login?error=expired (reuses the login page's existing
  "expired" message).
  FIX 4: /api/v4/status was ALREADY independent of the /live path
  (only _load_collector_state/_db_stats/_freshest_game_state) — now
  locked by a test that fails the route if the /live builder is invoked.
  FIX 5: single-flight caps concurrent analysis at ONE; verified at the
  HTTP layer (8 concurrent GETs → 1 build).

  TESTS: 33 new (tests/test_live_singleflight_cache.py,
  tests/test_dashboard_status_semantics.py) — single-flight, TTL reuse
  and expiry, no-cache-poisoning, degrade-to-last-good, fail-fast
  cooldown, /status isolation, HTTP-layer concurrency, /status
  responsiveness during a build, and the frontend state machine (incl.
  node --check on dashboard.js).  conftest sets BLM_LIVE_CACHE_TTL_S=0 so
  the pre-existing suite keeps its per-call rebuild semantics.
  Broader relevant suite (71 files): 996 passed / 7 failed.  The 7 fail
  IDENTICALLY on a pristine HEAD worktree (/tmp/blm-baseline) with the
  same assertion text and line numbers → PRE-EXISTING, with a captured
  baseline.  Names: test_dashboard_audio_alert, test_deviation, test_deviation_analysis, test_m009_m5_frontend_integrity,
  test_prospective_freeze, test_z_frontend_migration,
  test_z_historical_alert_frontend.)

  DEPLOYMENT: `systemctl --user restart blm-server` (explicitly
  authorized).  The FIRST attempt FAILED — sqlite "database is locked"
  in result_reconciler/scorecard `PRAGMA journal_mode=WAL`, i.e. the
  exactly-predicted 2026-09-29 chain (30.8 GB WAL → restart loses lock
  races).  systemd auto-restart did not win the race either (crash-loop,
  2 failed startups).  Recovery required (FIX 6 authorises WAL action to
  restore service): stop blm-server + blm-collector → checkpoint →
  start.  Because the WAL-hygiene worker (commit e8975f7) is now LIVE,
  the interim mitigation STATUS.md specified is what ran here.
    - pre-recovery WAL : blm_pokerbet.db-wal 30,817,735,992 B (30.8 GB);
      blm_metrics_clean.db-wal 3,215,635,312 B.
    - PRAGMA wal_checkpoint(PASSIVE): busy=0, log=7,480,033 frames,
      checkpointed=7,480,033 (ALL) in 805 s.
    - PRAGMA wal_checkpoint(TRUNCATE): WAL → 0 B.  journal_mode=wal.
    - post-recovery WAL: blm_pokerbet.db-wal 21 MB;
      blm_metrics_clean.db-wal 1.2 MB.  Disk 83%→69% (38 GB → 70 GB free).
    - PRAGMA quick_check: ABORTED, not completed.  It is a long full-scan
      read transaction, and in WAL mode an open reader pins the WAL tail
      — while it ran the WAL regrew 21 MB → 235 MB (the hygiene worker's
      PASSIVE checkpoint could not advance, exactly the documented
      "open-reader pins tail" behaviour).  It was killed and the tail
      reclaimed (PASSIVE: busy=0, log=37, copied=37, held_back=0; WAL
      152 KB).  This matches the prior "PRODUCTION DB INTEGRITY CHECK —
      NOT COMPLETED (safety abort)" precedent above.  Operational
      integrity is instead evidenced by: the checkpoint itself
      (busy=0, 7,480,033/7,480,033 frames copied, no error),
      journal_mode=wal, and both services reading AND writing the DB
      continuously with zero errors afterwards.  A full integrity_check
      should be run in a maintenance window with writers stopped.
    - Both units restarted cleanly afterwards: blm-server pid 289789
      (23:17:50Z), blm-collector pid 290120 (23:18:17Z), each NRestarts=0.

  PRODUCTION VERIFICATION (deployed, 2026-10-01T23:17Z+):
    - endpoint latency before → after: /login 18.371 s → 0.004 s;
      /healthz 5.756 s → 0.003 s; /static/dashboard.js 0.158 s → 0.002 s;
      /api/v4/status 0.037 s → 0.001 s; /api/v4/live 0.004 s → 0.002 s.
    - AnyIO worker threads: 28 (23 inside v4_live) → 1 (0 inside v4_live).
    - identical harness, 40 concurrent /live: 31.36 s → 0.73 s wall;
      /login max during burst 16.012 s → 0.637 s; peak threads 46 → 12.
    - real browser traffic (Caddy log): /api/v4/live p50 2.00 s,
      max 21,463 s BEFORE → p50 0.00 s, max 11.79 s AFTER; /api/v4/status
      max 1,277 s → 1.31 s.  Since 23:17Z: 16×200, and the only 3×502
      were at 23:17:42 (the seconds the server was still binding).
    - collector: tick advancing, errors 0, fresh snapshots +
      market_observations landing after the restart; collector was NOT
      restarted by the fix (its PID was untouched by the server restart).
    - host: I/O PSI some 41% → 16%; memory PSI 13% → 0.00%; swap
      2.9 GiB → 154 MiB.

  FIX 9 (alert/capture safety) — PROVEN, not asserted: 11 critical
  modules byte-identical to HEAD (under_alert, under_fingerprints,
  fingerprint_stats, under_outcome, clean_metrics, projection,
  terminal_eligibility, collector, benchmark, betting/worker,
  betting/executor).  The analysis body is byte-identical.  No
  alert/capture/checkpoint/fingerprint file was opened for writing.

  KNOWN ITEMS HANDED OVER (not fixed — out of scope):
    1. blm-collector crash-loop: NRestarts=16, restarted on its own at
       22:36:31Z (PID 233339 → 275485 → 290120).  Pre-existing, and NOT
       the cause of the dashboard outage.
    2. The betting worker consumes the same (now cached) /live payload.
       It already tolerated 4 s of staleness via its own cache, and
       betting is DRY_RUN=true with the kill switch OFF.  If auto-betting
       is ever enabled beyond DRY_RUN, set BLM_LIVE_MAX_STALE_S small
       (e.g. 15) so the betting input can never be older than that; the
       payload's own LIVE-MARKET / stale-state gates remain the
       authoritative net.  Changing betting plumbing was explicitly out
       of scope.
    3. WAL hygiene threshold is 256 MB — fine now that the WAL is 21 MB.

  Git State: branch handoff-2026-09-07, HEAD **d864988** (local commit,
  NOT pushed — push remains the operator's separate decision; origin
  head unchanged).  Owner-dirty paths byte-untouched: 63 owner paths
  still dirty, +3 files modified by this fix, +2 new test files = 68.
  Files in d864988: blm_v4/api.py, blm_v4/dashboard/static/dashboard.js,
  tests/conftest.py, tests/test_live_singleflight_cache.py,
  tests/test_dashboard_status_semantics.py.

## 2026-10-02 (wal-hygiene directive) — WAL ROOT CAUSE FIXED & DEPLOYED (uncommitted)

  DIRECTIVE: investigate/fix the database/WAL situation — why ~30.8 GB of
  WAL was generated, a safe integrity check, collector impact, smallest
  safe fix, validate, and state before deploying.  Constraints honoured:
  no production data deleted, no unrelated dirty path reset/stashed, no
  restart until the diagnosis + fix were identified and its reason stated.

  ROOT CAUSE — WAL unbounded growth is INHERENT to WAL mode when a long
  transaction pins the tail.  blm_pokerbet.db is written continuously
  (~5.7 MB/min: collector snapshots + market_observations, scorecard
  sections, settle/reconcile).  A checkpoint may only copy frames up to
  the OLDEST active reader/writer; while any long txn is held, the
  checkpoint stalls and writers keep appending → growth without bound.
  The 30.8 GB was a SUSTAINED pin (the /api/v4/live amplification fixed
  at d864988, plus the collector's watchdog kill/relaunch loop keeping a
  txn active continuously).  TWO structural defects made it
  unfixable-by-design:
    (a) the janitor (blm_v4/wal_hygiene.py) was PASSIVE-ONLY — and PASSIVE
        NEVER SHRINKS THE WAL FILE; it copies frames but leaves the file
        at its high-water mark, so the janitor literally could not bound
        the file size; and
    (b) gate 256 MB + cadence 300 s were too coarse — zero passes fired
        during the whole incident.

  EVIDENCE (measured, not inferred):
    - Reproduced in production: a controlled long read (consistent
      snapshot) pinned the tail; source WAL grew 2.1 MB → 205.1 MB
      (+203 MB) in 1609 s; WAL reclaimed to 32 B the instant it ended.
    - Mechanism lab (throwaway DB): fully-consumed open read conn →
      held_back=0 (no pin); PARTIAL cursor → held_back=1; explicit BEGIN
      → held_back=4.  So /live (fully consumes every query) is NOT a pin
      source; long write txns / unterminated cursors are.
    - PASSIVE-vs-TRUNCATE lab: PASSIVE with no readers → file stays
      9.29 MB; TRUNCATE + journal_size_limit=1 MB → 0.00 MB.  Confirms
      defect (a).
    - This doc's own 2026-09-29 record (below) already named both the
      30.8 GB WAL and the "open reader pins the WAL tail" mechanism.

  INTEGRITY CHECK (on a consistent copy, never touching production):
    - snapshot via SQLite online-backup API: 18.47 GB in 1609 s
      (11.5 MB/s); source WAL pinned only for the copy's duration.
    - PRAGMA quick_check: PASS (ok), 3226 s.
    - PRAGMA integrity_check: PASS (ok), 3744 s.  Both on the consistent
      snapshot copy — production was never pinned by the check itself.
      DB is structurally sound; the 30.8 GB WAL was a checkpoint-starvation
      symptom, not corruption (matches the 2026-09-29 full-frame PASSIVE
      checkpoint: busy=0, ALL 7,480,033 frames copied, no error).

  FIX (smallest safe change; only clean files touched):
    1. blm_v4/wal_hygiene.py — after PASSIVE proves nothing pins the tail
       (held_back==0) the pass runs PRAGMA wal_checkpoint(TRUNCATE) with
       journal_size_limit=64 MB, so the WAL FILE can never persist above
       the cap.  The TRUNCATE is SKIPPED whenever a reader/writer pins the
       tail (it would wait) → the pass still NEVER blocks the pipeline.
    2. server.py — WAL_HYGIENE_INTERVAL_S 300 → 60 s; THRESHOLD_MB 256 →
       64 MB (engage while the WAL is still small); new
       WAL_HYGIENE_JOURNAL_LIMIT_MB=64; new WAL_HYGIENE_ALERT_MB=512.
    3. blm_v4/wal_hygiene.py — a hard ALERT at 512 MB logs a WARNING with
       held_back, so a pinned txn is visible immediately instead of at
       tens of GB.  New env knobs BLM_WAL_HYGIENE_JOURNAL_LIMIT_MB and
       BLM_WAL_HYGIENE_ALERT_MB (mirroring the existing env_* helpers).
    Files: blm_v4/wal_hygiene.py (+111/−33), server.py (+35/−...),
    tests/test_wal_hygiene.py (+84).

  TESTS: 16 pass (12 pre-existing + 4 new: truncate shrinks the file; no
    truncate while a reader pins; alert logged when over bound; env knobs
    parse + fall back).  py_compile clean on both edited modules.

  DEPLOYMENT: `systemctl --user restart blm-server` — REQUIRED (the
    WalHygieneWorker is built at startup; no hot-reload, so the running
    server held the old PASSIVE-only config).  Low risk this time: WAL
    was single-digit MB, not 30.8 GB.  Pre-restart WAL 3.44 MB → restart
    2026-10-02T03:42:15Z, pid 289789 → 419286, NRestarts=0, clean startup;
    "wal_hygiene_worker_started interval_s=60.0 threshold_mb=64.0
    journal_limit_mb=64.0 alert_mb=512.0".  blm-collector NOT restarted.

  VALIDATION (post-restart):
    - WAL pin sampler across 24 passes: 0/24 reported busy=1 (no pinned
      pass); WAL cycling (e.g. 9.3 → 16 MB) with frames copied each tick.
    - Live code path on the production DB: passes ran with held_back
      ~4,200-8,600 (a writer pinning) → truncated=false (correctly
      skipped, never blocked); the moment held_back hit 0 the SAME pass
      returned truncated=true and the file shrank (logged: WAL file ≤ cap).
    - Server: /healthz 200, / 303, /api/v4/status 401 (auth); scorecard
      runs completing; no real errors since restart (the blm_v1.collector
      "TargetClosedError" stream is PRE-EXISTING — 17 occurrences logged
      in the window BEFORE my restart, unchanged after).
    - Collector: capturing continuously (snapshots advancing, errors=0),
      fast ticks 0.9-10.1 s.

  COLLECTOR IMPACT (directive §4): the collector crash-loop is NOT
    DB/WAL-caused.  24 h: 151 watchdog stalls / 39 kills (~every 37 min),
    driven by Playwright page.content() overruns (3,548 in 24 h; 0.6 →
    106 s against a 3.0 s budget) and page.evaluate stalls — one browser
    call per 10 s tick that escalates past the 30 s watchdog.  DB
    contention caused ZERO skipped captures in 24 h (the lock path skips
    the tick while keeping the watchdog fed).  settle_worker/scorecard do
    log "database is locked" inside scorecard's 30-90 s write windows — a
    real but SECONDARY latency hit.  Also observed: orphaned opera/chrome
    browsers accumulating (ages 23h45m, 5h24m) → a resource leak feeding
    the host I/O saturation (PSI some ~20-30%).

  BEFORE/AFTER WAL: 2026-09-29 incident pokerbet 30.82 GB / metrics_clean
    3.22 GB → 0.  Session start pokerbet ~21 MB cycling.  Controlled pin
    reproduced +203 MB.  Post-fix: pokerbet cycling ≤ ~40 MB (cap 64),
    metrics_clean 0; journal_mode=wal, wal_autocheckpoint=1000 both.

  REMAINING RISK (ranked):
    1. Inherent: the janitor cannot beat a SUSTAINED multi-day pin (a WAL-
       mode property) — but the ALERT now fires at 512 MB and the
       historical trigger (/live amplification) is fixed.
    2. scorecard's 30-90 s per-section write transactions remain
       (owner-dirty file, off-limits) — the largest residual pin source.
    3. blm-collector browser-driven kill-loop UNFIXED (out of DB scope).
    4. Host I/O saturation + orphaned-browser leak are systemic latency
       risks.
    5. synchronous=FULL (default) never relaxed; synchronous=NORMAL would
       roughly halve write I/O at a small durability cost — NOT changed
       (operator's call).
    6. FD limit: soft NOFILE 1024 vs unit LimitNOFILE 1048576 — candidate
       hardening.
    Uncommitted: the 3 changed files are working-tree only; HEAD is still
    d864988 (push remains the operator's separate decision).
