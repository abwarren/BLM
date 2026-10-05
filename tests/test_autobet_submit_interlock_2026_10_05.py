"""THE SECOND SERVER-SIDE SUBMISSION INTERLOCK (2026-10-05).

A real browser submission requires BOTH:
  * the frontend's AUTO-BET switch (the store's kill switch), AND
  * the server-side ``BETTING_BROWSER_SUBMIT`` opt-in.

The flag is read AT THE SUBMISSION BOUNDARY (call time), so it can never be
baked in when a provider is constructed — an accidental live-provider
invocation (e.g. from a test) cannot reach a real click without it.  These
tests prove the interlock using injected fakes: NO real browser, ZERO wagers.

ONE NAMED TEST PER CONTRACT PROPERTY:
  * test_browser_submit_defaults_off_and_never_authorizes_by_accident
  * test_live_submit_refused_without_the_interlock
  * test_live_submit_permitted_with_the_interlock
  * test_the_interlock_is_read_at_call_time_not_construction
  * test_gate_only_mode_needs_no_interlock
  * test_status_exposes_the_live_interlock_state
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_browser_adapter import FakeBrowserAdapter  # noqa: E402

from blm_v4.betting.provider import (  # noqa: E402
    PokerBetBrowserProvider,
    browser_submit_authorized,
)
from blm_v4.execution.browser_bridge import ResolverBrowserBridge  # noqa: E402

EVENT = "Team A vs Team B"


def _adapter() -> FakeBrowserAdapter:
    a = FakeBrowserAdapter()
    a.set_market(EVENT, 190.5, 1.90, 2.00)
    return a


def _live_provider(adapter) -> PokerBetBrowserProvider:
    return PokerBetBrowserProvider(
        adapter=adapter, bridge=ResolverBrowserBridge(adapter), live=True,
        event_resolver=lambda _gid: EVENT)


def _submit(p):
    return p.submit(execution_id="exec-il", game_id="30990001",
                    alert_id="alert-il", selection="UNDER", price=2.00,
                    stake_amount=2.00, market="TOTAL", line=190.5)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("BETTING_BROWSER_SUBMIT", raising=False)


def test_browser_submit_defaults_off_and_never_authorizes_by_accident():
    assert browser_submit_authorized() is False
    for junk in ("", "false", "0", "no", "off", "FALSE", "False"):
        import os
        os.environ["BETTING_BROWSER_SUBMIT"] = junk
        assert browser_submit_authorized() is False
    import os
    os.environ.pop("BETTING_BROWSER_SUBMIT", None)


def test_live_submit_refused_without_the_interlock():
    """No BETTING_BROWSER_SUBMIT ⇒ the live provider refuses at the boundary
    and the browser is NEVER asked to place."""
    a = _adapter()
    out = _submit(_live_provider(a))
    assert out["status"] == "FAILED"
    assert out["error_code"] == "SUBMIT_NOT_AUTHORIZED"
    assert a.placements == []          # nothing reached the bookmaker


def test_live_submit_permitted_with_the_interlock(monkeypatch):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "TRUE")
    a = _adapter()
    out = _submit(_live_provider(a))
    assert out["status"] in ("ACCEPTED", "SUBMITTED")
    assert a.placements, "authorized live mode must reach the placement"


def test_the_interlock_is_read_at_call_time_not_construction(monkeypatch):
    """The authority is read at the SUBMISSION BOUNDARY: a provider built
    while OFF still submits once the flag is set, and a provider built while
    ON refuses once the flag is cleared."""
    a = _adapter()
    p = _live_provider(a)              # built while the interlock is OFF
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "true")
    assert p.submit(execution_id="e1", game_id="30990001", alert_id="a",
                    selection="UNDER", price=2.00, stake_amount=2.00,
                    market="TOTAL", line=190.5)["status"] in (
                        "ACCEPTED", "SUBMITTED")
    monkeypatch.delenv("BETTING_BROWSER_SUBMIT", raising=False)
    before = len(a.placements)
    out = p.submit(execution_id="e2", game_id="30990001", alert_id="a",
                   selection="UNDER", price=2.00, stake_amount=2.00,
                   market="TOTAL", line=190.5)
    assert out["error_code"] == "SUBMIT_NOT_AUTHORIZED"
    assert len(a.placements) == before  # the second call placed nothing


def test_gate_only_mode_needs_no_interlock():
    """The default (gate-only) provider proves GATE_READY without any
    submission authority — and without the interlock."""
    a = _adapter()
    p = PokerBetBrowserProvider(adapter=a, bridge=ResolverBrowserBridge(a),
                                live=False,
                                event_resolver=lambda _gid: EVENT)
    out = _submit(p)
    assert out["status"] == "ACCEPTED"
    assert a.placements == []


def test_status_exposes_the_live_interlock_state():
    """The UI must show the REAL authority, not a cosmetic toggle."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from blm_v4.betting import api as B
    from blm_v4.betting.store import BettingStore
    from blm_v4.betting.config import BettingConfig
    import tempfile, os

    d = tempfile.mkdtemp()
    store = BettingStore(str(Path(d) / "ui.db"))
    store.set_unit_price(2.0)
    cfg = BettingConfig(db_path=str(Path(d) / "ui.db"))
    app = FastAPI()
    app.include_router(B.router)          # the router carries its own prefix
    B.configure_betting(store, cfg)
    client = TestClient(app)
    st = client.get("/api/v4/betting/status").json()
    assert st["browser_submit_authorized"] is False
    os.environ["BETTING_BROWSER_SUBMIT"] = "true"
    try:
        assert client.get("/api/v4/betting/status") \
            .json()["browser_submit_authorized"] is True
    finally:
        os.environ.pop("BETTING_BROWSER_SUBMIT", None)


def test_dashboard_shows_the_real_submission_authority():
    """Item 3 — the UI must render the REAL authority, not a cosmetic toggle:
    the AUTO BETTING panel binds `browser_submit_authorized` from /status, and
    the DOM carries the node that displays it alongside the switch + unit size."""
    root = Path(__file__).resolve().parent.parent / "blm_v4" / "dashboard" \
        / "static"
    js = (root / "dashboard.js").read_text(encoding="utf-8")
    html = (root / "index.html").read_text(encoding="utf-8")
    assert "browser_submit_authorized" in js        # bound from /status
    assert "abSubmitPill" in js and "REAL SUBMIT" in js
    assert 'id="abSubmitPill"' in html              # the node exists
    for token in ("abSwitch", "abUnitPrice", "abGlobalPill"):
        assert token in html, token                 # switch + unit size + status
