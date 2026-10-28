"""Auto-bet must survive the actuator browser being relaunched underneath it.

Requirement (operator, 2026-10-27): the browser on CDP :9222 is restarted by
systemd (`blm-exec-actuator.service`, Restart=always) and by snap refreshes.
Before this change the provider kept the ONE Playwright handle it built at the
first betting pass, so after any relaunch the page was dead for good: the
balance read None on every pass and a submission would fail at the transport —
the engine only recovered when the server itself was restarted.  The cached
transport is now probed and rebuilt, so one pass is enough to re-arm.

One named test per property.
"""
import os
import threading

import pytest

from blm_v4.betting.provider import PokerBetBrowserProvider


class _FakePage:
    def __init__(self, closed: bool = False, probe_error: bool = False,
                 url: str = "https://www.pokerbet.co.za/en/sports/live",
                 has_balance: bool = True):
        self.closed = closed
        self.probe_error = probe_error
        self.url = url
        self.has_balance = has_balance
        self.navigated_to = None

    def is_closed(self):
        if self.probe_error:
            raise AssertionError("liveness probed off the wiring thread")
        return self.closed

    def goto(self, url, wait_until=None, timeout=None):
        self.navigated_to = url
        self.url = url
        self.has_balance = True

    def wait_for_timeout(self, ms):
        return None


class _FakeContext:
    def __init__(self, pages):
        self.pages = pages


class _FakeCdpBrowser:
    """Stands in for CdpBrowser: one instance per (re)connect."""

    connect_count = 0
    fail_connect = False
    balance = 250.0
    pages_factory = None      # tests preset a restored-tab ORDER here

    def __init__(self, url):
        self.url = url
        self.page_obj = _FakePage()
        self.pages = (list(type(self).pages_factory)
                      if type(self).pages_factory else [self.page_obj])
        self.disconnected = False

    def connect(self):
        if type(self).fail_connect:
            raise RuntimeError("CDP endpoint unreachable")
        type(self).connect_count += 1
        return self

    @property
    def connected(self):
        return not self.page_obj.closed

    @property
    def context(self):
        return _FakeContext(self.pages)

    def page(self):
        for p in self.pages:
            if "pokerbet.co.za" in (getattr(p, "url", "") or ""):
                return p
        blank = _FakePage(url="about:blank", has_balance=False)
        self.pages.append(blank)
        return blank

    def disconnect(self):
        self.disconnected = True
        self.page_obj.closed = True


class _FakeAdapter:
    def __init__(self, browser, hydrate_timeout_ms=15000):
        self._browser = browser
        self._page = None

    def read_balance(self):
        if self._page is None or self._page.is_closed():
            return None
        if not getattr(self._page, "has_balance", True):
            return None
        return _FakeCdpBrowser.balance


class _FakeBridge:
    def __init__(self, adapter, **kw):
        self.adapter = adapter
        self.placed = 0

    def place(self, *, command, stake_amount):
        self.placed += 1
        return {"status": "ACCEPTED", "provider_ref": "fake-accepted"}


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    import blm_v4.execution.pokerbet.browser as browser_mod
    import blm_v4.execution.pokerbet.dom as dom_mod
    import blm_v4.execution.browser_bridge as bridge_mod

    _FakeCdpBrowser.connect_count = 0
    _FakeCdpBrowser.fail_connect = False
    _FakeCdpBrowser.balance = 250.0
    _FakeCdpBrowser.pages_factory = None
    monkeypatch.setattr(browser_mod, "CdpBrowser", _FakeCdpBrowser)
    monkeypatch.setattr(dom_mod, "PokerBetDomAdapter", _FakeAdapter)
    monkeypatch.setattr(bridge_mod, "ResolverBrowserBridge", _FakeBridge)
    yield


def _provider(live=False):
    return PokerBetBrowserProvider(cdp_url="http://127.0.0.1:9222",
                                   db_path=None, live=live)


def test_a_dead_page_triggers_a_rewire_and_the_balance_returns():
    p = _provider()
    assert p.account_balance() == 250.0
    assert _FakeCdpBrowser.connect_count == 1
    old_browser = p._adapter._browser

    # the operator/systemd relaunched the browser: the cached handle is dead
    old_browser.page_obj.closed = True
    assert p.account_balance() == 250.0          # numeric again, same pass
    assert _FakeCdpBrowser.connect_count == 2    # exactly one re-attach
    assert p._rewires == 1
    assert old_browser.disconnected is True      # dead handle let go
    assert p._adapter._browser is not old_browser


def test_a_live_page_is_reused_without_rewiring():
    p = _provider()
    assert p.account_balance() == 250.0
    assert p.account_balance() == 250.0
    assert _FakeCdpBrowser.connect_count == 1
    assert p._rewires == 0


def test_a_failed_rewire_stays_fail_closed_and_retries_next_time():
    p = _provider()
    assert p.account_balance() == 250.0
    p._adapter._browser.page_obj.closed = True

    _FakeCdpBrowser.fail_connect = True          # browser not back yet
    assert p.account_balance() is None           # no exception, no phantom number
    assert p._bridge is None                     # nothing cached as usable

    _FakeCdpBrowser.fail_connect = False         # systemd brought it back
    assert p.account_balance() == 250.0
    assert _FakeCdpBrowser.connect_count == 2


def test_an_off_thread_caller_never_probes_or_rewires():
    p = _provider()
    assert p.account_balance() == 250.0
    # a probe from the wiring thread is fine …
    dying = _FakePage(closed=True, probe_error=True)
    p._adapter._page = dying
    p._wire_thread = threading.get_ident() ^ 0xFFFF   # pretend another thread
    # … but an off-thread caller keeps the legacy reuse-only behaviour:
    assert p._transport_usable() is True
    assert p._rewires == 0


def test_submit_re_attaches_before_the_gate(monkeypatch):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "1")
    p = _provider(live=True)
    first = p.submit(execution_id="bet-1", game_id="123", alert_id="123|75",
                     selection="UNDER", price=1.8, stake_amount=100.0,
                     line=180.5)
    assert first["status"] == "ACCEPTED"
    assert _FakeCdpBrowser.connect_count == 1

    # the browser is relaunched between two candidates
    p._adapter._browser.page_obj.closed = True
    second = p.submit(execution_id="bet-2", game_id="456", alert_id="456|75",
                      selection="UNDER", price=1.8, stake_amount=100.0,
                      line=175.5)
    assert second["status"] == "ACCEPTED"
    assert _FakeCdpBrowser.connect_count == 2
    assert p._rewires == 1


def test_attach_prefers_a_tab_whose_balance_reads():
    """A relaunched browser restores tabs in an arbitrary order; a stale tab
    with no header must not win over a tab that can actually read a balance."""
    bad = _FakePage(url="https://www.pokerbet.co.za/en/sports/live/event-view/Soccer/Algeria/x",
                    has_balance=False)
    good = _FakePage(url="https://www.pokerbet.co.za/en/sports/live/event-view/Soccer/Virtual/y",
                     has_balance=True)
    _FakeCdpBrowser.pages_factory = [bad, good]
    p = _provider()
    assert p.account_balance() == 250.0
    assert p._adapter._page is good


def test_attach_navigates_a_blank_page_when_no_tab_can_read_a_balance():
    blank = _FakePage(url="about:blank", has_balance=False)
    _FakeCdpBrowser.pages_factory = [blank]
    p = _provider()
    assert p.account_balance() == 250.0
    # the attached page (a fresh blank tab when no pokerbet tab exists) is
    # navigated to the live page so it becomes usable
    assert p._adapter._page.navigated_to == "https://www.pokerbet.co.za/en/sports/live"
    assert "pokerbet.co.za" in p._adapter._page.url


def test_a_non_pokerbet_page_is_not_a_usable_transport():
    p = _provider()
    assert p.account_balance() == 250.0
    p._adapter._page = _FakePage(url="about:blank")
    assert p._transport_usable() is False
