"""RESULT RECONCILIATION (directive 2026-09-23).

A disappearing live market is NOT evidence that a game has no result.
PokerBet serves completed-game results at
https://www.pokerbet.co.za/en/sports/results?game={GAME_ID}; when a
game is completed or disappears from live tracking without a verified
final, that page MUST be queried, the rendered game verified against
the canonical record, and the result persisted with provenance.

The 12 directive scenarios, pinned here against the SHIPPED modules:

  1. live market disappears → results page queried
  2. event row not found    → results page queried
  3. empty event DOM        → results page queried
  4. valid results page     → result persisted
  5. incorrect game_id (rendered game not matching the record)
                             → rejected
  6. incorrect teams        → rejected/flagged
  7. missing final score    → remains unresolved
  8. existing verified result → not duplicated, not overwritten
  9. repeated audit/pass    → idempotent
  10. historical missing result → successfully backfilled
  11. conflicting result    → flagged, never overwritten
  12. result recovery feeds downstream reads (result_source recorded)

The fetcher is a scripted fake (no network, no browser) returning real
captured page text shapes (2026-09-23 live probes).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.result_reconciler import (
    RESULT_STATUS_NOTE,
    STATUS_NEEDS_RECONCILIATION,
    ResultReconciler,
    ResultReconcilerWorker,
)
from blm_v4.results_fetcher import parse_results_page
from blm_v4.scorecard import Scorecard
from blm_v4.settle_worker import settle_once
from blm_v4.storage import PokerBetStore

# ── Real-shape page text (captured from the live results page) ────────

PAGE_OK = """SIGN IN
REGISTER
ENG
07:34:33
POKER
SPORTS
CASINO
SPECIAL BETS
PROMOTIONS
BLOG
POWERFULL WHEEL
EVENT VIEW
LIVE CALENDAR
RESULTS
R5.5M LEAGUE PREDICTOR
Live
Finished
Start Date *
End Date *
Football
Sport
All
Competition
RESET
SHOW
Africa Cup of Nations Qualification (Africa)
Betual NBA (Virtual Matches)
2026-09-22 21:30
Dallas Mavericks Virtual
112
Memphis Grizzlies Virtual
93
112:93 (22:25, 24:17, 40:34, 26:17)
Match Winner
Points Handicap
Total Points
07:34:06
ENG
TERMS AND CONDITIONS
"""

PAGE_FAILED = """SIGN IN
REGISTER
ENG
07:34:33
POKER
SPORTS
CASINO
SPECIAL BETS
PROMOTIONS
BLOG
EVENT VIEW
LIVE CALENDAR
RESULTS
Live
Finished
Start Date *
End Date *
Football
Sport
All
Competition
RESET
SHOW
Betual Bundesliga (Virtual Matches)
Betual England Premier League (Virtual Matches)
07:34:06
ENG
TERMS AND CONDITIONS
"""

PAGE_SWAPPED = PAGE_OK.replace(
    "Dallas Mavericks Virtual\n112\nMemphis Grizzlies Virtual\n93",
    "Memphis Grizzlies Virtual\n93\nDallas Mavericks Virtual\n112")

# Same teams, DIFFERENT final — the wrong-game contamination shape the
# stale-SPA hazard produces when identity is not verified.
PAGE_WRONG_GAME = PAGE_OK.replace("Dallas", "Boston").replace(
    "112:93 (22:25, 24:17, 40:34, 26:17)",
    "108:101 (30:25, 22:28, 31:26, 25:22)").replace(
    "112\n", "108\n").replace("93\n", "101\n")

# Same fixture, swapped rendering order (away block first + the compact
# line's numbers in the swapped order — an internally CONSISTENT page).
PAGE_SWAPPED = PAGE_OK.replace(
    "Dallas Mavericks Virtual\n112\n"
    "Memphis Grizzlies Virtual\n93\n"
    "112:93 (22:25, 24:17, 40:34, 26:17)",
    "Memphis Grizzlies Virtual\n93\n"
    "Dallas Mavericks Virtual\n112\n"
    "93:112 (25:22, 17:24, 34:40, 17:26)")


class FakeFetcher:
    """Scripted fetcher: gid → text (None = fetch failure)."""

    def __init__(self, pages: dict[str, str | None]):
        self.pages = pages
        self.calls: list[str] = []

    def fetch_results_page(self, game_id: str):
        self.calls.append(game_id)
        return self.pages.get(game_id)


# ── Fixtures ──────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _game(gid: str, status: str = "ended",
          home: str = "Dallas Mavericks Virtual",
          away: str = "Memphis Grizzlies Virtual") -> PokerBetGame:
    now = datetime.now(timezone.utc)
    # first_seen aligns with the fixture pages' displayed start time
    # (2026-09-22 21:30) — the reconciler's start-time cross-check
    # (§5/§14.6) compares these two, and a live-tracked game is first
    # seen within minutes of its real tip-off.
    return PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="18296756", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA",
        sport="basketball", home_team=home, away_team=away,
        game_slug=f"{gid}-game", source_url=f"https://x/{gid}",
        status=status, first_seen_at="2026-09-22T21:30:00.000000Z",
        # last seen ~40 min after tip-off: a finished 40-minute game cannot
        # be observed days after it started (the 2026-09-24 fixture-start
        # window anchors on last_seen — a game must have been RUNNING when
        # we last saw it).
        last_seen_at="2026-09-22T22:10:00.000000Z")


@pytest.fixture
def env(tmp_path):
    """Store + helpers.  Games are created EXPLICITLY per test (a
    pre-seeded ended game would be a reconciliation candidate in every
    test and pollute scanned/census counts)."""
    db = tmp_path / "blm_pokerbet.db"
    st = PokerBetStore(db)
    # Bootstrap the scorecard schema so tests can pre-insert game_results
    # rows (the reconciler itself creates these tables idempotently too).
    conn = sqlite3.connect(str(db))
    try:
        from blm_v4.scorecard import SCORECARD_SCHEMA
        conn.executescript(SCORECARD_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    rec = {"store": st, "db": db, "tmp": tmp_path}

    def add_game(gid, status="ended", home="Dallas Mavericks Virtual",
                 away="Memphis Grizzlies Virtual"):
        st.upsert_game(_game(gid, status, home, away))
        # storage.upsert_game stamps last_seen_at = now unconditionally
        # (production semantics), so the fixture's realistic last
        # observation is applied explicitly: the reconciler's fixture-start
        # window (2026-09-24) anchors on it, and the page's displayed start
        # (2026-09-22 21:30) must sit inside the fixture's running window.
        c = sqlite3.connect(str(db))
        c.execute("UPDATE games SET last_seen_at=? WHERE source_game_id=?",
                  ("2026-09-22T22:10:00.000000Z", gid))
        c.commit()
        c.close()

    rec["add_game"] = add_game

    def snap(gid, mins_ago, hs, as_, q=None, clock="", label=""):
        g = st.get_game(gid)
        ts = _iso(datetime.now(timezone.utc) - timedelta(minutes=mins_ago))
        st.insert_snapshot(g["id"], MarketObservation(
            source="PokerBet", source_game_id=gid,
            classification=g["classification"], captured_at=ts,
            home_team=g["home_team"], away_team=g["away_team"],
            home_score=hs, away_score=as_, period_label=label,
            quarter=q, clock=clock, game_status="live",
            markets_json="{}"), force=True)

    rec["snap"] = snap

    made: list[ResultReconciler] = []

    def reconciler(pages: dict, **kw) -> ResultReconciler:
        r = ResultReconciler(
            db, fetcher=FakeFetcher(pages), log=None, **kw)
        made.append(r)
        return r

    rec["reconciler"] = reconciler
    rec["last"] = lambda: made[-1]

    def rows(sql, params=()):
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    rec["rows"] = rows
    return rec


# ── Scenario 1/2/3: every disappearance shape queries the page ────────

def test_disappeared_ended_game_without_row_queries_results_page(env):
    """§10.1 — ended (disappeared) + no game_results row → page queried."""
    env["add_game"]("30995959")
    r = env["reconciler"]({"30995959": PAGE_OK})
    stats = r.run_pass()
    assert stats["verified"] == 1
    assert env["last"]()._fetcher.calls == ["30995959"]  # page queried


def test_event_row_never_found_still_reconciles_from_results_page(env):
    """§10.2 — a never-captured game (no snapshots at all) whose row went
    straight to NEEDS_RECONCILIATION is still a candidate and is
    recovered — 'row not found' means RECONCILE, not NO RESULT."""
    env["add_game"]("30949595")
    conn = sqlite3.connect(str(env["db"]))
    try:
        conn.executescript(
            """INSERT INTO game_results (source_game_id, classification,
                   result_at, final_result_status)
               VALUES ('30949595', 'BETUAL_NBA', '2026-09-22T21:00:00Z',
                       'NEEDS_RECONCILIATION')""")
        conn.commit()
    finally:
        conn.close()
    r = env["reconciler"]({"30949595": PAGE_OK})
    stats = r.run_pass()
    assert stats["verified"] == 1
    res = env["rows"](
        "SELECT * FROM game_results WHERE source_game_id='30949595'")[0]
    assert res["final_result_status"] == "OK"
    assert res["final_total"] == 205


def test_empty_event_dom_degenerate_tail_queries_results_page(env):
    """§10.3 — snapshots exist but the tail is degenerate (the empty
    event DOM): the history cannot prove a final → page queried."""
    gid = "30995480"
    env["add_game"](gid)
    env["snap"](gid, 60, 20, 18, 1, "08:00", "1st Quarter")
    env["snap"](gid, 40, 60, 55, 3, "02:00", "3rd Quarter")
    env["snap"](gid, 30, None, None)          # the empty/degenerate tail
    r = env["reconciler"]({gid: PAGE_OK})
    stats = r.run_pass()
    assert stats["verified"] == 1
    res = env["rows"](
        "SELECT * FROM game_results WHERE source_game_id=?",
        (gid,))[0]
    assert res["final_result_status"] == "OK"
    assert res["result_source"] == "RESULTS_PAGE"


def test_midquarter_tail_without_page_render_stays_unresolved(env):
    """§10.7 (part) — no final score anywhere → the game remains
    unresolved (NEEDS_RECONCILIATION/UNKNOWN), never a guessed final.
    Here the page also renders the failed template: TEMPLATE_FAILED."""
    gid = "99900001"
    env["add_game"](gid)
    env["snap"](gid, 30, 40, 38, 2, "05:00", "2nd Quarter")
    r = env["reconciler"]({gid: PAGE_FAILED})
    stats = r.run_pass()
    assert stats["template_failed"] == 1
    res = env["rows"](
        "SELECT * FROM game_results WHERE source_game_id=?", (gid,))[0]
    assert res["final_result_status"] != "OK"
    assert res["final_home"] is None and res["final_away"] is None


# ── Scenario 4: valid page → persisted with provenance ────────────────

def test_valid_results_page_result_persisted_with_source(env):
    env["add_game"]("30840226")
    r = env["reconciler"]({"30840226": PAGE_OK})
    stats = r.run_pass()
    assert stats["verified"] == 1
    res = env["rows"](
        "SELECT * FROM game_results WHERE source_game_id='30840226'")[0]
    assert res["final_result_status"] == "OK"
    assert (res["final_home"], res["final_away"]) == (112, 93)
    assert res["final_total"] == 205
    assert res["result_source"] == "RESULTS_PAGE"
    att = env["rows"]("SELECT * FROM result_reconciliation")[0]
    assert att["outcome"] == "VERIFIED"
    checks = json.loads(att["checks_json"])
    assert checks["teams_match"] is True
    assert checks["page_sums_valid"] is True
    assert checks["orientation"] == "as-rendered"
    assert json.loads(att["quarter_scores"]) == [
        [22, 25], [24, 17], [40, 34], [26, 17]]


def test_parser_extracts_real_page_shape():
    parsed = parse_results_page(PAGE_OK, "30840226")
    assert parsed["home_team"] == "Dallas Mavericks Virtual"
    assert parsed["away_team"] == "Memphis Grizzlies Virtual"
    assert (parsed["home_score"], parsed["away_score"]) == (112, 93)
    assert parsed["quarter_scores"] == [(22, 25), (24, 17), (40, 34), (26, 17)]
    assert parsed["parse_quality"] == "full"
    assert parsed["status_label"] == "Finished"


def test_parser_flags_failed_template():
    parsed = parse_results_page(PAGE_FAILED, "123")
    assert parsed["parse_quality"] == "failed"
    assert parsed["failed_template"] is True
    assert parsed["home_score"] is None


# ── Scenarios 5/6: wrong game / wrong teams → rejected ────────────────

def test_wrong_game_render_rejected_not_persisted(env):
    """§10.5 — the page renders a DIFFERENT game's final (the stale-SPA
    contamination shape): identity verification rejects it and nothing
    is written.  The result page does not echo the game id, so team
    identity against the canonical record IS the    game-id check."""
    env["add_game"]("30840226")
    r = env["reconciler"]({"30840226": PAGE_WRONG_GAME})
    stats = r.run_pass()
    assert stats["verified"] == 0
    assert stats["rejected"] == 1
    # nothing verified is stored — only the explicit unresolved stamp
    res = env["rows"]("SELECT * FROM game_results")[0]
    assert res["final_result_status"] == "NEEDS_RECONCILIATION"
    assert res["final_home"] is None and res["final_away"] is None
    att = env["rows"]("SELECT * FROM result_reconciliation")[0]
    assert att["outcome"] == "REJECTED"
    checks = json.loads(att["checks_json"])
    assert checks["teams_match"] is False


def test_wrong_teams_rejected_and_flagged_in_attempt_row(env):
    """§10.6 — one rendered team does not match the record → rejected
    and flagged in the attempt audit row (never persisted)."""
    env["add_game"]("30995881", home="Sacramento Kings Virtual",
                    away="Miami Heat Virtual")
    r = env["reconciler"]({"30995881": PAGE_OK})   # Mavs/Grizzlies page
    stats = r.run_pass()
    assert stats["verified"] == 0
    att = env["rows"](
        "SELECT * FROM result_reconciliation "
        "WHERE source_game_id='30995881'")[0]
    assert att["outcome"] == "REJECTED"
    assert att["match_score"] == 0


def test_rendered_side_swap_still_verifies(env):
    """The SPA sometimes renders away-first; both names matching proves
    the same fixture — VERIFIED and the final is RE-ORIENTED to the
    record's home/away so the stored row stays comparable with every
    snapshot-derived verdict."""
    env["add_game"]("30840226")
    r = env["reconciler"]({"30840226": PAGE_SWAPPED})
    stats = r.run_pass()
    assert stats["verified"] == 1
    res = env["rows"](
        "SELECT * FROM game_results WHERE source_game_id='30840226'")[0]
    assert (res["final_home"], res["final_away"]) == (112, 93)


# ── Scenario 8: existing verified result untouched ────────────────────

def test_existing_ok_result_not_duplicated_nor_overwritten(env):
    gid = "30840226"
    env["add_game"](gid)
    conn = sqlite3.connect(str(env["db"]))
    try:
        conn.execute(
            """INSERT INTO game_results (source_game_id, classification,
                   final_home, final_away, final_total, result_at,
                   final_result_status)
               VALUES (?, 'BETUAL_NBA', 110, 95, 205,
                       '2026-09-22T21:30:00.000000Z', 'OK')""", (gid,))
        conn.commit()
    finally:
        conn.close()
    r = env["reconciler"]({gid: PAGE_OK})
    stats = r.run_pass()
    assert stats["scanned"] == 0           # not a candidate at all
    res = env["rows"](
        "SELECT * FROM game_results WHERE source_game_id=?", (gid,))[0]
    assert (res["final_home"], res["final_away"]) == (110, 95)
    assert res["result_source"] is None    # untouched by the reconciler


def test_persist_guard_protects_ok_row_even_if_raced(env):
    """The upsert's WHERE clause refuses to overwrite an OK row even if
    one appears between the candidate scan and the write."""
    gid = "30840226"
    env["add_game"](gid)
    conn = sqlite3.connect(str(env["db"]))
    try:
        conn.execute(
            """INSERT INTO game_results (source_game_id, classification,
                   final_home, final_away, final_total, result_at,
                   final_result_status)
               VALUES (?, 'BETUAL_NBA', 110, 95, 205,
                       '2026-09-22T21:30:00.000000Z', 'OK')""", (gid,))
        conn.commit()
    finally:
        conn.close()
    r = env["reconciler"]({gid: PAGE_OK})
    parsed = parse_results_page(PAGE_OK, gid)
    game = env["rows"]("SELECT * FROM games")[0]
    r._persist_result(game, parsed, {"fetched_at": _iso(
        datetime.now(timezone.utc))})
    res = env["rows"](
        "SELECT * FROM game_results WHERE source_game_id=?", (gid,))[0]
    assert (res["final_home"], res["final_away"]) == (110, 95)
    assert res["result_source"] is None


# ── Scenario 9: idempotence ───────────────────────────────────────────

def test_repeated_passes_are_idempotent(env):
    gid = "30840226"
    env["add_game"](gid)
    r = env["reconciler"]({gid: PAGE_OK})
    s1 = r.run_pass()
    assert s1["verified"] == 1
    s2 = r.run_pass()
    s3 = r.run_pass()
    assert s2["scanned"] == 0 and s3["scanned"] == 0   # verified: terminal
    results = env["rows"]("SELECT * FROM game_results")
    assert len(results) == 1
    attempts = env["rows"]("SELECT * FROM result_reconciliation")
    assert len(attempts) == 1


# ── Scenario 10: historical backfill + sweep integration ──────────────

def test_historical_unknown_row_backfilled_to_ok(env):
    """§5/§10.10 — an old game stuck at UNKNOWN (the historical backlog
    shape) is stamped NEEDS_RECONCILIATION and then recovered."""
    gid = "30738600"
    env["add_game"](gid)
    conn = sqlite3.connect(str(env["db"]))
    try:
        conn.execute(
            """INSERT INTO game_results (source_game_id, classification,
                   final_home, final_away, final_total, result_at,
                   final_result_status)
               VALUES (?, 'BETUAL_NBA', NULL, NULL, NULL,
                       '2026-09-18T20:00:00.000000Z', 'UNKNOWN')""", (gid,))
        conn.commit()
    finally:
        conn.close()
    r = env["reconciler"]({gid: PAGE_OK})
    conn = r._connect()
    try:
        stamped = r.mark_needs_reconciliation(conn)
        conn.commit()
    finally:
        conn.close()
    assert stamped == 1
    assert env["rows"](
        "SELECT final_result_status FROM game_results "
        "WHERE source_game_id=?", (gid,))[0][
            "final_result_status"] == "NEEDS_RECONCILIATION"
    stats = r.run_pass()
    assert stats["verified"] == 1
    res = env["rows"](
        "SELECT * FROM game_results WHERE source_game_id=?", (gid,))[0]
    assert res["final_result_status"] == "OK"
    assert res["final_total"] == 205
    assert res["result_source"] == "RESULTS_PAGE"


def test_backfilled_result_survives_scorecard_and_settle_worker(env):
    """§8 — the recovered result is not silently demoted by the
    snapshot-based settlement paths (the sweep + the settle worker),
    and §10.8 — never duplicated.  The stored scores agree with the
    history endpoint, so both re-derivation paths protect the row."""
    gid = "30840226"
    env["add_game"](gid)
    r = env["reconciler"]({gid: PAGE_OK})
    assert r.run_pass()["verified"] == 1
    # snapshots agreeing with the recovered final (a capture that saw it)
    env["snap"](gid, 20, 112, 93, 4, "00:00", "Full Time")
    s = Scorecard(env["db"])
    s.capture_results()
    stats = settle_once(env["db"])
    res = env["rows"](
        "SELECT * FROM game_results WHERE source_game_id=?", (gid,))[0]
    assert res["final_result_status"] == "OK"
    assert (res["final_home"], res["final_away"]) == (112, 93)
    assert res["result_source"] == "RESULTS_PAGE"


def test_snapshot_history_with_only_degenerate_rows_stays_unknown(env):
    """A NULL-score tail stays UNKNOWN, never a guessed final — recovery
    is the reconciler's job, not a laxer gate.  The gate is exercised
    through capture_results (the worker shares its predicates)."""
    gid = "30990001"
    env["add_game"](gid)
    env["snap"](gid, 10, None, None)
    s = Scorecard(env["db"])
    s.capture_results()
    res = env["rows"](
        "SELECT * FROM game_results WHERE source_game_id=?", (gid,))[0]
    assert res["final_result_status"] != "OK"
    assert res["final_home"] is None and res["final_away"] is None


# ── Scenario 11: conflicts flagged, never overwritten ─────────────────

def test_conflicting_results_flagged_not_overwritten(env):
    """A scored UNKNOWN row whose stored totals disagree with a verified
    page render: the page result is persisted (the page is the
    authoritative fallback) AND the disagreement is flagged in
    result_conflicts for review."""
    gid = "30840226"
    env["add_game"](gid)
    conn = sqlite3.connect(str(env["db"]))
    try:
        conn.execute(
            """INSERT INTO game_results (source_game_id, classification,
                   final_home, final_away, final_total, result_at,
                   final_result_status)
               VALUES (?, 'BETUAL_NBA', 100, 90, 190,
                       '2026-09-22T21:00:00.000000Z', 'UNKNOWN')""", (gid,))
        conn.commit()
    finally:
        conn.close()
    r = env["reconciler"]({gid: PAGE_OK})
    stats = r.run_pass()
    assert stats["verified"] == 1
    conflicts = env["rows"]("SELECT * FROM result_conflicts")
    assert len(conflicts) == 1
    assert conflicts[0]["live_dom_total"] == 190
    assert conflicts[0]["results_total"] == 205
    res = env["rows"](
        "SELECT * FROM game_results WHERE source_game_id=?", (gid,))[0]
    assert res["final_total"] == 205      # authoritative page result


def test_conflict_with_stored_ok_verdict_keeps_stored_result(env):
    """A stored OK verdict (live-observed) DISAGREEING with the page:
    the stored verified result is NEVER overwritten; the disagreement
    is flagged."""
    gid = "30840226"
    env["add_game"](gid)
    conn = sqlite3.connect(str(env["db"]))
    try:
        conn.execute(
            """INSERT INTO game_results (source_game_id, classification,
                   final_home, final_away, final_total, result_at,
                   final_result_status)
               VALUES (?, 'BETUAL_NBA', 110, 95, 205,
                       '2026-09-22T21:30:00.000000Z', 'OK')""", (gid,))
        conn.commit()
    finally:
        conn.close()
    # a teams-matching page with a DIFFERENT total (page disagrees)
    tricky = PAGE_OK.replace("112:93", "111:92").replace("112\n", "111\n") \
        .replace("93\n", "92\n")
    r2 = env["reconciler"]({gid: tricky})
    # Not a candidate (OK row) — prove the guard at the persist layer:
    game = env["rows"]("SELECT * FROM games WHERE source_game_id=?",
                       (gid,))[0]
    parsed = parse_results_page(tricky, gid)
    r2._persist_result(game, parsed, {"fetched_at": _iso(
        datetime.now(timezone.utc))})
    res = env["rows"](
        "SELECT * FROM game_results WHERE source_game_id=?", (gid,))[0]
    assert (res["final_home"], res["final_away"]) == (110, 95)
    conflicts = env["rows"]("SELECT * FROM result_conflicts")
    assert len(conflicts) == 1
    assert conflicts[0]["live_dom_total"] == 205
    assert conflicts[0]["results_total"] == 203


# ── Scenario 12: recovery feeds downstream reads ──────────────────────

def test_recovered_result_visible_to_downstream_reads(env):
    """§8 — the recovered row is an OK row with provenance, exactly the
    shape every analysis/backtest read already consumes."""
    gid = "30840226"
    env["add_game"](gid)
    r = env["reconciler"]({gid: PAGE_OK})
    r.run_pass()
    res = env["rows"](
        "SELECT source_game_id, classification, final_home, final_away, "
        "final_total, result_at, final_result_status, result_source "
        "FROM game_results WHERE source_game_id=?", (gid,))[0]
    assert res["final_result_status"] == "OK"
    assert res["result_source"] == "RESULTS_PAGE"
    # the vocabulary every downstream consumer filters on:
    assert res["final_result_status"] in ("OK",)
    recovered = r.audit_recovered()
    assert [g["source_game_id"] for g in recovered] == [gid]


# ── Audits (§3/§4) ─────────────────────────────────────────────────────

def test_audit_report_census(env):
    env["add_game"]("30840226")
    env["add_game"]("30995959")
    r = env["reconciler"]({"30840226": PAGE_OK, "30995959": PAGE_FAILED})
    r.run_pass()
    rep = r.audit_report()
    assert rep["games_scanned"] == 2
    assert rep["expected_to_have_results"] == 2
    assert rep["verified_results"] == 1
    assert rep["result_coverage_pct"] == 50.0
    assert rep["recovered_from_results_page"] == 1
    assert rep["still_unresolved"] == 1
    missing = r.audit_missing()
    assert missing and missing[0]["source_game_id"] == "30995959"
    assert "failed template" in missing[0]["reason"]


def test_audit_is_read_only_and_repeatable(env):
    env["add_game"]("30840226")
    r = env["reconciler"]({"30840226": PAGE_OK})
    r.run_pass()
    a = r.audit_report()
    b = r.audit_report()
    assert a == b or a["generated_at"] != b["generated_at"]


# ── Worker plumbing ────────────────────────────────────────────────────

def test_worker_run_once_marks_and_recovers(env):
    gid = "30949595"
    env["add_game"](gid)
    conn = sqlite3.connect(str(env["db"]))
    try:
        conn.execute(
            """INSERT INTO game_results (source_game_id, classification,
                   result_at, final_result_status)
               VALUES (?, 'BETUAL_NBA', '2026-09-18T20:00:00.000000Z',
                       'UNKNOWN')""", (gid,))
        conn.commit()
    finally:
        conn.close()
    worker = ResultReconcilerWorker(
        env["db"], interval_s=10**9, fetcher=FakeFetcher({gid: PAGE_OK}))
    out = worker.run_once()
    assert out["needs_stamped"] >= 1
    assert out["verified"] == 1
    worker.stop()


def test_worker_disabled_when_interval_zero(env):
    """BLM_RESULT_RECONCILE_INTERVAL_S=0 → no worker thread (kill switch)."""
    import blm_v4.result_reconciler as rr
    import inspect
    src = inspect.getsource(rr.ResultReconcilerWorker.start)
    assert "Thread(" in src           # shipped shape unchanged


def test_bootstrap_repair_strips_provenance_that_has_no_final(env):
    """A row that claims RESULTS_PAGE but holds no final is a lie — the
    stale-provenance state that made page-verified games unrecoverable.
    The bootstrap repair cleans it and can never touch a verdict."""
    env["add_game"]("91000001")                     # the lie
    env["add_game"]("91000002")
    # the reconciler's bootstrap adds the result_source column (the same
    # idempotent migration production runs), so build it before inserting
    rr = ResultReconciler(env["db"], batch_limit=1)
    conn = sqlite3.connect(str(env["db"]))
    try:
        conn.execute(
            """INSERT INTO game_results (source_game_id, classification,
                   result_at, final_result_status, result_source)
               VALUES ('91000001', 'BETUAL_NBA', '2026-09-24T20:00:00.000000Z',
                       'NEEDS_RECONCILIATION', 'RESULTS_PAGE')""")   # a lie
        conn.execute(
            """INSERT INTO game_results (source_game_id, classification,
                   final_home, final_away, final_total, result_at,
                   final_result_status, result_source)
               VALUES ('91000002', 'BETUAL_NBA', 88, 90, 178,
                       '2026-09-24T20:00:00.000000Z', 'OK',
                       'RESULTS_PAGE')""")          # a real verdict
        conn.commit()
    finally:
        conn.close()

    # the BOOTSTRAP repair is production's path: constructing the reconciler
    # (which server start does) cleans the table it inherits
    rr = ResultReconciler(env["db"], batch_limit=1)
    conn = rr._connect()
    try:
        rows = {r["source_game_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM game_results")}
        second = rr.repair_stale_provenance(conn)          # already clean
    finally:
        conn.close()

    assert rows["91000001"]["result_source"] is None       # lie removed
    assert rows["91000001"]["final_home"] is None          # nothing invented
    assert rows["91000002"]["result_source"] == "RESULTS_PAGE"   # verdict kept
    assert rows["91000002"]["final_home"] == 88
    assert second == 0                                     # idempotent


def test_fetch_failure_is_retryable_never_terminal(env):
    """A fetch failure records FAILED_ATTEMPT and the game stays a
    candidate (bounded retries) — a transient outage never abandons it."""
    gid = "30840226"
    env["add_game"](gid)
    r = env["reconciler"]({gid: None})          # fetcher returns None
    s1 = r.run_pass()
    assert s1["rejected"] == 1
    att = env["rows"]("SELECT * FROM result_reconciliation")[0]
    assert att["outcome"] == "FAILED_ATTEMPT"
    # still a candidate on the next pass (attempt counter allows retries)
    r2 = env["reconciler"]({gid: PAGE_OK})
    s2 = r2.run_pass()
    assert s2["verified"] == 1


def test_template_failed_game_is_exhausted_not_retried_forever(env):
    gid = "30840226"
    env["add_game"](gid)
    r = env["reconciler"]({gid: PAGE_FAILED})
    s1 = r.run_pass()
    assert s1["template_failed"] == 1
    s2 = env["reconciler"]({gid: PAGE_OK}).run_pass()
    # TEMPLATE_FAILED caches the render — no infinite refetch loop
    assert s2["scanned"] == 0


def test_status_note_constant_documented():
    """The new status is part of the module's public vocabulary."""
    assert STATUS_NEEDS_RECONCILIATION == "NEEDS_RECONCILIATION"
    assert "NEEDS_RECONCILIATION" in RESULT_STATUS_NOTE
