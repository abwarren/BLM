"""WS market-frame → current-instance resolution regression tests.

Confirmed defect (2026-09-09): the eu-swarm WS feed keys market frames
to the event's BASE id (e.g. 30840003), but after a virtual-replay
split the collector tracks only the current #iN instance
(30840003#i1).  `_attach_ws_market_hook` resolved frames with an exact
`_find_tracked` match and dropped the observation when it missed —
so no #iN instance ever received a market line (0 market rows across
all 5,548 line-bearing games), while the score/resolve path already
used the base → `self._instances` mapping.

Fix: `_ingest_ws_observation` resolves through the SAME existing
base → current-instance mapping and stores the observation (and its
WS → snapshot bridge) under the CURRENT instance id, never the
completed base game.
"""
from __future__ import annotations

import sqlite3

from blm_v4.classifications import Classification
from blm_v4.collector import PokerBetCollector
from blm_v4.models import PokerBetGame
from blm_v4.storage import PokerBetStore

TS = "2026-09-09T22:20:00.123456Z"          # old: never trips the 30 s ws dedup


def _game(gid: str, status: str = "live",
          home: str = "Los Angeles Lakers Cyber",
          away: str = "Golden State Warriors Cyber") -> PokerBetGame:
    return PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="18295203", competition_slug="cyber-basketball-2k26-matches",
        competition="Cyber Basketball 2K26", region="World",
        game_family="cyber", classification="CYBER_2K26", sport="basketball",
        home_team=home, away_team=away,
        game_slug="lakers-cyber-warriors-cyber",
        source_url=f"https://x/{gid}", status=status,
        first_seen_at=TS, last_seen_at=TS,
    )


def _obs(gid: str, line: float = 216.5, ts: str = TS,
         home: int = 106, away: int = 93) -> dict:
    return {
        "source_game_id": gid, "captured_at": ts,
        "market_type": "MatchTotal", "market_name": "Total Points",
        "line_value": line, "over_price": 1.95, "under_price": 1.85,
        "home_score": home, "away_score": away,
        "period_label": "4th Quarter", "clock": "02:59", "raw": {},
    }


def _track(c: PokerBetCollector, game: PokerBetGame) -> None:
    key = f"{game.home_team}|{game.away_team}"
    c._tracked.setdefault(game.classification, {})[key] = game


def _collector(st: PokerBetStore) -> PokerBetCollector:
    return PokerBetCollector(store=st)


def _rows(st: PokerBetStore, table: str, gid: str) -> list[dict]:
    with sqlite3.connect(st.db_path) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(
            f"SELECT * FROM {table} WHERE source_game_id=? "
            "ORDER BY id ASC", (gid,)).fetchall()]


# 1. base game with no instance → frame base ID → base observation
def test_base_frame_no_instance_stores_base(tmp_path):
    st = PokerBetStore(tmp_path / "b.db")
    st.upsert_game(_game("11110000", status="live"))
    c = _collector(st)
    _track(c, _game("11110000", status="live"))
    c._ingest_ws_observation(_obs("11110000"))
    rows = _rows(st, "market_observations", "11110000")
    assert len(rows) == 1
    assert rows[0]["source_game_id"] == "11110000"


# 2. base game with current instance → frame base ID → current instance obs
def test_base_frame_resolves_to_current_instance(tmp_path):
    st = PokerBetStore(tmp_path / "b.db")
    st.upsert_game(_game("22220000", status="ended"))      # completed base row
    inst = _game("22220000#i1", status="live")
    st.upsert_game(inst)
    c = _collector(st)
    _track(c, inst)                                        # only the instance
    c._instances["22220000"] = "22220000#i1"
    c._ingest_ws_observation(_obs("22220000"))
    rows = _rows(st, "market_observations", "22220000#i1")
    assert len(rows) == 1
    assert rows[0]["source_game_id"] == "22220000#i1"
    assert _rows(st, "market_observations", "22220000") == []   # base untouched
    # the WS → snapshot bridge lands under the instance too
    snaps = _rows(st, "snapshots", "22220000#i1")
    assert len(snaps) == 1
    assert snaps[0]["total_line"] == 216.5
    assert _rows(st, "snapshots", "22220000") == []


# 3. exact instance frame → #i1 → #i1 observation
def test_exact_instance_frame_roundtrip(tmp_path):
    st = PokerBetStore(tmp_path / "b.db")
    inst = _game("33330000#i1", status="live")
    st.upsert_game(inst)
    c = _collector(st)
    _track(c, inst)
    c._ingest_ws_observation(_obs("33330000#i1"))
    rows = _rows(st, "market_observations", "33330000#i1")
    assert len(rows) == 1
    assert rows[0]["source_game_id"] == "33330000#i1"


# 4. unknown frame → no tracked game → safely ignored
def test_unknown_frame_safely_ignored(tmp_path):
    st = PokerBetStore(tmp_path / "b.db")
    c = _collector(st)                                      # nothing tracked
    c._ingest_ws_observation(_obs("99999999"))
    with sqlite3.connect(st.db_path) as con:
        n = con.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0]
        assert n == 0


# 5. completed base + active #i1 → new obs MUST NOT attach to base
def test_completed_base_never_contaminated(tmp_path):
    st = PokerBetStore(tmp_path / "b.db")
    st.upsert_game(_game("44440000", status="ended"))       # completed first sim
    inst = _game("44440000#i1", status="live")
    st.upsert_game(inst)
    c = _collector(st)
    _track(c, inst)
    c._instances["44440000"] = "44440000#i1"
    c._ingest_ws_observation(_obs("44440000"))
    assert _rows(st, "market_observations", "44440000") == []      # no base rows
    rows = _rows(st, "market_observations", "44440000#i1")
    assert len(rows) == 1 and rows[0]["source_game_id"] == "44440000#i1"


# 6. multiple instances → frame resolves to the CORRECT current instance
def test_frame_resolves_to_latest_instance(tmp_path):
    st = PokerBetStore(tmp_path / "b.db")
    for gid in ("55550000", "55550000#i1", "55550000#i2", "55550000#i3"):
        st.upsert_game(_game(gid, status="live" if gid.endswith("#i3")
                             else "ended"))
    c = _collector(st)
    # only the newest instance is tracked (production: split pops the old)
    newest = _game("55550000#i3", status="live")
    _track(c, newest)
    c._instances["55550000"] = "55550000#i3"
    c._ingest_ws_observation(_obs("55550000"))
    assert len(_rows(st, "market_observations", "55550000#i3")) == 1
    assert _rows(st, "market_observations", "55550000#i2") == []
    assert _rows(st, "market_observations", "55550000") == []
    # a later split re-points the mapping → new frames follow the new id
    newer = _game("55550000#i4", status="live")
    st.upsert_game(newer)
    _track(c, newer)
    c._instances["55550000"] = "55550000#i4"
    c._ingest_ws_observation(_obs("55550000", ts="2026-09-09T22:21:00.000000Z"))
    assert len(_rows(st, "market_observations", "55550000#i4")) == 1


# 7. WS observation timestamp preserved
def test_ws_timestamp_preserved(tmp_path):
    st = PokerBetStore(tmp_path / "b.db")
    st.upsert_game(_game("66660000", status="live"))
    c = _collector(st)
    _track(c, _game("66660000", status="live"))
    c._ingest_ws_observation(_obs("66660000"))
    rows = _rows(st, "market_observations", "66660000")
    assert rows[0]["captured_at"] == TS


# 8. market value preserved exactly
def test_market_value_preserved_exactly(tmp_path):
    st = PokerBetStore(tmp_path / "b.db")
    st.upsert_game(_game("77770000", status="live"))
    c = _collector(st)
    _track(c, _game("77770000", status="live"))
    c._ingest_ws_observation(_obs("77770000", line=216.5))
    rows = _rows(st, "market_observations", "77770000")
    assert rows[0]["line_value"] == 216.5
    assert rows[0]["over_price"] == 1.95 and rows[0]["under_price"] == 1.85


# 9. existing market tie-break semantics unchanged (UNIQUE on
#    source_game_id|market_type|line_value|captured_at; a book offers a
#    RANGE of lines at one capture)
def test_market_tiebreak_semantics_unchanged(tmp_path):
    st = PokerBetStore(tmp_path / "b.db")
    st.upsert_game(_game("88880000", status="live"))
    c = _collector(st)
    _track(c, _game("88880000", status="live"))
    obs = _obs("88880000", line=216.5)
    c._ingest_ws_observation(obs)
    c._ingest_ws_observation(obs)                       # identical → deduped
    assert len(_rows(st, "market_observations", "88880000")) == 1
    # a second DISTINCT line at the same capture is a separate row
    c._ingest_ws_observation(_obs("88880000", line=217.5))
    rows = _rows(st, "market_observations", "88880000")
    assert [r["line_value"] for r in rows] == [216.5, 217.5]
