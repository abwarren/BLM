"""Phase 5 — collector timeout budget tests.

Verifies the centralized timeout constants, the _page_content_timed
override, and the tick budget accounting.
"""
from __future__ import annotations

import pytest
from blm_v4.collector import PokerBetCollector

# Phase 5 constants live on the class, not the module
PAGE_CONTENT_TICK_TIMEOUT_S = PokerBetCollector.PAGE_CONTENT_TICK_TIMEOUT_S
PAGE_CONTENT_RETRY_TIMEOUT_S = PokerBetCollector.PAGE_CONTENT_RETRY_TIMEOUT_S
RECOVERY_TIMEOUT_S = PokerBetCollector.RECOVERY_TIMEOUT_S
PAGE_CAPTURE_BUDGET_S = PokerBetCollector.PAGE_CAPTURE_BUDGET_S
TICK_TARGET_S = PokerBetCollector.TICK_TARGET_S
TICK_HARD_CEILING_S = PokerBetCollector.TICK_HARD_CEILING_S


# ── 1. Centralized constants ────────────────────────────────────────

def test_phase5_constants_have_expected_values():
    """The Phase 5 timeout budget constants are the single source of truth
    and carry the documented values."""
    assert PAGE_CONTENT_TICK_TIMEOUT_S == 3.0
    assert PAGE_CONTENT_RETRY_TIMEOUT_S == 5.0
    assert RECOVERY_TIMEOUT_S == 5.0
    assert PAGE_CAPTURE_BUDGET_S == 12.0
    assert TICK_TARGET_S == 10.0
    assert TICK_HARD_CEILING_S == 30.0


def test_collector_class_exposes_timeout_constants():
    """PokerBetCollector carries the timeout constants as class attributes
    so the implementation can never drift from the policy."""
    assert PokerBetCollector.PAGE_CONTENT_TICK_TIMEOUT_S == 3.0
    assert PokerBetCollector.PAGE_CONTENT_RETRY_TIMEOUT_S == 5.0
    assert PokerBetCollector.RECOVERY_TIMEOUT_S == 5.0
    assert PokerBetCollector.PAGE_CAPTURE_BUDGET_S == 12.0
    assert PokerBetCollector.TICK_TARGET_S == 10.0
    assert PokerBetCollector.TICK_HARD_CEILING_S == 30.0


def test_collector_instance_inherits_timeout_constants():
    """Every collector instance uses the class constants; there is no
    per-instance override that could hide a misconfiguration."""
    c = PokerBetCollector.__new__(PokerBetCollector)
    assert c.PAGE_CONTENT_TICK_TIMEOUT_S == 3.0
    assert c.PAGE_CONTENT_RETRY_TIMEOUT_S == 5.0
    assert c.PAGE_CAPTURE_BUDGET_S == 12.0
    assert c.TICK_TARGET_S == 10.0
    assert c.TICK_HARD_CEILING_S == 30.0


# ── 2. _page_content_timed override ────────────────────────────────

def test_page_content_timed_accepts_timeout_override():
    """The _page_content_timed method accepts an explicit timeout_s so the
    tick budget can shrink the per-call deadline as the budget burns down."""
    import inspect
    sig = inspect.signature(PokerBetCollector._page_content_timed)
    params = list(sig.parameters)
    assert "timeout_s" in params
    assert params[-1] == "timeout_s"  # last param, has a default


def test_page_content_timed_default_matches_constant():
    """The default timeout for _page_content_timed is None (meaning "use the
    class-level constant"), and the class constant is 3.0 s — not the legacy
    8 s value."""
    import inspect
    sig = inspect.signature(PokerBetCollector._page_content_timed)
    default = sig.parameters["timeout_s"].default
    assert default is None, f"expected None (use class default), got {default}"
    assert PokerBetCollector.PAGE_CONTENT_TICK_TIMEOUT_S == 3.0
