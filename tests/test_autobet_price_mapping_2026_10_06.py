"""REGRESSION — the execution record's ``price`` field (2026-10-06).

``rec["price"]`` must be the market's UNDER **odds**, never the total **line**.
Previously ``"price": line`` (``line = market.get("total_line")``) wrote the
market's total line into the price column, so every dry-run record showed a
line (229.5, 157.5, …) where the odds belong while ``triggered_line`` carried
the alert's trigger.  Found during the P4 live gate-only rehearsal review.

ONE NAMED TEST PER CONTRACT PROPERTY:
  * test_triggered_line_remains_the_alert_trigger_line
  * test_price_equals_market_under_odds
  * test_total_line_is_never_stored_as_price

Values are deliberately mutually distinct so each assertion discriminates.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blm_v4.betting.executor import evaluate  # noqa: E402

from test_betting_execution import (  # noqa: E402
    make_cfg,
    make_store,
    qualifying_game,
)

TOTAL_LINE = 193.5     # the market's live total line (the fixture's own value)
TRIGGER_LINE = 185.5   # deliberately DIFFERENT from the total line
UNDER_ODDS = 2.25      # deliberately neither the line nor 1.0


def _game_with_odds(game_id="30990777"):
    g = qualifying_game(game_id=game_id)
    g["market"]["total_line"] = TOTAL_LINE
    g["market"]["under_odds"] = UNDER_ODDS
    g["under_alert"]["trigger_line"] = TRIGGER_LINE
    return g


def _claim(tmp_path, **over):
    store = make_store(tmp_path)
    cfg = make_cfg(tmp_path, **over)
    res = evaluate(_game_with_odds(), cfg=cfg, store=store, enabled=True,
                   unit_price=2.0, stats=store.today_stats(), claim=True)
    assert res["decision"] in ("WOULD_BET", "EXECUTE"), res
    return res["candidate"], store


def test_triggered_line_remains_the_alert_trigger_line(tmp_path):
    cand, _ = _claim(tmp_path)
    assert cand["triggered_line"] == TRIGGER_LINE
    assert cand["triggered_line"] != TOTAL_LINE


def test_price_equals_market_under_odds(tmp_path):
    cand, _ = _claim(tmp_path)
    assert cand["price"] == UNDER_ODDS


def test_total_line_is_never_stored_as_price(tmp_path):
    cand, store = _claim(tmp_path)
    assert cand["price"] != TOTAL_LINE
    with store._conn() as c:
        row = c.execute(
            "SELECT price, triggered_line FROM bet_executions").fetchone()
    stored = dict(row)
    assert stored["price"] == UNDER_ODDS
    assert stored["triggered_line"] == TRIGGER_LINE
    assert stored["price"] != TOTAL_LINE
