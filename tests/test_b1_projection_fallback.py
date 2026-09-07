"""B1 — projection layer game-time fallback from ``quarter`` to period label.

Production list/WS snapshots store only ``period_label`` + ``clock``
(``quarter`` is NULL on ~95% of rows), so ``project()``/``pace_from_snapshots``
returned ``elapsed=None, progress=None`` and the game-clock pace fallback was
unavailable.  The projection layer must resolve the quarter exactly like the
already-working scorecard/API paths:

  1. structured ``quarter`` when available;
  2. else ``period_quarter(period_label)``;
  3. half-time boundary rows pinned at 50%.

The result must propagate into derived records (checkpoint_market /
predictions store ``proj[elapsed_minutes]``/``proj[progress]`` directly), so
label-only games freeze non-NULL elapsed/progress — never backfilled.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.projection import (pace_from_snapshots, period_quarter,
                               project, row_elapsed_minutes)
from blm_v4.scorecard import Scorecard
from blm_v4.storage import PokerBetStore

HERE = Path(__file__).resolve().parent


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row(cls: str, period_label: str, clock: str, hs: int, aw: int,
         total_line: float = 185.5) -> dict:
    """ONE label-only scored snapshot (quarter omitted — the production case)."""
    return {
        "classification": cls,
        "captured_at": _iso(_now() - timedelta(minutes=30)),
        "quarter": None,
        "period_label": period_label,
        "clock": clock,
        "home_score": hs,
        "away_score": aw,
        "total_line": total_line,
    }


# ── period label resolution ──────────────────────────────────────────


def test_period_quarter_label_fallback():
    assert period_quarter("1st Quarter") == 1
    assert period_quarter("2nd Quarter") == 2
    assert period_quarter("3rd Quarter") == 3
    assert period_quarter("4th Quarter") == 4
    assert period_quarter("Half Time") == 2
    assert period_quarter("Half End") == 2
    assert period_quarter("Finished") is None
    assert period_quarter(None) is None


def test_row_elapsed_half_time_pinned():
    # half-time boundary: 50% regardless of the clock sentinel display
    assert row_elapsed_minutes(
        {"quarter": None, "period_label": "Half Time", "clock": "10:00"},
        10.0, 40.0) == 20.0
    assert row_elapsed_minutes(
        {"quarter": None, "period_label": "Half End", "clock": "12:00"},
        12.0, 48.0) == 24.0


# ── project() with quarter-less rows ─────────────────────────────────


def test_project_label_only_betual():
    """BETUAL: 2nd Quarter 07:49 -> elapsed 12.18 / 40, progress ~30.5%."""
    p = project([_row("BETUAL_NBA", "2nd Quarter", "07:49", 45, 40)])
    assert p["elapsed_minutes"] == pytest.approx(12.1833, abs=0.01)
    assert p["progress"] == pytest.approx(12.1833 / 40.0, abs=1e-3)
    assert p["elapsed_minutes"] is not None and p["progress"] is not None


def test_project_label_only_cyber():
    """CYBER: 2nd Quarter 07:49 -> elapsed 16.18 / 48, progress ~33.715%."""
    p = project([_row("CYBER_2K26", "2nd Quarter", "07:49", 45, 40)])
    assert p["elapsed_minutes"] == pytest.approx(16.1833, abs=0.01)
    assert p["progress"] == pytest.approx(16.1833 / 48.0, abs=1e-3)
    assert p["elapsed_minutes"] is not None and p["progress"] is not None


def test_project_structured_quarter_still_wins():
    """Structured quarter remains authoritative when present."""
    p = project([{**_row("CYBER_2K26", "2nd Quarter", "07:49", 45, 40),
                  "quarter": 2}])
    assert p["elapsed_minutes"] == pytest.approx(16.1833, abs=0.01)


def test_project_finished_no_period_returns_none():
    """A Finished row has no resolvable period -> elapsed None (unchanged)."""
    p = project([_row("BETUAL_NBA", "Finished", "00:00", 120, 110)])
    assert p["elapsed_minutes"] is None
    assert p["progress"] is None


# ── game-clock pace fallback with quarter-less rows ──────────────────


def test_pace_fallback_label_only_available():
    """One label-only scored row: the game-clock pace fallback must work
    (previously it silently vanished because quarter was NULL)."""
    rows = [{"classification": "CYBER_2K26",
             "captured_at": _iso(_now() - timedelta(minutes=30)),
             "quarter": None, "period_label": "2nd Quarter", "clock": "06:00",
             "home_score": 30, "away_score": 30}]
    pace = pace_from_snapshots(rows)
    # elapsed = 12 + (12 - 6) = 18 -> pace = 60/18 * 48 = 160.0
    assert pace == 160.0


def test_pace_fallback_betual_scale():
    rows = [{"classification": "BETUAL_NBA",
             "captured_at": _iso(_now() - timedelta(minutes=30)),
             "quarter": None, "period_label": "2nd Quarter", "clock": "06:00",
             "home_score": 30, "away_score": 30}]
    # elapsed = 10 + (10 - 6) = 14 -> pace = 60/14 * 40 = 171.4
    assert pace_from_snapshots(rows) == 171.4


# ── persistence into derived records (checkpoint_market) ─────────────

# label-only CYBER game: el 0..32 step 2, then 36/40/44/48.  In-play rows
# carry ONLY period_label + clock (quarter NULL); the terminal row keeps an
# explicit quarter so its stored elapsed resolves (mirrors the original
# classification-duration fixture, minus the structured quarter).
_CYBER_ELS = list(range(0, 34, 2)) + [36, 40, 44, 48]


def _build_label_only_cyber(db: Path, gid: str = "C-LABELONLY") -> None:
    st = PokerBetStore(db)
    base = _now() - timedelta(hours=2)
    game = PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="comp-cyber", competition_slug="cyber-basketball-2k26",
        competition="Cyber Basketball 2K26", region="Virtual Matches",
        game_family="cyber", classification="CYBER_2K26",
        sport="basketball", home_team=f"{gid} Home Cyber",
        away_team=f"{gid} Away Cyber", game_slug=f"{gid.lower()}-game",
        source_url=f"https://x/{gid}", status="ended",
        first_seen_at=_iso(base),
        last_seen_at=_iso(base + timedelta(minutes=2 * (len(_CYBER_ELS) - 1))),
    )
    gid_db = st.upsert_game(game)
    for i, el in enumerate(_CYBER_ELS):
        qlen = 12.0
        if el == 48:
            clock, label = "00:00", "Finished"
            q = 4
        else:
            q = min(el // 12 + 1, 4)
            mod = el % 12
            clock = "12:00" if mod == 0 else f"{12 - mod:02d}:00"
            label = {1: "1st Quarter", 2: "2nd Quarter",
                     3: "3rd Quarter", 4: "4th Quarter"}[q]
        obs = MarketObservation(
            source="PokerBet", source_game_id=gid, classification="CYBER_2K26",
            captured_at=_iso(base + timedelta(minutes=2 * i)),
            home_team=f"{gid} Home Cyber", away_team=f"{gid} Away Cyber",
            home_score=el, away_score=el,          # total = 2*el (0 .. 96)
            period_label=label,
            quarter=q if el == 48 else None,       # label-only during play
            clock=clock,
            game_status="ended", total_line=200.0,
            spread=None, w1_odds=None, w2_odds=None,
            markets_json="{}",
        )
        st.insert_snapshot(gid_db, obs, force=True)


@pytest.fixture
def label_only_cyber(tmp_path, monkeypatch):
    dbfile = tmp_path / "blm_pokerbet.db"
    monkeypatch.setenv("BLM_POKERBET_DB", str(dbfile))
    _build_label_only_cyber(dbfile)
    s = Scorecard(dbfile)
    s.capture_results()
    s.record_checkpoint_market()
    return dbfile


def _cm_rows(dbfile: Path, gid: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{dbfile}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM checkpoint_market WHERE source_game_id=? "
            "ORDER BY checkpoint_pct", (gid,))]
    finally:
        conn.close()


def test_checkpoint_persistence_label_only(label_only_cyber):
    """Label-only in-play snapshots must freeze NON-NULL stored
    progress/elapsed_minutes into checkpoint_market (the production
    failure mode: derived records were NULL because project() had no
    period-label fallback)."""
    rows = {r["checkpoint_pct"]: r for r in _cm_rows(label_only_cyber, "C-LABELONLY")}
    assert rows[50]["progress"] == pytest.approx(0.5, abs=1e-3)
    assert rows[50]["elapsed_minutes"] == pytest.approx(24.0, abs=0.5)
    assert rows[30]["progress"] is not None
    assert rows[30]["elapsed_minutes"] is not None
    # terminal row resolves via its explicit quarter (unchanged behavior)
    assert rows[100]["progress"] == 1.0
    assert rows[100]["elapsed_minutes"] == 48.0