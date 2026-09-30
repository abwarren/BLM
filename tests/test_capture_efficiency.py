"""Capture-efficiency directive 2026-09-30 — regression tests.

Directive: "FIX CAPTURE EFFICIENCY, DO NOT ALTER ALERT LOGIC".
TEST ENVIRONMENT ONLY.  These tests pin the collector-side behavior
added under the directive:

  PHASE 2 — per-tick timing buckets (tick_total / game_discovery /
            page_content / score_parse / board_parse / line_parse /
            db_write / retry / ended_game) exposed with P50/P95/P99/MAX
            percentiles in _tick_timing_summary.
  PHASE 3 — explicit lifecycle LIVE -> FINALIZING -> DONE: the
            WS -> snapshot bridge must NOT persist observations for a
            DONE game (ended or left tracking), while a FINALIZING game
            (final-capture window open) is still accepted.
  PHASE 4 — bounded NULL-score recovery: at most one re-read per game
            per tick, at most NULL_SCORE_RECOVER_MAX_PER_TICK games per
            tick, budget-aware, backoff after repeated failure; a
            problematic game can never monopolize the tick.
  PHASE 5 — checkpoint-aware market-queue ordering (never-line first,
            then tier 1 checkpoint-critical before tiers 2-6), WITHOUT
            changing any checkpoint definition.
  PHASE 6 — _page_content_timed must keep Playwright same-thread
            semantics: runs page.content() on the CALLING thread, arms
            SIGALRM there, never spawns a worker thread that would
            cross the greenlet thread-affinity boundary.

No alert mathematics, REQUIRED_MARGIN, checkpoint semantics, or
fingerprint definitions are touched by anything under test here.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from blm_v4 import collector as colmod
from blm_v4.classifications import Classification
from blm_v4.collector import PokerBetCollector, _tick_timing_summary
from blm_v4.discovery import RowGame
from blm_v4.models import PokerBetGame


CYBER = Classification.CYBER_2K26


def _make_collector() -> PokerBetCollector:
    """Minimal collector with every external I/O mocked out."""
    with patch("blm_v4.collector.PokerBetStore"), \
         patch("blm_v4.collector.CleanMetricsStore"), \
         patch("blm_v4.collector.DeviationEngine"), \
         patch("blm_v4.collector.load_comp_ids", return_value={}):
        c = PokerBetCollector.__new__(PokerBetCollector)
    # Attributes the new code paths touch (mirror __init__ minimally).
    c._tick_stats = {
        key: deque(maxlen=500)
        for key in ("work", "sleep", "cycle", "event", "sub",
                    "fast_cycle_interval_ms", "fast_work_ms",
                    "slow_event_view_ms", "ws_subscription_ms",
                    "persistence_ms", "tick_total_ms", "game_discovery_ms",
                    "page_content_ms", "score_parse_ms", "board_parse_ms",
                    "line_parse_ms", "db_write_ms", "retry_ms",
                    "ended_game_ms")
    }
    c._end_state_terminal = {}
    c._null_score_streak = {}
    c._null_score_backoff_until = {}
    c._null_incomplete_marked = set()
    c._null_recovery_stats = {
        "attempts": 0, "recovered": 0, "incomplete_marked": 0,
        "backoff_skips": 0, "budget_skips": 0,
    }
    c._ws_lifecycle_suppressed = 0
    c._final_capture_gids = set()
    c._final_capture_armed = set()
    c._final_capture_until = {}
    c._instances = {}
    c._track_lock = threading.RLock()
    c._tracked = {CYBER.value: {}}
    c._market_queue = []
    c._tick_no = 100
    c.store = MagicMock()
    c.clean_metrics = None
    return c


def _game(gid: str = "31060001", status: str = "live") -> PokerBetGame:
    return PokerBetGame(
        source_game_id=gid,
        home_team="Alpha Cyber", away_team="Beta Cyber",
        classification=CYBER.value, status=status,
    )


def _row(home=None, away=None, name_h="Alpha Cyber", name_a="Beta Cyber"):
    return RowGame(home_team=name_h, away_team=name_a,
                   home_score=home, away_score=away)


# ══════════════════════════════════════════════════════════════════════
# PHASE 2 — per-tick timing buckets + percentiles
# ══════════════════════════════════════════════════════════════════════

PHASE2_KEYS = (
    "tick_total_ms", "game_discovery_ms", "page_content_ms",
    "score_parse_ms", "board_parse_ms", "line_parse_ms", "db_write_ms",
    "retry_ms", "ended_game_ms",
)


def test_phase2_all_buckets_in_tick_stats_init():
    """__init__ must declare every directive bucket (no KeyError later)."""
    import inspect
    src = inspect.getsource(colmod.PokerBetCollector.__init__)
    for key in PHASE2_KEYS:
        assert f'"{key}"' in src, f"missing timing bucket {key}"


def test_phase2_summary_has_percentiles_for_every_bucket():
    stats = {k: deque(maxlen=100) for k in PHASE2_KEYS}
    stats["tick_total_ms"].extend([0.5, 1.0, 2.0, 4.0, 8.0])
    out = _tick_timing_summary(stats)
    for key in PHASE2_KEYS:
        assert key in out, f"_tick_timing_summary missing {key}"
        assert set(out[key].keys()) == {"p50", "p95", "p99", "max"}
    # nearest-rank percentiles in ms
    assert out["tick_total_ms"]["p50"] == 2000.0
    assert out["tick_total_ms"]["max"] == 8000.0


def test_phase2_summary_empty_bucket_reports_none():
    out = _tick_timing_summary({k: deque() for k in PHASE2_KEYS})
    for key in PHASE2_KEYS:
        assert out[key] == {"p50": None, "p95": None, "p99": None,
                            "max": None}


def test_phase2_tick_body_records_buckets():
    """A driven _tick_body fills the core buckets without raising."""
    c = _make_collector()
    c.stats = {"ticks": 0, "games_seen": 0, "snapshots": 0, "errors": 0}
    c._empty_ticks = 0
    c._tick_completed_fully = False
    c._last_fast_completed_at = 0.0
    c._fast_cycle_completed_at_iso = ""
    c._tick_liveness_only = False
    c._unseen_ticks = {CYBER.value: {}}
    c._pending_resolve = {}
    c._last_slow_request_at = 0.0
    c._slow_job_pending = False
    c._slow_wake = threading.Event()
    c._page_content_timed = MagicMock(return_value="<html></html>")
    comp = MagicMock()
    comp.classification = CYBER
    comp.games = [_row(50, 48)]
    import blm_v4.collector as cm
    with patch.object(cm, "find_relevant_competitions",
                      return_value=[comp]):
        c._deviation_dirty = set()
        c._deviation = None
        c._write_state = lambda **kw: None
        c._sync_market_subscriptions = lambda page: None
        c._mark_ended = lambda seen: None
        c._prune_pending_resolve = lambda seen: None
        c._store_list_snapshot = MagicMock()
        c._tracked[CYBER.value]["Alpha Cyber|Beta Cyber"] = _game()
        fake_page = MagicMock()
        fake_page.url = "https://x/"
        c._tick_body(fake_page)
    for key in ("tick_total_ms", "game_discovery_ms", "page_content_ms",
                "board_parse_ms", "ended_game_ms"):
        assert len(c._tick_stats[key]) >= 1, f"bucket {key} never recorded"


# ══════════════════════════════════════════════════════════════════════
# PHASE 3 — lifecycle guard on the WS -> snapshot bridge
# ══════════════════════════════════════════════════════════════════════

def _wire_ws_bridge(c, game):
    """Give _ingest_ws_observation the minimum collaborative state."""
    c._ws_market_last = {}
    c._ws_snap_last = {}
    c._last_market_at = {}
    c._find_tracked = lambda gid: game if gid == game.source_game_id else None
    c._game_db_id = lambda g: 1


def _ws_obs(gid, line=205.5):
    return {
        "source_game_id": gid,
        "captured_at": "2026-09-30T12:00:00Z",
        "line_value": line,
        "home_score": 50, "away_score": 48,
        "period_label": "2nd Quarter", "clock": "05:00",
        "game_id": 1,
    }


def test_phase3_ws_bridge_suppresses_ended_game():
    """ENDED (DONE) game: WS frames must NOT persist any observation."""
    c = _make_collector()
    g = _game(status="ended")
    c._tracked[CYBER.value]["Alpha Cyber|Beta Cyber"] = g
    _wire_ws_bridge(c, g)
    with patch.object(c.store, "upsert_market_observation") as up, \
         patch.object(c.store, "insert_snapshot") as ins:
        c._ingest_ws_observation(_ws_obs(g.source_game_id))
    up.assert_not_called()
    ins.assert_not_called()
    assert c._ws_lifecycle_suppressed == 1


def test_phase3_ws_bridge_suppresses_removed_from_tracking():
    """A gid recorded terminal by the disappeared path is suppressed too."""
    c = _make_collector()
    g = _game(status="live")
    c._tracked[CYBER.value]["Alpha Cyber|Beta Cyber"] = g
    c._end_state_terminal[g.source_game_id] = "ended"
    _wire_ws_bridge(c, g)
    with patch.object(c.store, "upsert_market_observation") as up, \
         patch.object(c.store, "insert_snapshot") as ins:
        c._ingest_ws_observation(_ws_obs(g.source_game_id))
    up.assert_not_called()
    ins.assert_not_called()


def test_phase3_ws_bridge_accepts_finalizing_game():
    """FINALIZING (final-capture window open) frames are still accepted."""
    c = _make_collector()
    g = _game(status="live")
    c._tracked[CYBER.value]["Alpha Cyber|Beta Cyber"] = g
    c._final_capture_gids.add(g.source_game_id)   # FINALIZING
    _wire_ws_bridge(c, g)
    c.store.insert_snapshot.return_value = 123
    with patch.object(c.store, "upsert_market_observation") as up, \
         patch.object(c.store, "insert_snapshot") as ins:
        c._ingest_ws_observation(_ws_obs(g.source_game_id))
    up.assert_called_once()
    ins.assert_called_once()
    assert c._ws_lifecycle_suppressed == 0


def test_phase3_end_game_records_terminal():
    """_end_game must register the gid terminal (lifecycle DONE)."""
    c = _make_collector()
    g = _game(status="live")
    c._end_state_terminal = {}
    c.stats = {"games_ended_final": 0}
    c._unseen_ticks = {CYBER.value: {}}
    c.betual = MagicMock()
    c._betual_end_game = lambda game: None
    c.store.get_snapshots.return_value = [{}]
    c._finalize_clean = lambda game, obs=None: None
    c._end_game(g)
    assert c._end_state_terminal[g.source_game_id] == "ended"


def test_phase3_mark_ended_records_terminal():
    """The disappeared path registers the gid terminal as well."""
    c = _make_collector()
    g = _game(status="live")
    key = f"{g.home_team}|{g.away_team}"
    c._tracked[CYBER.value][key] = g
    c._unseen_ticks = {CYBER.value: {key: 10**6}}
    c._ended_grace_ticks = 3
    c._final_capture_grace_s = 0.0
    c._tail_provable_final = lambda gid: False
    c.betual = MagicMock()
    c._betual_end_game = lambda game: None
    c._finalize_clean = lambda game, obs=None: None
    seen = {CYBER.value: set()}          # game absent -> grace expiry
    c._mark_ended(seen)
    assert key not in c._tracked[CYBER.value]
    assert c._end_state_terminal[g.source_game_id] == "ended"


# ══════════════════════════════════════════════════════════════════════
# PHASE 4 — bounded NULL-score recovery
# ══════════════════════════════════════════════════════════════════════

class _Comp:
    def __init__(self, games):
        self.classification = CYBER
        self.games = games


def test_phase4_recovers_scores_from_reread():
    c = _make_collector()
    g = _game()
    snap_list = [(g, _row(None, None), CYBER)]
    c._page_content_timed = MagicMock(return_value="<html>ok</html>")
    with patch.object(colmod, "find_relevant_competitions",
                      return_value=[_Comp([_row(40, 38)])]):
        out, budget = c._recover_null_scores(snap_list, MagicMock(), 6.0)
    assert out[0][1].home_score == 40 and out[0][1].away_score == 38
    assert budget < 6.0                       # re-read consumed budget
    assert c._null_recovery_stats["recovered"] == 1


def test_phase4_budget_zero_never_rereads():
    c = _make_collector()
    g = _game()
    snap_list = [(g, _row(None, None), CYBER)]
    c._page_content_timed = MagicMock(
        side_effect=AssertionError("must not re-read on empty budget"))
    out, budget = c._recover_null_scores(snap_list, MagicMock(), 0.0)
    assert out[0][1].home_score is None
    assert c._null_recovery_stats["budget_skips"] == 1


def test_phase4_at_most_max_per_tick_games():
    c = _make_collector()
    games = [(_game(f"3107000{i}"), _row(None, None), CYBER)
             for i in range(5)]
    c._page_content_timed = MagicMock(return_value="<html>x</html>")
    with patch.object(colmod, "find_relevant_competitions",
                      return_value=[]):          # never recovers
        c._recover_null_scores(list(games), MagicMock(), 60.0)
    assert c._null_recovery_stats["attempts"] == \
        c.NULL_SCORE_RECOVER_MAX_PER_TICK   # hard per-tick cap


def test_phase4_incomplete_marked_then_backoff():
    c = _make_collector()
    g = _game("31070099")
    snap_list = [(g, _row(None, None), CYBER)]
    c._page_content_timed = MagicMock(return_value="<html>x</html>")
    with patch.object(colmod, "find_relevant_competitions",
                      return_value=[]):
        for _ in range(c.NULL_SCORE_INCOMPLETE_AFTER):
            c._recover_null_scores(list(snap_list), MagicMock(), 60.0)
    assert c._null_recovery_stats["incomplete_marked"] == 1
    assert g.source_game_id in c._null_score_backoff_until
    # Backoff window: re-reads skipped entirely.
    before = c._null_recovery_stats["attempts"]
    c._recover_null_scores(list(snap_list), MagicMock(), 60.0)
    assert c._null_recovery_stats["attempts"] == before
    assert c._null_recovery_stats["backoff_skips"] == 1


def test_phase4_recovery_clears_streak_and_backoff():
    c = _make_collector()
    g = _game("31070042")
    c._null_score_streak[g.source_game_id] = 2
    c._null_score_backoff_until[g.source_game_id] = 10**9
    c._null_incomplete_marked.add(g.source_game_id)
    c._tick_no = 5
    c._null_score_backoff_until[g.source_game_id] = 4   # window expired
    snap_list = [(g, _row(None, None), CYBER)]
    c._page_content_timed = MagicMock(return_value="<html>y</html>")
    with patch.object(colmod, "find_relevant_competitions",
                      return_value=[_Comp([_row(10, 9)])]):
        out, _b = c._recover_null_scores(snap_list, MagicMock(), 30.0)
    assert out[0][1].home_score == 10
    assert g.source_game_id not in c._null_score_streak
    assert g.source_game_id not in c._null_incomplete_marked


def test_phase4_dom_failure_returns_list_unchanged():
    c = _make_collector()
    g = _game()
    snap_list = [(g, _row(None, None), CYBER)]
    c._page_content_timed = MagicMock(return_value=None)   # timed out
    out, budget = c._recover_null_scores(snap_list, MagicMock(), 6.0)
    assert out is snap_list and out[0][1].home_score is None


# ══════════════════════════════════════════════════════════════════════
# PHASE 5 — checkpoint-aware scheduling
# ══════════════════════════════════════════════════════════════════════

def _proj_row(progress, age_s=5.0):
    cap = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return (cap.isoformat().replace("+00:00", "Z"), progress)


def test_phase5_tiers():
    c = _make_collector()
    g = _game("31080001")
    c._tracked[CYBER.value]["Alpha Cyber|Beta Cyber"] = g
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = None
    c.clean_metrics = MagicMock()
    c.clean_metrics.db = db
    # no clean state -> normal live (3)
    assert c._progress_tier(g.source_game_id)[0] == 3
    # fresh projection at 70% (75 boundary 5 game-min away at 48min) -> 2
    db.execute.return_value.fetchone.return_value = _proj_row(70.0)
    assert c._progress_tier(g.source_game_id)[0] == 2
    # fresh projection 1 game-minute before the 75 boundary -> 1
    db.execute.return_value.fetchone.return_value = _proj_row(72.9)
    assert c._progress_tier(g.source_game_id)[0] == 1
    # stale projection -> 4
    db.execute.return_value.fetchone.return_value = _proj_row(
        72.9, age_s=c.PROGRESS_STALENESS_S + 30)
    assert c._progress_tier(g.source_game_id)[0] == 4
    # finalizing -> 5 regardless of progress
    c._final_capture_gids.add(g.source_game_id)
    assert c._progress_tier(g.source_game_id)[0] == 5
    c._final_capture_gids.discard(g.source_game_id)
    # ended -> 6
    g.status = "ended"
    assert c._progress_tier(g.source_game_id)[0] == 6


def test_phase5_sort_order_never_line_then_tiers():
    c = _make_collector()
    gids = {"never": "31080010", "crit": "31080011",
            "stale": "31080012", "normal": "31080013"}
    db = MagicMock()
    rows = {
        gids["crit"]: _proj_row(72.5),           # tier 1
        gids["stale"]: _proj_row(72.5, age_s=999),  # tier 4
        gids["normal"]: None,                    # tier 3
    }
    db.execute.return_value.fetchone.side_effect = \
        lambda: None  # replaced below via function
    c.clean_metrics = MagicMock()
    c.clean_metrics.db = db

    def _fake_find_tracked(gid):
        return _game(gid)

    c._find_tracked = _fake_find_tracked

    def _tier_stub(gid):
        if gid == gids["crit"]:
            return (1, 75.0)
        if gid == gids["stale"]:
            return (4, 0.0)
        return (3, 0.0)

    c._progress_tier = _tier_stub
    c._market_queue = [gids["normal"], gids["stale"], gids["crit"]]
    c._sort_market_queue_checkpoint_aware(never_line={gids["never"]})
    # never-line prepended first even though it was not in the queue,
    # tier 1 next, then 3, then 4
    assert c._market_queue[0] == gids["crit"]
    assert c._market_queue[-1] == gids["stale"]
    assert gids["normal"] in c._market_queue


def test_phase5_checkpoint_critical_exempt_from_freshness_gate():
    """Tier-1 games skip the fresh-observation gate (source-level pin)."""
    import inspect
    src = inspect.getsource(
        PokerBetCollector._capture_slow_market)
    assert "_progress_tier(gid)[0] == 1" in src
    assert "checkpoint-critical" in src


def test_phase5_no_checkpoint_definition_changed():
    """The directive forbids touching checkpoint semantics — pin them."""
    from blm_v4.live_analytics import under_alert
    assert under_alert.ALERT_PROGRESS_PCT == 75.0
    from blm_v4.projection import duration_for
    assert duration_for("BETUAL_NBA") == (10.0, 40.0)
    assert duration_for("CYBER_2K26") == (12.0, 48.0)


# ══════════════════════════════════════════════════════════════════════
# PHASE 6 — Playwright same-thread / thread-affinity preservation
# ══════════════════════════════════════════════════════════════════════

def test_phase6_page_content_timed_runs_on_calling_thread():
    """page.content() must execute on the CALLING thread (the thread that
    owns the sync_playwright greenlet).  A worker-thread implementation
    raises greenlet.error — the historical regression this pins."""
    c = _make_collector()
    calling_thread = threading.current_thread().ident
    seen_thread = []

    class _FakePage:
        url = "https://x/"

        def content(self):
            seen_thread.append(threading.current_thread().ident)
            return "<html></html>"

    out = c._page_content_timed(_FakePage(), "test", timeout_s=0.2)
    assert out == "<html></html>"
    assert seen_thread == [calling_thread]


def test_phase6_page_content_timed_no_worker_threads_spawned():
    c = _make_collector()
    before = threading.active_count()

    class _FakePage:
        url = "https://x/"

        def content(self):
            time.sleep(0.01)
            return "<html></html>"

    for _ in range(5):
        assert c._page_content_timed(
            _FakePage(), "test", timeout_s=0.5) == "<html></html>"
    assert threading.active_count() == before


def test_phase6_overrun_flags_but_keeps_capture():
    """The SIGALRM handler FLAGS an overrun; it must never RAISE (nested
    WS callbacks on this thread write to SQLite — a raise used to abort a
    mid-transaction commit)."""
    c = _make_collector()
    held = []

    class _FakePage:
        url = "https://x/"

        def content(self):
            # simulate the alarm firing mid-content()
            import signal as _signal
            _signal.raise_signal(_signal.SIGALRM)
            held.append("kept")
            return "<html>late</html>"

    out = c._page_content_timed(_FakePage(), "test", timeout_s=10.0)
    assert out == "<html>late</html>"      # capture kept, no exception
    assert held == ["kept"]
    assert c._capture_overran is True


def test_phase6_timeout_uses_signal_not_thread_join():
    """The deadline mechanism is SIGALRM on the calling thread; source
    must not contain the historical cross-thread implementation."""
    import inspect
    src = inspect.getsource(PokerBetCollector._page_content_timed)
    assert "threading" not in src, \
        "deadline must stay on the calling thread (greenlet affinity)"
    assert "setitimer" in src
