"""LINE-AGE leg (operator directive 2026-11-15).

The live gate must match the tc-new cohort, which admits a candidate only when
``captured_at - market_captured_at <= 15 s``.  This is a THIRD freshness rule,
separate from the lane's 300 s stale-retry policy (2026-10-28), and it is
deployment configurable: ``TRADE_MAX_LINE_AGE_S``, 0 disables it.

One named test per requirement.
"""
import inspect

import pytest

from blm_v4 import trade_window as TW
from blm_v4.trade_window import LINE_STALE, line_age_reason, line_age_seconds


CAP = "2026-11-15T10:00:15.000000Z"


def test_the_code_default_is_off():
    """The 2026-10-28 stale-retry contract is the module default; the
    deployment selects 15 s to match the cohort."""
    assert TW.TRADE_MAX_LINE_AGE_S == pytest.approx(0.0)
    assert line_age_reason(CAP, "2026-11-15T09:00:00.000000Z") is None


def test_age_is_capture_minus_market_timestamp():
    assert line_age_seconds(CAP, "2026-11-15T10:00:00.000000Z") == \
        pytest.approx(15.0)
    # a market stamp AFTER the capture yields a negative age (not stale)
    assert line_age_seconds(CAP, "2026-11-15T10:00:20.000000Z") == \
        pytest.approx(-5.0)


def test_the_boundary_is_inclusive(monkeypatch):
    """At the deployed 15 s: <= 15 passes; beyond it refuses."""
    monkeypatch.setattr(TW, "TRADE_MAX_LINE_AGE_S", 15.0)
    assert line_age_reason(CAP, "2026-11-15T10:00:00.000000Z") is None
    assert line_age_reason(CAP, "2026-11-15T09:59:59.999000Z") == LINE_STALE
    assert line_age_reason(CAP, "2026-11-15T10:00:15.100000Z") is None


def test_no_provenance_supplied_is_not_this_leg():
    """Older callers build legs from paces only; the leg stays silent."""
    assert line_age_reason(None, None) is None


def test_partial_or_unreadable_provenance_fails_closed(monkeypatch):
    monkeypatch.setattr(TW, "TRADE_MAX_LINE_AGE_S", 15.0)
    assert line_age_reason(CAP, None) == LINE_STALE
    assert line_age_reason(None, CAP) == LINE_STALE
    assert line_age_reason("n/a", "2026-11-15T10:00:00.000000Z") == LINE_STALE
    assert line_age_reason(CAP, "not-a-time") == LINE_STALE
    assert line_age_reason("", "") == LINE_STALE


def test_zero_disables_the_leg(monkeypatch):
    monkeypatch.setattr(TW, "TRADE_MAX_LINE_AGE_S", 0)
    assert line_age_reason(CAP, "2026-11-15T09:00:00.000000Z") is None
    assert line_age_reason(None, None) is None


def test_the_limit_is_configurable(monkeypatch):
    monkeypatch.setattr(TW, "TRADE_MAX_LINE_AGE_S", 120.0)
    assert line_age_reason(CAP, "2026-11-15T09:59:00.000000Z") is None
    assert line_age_reason(CAP, "2026-11-15T09:57:00.000000Z") == LINE_STALE


def test_the_fast_lane_composes_the_leg_and_passes_provenance():
    from blm_v4.betting import fast_lane
    src = inspect.getsource(fast_lane.legs)
    assert '"line_age": line_age_reason(' in src
    assert "captured_at=None, market_captured_at=None" in src
    lane = inspect.getsource(fast_lane)
    assert 'captured_at=row["captured_at"]' in lane
    assert 'market_captured_at=row["market_captured_at"]' in lane


def test_the_fast_lane_leg_uses_the_real_timestamps(monkeypatch):
    from blm_v4.betting.fast_lane import legs
    monkeypatch.setattr(TW, "TRADE_MAX_LINE_AGE_S", 15.0)
    base = dict(required_pace=4.5, avg_pace=4.0, actual_pace=4.0,
                progress_pct=80.0, total_points=80, under_odds=1.80,
                competition_slug="betual-nba")
    fresh = legs(**base, captured_at=CAP,
                 market_captured_at="2026-11-15T10:00:05.000000Z")
    stale = legs(**base, captured_at=CAP,
                 market_captured_at="2026-11-15T09:58:00.000000Z")
    assert fresh["line_age"] is None
    assert stale["line_age"] == LINE_STALE


def test_the_served_flag_and_the_submission_path_compose_it():
    """Dashboard boolean, fast lane and revalidation must agree about the leg."""
    import blm_v4.api as api
    import blm_v4.betting.revalidate as revalidate
    assert "line_age_reason(" in inspect.getsource(api)
    rsrc = inspect.getsource(revalidate)
    assert "line_age_reason(" in rsrc
    # revalidation must actually load the market timestamp it judges
    assert "market_captured_at" in inspect.getsource(revalidate.fetch_state)


def test_the_slate_publishes_the_limit_and_each_lines_age():
    import blm_v4.betting.slate as slate
    src = inspect.getsource(slate.build_slate)
    assert '"max_line_age_s": TW.TRADE_MAX_LINE_AGE_S' in src
    assert "line_age_seconds(" in src
