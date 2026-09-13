"""Collector crash-recovery regressions (2026-09-12 incident).

Incident: after a reboot whose DHCP returned no usable DNS, 10 consecutive
event-view failures took the slow worker into its "rotate browser" branch.
That branch called the FAST-path ``_relaunch()`` from the WORKER thread.
Playwright's sync greenlet belongs to the main thread, so the relaunch died
with ``greenlet.error: Cannot switch to a different thread`` — after having
already closed the fast browser and cleared the fast page.

The next fast tick then hit ``page.content()`` -> ``TargetClosedError``.  The
handler that was supposed to relaunch the fast session was written as
``except TargetClosedError:`` while logging ``exc``, so it raised
``UnboundLocalError`` from inside the handler.  Sibling handlers cannot catch
a sibling handler's error, so it reached the generic ``except Exception`` at
the bottom of ``start()``, was logged as "collector crashed" and swallowed:
``start()`` returned, the process exited 0, and ``Restart=on-failure`` never
fired.  The collector stayed dead from 12:43:13 until it was restarted by
hand.

These tests pin the two code-level fixes:

1. the slow worker's failure-storm branch rotates the WORKER'S OWN browser
   and never calls the fast-path relaunch;
2. the fast loop's TargetClosedError handler binds its exception (the
   UnboundLocalError class cannot come back).

The unit-level fix (``Restart=always`` in
``~/.config/systemd/user/blm-collector.service``) is not testable here.
"""
from __future__ import annotations

import inspect
import re
import time

from blm_v4 import collector as collector_mod
from blm_v4.classifications import Classification
from blm_v4.collector import PokerBetCollector
from blm_v4.discovery import RowGame
from blm_v4.storage import PokerBetStore


def _collector(tmp_path) -> PokerBetCollector:
    return PokerBetCollector(store=PokerBetStore(tmp_path / "b.db"))


# ── 1. the fatal handler must bind its exception ─────────────────────

def test_targetclosed_handler_binds_exc():
    """``except TargetClosedError:`` + ``exc`` in the body == UnboundLocalError.

    That is the exact regression from commit 2400b8fd; it turned a
    recoverable dead tab into a silent process exit.
    """
    src = inspect.getsource(PokerBetCollector.start)
    assert "except TargetClosedError as exc:" in src
    assert "except TargetClosedError:" not in src.replace(
        "except TargetClosedError as exc:", "")


def test_no_lossy_targetclosed_handlers_in_fast_path():
    """Every TargetClosedError handler in this module is either bound or
    free of ``exc`` references."""
    src = inspect.getsource(collector_mod)
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "except TargetClosedError:":
            body = "\n".join(lines[i + 1:i + 12]).replace("format_exc", "")
            assert not re.search(r"\bexc\b", body), (
                f"unbound `exc` referenced in TargetClosedError handler "
                f"at module line {i + 1}")


# ── 2. storm branch rotates the WORKER browser only ──────────────────

def test_failure_storm_rotates_worker_browser_not_fast(tmp_path, monkeypatch):
    c = _collector(tmp_path)
    c._slow_page = object()                     # guard: page is "ready"
    c._market_queue = ["g1"]
    c._event_view_failures = collector_mod.BROWSER_RELAUNCH_AFTER_EMPTY

    rotated: list[int] = []
    monkeypatch.setattr(c, "_rotate_slow_browser",
                        lambda: rotated.append(1))

    def _forbidden(reason):                      # the pre-fix call
        raise AssertionError(
            f"fast-path _relaunch() called from the slow worker: {reason}")

    monkeypatch.setattr(c, "_relaunch", _forbidden)

    c._capture_slow_market()

    assert rotated == [1], "storm branch did not rotate the worker browser"
    assert c._event_view_failures == 0, "failure counter not reset"


def test_worker_rotation_never_touches_the_fast_browser(tmp_path):
    """``_rotate_slow_browser`` is worker-local: it must not close or clear
    the fast browser/page even when the worker's own launch fails."""
    c = _collector(tmp_path)

    class _DeadPw:                               # launch always fails
        class chromium:
            @staticmethod
            def launch(**_kw):
                raise RuntimeError("boom")

    fast_browser, fast_page = object(), object()
    c._slow_pw = _DeadPw()
    c._slow_browser = object()
    c._browser = fast_browser
    c._browser_started_at = 123.0

    c._rotate_slow_browser()                     # must not raise

    assert c._browser is fast_browser, "fast browser was disturbed"
    assert c._slow_browser is None and c._slow_page is None


def test_worker_rotation_uses_its_own_playwright_scope(tmp_path):
    """The worker relaunch must go through ``self._slow_pw`` (worker
    thread's scope) — never ``self._pw`` (main thread's scope)."""
    c = _collector(tmp_path)
    used: list[str] = []

    class _Recorder:
        class chromium:
            @staticmethod
            def launch(**_kw):
                used.append("slow_pw")
                raise RuntimeError("stop here")

    class _FastPw:
        class chromium:
            @staticmethod
            def launch(**_kw):
                used.append("fast_pw")
                raise AssertionError("fast Playwright scope used by worker")

    c._slow_pw = _Recorder()
    c._pw = _FastPw()
    c._slow_browser = object()
    c._slow_page = object()

    c._rotate_slow_browser()

    assert used == ["slow_pw"], f"unexpected launch scope(s): {used}"


# ── 3. slow-page self-heal guard (2026-09-13 zombie incident) ─────────

"""Incident: 2026-09-13 ~00:07-01:41.  The fast-path _relaunch() cleared
_slow_page from the main thread.  The slow worker stayed alive but had no
page; _resolve_pending_on_worker returned a silent False for every claim,
so _requeue_resolve incremented resolve_failures +1,190 times with ZERO
journal output and newly discovered Betual NBA games never received their
games DB row (e.g. 30887787: tracked in memory, 0 DB rows).  Recovery
only happened when the worker's own 3600s browser rotation happened to
recreate the page.  These tests pin the worker-side self-heal guard."""


def _fake_game(cls_val="BETUAL_NBA", home="Los Angeles Lakers",
               away="Dallas Mavericks", gid="30899999"):
    from blm_v4.models import PokerBetGame
    return PokerBetGame(
        source_game_id=gid, classification=cls_val,
        competition_slug="betual-nba", competition="Betual NBA",
        region="virtual-matches", game_family="betual", sport="basketball",
        home_team=home, away_team=away,
        source_url=("https://x.com/en/sports/live/event-view/basketball/"
                    f"virtual-matches/9876/betual-nba/{gid}/"
                    "los-angeles-lakers-dallas-mavericks"),
        status="live",
    )


def _fake_page(c, text=""):
    """Minimal page double satisfying the resolve path's calls."""
    class _FakePage:
        url = ("https://x.com/en/sports/live/event-view/basketball/"
               "virtual-matches/9876/betual-nba/30899999/"
               "los-angeles-lakers-dallas-mavericks")

        def evaluate(self, *_a, **_k):
            return True                     # panel row "found" + clicked

        def inner_text(self, *_a, **_k):
            return text

        def wait_for_timeout(self, *_a, **_k):
            pass

    p = _FakePage()
    c._slow_page = p
    return p


def test_ensure_worker_page_noop_when_page_present(tmp_path):
    """Healthy page: the guard is a cheap no-op — no launches, no state."""
    c = _collector(tmp_path)
    c._slow_page = object()
    c._slow_browser = object()
    launched = []
    monkey_launch = lambda: launched.append(1)  # noqa: E731

    class _NoLaunch:
        class chromium:
            @staticmethod
            def launch(**_kw):
                raise AssertionError("launched despite a healthy page")

    c._slow_pw = _NoLaunch()
    c._ensure_worker_page()                 # must not raise or launch
    assert c._slow_page_ensure_failed_until == 0.0


def test_worker_selfheals_missing_page_before_resolve(tmp_path, monkeypatch):
    """Directive §6 (steps 1-3, 6): page missing → guard recreates it →
    resolution resumes — WITHOUT the fast-path _relaunch()."""
    c = _collector(tmp_path)
    c._slow_page = None                      # zombie state
    c._slow_browser = object()               # worker browser still alive
    calls = {"ensure": 0}

    def _fake_ensure_slow_page():
        calls["ensure"] += 1
        _fake_page(c)

    monkeypatch.setattr(c, "_ensure_slow_page", _fake_ensure_slow_page)
    monkeypatch.setattr(
        c, "_relaunch",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("fast-path _relaunch() called from worker")))

    c._ensure_worker_page()

    assert calls["ensure"] == 1, "existing page machinery not used"
    assert c._slow_page is not None, "page not recreated"
    assert c._slow_page_ensure_failed_until == 0.0


def test_worker_relax_no_relaunch_invariant_survives_guard(tmp_path):
    """Directive §2: the guard must not CALL the fast-path _relaunch —
    verified behaviorally — and must use the existing slow-browser
    lifecycle machinery (structural check)."""
    c = _collector(tmp_path)
    c._slow_page = None                      # browser AND page gone

    class _DeadPw:
        class chromium:
            @staticmethod
            def launch(**_kw):
                raise RuntimeError("boom")

    c._slow_pw = _DeadPw()

    def _forbidden(*_a, **_k):
        raise AssertionError("fast-path _relaunch() called from worker")

    c._relaunch = _forbidden
    try:
        c._ensure_worker_page()
    except c.PageUnavailable:
        pass                                 # expected: launch failed
    assert c._slow_page is None

    import inspect
    body = inspect.getsource(PokerBetCollector._ensure_worker_page)
    body_code = body.split('"""', 2)[-1]     # exclude the docstring
    assert "self._relaunch" not in body_code
    assert "_rotate_slow_browser" in body or "_ensure_slow_page" in body


def test_recreation_failure_backs_off_and_stays_safe(tmp_path, caplog):
    """Directive §6 failure case: recreation fails → no crash, explicit
    diagnostic, bounded backoff, worker state intact."""
    c = _collector(tmp_path)
    c._slow_page = None

    class _DeadPw:
        class chromium:
            @staticmethod
            def launch(**_kw):
                raise RuntimeError("boom")

    c._slow_pw = _DeadPw()

    with caplog.at_level("WARNING", logger="blm_v4.collector"):
        for i in range(3):                   # three loop iterations
            if i:
                # bypass the loop's backoff sleep between attempts (the
                # LAST attempt's backoff must stay armed for the assert)
                c._slow_page_ensure_failed_until = 0.0
            try:
                c._ensure_worker_page()
            except c.PageUnavailable:
                pass

    warns = [r for r in caplog.records
             if "page unavailable" in r.getMessage().lower()
             or "recreation failed" in r.getMessage().lower()]
    assert warns, "no diagnostic emitted for pageless worker"
    assert c._slow_page is None, "should still be pageless after failures"
    assert c._slow_page_ensure_failed_until > 0, "backoff not re-armed"


def test_page_none_resolve_raises_not_silent(tmp_path, caplog):
    """Directive §5: the old ``page is None → return False`` was silent.
    It must now surface as PageUnavailable so the loop logs + requeues."""
    import pytest

    c = _collector(tmp_path)
    c._slow_page = None
    game = _fake_game()
    with caplog.at_level("WARNING", logger="blm_v4.collector"):
        with pytest.raises(c.PageUnavailable):
            c._resolve_pending_on_worker(
                Classification.BETUAL_NBA,
                RowGame(home_team=game.home_team, away_team=game.away_team))
    assert any("page unavailable" in r.getMessage().lower()
               or "no slow page" in r.getMessage().lower()
               for r in caplog.records), (
        "page-None resolve failure produced no diagnostic")


def test_market_queue_drops_untracked_gids(tmp_path, monkeypatch):
    """Directive §7: ended/pruned gids cannot cycle forever — they are
    dropped; the legitimate tracked live game stays queued."""
    c = _collector(tmp_path)
    ended = _fake_game(gid="11111111")
    ended.status = "ended"
    live = _fake_game(gid="22222222")
    cls = Classification.BETUAL_NBA
    c._tracked[cls]["Ended|Game"] = ended
    c._tracked[cls]["Live|Game"] = live
    c._market_queue = ["99999999", "11111111", "22222222"]  # pruned, ended, live

    # stub the rest of the round so nothing else runs
    monkeypatch.setattr(c, "_never_line_gids", lambda: set())
    monkeypatch.setattr(c, "_sync_market_subscriptions", lambda *a, **k: None)
    monkeypatch.setattr(c, "_capture_event_state", lambda *a, **k: None)
    monkeypatch.setattr(c, "_goto", lambda *a, **k: True)
    monkeypatch.setattr(c, "_wait_panel", lambda *a, **k: None)

    class _QPage:
        def evaluate(self, *a, **k):
            return True

        def inner_text(self, *a, **k):
            return ""

        def wait_for_timeout(self, *a, **k):
            pass

    c._slow_page = _QPage()
    c._capture_slow_market()

    assert "99999999" not in c._market_queue, "pruned gid not dropped"
    assert "11111111" not in c._market_queue, "ended gid not dropped"
    assert "22222222" in c._market_queue, "legitimate live game dropped!"
    assert c._market_stats.get("dropped_untracked", 0) == 2


def test_full_incident_sequence_selfheal_to_db_row(tmp_path, monkeypatch):
    """Directive §8 — the incident end-to-end:
    A. healthy page → B. fast _relaunch clears _slow_page → D. new Betual
    NBA game pending → F. worker encounters page=None → G. self-heal →
    H. resolve succeeds → I. game receives its DB row."""
    c = _collector(tmp_path)

    # A. healthy worker page
    page = _fake_page(c)
    assert c._slow_page is not None

    # B. the fast-path relaunch clears the worker's page handle (the real
    #    _relaunch does this unconditionally on browser lifetime cap)
    c._slow_page = None

    # D/E. a newly discovered Betual NBA game is queued for resolution
    game = _fake_game()
    key = f"{game.home_team}|{game.away_team}"
    c._pending_resolve[(Classification.BETUAL_NBA.value, key)] = {
        "queued_at": time.monotonic(), "attempted_at": 0.0,
        "home": game.home_team, "away": game.away_team,
        "classification": Classification.BETUAL_NBA.value,
    }

    # G. the worker's guard self-heals the page (existing machinery only;
    #    the worker browser object is still alive — only the page was lost)
    c._slow_browser = object()
    monkeypatch.setattr(
        c, "_relaunch",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("fast-path _relaunch() called from worker")))
    recreated = []

    def _fake_ensure_slow_page():
        recreated.append(1)
        _fake_page(c)

    monkeypatch.setattr(c, "_ensure_slow_page", _fake_ensure_slow_page)
    c._ensure_worker_page()
    assert recreated and c._slow_page is not None

    # H/I. resolve now succeeds and the game gets its DB row
    ev_text = (
        "Betual NBA\n"
        "Los Angeles Lakers : Dallas Mavericks, (30:28), (25:26), "
        "(31:26), (28:24), 09:46\n"
        "09:46\n"
        "Los Angeles Lakers\n"
        "Dallas Mavericks\n"
    )

    class _ResPage:
        url = game.source_url

        def evaluate(self, *a, **k):
            return True

        def inner_text(self, *a, **k):
            return ev_text

        def wait_for_timeout(self, *a, **k):
            pass

    c._slow_page = _ResPage()
    monkeypatch.setattr(c, "_goto", lambda *a, **k: True)
    monkeypatch.setattr(c, "_wait_panel", lambda *a, **k: None)
    monkeypatch.setattr(c, "_capture_event_state", lambda *a, **k: None)

    ok = c._resolve_pending_on_worker(
        Classification.BETUAL_NBA,
        RowGame(home_team=game.home_team, away_team=game.away_team))

    assert ok is True, "resolution did not succeed after self-heal"
    row = c.store.get_game(game.source_game_id)
    assert row is not None, "games DB row missing — the blackout regression"
    assert row["home_team"] == game.home_team
    assert row["away_team"] == game.away_team
    assert row["competition_slug"] == "betual-nba"
