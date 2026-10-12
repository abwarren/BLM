"""PREMATURE SETTLEMENT (directive 2026-10-12).

The reconciler's verification is TIME-BLIND: it decides "is this score
plausible for THIS fixture" but never "has this fixture FINISHED".  A LIVE
mid-game frame satisfies the score rule ``final >= observed max`` BY
CONSTRUCTION — the live score IS the running max — so a game still playing
was settled with a final BELOW a total it later reached.

Measured (21 days): 1,856 of 15,577 OK finals (11.9%) were written before
the game's own last observation.  Proof (game 31202604): settled 69:108=177
at 13:45:06Z while its snapshots kept arriving every ~6 s and reached
80:121=201 at 13:49:50Z.

Fix: ONE predicate — ``result_policy.game_is_terminal`` — a result may only
be accepted once the game's own observation stream has gone QUIET (or
carries an explicit terminal marker).  Enforced at BOTH reconciler decision
points (the swarm/feed path and the page path) AND at candidate selection,
so a live game burns no attempt budget.

One named test per requirement, against the SHIPPED functions — no copies.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import blm_v4.result_policy as policy
import blm_v4.result_reconciler as rr
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.result_reconciler import ResultReconciler
from blm_v4.result_policy import game_is_terminal
from blm_v4.scorecard import SCORECARD_SCHEMA
from blm_v4.storage import PokerBetStore
from blm_v4.swarm_results import normalize_result

HOME, AWAY = "Home Five", "Away Six"
GID = "31202604"
DB_NAME = "blm_pokerbet.db"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def page(home: str, away: str, h: int, a: int, quarters, start: str,
         *, comp: str = "Betual NBA (Virtual Matches)",
         status: str = "Finished") -> str:
    qs = ", ".join(f"{x}:{y}" for x, y in quarters)
    return (f"SIGN IN\nREGISTER\n{comp}\n{start}\n"
            f"{home}\n{h}\n{away}\n{a}\n{h}:{a} ({qs})\n{status}\n")


class _FakeFetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch_results_page(self, gid):
        self.calls.append(gid)
        return self.pages.get(gid)


def _frame(env, h: int, a: int, *, game_id: str = GID, start_dt=None,
           quarters=None) -> dict:
    """One swarm feed reply -> normalize_result() output (the shape the
    reconciler trusts).  Quarters default to a split that SUMS to h:a."""
    if quarters is None:
        bh, ba = h // 4, a // 4
        quarters = [(bh, ba), (bh, ba), (bh, ba),
                    (h - 3 * bh, a - 3 * ba)]
    qs = ", ".join(f"{x}:{y}" for x, y in quarters)
    ts = int((start_dt or (env["now"] - timedelta(hours=1))).timestamp())
    return normalize_result({
        "game_id": game_id, "scores": f"{h}:{a}({qs})",
        "team1_name": HOME, "team2_name": AWAY,
        "team1_id": 1423916, "team2_id": 1423878, "date": ts,
        "competition_name": "Betual NBA", "region_name": "Virtual Matches",
        "sport_id": "3", "sport_alias": "Basketball"})


class _FakeSwarm:
    def __init__(self, results: dict):
        self.results, self.calls = results, []

    def fetch_results(self, ids):
        self.calls.append([str(i) for i in ids])
        return {str(g): self.results.get(str(g)) for g in ids}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Real storage DB + scorecard schema + helpers.  Mirrors the authority
    suite's fixture so identity/start-time checks behave identically."""
    monkeypatch.setenv("BLM_POKERBET_DB", str(tmp_path / DB_NAME))
    db = tmp_path / DB_NAME
    st = PokerBetStore(db)
    conn = sqlite3.connect(str(db))
    conn.executescript(SCORECARD_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(game_results)")}
    if "result_source" not in cols:
        conn.execute(
            "ALTER TABLE game_results ADD COLUMN result_source TEXT")
    conn.commit()
    conn.close()
    now = datetime.now(timezone.utc)

    def add(gid: str = GID) -> None:
        st.upsert_game(PokerBetGame(
            source="PokerBet", source_game_id=gid,
            competition_id="c", competition_slug="betual-nba",
            competition="Betual NBA", region="Virtual Matches",
            game_family="betual", classification="BETUAL_NBA",
            sport="basketball", home_team=HOME, away_team=AWAY,
            game_slug=f"{gid}-game", source_url=f"https://x/{gid}",
            status="ended", first_seen_at=_iso(now - timedelta(hours=1)),
            last_seen_at=_iso(now - timedelta(minutes=55))))
        # upsert_game stamps last_seen_at = now; re-stamp the realistic
        # last observation the fixture-start window anchors on.
        c = sqlite3.connect(str(db))
        c.execute("UPDATE games SET last_seen_at=? WHERE source_game_id=?",
                  (_iso(now - timedelta(minutes=55)), gid))
        c.commit()
        c.close()

    def snap(gid: str, t: datetime, hs, as_, q=None, clock="",
             *, status: str = "live", label: str = "") -> str:
        g = st.get_game(gid)
        st.insert_snapshot(g["id"], MarketObservation(
            source="PokerBet", source_game_id=gid,
            classification="BETUAL_NBA", captured_at=_iso(t),
            home_team=HOME, away_team=AWAY, home_score=hs, away_score=as_,
            period_label=label or (f"{q}th Quarter" if q else ""),
            quarter=q, clock=clock, game_status=status, total_line=None,
            spread=None, w1_odds=None, w2_odds=None, markets_json="{}"),
            force=True)
        return _iso(t)

    def reconciler(pages=None, **kw) -> ResultReconciler:
        return ResultReconciler(db, fetcher=_FakeFetcher(pages or {}), **kw)

    def row(gid: str = GID):
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM game_results WHERE source_game_id=?",
                      (gid,)).fetchone()
        c.close()
        return dict(r) if r else None

    def attempts(gid: str = GID) -> list:
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM result_reconciliation WHERE source_game_id=? "
            "ORDER BY attempt", (gid,))]
        c.close()
        return rows

    def state(gid: str = GID):
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM result_reconciliation_state "
                      "WHERE source_game_id=?", (gid,)).fetchone()
        c.close()
        return dict(r) if r else None

    def game_dict(gid: str = GID) -> dict:
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM games WHERE source_game_id=?",
                      (gid,)).fetchone()
        last = c.execute("SELECT MAX(captured_at) FROM snapshots "
                         "WHERE source_game_id=?", (gid,)).fetchone()[0]
        c.close()
        d = dict(r)
        d["last_snap_at"] = last
        return d

    return {"db": db, "now": now, "iso": _iso, "add": add, "snap": snap,
            "reconciler": reconciler, "row": row, "attempts": attempts,
            "state": state, "game_dict": game_dict,
            "page_start": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")}


# ── the predicate itself (pure, unit-testable) ────────────────────────

def test_game_is_terminal_predicate_unit():
    """The ONE predicate: quiet OR explicit marker; fail closed otherwise."""
    now = "2026-10-12T13:45:00.000000Z"
    # FRESH stream (within the grace) → NOT terminal
    assert game_is_terminal("2026-10-12T13:44:55.000000Z", now) is False
    # 89 s < 90 s grace → still NOT terminal
    assert game_is_terminal("2026-10-12T13:43:31.000000Z", now) is False
    # exactly at the 90 s grace → terminal (>= is inclusive)
    assert game_is_terminal("2026-10-12T13:43:30.000000Z", now) is True
    assert game_is_terminal("2026-10-12T13:43:30.000000Z", now, grace_s=91.0) \
        is False
    # silent stream → terminal
    assert game_is_terminal("2026-10-12T13:30:00.000000Z", now) is True
    # explicit terminal marker wins even for a fresh stream
    assert game_is_terminal("2026-10-12T13:44:59.000000Z", now,
                            terminal_marker=True) is True
    # FAIL CLOSED: absent / unparseable is NOT terminal
    assert game_is_terminal(None, now) is False
    assert game_is_terminal("", now) is False
    assert game_is_terminal("not-a-timestamp", now) is False
    # accepts datetime objects too
    n = datetime(2026, 10, 12, 13, 45, tzinfo=timezone.utc)
    assert game_is_terminal(n - timedelta(seconds=5), n) is False
    assert game_is_terminal(n - timedelta(minutes=5), n) is True


# ── 1. a LIVE game is NOT settled ─────────────────────────────────────

def test_live_game_result_is_not_settled(env):
    """A game with a FRESH snapshot (still playing) must not settle from
    EITHER source — the candidate scan excludes it AND the apply path
    refuses a result handed to it directly."""
    env["add"]()
    # snapshot 5 s ago — the live cadence; the game is mid-Q4 and scoring
    env["snap"](GID, env["now"] - timedelta(seconds=5), 69, 106, 4, "01:00")

    # (a) the CANDIDATE scan excludes it: nothing is fetched/scanned.
    feed = _FakeSwarm({GID: _frame(env, 69, 108)})
    page_txt = page(HOME, AWAY, 69, 108, [(18, 27), (17, 26), (16, 27),
                                          (18, 28)], env["page_start"])
    stats = env["reconciler"]({GID: page_txt}, swarm=feed).run_pass()
    assert stats["scanned"] == 0, stats
    assert stats["verified"] == 0
    assert feed.calls == []                       # not even fetched
    assert env["row"]() is None or env["row"]()["final_result_status"] != "OK"

    # (b) the APPLY path refuses a live game handed to it directly
    r2 = env["reconciler"]({})
    game = env["game_dict"]()                      # carries the fresh last_snap_at
    out = r2._apply_swarm_result(game, _frame(env, 69, 108))
    assert out == "REJECTED"
    assert env["row"]() is None or env["row"]()["final_result_status"] != "OK"
    checks = json.loads(env["attempts"]()[0]["checks_json"])
    assert checks["terminal_guard"] == "game_not_terminal"


# ── 2. a QUIET game (silent >= grace) IS settled ──────────────────────

def test_quiet_game_result_settles(env):
    """Once the stream has gone quiet, a plausible final IS accepted."""
    env["add"]()
    env["snap"](GID, env["now"] - timedelta(minutes=30), 55, 52, 3, "05:00")
    env["snap"](GID, env["now"] - timedelta(minutes=10), 77, 79, 3, "01:00")

    feed = _FakeSwarm({GID: _frame(env, 95, 95)})
    stats = env["reconciler"]({}, swarm=feed).run_pass()

    assert stats["scanned"] == 1 and stats["verified"] == 1, stats
    r = env["row"]()
    assert r["final_result_status"] == "OK"
    assert (r["final_home"], r["final_away"]) == (95, 95)
    assert r["final_total"] == 190
    assert r["result_source"] == "RESULTS_PAGE"


# ── 3. the existing foreign-instance guard still fires ────────────────

def test_lower_than_bound_still_refused(env):
    """The 2026-09-24 impossible-final guard is UNTOUCHED: a final BELOW an
    already-observed score is still refused even when the game is quiet."""
    env["add"]()
    env["snap"](GID, env["now"] - timedelta(minutes=30), 60, 58, 3, "05:00")

    impossible = page(HOME, AWAY, 55, 50,
                      [(14, 12), (13, 13), (15, 14), (13, 11)],
                      env["page_start"])            # 55:50 < observed 60:58
    stats = env["reconciler"]({GID: impossible}).run_pass()

    assert stats["verified"] == 0 and stats["rejected"] == 1
    checks = json.loads(env["attempts"]()[0]["checks_json"])
    assert checks["history_consistent"] is False
    assert checks["terminal_guard"] == "terminal"   # the stream IS quiet
    assert env["row"]()["final_home"] is None


# ── 4. the guard is SHARED between both decision points ───────────────

def test_guard_is_shared(env):
    """ONE predicate: the reconciler uses result_policy.game_is_terminal
    itself (no copy), and BOTH apply paths call it."""
    assert rr.game_is_terminal is policy.game_is_terminal

    env["add"]()
    env["snap"](GID, env["now"] - timedelta(seconds=5), 69, 106, 4, "01:00")
    game = env["game_dict"]()

    seen: list = []

    def spy(last_snapshot_at, now, grace_s=None, **kw):
        seen.append((last_snapshot_at, kw.get("terminal_marker")))
        return False                              # force a refusal

    env["reconciler"]({})                          # spins up schema
    monkey = rr.game_is_terminal
    rr.game_is_terminal = spy
    try:
        # feed path
        r = env["reconciler"]({})
        assert r._apply_swarm_result(dict(game), _frame(env, 69, 108)) \
            == "REJECTED"
        # page path (no swarm result → DOM/text path)
        r2 = env["reconciler"]({})
        assert r2._reconcile_one(r2._fetcher, dict(game)) == "REJECTED"
    finally:
        rr.game_is_terminal = monkey

    # the SAME predicate object was invoked by BOTH paths
    assert len(seen) >= 2
    assert seen[0][0] == game["last_snap_at"] == seen[1][0]


# ── 5. a live game burns NO attempt budget ────────────────────────────

def test_no_attempt_burned_while_live(env):
    """A still-live game is SKIPPED at candidate selection, not retried:
    no attempt row, no state row, no OK verdict."""
    env["add"]()
    env["snap"](GID, env["now"] - timedelta(seconds=5), 69, 106, 4, "01:00")

    feed = _FakeSwarm({GID: _frame(env, 69, 108)})
    out = env["reconciler"]({}, swarm=feed).run_pass()

    assert out["scanned"] == 0, out
    assert env["attempts"]() == []                 # attempt budget intact
    assert env["state"]() is None
    assert env["row"]() is None or env["row"]()["final_result_status"] != "OK"


# ── 6. the 31202604 shape regression ─────────────────────────────────

def test_31202604_shape_regression(env):
    """Settle-at-T while the game's own feed keeps writing to T+4min.

    The pre-fix reconciler wrote 69:108=177 at T; the game then reached
    80:121=201.  With the guard, the premature settle is REFUSED and the
    later, complete final settles instead — the stored total is never 177.
    """
    env["add"]()
    # T: the feed answers LIVE (69:108) while the game's own latest
    # observation is only 5 s old — the exact race that wrote 177.
    at_t = env["game_dict"]()
    at_t["last_snap_at"] = env["iso"](env["now"] - timedelta(seconds=5))
    assert env["reconciler"]({})._apply_swarm_result(
        at_t, _frame(env, 69, 108)) == "REJECTED"
    assert env["row"]() is None or env["row"]()["final_result_status"] != "OK"

    # ...the same game kept scoring (69:106 -> 80:121), then went quiet.
    env["snap"](GID, env["now"] - timedelta(minutes=30), 69, 106, 4, "01:00")
    env["snap"](GID, env["now"] - timedelta(minutes=3), 80, 121, 4, "00:30")
    stats = env["reconciler"](
        {}, swarm=_FakeSwarm({GID: _frame(env, 80, 121)})).run_pass()

    assert stats["verified"] == 1, stats
    r = env["row"]()
    assert r["final_result_status"] == "OK"
    assert r["final_total"] == 201                 # the TRUE final
    assert r["final_total"] != 177                 # 177 never stuck


# ── population-scale companion ────────────────────────────────────────

def test_many_live_games_are_all_refused(env):
    """A whole live cohort (fresh cadence) settles NONE of them; the pass
    touches no attempt budget while they play."""
    ids = [f"9{1000000 + i}" for i in range(12)]
    feed_map = {}
    for gid in ids:
        env["add"](gid)
        env["snap"](gid, env["now"] - timedelta(seconds=4), 50, 60, 4, "02:00")
        feed_map[gid] = _frame(env, 50, 62, game_id=gid)

    out = env["reconciler"]({}, swarm=_FakeSwarm(feed_map)).run_pass()

    assert out["scanned"] == 0, out
    for gid in ids:
        assert env["attempts"](gid) == []
        assert env["row"](gid) is None
