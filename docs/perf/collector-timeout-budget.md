# Collector Timeout & Tick Budget Policy

> **Phase 5** — 2026-09-27

Every Playwright page operation in the fast collection path is now bounded
by a centralized timeout budget.  The tick itself has a soft target and a
hard ceiling.  This document records the policy so the architecture is
auditable without reading the implementation.

## Constants (single source of truth: `blm_v4/collector.py`)

All values are class-level constants on `PokerBetCollector`.  No other file
carries a magic number for these operations.

| task                       | constant                        | value | purpose |
|----------------------------|---------------------------------|-------|---------|
| page.content() (tick)      | `PAGE_CONTENT_TICK_TIMEOUT_S`   | 3 s   | single fast-path round-trip; the tick target is 10 s but the page operation must leave room for parse + persist + heartbeat |
| page.content() (retry)     | `PAGE_CONTENT_RETRY_TIMEOUT_S`  | 5 s   | second attempt after a failed parse; more generous because the browser may be recovering from a thin parse |
| recovery (fresh context)   | `RECOVERY_TIMEOUT_S`            | 5 s   | the recovery path replaces the page; must complete well inside the tick ceiling so the next cycle is not pushed |
| retry page.content() after recovery | `PAGE_CONTENT_TICK_TIMEOUT_S` (same as first) | 3 s | same deadline as the first tick attempt |
| page-capture budget        | `PAGE_CAPTURE_BUDGET_S`         | 12 s  | total wall-clock spent inside `page.content()` calls per tick (tick + retry); exhausts → fresh context to prevent a wedged browser from consuming the whole tick |
| tick target (soft)         | `TICK_TARGET_S`                 | 10 s  | the collector aims to complete each fast cycle in this window (relaxed from the original 5 s hard requirement as Phase 5 optimization matures) |
| tick hard ceiling          | `TICK_HARD_CEILING_S`           | 30 s  | a tick that has not completed within this wall-clock window is forcibly terminated and the page is recycled; prevents a single stuck operation from stalling the pipeline for minutes |

## Rationale

### Why 3 s for page.content() (tick)?

The tick target is 10 s.  A single `page.content()` round-trip must leave
room for:

- competition parsing (regex over the HTML)
- list snapshot persistence (SQLite writes)
- WS subscription maintenance
- heartbeat emission

In a healthy tick `page.content()` completes in under 1 s.  The 3 s
deadline catches wedged browsers while leaving budget for the rest of the
pipeline.

### Why 5 s for the retry?

A failed parse typically means the browser was mid-hydration or the SPA
served a thin page.  The retry happens after `_ensure_discovery_page`, so
the browser has had a moment to recover.  A more generous deadline avoids
failing a recoverable situation.

### Why 12 s page-capture budget?

Two `page.content()` calls per tick: 3 s (tick) + 5 s (retry) = 8 s of
potential timeout exposure.  The 12 s budget gives 4 s of headroom for the
calls to complete normally (most finish in under 1 s each), while still
capping total time spent inside Playwright.  When the budget is exhausted
the collector forces a fresh context rather than letting a wedged browser
consume the entire tick.

### Why 10 s tick target?

The original 5 s hard requirement (2026-09-10) was appropriate when the
tick body was minimal.  Phase 5 work — page-content timeouts, recovery
paths, budget tracking — adds overhead.  A 10 s soft target reflects the
current collector load while the hard 30 s ceiling protects against the
worst case (a stuck browser that the timeout wrapper cannot interrupt).

### Why 30 s hard ceiling?

The worst observed failure mode (2026-09-26 00:57→01:03) was a 6.8-minute
gap between fast ticks during browser rotation — the process was "alive"
but producing no observations.  The 30 s ceiling is two full missed
5 s cycles plus margin, far below the minutes-long wedges, and below
typical systemd `WatchdogSec` values.

## Tick budget flow

```
tick start (monotonic)
  │
  ├─ page_capture_budget = 12.0 s
  ├─ tick_deadline = now + 30.0 s
  │
  ├─ 1. page.content() (tick)  timeout=3.0s  budget=12.0s
  │     ├─ success → budget -= spend   (typically < 1 s)
  │     └─ timeout → fresh context + return
  │
  ├─ parse competitions
  │   └─ empty? → refresh page + retry
  │
  ├─ 2. page.content() (retry) timeout=min(budget, 5.0s)  budget=remaining
  │     ├─ success → budget -= spend
  │     ├─ timeout → fresh context + return
  │     └─ budget exhausted → fresh context + return
  │
  ├─ parse competitions (retry)
  │
  ├─ persist snapshots, WS subs, mark ended, deviation flush, state write
  │
  └─ tick end → log: "done in X s · Y s page-capture budget remaining"
```

## Recovery semantics

When `page.content()` times out or the page-capture budget is exhausted:

1. the fast path requests a fresh context (`_fresh_context`)
2. the fresh-context path has its own 5 s deadline (`RECOVERY_TIMEOUT_S`)
3. after recovery, the next tick's first `page.content()` uses the standard
   3 s deadline (`PAGE_CONTENT_TICK_TIMEOUT_S`)

The retry path after a failed parse follows the same pattern: refresh the
page, then retry with the retry timeout (5 s) bounded by the remaining
page-capture budget.

## Instrumentation

Every tick log line now includes the remaining page-capture budget when
positive:

```
fast tick 42 done in 2.34s: tracked=12 snapshots=12 errors=0 — page-capture budget remaining: 11.823s
```

This makes budget exhaustion visible in the journal without adding a new
metric endpoint.  Budget exhaustion is always a warning-level event that
triggers a fresh context.

## Testing

- `tests/test_v1_collector_staleness.py` — existing staleness tests cover
  the collector's page recovery paths; the timeout constants are
  accessible as class attributes for test assertion.
- New tests (to be added): `tests/test_collector_timeout_budget.py` —
  asserts the constants have the expected values, verifies
  `_page_content_timed` accepts an override timeout, and simulates a
  budget-exhausted tick returning a fresh context.
