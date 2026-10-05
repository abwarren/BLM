"""ENGINE → EXECUTOR seam — PokerBetBrowserProvider (2026-10-05).

The BLM engine must NOT click.  It emits a bet REQUEST; an eligibility gate
decides WHETHER; the provider resolves the exact game, selects it with the
platform strategy (Cyber pointer / BETUAL DOM click) and drives the chain to
GATE_READY; only live mode submits.  These tests pin the seam with the existing
deterministic ``SelectionResolver`` double (``tests/fake_browser_adapter.py``)
— NO real browser, NO credentials, ZERO real money.

ONE NAMED TEST PER CONTRACT PROPERTY:
  * test_gate_only_never_submits
  * test_gate_only_returns_accepted_on_gate_ready
  * test_unresolved_market_is_failed_not_submitted
  * test_selection_not_added_is_failed
  * test_live_place_returns_the_honest_bridge_status
  * test_live_ambiguous_outcome_raises_provider_ambiguous
  * test_provider_is_a_bet_provider
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_browser_adapter import (  # noqa: E402
    ACCEPTED as FAKE_ACCEPTED,
    FakeBrowserAdapter,
)

from blm_v4.betting.provider import (  # noqa: E402
    BetProvider,
    PokerBetBrowserProvider,
    ProviderAmbiguous,
)
from blm_v4.execution.browser_bridge import ResolverBrowserBridge  # noqa: E402

EVENT = "Team A vs Team B"
GID = "30990001"


def _adapter(**kw) -> FakeBrowserAdapter:
    a = FakeBrowserAdapter()
    a.set_market(EVENT, line=190.5, over=1.90, under=2.00)
    return a


def _provider(adapter, *, live=False):
    return PokerBetBrowserProvider(
        adapter=adapter, bridge=ResolverBrowserBridge(adapter), live=live,
        event_resolver=lambda _gid: EVENT)


def _submit(p, **kw):
    args = dict(execution_id="exec-1", game_id=GID, alert_id="alert-1",
                selection="UNDER", price=2.00, stake_amount=2.00,
                market="TOTAL", line=190.5)
    args.update(kw)
    return p.submit(**args)


def test_provider_is_a_bet_provider():
    assert isinstance(_provider(_adapter()), BetProvider)


def test_gate_only_never_submits():
    a = _adapter()
    p = _provider(a)
    _submit(p)
    assert a.placements == []          # ZERO submissions in gate-only mode


def test_gate_only_returns_accepted_on_gate_ready():
    p = _provider(_adapter())
    out = _submit(p)
    assert out["status"] == "ACCEPTED"
    assert out["provider_ref"].startswith("gate-ready-")


def test_unresolved_market_is_failed_not_submitted():
    a = FakeBrowserAdapter()            # no market registered
    p = _provider(a)
    out = _submit(p)
    assert out["status"] == "FAILED"
    assert a.placements == []


def test_selection_not_added_is_failed():
    a = _adapter()
    a.click_fail_on = 1                 # the double's click-not-registered knob
    p = _provider(a)
    out = _submit(p)
    assert out["status"] == "FAILED"
    assert a.placements == []


def test_live_place_returns_the_honest_bridge_status(monkeypatch):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "true")   # the 2nd interlock
    a = _adapter()
    p = _provider(a, live=True)
    out = _submit(p)
    assert out["status"] in ("ACCEPTED", FAKE_ACCEPTED, "SUBMITTED")
    assert a.placements, "live mode must reach the placement"


def test_live_ambiguous_outcome_raises_provider_ambiguous(monkeypatch):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "true")   # the 2nd interlock
    a = _adapter()
    a.adapt_down = True                 # the placement boundary can't be proven
    p = _provider(a, live=True)
    with pytest.raises(ProviderAmbiguous):
        _submit(p)
