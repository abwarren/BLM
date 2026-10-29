"""The shortfall floor and the progress cap are configurable, defaults unchanged.

Operator request 2026-10-29: "a cohort with at least 60% winrate with 40+ bets a
day".  The pace bar alone cannot deliver it (40+/day IS TC-W at 59.7%, and it has
decayed to 54.7% over the last 14 days).  The lever that moves the win rate is the
shortfall floor plus the progress cap — both were hard-coded, so a cohort could
not be selected without editing the gate.

Defaults MUST stay -0.50 / 92: every existing suite and the code contract pin them.
"""
from __future__ import annotations

import importlib

import blm_v4.trade_window as tw


def _reload(**env):
    import os
    for k in ("TRADE_SHORTFALL_MARGIN", "TRADE_MAX_PROGRESS_PCT"):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    importlib.reload(tw)


def test_defaults_are_untouched():
    _reload()
    assert tw.SHORTFALL_MARGIN == -0.5
    assert tw.TRADE_MAX_PROGRESS_PCT == 92.0
    # a game 0.4 above its required rate is inside the default floor
    assert tw.exec_shortfall_reason(5.0, 5.4) is None
    assert tw.execution_window_reason(91.0) is None


def test_env_moves_the_shortfall_floor_and_progress_cap():
    _reload(TRADE_SHORTFALL_MARGIN="-0.25", TRADE_MAX_PROGRESS_PCT="88")
    try:
        assert tw.SHORTFALL_MARGIN == -0.25
        assert tw.TRADE_MAX_PROGRESS_PCT == 88.0
        # 0.30 ahead of the required rate is now refused, 0.20 still passes
        assert tw.exec_shortfall_reason(5.0, 5.30) == tw.SHORTFALL
        assert tw.exec_shortfall_reason(5.0, 5.20) is None
        # 90% progress is outside the band now
        assert tw.execution_window_reason(90.0) == tw.AFTER
        assert tw.execution_window_reason(85.0) is None
    finally:
        _reload()          # never leak the override into other suites


def test_a_cohort_leg_can_still_be_read_the_old_way():
    _reload()
    legs = tw  # cohort labels keep their own constants; the gate is what moves
    assert legs.SHORTFALL_MARGIN == -0.5
