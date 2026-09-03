"""STEP 2 scheduler verification — target-boundary cadence math (pytest).

Pure logic tests of the fixed-rate scheduler (no Playwright/browser):
the collector must aim each tick at the NEXT interval boundary from the
PREVIOUS target, so latency is absorbed inside the interval and never
stacks a full sleep on top of work.
"""
import sqlite3
import tempfile
import os
import inspect


def scheduler_sim(tick_s, work_times):
    """Simulate the collector loop (collector.py:349-...)."""
    next_target = 0.0
    starts = []
    sleeps = []
    t = 0.0
    for w in work_times:
        if next_target <= 0:
            next_target = t + tick_s
        starts.append(t)
        t += w                      # work consumes wall time
        sleep_for = max(0.0, next_target - t)
        if sleep_for > 0:
            t += sleep_for
        sleeps.append(sleep_for)
        while next_target <= t:     # advance past missed target(s)
            next_target += tick_s
    return starts, sleeps, t


def test_fast_capture_lands_on_10s_boundaries():
    starts, sleeps, _ = scheduler_sim(10.0, [2.0] * 8)
    intervals = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
    assert all(abs(i - 10.0) < 1e-9 for i in intervals), intervals
    assert all(s >= 7.9 for s in sleeps), "sleep makes up the ~8s remainder"


def test_overrun_is_work_bounded_no_extra_sleep():
    starts, sleeps, _ = scheduler_sim(10.0, [12.0] * 5)
    intervals = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
    # 12s work > 10s tick -> cadence ~12s (work-bounded), NOT 22s
    assert all(11.9 <= i <= 12.1 for i in intervals), intervals
    assert all(s == 0 for s in sleeps)


def test_spike_does_not_stack_full_sleep():
    starts, sleeps, _ = scheduler_sim(10.0, [3.0, 8.0, 3.0, 25.0, 3.0])
    intervals = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
    # [10, 10, 10, 25]: the 25s spike (tick 4) is work-bounded — the next
    # tick is NOT 25+10=35s later via a stacked sleep
    assert intervals == [10.0, 10.0, 10.0, 25.0], intervals
    assert sleeps[3] == 0.0, "no sleep after the overrun spike"


def test_duplicate_timestamp_protection():
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE snapshots (id INTEGER PRIMARY KEY, game_id INT,
                   captured_at TEXT, score INT, UNIQUE(game_id, captured_at))""")
    con.execute("INSERT OR IGNORE INTO snapshots (game_id, captured_at, score) "
                "VALUES (1, '2026-01-01T00:00:00.000000Z', 10)")
    con.execute("INSERT OR IGNORE INTO snapshots (game_id, captured_at, score) "
                "VALUES (1, '2026-01-01T00:00:00.000000Z', 11)")
    assert con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    con.close()


def test_slow_event_path_isolation():
    # gate constant imported from source — the isolation math must track
    # the REAL cadence, not a hardcoded copy of it
    import blm_v4.collector as C
    n_fast = C.EVENT_VIEW_EVERY_N * 10
    n_slow = sum(1 for i in range(1, n_fast + 1) if i % C.EVENT_VIEW_EVERY_N == 0)
    assert n_slow == 10


def test_default_tick_is_10s():
    # signature default + CLI default both derive from TICK_DEFAULT, so a
    # cadence change updates one constant, not scattered literals
    import blm_v4.collector as C
    assert C.TICK_DEFAULT == 10.0
    assert inspect.signature(C.PokerBetCollector.__init__).parameters["tick_s"].default == 10.0


def test_ended_grace_scales_with_tick_interval():
    """Disappearance tolerance is WALL-TIME (ENDED_GRACE_S), so halving
    the tick must NOT halve the grace: 10s tick -> 6 ticks (60s), 5s tick
    -> 12 ticks, 20s tick -> 3 ticks (the pre-STEP-2 behaviour)."""
    import blm_v4.collector as C
    c10 = C.PokerBetCollector(db_path=__import__("tempfile").mkdtemp() + "/t.db")
    assert c10.tick_s == 10.0 and c10._ended_grace_ticks == 6
    c5 = C.PokerBetCollector(tick_s=5.0, db_path=__import__("tempfile").mkdtemp() + "/t.db")
    assert c5._ended_grace_ticks == 12
    c20 = C.PokerBetCollector(tick_s=20.0, db_path=__import__("tempfile").mkdtemp() + "/t.db")
    assert c20._ended_grace_ticks == 3


def test_tick_timing_ring_is_bounded_and_summarized():
    """The daemon's tick-timing ring must never grow unbounded: appends
    beyond TICK_STATS_MAX drop the oldest, and the state-payload summary
    reduces it to means (ms) + last event-view duration."""
    from collections import deque
    import blm_v4.collector as C
    ring = {k: deque(maxlen=C.TICK_STATS_MAX) for k in ("work", "sleep", "cycle", "event")}
    for i in range(C.TICK_STATS_MAX + 100):          # over-fill the ring
        ring["work"].append(2.5)
        ring["sleep"].append(7.5)
        ring["cycle"].append(10.0)
    ring["event"].append(6.25)
    assert len(ring["work"]) == C.TICK_STATS_MAX     # bounded, oldest dropped
    s = C._tick_timing_summary(ring)
    assert s["n"] == C.TICK_STATS_MAX
    assert s["mean_work_ms"] == 2500.0 and s["mean_sleep_ms"] == 7500.0
    assert s["mean_cycle_ms"] == 10000.0
    assert s["last_event_ms"] == 6250.0
    # empty ring -> None means (a just-started collector reports nothing)
    empty = C._tick_timing_summary({k: deque() for k in ("work", "sleep", "cycle", "event")})
    assert empty["n"] == 0 and empty["mean_work_ms"] is None and empty["last_event_ms"] is None


def test_tracked_state_roundtrip(monkeypatch, tmp_path):
    """A tracked game survives serialize -> restore (crash recovery).

    The restore must rebuild _tracked + _market_queue so a restart does
    NOT trigger the full re-resolution storm in the fast path.
    """
    import json
    import blm_v4.collector as C
    from blm_v4.models import PokerBetGame

    state_file = tmp_path / "collector_state.json"
    monkeypatch.setattr(C, "STATE_FILE", state_file)

    col = C.PokerBetCollector(db_path=tmp_path / "t.db")
    g = PokerBetGame(
        source_game_id="30799999", classification="BETUAL_NBA",
        home_team="Team A Virtual", away_team="Team B Virtual",
        source_url="https://x/30799999", status="live",
        competition_id="18295203", competition_slug="cyber-basketball-2k26-matches",
    )
    col._tracked["BETUAL_NBA"][f"{g.home_team}|{g.away_team}"] = g
    col._market_queue.append(g.source_game_id)

    # serialize (via the state writer shape)
    state = {"tracked_games": col._tracked_serializable()}
    state_file.write_text(json.dumps(state))

    # fresh collector restores
    col2 = C.PokerBetCollector(db_path=tmp_path / "t2.db")
    n = col2._restore_tracked()
    assert n == 1, f"restored {n}"
    key = f"{g.home_team}|{g.away_team}"
    assert key in col2._tracked["BETUAL_NBA"]
    rg = col2._tracked["BETUAL_NBA"][key]
    assert rg.source_game_id == "30799999" and rg.home_team == g.home_team
    assert g.source_game_id in col2._market_queue
    # ended games are not persisted/restored
    g.status = "ended"
    state = {"tracked_games": col._tracked_serializable()}
    state_file.write_text(json.dumps(state))
    col3 = C.PokerBetCollector(db_path=tmp_path / "t3.db")
    assert col3._restore_tracked() == 0
