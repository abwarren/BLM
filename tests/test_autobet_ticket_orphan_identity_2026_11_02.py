"""The five fixes of 2026-11-02, one named test each.

1. ticket reference  — a real placement's ticket id becomes provider_ref
2. orphan reap       — rows a dead process left mid-flight are closed
3. balance jump      — a genuine step change needs (and now gets) a third read
4. stop budget       — TimeoutStopSec exceeds the documented shutdown joins
5. account identity  — the guard compares the page's account to the configured one
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from blm_v4.betting import account_guard
from blm_v4.betting.provider import PokerBetBrowserProvider
from blm_v4.betting.store import BettingStore
from blm_v4.execution.pokerbet import balance as _balance
from blm_v4.execution.pokerbet import dom as DOM


# ── minimal Playwright doubles ────────────────────────────────────────────
class _Loc:
    def __init__(self, texts):
        self._texts = list(texts)

    def count(self):
        return len(self._texts)

    @property
    def first(self):
        return self

    def inner_text(self):
        return self._texts[0] if self._texts else ""


class _Page:
    def __init__(self, balance_texts=None):
        self.open_bets = ""
        self.receipt = ""
        self.balance_texts = list(balance_texts or [])
        self.balance_reads = 0

    def locator(self, sel, has_text=None):
        if sel == DOM.SEL_OPEN_BETS:
            return _Loc([self.open_bets] if self.open_bets else [])
        if sel == DOM.SEL_RECEIPT:
            return _Loc([self.receipt] if self.receipt else [])
        if sel == ".balanceAmount":
            self.balance_reads += 1
            if not self.balance_texts:
                return _Loc([])
            idx = min(self.balance_reads - 1, len(self.balance_texts) - 1)
            return _Loc([self.balance_texts[idx]])
        return _Loc([])

    def wait_for_timeout(self, _ms):
        return None

    def content(self):
        return "<html>stub</html>"


class _Browser:
    def __init__(self, page):
        self._page = page

    def page(self):
        return self._page


def _adapter(page):
    ad = DOM.PokerBetDomAdapter(_Browser(page))
    ad._page = page
    return ad


# ── 1. ticket reference ───────────────────────────────────────────────────
def test_ticket_reference_comes_from_the_ticket_that_appeared():
    """A placement that adds exactly one ticket to OPEN BETS yields that id.

    Before this fix `read_order_confirmation` read a selector that matches
    nothing on the live site, once, with no wait — every real bet was
    recorded SUBMITTED with a NULL provider_ref.
    """
    page = _Page()
    page.open_bets = "OPEN BETS\n1111111111 stake 50"
    ad = _adapter(page)
    before = ad._open_bet_ids()
    assert before == {"1111111111"}

    page.open_bets = ("OPEN BETS\n1111111111 stake 50\n"
                      "6805870840 UNDER 1.75 stake 200")
    conf = ad.read_order_confirmation(before)
    assert conf == {"reference": "6805870840", "source": "open_bets"}


def test_two_new_tickets_are_ambiguous_not_a_guess():
    page = _Page()
    page.open_bets = "OPEN BETS\n1111111111"
    ad = _adapter(page)
    before = ad._open_bet_ids()
    page.open_bets = "OPEN BETS\n1111111111\n2222222222\n3333333333"
    assert ad.read_order_confirmation(before) is None


# ── 2. orphan reap ────────────────────────────────────────────────────────
def _claim(store, ik, execution_id):
    ok, _ = store.claim({
        "execution_id": execution_id, "idempotency_key": ik,
        "game_id": "31429632", "alert_id": "31429632|75", "checkpoint": "75",
        "price": 1.70, "unit_price": 220.0, "stake_units": 1.0,
        "stake_amount": 220.0, "status": "PENDING",
    })
    assert ok
    return execution_id


def test_orphan_reap_closes_only_rows_older_than_this_process(tmp_path):
    """A row left SUBMITTING by a killed process is closed; a row this
    process just opened is untouched."""
    store = BettingStore(str(tmp_path / "betting.db"))
    old = _claim(store, "ik-old", "exec-old")
    store.update_status(old, "SUBMITTING")
    con = store._conn()
    try:
        con.execute(
            "UPDATE bet_executions SET created_at='2026-11-02T02:13:24.390Z' "
            "WHERE execution_id=?", (old,))
        con.commit()
    finally:
        con.close()
    new = _claim(store, "ik-new", "exec-new")
    store.update_status(new, "SUBMITTING")

    reaped = store.reap_orphans(started_at_utc="2026-11-02T02:34:11.000Z")

    assert [r["execution_id"] for r in reaped] == [old]
    assert store.get_execution(old)["status"] == "UNKNOWN"
    assert store.get_execution(old)["error_code"] == "ORPHANED_BY_RESTART"
    assert store.get_execution(new)["status"] == "SUBMITTING"
    with store._conn() as c:
        audits = c.execute(
            "SELECT reason FROM bet_audit WHERE execution_id=?",
            (old,)).fetchall()
    assert any("reaped" in (a["reason"] or "") for a in audits)


# ── 3. balance jump needs the third read ──────────────────────────────────
def test_balance_jump_takes_three_reads_and_confirms():
    """A real deposit moves the balance far past SUSPECT_RATIO; two agreeing
    reads used to be enough for the sampler to stop, while decide() demands
    three — so a genuine jump pinned the balance to None forever."""
    page = _Page(balance_texts=["2000.00", "2000.00", "2000.00"])
    ad = _adapter(page)
    ad._last_good_balance = 500.0

    value = ad.read_balance()

    assert value == 2000.0
    assert page.balance_reads == _balance.SUSPECT_SAMPLES


def test_balance_steady_state_still_stops_at_two_reads():
    page = _Page(balance_texts=["500.00", "500.00"])
    ad = _adapter(page)
    ad._last_good_balance = 500.0
    assert ad.read_balance() == 500.0
    assert page.balance_reads == _balance.AGREE_SAMPLES


# ── 4. stop budget ────────────────────────────────────────────────────────
def test_stop_budget_outlasts_the_documented_shutdown_joins():
    """TimeoutStopSec must exceed the longest join in server.stop_pipeline
    (25 s for the snapshot scheduler) or every restart ends in SIGKILL —
    which is what orphaned a claimed 220 R bet."""
    conf = Path("/home/ubuntu/.config/systemd/user/blm-server.service.d/"
                "99-stop-timeout.conf")
    if not conf.exists():
        pytest.skip("unit drop-in not present in this checkout")
    m = re.search(r"TimeoutStopSec=(\d+)", conf.read_text())
    assert m, "TimeoutStopSec missing"
    assert int(m.group(1)) > 25


# ── 5. account identity guard ─────────────────────────────────────────────
class _IdentityAdapter:
    def __init__(self, account_id):
        self._account_id = account_id

    def read_account_id(self):
        return self._account_id


def _provider(account_id):
    p = PokerBetBrowserProvider(adapter=_IdentityAdapter(account_id))
    p._bridge = object()          # _wire() early-returns; no browser needed
    return p


def test_account_identity_guard_compares_page_to_configured(monkeypatch):
    monkeypatch.setenv(PokerBetBrowserProvider.ACCOUNT_ENV, "233997789")

    assert _provider("233997789").requires_account_identity is True
    assert account_guard.verify_account(_provider("233997789")) is None

    mismatch = account_guard.verify_account(_provider("999999999"))
    assert mismatch and "233997789" in mismatch

    # unreadable page id is a REFUSAL, never a match
    unreadable = account_guard.verify_account(_provider(None))
    assert unreadable and "unverifiable" in unreadable

    # with no configured account the guard stays off (nothing to compare)
    monkeypatch.delenv(PokerBetBrowserProvider.ACCOUNT_ENV)
    assert _provider("233997789").requires_account_identity is False
    assert account_guard.verify_account(_provider("999999999")) is None
