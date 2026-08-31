"""M009-M1 — immutable per-checkpoint Market-vs-Fair history.

The scorecard's PRIMARY analytical question is MARKET LINE vs FAIR VALUE
at every checkpoint (10..100%).  Each checkpoint must freeze what was
actually available at that point in the game:

  market_vs_fair = live_market_line - blm_fair_value   (signed, retained)
  signal         = UNDER_VALUE | OVER_VALUE | PUSH     (market vs fair)
  outcome        = UNDER_WIN | OVER_WIN | UNDER_LOSS | OVER_LOSS | PUSH
                   (BLM position vs market, resolved against the actual)

The historical record is FROZEN at first write: later model builds or
later market observations must NEVER rewrite a recorded checkpoint
(no rebase — unlike predictions, which are current-code-wins).

Covers:
  - one row per (clean completed game, checkpoint 10..100%)
  - OLV / frozen live market / BLM fair / CLV / actual all linked
  - signed market_vs_fair (negative values preserved)
  - signal classification (market > fair = UNDER_VALUE, < = OVER_VALUE)
  - outcome classification per the M009 spec (pushes handled explicitly)
  - OLV/CLV/actual linkage + blm_vs_olv / blm_vs_clv / olv_to_clv
  - market_move_toward_blm (TOWARD | AWAY | UNCHANGED vs BLM fair)
  - immutability: a second run never changes recorded rows
  - honest NULLs: missing market -> no signal, no disparity, no outcome
  - eligibility: only OK-result, >=15-snap, starts-Q1, non-INVALID games
  - WS-fallback OLV/CLV/live lines when snapshots carry no market
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.scorecard import Scorecard
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# 20 snapshots, one every 3 wall-clock minutes, spanning Q1 10:00 -> Q4 00:00.
# Fast early scoring (hot pace -> fair ABOVE the market early), slow late
# scoring (cold pace -> fair BELOW the market late): both disparity
# directions in ONE game.
_HOME = [0, 8, 16, 24, 32, 40, 44, 47, 50, 53, 56, 59, 62, 65, 68, 71, 74, 76, 78, 80]
_AWAY = [0, 6, 12, 18, 24, 30, 33, 36, 39, 42, 45, 48, 51, 53, 55, 57, 59, 61, 62, 63]
_TOTALS = [h + a for h, a in zip(_HOME, _AWAY)]  # ends at 143
_CLOCKS = ["10:00", "08:00", "06:00", "04:00", "02:00"]
# line on every snapshot: OLV=170 -> CLV=189 (movement UP all game)
_LINES = [170 + i for i in range(20)]


def _build(db: Path, gid: str, *, status: str = "ended", nsnaps: int = 20,
           lines: list | None = None, ws: list[tuple[int, float]] | None = None,
           dip: bool = False,
           start: datetime | None = None) -> None:
    """Insert a synthetic game.  ws = [(snapshot_idx, line), ...] WS
    observations; lines = per-snapshot total_line (None = no market).
    start overrides the game's first_seen_at (default: now - 2h)."""
    st = PokerBetStore(db)
    base = start or (_now() - timedelta(hours=2))
    game = PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="comp-1", competition_slug="betual-tbsl",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA",
        sport="basketball", home_team=f"{gid} Home Virtual",
        away_team=f"{gid} Away Virtual",
        game_slug=f"{gid.lower()}-game",
        source_url=f"https://x/{gid}", status=status,
        first_seen_at=_iso(base), last_seen_at=_iso(base + timedelta(minutes=3 * (nsnaps - 1))),
    )
    gid_db = st.upsert_game(game)
    for i in range(nsnaps):
        t = base + timedelta(minutes=3 * i)
        q = i // 5 + 1
        clock = "00:00" if (i == nsnaps - 1 and q >= 4) else _CLOCKS[i % 5]
        hs, as_ = _HOME[i], _AWAY[i]
        if dip and i == 6:  # score regression -> quality INVALID
            hs = _HOME[5] - 1
        obs = MarketObservation(
            source="PokerBet", source_game_id=gid, classification="BETUAL_NBA",
            captured_at=_iso(t),
            home_team=f"{gid} Home Virtual", away_team=f"{gid} Away Virtual",
            home_score=hs, away_score=as_,
            period_label=f"{q}th Quarter", quarter=q, clock=clock,
            game_status=status,
            total_line=(lines[i] if lines is not None else None),
            spread=None, w1_odds=None, w2_odds=None,
            markets_json=json.dumps({"total": {"first_line": lines[i]}}) if lines else "{}",
        )
        st.insert_snapshot(gid_db, obs, force=True)
    for idx, line in (ws or []):
        st.upsert_market_observation({
            "game_id": gid_db, "source_game_id": gid,
            "captured_at": _iso(base + timedelta(minutes=3 * idx, seconds=30)),
            "market_type": "MatchTotal", "market_name": "Match Total",
            "line_value": line, "over_price": 1.9, "under_price": 1.9,
            "home_score": 0, "away_score": 0,
            "period_label": "", "clock": "", "raw_json": "{}",
        })


@pytest.fixture
def db(tmp_path, monkeypatch):
    dbfile = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(dbfile))
    # G-MIX: clean completed game, market on every snapshot, both directions.
    _build(dbfile, "G-MIX", lines=_LINES)
    # G-PUSH: market lands exactly on the actual -> every outcome is PUSH.
    _build(dbfile, "G-PUSH", lines=[143] * 20)
    # G-NOMKT: no market anywhere -> honest NULLs, but rows must exist.
    _build(dbfile, "G-NOMKT")
    # G-WS: no snapshot lines, market only via WS observations.  Each
    # observation sits BEFORE the snapshot it must freeze for (the
    # at-or-before rule): obs idx N lands at base+3N min + 30s.
    _build(dbfile, "G-WS", ws=[(0, 168.5), (1, 172.5), (9, 180.5), (18, 189.5)])
    # G-LIVE: still live -> not completed -> no rows.
    _build(dbfile, "G-LIVE", status="live", lines=_LINES)
    # G-BAD: score regression -> quality INVALID -> no rows.
    _build(dbfile, "G-BAD", lines=_LINES, dip=True)
    # G-FRAG: OK result but only 8 snapshots (< 15) -> fragment -> no rows.
    _build(dbfile, "G-FRAG", nsnaps=8, lines=[170 + 2 * i for i in range(8)])
    return dbfile


@pytest.fixture
def sc(db):
    s = Scorecard(db)
    s.capture_results()
    return s


def _rows(sc: Scorecard, gid: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{sc._db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM checkpoint_market WHERE source_game_id=? "
            "ORDER BY checkpoint_pct", (gid,))]
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════

def test_cm_clean_game_records_all_ten_checkpoints(sc):
    """G-MIX records exactly pct10..pct90 + pct100, all key fields set."""
    stats = sc.record_checkpoint_market()
    assert stats["recorded"] >= 10
    rows = _rows(sc, "G-MIX")
    assert [r["checkpoint_pct"] for r in rows] == list(range(10, 101, 10))
    for r in rows:
        for key in ("checkpoint_pct", "checkpoint_timestamp", "opening_line",
                    "live_market_line", "blm_fair_value", "closing_line",
                    "actual_final_total", "market_vs_fair", "signal",
                    "blm_vs_olv", "blm_vs_clv", "olv_to_clv",
                    "market_move_toward_blm", "outcome", "frozen"):
            assert r[key] is not None, f"{r['checkpoint_pct']} missing {key}"
        assert r["frozen"] == 1


def test_cm_signed_disparity_and_signal(sc):
    """market_vs_fair = live - fair, sign retained; early OVER_VALUE
    (market below fair), late UNDER_VALUE (market above fair)."""
    sc.record_checkpoint_market()
    rows = {r["checkpoint_pct"]: r for r in _rows(sc, "G-MIX")}
    for pct in rows:
        r = rows[pct]
        assert r["market_vs_fair"] == pytest.approx(
            round(r["live_market_line"] - r["blm_fair_value"], 2))
    # pct10: pace-hot game, fair 182.x vs market 172 -> NEGATIVE disparity
    assert rows[10]["market_vs_fair"] < 0
    assert rows[10]["signal"] == "OVER_VALUE"
    # pct50: fair 148.x vs market 180 -> POSITIVE disparity
    assert rows[50]["market_vs_fair"] > 0
    assert rows[50]["signal"] == "UNDER_VALUE"
    # pct100: terminal, fair >= actual (floor) -> positive disparity retained
    assert rows[100]["market_vs_fair"] > 0
    assert rows[100]["signal"] == "UNDER_VALUE"


def test_cm_outcome_classification(sc):
    """M009 spec: BLM below market + actual below market = UNDER WIN;
    BLM above market + actual below market = OVER LOSS; exact = PUSH."""
    sc.record_checkpoint_market()
    rows = {r["checkpoint_pct"]: r for r in _rows(sc, "G-MIX")}
    # pct50: fair 148 < market 180, actual 143 < 180 -> UNDER WIN
    assert rows[50]["outcome"] == "UNDER_WIN"
    # pct10: fair 182 > market 172, actual 143 < 172 -> OVER LOSS
    assert rows[10]["outcome"] == "OVER_LOSS"
    # G-PUSH: market == actual -> PUSH at every checkpoint
    pushes = _rows(sc, "G-PUSH")
    assert pushes, "G-PUSH must record rows"
    for r in pushes:
        assert r["outcome"] == "PUSH"


def test_cm_olv_clv_actual_linkage(sc):
    """OLV = first line (170), CLV = last line (189), actual = 143;
    blm_vs_olv / blm_vs_clv / olv_to_clv follow the arithmetic."""
    sc.record_checkpoint_market()
    rows = {r["checkpoint_pct"]: r for r in _rows(sc, "G-MIX")}
    assert rows[10]["opening_line"] == 170.0
    assert rows[100]["closing_line"] == 189.0
    for pct, r in rows.items():
        assert r["actual_final_total"] == 143
        assert r["blm_vs_olv"] == pytest.approx(
            round(r["blm_fair_value"] - r["opening_line"], 2))
        assert r["blm_vs_clv"] == pytest.approx(
            round(r["blm_fair_value"] - r["closing_line"], 2))
        assert r["olv_to_clv"] == pytest.approx(
            round(r["closing_line"] - r["opening_line"], 2))
        assert r["olv_to_clv"] == 19.0


def test_cm_market_move_toward_blm(sc):
    """Market subsequently moved TOWARD / AWAY / UNCHANGED vs BLM fair:
    compare |CLV - fair| against |OLV - fair| (M009 section 10)."""
    sc.record_checkpoint_market()
    rows = _rows(sc, "G-MIX")
    for r in rows:
        assert r["market_move_toward_blm"] in ("TOWARD", "AWAY", "UNCHANGED")
    # OLV 170, CLV 189, fair ~148 at pct50: |189-148|=41 vs |170-148|=22
    # -> closing is FARTHER from fair than opening -> AWAY
    r50 = next(r for r in rows if r["checkpoint_pct"] == 50)
    assert r50["market_move_toward_blm"] == "AWAY"


def test_cm_immutable_no_rebase(sc):
    """A second run never rewrites or duplicates recorded rows (unlike
    predictions, checkpoint_market is frozen at first write)."""
    sc.record_checkpoint_market()
    first = _rows(sc, "G-MIX")
    sc.record_checkpoint_market()
    second = _rows(sc, "G-MIX")
    assert len(second) == len(first) == 10
    assert second == first


def test_cm_skips_ineligible_games(sc):
    """Live, INVALID-quality, and fragment (<15 snaps) games record
    nothing — the headline base must only ever be clean completed games."""
    sc.record_checkpoint_market()
    for gid in ("G-LIVE", "G-BAD", "G-FRAG"):
        assert _rows(sc, gid) == [], f"{gid} must be excluded"


def test_cm_missing_market_honest_nulls(sc):
    """No market anywhere -> rows still exist (fair = pace-based) but
    market-linked fields are NULL: no signal, no disparity, no outcome."""
    sc.record_checkpoint_market()
    rows = _rows(sc, "G-NOMKT")
    assert rows, "G-NOMKT must record fair values"
    for r in rows:
        assert r["live_market_line"] is None
        assert r["opening_line"] is None
        assert r["closing_line"] is None
        assert r["market_vs_fair"] is None
        assert r["signal"] is None
        assert r["outcome"] is None
        assert r["blm_fair_value"] is not None  # pace-based fair still exists


def test_cm_ws_fallback_lines(sc):
    """Snapshot-less games get OLV/CLV/live lines from the eu-swarm WS
    observations (first / last / at-or-before checkpoint)."""
    sc.record_checkpoint_market()
    rows = {r["checkpoint_pct"]: r for r in _rows(sc, "G-WS")}
    assert rows[10]["opening_line"] == 168.5
    assert rows[10]["live_market_line"] == 172.5  # WS obs at/before pct10
    assert rows[50]["live_market_line"] == 180.5
    assert rows[100]["closing_line"] == 189.5
    assert rows[100]["live_market_line"] == 189.5


def test_cm_terminal_fair_floor(sc):
    """At pct100 the fair value is the model's FINAL projection which the
    live-score floor pins at-or-above the actual total."""
    sc.record_checkpoint_market()
    rows = {r["checkpoint_pct"]: r for r in _rows(sc, "G-MIX")}
    assert rows[100]["blm_fair_value"] >= rows[100]["actual_final_total"]
