"""BLM EXECUTION — POKERBET LIVE ADAPTER TESTS (phase ③, mocked DOM).

Directive 2026-09-23 §4 scenarios, all against a FAKE page/browser —
no live PokerBet website, no network, no real money:

  authenticated session detection · game resolution · game_id mismatch
  · team mismatch · market mismatch · line mismatch · odds mismatch
  (incl. tolerance) · suspended market · disappeared market · betslip
  verification · DRY_RUN never submitting · successful LIVE adapter
  path on a mocked browser/DOM · execution confirmation persistence.

2026-10-05 READ-PATH HARDENING (live-verified against a real authenticated
PokerBet session on both Soccer and Basketball).  The fake DOM below models
the REAL structure — the header renders each team name more than once, the
Totals grid holds the GAME total *and* team totals whose titles carry the
team name (``"<Team> Total Points"``), and each item can hold several
line-triples.  The adapter must therefore (a) never assume two header
nodes, (b) resolve the GAME total explicitly rather than ``totals.first``,
and (c) fail closed on ambiguous/missing game totals and on hydration
timeout.  The WRITE path (betslip → stake → submit → receipt) is unchanged
and remains unverified.

The REAL contracts stay intact: the adapter implements the existing
SelectionResolver protocol, the engine's terminal semantics are
unchanged, and the pre-submit verification runs immediately before
placement — never earlier and assumed valid.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_browser_adapter import FakeBrowserAdapter  # noqa: E402,F401  (protocol sibling)

from blm_v4.execution.adapter import AdapterUnavailable  # noqa: E402
from blm_v4.execution.config import ExecutionConfig  # noqa: E402
from blm_v4.execution.pokerbet import dom as DOM  # noqa: E402
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
SLUG = "team-a-vs-team-b"
# The REAL event-view URL shape: …/<Sport>/<Region>/<comp_id>/<slug>/
# <event_id>/<name>.  The event id is the FINAL id segment before the slug.
SOURCE_URL = ("https://www.pokerbet.co.za/en/sports/live/event-view/"
              "Basketball/World/18296901/betual-nba/30840226/" + SLUG)
# legacy builder (kept only to assert it still exists + is not used)
EVENT_URL_LEGACY = ("https://www.pokerbet.co.za/en/sports/live/event-view/"
                    "Basketball/World/1/betual-nba/30840226/x")
SLIP_TEXT = "Team A vs Team B\nTotal Points\nUnder 164.5 @ 1.90\n"
SLIP_TEXT_GID = (SLIP_TEXT + "Ref 30840226\n")
RECEIPT = "Bet placed successfully — reference 881234"


# ── the validated real DOM model ──────────────────────────────────────

def _market(title, lines, suspended=False):
    """One ``.sgm-market-g`` group: a title + (line, over, under) triples."""
    return {"title": title, "lines": [tuple(l) for l in lines],
            "suspended": bool(suspended)}


#: cells[0]="Over", cells[1]="Under", then line, over, under per triple.
#: A market may carry an explicit "cells" override to model a malformed grid
#: (missing line / missing odds).
def _cells(m):
    if "cells" in m:
        return list(m["cells"])
    out = ["Over", "Under"]
    for (ln, o, u) in m["lines"]:
        out += [f"{ln:g}", f"{o:.2f}", f"{u:.2f}"]
    return out


def _item_text(m):
    return "\n".join(_cells(m))


def _group_text(m):
    return f"{m['title']}\n\n{_item_text(m)}"


DEFAULT_MARKETS = [_market("Total Points", [(164.5, 1.90, 1.90)])]


class _E:
    """A fake element."""

    def __init__(self, text="", cls="", market=None, role="leaf",
                 disabled=False):
        self.text = text
        self.cls = cls
        self.market = market
        self.role = role
        self.disabled = disabled


class FakeLocator:
    def __init__(self, page, elements, sel=""):
        self._page = page
        self._els = list(elements)
        self._sel = sel

    def count(self) -> int:
        return len(self._els)

    def nth(self, i) -> "FakeLocator":
        return FakeLocator(self._page, [self._els[i]], self._sel)

    @property
    def first(self) -> "FakeLocator":
        return FakeLocator(self._page, self._els[:1], self._sel)

    @property
    def last(self) -> "FakeLocator":
        return FakeLocator(self._page, self._els[-1:], self._sel)

    def inner_text(self) -> str:
        return self._els[0].text if self._els else ""

    def get_attribute(self, name):
        if not self._els:
            return None
        return self._els[0].cls if name == "class" else None

    def is_disabled(self) -> bool:
        return bool(self._els[0].disabled) if self._els else True

    def click(self, timeout=None):
        el = self._els[0] if self._els else None
        self._page._on_click(el.role if el else "leaf", el)
        if self._page.click_should_fail:
            raise RuntimeError("click failed")

    def evaluate(self, js, arg=None):
        """The adapter's BETUAL strategy activates an odds cell with an
        in-page DOM click (``el => el.click()``); the fixture models a
        BETUAL event, so mirror the normal click path (and its fail flag)
        for that call.  Other expressions are not needed by the adapter."""
        if isinstance(js, str) and "click" in js:
            self.click()
        return None

    def fill(self, value):
        if not getattr(self._page, "stake_fill_ignored", False):
            self._page.stake_value = value

    def input_value(self) -> str:
        return str(self._page.stake_value)

    def locator(self, sel) -> "FakeLocator":
        """Sub-locator — only the market-group ancestor xpath and the
        market-cell selector are used by the adapter."""
        if not self._els:
            return FakeLocator(self._page, [], sel)
        el = self._els[0]
        if "ancestor" in sel and "sgm-market-g" in sel:
            if el.market is not None:
                return FakeLocator(self._page, [_E(
                    text=_group_text(el.market), cls="sgm-market-g ",
                    market=el.market, role="group")], sel)
            return FakeLocator(self._page, [], sel)
        if el.market is not None and "sgm-market-g-i-cell-bc" in sel:
            cells = _cells(el.market)
            # cells = [Over hdr, Under hdr, (line, OVER odds, UNDER odds)*]
            # The ODDS cells are the real clickable controls (live-verified
            # 2026-10-05); headers/line are not.
            els = []
            for i, c in enumerate(cells):
                is_odds = i >= 3 and (i - 2) % 3 in (1, 2)
                els.append(_E(text=c, cls="sgm-market-g-i-cell-bc",
                              market=el.market,
                              role=("selection" if is_odds else "cell")))
            return FakeLocator(self._page, els, sel)
        return FakeLocator(self._page, [], sel)


class FakePage:
    def __init__(self, url=SOURCE_URL, teams=("Team A", "Team B"),
                 markets=None, duplicate_teams=True, ready=True,
                 ready_after_ticks=0, totals_selected=True,
                 betslip_text=SLIP_TEXT, receipt_text=RECEIPT,
                 signin_count=0, suspended=False):
        self.url = url
        self.teams = list(teams)
        self.markets = (DEFAULT_MARKETS if markets is None else list(markets))
        self.duplicate_teams = duplicate_teams
        self.ready_headers = ready
        self.ready_tabs = ready
        self.ready_after_ticks = ready_after_ticks
        self.ticks = 0
        self.totals_selected = totals_selected
        self.betslip_text = betslip_text
        self.slip_after_click = betslip_text
        self.suspend_after_click = False
        self.receipt_text = receipt_text
        self.signin_count = signin_count
        self.suspended = suspended
        self.clicks: list = []
        self.goto_calls: list = []
        self.click_should_fail = False
        self.stake_value = "0"
        self.stake_fill_ignored = False
        #: the SUBMIT control is state-dependent on the live site — the double
        #: can model it as absent or DISABLED so the gate's refusals are real
        self.place_button_present = True
        self.place_button_disabled = False
        self.tabs = ["", "All", "Match", "Totals", "Handicaps", "Halves",
                     "Quarters"]

    # ── the exact playwright surface dom.py touches ────────────────────
    def goto(self, url):
        self.url = url
        self.goto_calls.append(url)
    def wait_for_load_state(self, *a, **k):
        pass

    def wait_for_timeout(self, ms):
        self.ticks += 1
        if self.ready_after_ticks and self.ticks >= self.ready_after_ticks:
            self.ready_headers = True
            self.ready_tabs = True

    def _on_click(self, role, el):
        self.clicks.append({"role": role, "text": (el.text if el else "")})
        if role == "tab":
            if "total" in (el.text or "").lower():
                self.totals_selected = True
        elif role == "selection":
            if self.slip_after_click is not None:
                self.betslip_text = self.slip_after_click
            if self.suspend_after_click:
                self.suspended = True

    def _visible_markets(self):
        if not (self.ready_headers and self.ready_tabs
                and self.totals_selected):
            return []
        return self.markets

    def locator(self, sel, has_text=None):
        if sel.startswith("text="):
            return FakeLocator(self, [_E() for _ in range(self.signin_count)])
        if sel == DOM.SEL_TEAM_HEADER:
            if not self.ready_headers:
                return FakeLocator(self, [])
            names = []
            for t in self.teams:
                names += [t, t] if self.duplicate_teams else [t]
            return FakeLocator(self, [_E(text=n, cls="game-d-c-b-r-c-team-name")
                                      for n in names])
        if sel == DOM.SEL_MARKET_TAB:
            if not self.ready_tabs:
                return FakeLocator(self, [])
            return FakeLocator(self, [_E(text=t, cls="horizontal-sl-tab-bc",
                                         role="tab") for t in self.tabs])
        if sel == DOM.SEL_MARKET_ITEM:
            return FakeLocator(self, [_E(text=_item_text(m),
                                         cls="sgm-market-g-item-bc",
                                         market=m, role="item")
                                      for m in self._visible_markets()])
        if sel == DOM.SEL_MARKET_CELL:
            return FakeLocator(self, [
                _E(text=c, cls="sgm-market-g-i-cell-bc")
                for m in self._visible_markets() for c in _cells(m)])
        if "suspended" in sel or "locked" in sel:
            susp = bool(self.suspended) or any(
                m.get("suspended") for m in self._visible_markets())
            return FakeLocator(self, [_E()] if susp else [])
        if sel == DOM.SEL_BETSLIP_LEG or "bs-bet-item-bc" in sel:
            # the verified leg container: one leg per entry in the fake slip
            return (FakeLocator(self, [_E(text=self.betslip_text,
                                          cls="bs-bet-item-bc")])
                    if self.betslip_text else FakeLocator(self, []))
        if sel == DOM.SEL_BETSLIP or "betslip" in sel:
            return FakeLocator(self, [_E(text=self.betslip_text)]
                               if self.betslip_text else [])
        if sel == DOM.SEL_STAKE_INPUT or "stake" in sel or "number" in sel:
            return FakeLocator(self, [_E(role="stake")])
        if sel == DOM.SEL_RECEIPT or "receipt" in sel or "confirmation" in sel:
            return FakeLocator(self, [_E(text=self.receipt_text)]
                               if self.receipt_text else [])
        if sel == DOM.SEL_PLACE_BUTTON or "outcome" in sel or sel == "button":
            if has_text and str(has_text).lower() in ("over", "under"):
                return FakeLocator(self, [_E(role="selection")])
            if has_text and str(has_text).lower() == "place":
                return FakeLocator(self, [_E(role="submit", disabled=(
                    not self.place_button_present
                    or self.place_button_disabled))])
            if sel == DOM.SEL_PLACE_BUTTON:
                if not self.place_button_present:
                    return FakeLocator(self, [])
                return FakeLocator(self, [_E(
                    role="submit", text="ACCEPT CHANGES AND PLACE BET",
                    disabled=self.place_button_disabled)])
            return FakeLocator(self, [])
        return FakeLocator(self, [])


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
    kw.setdefault("hydrate_timeout_ms", 200)   # fast bounded wait in tests
    adapter = PokerBetDomAdapter(FakeBrowser(page), **kw)
    adapter.register_game(GAME_A, game_id=GID_A,
                          home_team="Team A", away_team="Team B",
                          classification="BETUAL_NBA",
                          source_url=SOURCE_URL)
    return adapter


def make_attached_adapter(page: Optional[FakePage] = None, **kw):
    """make_adapter PLUS the fake page attached up-front, so the READ helpers
    are unit-testable directly.  The engine's real flow attaches the page via
    find_event() instead — which matters: the pre-click idempotency probe must
    see an EMPTY slip while the adapter has no page yet."""
    adapter = make_adapter(page, **kw)
    adapter._page = adapter._browser.page()
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
    # legacy builder still exists (back-compat) …
    url = session_mod.event_view_url("30840226", "BETUAL_NBA")
    assert url.endswith("/betual-nba/30840226/x")
    assert url.startswith("https://www.pokerbet.co.za/en/sports/")
    # … but the canonical resolver returns the RECORDED source_url verbatim
    assert session_mod.event_view_url_for({"source_url": SOURCE_URL}) \
        == SOURCE_URL


def test_missing_source_url_fails_closed_unrecoverable():
    """No recorded source_url → the adapter must NOT fabricate a URL."""
    adapter = PokerBetDomAdapter(FakeBrowser(), hydrate_timeout_ms=200)
    adapter.register_game(GAME_A, game_id=GID_A, home_team="Team A",
                          away_team="Team B")          # no source_url
    with pytest.raises(AdapterUnavailable):
        adapter.find_event(GAME_A)


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
    page = FakePage(markets=[])
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

def test_betslip_parse_and_gate_accepts_moved_line():
    """Production rule 2026-10-06: line movement is IRRELEVANT — a slip leg
    whose line moved away from the verified offer is ACCEPTED (taken at the
    book's current line), never refused."""
    adapter = make_adapter()
    adapter._pending_legs = [{"event": GAME_A, "market": "TOTAL",
                              "position": "UNDER", "line": 166.5,
                              "price": 1.90, "game_id": GID_A}]
    result = adapter.place_parlay(10.0)
    assert result["status"] in ("ACCEPTED", "SUBMITTED")


def test_pre_submit_gate_rejects_game_id_mismatch():
    adapter = make_adapter()
    adapter._browser._page.betslip_text = SLIP_TEXT_GID.replace(
        "30840226", "99999999")
    adapter._pending_legs = [{"event": GAME_A, "market": "TOTAL",
                              "position": "UNDER", "line": 164.5,
                              "price": 1.90, "game_id": GID_A}]
    result = adapter.place_parlay(10.0)
    assert result["error_code"] == "GAME_ID_MISMATCH"


def test_pre_submit_gate_accepts_moved_odds_but_rejects_unreadable_slip():
    """Production rule 2026-10-06: a moved price is ACCEPTED (taken at the
    book's current odds); an UNREADABLE slip is still refused."""
    adapter = make_adapter()
    adapter._pending_legs = [{"event": GAME_A, "market": "TOTAL",
                              "position": "UNDER", "line": 164.5,
                              "price": 2.50, "game_id": GID_A}]
    assert adapter.place_parlay(10.0)["status"] in ("ACCEPTED", "SUBMITTED")
    empty = make_adapter(FakePage(markets=[]))
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


# ════════════ 13. READ-PATH HARDENING (live-validated 2026-10-05) ═══

#: the REAL basketball layout: GAME total + two TEAM totals, each title
#: carrying either nothing (game) or a team name (team total).
BASKETBALL_MARKETS = [
    _market("Total Points", [(182.5, 1.85, 1.85), (184.5, 1.95, 1.75)]),
    _market("Team A Total Points", [(84.5, 2.10, 1.65)]),
    _market("Team B Total Points", [(98.5, 1.85, 1.85)]),
]


def test_duplicated_team_headers_resolve_two_teams():
    """The real header renders each team name twice (4 nodes for 2 teams):
    count == 2 must never be required, and the pair must resolve."""
    page = FakePage(duplicate_teams=True)
    assert page.locator(DOM.SEL_TEAM_HEADER).count() == 4
    adapter = make_attached_adapter(page)
    assert adapter._page_teams() == ("Team A", "Team B")
    assert adapter.find_event(GAME_A) is True


def test_single_node_team_headers_still_resolve():
    page = FakePage(duplicate_teams=False)
    assert page.locator(DOM.SEL_TEAM_HEADER).count() == 2
    adapter = make_attached_adapter(page)
    assert adapter._page_teams() == ("Team A", "Team B")


def test_three_distinct_header_values_fail_closed():
    page = FakePage(teams=("Team A", "Team B", "Team C"),
                    duplicate_teams=False)
    adapter = make_attached_adapter(page)
    assert adapter._page_teams() == (None, None)      # cannot establish two


def test_basketball_game_total_not_a_team_total():
    """GAME total + two TEAM totals → the GAME total (182.5) is read, never
    a team total (84.5 / 98.5)."""
    adapter = make_attached_adapter(FakePage(markets=BASKETBALL_MARKETS))
    assert adapter.find_event(GAME_A)
    obs = adapter.find_position(GAME_A, "TOTAL", "OVER")
    assert obs is not None
    assert obs.line == 182.5                 # the GAME total, not 84.5/98.5
    assert obs.price == 1.85                 # OVER of the first game triple
    under = adapter.find_position(GAME_A, "TOTAL", "UNDER")
    assert (under.line, under.price) == (182.5, 1.85)


def test_reordered_totals_still_find_the_game_total():
    """The game total is NOT first — order must not decide identity."""
    reordered = [BASKETBALL_MARKETS[1], BASKETBALL_MARKETS[2],
                 BASKETBALL_MARKETS[0]]
    adapter = make_attached_adapter(FakePage(markets=reordered))
    obs = adapter.find_position(GAME_A, "TOTAL", "OVER")
    assert obs is not None and obs.line == 182.5


def test_missing_game_total_fails_closed():
    """Only team totals present → NO game total → fail closed (never pick a
    team total as if it were the game total)."""
    only_team_totals = [_market("Team A Total Points", [(84.5, 2.10, 1.65)]),
                        _market("Team B Total Points", [(98.5, 1.85, 1.85)])]
    adapter = make_attached_adapter(FakePage(markets=only_team_totals))
    assert adapter.find_market(GAME_A, "TOTAL") is False
    assert adapter.find_position(GAME_A, "TOTAL", "UNDER") is None


def test_ambiguous_game_total_fails_closed():
    """Two title-less totals candidates → ambiguous → fail closed."""
    ambiguous = [_market("Total Points", [(182.5, 1.85, 1.85)]),
                 _market("Total Runs", [(3.5, 1.90, 1.90)])]
    adapter = make_attached_adapter(FakePage(markets=ambiguous))
    assert adapter.find_market(GAME_A, "TOTAL") is False
    assert adapter.find_position(GAME_A, "TOTAL", "OVER") is None


def test_hydration_delay_is_waited_for_then_succeeds():
    """The page is NOT ready at load (header/tabs absent) and only becomes
    ready after a few polls; the Totals tab is then selected.  The bounded
    wait must carry through to success."""
    page = FakePage(ready=False, ready_after_ticks=3, totals_selected=False)
    adapter = make_attached_adapter(page)
    assert adapter.find_event(GAME_A) is True
    assert any(c["role"] == "tab" for c in page.clicks)   # Totals selected
    obs = adapter.find_position(GAME_A, "TOTAL", "UNDER")
    assert obs is not None and obs.line == 164.5


def test_hydration_timeout_fails_closed_and_is_bounded():
    """Hydration never completes → fail closed, and within the bound."""
    page = FakePage(ready=False, ready_after_ticks=0, totals_selected=False)
    adapter = make_attached_adapter(page, hydrate_timeout_ms=150)
    t0 = time.monotonic()
    assert adapter.find_event(GAME_A) is False
    assert (time.monotonic() - t0) < 5.0      # bounded, no hang


def test_hydration_timeout_on_market_reads_fails_closed():
    page = FakePage(ready=False, ready_after_ticks=0, totals_selected=False)
    adapter = make_attached_adapter(page, hydrate_timeout_ms=150)
    assert adapter.find_market(GAME_A, "TOTAL") is False
    assert adapter.find_position(GAME_A, "TOTAL", "UNDER") is None


def test_live_line_and_decimal_odds_are_distinct():
    """executable_line is never mapped into odds: the line is a half-point
    and the odds are decimal prices."""
    adapter = make_attached_adapter(FakePage(
        markets=[_market("Total Points", [(164.5, 1.90, 1.95)])]))
    obs_o = adapter.find_position(GAME_A, "TOTAL", "OVER")
    obs_u = adapter.find_position(GAME_A, "TOTAL", "UNDER")
    assert obs_o.line == 164.5 and obs_u.line == 164.5
    assert obs_o.price == 1.90 and obs_u.price == 1.95    # separate odds
    assert obs_o.line != obs_o.price


def test_multi_triple_grid_reads_first_triple():
    adapter = make_attached_adapter(FakePage(markets=[
        _market("Total Points", [(182.5, 1.85, 1.85),
                                 (184.5, 1.95, 1.75),
                                 (186.5, 2.20, 1.60)])]))
    obs = adapter.find_position(GAME_A, "TOTAL", "OVER")
    assert (obs.line, obs.price) == (182.5, 1.85)


def test_suspended_market_reported_in_observation():
    adapter = make_attached_adapter(FakePage(markets=[
        _market("Total Points", [(164.5, 1.90, 1.90)], suspended=True)]))
    obs = adapter.find_position(GAME_A, "TOTAL", "UNDER")
    assert obs is not None and obs.suspended is True


def test_unavailable_market_returns_none():
    adapter = make_attached_adapter(FakePage(markets=[]))
    assert adapter.find_position(GAME_A, "TOTAL", "OVER") is None


def test_missing_odds_fails_closed():
    """A game-total item whose odds cells are blank → no price → closed."""
    m = {"title": "Total Points", "lines": [(164.5, 1.90, 1.90)],
         "cells": ["Over", "Under", "164.5", "", ""]}
    adapter = make_attached_adapter(FakePage(markets=[m]))
    assert adapter.find_position(GAME_A, "TOTAL", "OVER") is None
    assert adapter.find_position(GAME_A, "TOTAL", "UNDER") is None


def test_missing_line_fails_closed():
    """A totals item with no line token → closed."""
    m = {"title": "Total Points", "lines": [(164.5, 1.90, 1.90)],
         "cells": ["Over", "Under", "n/a", "1.90", "1.90"]}
    adapter = make_attached_adapter(FakePage(markets=[m]))
    assert adapter.find_position(GAME_A, "TOTAL", "OVER") is None


# ═══════════════ misc contract guards ═══════════════════════════════

def test_team_name_pair_normalization():
    assert names_match_pair("Team A", "Team B", "team a", "Team B.")
    assert not names_match_pair("Team A", "Team C", "Team A", "Team B")
    assert not names_match_pair("", "Team B", "Team A", "Team B")


# ── the virtual RENDERING suffix (live-verified blocker, 2026-10-05) ────────
REAL_MADRID = ("Real Madrid Virtual", "BC Dubai Virtual")


def test_game_total_ignores_the_odd_even_market():
    """Live 2026-10-05: "Total Points Odd/Even" also contains "total" with no
    team name, so it was a SECOND candidate and the game total could never
    resolve (fail closed) on every page that carried it."""
    page = FakePage(teams=REAL_MADRID, markets=[
        _market("Match Winner", [(0, 1.17, 4.42)]),
        _market("Total Points", [(193.5, 1.55, 2.30), (195.5, 1.95, 1.75)]),
        _market("Real Madrid Virtual Total Points", [(100.5, 1.95, 1.75)]),
        _market("BC Dubai Virtual Total Points", [(94.5, 1.85, 1.85)]),
        _market("Total Points Odd/Even", [(0, 1.96, 1.78)]),
    ])
    adapter = make_attached_adapter(page)
    item = adapter._totals_item()
    assert item is not None
    text = item.inner_text()
    assert "193.5" in text and "Even" not in text
    line, over, under = adapter._read_totals()
    assert (line, over, under) == (193.5, 1.55, 2.30)


def test_game_total_still_fails_closed_without_a_real_total():
    page = FakePage(teams=REAL_MADRID, markets=[
        _market("Total Points Odd/Even", [(0, 1.96, 1.78)]),
        _market("Real Madrid Virtual Total Points", [(100.5, 1.95, 1.75)]),
    ])
    adapter = make_attached_adapter(page)
    assert adapter._totals_item() is None          # never a parity/team total
    assert adapter._read_totals() == (None, None, None)


def test_virtual_rendering_suffix_matches_the_stored_base_name():
    """PokerBet renders every virtual participant "<Team> Virtual" while the
    collector stores the base name; without tolerating that literal the adapter
    could never resolve a virtual event (find_event=False with a matching
    game_id — observed live)."""
    assert names_match_pair("Bursaspor Virtual", "Esenler Erokspor Virtual",
                            "Bursaspor", "Esenler Erokspor")
    assert names_match_pair("KCC Egis Virtual",
                            "Goyang Sono Skygunners Virtual",
                            "KCC Egis", "Goyang Sono Skygunners")


def test_virtual_suffix_tolerance_never_merges_different_teams():
    """A real distinction that merely CONTAINS the suffix word must still fail:
    only the exact trailing literal is stripped."""
    assert not names_match_pair("Bursaspor U19 Virtual",
                                "Esenler Erokspor Virtual",
                                "Bursaspor", "Esenler Erokspor")
    assert not names_match_pair("Virtual", "Virtual", "Team A", "Team B")
    assert not names_match_pair("Denver Nuggets", "Orlando Magic",
                                "Denver Nuggets", "Orlando Magic U19")


def test_real_market_names_are_unaffected():
    assert names_match_pair("Denver Nuggets", "Orlando Magic",
                            "denver  nuggets", "ORLANDO MAGIC")
    assert not names_match_pair("Denver Nuggets", "Orlando Magic",
                                "Denver Nuggets", "Miami Heat")


# ═════════ BETUAL vs CYBER selection strategy (live-verified 2026-10-05) ═══
# BETUAL virtual odds cells only add to the betslip on an in-page DOM click;
# Cyber cells take a pointer click.  The Cyber path must stay untouched.

def test_betual_uses_the_dom_click_strategy():
    a = make_attached_adapter(FakePage(url=SOURCE_URL))       # betual-nba
    assert a._selection_strategy() == "dom_click"


def test_cyber_selection_strategy_stays_a_pointer_click():
    """PRESERVE the Cyber path — a Cyber event must NOT use DOM-click."""
    cyber = ("https://www.pokerbet.co.za/en/sports/live/event-view/"
             "Basketball/World/18295203/cyber-basketball-2k26-matches/"
             "31123138/x")
    a = make_attached_adapter(FakePage(url=cyber))
    assert a._selection_strategy() == "pointer"


def test_betual_slip_legs_are_read_structurally():
    """The BETUAL leg layout (selection/market BEFORE the event line) can't be
    read by the event-line-first text parse, so it is read from the leg DOM."""
    class _Leaf:
        def __init__(self, text=""):
            self._text = text
        def count(self):
            return 1 if self._text else 0
        @property
        def first(self):
            return self
        def inner_text(self):
            return self._text

    class _Leg:
        def __init__(self, fields):
            self._fields = fields
        def locator(self, sel):
            return _Leaf(self._fields.get(sel, ""))

    class _Legs:
        def __init__(self, legs):
            self._legs = [_Leg(f) for f in legs]
        def count(self):
            return len(self._legs)
        def nth(self, i):
            return self._legs[i]

    class _Page:
        def __init__(self, legs):
            self._legs = legs
        def locator(self, sel):
            return _Legs(self._legs) if "bs-bet-item-bc" in sel else _Legs([])

    page = _Page([{".bs-bet-i-b-title-bc.t-2": "Under (180.5)",
                   ".bs-bet-i-b-coefficient-bc": "1.95",
                   ".bs-bet-i-h-title-bc-text": "A Virtual - B Virtual"}])
    a = make_attached_adapter(FakePage(url=SOURCE_URL))
    a._page = page
    assert a._parse_betslip_dom() == [{
        "event": "A Virtual - B Virtual", "market": "TOTAL",
        "position": "UNDER", "line": 180.5, "price": 1.95, "game_id": None}]
