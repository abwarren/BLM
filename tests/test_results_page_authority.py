"""RESULTS-PAGE AUTHORITY + VALIDITY (directive 2026-09-24).

The directive's two non-negotiables, pinned one test per requirement:

  A. "THE RESULTS PAGE IS AUTHORITATIVE" — a result verified from the
     PokerBet results page is a FINAL.  A snapshot-derived re-derivation
     (the settle worker, the scorecard sweep) must NEVER destroy it, and
     a disagreeing captured endpoint is FLAGGED in ``result_conflicts``,
     never written over.  A "NO FINAL" may never be produced merely
     because the live market is unavailable.

  B. "VALIDATE GAME ID / TEAMS / SCORE" — a page render is accepted as a
     final only when the identity proves the fixture (team names + the
     start-time cross-check) AND the SCORE is believable:
       1. all four quarters rendered (a completed 4-quarter game always
          renders four — a shorter render is a mid-game or foreign frame),
       2. the quarters SUM to the compact final line,
       3. the final is never BELOW a score already observed for the same
          game (scores are monotonic within a fixture — measured live:
          739 of 2,907 page-verified results failed this and were written
          into the statistics anyway).

  C. THE STATE MACHINE — a destroyed verification is re-attempted, a
     REJECTED render is retryable, a TEMPLATE_FAILED render is exhausted,
     and an unresolved game always has an EXPLICIT row (never a silent
     absence, never a terminal "NO FINAL").

Runs against the SHIPPED reconciler, the SHIPPED settle worker and the
SHIPPED scorecard — no copies of the logic under test.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from blm_v4 import result_policy as policy
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.result_reconciler import (
    STATUS_NEEDS_RECONCILIATION,
    ResultReconciler,
)
from blm_v4.scorecard import SCORECARD_SCHEMA, Scorecard
from blm_v4.settle_worker import settle_once

HOME, AWAY = "Home Five", "Away Six"
GID = "91000001"

DB_NAME = "blm_pokerbet.db"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def page(home: str, away: str, h: int, a: int, quarters: list[tuple[int, int]],
         start: str, *, comp: str = "Betual NBA (Virtual Matches)",
         status: str = "Finished") -> str:
    """A results-page body text in the shape the parser documents."""
    qs = ", ".join(f"{x}:{y}" for x, y in quarters)
    return (f"SIGN IN\nREGISTER\n{comp}\n{start}\n"
            f"{home}\n{h}\n{away}\n{a}\n"
            f"{h}:{a} ({qs})\n{status}\n")


FAILED_TEMPLATE = ("RESULTS\nLIVE CALENDAR\nStart Date *\nEnd Date *\n"
                   "Sport\nCompetition\nRESET\nSHOW\n")


class _FakeFetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch_results_page(self, gid):
        self.calls.append(gid)
        return self.pages.get(gid)


# ── fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    """A real storage DB + the scorecard schema + helpers."""
    from blm_v4.storage import PokerBetStore
    db = tmp_path / DB_NAME
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    st = PokerBetStore(db)
    conn = sqlite3.connect(str(db))
    conn.executescript(SCORECARD_SCHEMA)
    # the provenance column the reconciler/settle worker migrate in
    # (idempotent — the same bootstrap production runs)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(game_results)")}
    if "result_source" not in cols:
        conn.execute(
            "ALTER TABLE game_results ADD COLUMN result_source TEXT")
    conn.commit()
    conn.close()
    now = datetime.now(timezone.utc)

    def add(gid: str = GID, *, status: str = "ended") -> None:
        st.upsert_game(PokerBetGame(
            source="PokerBet", source_game_id=gid,
            competition_id="c", competition_slug="betual-nba",
            competition="Betual NBA", region="Virtual Matches",
            game_family="betual", classification="BETUAL_NBA",
            sport="basketball", home_team=HOME, away_team=AWAY,
            game_slug=f"{gid}-game", source_url=f"https://x/{gid}",
            status=status, first_seen_at=_iso(now - timedelta(hours=1)),
            last_seen_at=_iso(now - timedelta(minutes=55))))
        # storage.upsert_game stamps last_seen_at = now unconditionally
        # (production semantics: "the last time the collector saw it"), so
        # a fixture that needs a realistic last observation must set it.
        # The page start (now-1h) requires the fixture to have been RUNNING
        # 55 min in — the 2026-09-24 fixture-start window anchors on it.
        c = sqlite3.connect(str(db))
        c.execute("UPDATE games SET last_seen_at=? WHERE source_game_id=?",
                  (_iso(now - timedelta(minutes=55)), gid))
        c.commit()
        c.close()

    def snap(gid: str, t: datetime, hs: int, as_: int, q: int,
             clock: str, *, status: str = "live") -> None:
        g = st.get_game(gid)
        game_id = st.upsert_game(PokerBetGame(
            source="PokerBet", source_game_id=gid,
            competition_id="c", competition_slug="betual-nba",
            competition="Betual NBA", region="Virtual Matches",
            game_family="betual", classification="BETUAL_NBA",
            sport="basketball", home_team=HOME, away_team=AWAY,
            game_slug=f"{gid}-game", source_url=f"https://x/{gid}",
            status=g["status"], first_seen_at=_iso(now - timedelta(hours=1)),
            last_seen_at=_iso(now)))
        # ...and re-stamp the realistic last observation (upsert_game sets
        # last_seen_at = now — see add() above)
        c = sqlite3.connect(str(db))
        c.execute("UPDATE games SET last_seen_at=? WHERE source_game_id=?",
                  (_iso(now - timedelta(minutes=55)), gid))
        c.commit()
        c.close()
        st.insert_snapshot(game_id, MarketObservation(
            source="PokerBet", source_game_id=gid,
            classification="BETUAL_NBA", captured_at=_iso(t),
            home_team=HOME, away_team=AWAY, home_score=hs, away_score=as_,
            period_label=f"{q}th Quarter", quarter=q, clock=clock,
            game_status=status, total_line=None, spread=None,
            w1_odds=None, w2_odds=None, markets_json="{}"), force=True)

    def reconciler(pages: dict, **kw) -> ResultReconciler:
        return ResultReconciler(db, fetcher=_FakeFetcher(pages), **kw)

    def row(gid: str = GID) -> dict | None:
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM game_results WHERE source_game_id=?",
                      (gid,)).fetchone()
        c.close()
        return dict(r) if r else None

    def audit(gid: str = GID) -> list[dict]:
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM result_reconciliation WHERE source_game_id=? "
            "ORDER BY attempt", (gid,))]
        c.close()
        return rows

    def conflicts(gid: str = GID) -> list[dict]:
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM result_conflicts WHERE source_game_id=?", (gid,))]
        c.close()
        return rows

    def put_result(gid: str, h: int, a: int, total: int, *, source: str,
                   status: str = "OK", result_at: str = None) -> None:
        c = sqlite3.connect(str(db))
        c.execute(
            """INSERT INTO game_results (source_game_id, classification,
                   final_home, final_away, final_total, result_at,
                   final_result_status, result_source)
               VALUES (?, 'BETUAL_NBA', ?, ?, ?, ?, ?, ?)""",
            (gid, h, a, total, result_at or _iso(now - timedelta(hours=2)),
             status, source))
        c.commit()
        c.close()

    return {"db": db, "add": add, "snap": snap, "now": now, "iso": _iso,
            "reconciler": reconciler, "row": row, "audit": audit,
            "conflicts": conflicts, "put_result": put_result,
            "page_start": (now - timedelta(hours=1)).strftime(
                "%Y-%m-%d %H:%M")}


def _verified_page(env, h=95, a=95):
    """A complete, internally consistent page for the fixture.

    95:95 = (25:25, 20:22, 22:21, 28:27) — four quarters that sum.
    """
    return page(HOME, AWAY, h, a,
                [(25, 25), (20, 22), (22, 21), (28, 27)], env["page_start"])


# ══════════════════════════════════════════════════════════════════════
# A. THE RESULTS PAGE IS AUTHORITATIVE
# ══════════════════════════════════════════════════════════════════════

def test_results_page_verdict_survives_the_settle_worker_rederivation(env):
    """A verified page final is IMMUTABLE: the settle worker's pass never
    re-derives it — no NULLed scores, no demotion to UNKNOWN, and the
    stored verdict (and its result_at) is byte-identical afterwards."""
    env["add"]()
    now = env["now"]
    env["snap"](GID, now - timedelta(minutes=50), 20, 18, 1, "05:00")
    env["snap"](GID, now - timedelta(minutes=30), 55, 52, 3, "05:00")
    env["snap"](GID, now - timedelta(minutes=10), 88, 90, 3, "01:00")
    env["put_result"](GID, 95, 95, 190, source="RESULTS_PAGE",
                      result_at=env["iso"](now - timedelta(minutes=50)))
    before = env["row"]()

    settle_once(env["db"])

    r = env["row"]()
    assert (r["final_home"], r["final_away"], r["final_total"]) == (95, 95, 190)
    assert r["final_result_status"] == "OK"
    assert r["result_source"] == "RESULTS_PAGE"
    assert r["result_at"] == before["result_at"]   # never re-derived


def test_settle_worker_flags_a_disagreeing_history_without_overwriting(env):
    """Even when a settlement attempt IS made on an authoritative row
    (defence in depth: the candidate query already excludes them), the
    guard keeps the verdict and FLAGS the disagreement."""
    from blm_v4.settle_worker import _settle_game
    env["add"]()
    now = env["now"]
    env["snap"](GID, now - timedelta(minutes=30), 55, 52, 3, "05:00")
    env["snap"](GID, now - timedelta(minutes=10), 88, 90, 3, "01:00")
    env["put_result"](GID, 95, 95, 190, source="RESULTS_PAGE",
                      result_at=env["iso"](now - timedelta(minutes=50)))

    conn = sqlite3.connect(str(env["db"]))
    conn.row_factory = sqlite3.Row
    g = conn.execute(
        "SELECT id, source_game_id, classification FROM games "
        "WHERE source_game_id=?", (GID,)).fetchone()
    assert _settle_game(conn, g) is None            # protected, untouched
    conn.commit()
    conn.close()

    r = env["row"]()
    assert (r["final_home"], r["final_away"], r["final_total"]) == (95, 95, 190)
    assert r["final_result_status"] == "OK"
    conf = env["conflicts"]()
    assert len(conf) == 1, conf
    assert conf[0]["live_dom_total"] == 178          # captured endpoint
    assert conf[0]["results_total"] == 190           # verified page final
    assert "RESULTS_PAGE" in conf[0]["detail"]


def test_results_page_verdict_survives_the_scorecard_rederivation(env):
    """Same immutability through the 4-hour sweep's own settle step."""
    env["add"]()
    now = env["now"]
    env["snap"](GID, now - timedelta(minutes=50), 20, 18, 1, "05:00")
    env["snap"](GID, now - timedelta(minutes=30), 55, 52, 3, "05:00")
    env["snap"](GID, now - timedelta(minutes=10), 88, 90, 3, "01:00")
    env["put_result"](GID, 95, 95, 190, source="RESULTS_PAGE",
                      result_at=env["iso"](now - timedelta(minutes=50)))

    Scorecard(env["db"]).capture_results()

    r = env["row"]()
    assert (r["final_home"], r["final_away"], r["final_total"]) == (95, 95, 190)
    assert r["final_result_status"] == "OK"
    assert len(env["conflicts"]()) == 1


def test_a_non_ok_row_never_borrows_authority_from_the_source_column(env):
    """Authority is provenance AND status: a NEEDS_RECONCILIATION row
    carrying a stale result_source='RESULTS_PAGE' is not a verdict and
    must be re-derivable (the exact state the old clobber left behind)."""
    assert policy.is_results_page_verdict(
        {"result_source": "RESULTS_PAGE", "final_result_status": "OK"})
    for status in ("NEEDS_RECONCILIATION", "UNKNOWN", "INVALID"):
        assert not policy.is_results_page_verdict(
            {"result_source": "RESULTS_PAGE", "final_result_status": status})


# ══════════════════════════════════════════════════════════════════════
# B. VALIDATE THE RENDERED SCORE
# ══════════════════════════════════════════════════════════════════════

def test_a_three_quarter_render_is_not_a_final(env):
    """A completed 4-quarter game renders four quarters; a 3-quarter
    frame is a mid-game capture (or another fixture) and never becomes
    this game's final."""
    env["add"]()
    now = env["now"]
    env["snap"](GID, now - timedelta(minutes=50), 20, 18, 1, "05:00")
    env["snap"](GID, now - timedelta(minutes=30), 55, 52, 3, "05:00")
    partial = page(HOME, AWAY, 66, 59, [(20, 18), (21, 19), (25, 22)],
                   env["page_start"])

    stats = env["reconciler"]({GID: partial}).run_pass()

    assert stats["verified"] == 0 and stats["rejected"] == 1
    row = env["row"]()
    assert row["final_result_status"] == STATUS_NEEDS_RECONCILIATION
    assert row["final_home"] is None
    assert row["result_source"] is None          # provenance never lies
    checks = json.loads(env["audit"]()[0]["checks_json"])
    assert checks["render_complete"] is False
    assert checks["parse_quality"] == "partial"


def test_quarter_scores_must_sum_to_the_rendered_final(env):
    """A self-contradicting page is never a result."""
    env["add"]()
    bad = page(HOME, AWAY, 95, 95, [(25, 25), (20, 22), (22, 21), (28, 30)],
               env["page_start"])          # quarters sum 95:98

    stats = env["reconciler"]({GID: bad}).run_pass()

    assert stats["verified"] == 0 and stats["rejected"] == 1
    checks = json.loads(env["audit"]()[0]["checks_json"])
    assert checks["page_sums_valid"] is False
    assert env["row"]()["final_result_status"] == STATUS_NEEDS_RECONCILIATION


def test_a_final_below_the_captured_history_is_rejected(env):
    """The guard that kills the 739 impossible results: a final can never
    be lower than a score the same game was already observed at."""
    env["add"]()
    now = env["now"]
    env["snap"](GID, now - timedelta(minutes=50), 30, 28, 1, "05:00")
    env["snap"](GID, now - timedelta(minutes=30), 60, 58, 3, "05:00")
    impossible = page(HOME, AWAY, 55, 50,
                      [(14, 12), (13, 13), (15, 14), (13, 11)],
                      env["page_start"])      # 55:50 < observed 60:58

    stats = env["reconciler"]({GID: impossible}).run_pass()

    assert stats["verified"] == 0 and stats["rejected"] == 1
    checks = json.loads(env["audit"]()[0]["checks_json"])
    assert checks["history_consistent"] is False
    assert checks["observed_max_home"] == 60 and checks["observed_max_away"] == 58
    assert env["row"]()["final_home"] is None


def test_a_complete_consistent_final_above_the_history_verifies(env):
    """The positive control for the same guard: the true final (above
    every observed state, four quarters, sums) IS verified."""
    env["add"]()
    now = env["now"]
    env["snap"](GID, now - timedelta(minutes=50), 20, 18, 1, "05:00")
    env["snap"](GID, now - timedelta(minutes=30), 55, 52, 3, "05:00")

    stats = env["reconciler"]({GID: _verified_page(env)}).run_pass()

    assert stats["verified"] == 1
    r = env["row"]()
    assert (r["final_home"], r["final_away"], r["final_total"]) == (95, 95, 190)
    assert r["final_result_status"] == "OK"
    assert r["result_source"] == "RESULTS_PAGE"


def test_same_teams_from_another_fixture_are_rejected_by_start_time(env):
    """Identity: same names, different fixture start time → the render is
    not this game's result.  (Team names alone prove nothing on a virtual
    league that replays the same two teams back to back.)"""
    env["add"]()
    other_day = (env["now"] - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    stale = page(HOME, AWAY, 95, 95,
                 [(25, 25), (20, 22), (22, 21), (28, 27)], other_day)

    stats = env["reconciler"]({GID: stale}).run_pass()

    assert stats["verified"] == 0 and stats["rejected"] == 1
    checks = json.loads(env["audit"]()[0]["checks_json"])
    assert checks["teams_match"] is True
    assert checks["start_time_consistent"] is False


def test_a_fixture_starting_after_our_last_observation_is_rejected(env):
    """THE replay-league identity case, from live evidence (2026-09-24).

    Game 31013207 was last observed at 18:44:34 and its results page
    rendered a fixture starting 19:14:45 — the NEXT instance of the same
    two teams, 61 minutes after our first_seen and inside the generous
    2-hour first_seen skew, so name + skew alone accepted it.  A result
    can never come from a fixture that STARTED after we stopped watching
    our game: that is the decisive rejection.
    """
    env["add"]()
    now = env["now"]
    env["snap"](GID, now - timedelta(minutes=50), 20, 18, 1, "05:00")
    env["snap"](GID, now - timedelta(minutes=10), 77, 79, 3, "01:00")
    # the page's fixture began 30 min AFTER our game's last observation
    later = (now + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
    foreign = page(HOME, AWAY, 101, 111,
                   [(29, 23), (25, 27), (26, 31), (21, 30)], later)

    stats = env["reconciler"]({GID: foreign}).run_pass()

    assert stats["verified"] == 0 and stats["rejected"] == 1
    checks = json.loads(env["audit"]()[0]["checks_json"])
    assert checks["teams_match"] is True            # names proved nothing
    assert checks["start_time_consistent"] is False
    assert checks["render_complete"] is True        # and the render LOOKED fine
    assert env["row"]()["final_home"] is None


def test_history_check_is_inapplicable_without_any_capture(env):
    """A game the collector never scored still reconciles from the page:
    the history guard cannot apply and says so (None, not False)."""
    env["add"]()
    stats = env["reconciler"]({GID: _verified_page(env)}).run_pass()
    assert stats["verified"] == 1
    checks = json.loads(env["audit"]()[0]["checks_json"])
    assert checks["history_consistent"] is None
    assert checks["observed_snapshots"] == 0


# ══════════════════════════════════════════════════════════════════════
# C. THE STATE MACHINE — retry, never a silent or terminal absence
# ══════════════════════════════════════════════════════════════════════

def test_a_destroyed_verification_is_re_attempted_and_restored(env):
    """The defect this directive exists for: a page verification whose OK
    row was destroyed must re-enter the candidate set (a 'VERIFIED' state
    row is NOT terminal on its own — it is terminal only while its OK row
    is persisted) and the final must come back."""
    env["add"]()
    now = env["now"]
    env["snap"](GID, now - timedelta(minutes=50), 20, 18, 1, "05:00")
    env["snap"](GID, now - timedelta(minutes=30), 55, 52, 3, "05:00")
    pages = {GID: _verified_page(env)}

    first = env["reconciler"](pages).run_pass()
    assert first["verified"] == 1
    assert env["row"]()["final_result_status"] == "OK"

    # the clobber, exactly as the pre-fix writers left it behind
    c = sqlite3.connect(str(env["db"]))
    c.execute(
        """UPDATE game_results SET final_home=NULL, final_away=NULL,
               final_total=NULL, final_result_status='NEEDS_RECONCILIATION',
               result_at=?
           WHERE source_game_id=?""",
        (env["iso"](now - timedelta(minutes=10)), GID))
    c.commit()
    c.close()
    assert env["row"]()["final_home"] is None

    second = env["reconciler"](pages).run_pass()
    assert second["scanned"] == 1, second          # re-entered the queue
    assert second["verified"] == 1
    r = env["row"]()
    assert (r["final_home"], r["final_away"], r["final_total"]) == (95, 95, 190)
    assert r["final_result_status"] == "OK"
    # the audit row is the idempotence key — a re-verification never
    # duplicates the attempt history
    assert len(env["audit"]()) == 1


def test_an_intact_verdict_is_not_re_attempted(env):
    """Idempotence is preserved: while the OK row stands, the game is not
    a candidate and no page fetch happens."""
    env["add"]()
    now = env["now"]
    env["snap"](GID, now - timedelta(minutes=50), 20, 18, 1, "05:00")
    fetch = _FakeFetcher({GID: _verified_page(env)})
    r = ResultReconciler(env["db"], fetcher=fetch)
    assert r.run_pass()["verified"] == 1
    calls_after_first = len(fetch.calls)

    again = r.run_pass()

    assert again["scanned"] == 0 and again["verified"] == 0
    assert len(fetch.calls) == calls_after_first


def test_a_rejected_render_is_retryable(env):
    """REJECTED is a RETRY, not a verdict: the next render (a complete
    one) verifies.  Otherwise the day's backlog could be lost to one bad
    frame."""
    env["add"]()
    now = env["now"]
    env["snap"](GID, now - timedelta(minutes=50), 20, 18, 1, "05:00")
    env["snap"](GID, now - timedelta(minutes=30), 55, 52, 3, "05:00")
    partial = page(HOME, AWAY, 66, 59, [(20, 18), (21, 19), (25, 22)],
                   env["page_start"])

    first = env["reconciler"]({GID: partial}).run_pass()
    assert first["rejected"] == 1
    second = env["reconciler"]({GID: _verified_page(env)}).run_pass()
    assert second["scanned"] == 1 and second["verified"] == 1
    assert env["row"]()["final_result_status"] == "OK"


def test_a_failed_template_is_exhausted_not_retried(env):
    """The results page has no result for this game (filters-only
    template): re-fetching re-reads the same cached render, so the game
    is recorded as exhausted — visibly, never as 'no result'."""
    env["add"]()
    first = env["reconciler"]({GID: FAILED_TEMPLATE}).run_pass()
    assert first["template_failed"] == 1
    row = env["row"]()
    assert row["final_result_status"] == STATUS_NEEDS_RECONCILIATION
    assert row["final_home"] is None

    second = env["reconciler"]({GID: _verified_page(env)}).run_pass()
    assert second["scanned"] == 0, second      # exhausted by design


def test_an_unresolved_game_always_has_an_explicit_row(env):
    """A disappearing market is never a missing row: the unresolved game
    is enumerated with a reason, and no terminal 'NO FINAL' state exists
    anywhere in the pipeline's vocabulary."""
    env["add"]()
    stats = env["reconciler"]({GID: None}).run_pass()   # fetch fails
    assert stats["scanned"] == 1
    row = env["row"]()
    assert row is not None
    assert row["final_result_status"] == STATUS_NEEDS_RECONCILIATION
    missing = env["reconciler"]({}).audit_missing(limit=10)
    assert [m["source_game_id"] for m in missing] == [GID]
    assert "NO_FINAL" not in json.dumps(row)


def test_audit_missing_names_the_reason_it_is_unresolved(env):
    """The audit says WHY each game is still open — 'pending' is not a
    reason, it is a confession."""
    env["add"]()
    now = env["now"]
    env["snap"](GID, now - timedelta(minutes=30), 60, 58, 3, "05:00")
    env["reconciler"]({GID: page(
        HOME, AWAY, 55, 50, [(14, 12), (13, 13), (15, 14), (13, 11)],
        env["page_start"])}).run_pass()
    reason = env["reconciler"]({}).audit_missing(limit=5)[0]["reason"]
    assert "impossible final" in reason

    env["add"]("91000002")
    env["reconciler"]({"91000002": page(
        HOME, AWAY, 66, 59, [(20, 18), (21, 19), (25, 22)],
        env["page_start"])}).run_pass()
    reasons = {m["source_game_id"]: m["reason"]
               for m in env["reconciler"]({}).audit_missing(limit=5)}
    assert "incomplete render" in reasons["91000002"]


# ── policy unit tests (the shared definition, no DB) ─────────────────

def test_history_bound_and_disagreement_helpers():
    rows = [{"home_score": 10, "away_score": 8},
            {"home_score": 30, "away_score": 25},
            {"home_score": 28, "away_score": 30}]      # glitch row
    assert policy.history_bound(rows) == (30, 30)
    assert policy.history_endpoint(rows) == (28, 30)
    assert policy.history_disagrees(rows, 30, 30) is True
    assert policy.history_disagrees(rows, None, 30) is False
    assert policy.history_bound([]) is None


def test_validate_page_result_requires_all_four_quarters():
    base = {"home_score": 80, "away_score": 70,
            "quarter_scores": [(20, 18), (20, 17), (20, 18), (20, 17)],
            "parse_quality": "full"}
    ok = policy.validate_page_result(base, [{"home_score": 40,
                                             "away_score": 35}])
    assert ok["passed"] is True
    short = dict(base, quarter_scores=[(20, 18), (20, 17)], parse_quality="partial")
    assert policy.validate_page_result(short, [])["passed"] is False


# ── the AUTHORITATIVE feed (swarm — the results page's own source) ───
#
# The PokerBet results page is a rendering of a swarm feed.  Reading that
# feed directly is what the reconciler now prefers, because the DOM carries
# no game id at all (identity on the DOM path can only be guessed from team
# names + footer time), while the feed matches game_id exactly and returns
# a structured four-quarter score line.

def _frame(env, h: int = 95, a: int = 95, *, home: str = None,
           away: str = None, quarters=None, start_dt=None, game_id: str = GID):
    """One real swarm reply frame -> normalize_result() output."""
    from blm_v4.swarm_results import normalize_result
    if quarters is None:
        base_h, base_a = h // 4, a // 4
        quarters = [(base_h, base_a), (base_h, base_a), (base_h, base_a),
                    (h - 3 * base_h, a - 3 * base_a)]
    qs = ", ".join(f"{x}:{y}" for x, y in quarters)
    ts = int((start_dt or (env["now"] - timedelta(hours=1))).timestamp())
    return normalize_result({
        "game_id": game_id,
        "scores": f"{h}:{a}({qs})",
        "team1_name": home if home is not None else HOME,
        "team2_name": away if away is not None else AWAY,
        "team1_id": 1423916, "team2_id": 1423878,
        "date": ts,
        "competition_name": "Betual NBA", "region_name": "Virtual Matches",
        "sport_id": "3", "sport_alias": "Basketball"})


class _FakeSwarm:
    """Stands in for SwarmResultsClient (gid -> normalized feed result)."""

    def __init__(self, results: dict):
        self.results, self.calls = results, []

    def fetch_results(self, ids):
        self.calls.append([str(i) for i in ids])
        return {str(g): self.results.get(str(g)) for g in ids}


def test_the_authoritative_feed_result_is_persisted(env):
    """The feed's four-quarter final is taken, stored as a verified result
    and marked with the results-source provenance the downstream readers
    treat as authoritative."""
    env["add"]()
    now = env["now"]
    env["snap"](GID, now - timedelta(minutes=50), 20, 18, 1, "05:00")
    env["snap"](GID, now - timedelta(minutes=30), 55, 52, 3, "05:00")

    swarm = _FakeSwarm({GID: _frame(env, 95, 95)})
    stats = env["reconciler"]({}, swarm=swarm).run_pass()

    assert stats["verified"] == 1
    assert stats["swarm_lookups"] == 1 and stats["swarm_results"] == 1
    row = env["row"]()
    assert row["final_result_status"] == "OK"
    assert (row["final_home"], row["final_away"]) == (95, 95)
    assert row["final_total"] == 190
    assert row["result_source"] == "RESULTS_PAGE"
    attempt = env["audit"]()[0]
    assert attempt["outcome"] == "VERIFIED"
    checks = json.loads(attempt["checks_json"])
    assert checks["source"] == "swarm_feed"
    assert checks["game_id_match"] is True
    assert checks["feed_scores"] == "95:95(23:23, 23:23, 23:23, 26:26)"
    assert checks["render_complete"] is True


def test_the_feed_is_preferred_over_the_results_page_dom(env):
    """When both sources answer, the feed wins — and the page is not even
    fetched (the DOM path cannot prove identity; the feed can)."""
    env["add"]()
    now = env["now"]
    env["snap"](GID, now - timedelta(minutes=30), 55, 52, 3, "05:00")
    pages = {GID: page(HOME, AWAY, 200, 200,
                       [(50, 50), (50, 50), (50, 50), (50, 50)],
                       env["page_start"])}

    swarm = _FakeSwarm({GID: _frame(env, 95, 95)})
    r = env["reconciler"](pages, swarm=swarm)
    stats = r.run_pass()

    assert stats["verified"] == 1
    assert r._fetcher.calls == []                    # DOM never consulted
    row = env["row"]()
    assert (row["final_home"], row["final_away"]) == (95, 95)


def test_a_feed_result_from_another_fixture_instance_is_refused(env):
    """A virtual league reuses one game id for a fixture slot replayed all
    day.  A reply whose fixture STARTED AFTER our last observation is the
    next replay of the same teams and must not become this game's final."""
    env["add"]()
    now = env["now"]
    env["snap"](GID, now - timedelta(minutes=50), 20, 18, 1, "05:00")
    env["snap"](GID, now - timedelta(minutes=30), 55, 52, 3, "05:00")

    later = now + timedelta(minutes=30)              # after last_seen
    swarm = _FakeSwarm({GID: _frame(env, 101, 111, start_dt=later)})
    stats = env["reconciler"]({}, swarm=swarm).run_pass()

    assert stats["verified"] == 0 and stats["rejected"] == 1
    checks = json.loads(env["audit"]()[0]["checks_json"])
    assert checks["teams_match"] is True             # names proved nothing
    assert checks["start_time_consistent"] is False
    assert checks["render_complete"] is True         # and the render looked fine
    assert env["row"]()["final_home"] is None


def test_a_feed_result_for_a_different_game_id_is_refused(env):
    env["add"]()
    env["snap"](GID, env["now"] - timedelta(minutes=30), 55, 52, 3, "05:00")
    swarm = _FakeSwarm({GID: _frame(env, 95, 95, game_id="99999999")})
    stats = env["reconciler"]({}, swarm=swarm).run_pass()

    assert stats["verified"] == 0 and stats["rejected"] == 1
    checks = json.loads(env["audit"]()[0]["checks_json"])
    assert checks["game_id_match"] is False


def test_feed_absence_falls_back_to_the_results_page(env):
    """No result in the feed for this id is an ABSENCE, not a verdict — the
    page path still runs."""
    env["add"]()
    now = env["now"]
    env["snap"](GID, now - timedelta(minutes=50), 20, 18, 1, "05:00")
    env["snap"](GID, now - timedelta(minutes=30), 55, 52, 3, "05:00")
    pages = {GID: _verified_page(env)}

    r = env["reconciler"](pages, swarm=_FakeSwarm({GID: None}))
    stats = r.run_pass()

    assert stats["verified"] == 1
    assert r._fetcher.calls == [GID]                 # DOM path used
    assert env["row"]()["final_home"] == 95

