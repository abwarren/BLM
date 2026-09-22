# AGENT DIRECTIVE — BLM-PASS-001: run one full Scorecard pass

Issued 2026-09-20 by the operator. Honoured literally. One named verification
per requirement. A DO-NOT item outranks any inferred goal.

---

## MISSION

Cause exactly ONE Scorecard pass to run against `blm_pokerbet.db` and end in a
state you can prove: either it **completes and stamps** `scorecard_state`
(activating the incremental gate), or it **fails and yields the full
traceback** that M010 now renders. A clean, evidence-backed negative is a
success; a fabricated positive is a failure.

## WHY THIS IS THE REMAINING WORK

- The incremental gate is DEPLOYED but INERT: `scorecard_state` is empty, so
  every pass is still FULL and `checkpoint_market` is stalled at 14:03:23.
- Passes have been dying non-deterministically: 169s, 248s, 1,096s. Two longer
  runs (1,436s, 2,453s) were KILLED by restarts, not completed — so a full pass
  has never once reached the end.
- `server.py:171` documents the mechanism: the scorecard's long write-lock
  windows lock the V4 collector out of the same DB ("database is locked"
  storms). A comment records a real pass at **6,446s (1h47m)** — long runs are
  NORMAL; the short deaths are the anomaly.
- The alert work is DONE and needs no backfill (verdicts are derived on read,
  never stored). This pass is about the gate, not the alerts.

---

## HARD CONSTRAINTS (DO-NOT)

D1. Do NOT delete anything. Any `rm` — including a single file — requires the
    operator's explicit approval first. State the exact path and wait.
D2. Do NOT stop, restart, or start any systemd unit without explicit operator
    approval. Present the exact command and wait for a yes.
D3. Do NOT touch `blm-collector`. It is a separate process; never restart it.
D4. Do NOT run VACUUM, DROP, DELETE, or a manual UPDATE against
    `blm_pokerbet.db`. Only the Scorecard's own writer path may write.
D5. Do NOT run a pass while another pass is in flight. ONE writer or none —
    two concurrent writers is the leading crash hypothesis and this directive
    exists partly to stop creating that condition.
D6. Do NOT edit any source file. `scorecard.py:1110` (`timeout=30` vs
    `storage.py:181`'s `timeout=90`) is a SUSPECT, not a finding, and changing
    it is a separate evidence-gated decision.
D7. Do NOT print, log, or commit secrets, keys, or passwords.
D8. Do NOT commit or push anything.
D9. Do NOT report success from absence of an error. "No failure logged" is not
    "completed" — a killed process logs nothing (proven: the 18:19 and 18:51
    runs left no `run_failed` line and did not finish).

---

## REQUIREMENTS + ONE NAMED VERIFICATION EACH

### R1 — Monitor the in-flight pass FIRST. It may already answer the mission.
A pass started at 19:31:58 (service MainPID 29530). Do not start a competing
one. Watch it to a terminal state.
- **VERIFY `R1_inflight_resolved`**: `journalctl --user -u blm-server` since
  19:31 shows a `scorecard_run` (completed) OR a `scorecard_run_failed` for
  that pass. If it COMPLETED → go to R6. If it FAILED → go to R5. If the
  service pid changed → it was killed; note that and continue.

### R2 — Record the pre-state verbatim before touching anything.
Capture: MainPID, ExecMainStartTimestamp, NRestarts, `scorecard_state` value,
`MAX(recorded_at)` from `checkpoint_market`, row counts, and `game_results`
status counts.
- **VERIFY `R2_prestate`**: a saved pre-state block exists on disk AND
  `checkpoint_market` reads 115,196 rows / `market_history` 11,576.

### R3 — Establish SINGLE-WRITER before any pass you start.
Prove no other Scorecard writer is active: no in-flight `scorecard_run_start`
without a matching terminal event, and no second `server.py` process.
- **VERIFY `R3_single_writer`**: the check prints PASS and names the one
  allowed writer, or it BLOCKS the run. Do not proceed on a FAIL.
- If a single writer cannot be guaranteed without restarting the service,
  STOP and escalate under E1. Do not force it.

### R4 — Run the pass bounded, foreground, logged, with progress.
Run against the production DB only if R3 passed. Bound the wall clock and
track progress so a HANG is distinguishable from a CRASH. Emit `cm_max` /
`mh_max` at least every 60s. Documented worst case is 6,446s — do not kill it
for merely being slow.
- **VERIFY `R4_bounded_run`**: a timestamped log exists containing the start
  line and at least one progress line; the process ran with a hard timeout.

### R5 — If it fails, capture the traceback. This is M010's live proof.
- **VERIFY `R5_traceback`**: the journal output for `scorecard_run_failed`
  contains BOTH `Traceback (most recent call last)` AND a real exception class
  name (e.g. `sqlite3.OperationalError`). If it contains only
  `{"exc_info": true}`, M010 is NOT loaded — report that as its own finding.
  Paste the traceback verbatim in the report. Suggest a fix; do not apply it.

### R6 — If it completes, prove the stamp and the gate.
- **VERIFY `R6_stamp`**: `SELECT value FROM scorecard_state WHERE
  key='logic_revision'` returns `1` (== `SCORECARD_LOGIC_REVISION`) AND
  `checkpoint_market`'s `MAX(recorded_at)` has advanced past 14:03:23. Report
  the new row count and the pass's `elapsed_s`.

### R7 — Leave the service in the state you found it.
If you stopped anything under an approved E1, restore it and confirm.
- **VERIFY `R7_restored`**: MainPID and NRestarts are as intended and the
  startup sequence (`collector_starting` / `scheduler_starting` /
  `settle_worker_started` / `pipeline_started`) is present.

### R8 — Write the evidence to `docs/milestones/M011-SCORECARD-PASS.md`.
Include: elapsed, outcome, cm/mh deltas, `logic_revision` value, the verdict on
the timeout hypothesis (supported / not supported / undetermined), and the
exact next action. State the honest limit of what was proven.
- **VERIFY `R8_doc`**: the file exists and names the pass's elapsed, the
  outcome, and the stamp value.

---

## ESCALATION — STOP AND ASK, DO NOT PROCEED

E1. A stop/start of `blm-server` is needed to guarantee single-writer.
E2. Any deletion is needed.
E3. The pass exceeds ~2h (documented worst case 6,446s).
E4. The traceback names something needing a source change.
E5. The DB shows signs of corruption or unexpected growth.

---

## REPORT FORMAT

Bare answer FIRST, then evidence. The operator reads the first line and stops.

  1. Did a pass complete? YES / NO.
  2. `logic_revision` value now.
  3. Elapsed of the pass.
  4. THE single next action.
  Then: R1–R8 verification results, the traceback verbatim if any, and what
  remains unproven.

---

## ENVIRONMENT FACTS (do not rediscover)

- Repo `/home/ubuntu/BLM`; branch `handoff-2026-09-07`; HEAD `76dba4a`.
- `blm-server` is a **USER** unit (uid 1000). `systemctl --user …`.
  `sudo systemctl` will report it not-found. Use
  `XDG_RUNTIME_DIR=/run/user/1000` if the bus is unreachable.
- M010 is live only from the 19:31:55 start: on-disk
  `blm_v2/telemetry/logging.py` == committed HEAD `76dba4a`,
  `format_exc_info` active. Before that start, tracebacks did NOT render.
- Gate: `_gate_active` is true only when `BLM_SCORECARD_INCREMENTAL` is not
  false AND `scorecard_state.logic_revision == SCORECARD_LOGIC_REVISION` ("1").
  `BLM_SCORECARD_INCREMENTAL=0` forces a FULL pass.
- Gate skips conservatively: `_gate_checkpoints` skips a game only when all 10
  `FIXED_CHECKPOINT_PCTS` rows exist (10..90 + 100). 395 games are incomplete
  and 7,065 have no rows — they all stay in the work set by design. No manual
  backfill is ever needed.
- Loop cadence: `BLM_SCORECARD_INTER_RUN_GAP_S` (default 300s) between passes.
- Other env switches: `BLM_SCORECARD_SETTLE_GRACE_S`, `BLM_PREDICTION_FREEZE`,
  `BLM_ANALYTICS_TZ`, `BLM_MARKET_STALE_SECONDS`.
- Baseline tests: `tests/test_scorecard_incremental_gate.py` +
  `tests/test_logging_traceback.py` + `tests/test_settle_worker.py` = 39
  passed. Full suite 1,173 passed / 5 pre-existing failures
  (`test_forensic_relative_pace_freeze.py` ×3, `test_prospective_freeze.py`,
  `test_resulted_alerts_review.py`) — reproduce on a pristine worktree, so
  they are baseline, never a regression from this work.
- Untracked and NOT yours: `DECISIONS.md`, `FOUNDATION.md`, `rag/`, `backups/`,
  `*.svg`, `*.jsonl`, `*.csv`. Leave them alone.

## APPENDIX — the runner

Write, then run, a runner of this shape (do not inline `$( )` — command
substitution needs operator approval):

    python3 - <<'PY'
    import os, sqlite3, sys, time
    os.environ["BLM_ENV"] = "production"
    sys.path.insert(0, "/home/ubuntu/BLM")
    from blm_v2.telemetry.logging import setup_logging, get_logger
    from blm_v4.scorecard import Scorecard
    setup_logging(environment="production"); log = get_logger("server")
    DB = "/home/ubuntu/BLM/blm_pokerbet.db"

    def probe():
        c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        cm = c.execute("SELECT MAX(recorded_at) FROM checkpoint_market").fetchone()[0]
        mh = c.execute("SELECT MAX(recorded_at) FROM market_history").fetchone()[0]
        c.close(); return cm, mh

    sc = Scorecard(DB); t0 = time.monotonic()
    log.info("scorecard_run_start")
    try:
        stats = sc.run()
        log.info("scorecard_run", elapsed_s=round(time.monotonic()-t0, 1), **stats)
        print("COMPLETED", stats)
    except Exception:
        log.exception("scorecard_run_failed")   # M010 renders this in full
        print("FAILED at %.0fs" % (time.monotonic()-t0)); raise
    finally:
        print("final cm/mh:", probe())
    PY

Watch it in a second terminal with:

    sqlite3 "file:/home/ubuntu/BLM/blm_pokerbet.db?mode=ro" \
      "SELECT MAX(recorded_at) FROM checkpoint_market;"
