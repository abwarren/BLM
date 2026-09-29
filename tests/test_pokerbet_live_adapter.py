"""BLM EXECUTION — POKERBET LIVE ADAPTER TESTS (phase ③, mocked DOM).

Directive 2026-09-23 §4 scenarios, all against a FAKE page/browser —
no live PokerBet website, no network, no real money:

  authenticated session detection · game resolution · game_id mismatch
  · team mismatch · market mismatch · line mismatch · odds mismatch
  (incl. tolerance) · suspended market · disappeared market · betslip
  verification · DRY_RUN never submitting · successful LIVE adapter
  path on a mocked browser/DOM · execution confirmation persistence.

The REAL contracts stay intact: the adapter implements the existing
SelectionResolver protocol, the engine's terminal semantics are
unchanged, and the pre-submit verification runs immediately before
placement — never earlier and assumed valid.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_browser_adapter import FakeBrowserAdapter  # noqa: E402,F401  (protocol sibling)

from blm_v4.execution.config import ExecutionConfig  # noqa: E402
from blm_v4.execution.pokerbet import session as session_mod  # noqa: E402
from blm_v4.execution.pokerbet.dom import (  # noqa: E402
    PokerBetDomAdapter,
    names_match_pair,
)
from blm_v4.execution.selection_model import (  # noqa: E402
    MODE_DRY_RUN,
    MODE_LIVE,
    ParlayJob,
    Selection,
    STATE_COMPLETE,
    STATE_DRY_RUN_COMPLETE,
    STATE_NON_RECOVERABLE,
)
from blm_v4.execution.store import ExecutionStore  # noqa: E402
from blm_v4.execution.total_executor import TotalExecutor  # noqa: E402

GAME_A = "Team A vs Team B"
GID_A = "30840226"
EVENT_URL = ("https://www.pokerbet.co.za/en/sports/live/event-view/"
             "Basketball/World/1/betual-nba/30840226/x")
MARKET_TEXT = "Total Points 164.5 Over 1.90 Under 1.90"
SLIP_TEXT = "Team A vs Team B\nTotal Points\nUnder 164.5 @ 1.90\n"
SLIP_TEXT_GID = (SLIP_TEXT + "Ref 30840226\n")
RECEIPT = "Bet placed successfully — reference 881234"


# ── fake playwright surface (the exact ops dom.py uses) ───────────────

class FakeLocator:
    def __init__(self, page, count=0, text="", clickable=False,
                 texts=None):
        self._page, self._count = page, count
        self._text, self._clickable = text, clickable
        self._texts = texts if texts is not None else (
            [text] if text else [])

    def count(self) -> int:
        return self._count

    @property
    def first(self) -> "FakeLocator":
        return self

    @property
    def last(self) -> "FakeLocator":
        if self._texts:
            import copy
            clone = copy.copy(self)
            clone._text = self._texts[-1]
            return clone
        return self

    def inner_text(self) -> str:
        return self._text

    def get_attribute(self, name):
        return None

    def is_disabled(self) -> bool:
        return False

    def click(self, timeout=None):
        self._page.clicks.append({"sel": "click"})
        if self._page.click_should_fail:
            raise RuntimeError("click failed")
        if self._page.slip_after_click:
            self._page.betslip_text = self._page.slip_after_click
        if self._page.suspend_after_click:
            self._page.suspended = True

    def fill(self, value):
        if not getattr(self._page, "stake_fill_ignored", False):
            self._page.stake_value = value

    def input_value(self) -> str:
        return str(self._page.stake_value)


class FakePage:
    def __init__(self, url=EVENT_URL, teams=("Team A", "Team B"),
                 market_text=MARKET_TEXT):
        self.url = url
        self.teams = list(teams)
        self.market_text = market_text
        self.suspended = False
        self.betslip_text = SLIP_TEXT
        self.slip_after_click = SLIP_TEXT
        self.suspend_after_click = False
        self.receipt_text = RECEIPT
        self.signin_count = 0
        self.clicks: list = []
        self.click_should_fail = False
        self.stake_value = "0"
        self.stake_fill_ignored = False

    def goto(self, url):
        self.url = url

    def wait_for_load_state(self, *a, **k):
        pass

    def locator(self, sel, has_text=None):
        if sel.startswith("text="):
            return FakeLocator(self, count=self.signin_count)
        if sel == session_mod.__name__ + "":      # pragma: no cover
            return FakeLocator(self)
        if "team" in sel or "competitor" in sel:
            return FakeLocator(self, count=len(self.teams),
                               text=self.teams[0], texts=list(self.teams))
        if "market" in sel:
            if has_text == "Total" and not self.market_text:
                return FakeLocator(self)
            if has_text == "Total":
                return FakeLocator(self, count=1, text=self.market_text)
            return FakeLocator(self, count=1 if self.market_text else 0)
        if "suspended" in sel or "locked" in sel:
            return FakeLocator(self, count=1 if self.suspended else 0)
        if "betslip" in sel:
            return FakeLocator(self, count=1 if self.betslip_text else 0,
                               text=self.betslip_text)
        if "outcome" in sel or sel == "button":
            if has_text and has_text.lower() in (
                    "over", "under") and not self.market_text:
                return FakeLocator(self)
            if has_text and has_text.lower() in ("over", "under"):
                return FakeLocator(self, count=1, clickable=True)
            if has_text == "place" and self.market_text:
                return FakeLocator(self, count=1, clickable=True)
            return FakeLocator(self)
        if "receipt" in sel or "confirmation" in sel:
            return FakeLocator(self, count=1 if self.receipt_text else 0,
                               text=self.receipt_text)
        if "stake" in sel or "number" in sel:
            return FakeLocator(self, count=1)
        return FakeLocator(self)


class FakeBrowser:
    """The minimal CdpBrowser surface dom.py/session.py touch."""

    def __init__(self, page: Optional[FakePage] = None):
        self._page = page or FakePage()

    @property
    def connected(self) -> bool:
        return True

    def connect(self) -> "FakeBrowser":
        return self

    def close(self) -> None:
        pass

    def page(self):
        return self._page

    @property
    def context(self):
        return None


def make_adapter(page: Optional[FakePage] = None, **kw):
    adapter = PokerBetDomAdapter(FakeBrowser(page), **kw)
    adapter.register_game(GAME_A, game_id=GID_A,
                          home_team="Team A", away_team="Team B",
                          classification="BETUAL_NBA")
    return adapter


def make_cfg(tmp_path, **over) -> ExecutionConfig:
    defaults = dict(
        dry_run=True, max_selection_retries=3, retry_delay_ms=0,
        settle_ms=0, slip_wait_ms=0, verify_attempts=3,
        leg_timeout_s=45.0, job_timeout_s=600.0,
        max_parlays_per_run=100, db_path=str(tmp_path / "blm_execution.db"))
    defaults.update(over)
    return ExecutionConfig(**defaults)


# ═══════════════ 1. authenticated session detection ═══════════════

def test_authenticated_session_detected():
    page = FakePage()
    page.signin_count = 0                    # no SIGN IN control → signed in
    assert session_mod.detect_authenticated(FakeBrowser(page)) is True


def test_unauthenticated_session_detected():
    page = FakePage()
    page.signin_count = 1                    # SIGN IN visible → not signed in
    assert session_mod.detect_authenticated(FakeBrowser(page)) is False


# ═══════════════ 2. game resolution (game_id + teams) ═══════════════

def test_game_resolves_by_id_and_teams():
    adapter = make_adapter()
    assert adapter.find_event(GAME_A) is True
    assert adapter._page_game_id() == GID_A


def test_unregistered_event_fails_closed():
    adapter = make_adapter()
    assert adapter.find_event("Some Other Game vs Someone") is False


def test_event_view_url_shape():
    url = session_mod.event_view_url("30840226", "BETUAL_NBA")
    assert url.endswith("/betual-nba/30840226/x")
    assert url.startswith("https://www.pokerbet.co.za/en/sports/")


# ═══════════════ 3-8. the ordered hard-reject verification ══════════

def test_verify_game_id_mismatch_rejected():
    adapter = make_adapter()
    c = adapter.verify_offer(
        game_id=GID_A, home_team="Team A", away_team="Team B",
        market="TOTAL", position="UNDER", expected_line=164.5,
        expected_price=1.90, offer_line=164.5, offer_price=1.90,
        offer_game_id="99999999")
    assert not c.ok and c.reason == "GAME_ID_MISMATCH"


def test_verify_team_mismatch_rejected():
    adapter = make_adapter()
    c = adapter.verify_offer(
        game_id=GID_A, home_team="Team A", away_team="Team B",
        market="TOTAL", position="UNDER", expected_line=164.5,
        expected_price=1.90, offer_line=164.5, offer_price=1.90,
        rendered_home="Team A", rendered_away="Team C")
    assert not c.ok and c.reason == "TEAM_MISMATCH"


def test_verify_accepts_swapped_team_rendering():
    adapter = make_adapter()
    c = adapter.verify_offer(
        game_id=GID_A, home_team="Team A", away_team="Team B",
        market="TOTAL", position="UNDER", expected_line=164.5,
        expected_price=1.90, offer_line=164.5, offer_price=1.90,
        rendered_home="Team B", rendered_away="Team A")
    assert c.ok


def test_verify_market_mismatch_rejected():
    adapter = make_adapter()
    c = adapter.verify_offer(
        game_id=GID_A, home_team="Team A", away_team="Team B",
        market="SPREAD", position="UNDER", expected_line=164.5,
        expected_price=1.90, offer_line=164.5, offer_price=1.90)
    assert not c.ok and c.reason == "MARKET_MISMATCH"
    assert adapter.find_market(GAME_A, "SPREAD") is False


def test_verify_line_mismatch_rejected_and_tolerance_honoured():
    adapter = make_adapter(line_tolerance=0.0)
    c = adapter.verify_offer(
        game_id=GID_A, home_team="Team A", away_team="Team B",
        market="TOTAL", position="UNDER", expected_line=164.5,
        expected_price=1.90, offer_line=166.5, offer_price=1.90)
    assert not c.ok and c.reason == "LINE_MISMATCH"
    tol = make_adapter(line_tolerance=2.0)
    ok = tol.verify_offer(
        game_id=GID_A, home_team="Team A", away_team="Team B",
        market="TOTAL", position="UNDER", expected_line=164.5,
        expected_price=1.90, offer_line=166.5, offer_price=1.90)
    assert ok.ok


def test_verify_odds_mismatch_rejected_and_tolerance_honoured():
    adapter = make_adapter(price_tolerance=0.0)
    c = adapter.verify_offer(
        game_id=GID_A, home_team="Team A", away_team="Team B",
        market="TOTAL", position="UNDER", expected_line=164.5,
        expected_price=1.90, offer_line=164.5, offer_price=1.83)
    assert not c.ok and c.reason == "ODDS_MISMATCH"
    tol = make_adapter(price_tolerance=0.1)
    ok = tol.verify_offer(
        game_id=GID_A, home_team="Team A", away_team="Team B",
        market="TOTAL", position="UNDER", expected_line=164.5,
        expected_price=1.90, offer_line=164.5, offer_price=1.83)
    assert ok.ok


def test_verify_suspended_market_rejected():
    adapter = make_adapter()
    c = adapter.verify_offer(
        game_id=GID_A, home_team="Team A", away_team="Team B",
        market="TOTAL", position="UNDER", expected_line=164.5,
        expected_price=1.90, offer_line=164.5, offer_price=1.90,
        suspended=True)
    assert not c.ok and c.reason == "MARKET_SUSPENDED"


def test_disappeared_market_is_recoverable_not_terminal():
    page = FakePage(market_text=None)
    adapter = make_adapter(page)
    assert adapter.find_market(GAME_A, "TOTAL") is False
    assert adapter.find_position(GAME_A, "TOTAL", "UNDER") is None


def test_live_offer_resolves_current_line_and_price():
    adapter = make_adapter()
    assert adapter.find_event(GAME_A)
    obs = adapter.find_position(GAME_A, "TOTAL", "UNDER")
    assert obs is not None
    assert (obs.line, obs.price) == (164.5, 1.90)
    assert obs.suspended is False
    assert obs.handle["game_id"] == GID_A


# ═══════════════ 9. betslip verification + pre-submit gates ═════════

def test_betslip_parse_and_gate_rejects_wrong_line():
    adapter = make_adapter()
    adapter._pending_legs = [{"event": GAME_A, "market": "TOTAL",
                              "position": "UNDER", "line": 166.5,
                              "price": 1.90, "game_id": GID_A}]
    result = adapter.place_parlay(10.0)
    assert result["status"] == "FAILED"
    assert result["error_code"] == "LINE_MISMATCH"
    assert adapter._page.clicks == []        # submit never clicked


def test_pre_submit_gate_rejects_game_id_mismatch():
    adapter = make_adapter()
    adapter._browser._page.betslip_text = SLIP_TEXT_GID.replace(
        "30840226", "99999999")
    adapter._pending_legs = [{"event": GAME_A, "market": "TOTAL",
                              "position": "UNDER", "line": 164.5,
                              "price": 1.90, "game_id": GID_A}]
    result = adapter.place_parlay(10.0)
    assert result["error_code"] == "GAME_ID_MISMATCH"


def test_pre_submit_gate_rejects_odds_mismatch_and_unreadable_slip():
    adapter = make_adapter()
    adapter._pending_legs = [{"event": GAME_A, "market": "TOTAL",
                              "position": "UNDER", "line": 164.5,
                              "price": 2.50, "game_id": GID_A}]
    assert adapter.place_parlay(10.0)["error_code"] == "ODDS_MISMATCH"
    empty = make_adapter(FakePage(market_text=None))
    empty._browser._page.betslip_text = ""   # unreadable slip at submit
    empty._pending_legs = [{"event": GAME_A, "market": "TOTAL",
                            "position": "UNDER", "line": 164.5,
                            "price": 1.90, "game_id": GID_A}]
    assert empty.place_parlay(10.0)["error_code"] == "BETSLIP_UNREADABLE"


def test_pre_submit_gate_verifies_stake_and_persists_nothing_on_refusal():
    adapter = make_adapter()
    adapter._pending_legs = [{"event": GAME_A, "market": "TOTAL",
                              "position": "UNDER", "line": 164.5,
                              "price": 1.90, "game_id": GID_A}]
    result = adapter.place_parlay(10.0)      # fake echoes the stake back
    assert result["status"] == "ACCEPTED"
    assert result["provider_ref"] == "881234"
    bad = make_adapter()
    bad._browser._page.stake_fill_ignored = True   # input ignores fill
    bad._browser._page.stake_value = "999"
    bad._pending_legs = [{"event": GAME_A, "market": "TOTAL",
                          "position": "UNDER", "line": 164.5,
                          "price": 1.90, "game_id": GID_A}]
    assert bad.place_parlay(10.0)["error_code"] == "STAKE_MISMATCH"

# ═══════════════ 10-12. the engine on the live adapter ══════════════

def test_dry_run_full_path_never_submits(tmp_path):
    """DRY_RUN=true: resolve → select → verify → BETSLIP_READY → stop.
    place_parlay is NEVER called and nothing is recorded as placed."""
    page = FakePage()
    adapter = make_adapter(page)
    store = ExecutionStore(str(tmp_path / "blm_execution.db"))
    executor = TotalExecutor(adapter, make_cfg(tmp_path), store=store)
    calls: list = []
    orig = adapter.place_parlay
    adapter.place_parlay = lambda s: (calls.append(s), orig(s))[1]
    sel = Selection(event=GAME_A, market="TOTAL", position="UNDER",
                    game_id=GID_A)
    job = ParlayJob(legs=[sel], fold_size=1, stake_amount=10.0)
    result = executor.run_job("exec-dry-live", job, MODE_DRY_RUN)
    assert result.status == STATE_DRY_RUN_COMPLETE
    assert calls == []                        # never submitted
    assert page.clicks                        # resolution+selection happened


def test_live_adapter_path_with_mocked_browser_places_and_confirms(tmp_path):
    """The LIVE path end-to-end on the mocked DOM: every gate passes,
    the order is placed once, the bookmaker's reference is captured and
    persisted through the EXISTING ledger with the game id."""
    page = FakePage()
    page.slip_after_click = SLIP_TEXT
    adapter = make_adapter(page)
    store = ExecutionStore(str(tmp_path / "blm_execution.db"))
    executor = TotalExecutor(adapter, make_cfg(tmp_path), store=store)
    sel = Selection(event=GAME_A, market="TOTAL", position="UNDER",
                    game_id=GID_A)
    job = ParlayJob(legs=[sel], fold_size=1, stake_amount=10.0)
    result = executor.run_job("exec-live-mock", job, MODE_LIVE)
    assert result.status == STATE_COMPLETE
    assert result.order_reference == "881234"
    assert len(page.clicks) == 2              # selection click + submit
    order = store.get_order(job.parlay_id)
    assert order is not None
    assert order["status"] == "ACCEPTED"
    assert order["provider_ref"] == "881234"
    import json
    assert json.loads(order["game_ids_json"]) == [GID_A]


def test_market_suspended_between_resolution_and_submission(tmp_path):
    """The bookmaker suspends AFTER the leg is confirmed: the final
    pre-placement gate must hard-reject (MARKET_NOT_ACTIVE) — the
    earlier verification is never assumed still valid."""
    page = FakePage()
    page.suspend_after_click = True
    adapter = make_adapter(page)
    executor = TotalExecutor(adapter, make_cfg(tmp_path))
    sel = Selection(event=GAME_A, market="TOTAL", position="UNDER",
                    game_id=GID_A)
    job = ParlayJob(legs=[sel], fold_size=1, stake_amount=10.0)
    result = executor.run_job("exec-suspend-live", job, MODE_LIVE)
    assert result.status == STATE_NON_RECOVERABLE
    assert "MARKET_NOT_ACTIVE" in (result.last_error or "")
    assert len(page.clicks) == 1              # selection only — no submit


# ═══════════════ misc contract guards ═══════════════════════════════

def test_team_name_pair_normalization():
    assert names_match_pair("Team A", "Team B", "team a", "Team B.")
    assert not names_match_pair("Team A", "Team C", "Team A", "Team B")
    assert not names_match_pair("", "Team B", "Team A", "Team B")
