# M010 — Exception Observability (tracebacks were unrenderable)

STATUS: COMPLETE at L3 · DEPLOYED: NO · LIVE VERIFICATION: PENDING DEPLOY

## INCIDENT

2026-09-20 17:46:51 the scorecard loop started a pass; at 17:50:59 it failed
after 4m08s. `journalctl --user -u blm-server` shows the entire record of that
failure:

```
{"exc_info": true, "event": "scorecard_run_failed", "timestamp":
 "2026-09-20T17:50:59.358069Z", "level": "error", "logger": "server", ...}
```

No exception type. No message. No frame. `grep -c Traceback` over the whole
boot returned 0.

## OBJECTIVE

An exception logged anywhere in BLM must render its full traceback.

## ROOT CAUSE (proven, not inferred)

`server.py:192-193` does `except Exception: logger.exception("scorecard_run_failed")`
— correct. The defect was in the pipeline.

`blm_v2/telemetry/logging.py::_shared_processors()` built

```
TimeStamper, add_log_level, add_logger_name, StackInfoRenderer,
set_exc_info, _add_correlation_id, UnicodeDecoder
```

`structlog.dev.set_exc_info` sets `exc_info` to a **BOOLEAN flag**, expecting a
later processor to replace it with the real traceback. That processor is
`structlog.processors.format_exc_info` / `ExceptionRenderer` — and

```
grep -rn "format_exc_info\|ExceptionRenderer" --include=*.py .
```

returned **zero hits across the entire repository**. The flag therefore reached
`JSONRenderer` verbatim and was serialised literally. The crash destroyed its
own evidence by construction; it was never that the traceback was lost.

## WHAT CHANGED

One entry added to `_shared_processors()`, after `set_exc_info`:

```python
format_exc_info,
```

Positioned last in the shared list so it feeds all three consumers of that list
— the structlog config, `ProcessorFormatter(processors=...)`, and
`foreign_pre_chain` — which means one insertion fixes BOTH environments and
both structlog-origin and stdlib-origin records.

## FILES

- `blm_v2/telemetry/logging.py` — import + one processor entry
- `tests/test_logging_traceback.py` — new, 5 tests

## EVIDENCE LADDER

- L1 CODE — done
- L2 UNIT TEST — done. RED observed first: **4 failed** against the unmodified
  pipeline, with the defect payload visible in the assertion output
  (`{"exc_info": true, "event": "scorecard_run_failed", ...}`). GREEN after the
  change: **5 passed**. Mutation-proven: commenting out the `format_exc_info`
  entry took the suite back to **4 failed**, then restored to green.
- L3 FULL SUITE — done. **1173 passed, 5 failed**, hash-bracketed (the bracket
  exists because `tests/test_settle_worker.py` calls
  `inspect.getsource(Scorecard.run)`, which reads from disk — an edit during a
  run produces phantom failures, which happened earlier this session).
  Passed count moved 1168 → 1173, i.e. exactly the 5 new tests.
- L4 DEPLOYED — **NO**. `blm-server` (PID 1442) still executes pre-change code.
- L5 LIVE VERIFICATION — **PENDING DEPLOY**. See below.

## THE 5 PRE-EXISTING FAILURES (not from this milestone)

Reproduced identically on a pristine `ddf4c87` worktree with none of these
changes present, so they are baseline:

```
tests/test_forensic_relative_pace_freeze.py::test_primary_condition_under_rate_frozen
tests/test_forensic_relative_pace_freeze.py::test_primary_direction_holds_in_every_competition
tests/test_forensic_relative_pace_freeze.py::test_same_game_exclusion_is_not_load_bearing_for_the_result
tests/test_prospective_freeze.py::test_report_cli_writes_json
tests/test_resulted_alerts_review.py::test_ordinary_polling_cannot_rewrite_a_resulted_row
```

They are frozen-literal assertions drifted by DB growth.

## HARNESS PITFALLS FOUND WHILE WRITING THE TESTS

Both cost a cycle; both are recorded so the next test author does not repeat them.

1. **`capsys` returns an empty string for this pipeline.** `logging.StreamHandler`
   binds its stream at CONSTRUCTION time, and pytest swaps `sys.stdout` per test
   phase — so the stream the handler captured is not the one `capsys` reports.
   The tests install their own `StringIO` as `sys.stdout` *before* calling
   `setup_logging()`. Symptom if you forget: a test asserting
   `'"exc_info": true' not in out` PASSES against the broken pipeline, because
   `''` contains nothing — a green test proving nothing.
2. **Do not assert raw substrings of serialized JSON.** The traceback's `File "`
   appears in the output as `File \"` (escaped), so a substring assertion fails
   against working code. Parse the emitted line with `json.loads` and assert on
   the `exception` field.

## KNOWN LIMITATION — L5 CANNOT BE SELF-TRIGGERED

Live verification needs an actual exception in the running service. There is no
safe synthetic way to inject one into the deployed scorecard loop, and doing so
would mean editing production code — so L5 is owed to the **next real failure**
or a deliberate fault-injection drill. What CAN be verified after deploy: that
the running service is serving the new module (hash comparison), not that a
traceback renders in situ.

## COMMIT

Pending user approval. RED evidence is recorded above from this session's runs.

## NEXT MILESTONE

**M002 — reproduce the 17:46 failure**, now that it is diagnosable.
Harness: `/tmp/hermes-verify-m002-repro.py` — a FULL UNGATED `Scorecard.run()`
(`BLM_SCORECARD_INCREMENTAL=0`) against a fresh copy of production, with
`format_exc_info` in the pipeline, logging via `server.py:193` verbatim.

If it reproduces: the traceback names the cause, and the fix is scoped then.
If it completes clean: that RULES OUT a data-shape bug in the full sweep and
points at the environment — specifically the timeout asymmetry between
`blm_v4/storage.py:181` (`timeout=90`, with a comment stating 30s was shorter
than a scorecard section window and caused "database is locked") and
`blm_v4/scorecard.py:1110` (`timeout=30`, unchanged). An isolated copy has no
second writer, so by construction it cannot reproduce cross-process contention.

A negative result here is a real result, not a failed attempt.
