"""The SHORTFALL leg kill switch (operator directive 2026-11-15).

"shortfall should be off".  The leg is the second execution leg
(``required_pts_per_min - actual_pts_per_min >= SHORTFALL_MARGIN``); the switch
is ``TRADE_SHORTFALL_ENABLED`` and it DEFAULTS ON, so the frozen rule and every
suite that pins it keep their contract unless a deployment turns it off.

One named test per requirement.
"""
import inspect

import pytest

from blm_v4 import trade_window
from blm_v4.trade_window import (PACE_BELOW, SHORTFALL, exec_alert_reason,
                                 exec_shortfall_reason)


def test_the_switch_defaults_on():
    """An unconfigured deployment keeps the leg — the default is the old rule."""
    assert trade_window.TRADE_SHORTFALL_ENABLED is True
    assert exec_shortfall_reason(5.0, 6.0) == SHORTFALL
    assert exec_shortfall_reason(5.0, 5.5001) == SHORTFALL
    assert exec_shortfall_reason(5.0, 5.0) is None


def test_disabled_the_leg_always_clears(monkeypatch):
    """Off -> the leg passes even where the rule would refuse."""
    monkeypatch.setattr(trade_window, "TRADE_SHORTFALL_ENABLED", False)
    assert exec_shortfall_reason(5.0, 6.0) is None     # gap -1.0, refused when on
    assert exec_shortfall_reason(5.0, 9.9) is None     # far above required
    # even an unprovable payload clears: the leg is off, not merely relaxed
    assert exec_shortfall_reason(None, None) is None
    assert exec_shortfall_reason("n/a", 4.0) is None


def test_disabling_shortfall_does_not_touch_the_pace_leg(monkeypatch):
    """Only the shortfall leg moves; the required-vs-league-average bar stands."""
    monkeypatch.setattr(trade_window, "TRADE_SHORTFALL_ENABLED", False)
    # required 4.0 vs league average 4.0 = ratio 1.00, below the pace bar
    assert exec_alert_reason(4.0, 4.0, 80.0) == PACE_BELOW
    assert exec_shortfall_reason(4.0, 4.0) is None


def test_the_served_and_submission_paths_share_the_one_leg():
    """api (served armed flag) and revalidate (submission) both call THIS name,
    so the switch cannot be honoured by one path and ignored by the other."""
    import blm_v4.api as api
    import blm_v4.betting.revalidate as revalidate
    assert "exec_shortfall_reason(" in inspect.getsource(api)
    assert "exec_shortfall_reason(" in inspect.getsource(revalidate)


def test_the_frozen_prefix_is_still_a_prefix():
    """The switch lives in the tail; the frozen body must not have been edited."""
    frozen = trade_window._shortfall_leg_frozen
    # the frozen rule is preserved verbatim behind the wrapper
    assert frozen(5.0, 6.0) == SHORTFALL
    assert frozen(5.0, 4.0) is None
    assert frozen(None, 4.0) == SHORTFALL
