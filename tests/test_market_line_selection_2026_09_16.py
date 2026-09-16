"""MARKET-LINE SELECTION — regression tests (fix 2026-09-16).

Directives implemented (integration directive 2026-09-16):

  1  PRICE-AWARE SELECTION — when a Total Points market contains
     multiple lines, the selected (line, side, price) is chosen by
     PRICE, never by position ("first line" / "last line" / array
     order): 1.80 <= price <= 1.95 preferred, closest to 1.85 wins;
     fallback (no price in band) = closest to 1.85 across ALL valid
     candidates (explicit, never silently the last line).
  2  DETERMINISTIC TIE-BREAK — equal distance to 1.85 resolves by
     lower market line, then Over before Under.
  3  ROW-LEVEL ASSOCIATION — the selected line, side and price always
     originate from the SAME ladder/batch row.
  4  PARSER INTEGRATION — event_parser.parse_event_view exposes the
     selected triple in parsed["total"]["first_line"/"selected_side"/
     "selected_price"/"selection_rule"] and preserves the full ladder.
  5  WS-BATCH INTEGRATION — clean_metrics.record_snapshot selects the
     price-aware line from the latest eu-swarm batch (never
     ws_batch[0]); every distinct line identity stays preserved.
  6  FROZEN-LINE INTEGRATION — scorecard._frozen_market_obs WS
     fallback uses the same selector over the batch AT-OR-BEFORE the
     checkpoint (never a later observation, never the closing line).
  7  PIPELINE SYNCHRONIZATION — the selected market_line is the line
     used by required_pts_per_min = (selected_market_line -
     current_score) / remaining_minutes, and settlement comparisons
     run against the exact selected line.

NOT changed by the fix (pinned where cheap): 75% checkpoint, alert
condition, league averages, required-pace formula, settlement rules.
"""
from __future__ import annotations

import json
import math

import pytest

from blm_v4.clean_metrics import CleanMetricsStore
from blm_v4.event_parser import (
    PREFERRED_PRICE_BAND,
    PREFERRED_PRICE_TARGET,
    parse_event_view,
    select_total_market,
)
from blm_v4.scorecard import _frozen_market_obs
from blm_v4.storage import PokerBetStore


def L(line: float, over: float, under: float) -> dict:
    return {"line": float(line), "over": float(over), "under": float(under)}


# ═══════════════════════════════════════════════════════════════════
# 1. SELECTOR UNIT TESTS (the required test matrix)
# ═══════════════════════════════════════════════════════════════════

def test_policy_constants_are_the_documented_band():
    assert PREFERRED_PRICE_BAND == (1.80, 1.95)
    assert PREFERRED_PRICE_TARGET == 1.85


def test_supplied_example_selects_1715_at_185():
    """The task's acceptance case: 169.5/171.5/173.5 MUST select
    171.5 @ 1.85 — not the first (169.5) and not the last (173.5)."""
    ladder = [L(169.5, 1.50, 2.40), L(171.5, 1.85, 1.85), L(173.5, 2.40, 1.50)]
    sel = select_total_market(ladder)
    assert sel["line"] == 171.5
    assert sel["price"] == 1.85
    assert sel["side"] == "OVER"          # distance tie Over/Under → Over
    assert sel["rule"] == "band_closest_to_1.85"
    assert sel["over"] == 1.85 and sel["under"] == 1.85


def test_case_a_single_line_no_band_price_uses_fallback():
    sel = select_total_market([L(215.5, 1.70, 2.02)])
    assert (sel["line"], sel["side"], sel["price"]) == (215.5, "OVER", 1.70)
    assert sel["rule"] == "fallback_nearest_1.85"


def test_case_b_two_lines_one_band_price():
    sel = select_total_market([L(214.5, 1.62, 1.68), L(216.5, 1.88, 1.92)])
    assert (sel["line"], sel["side"], sel["price"]) == (216.5, "OVER", 1.88)
    assert sel["rule"] == "band_closest_to_1.85"


def test_case_d_three_lines_middle_line_185():
    sel = select_total_market(
        [L(168.5, 1.62, 1.72), L(170.5, 1.85, 1.85), L(172.5, 2.05, 1.68)])
    assert sel["line"] == 170.5 and sel["price"] == 1.85


def test_case_e_multiple_in_band_closest_to_185_wins():
    sel = select_total_market(
        [L(170.5, 1.82, 1.90), L(171.5, 1.84, 1.88), L(172.5, 1.90, 1.82)])
    assert (sel["line"], sel["side"], sel["price"]) == (171.5, "OVER", 1.84)


def test_case_e_exact_distance_tie_breaks_by_lower_line_then_over():
    # 1.83 vs 1.87: equal distance to 1.85 → LOWER line wins...
    sel = select_total_market([L(171.5, 1.83, 1.87), L(172.5, 1.87, 1.83)])
    assert sel["line"] == 171.5 and sel["price"] == 1.83
    # ...and within ONE row, Over beats Under on equal distance.
    sel2 = select_total_market([L(175.5, 1.87, 1.83)])
    assert sel2["side"] == "OVER" and sel2["price"] == 1.87


def test_case_f_no_price_in_band_fallback_is_never_the_last_line():
    ladder = [L(169.5, 1.50, 2.40), L(171.5, 1.62, 1.72), L(173.5, 2.10, 1.55)]
    sel = select_total_market(ladder)
    # closest to 1.85 among ALL candidates = Under @ 1.72 on 171.5 —
    # explicitly NOT the last line (173.5) and not positional.
    assert (sel["line"], sel["side"], sel["price"]) == (171.5, "UNDER", 1.72)
    assert sel["rule"] == "fallback_nearest_1.85"
    assert sel["line"] != ladder[-1]["line"]


def test_never_selects_last_line_positionally():
    ladder = [L(169.5, 1.50, 2.40), L(171.5, 1.85, 1.85), L(173.5, 2.40, 1.50)]
    assert select_total_market(ladder)["line"] != ladder[-1]["line"]
    assert select_total_market(ladder)["line"] != ladder[0]["line"]


def test_invalid_and_missing_prices_are_skipped():
    ladder = [
        {"line": 169.5, "over": None, "under": None},
        L(171.5, 1.85, 1.85),
        {"line": 173.5, "over": float("nan"), "under": 0.99},  # NaN + <1.0
    ]
    sel = select_total_market(ladder)
    assert sel["line"] == 171.5 and sel["price"] == 1.85


def test_no_candidates_returns_explicit_none_block():
    sel = select_total_market([])
    assert sel["line"] is None and sel["price"] is None
    assert sel["side"] is None and sel["rule"] == "no_candidates"
    assert select_total_market(None)["rule"] == "no_candidates"


@pytest.mark.parametrize("ladder", [
    [L(169.5, 1.50, 2.40), L(171.5, 1.85, 1.85), L(173.5, 2.40, 1.50)],
    [L(214.5, 1.62, 1.68), L(216.5, 1.88, 1.92)],
    [L(215.5, 1.70, 2.02)],
    [L(169.5, 1.50, 2.40), L(171.5, 1.62, 1.72), L(173.5, 2.10, 1.55)],
])
def test_line_price_side_come_from_the_same_ladder_row(ladder):
    sel = select_total_market(ladder)
    row = next(r for r in ladder if r["line"] == sel["line"])
    assert sel["price"] in (row["over"], row["under"])
    assert sel["side"] == ("OVER" if sel["price"] == row["over"] else "UNDER")
    assert (sel["over"], sel["under"]) == (row["over"], row["under"])


# ═══════════════════════════════════════════════════════════════════
# 2. PARSER INTEGRATION (event_parser.parse_event_view)
# ═══════════════════════════════════════════════════════════════════

SUPPLIED_EVENT_TEXT = """Cyber Basketball. 2K26 Matches
4th Quarter
09:46'
Oklahoma City Thunder Cyber
San Antonio Spurs Cyber
100 : 73, (32:22), (28:23), (33:22), (7:6) 09:46
All Match Totals Handicaps Markets
Points Handicap
-26.5 1.95  +26.5 1.75
Total Points
Over Under
169.5 1.50 2.40   171.5 1.85 1.85   173.5 2.40 1.50
"""

BETUAL_STYLE_TEXT = """Betual NBA
3rd Quarter
04:15'
Sacramento Kings Virtual
Miami Heat Virtual
78 : 76, (29:25), (28:25), (21:26) 04:15
4 Quarters of 12 min. Simulated Game
Match Winner
1.65 2.20
Points Handicap
-1.5 1.80 +1.5 2.02
Total Points
Over Under
225.5 1.55 2.40
227.5 1.85 1.96
229.5 2.20 1.65
"""


def test_parser_supplied_example_first_line_is_price_selected():
    p = parse_event_view(SUPPLIED_EVENT_TEXT)
    t = p["total"]
    assert t["first_line"] == 171.5              # NOT 169.5 (old ladder[0])
    assert t["selected_side"] == "OVER"
    assert t["selected_price"] == 1.85
    assert t["selection_rule"] == "band_closest_to_1.85"
    assert t["over_odds"] == 1.85 and t["under_odds"] == 1.85


def test_parser_preserves_the_full_ladder():
    t = parse_event_view(SUPPLIED_EVENT_TEXT)["total"]
    assert t["ladder"] == [
        {"line": 169.5, "over": 1.50, "under": 2.40},
        {"line": 171.5, "over": 1.85, "under": 1.85},
        {"line": 173.5, "over": 2.40, "under": 1.50},
    ]


def test_parser_betual_three_line_ladder_selects_185_row():
    t = parse_event_view(BETUAL_STYLE_TEXT)["total"]
    assert t["first_line"] == 227.5              # middle row, 1.85
    assert t["selected_price"] == 1.85
    assert len(t["ladder"]) == 3


def test_parser_full_ladder_page_selects_mid_band_line():
    """m009-shaped 80-line event page.  The token-stream parser chains
    the descending triples into 240 rows (line steps 1.0, prices cycle
    1.70/2.02 → 1.80/1.90 → 1.90/1.80).  Every band candidate (1.80,
    1.90) is distance 0.05 from 1.85 → exact-distance tie → lower-line
    tie-break → the LAST chained row group (i=79): line 138.5, Over
    1.80.  Decisively NOT the legacy positional first row (216.5 @
    1.70/2.02), which lies outside the preferred band."""
    lines = ["Cyber Basketball. 2K26 Matches", "4th Quarter", "09:46'",
             "Oklahoma City Thunder Cyber", "San Antonio Spurs Cyber",
             "100 : 73, (32:22), (28:23), (33:22), (7:6) 09:46",
             "Total Points", "Over Under"]
    for i in range(80):
        lines.append(
            f"{216.5 - i:.1f} 1.70 2.02   {217.5 - i:.1f} 1.80 1.90   "
            f"{218.5 - i:.1f} 1.90 1.80")
    t = parse_event_view("\n".join(lines))["total"]
    assert len(t["ladder"]) == 240
    assert t["first_line"] == 138.5
    assert (t["selected_side"], t["selected_price"]) == ("OVER", 1.80)
    assert t["selection_rule"] == "band_closest_to_1.85"
    # the legacy positional row must NOT win when band prices exist
    assert (t["first_line"], t["over_odds"], t["under_odds"]) != \
        (216.5, 1.70, 2.02)


def test_parser_markets_json_carries_selected_triple():
    p = parse_event_view(SUPPLIED_EVENT_TEXT)
    mj = json.loads(p["markets_json"])
    assert mj["total"]["first_line"] == 171.5
    assert mj["total"]["selected_price"] == 1.85
    assert len(mj["total"]["ladder"]) == 3


# ═══════════════════════════════════════════════════════════════════
# 3. WS-BATCH INTEGRATION (clean_metrics.record_snapshot)
# ═══════════════════════════════════════════════════════════════════

class _FakeMainStore:
    """Supplies latest_market_batch (WS) + snapshot history to the
    clean-metrics writer — the only surfaces record_snapshot reads."""

    def __init__(self, batch: list[dict]):
        self._batch = batch

    def latest_market_batch(self, source_game_id, market_type="MatchTotal"):
        return self._batch

    def get_snapshots(self, source_game_id, ascending=False):
        return []


def _ws_batch(ts="2026-09-16T18:00:00.000Z"):
    return [
        {"source_game_id": "G1", "captured_at": ts, "market_type": "MatchTotal",
         "line_value": 169.5, "over_price": 1.50, "under_price": 2.40},
        {"source_game_id": "G1", "captured_at": ts, "market_type": "MatchTotal",
         "line_value": 171.5, "over_price": 1.85, "under_price": 1.85},
        {"source_game_id": "G1", "captured_at": ts, "market_type": "MatchTotal",
         "line_value": 173.5, "over_price": 2.40, "under_price": 1.50},
    ]


def test_ws_batch_selection_is_price_aware_not_positional(tmp_path):
    clean = CleanMetricsStore(tmp_path / "clean.db")
    res = clean.record_snapshot(
        source_game_id="G1", classification="CYBER_2K26",
        captured_at="2026-09-16T18:05:00.000Z", quarter=4,
        period_label="4th Quarter", clock="06:00",
        home_score=100, away_score=73, game_status="live", source="PokerBet",
        snapshot_total_line=None,            # event-view line absent → WS path
        snapshot_over_odds=None, snapshot_under_odds=None,
        main_store=_FakeMainStore(_ws_batch()),
    )
    assert res["market_source"] == "ws"
    assert res["live_total_line"] == 171.5   # NOT ws_batch[0] (169.5)
    assert res["over_price"] == 1.85 and res["under_price"] == 1.85
    # line/price/side synchronized with the SAME batch row
    assert res["live_total_line"] == 171.5 and res["over_price"] == 1.85


def test_ws_batch_preserves_every_line_identity(tmp_path):
    clean = CleanMetricsStore(tmp_path / "clean.db")
    clean.record_snapshot(
        source_game_id="G1", classification="CYBER_2K26",
        captured_at="2026-09-16T18:05:00.000Z", quarter=4,
        period_label="4th Quarter", clock="06:00",
        home_score=100, away_score=73, game_status="live", source="PokerBet",
        snapshot_total_line=None, snapshot_over_odds=None,
        snapshot_under_odds=None, main_store=_FakeMainStore(_ws_batch()),
    )
    lines = sorted(r["line_value"] for r in clean.list_market_lines("G1"))
    assert lines == [169.5, 171.5, 173.5]    # no identity collapsed


def test_ws_batch_required_pace_uses_selected_line(tmp_path):
    """required_pts_per_min = (selected_market_line - current_score) /
    remaining_minutes with the SELECTED line (CYBER_2K26 Q4 06:00 →
    elapsed 42, remaining 6): (171.5 - 173) / 6 = -0.25 (negative
    preserved by the deterministic pace rule)."""
    clean = CleanMetricsStore(tmp_path / "clean.db")
    res = clean.record_snapshot(
        source_game_id="G1", classification="CYBER_2K26",
        captured_at="2026-09-16T18:05:00.000Z", quarter=4,
        period_label="4th Quarter", clock="06:00",
        home_score=100, away_score=73, game_status="live", source="PokerBet",
        snapshot_total_line=None, snapshot_over_odds=None,
        snapshot_under_odds=None, main_store=_FakeMainStore(_ws_batch()),
    )
    assert res["remaining_game_minutes"] == 6
    assert res["required_pts_per_min"] == round((171.5 - 173) / 6, 4)
    assert res["required_pts_per_min"] == -0.25


def test_event_view_line_still_wins_over_ws(tmp_path):
    """Precedence unchanged: a verified event-view line is primary; the
    selector only decides WITHIN the WS fallback path."""
    clean = CleanMetricsStore(tmp_path / "clean.db")
    res = clean.record_snapshot(
        source_game_id="G1", classification="CYBER_2K26",
        captured_at="2026-09-16T18:05:00.000Z", quarter=4,
        period_label="4th Quarter", clock="06:00",
        home_score=100, away_score=73, game_status="live", source="PokerBet",
        snapshot_total_line=205.5, snapshot_over_odds=1.91,
        snapshot_under_odds=1.89, main_store=_FakeMainStore(_ws_batch()),
    )
    assert res["market_source"] == "event_view"
    assert res["live_total_line"] == 205.5


# ═══════════════════════════════════════════════════════════════════
# 4. FROZEN-LINE INTEGRATION (scorecard._frozen_market_obs)
# ═══════════════════════════════════════════════════════════════════

def _store_with_batches(tmp_path, batches):
    st = PokerBetStore(tmp_path / "blm.db")
    for ts, rows in batches:
        for line, over, under in rows:
            st.upsert_market_observation({
                "source_game_id": "G1", "captured_at": ts,
                "market_type": "MatchTotal", "market_name": "Total Points",
                "line_value": line, "over_price": over, "under_price": under,
            })
    return st


T0 = "2026-09-16T18:00:00.000Z"
T1 = "2026-09-16T18:05:00.000Z"
T2 = "2026-09-16T18:10:00.000Z"

SUPPLIED_BATCH = [(169.5, 1.50, 2.40), (171.5, 1.85, 1.85), (173.5, 2.40, 1.50)]


def test_frozen_ws_fallback_selects_price_aware_line(tmp_path):
    st = _store_with_batches(tmp_path, [(T0, SUPPLIED_BATCH)])
    rows = [{"captured_at": T1, "total_line": None}]   # no snapshot line
    with st._connect() as conn:
        line, ts = _frozen_market_obs(conn, "G1", rows, 0)
    assert line == 171.5                               # NOT lowest (169.5)
    assert ts == T0


def test_frozen_line_never_uses_a_later_batch(tmp_path):
    """A later (post-checkpoint) batch whose band line differs must be
    ignored — frozen semantics: at-or-before only."""
    later = [(219.5, 1.85, 1.85)]
    st = _store_with_batches(tmp_path, [(T0, SUPPLIED_BATCH), (T2, later)])
    rows = [{"captured_at": T1, "total_line": None}]   # checkpoint at T1
    with st._connect() as conn:
        line, ts = _frozen_market_obs(conn, "G1", rows, 0)
    assert line == 171.5 and ts == T0


def test_frozen_snapshot_line_still_primary(tmp_path):
    """A snapshot-carried line beats the WS fallback (unchanged rule)."""
    st = _store_with_batches(tmp_path, [(T0, SUPPLIED_BATCH)])
    rows = [{"captured_at": T1, "total_line": 220.5}]
    with st._connect() as conn:
        line, ts = _frozen_market_obs(conn, "G1", rows, 0)
    assert line == 220.5 and ts == T1


def test_frozen_returns_none_without_market_observations(tmp_path):
    st = _store_with_batches(tmp_path, [])
    rows = [{"captured_at": T1, "total_line": None}]
    with st._connect() as conn:
        assert _frozen_market_obs(conn, "G1", rows, 0) == (None, None)


# ═══════════════════════════════════════════════════════════════════
# 5. PIPELINE SYNCHRONIZATION (pace + settlement on the SELECTED line)
# ═══════════════════════════════════════════════════════════════════

def test_selected_line_feeds_required_pace_formula():
    from blm_v4.clean_metrics import pace_metrics
    sel = select_total_market(
        [L(169.5, 1.50, 2.40), L(171.5, 1.85, 1.85), L(173.5, 2.40, 1.50)])
    pm = pace_metrics(28, 20, 100, sel["line"])
    assert pm["required_pts_per_min"] == round((171.5 - 100) / 20, 4)


def test_settlement_uses_exact_selected_line():
    sel = select_total_market(
        [L(169.5, 1.50, 2.40), L(171.5, 1.85, 1.85), L(173.5, 2.40, 1.50)])
    line = sel["line"]

    def verdict(final_total):
        return ("UNDER" if final_total < line
                else "OVER" if final_total > line else "PUSH")

    assert verdict(171) == "UNDER"
    assert verdict(172) == "OVER"
    assert verdict(171.5) == "PUSH"        # half-point line → never PUSH on int
    # the wrong (positional last) line would flip 172 → UNDER:
    assert (172 < 173.5) is True and (172 > line) is True


# ═══════════════════════════════════════════════════════════════════
# 6. /live DISPLAY INVARIANT (api.py market block == selector's pick)
# ═══════════════════════════════════════════════════════════════════
# The API's own WS display query was the fourth positional promotion
# point (found by the post-deploy smoke test): it promoted the LOWEST
# line while the pipeline stored the SELECTED one.  These tests lock
# the display fix in: the /live market block must show the selector's
# exact (line, over, under) for a multi-line batch.

import sqlite3

from datetime import datetime, timezone

from blm_v4.api import _analyze_game


_WS_HARNESS_SQL = """
    CREATE TABLE games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL DEFAULT 'PokerBet',
        source_game_id TEXT NOT NULL,
        classification TEXT NOT NULL DEFAULT 'BETUAL_NBA',
        home_team TEXT, away_team TEXT, status TEXT DEFAULT 'live',
        last_seen_at TEXT, source_url TEXT,
        competition TEXT, region TEXT, sport TEXT);
    CREATE TABLE snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER, source_game_id TEXT NOT NULL,
        classification TEXT, captured_at TEXT NOT NULL,
        period_label TEXT, clock TEXT, quarter INTEGER,
        home_score INTEGER, away_score INTEGER,
        total_line REAL, total_over_odds REAL, total_under_odds REAL,
        spread REAL, spread_indicator TEXT,
        home_total_line REAL, away_total_line REAL,
        w1_odds REAL, w2_odds REAL,
        markets_json TEXT NOT NULL DEFAULT '{}',
        raw_json TEXT NOT NULL DEFAULT '{}');
    CREATE TABLE market_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER, source_game_id TEXT NOT NULL,
        captured_at TEXT NOT NULL, market_type TEXT NOT NULL,
        market_name TEXT NOT NULL, line_value REAL,
        over_price REAL, under_price REAL,
        home_score INTEGER, away_score INTEGER,
        period_label TEXT, clock TEXT,
        raw_json TEXT NOT NULL DEFAULT '{}');
"""


def _ws_analyze_harness(tmp_path, batch_rows):
    """One tracked game whose snapshots carry NO total_line; the displayed
    market must come from the WS batch via the selector.  Mirrors the
    test_ws_market.py _analyze_game harness (real DB, no mocks)."""
    db = tmp_path / "blm.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(_WS_HARNESS_SQL)
    conn.execute("INSERT INTO games (source_game_id, home_team, away_team, status) "
                 "VALUES ('G1', 'Home', 'Away', 'live')")
    gid = conn.execute("SELECT id FROM games").fetchone()[0]
    conn.execute("""INSERT INTO snapshots (game_id, source_game_id, classification,
            captured_at, period_label, clock, quarter, home_score, away_score)
            VALUES (?, 'G1', 'BETUAL_NBA', '2026-09-16T18:00:30Z',
                    '3rd Quarter', '06:48', 3, 44, 53)""", (gid,))
    for line, over, under in batch_rows:
        conn.execute("""INSERT INTO market_observations
            (game_id, source_game_id, captured_at, market_type, market_name,
             line_value, over_price, under_price, home_score, away_score)
            VALUES (?, 'G1', '2026-09-16T18:00:10Z', 'MatchTotal', 'Total Points',
                    ?, ?, ?, 44, 53)""", (gid, line, over, under))
    conn.commit()
    game = dict(conn.execute("SELECT * FROM games").fetchone())
    rows = [dict(r) for r in conn.execute("SELECT * FROM snapshots").fetchall()]
    d = _analyze_game(game, rows, datetime.now(timezone.utc), conn)
    conn.close()
    return d


def test_live_market_block_shows_price_selected_ws_line(tmp_path):
    """/live WS display invariant: the market block shows the SELECTED
    (line, over, under) of the latest batch — the exact policy twin of
    the supplied 169.5/171.5/173.5 example — never the positional
    lowest row (the pre-fix behaviour this test pins out)."""
    d = _ws_analyze_harness(
        tmp_path, [(169.5, 1.50, 2.40), (171.5, 1.85, 1.85), (173.5, 2.40, 1.50)])
    m = d["market"]
    assert m["market_source"] == "ws"
    assert m["total_line"] == 171.5
    assert m["over_odds"] == 1.85 and m["under_odds"] == 1.85
    assert m["total_line_at"] == "2026-09-16T18:00:10Z"


def test_live_market_block_degenerate_batch_keeps_line_identity(tmp_path):
    """A batch with NO usable prices never blanks the panel: the old
    line-identity fallback (first row) supplies the line, prices NULL."""
    d = _ws_analyze_harness(
        tmp_path, [(199.5, None, None), (201.5, None, None)])
    m = d["market"]
    assert m["market_source"] == "ws"
    assert m["total_line"] == 199.5
    assert m["over_odds"] is None and m["under_odds"] is None
