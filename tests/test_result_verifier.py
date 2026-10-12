"""SCHEDULED RESULTS VERIFICATION — one named test per requirement.

The verifier compares the DB's recorded finals against the PokerBet
results page's finals (the swarm feed that backs the page) and REPORTS
every disagreement.  Report-only by default; a correction mode exists but
is OFF behind an explicit flag.

EVERY test is offline: page/feed data comes from SAVED fixtures under
tests/fixtures/results_verification/, and every source is a fake — no
network, no browser, no Playwright.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4 import result_verifier as rv

FIX = Path(__file__).parent / "fixtures" / "results_verification"

DDL = """
CREATE TABLE games (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_game_id TEXT NOT NULL UNIQUE,
    classification TEXT,
    home_team      TEXT,
    away_team      TEXT,
    first_seen_at  TEXT,
    last_seen_at   TEXT
);
CREATE TABLE game_results (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_game_id      TEXT NOT NULL UNIQUE,
    classification      TEXT NOT NULL DEFAULT '',
    final_home          INTEGER,
    final_away          INTEGER,
    final_total         INTEGER,
    result_at           TEXT NOT NULL,
    final_result_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    result_source       TEXT
);
"""


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _db(tmp_path) -> Path:
    p = tmp_path / "blm_pokerbet.db"
    c = sqlite3.connect(str(p))
    c.executescript(DDL)
    c.commit()
    c.close()
    return p


def _add_game(db: Path, gid: str, home: str, away: str, *,
              first: str = None, last: str = None,
              classification: str = "BETUAL_NBA") -> None:
    c = sqlite3.connect(str(db))
    c.execute(
        "INSERT INTO games (source_game_id, classification, home_team,"
        " away_team, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
        (gid, classification, home, away, first, last))
    c.commit()
    c.close()


def _add_final(db: Path, gid: str, h: int, a: int, *, total: int = None,
               result_at: str = None, status: str = "OK",
               source: str = "RESULTS_PAGE",
               classification: str = "BETUAL_NBA") -> None:
    if total is None and h is not None and a is not None:
        total = h + a
    c = sqlite3.connect(str(db))
    c.execute(
        "INSERT INTO game_results (source_game_id, classification,"
        " final_home, final_away, final_total, result_at,"
        " final_result_status, result_source) VALUES (?,?,?,?,?,?,?,?)",
        (gid, classification, h, a, total,
         result_at or _iso(datetime.now(timezone.utc)),
         status, source))
    c.commit()
    c.close()


def _row(db: Path, gid: str) -> dict:
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM game_results WHERE source_game_id=?",
                  (gid,)).fetchone()
    c.close()
    return dict(r)


def _page_final(h: int, a: int, *, home: str = None, away: str = None,
                start_time: str = None) -> dict:
    """A page final in the shape SwarmResultsClient.normalize_result emits."""
    return {"game_id": None, "home_team": home, "away_team": away,
            "home_score": h, "away_score": a, "final_total": h + a,
            "start_time": start_time, "quarter_scores": [],
            "raw_scores": f"{h}:{a}"}


class _FakeSource:
    """Stands in for a FinalsSource (gid -> page final).  Offline."""
    name = "fake"

    def __init__(self, finals: dict, *, raise_exc: Exception = None):
        self.finals = finals
        self.raise_exc = raise_exc
        self.calls = []

    def fetch_finals(self, records):
        self.calls.append([r["source_game_id"] for r in records])
        if self.raise_exc is not None:
            raise self.raise_exc
        return {r["source_game_id"]: self.finals.get(r["source_game_id"])
                for r in records}


# ══════════════════════════════════════════════════════════════════════
# R1 — the page's finals are parsed from the saved source fixture
# ══════════════════════════════════════════════════════════════════════

def test_swarm_source_parses_a_saved_bulk_day_fixture():
    """The default source reads the feed that backs the results page; here
    from a SAVED raw bulk-day frame — no network.  It maps results by
    game_id (and strips the #iN replay suffix)."""
    from blm_v4.swarm_results import normalize_result
    frame = json.loads((FIX / "swarm_bulk_day.json").read_text())
    nodes = frame["data"]["games"]["game"]
    normalized = [normalize_result(n) for n in nodes]

    class _FakeClient:
        def __init__(self):
            self.days = []

        def fetch_results_day(self, day, sport_id=3):
            self.days.append((day, sport_id))
            return list(normalized)

    client = _FakeClient()
    src = rv.SwarmFinalsSource(client=client, sport_ids=(3,))
    records = [
        {"source_game_id": "31205481", "result_at": "2026-10-12T19:07:22.000000Z"},
        {"source_game_id": "31205384#i1", "result_at": "2026-10-12T19:02:18.000000Z"},
        {"source_game_id": "99999999", "result_at": "2026-10-12T19:02:18.000000Z"},
    ]
    out = src.fetch_finals(records)

    assert out["31205481"]["home_score"] == 96      # from the fixture
    assert out["31205481"]["away_score"] == 76
    assert out["31205481"]["final_total"] == 172
    assert out["31205384#i1"]["home_score"] == 88   # #iN stripped -> base id
    assert out["99999999"] is None                  # absence, not a failure
    assert client.days                                # the day path was used


# ══════════════════════════════════════════════════════════════════════
# R2 — the comparison: what counts as a mismatch
# ══════════════════════════════════════════════════════════════════════

def test_comparison_flags_a_total_disagreement_with_the_required_fields():
    """A recorded final that differs from the page's final is a mismatch,
    reported as (source_game_id, teams, DB total, page total, delta, result_at)."""
    now = datetime.now(timezone.utc)
    db = {"source_game_id": "31205481", "home_team": "Bayern Munich",
          "away_team": "Anadolu Efes Sk", "final_home": 89, "final_away": 72,
          "final_total": 161, "classification": "BETUAL_NBA",
          "result_at": _iso(now), "final_result_status": "OK",
          "first_seen_at": _iso(now - timedelta(minutes=40)),
          "last_seen_at": _iso(now - timedelta(minutes=3))}
    page = _page_final(96, 76, home="Bayern Munich Virtual",
                       away="Anadolu Efes SK Virtual",
                       start_time=_iso(now - timedelta(minutes=41)))

    mm = rv.compare_final(db, page)

    assert mm is not None
    for k in ("source_game_id", "home_team", "away_team", "db_total",
              "page_total", "delta", "result_at"):
        assert k in mm, k
    assert mm["source_game_id"] == "31205481"
    assert mm["db_total"] == 161 and mm["page_total"] == 172
    assert mm["delta"] == 11
    assert mm["teams_match"] is True
    assert mm["start_time_consistent"] is True


def test_comparison_agrees_when_both_sides_match():
    db = {"final_home": 88, "final_away": 93, "final_total": 181}
    assert rv.compare_final(db, _page_final(88, 93)) is None


def test_comparison_treats_a_missing_page_final_as_absence_not_mismatch():
    """No page final is an ABSENCE, never a disagreement."""
    db = {"final_home": 88, "final_away": 93, "final_total": 181}
    assert rv.compare_final(db, None) is None
    assert rv.compare_final(db, {"home_score": None, "away_score": None}) is None


# ══════════════════════════════════════════════════════════════════════
# R3 — the report shape + every mismatch reported
# ══════════════════════════════════════════════════════════════════════

def test_report_shape_and_mismatch_fields(tmp_path):
    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    _add_game(db, "31205481", "Bayern Munich", "Anadolu Efes Sk",
              first=_iso(now - timedelta(minutes=40)),
              last=_iso(now - timedelta(minutes=3)))
    _add_final(db, "31205481", 89, 72, result_at=_iso(now))
    src = _FakeSource({"31205481": _page_final(
        96, 76, home="Bayern Munich Virtual", away="Anadolu Efes SK Virtual",
        start_time=_iso(now - timedelta(minutes=41)))})

    rep = rv.ResultVerifier(db, source=src, window_days=3,
                            data_root=tmp_path).run_pass()

    for k in ("generated_at", "window_days", "apply", "source", "reachable",
              "games_checked", "mismatch_count", "unmatched_count",
              "mismatches", "unmatched", "errors"):
        assert k in rep, k
    assert rep["mismatch_count"] == 1 and rep["games_checked"] == 1
    assert rep["apply"] is False
    m = rep["mismatches"][0]
    assert (m["db_total"], m["page_total"], m["delta"]) == (161, 172, 11)
    assert m["result_at"] == _row(db, "31205481")["result_at"]


def test_unmatched_games_are_reported_separately_from_mismatches(tmp_path):
    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    _add_game(db, "31205481", "Bayern Munich", "Anadolu Efes Sk")
    _add_final(db, "31205481", 89, 72, result_at=_iso(now))
    src = _FakeSource({"31205481": None})         # feed has no result

    rep = rv.ResultVerifier(db, source=src, window_days=3,
                            data_root=tmp_path).run_pass()

    assert rep["mismatch_count"] == 0
    assert rep["unmatched_count"] == 1
    assert rep["unmatched"][0]["source_game_id"] == "31205481"


# ══════════════════════════════════════════════════════════════════════
# R4 — DEFAULT REPORT-ONLY: no DB writes
# ══════════════════════════════════════════════════════════════════════

def test_report_only_never_writes_to_the_db(tmp_path):
    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    _add_game(db, "31205481", "Bayern Munich", "Anadolu Efes Sk")
    _add_final(db, "31205481", 89, 72, result_at=_iso(now))
    before = _row(db, "31205481")
    src = _FakeSource({"31205481": _page_final(96, 76)})

    rep = rv.ResultVerifier(db, source=src, window_days=3, apply=False,
                            data_root=tmp_path).run_pass()

    assert rep["mismatch_count"] == 1
    assert _row(db, "31205481") == before          # byte-identical
    assert "applied" not in rep["mismatches"][0]


# ══════════════════════════════════════════════════════════════════════
# R5 — APPLY mode: exists, OFF by default, corrects when enabled
# ══════════════════════════════════════════════════════════════════════

def test_apply_defaults_off(monkeypatch):
    monkeypatch.delenv("RECONCILE_VERIFY_APPLY", raising=False)
    assert rv.apply_from_env() is False
    for v in ("0", "false", "no", "", "off"):
        monkeypatch.setenv("RECONCILE_VERIFY_APPLY", v)
        assert rv.apply_from_env() is False, v
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv("RECONCILE_VERIFY_APPLY", v)
        assert rv.apply_from_env() is True, v


def test_apply_mode_corrects_the_recorded_final(tmp_path):
    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    _add_game(db, "31205481", "Bayern Munich", "Anadolu Efes Sk")
    _add_final(db, "31205481", 89, 72, result_at=_iso(now))
    src = _FakeSource({"31205481": _page_final(96, 76)})

    rep = rv.ResultVerifier(db, source=src, window_days=3, apply=True,
                            data_root=tmp_path).run_pass()

    r = _row(db, "31205481")
    assert (r["final_home"], r["final_away"], r["final_total"]) == (96, 76, 172)
    assert r["result_source"] == rv.CORRECTION_SOURCE
    assert rep["mismatches"][0]["applied"] is True


def test_apply_never_fabricates_a_final_for_a_row_without_one(tmp_path):
    """A row with no final is not even a verification target, and apply
    never creates one."""
    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    _add_game(db, "31205481", "Bayern Munich", "Anadolu Efes Sk")
    _add_final(db, "31205481", None, None, result_at=_iso(now), status="UNKNOWN")
    src = _FakeSource({"31205481": _page_final(96, 76)})

    rep = rv.ResultVerifier(db, source=src, window_days=3, apply=True,
                            data_root=tmp_path).run_pass()

    assert rep["games_checked"] == 0
    r = _row(db, "31205481")
    assert r["final_home"] is None and r["final_away"] is None


# ══════════════════════════════════════════════════════════════════════
# R6 — operator-settable knobs (interval, window, apply) read from env
# ══════════════════════════════════════════════════════════════════════

def test_interval_is_env_settable_with_3600_default(monkeypatch):
    monkeypatch.delenv("RECONCILE_VERIFY_INTERVAL_S", raising=False)
    assert rv.interval_from_env() == 3600.0
    monkeypatch.setenv("RECONCILE_VERIFY_INTERVAL_S", "120")
    assert rv.interval_from_env() == 120.0


def test_window_days_is_env_settable_with_3_default(monkeypatch):
    monkeypatch.delenv("RECONCILE_VERIFY_DAYS", raising=False)
    assert rv.window_days_from_env() == 3
    monkeypatch.setenv("RECONCILE_VERIFY_DAYS", "5")
    assert rv.window_days_from_env() == 5


def test_window_bounds_the_scan(tmp_path):
    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    _add_game(db, "recent", "A", "B")
    _add_game(db, "old", "C", "D")
    _add_final(db, "recent", 80, 70, result_at=_iso(now - timedelta(days=1)))
    _add_final(db, "old", 80, 70,
               result_at=_iso(now - timedelta(days=30)))

    src = _FakeSource({})
    v = rv.ResultVerifier(db, source=src, window_days=3, data_root=tmp_path)
    c = v._connect()
    try:
        ids = {r["source_game_id"] for r in v.load_db_finals(c)}
    finally:
        c.close()

    assert ids == {"recent"}          # the 30-day-old final is out of window
    assert src.calls == []            # and never even fetched


def test_worker_uses_env_config_and_runs_once_offline(tmp_path, monkeypatch):
    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    _add_game(db, "31205481", "Bayern Munich", "Anadolu Efes Sk",
              first=_iso(now - timedelta(minutes=40)),
              last=_iso(now - timedelta(minutes=3)))
    _add_final(db, "31205481", 89, 72, result_at=_iso(now))
    monkeypatch.setenv("RECONCILE_VERIFY_APPLY", "0")
    monkeypatch.setenv("RECONCILE_VERIFY_DAYS", "3")
    src = _FakeSource({"31205481": _page_final(96, 76)})

    w = rv.ResultVerifierWorker(db, interval_s=3600, data_root=tmp_path,
                                source=src, log=None)
    rep = w.run_once()

    assert w.verifier._window_days == 3
    assert w.verifier._apply is False
    assert rep["mismatch_count"] == 1
    assert rep["apply"] is False


# ══════════════════════════════════════════════════════════════════════
# R7 — FAIL-SOFT: an unreachable source never crashes the pass
# ══════════════════════════════════════════════════════════════════════

def test_an_unreachable_source_is_soft_and_reported(tmp_path):
    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    _add_game(db, "31205481", "Bayern Munich", "Anadolu Efes Sk")
    _add_final(db, "31205481", 89, 72, result_at=_iso(now))
    src = _FakeSource({}, raise_exc=RuntimeError("ws down"))

    rep = rv.ResultVerifier(db, source=src, window_days=3,
                            data_root=tmp_path).run_pass()

    assert rep["reachable"] is False
    assert rep["mismatch_count"] == 0
    assert rep["errors"] and "source_unreachable" in rep["errors"][0]
    assert _row(db, "31205481")["final_home"] == 89     # untouched


def test_the_report_is_written_under_data_root(tmp_path):
    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    _add_game(db, "31205481", "Bayern Munich", "Anadolu Efes Sk")
    _add_final(db, "31205481", 89, 72, result_at=_iso(now))
    src = _FakeSource({"31205481": _page_final(96, 76)})

    rep = rv.ResultVerifier(db, source=src, window_days=3,
                            data_root=tmp_path).run_pass()

    p = rv.report_path(tmp_path)
    assert p.exists()
    on_disk = json.loads(p.read_text())
    assert on_disk["mismatch_count"] == 1
    assert on_disk["source"] == rep["source"]


# ══════════════════════════════════════════════════════════════════════
# R8 — the DOM rows source parses a saved rows fixture (identity reuse)
# ══════════════════════════════════════════════════════════════════════

def test_dom_rows_source_parses_a_saved_rows_fixture():
    """The alternative source reuses the repo's row matcher.  Here it reads
    a SAVED rows fixture (no browser): a row matching the record's teams +
    fixture time resolves to its final."""
    rows = json.loads((FIX / "results_page_rows.json").read_text())["rows"]

    class _FakeFetcher:
        def fetch_results_rows(self, gid):
            return rows

    rec = {"source_game_id": "31205384", "home_team": "Liaoning Flying Leopards",
           "away_team": "Qingdao Eagles", "classification": "BETUAL_NBA",
           "first_seen_at": "2026-10-12T18:14:00.000000Z",
           "last_seen_at": "2026-10-12T18:38:00.000000Z"}
    src = rv.ResultsPageFinalsSource(fetcher=_FakeFetcher())
    out = src.fetch_finals([rec])

    got = out["31205384"]
    assert got is not None
    assert got["home_score"] == 88 and got["away_score"] == 93
    assert got["final_total"] == 181


def test_dom_rows_source_yields_absence_for_a_missing_row():
    rows = json.loads((FIX / "results_page_rows.json").read_text())["rows"]

    class _FakeFetcher:
        def fetch_results_rows(self, gid):
            return rows

    rec = {"source_game_id": "11111111", "home_team": "Nobody",
           "away_team": "No One", "classification": "BETUAL_NBA",
           "first_seen_at": "2026-10-12T18:14:00.000000Z",
           "last_seen_at": "2026-10-12T18:38:00.000000Z"}
    src = rv.ResultsPageFinalsSource(fetcher=_FakeFetcher())
    assert src.fetch_finals([rec])["11111111"] is None
