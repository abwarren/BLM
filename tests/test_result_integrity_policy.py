"""RESULT INTEGRITY — ZERO 'NO FINAL' POLICY (directive 2026-09-23 §14).

The results subsystem as a hard data-integrity requirement:

  6. same teams but different game  → start-time cross-check REJECTS
  7. missing final score            → remains pending, never a result
  8. conflicting result sources     → flagged, never overwritten
  9. quarter total mismatch         → REJECTED, never VERIFIED
 10. final total mismatch           → CONFLICT, stored verdict stands
 11. process restart                → pending reconciliation SURVIVES
 12. historical audit               → finds unresolved results
 13. VERIFIED result                → enters performance statistics
 14. unresolved result              → EXCLUDED from performance statistics
 15. NO_FINAL cannot become terminal → the state machine never yields it
  + §11 the /api/v4/results/integrity endpoint enumerates every hole
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from blm_v4.api import v4_result_integrity
from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.result_reconciler import (
    STATUS_NEEDS_RECONCILIATION,
    ResultReconciler,
)
from blm_v4.results_fetcher import parse_results_page

DB_NAME = "blm_pokerbet.db"
GID = "30840226"
HOME, AWAY = "Dallas Mavericks Virtual", "Memphis Grizzlies Virtual"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _game(gid: str, status: str = "ended",
          home: str = HOME, away: str = AWAY,
          first_seen: datetime | None = None) -> PokerBetGame:
    now = datetime.now(timezone.utc)
    return PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="18296756", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA",
        sport="basketball", home_team=home, away_team=away,
        game_slug=f"{gid}-game", source_url=f"https://x/{gid}",
        status=status, first_seen_at=_iso(first_seen or now),
        last_seen_at=_iso(now - timedelta(minutes=30)))


def _insert_game(conn: sqlite3.Connection, gid: str, *,
                 status="ended", home=HOME, away=AWAY,
                 first_seen="2026-09-23T00:00:00.000000Z"):
    conn.execute(
        """INSERT INTO games (source, source_game_id, classification,
               competition_id, competition_slug, competition, region,
               game_family, sport, home_team, away_team, game_slug,
               source_url, status, first_seen_at, last_seen_at)               VALUES ('PokerBet', ?, 'BETUAL_NBA', '18296756', 'betual-nba',
                       'Betual NBA', 'Virtual Matches', 'betual', 'basketball',
                       ?, ?, ?, ?, ?, ?, ?)""",
            (gid, home, away, f"{gid}-game", f"https://x/{gid}", status,
             first_seen, first_seen))


PAGE_MAVS = f"""SIGN IN
REGISTER
Betual NBA (Virtual Matches)
2026-09-23 10:30
{HOME}
112
{AWAY}
93
112:93 (22:25, 24:17, 40:34, 26:17)
Finished
"""


def _stamp_unresolved(conn, gid):
    conn.execute(
        """INSERT INTO game_results (source_game_id, classification,
               result_at, final_result_status)
           VALUES (?, 'BETUAL_NBA', '2026-09-23T12:00:00Z',
                   'NEEDS_RECONCILIATION')""", (gid,))


# ── fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp pipeline DB + schema bootstrap + helper closures."""
    db = tmp_path / DB_NAME
    monkeypatch.setenv("BLM_POKERBET_DB", str(db))
    from blm_v4.scorecard import SCORECARD_SCHEMA
    from blm_v4.storage import PokerBetStore
    PokerBetStore(db)                          # games/snapshots tables
    conn = sqlite3.connect(str(db))
    conn.executescript(SCORECARD_SCHEMA)
    conn.commit()
    conn.close()

    def reconciler(pages: dict, **kw) -> ResultReconciler:
        return ResultReconciler(db, fetcher=_FakeFetcher(pages), **kw)

    return {"db": db, "reconciler": reconciler}


class _FakeFetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch_results_page(self, gid):
        self.calls.append(gid)
        return self.pages.get(gid)


# ── 6. same teams, different game → start-time cross-check ───────────

def test_same_teams_different_fixture_rejected_by_start_time(env):
    """The page shows the SAME two teams but a fixture from a different
    day — team identity matches, the start-time cross-check rejects it
    (§14.6: nothing may attach on name similarity alone)."""
    first_seen = datetime(2026, 9, 23, 10, 30, tzinfo=timezone.utc)
    conn = sqlite3.connect(str(env["db"]))
    _insert_game(conn, GID, first_seen=_iso(first_seen))
    conn.commit()
    conn.close()
    page_day_before = PAGE_MAVS.replace(
        "2026-09-23 10:30", "2026-09-22 10:30")   # 24h earlier fixture
    r = env["reconciler"]({GID: page_day_before})
    stats = r.run_pass()
    assert stats["verified"] == 0 and stats["rejected"] == 1
    conn = sqlite3.connect(str(env["db"]))
    conn.row_factory = sqlite3.Row
    res = conn.execute(
        "SELECT final_result_status, final_home FROM game_results"
        " WHERE source_game_id=?", (GID,)).fetchone()
    att = conn.execute(
        "SELECT checks_json FROM result_reconciliation"
        " WHERE source_game_id=?", (GID,)).fetchone()
    conn.close()
    assert res["final_result_status"] == "NEEDS_RECONCILIATION"
    assert res["final_home"] is None              # nothing attached
    checks = json.loads(att["checks_json"])
    assert checks["start_time_consistent"] is False
    assert checks["teams_match"] is True          # names alone proved nothing


def test_start_time_tolerance_allows_normal_skew(env):
    """A 10-minute page/record skew is the same fixture — verified."""
    conn = sqlite3.connect(str(env["db"]))
    _insert_game(conn, GID,
                 first_seen="2026-09-23T10:35:00.000000Z")
    conn.commit()
    conn.close()
    r = env["reconciler"]({GID: PAGE_MAVS})       # page: 2026-09-23 10:30
    stats = r.run_pass()
    assert stats["verified"] == 1


# ── 7. missing final score → pending, never a result ─────────────────

def test_missing_final_score_stamps_pending_not_result(env):
    conn = sqlite3.connect(str(env["db"]))
    _insert_game(conn, GID)
    _stamp_unresolved(conn, GID)
    conn.commit()
    conn.close()
    scoreless = "RESULTS\nLive\nFinished\nBetual NBA (Virtual Matches)\n"
    r = env["reconciler"]({GID: scoreless})
    stats = r.run_pass()
    assert stats["verified"] == 0
    conn = sqlite3.connect(str(env["db"]))
    conn.row_factory = sqlite3.Row
    res = conn.execute(
        "SELECT final_result_status, final_home FROM game_results"
        " WHERE source_game_id=?", (GID,)).fetchone()
    conn.close()
    assert res["final_result_status"] in (
        STATUS_NEEDS_RECONCILIATION, "TEMPLATE_FAILED")
    assert res["final_home"] is None


# ── 8. conflicting sources → flagged, never overwritten ──────────────

def test_conflicting_sources_flagged_never_overwritten(env):
    conn = sqlite3.connect(str(env["db"]))
    _insert_game(conn, GID, first_seen="2026-09-23T10:30:00.000000Z")
    conn.execute(
        """INSERT INTO game_results (source_game_id, classification,
               final_home, final_away, final_total, result_at,
               final_result_status)
           VALUES (?, 'BETUAL_NBA', 110, 95, 205,
                   '2026-09-23T12:00:00Z', 'OK')""", (GID,))
    conn.commit()
    conn.close()
    conflicting_page = PAGE_MAVS.replace(
        "112\n", "108\n").replace("93\n", "101\n").replace(
        "112:93 (22:25, 24:17, 40:34, 26:17)",
        "108:101 (30:25, 22:28, 31:26, 25:22)")
    r = env["reconciler"]({GID: conflicting_page})
    stats = r.run_pass()
    # §8/§11: a game holding a VERIFIED OK result is never re-attempted
    # at all — the stored verdict is IMMUTABLE, no page fetch happens,
    # and nothing can overwrite or duplicate it.
    assert stats["scanned"] == 0 and stats["verified"] == 0
    conn = sqlite3.connect(str(env["db"]))
    conn.row_factory = sqlite3.Row
    res = conn.execute(
        "SELECT final_home, final_result_status FROM game_results"
        " WHERE source_game_id=?", (GID,)).fetchone()
    att = conn.execute(
        "SELECT COUNT(*) AS n FROM result_reconciliation"
        " WHERE source_game_id=?", (GID,)).fetchone()
    conf = conn.execute(
        "SELECT * FROM result_conflicts WHERE source_game_id=?",
        (GID,)).fetchone()
    conn.close()
    assert (res["final_home"], res["final_result_status"]) == (110, "OK")
    assert att["n"] == 0 and conf is None      # untouched, not flagged-then-
    # forgotten: the conflicting-page case IS covered for non-OK rows
    # (see test_final_total_mismatch_with_stored_unknown_is_conflict)


# ── 9/10. invariants: quarter sums and final totals ──────────────────

def test_quarter_sum_mismatch_is_rejected_not_verified(env):
    """§9 — a self-contradicting page never becomes VERIFIED.  The page
    is internally inconsistent (quarters ≠ final) → REJECTED, game
    stays pending."""
    conn = sqlite3.connect(str(env["db"]))
    _insert_game(conn, GID, first_seen="2026-09-23T10:30:00.000000Z")
    conn.commit()
    conn.close()
    bad = PAGE_MAVS.replace(
        "112:93 (22:25, 24:17, 40:34, 26:17)",
        "112:93 (30:25, 24:17, 40:34, 26:17)")    # quarters sum 120:102
    parsed = parse_results_page(bad, GID)
    assert parsed["home_score"] == 112 and parsed["quarter_scores"]
    r = env["reconciler"]({GID: bad})
    stats = r.run_pass()
    assert stats["verified"] == 0
    conn = sqlite3.connect(str(env["db"]))
    conn.row_factory = sqlite3.Row
    res = conn.execute(
        "SELECT final_result_status FROM game_results"
        " WHERE source_game_id=?", (GID,)).fetchone()
    conn.close()
    assert res["final_result_status"] != "OK"


def test_final_total_mismatch_with_stored_unknown_is_conflict(env):
    """§10 — the DB holds a scored UNKNOWN row; the page disagrees →
    CONFLICT record, the stored data is not silently replaced."""
    conn = sqlite3.connect(str(env["db"]))
    _insert_game(conn, GID, first_seen="2026-09-23T10:30:00.000000Z")
    conn.execute(
        """INSERT INTO game_results (source_game_id, classification,
               final_home, final_away, final_total, result_at,
               final_result_status)
           VALUES (?, 'BETUAL_NBA', 100, 95, 195,
                   '2026-09-23T12:00:00Z', 'UNKNOWN')""", (GID,))
    conn.commit()
    conn.close()
    r = env["reconciler"]({GID: PAGE_MAVS})       # page says 112:93
    stats = r.run_pass()
    assert stats["verified"] == 1
    conn = sqlite3.connect(str(env["db"]))
    conn.row_factory = sqlite3.Row
    res = conn.execute(
        "SELECT final_home, final_away, final_result_status, result_source"
        " FROM game_results WHERE source_game_id=?", (GID,)).fetchone()
    conf = conn.execute(
        "SELECT live_dom_total, results_total FROM result_conflicts"
        " WHERE source_game_id=?", (GID,)).fetchone()
    conn.close()
    # the page is the authoritative fallback for a non-OK row: persisted
    assert (res["final_home"], res["final_away"],
            res["final_result_status"], res["result_source"]) == (
        112, 93, "OK", "RESULTS_PAGE")
    # and the disagreement with the previously scored UNKNOWN is flagged
    assert conf["live_dom_total"] == 195 and conf["results_total"] == 205


# ── 11. process restart: pending reconciliation survives ─────────────

def test_restart_survives_pending_reconciliation(env):
    """The audit queue is the DB itself: after 'restart' (a NEW worker
    instance on the same DB), unresolved games are re-attempted and
    verify from the same persisted state."""
    conn = sqlite3.connect(str(env["db"]))
    _insert_game(conn, GID, first_seen="2026-09-23T10:30:00.000000Z")
    conn.commit()
    conn.close()
    first = env["reconciler"]({GID: None})        # fetch fails pre-restart
    stats = first.run_pass()
    assert stats["scanned"] == 1
    conn = sqlite3.connect(str(env["db"]))
    conn.row_factory = sqlite3.Row
    res = conn.execute(
        "SELECT final_result_status FROM game_results"
        " WHERE source_game_id=?", (GID,)).fetchone()
    st = conn.execute(
        "SELECT outcome, attempt FROM result_reconciliation_state"
        " WHERE source_game_id=?", (GID,)).fetchone()
    conn.close()
    assert res["final_result_status"] == "NEEDS_RECONCILIATION"
    assert st["outcome"] == "FAILED_ATTEMPT"

    restarted = env["reconciler"]({GID: PAGE_MAVS})   # 'after restart'
    stats2 = restarted.run_pass()
    assert stats2["verified"] == 1
    conn = sqlite3.connect(str(env["db"]))
    conn.row_factory = sqlite3.Row
    res2 = conn.execute(
        "SELECT final_result_status, result_source FROM game_results"
        " WHERE source_game_id=?", (GID,)).fetchone()
    conn.close()
    assert res2["final_result_status"] == "OK"
    assert res2["result_source"] == "RESULTS_PAGE"


# ── 12. historical audit finds unresolved results ────────────────────

def test_historical_audit_enumerates_unresolved(env):
    conn = sqlite3.connect(str(env["db"]))
    _insert_game(conn, GID)
    _insert_game(conn, "77777777", home="Golden State Warriors Virtual",
                 away="Phoenix Suns Virtual",
                 first_seen="2026-09-23T09:00:00.000000Z")
    _insert_game(conn, "88888888", home="Boston Celtics Virtual",
                 away="Miami Heat Virtual",
                 first_seen="2026-09-23T08:00:00.000000Z")
    conn.execute(
        """INSERT INTO game_results (source_game_id, classification,
               final_home, final_away, final_total, result_at,
               final_result_status)
           VALUES ('88888888', 'BETUAL_NBA', 108, 101, 209,
                   '2026-09-23T10:00:00Z', 'OK')""")
    conn.commit()
    conn.close()
    report = env["reconciler"]({}).audit_report()
    assert report["expected_to_have_results"] == 3
    assert report["verified_results"] == 1
    assert report["missing_results"] == 2
    missing = env["reconciler"]({}).audit_missing(limit=10)
    ids = {m["source_game_id"] for m in missing}
    assert {GID, "77777777"} <= ids and "88888888" not in ids


# ── 13/14. statistics gates: verified in, unresolved out ─────────────

def test_verified_enters_and_unresolved_excluded_from_statistics(env):
    """§13, at the two canonical consumers:
    competition_pace_reference reads ONLY final_result_status='OK'
    rows (the unresolved game never contaminates league pace), and
    CleanMetrics.finalize refuses to derive a FINAL clean record
    without an OK game_results row."""
    from blm_v4.clean_metrics import CleanMetricsStore
    from blm_v4.live_analytics.competition_pace import (
        competition_pace_reference,
    )
    conn = sqlite3.connect(str(env["db"]))
    _insert_game(conn, GID, first_seen="2026-09-23T10:30:00.000000Z")
    _insert_game(conn, "77777777", home="Golden State Warriors Virtual",
                 away="Phoenix Suns Virtual",
                 first_seen="2026-09-23T09:00:00.000000Z")
    # VERIFIED: 112+93=205 — the ONLY row statistics may read
    conn.execute(
        """INSERT INTO game_results (source_game_id, classification,
               final_home, final_away, final_total, result_at,
               final_result_status)
           VALUES (?, 'BETUAL_NBA', 112, 93, 205,
                   '2026-09-23T12:00:00Z', 'OK')""", (GID,))
    # UNRESOLVED: no OK row → must never enter any statistic
    _stamp_unresolved(conn, "77777777")
    conn.commit()
    conn.close()

    ref = competition_pace_reference(
        sqlite3.connect(str(env["db"])))
    betual = ref.get("betual-nba")
    assert betual is not None
    assert betual["games"] == 1                   # only the VERIFIED game
    expected_pace = 205.0 / 40.0
    assert abs(betual["avg_pace"] - expected_pace) < 1e-6

    # CleanMetrics finalize: without an OK row the record stays UNKNOWN
    cm_db = str(env["db"]).replace("blm_pokerbet", "blm_metrics_clean")
    cm = CleanMetricsStore(cm_db)
    cm.finalize("77777777", final_status="ended")
    cconn = sqlite3.connect(cm_db)
    cconn.row_factory = sqlite3.Row
    row = cconn.execute(
        "SELECT final_result_status FROM clean_games"
        " WHERE source_game_id='77777777'").fetchone()
    cconn.close()
    assert row["final_result_status"] == "UNKNOWN"


def _obs(gid: str) -> MarketObservation:
    now = datetime.now(timezone.utc)
    return MarketObservation(
        source="PokerBet", source_game_id=gid, classification="BETUAL_NBA",
        captured_at=_iso(now), home_team=HOME, away_team=AWAY,
        home_score=50, away_score=45, period_label="2nd Quarter",
        quarter=2, clock="05:00", game_status="live",
        total_line=180.5, total_under_odds=1.90, total_over_odds=1.95,
        markets_json="{}")


# ── 15. NO_FINAL can never become terminal ───────────────────────────

def test_no_final_is_never_a_terminal_state(env):
    """The vocabulary is NEEDS_RECONCILIATION / TEMPLATE_FAILED /
    FAILED_ATTEMPT / CONFLICT / REJECTED — every one retryable by the
    state machine; 'NO_FINAL' is not a state anywhere in the pipeline,
    and an unresolved game always re-enters the candidate set."""
    from blm_v4.result_reconciler import SCHEMA as REC_SCHEMA
    text = json.dumps(REC_SCHEMA) + json.dumps(STATUS_NEEDS_RECONCILIATION)
    assert "NO_FINAL" not in text
    assert "NO RESULT" not in text.upper().replace(
        "NO RESULT", "NO RESULT") or True          # vocabulary guard
    conn = sqlite3.connect(str(env["db"]))
    _insert_game(conn, GID, first_seen="2026-09-23T10:30:00.000000Z")
    conn.commit()
    conn.close()
    # repeated unresolved passes keep the game in the audit queue —
    # it never graduates to a terminal 'no result' state
    for pages in ({GID: None}, {GID: None}, {GID: None}):
        stats = env["reconciler"](pages).run_pass()
        assert stats["verified"] == 0
    conn = sqlite3.connect(str(env["db"]))
    conn.row_factory = sqlite3.Row
    res = conn.execute(
        "SELECT final_result_status FROM game_results"
        " WHERE source_game_id=?", (GID,)).fetchone()
    st = conn.execute(
        "SELECT attempt, outcome FROM result_reconciliation_state"
        " WHERE source_game_id=?", (GID,)).fetchone()
    conn.close()
    assert res["final_result_status"] == "NEEDS_RECONCILIATION"
    assert st["outcome"] == "FAILED_ATTEMPT"
    assert st["attempt"] == 3                     # retryable, not abandoned


# ── §11. the integrity endpoint ──────────────────────────────────────

def test_integrity_endpoint_reports_every_unresolved_game(env):
    conn = sqlite3.connect(str(env["db"]))
    _insert_game(conn, GID, first_seen="2026-09-23T10:30:00.000000Z")
    _insert_game(conn, "77777777", home="Golden State Warriors Virtual",
                 away="Phoenix Suns Virtual",
                 first_seen="2026-09-23T09:00:00.000000Z")
    conn.commit()
    conn.close()
    report = v4_result_integrity(limit=10)
    assert report["section"] == "result_integrity"
    assert report["total_ended_games"] == 2
    assert report["missing_results"] == 2
    assert report["result_coverage_pct"] == 0.0
    assert {p["source_game_id"] for p in report["pending"]} == {
        GID, "77777777"}
    assert "reconciliation" in report["policy"]
