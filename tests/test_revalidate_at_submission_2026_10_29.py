"""The gate is re-checked at the SUBMISSION boundary, not just at claim time.

MEASURED 2026-10-29 (31398377, KBL): claim 22:52:14.548 → submit 22:53:25.428,
71 seconds.  At the claim the game was 75.0% / 135 pts with pace 1.116 and
shortfall -0.15 — a textbook pass.  It reached the book at 77.5% with shortfall
-0.38, a state the cohort refuses, and the bet stood anyway.

executor.execute() now re-runs the same legs on the newest capture immediately
before the provider call, for the same reason it re-reads the kill switch there.
FAILS CLOSED: no capture, an unreadable one, or one older than the limit is a
refusal — with the feed ticking every ~2s, a two-minute-old capture for a live
game means the game is gone, and a state we cannot read must not be bet on.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import importlib
import os

import pytest

import blm_v4.trade_window as tw
from blm_v4.betting import revalidate as R


@pytest.fixture(autouse=True)
def _deployed_gate():
    """The guard inherits the DEPLOYMENT's gate, so the test must too.

    SHORTFALL_MARGIN defaults to -0.50 in code and is -0.25 in the drop-in; with
    the default the measured case (-0.38) would PASS, which is exactly the
    mistake the production env prevents.
    """
    os.environ["BET_REVALIDATE"] = "1"
    os.environ["TRADE_SHORTFALL_MARGIN"] = "-0.25"
    os.environ["TRADE_MIN_PACE_RATIO"] = "1.04"
    importlib.reload(tw)
    yield
    os.environ.pop("BET_REVALIDATE", None)
    os.environ.pop("TRADE_SHORTFALL_MARGIN", None)
    os.environ.pop("TRADE_MIN_PACE_RATIO", None)
    importlib.reload(tw)

NOW = datetime(2026, 10, 29, 22, 53, 25, tzinfo=timezone.utc)
GOOD = dict(captured_at="2026-10-29T22:53:15.000000Z", progress_pct=77.5,
            total_points=141, required_pts_per_min=4.35, actual_pts_per_min=4.50,
            under_price=1.85, live_total_line=178.5, status="VALID",
            competition_slug="betual-kbl", avg_pace=3.8972)
CAND = {"game_id": "31398377", "price": 1.85}


def _st(**over):
    d = dict(GOOD); d.update(over); return d


def test_a_state_that_still_passes_is_allowed():
    assert R.verdict(GOOD, CAND, now=NOW) is None


def test_the_measured_case_is_refused_on_the_shortfall():
    """The state the bet actually landed on: shortfall -0.38 < -0.25."""
    st = _st(required_pts_per_min=4.1667, actual_pts_per_min=4.5484)
    code, why = R.verdict(st, CAND, now=NOW)
    assert code.startswith("REVALIDATION_")
    assert "shortfall" in why.lower() or "below" in why.lower()


def test_leaving_the_band_is_refused():
    code, _ = R.verdict(_st(progress_pct=94.0), CAND, now=NOW)
    assert code is not None


def test_too_few_points_is_refused():
    assert R.verdict(_st(total_points=60), CAND, now=NOW) is not None


def test_an_excluded_competition_is_refused_at_submission():
    assert R.verdict(_st(competition_slug="cyber-basketball-2k26-matches"),
                     CAND, now=NOW) is not None


def test_a_stale_capture_is_a_refusal_not_a_pass():
    old = (NOW + timedelta(seconds=200)).isoformat().replace("+00:00", "Z")
    assert R.verdict(_st(captured_at=old), CAND, now=NOW) is None      # fresh
    stale = (NOW - timedelta(seconds=200)).isoformat().replace("+00:00", "Z")
    code, why = R.verdict(_st(captured_at=stale), CAND, now=NOW)
    assert code == "REVALIDATION_STALE" and "200s" in why


def test_no_capture_or_unreadable_is_a_refusal():
    assert R.verdict(None, CAND, now=NOW)[0] == "REVALIDATION_NO_CAPTURE"
    assert R.verdict(_st(captured_at="not-a-time"), CAND, now=NOW)[0] \
        == "REVALIDATION_UNREADABLE_CAPTURE"
    assert R.verdict(_st(status="MISSING_CLOCK"), CAND, now=NOW)[0] \
        == "REVALIDATION_NOT_VALID"


def test_price_drift_beyond_the_cap_is_refused():
    ok = R.verdict(_st(under_price=1.95), CAND, now=NOW)               # -0.05
    assert ok is None
    code, why = R.verdict(_st(under_price=1.75), CAND, now=NOW)        # -0.25
    assert code == "REVALIDATION_PRICE_DRIFT" and "1.75" in why


def test_the_switch_disables_the_whole_check(monkeypatch):
    monkeypatch.setenv("BET_REVALIDATE", "0")
    called = []
    out = R.revalidate(CAND, fetch=lambda gid: called.append(gid) or None)
    assert out is None and called == [], "the guard must not even read the feed"
    monkeypatch.setenv("BET_REVALIDATE", "1")
    assert R.revalidate(CAND, fetch=lambda gid: None)[0] == "REVALIDATION_NO_CAPTURE"
