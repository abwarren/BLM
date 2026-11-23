"""STRATEGY PROFILE — pace/volume gate (2026-11-11).

The production UNDER trigger (progress >= 75, required > league * 1.04) is
frozen by directive and stays the default.  ``pace_vol_2026_11_11`` narrows
the SAME rule to the parameters validated on the 7-day gate sweep:

    required > league * 1.12
    progress 75 .. 95
    UNDER price >= 1.50
    market line age <= 30s

These tests assert that (a) with no profile selected nothing changes and
(b) the profile is applied exactly, failing closed when a priced/freshness
input is missing.
"""
from __future__ import annotations

import pytest

from blm_v4.live_analytics.under_alert import (
    PROFILE_ENV,
    PROFILE_PACE_VOL,
    PROFILE_PRODUCTION,
    active_profile,
    under_alert_state,
)

LEAGUE = 2.0  # pts/min — required 2.13 is 6.5% above; 2.30 is 15% above


def _state(**kw) -> dict:
    # a candidate that clears the pace_vol profile on every leg; each test
    # overrides exactly the input it is about.
    args = {
        "actual_pace": 2.0,
        "required_pace": LEAGUE * 1.13,
        "league_average_pace": LEAGUE,
        "progress_pct": 80.0,
        "under_price": 1.62,
        "market_age_s": 12.0,
    }
    args.update(kw)
    return under_alert_state(**args)


def test_no_profile_env_keeps_production_rule(monkeypatch):
    monkeypatch.delenv(PROFILE_ENV, raising=False)
    st = _state(progress_pct=96.5, required_pace=LEAGUE * 1.05)
    assert st["active"] is True          # no upper progress bound
    assert st["profile"] == PROFILE_PRODUCTION


def test_profile_env_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv(PROFILE_ENV, PROFILE_PACE_VOL)
    assert active_profile().name == PROFILE_PACE_VOL
    assert _state()["active"] is True


def test_profile_rejects_pace_below_112(monkeypatch):
    monkeypatch.setenv(PROFILE_ENV, PROFILE_PACE_VOL)
    assert _state(required_pace=LEAGUE * 1.08)["active"] is False
    assert _state(required_pace=LEAGUE * 1.12)["active"] is False   # strict >
    assert _state(required_pace=LEAGUE * 1.121)["active"] is True


def test_profile_bounds_progress_band(monkeypatch):
    monkeypatch.setenv(PROFILE_ENV, PROFILE_PACE_VOL)
    assert _state(progress_pct=74.9)["active"] is False
    assert _state(progress_pct=75.0)["active"] is True
    assert _state(progress_pct=95.0)["active"] is True
    assert _state(progress_pct=95.1)["active"] is False


def test_profile_requires_price_and_fresh_line(monkeypatch):
    monkeypatch.setenv(PROFILE_ENV, PROFILE_PACE_VOL)
    assert _state(under_price=None, market_age_s=10)["active"] is False
    assert _state(under_price=1.49, market_age_s=10)["active"] is False
    assert _state(under_price=1.50, market_age_s=10)["active"] is True
    assert _state(under_price=1.70, market_age_s=None)["active"] is False
    assert _state(under_price=1.70, market_age_s=31)["active"] is False
    assert _state(under_price=1.70, market_age_s=30)["active"] is True


def test_profile_yields_margin_and_carries_its_name(monkeypatch):
    monkeypatch.setenv(PROFILE_ENV, PROFILE_PACE_VOL)
    prof = active_profile()
    assert prof.required_margin == 1.12
    assert prof.min_under_price == 1.50
    assert prof.max_market_age_s == 30.0
    st = _state(under_price=1.62, market_age_s=12)
    assert st["active"] is True and st["profile"] == PROFILE_PACE_VOL
    assert st["under_price"] == 1.62 and st["market_age_s"] == 12


def test_unknown_profile_name_degrades_to_production(monkeypatch):
    monkeypatch.setenv(PROFILE_ENV, "not-a-profile")
    assert active_profile().name == PROFILE_PRODUCTION


def test_eligibility_still_suppresses(monkeypatch):
    monkeypatch.setenv(PROFILE_ENV, PROFILE_PACE_VOL)
    st = _state(under_price=1.62, market_age_s=12, eligible=False)
    assert st["active"] is False


@pytest.mark.parametrize("bad", [None, "", 0, False])
def test_non_finite_inputs_fail_closed(monkeypatch, bad):
    monkeypatch.setenv(PROFILE_ENV, PROFILE_PACE_VOL)
    assert _state(required_pace=bad, under_price=1.62,
                  market_age_s=12)["active"] is False
