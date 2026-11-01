"""The panel diagnostic: what the lobby ACTUALLY renders, journalled.

2026-10-31: the collector sat at ``tracked=1`` for half an hour logging
``BETUAL_NBA lobby unreachable`` with no visibility into the panel, so
"site stopped rendering a Betual section" and "our header needles went
stale" were indistinguishable.  `_log_panel_diag` dumps section/row counts
and the header titles once a minute.

One named test per requirement.  The diagnostic must be: read-only,
throttled, switchable off, and incapable of raising into the collector.
"""
from __future__ import annotations

import logging

import pytest

from blm_v4.collector import PokerBetCollector
from blm_v4.classifications import Classification


class FakePage:
    def __init__(self, diag=None, url="https://example/live", boom=False):
        self.url = url
        self._diag = diag if diag is not None else {
            "sections": 3, "rows": 12,
            "heads": ["Basketball", "Virtual Matches", "Betual NBA"],
            "titles": ["Betual NBA", "Cyber Basketball. 2K26 Matches"],
        }
        self._boom = boom
        self.calls = 0

    def evaluate(self, *_args, **_kwargs):
        self.calls += 1
        if self._boom:
            raise RuntimeError("page gone")
        return self._diag


def _collector():
    """A collector instance without running __init__ (no browser)."""
    return object.__new__(PokerBetCollector)


def test_the_diag_logs_the_headers_and_the_row_counts(caplog):
    c = _collector()
    page = FakePage()
    with caplog.at_level(logging.WARNING, logger="blm_v4.collector"):
        c._log_panel_diag(page, Classification.BETUAL_NBA, "comp_lobby_unreachable")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "panel_diag" in text
    assert "sections=3" in text and "rows=12" in text
    assert "Betual NBA" in text
    assert "comp_lobby_unreachable" in text
    assert page.calls == 1


def test_the_diag_is_throttled_to_one_line_a_minute(caplog):
    c = _collector()
    page = FakePage()
    with caplog.at_level(logging.WARNING, logger="blm_v4.collector"):
        c._log_panel_diag(page, Classification.BETUAL_NBA, "a")
        c._log_panel_diag(page, Classification.BETUAL_NBA, "b")
        c._log_panel_diag(page, Classification.BETUAL_NBA, "c")
    lines = [r for r in caplog.records if "panel_diag" in r.getMessage()]
    assert len(lines) == 1
    assert page.calls == 1          # the page is not even queried again


def test_the_diag_never_raises_when_the_page_is_broken():
    c = _collector()
    c._log_panel_diag(FakePage(boom=True), Classification.BETUAL_NBA, "x")
    # reaching here is the assertion: no exception escaped


def test_the_diag_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("BLM_PANEL_DIAG", "0")
    c = _collector()
    page = FakePage()
    c._log_panel_diag(page, Classification.BETUAL_NBA, "x")
    assert page.calls == 0


def test_a_diag_failure_does_not_change_the_callers_verdict():
    """`_ensure_comp_lobby` must still return False when the lobby is missing."""
    c = _collector()
    c.comp_ids = {}
    page = FakePage(diag={"sections": 0, "rows": 0, "heads": [], "titles": []})
    page.evaluate = lambda *a, **k: 0          # no matching rows, ever
    c._goto = lambda *a, **k: True
    c._wait_panel = lambda *a, **k: None
    c._expand_target_sections = lambda *a, **k: None
    assert c._ensure_comp_lobby(page, Classification.BETUAL_NBA) is False
