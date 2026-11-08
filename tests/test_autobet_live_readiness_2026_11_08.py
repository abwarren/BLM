"""BLM AUTO-BET — LIVE BETTING READINESS (TEST branch, 2026-11-08).

Covers the directive's mandatory scenarios WITHOUT touching production,
without enabling Auto-Bet and without authorising real-money submission:

  Phase Two      correct game-total line selection (never closest-to-1.85,
                 never a substituted line, odds never confused with lines)
  Phase Three    canonical rung validation on the autonomous path
  Phase Four     one-bet-per-game, atomic claims, concurrency, UNKNOWN
  Phase Five     submission-time revalidation (fresh payload)
  Phase Seven    browser transport: identity, market, selection, betslip,
                 receipt; fail-closed on every ambiguity
  Phase Nine     persistent audit + duplicate/reconciliation behaviour
  Phase Twelve   end-to-end simulated session + failure variations

All bookmakers are fakes; all databases are tmp_path; the only real
provider constructed is PokerBetProvider with a fake adapter and the
BETTING_BROWSER_SUBMIT gate explicitly asserted in both states.
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# the sibling test double (fake Playwright page) is imported by name
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from blm_v4.betting.account_guard import verify_account
from blm_v4.betting.config import BettingConfig
from blm_v4.betting.executor import evaluate, execute
from blm_v4.betting.fake_provider import (
    FakeBookmakerContractProvider,
    TEST_BOOKMAKER_ACCOUNT_ID,
)
from blm_v4.betting.provider import (
    PokerBetBrowserTransport,
    PokerBetProvider,
    ProviderUnavailable,
)
from blm_v4.betting.store import BettingStore
from blm_v4.betting.worker import BettingWorker
from blm_v4.execution.adapter import MarketObservation
from blm_v4.execution.pokerbet.dom import (
    R_AMBIGUOUS_LINES,
    R_LINE_NOT_AVAILABLE,
    R_MARKET_SUSPENDED,
    R_MISSING_ODDS,
    parse_total_offers,
    select_total_offer,
)


ACCOUNT_ID = 900000001
GID = "G-READINESS-1"
EVENT = "Team A vs Team B"


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
            ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def game(*, game_id: str = GID, trigger: float = 164.5,
         current: float = 164.5, ladder=(161.5, 162.5, 163.5, 164.5,
                                        165.5, 166.5),
         odds: float = 1.90, checkpoint: int = 75,
         age_s: float = 2.0, market_status: str = "LIVE") -> dict:
    return {
        "game_id": game_id,
        "live": True,
        "live_reason": None,
        "market": {"total_line": current, "market_status": market_status,
                   "under_odds": odds, "observed_lines": list(ladder)},
        "projector": {"required_pts_per_min": 5.0, "progress_pct": 80.0,
                      "actual_pts_per_min": 4.0,
                      "captured_at": _iso(age_s)},
        "under_alert_eligibility": {"eligible": True, "reason": "market_live"},
        "under_alert": {"active": True, "checkpoint": checkpoint,
                        "trigger_line": trigger},
        "under_alert_fingerprint": {"fingerprint_count": 1,
                                    "fingerprints_fired": ["C3"]},
    }


@pytest.fixture
def cfg():
    return BettingConfig(
        dry_run=True, max_stake_per_bet=10.0, max_bets_per_day=5,
        max_daily_exposure=50.0, stake_units=1.0, alert_max_age_s=90.0)


@pytest.fixture
def store(tmp_path):
    s = BettingStore(str(tmp_path / "blm_betting_readiness.db"))
    s.set_config("auto_betting_enabled", "true")
    s.set_unit_price(2.0)
    return s


# ══════════════════════════════════════════════════════════════════════
# PHASE TWO — market / line selection
# ══════════════════════════════════════════════════════════════════════

MULTI_LINE = "\n".join([
    "Total Points 161.5 Over 2.05 Under 1.75",
    "Total Points 163.5 Over 1.95 Under 1.85",
    "Total Points 164.5 Over 1.90 Under 1.90",
    "Total Points 166.5 Over 1.80 Under 2.00",
])


def test_every_line_is_parsed_independently():
    offers = parse_total_offers(MULTI_LINE)
    assert [o.line for o in offers] == [161.5, 163.5, 164.5, 166.5]
    assert [(o.over, o.under) for o in offers] == [
        (2.05, 1.75), (1.95, 1.85), (1.90, 1.90), (1.80, 2.00)]


def test_trigger_line_wins_even_when_another_line_is_nearer_185():
    offers = parse_total_offers(MULTI_LINE)
    # 163.5 carries 1.85 (the old "closest to 1.85" target); the bet must
    # still be the ORIGINAL trigger line 164.5.
    offer, reason = select_total_offer(offers, 164.5, "UNDER")
    assert reason is None and offer is not None
    assert (offer.line, offer.price("UNDER")) == (164.5, 1.90)


def test_missing_trigger_line_is_refused_not_substituted():
    offers = parse_total_offers(MULTI_LINE)
    offer, reason = select_total_offer(offers, 162.5, "UNDER")
    assert offer is None and reason == R_LINE_NOT_AVAILABLE


def test_odds_are_never_mistaken_for_the_line():
    offers = parse_total_offers(MULTI_LINE)
    offer, _ = select_total_offer(offers, 164.5, "UNDER")
    assert offer.line == 164.5
    assert offer.under == 1.90            # 1.90 is the PRICE, not a line
    assert 1.90 not in [o.line for o in offers]


def test_suspended_market_is_refused():
    offers = parse_total_offers(MULTI_LINE)
    offer, reason = select_total_offer(offers, 164.5, "UNDER",
                                       suspended=True)
    assert offer is None and reason == R_MARKET_SUSPENDED


def test_team_total_rows_are_not_game_totals():
    text = ("Team Total Home 164.5 Over 1.90 Under 1.90\n"
            "Total Points 164.5 Over 1.90 Under 1.90")
    offers = parse_total_offers(text)
    assert [o.line for o in offers] == [164.5]
    assert len(offers) == 1


def test_duplicate_line_structure_is_ambiguous():
    text = ("Total Points 164.5 Over 1.90 Under 1.90\n"
            "Total Points 164.5 Over 1.95 Under 1.85")
    offers = parse_total_offers(text)
    assert offers == []                    # duplicate line dropped
    offer, reason = select_total_offer(offers, 164.5, "UNDER")
    assert offer is None and reason == R_LINE_NOT_AVAILABLE


def test_missing_odds_on_the_selected_line_is_refused():
    offers = parse_total_offers("Total Points 164.5 Under 1.90")
    # a one-sided row with the UNDER price present is valid for UNDER ...
    offer, reason = select_total_offer(offers, 164.5, "UNDER",
                                       tolerance=0.0)
    assert reason is None and offer is not None and offer.under == 1.90
    # ... and MUST be refused for OVER (no price was rendered)
    offer_over, reason_over = select_total_offer(offers, 164.5, "OVER")
    assert offer_over is None and reason_over == R_MISSING_ODDS


def test_ambiguous_unbound_multi_line_market_never_guesses():
    """The DOM adapter must refuse several selectable lines when no
    trigger line is bound."""
    from blm_v4.execution.pokerbet.dom import PokerBetDomAdapter
    from test_pokerbet_live_adapter import FakeBrowser, FakePage

    page = FakePage(market_text=MULTI_LINE)
    browser = FakeBrowser(page)
    adapter = PokerBetDomAdapter(browser)
    adapter.register_game(EVENT, game_id="30840226",
                          home_team="Team A", away_team="Team B")
    assert adapter.find_event(EVENT) is True
    assert adapter.find_position(EVENT, "TOTAL", "UNDER") is None
    adapter.set_required_line(164.5)
    obs = adapter.find_position(EVENT, "TOTAL", "UNDER")
    assert obs is not None and (obs.line, obs.price) == (164.5, 1.90)


# ══════════════════════════════════════════════════════════════════════
# PHASE THREE — canonical rung validation is ON the autonomous path
# ══════════════════════════════════════════════════════════════════════

def test_rung_one_down_is_allowed(cfg, store):
    res = evaluate(game(current=163.5, ladder=(161.5, 162.5, 163.5,
                                               164.5, 165.5)),
                   cfg=cfg, store=store, enabled=True, unit_price=2.0,
                   stats=store.today_stats())
    assert res["decision"] == "WOULD_BET", res
    assert res["candidate"]["rungs_moved"] == -1.0
    assert res["candidate"]["triggered_line"] == 164.5


def test_rung_two_down_is_rejected_before_any_claim(cfg, store):
    res = evaluate(game(current=162.5), cfg=cfg, store=store, enabled=True,
                   unit_price=2.0, stats=store.today_stats())
    assert res["decision"] == "NO_BET"
    assert res["reason"].startswith("rung_rejected:")
    assert store.recent() == []            # nothing claimed


def test_missing_ladder_is_rejected(cfg, store):
    g = game()
    g["market"]["observed_lines"] = None
    res = evaluate(g, cfg=cfg, store=store, enabled=True, unit_price=2.0,
                   stats=store.today_stats())
    assert res["decision"] == "NO_BET"
    assert res["reason"].startswith("rung_rejected:")


def test_trigger_line_is_never_replaced_by_the_current_total(cfg, store):
    res = evaluate(game(current=163.5), cfg=cfg, store=store, enabled=True,
                   unit_price=2.0, stats=store.today_stats())
    cand = res["candidate"]
    assert cand["triggered_line"] == 164.5
    assert cand["current_line"] == 163.5
    assert cand["price"] == 1.90           # odds, not the line


# ══════════════════════════════════════════════════════════════════════
# PHASE FOUR / FIVE — execution integrity + submission revalidation
# ══════════════════════════════════════════════════════════════════════

def test_one_bet_per_game_blocks_a_different_checkpoint(cfg, store):
    first = evaluate(game(checkpoint=75), cfg=cfg, store=store, enabled=True,
                     unit_price=2.0, stats=store.today_stats())
    assert first["decision"] == "WOULD_BET"
    second = evaluate(game(checkpoint=50), cfg=cfg, store=store, enabled=True,
                      unit_price=2.0, stats=store.today_stats())
    assert second["decision"] == "NO_BET"
    assert second["reason"] == "duplicate_execution"
    assert store.today_stats()["bets"] == 1


def test_concurrent_alerts_claim_exactly_one(tmp_path):
    store = BettingStore(str(tmp_path / "concurrent.db"))
    store.set_config("auto_betting_enabled", "true")
    store.set_unit_price(2.0)
    cfg = BettingConfig(dry_run=True, max_stake_per_bet=10.0,
                        max_bets_per_day=10, max_daily_exposure=100.0)
    results: list[str] = []
    barrier = threading.Barrier(8)

    def worker(checkpoint):
        barrier.wait()
        g = game(checkpoint=checkpoint)
        res = evaluate(g, cfg=cfg, store=store, enabled=True,
                       unit_price=2.0, stats=store.today_stats())
        results.append(res["decision"])

    threads = [threading.Thread(target=worker, args=(c,))
               for c in (25, 50, 75, 100, 25, 50, 75, 100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("WOULD_BET") == 1, results
    assert store.today_stats()["bets"] == 1
    assert len(store.recent()) == 1


def test_submission_revalidation_rejects_a_stale_candidate(cfg, store):
    res = evaluate(game(), cfg=cfg, store=store, enabled=True,
                   unit_price=2.0, stats=store.today_stats())
    assert res["decision"] == "WOULD_BET"
    stale = game(age_s=600.0)              # same claim, now 10 minutes old
    out = execute(res["candidate"], cfg=cfg, store=store,
                  provider=FakeBookmakerContractProvider(), game=stale)
    assert out["status"] == "BLOCKED"
    assert out["error_code"] == "ALERT_STALE_AT_SUBMISSION"
    rec = store.get_execution(res["candidate"]["execution_id"])
    assert rec["status"] == "BLOCKED"


def test_submission_revalidation_rejects_changed_odds(cfg, store):
    res = evaluate(game(), cfg=cfg, store=store, enabled=True,
                   unit_price=2.0, stats=store.today_stats())
    moved = game(odds=2.10)
    out = execute(res["candidate"], cfg=cfg, store=store,
                  provider=FakeBookmakerContractProvider(), game=moved)
    assert out["status"] == "BLOCKED"
    assert out["error_code"] == "ODDS_CHANGED"


def test_submission_revalidation_rejects_a_line_change(cfg, store):
    res = evaluate(game(), cfg=cfg, store=store, enabled=True,
                   unit_price=2.0, stats=store.today_stats())
    moved = game(current=165.5)
    out = execute(res["candidate"], cfg=cfg, store=store,
                  provider=FakeBookmakerContractProvider(), game=moved)
    assert out["status"] == "BLOCKED"
    assert out["error_code"] == "LINE_CHANGED"


def test_submission_revalidation_rejects_a_rung_breach(cfg, store):
    res = evaluate(game(), cfg=cfg, store=store, enabled=True,
                   unit_price=2.0, stats=store.today_stats())
    moved = game(current=162.5)
    out = execute(res["candidate"], cfg=cfg, store=store,
                  provider=FakeBookmakerContractProvider(), game=moved)
    assert out["status"] == "BLOCKED"
    assert out["error_code"] == "RUNG_REJECTED"


def test_kill_switch_flipped_between_claim_and_submit_blocks(cfg, store):
    res = evaluate(game(), cfg=cfg, store=store, enabled=True,
                   unit_price=2.0, stats=store.today_stats())
    store.set_config("auto_betting_enabled", "false")
    out = execute(res["candidate"], cfg=cfg, store=store,
                  provider=FakeBookmakerContractProvider())
    assert out["status"] == "BLOCKED"
    assert out["error_code"] == "GLOBAL_KILL_SWITCH"


def test_database_failure_is_not_permission_to_bet(monkeypatch, cfg, store):
    import sqlite3
    calls = {"n": 0}
    real_claim = store.claim

    def broken_claim(rec):
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "claim", broken_claim)
    provider = FakeBookmakerContractProvider()
    with pytest.raises(sqlite3.OperationalError):
        evaluate(game(), cfg=cfg, store=store, enabled=True,
                 unit_price=2.0, stats=store.today_stats())
    assert calls["n"] == 1
    assert provider.submission_count() == 0
    assert store.recent() == []


def test_unknown_submission_is_never_retried(cfg, store):
    live = BettingConfig(dry_run=False, max_stake_per_bet=10.0,
                         max_bets_per_day=5, max_daily_exposure=50.0,
                         stake_units=1.0)
    provider = FakeBookmakerContractProvider()
    provider.set_scenario("timeout")
    first = evaluate(game(), cfg=live, store=store, enabled=True,
                     unit_price=2.0, stats=store.today_stats())
    out = execute(first["candidate"], cfg=live, store=store,
                  provider=provider, game=game())
    assert out["status"] == "UNKNOWN"
    before = provider.submission_count()
    second = evaluate(game(checkpoint=50), cfg=live, store=store, enabled=True,
                      unit_price=2.0, stats=store.today_stats())
    assert second["decision"] == "NO_BET"
    assert second["reason"] == "duplicate_execution"
    assert provider.submission_count() == before      # no retry


# ══════════════════════════════════════════════════════════════════════
# PHASE SEVEN — PokerBet browser transport
# ══════════════════════════════════════════════════════════════════════

class FakeDomAdapter:
    """The exact surface PokerBetBrowserTransport uses."""

    def __init__(self):
        self.required_line = None
        self.line = 164.5
        self.price = 1.90
        self.clicked = 0
        self.placed = 0
        self.game_found = True
        self.market_found = True
        self.unique = True
        self.slip_line = 164.5
        self.slip_price = 1.90
        self.slip_count = 1
        self.receipt = "REF123456"
        self.place_status = "ACCEPTED"
        self.clickable = True

    def set_required_line(self, line):
        self.required_line = line

    def find_event(self, event):
        return self.game_found

    def find_market(self, event, market):
        return self.market_found

    def find_position(self, event, market, position):
        if not self.unique:
            return None
        return MarketObservation(
            event=event, market=market, position=position.upper(),
            line=self.line, price=self.price,
            handle={"game_id": GID, "clickable": object()}, suspended=False)

    def click_selection(self, obs):
        self.clicked += 1
        return self.clickable

    def read_betslip(self):
        return [{"event": EVENT, "market": "TOTAL", "position": "UNDER",
                 "line": self.slip_line, "price": self.slip_price,
                 "game_id": GID}] * self.slip_count

    def place_parlay(self, stake):
        self.placed += 1
        return ({"status": self.place_status,
                 "provider_ref": self.receipt}
                if self.place_status == "ACCEPTED"
                else {"status": self.place_status,
                      "error_code": "SUBMIT_FAILED"})


def _transport(adapter=None, *, event=EVENT):
    adapter = adapter or FakeDomAdapter()
    t = PokerBetBrowserTransport(adapter, {GID: event},
                                 authenticated_account_id=ACCOUNT_ID,
                                 configured_account_id=ACCOUNT_ID)
    return t, adapter


def _submit(transport, **over):
    kw = dict(execution_id="bet-readiness", game_id=GID,
              alert_id=f"{GID}|75", selection="UNDER", price=1.90,
              stake_amount=2.0, market="TOTAL", line=164.5)
    kw.update(over)
    return transport.submit(**kw)


def test_browser_submission_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BETTING_BROWSER_SUBMIT", raising=False)
    transport, adapter = _transport()
    out = _submit(transport)
    assert out["status"] == "FAILED"
    assert out["error_code"] == "TRANSPORT_UNAVAILABLE"
    assert adapter.clicked == 0 and adapter.placed == 0


def test_browser_happy_path_returns_the_bookmakers_receipt(monkeypatch):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "true")
    transport, adapter = _transport()
    out = _submit(transport)
    assert out == {"status": "ACCEPTED", "provider_ref": "REF123456"}
    assert adapter.required_line == 164.5
    assert adapter.clicked == 1 and adapter.placed == 1


def test_browser_refuses_an_unregistered_game(monkeypatch):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "true")
    transport, adapter = _transport()
    out = _submit(transport, game_id="G-UNKNOWN")
    assert out["status"] == "FAILED"
    assert out["error_code"] == "GAME_NOT_REGISTERED"
    assert adapter.placed == 0


def test_browser_refuses_a_different_resolved_line(monkeypatch):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "true")
    transport, adapter = _transport()
    adapter.line = 163.5                    # book moved to another line
    out = _submit(transport)
    assert out["status"] == "FAILED"
    assert out["error_code"] == "LINE_MISMATCH"
    assert adapter.clicked == 0 and adapter.placed == 0


def test_browser_refuses_changed_odds(monkeypatch):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "true")
    transport, adapter = _transport()
    adapter.price = 1.80
    out = _submit(transport)
    assert out["status"] == "FAILED"
    assert out["error_code"] == "ODDS_MISMATCH"
    assert adapter.clicked == 0


def test_browser_refuses_missing_market(monkeypatch):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "true")
    transport, adapter = _transport()
    adapter.market_found = False
    out = _submit(transport)
    assert out["status"] == "FAILED"
    assert out["error_code"] == "MARKET_NOT_FOUND"
    assert adapter.clicked == 0


def test_browser_refuses_ambiguous_selection(monkeypatch):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "true")
    transport, adapter = _transport()
    adapter.unique = False
    out = _submit(transport)
    assert out["status"] == "FAILED"
    assert out["error_code"] == "SELECTION_NOT_AVAILABLE"
    assert adapter.clicked == 0


def test_browser_refuses_unclickable_outcome(monkeypatch):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "true")
    transport, adapter = _transport()
    adapter.clickable = False
    out = _submit(transport)
    assert out["status"] == "FAILED"
    assert out["error_code"] == "SELECTION_NOT_CLICKED"
    assert adapter.placed == 0


def test_browser_refuses_betslip_mismatch(monkeypatch):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "true")
    transport, adapter = _transport()
    adapter.slip_line = 166.5               # the slip shows another line
    out = _submit(transport)
    assert out["status"] == "FAILED"
    assert out["error_code"] == "BETSLIP_VERIFICATION_FAILED"
    assert adapter.placed == 0


def test_browser_refuses_duplicate_betslip_entries(monkeypatch):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "true")
    transport, adapter = _transport()
    adapter.slip_count = 2
    out = _submit(transport)
    assert out["status"] == "FAILED"
    assert out["error_code"] == "BETSLIP_VERIFICATION_FAILED"
    assert adapter.placed == 0


def test_browser_acceptance_without_receipt_is_only_submitted(monkeypatch):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "true")
    transport, adapter = _transport()
    adapter.receipt = None
    out = _submit(transport)
    assert out["status"] == "SUBMITTED"
    assert out["provider_ref"] is None


def test_browser_transport_still_enforces_account_identity(monkeypatch):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "true")
    adapter = FakeDomAdapter()
    transport = PokerBetBrowserTransport(
        adapter, {GID: EVENT},
        authenticated_account_id=111, configured_account_id=222)
    out = _submit(transport)
    assert out["status"] == "FAILED"
    assert out["error_code"] == "ACCOUNT_MISMATCH"
    assert adapter.placed == 0


# ══════════════════════════════════════════════════════════════════════
# PHASE TWELVE — end-to-end simulated session
# ══════════════════════════════════════════════════════════════════════

def _live_provider(monkeypatch, adapter=None):
    monkeypatch.setenv("BETTING_BROWSER_SUBMIT", "true")
    transport, adapter = _transport(adapter)
    provider = PokerBetProvider(configured_account_id=ACCOUNT_ID)
    provider.set_authenticated_account(ACCOUNT_ID)
    provider.set_transport(transport)
    return provider, adapter


def test_end_to_end_simulated_session_accepts_once_and_rejects_duplicate(
        monkeypatch, tmp_path):
    store = BettingStore(str(tmp_path / "e2e.db"))
    store.set_config("auto_betting_enabled", "true")
    store.set_unit_price(2.0)
    live = BettingConfig(dry_run=False, max_stake_per_bet=10.0,
                         max_bets_per_day=5, max_daily_exposure=50.0,
                         stake_units=1.0,
                         expected_account_id=ACCOUNT_ID)
    provider, adapter = _live_provider(monkeypatch)

    payload = game()
    res = evaluate(payload, cfg=live, store=store, enabled=True,
                   unit_price=2.0, stats=store.today_stats())
    assert res["decision"] == "EXECUTE"
    cand = res["candidate"]
    assert cand["triggered_line"] == 164.5 and cand["price"] == 1.90

    out = execute(cand, cfg=live, store=store, provider=provider,
                  game=payload)
    assert out["status"] == "ACCEPTED", out
    assert out["provider_ref"] == "REF123456"
    assert adapter.clicked == 1 and adapter.placed == 1

    rec = store.get_execution(cand["execution_id"])
    assert (rec["status"], rec["provider_ref"]) == ("ACCEPTED", "REF123456")
    assert rec["triggered_line"] == 164.5
    assert rec["price"] == 1.90
    assert rec["stake_amount"] == 2.0
    assert rec["unit_price"] == 2.0
    assert rec["submitted_at_utc"] is not None

    # duplicate alert (same game, different checkpoint) is refused
    dup = evaluate(game(checkpoint=50), cfg=live, store=store, enabled=True,
                   unit_price=2.0, stats=store.today_stats())
    assert dup["decision"] == "NO_BET"
    assert dup["reason"] == "duplicate_execution"
    assert adapter.placed == 1


@pytest.mark.parametrize("scenario,expected", [
    ("browser_disconnect", "BROWSER_UNAVAILABLE"),
    ("market_suspended", "MARKET_NOT_FOUND"),
    ("changed_odds", "ODDS_MISMATCH"),
    ("missing_market", "MARKET_NOT_FOUND"),
    ("betslip_mismatch", "BETSLIP_VERIFICATION_FAILED"),
])
def test_end_to_end_failure_variations_never_place(
        monkeypatch, tmp_path, scenario, expected):
    store = BettingStore(str(tmp_path / f"e2e_{scenario}.db"))
    store.set_config("auto_betting_enabled", "true")
    store.set_unit_price(2.0)
    live = BettingConfig(dry_run=False, max_stake_per_bet=10.0,
                         max_bets_per_day=5, max_daily_exposure=50.0,
                         stake_units=1.0, expected_account_id=ACCOUNT_ID)
    adapter = FakeDomAdapter()
    provider, adapter = _live_provider(monkeypatch, adapter)
    if scenario == "browser_disconnect":
        def boom(event):
            raise ProviderUnavailable("browser disconnected")
        adapter.find_event = boom
    elif scenario == "market_suspended":
        adapter.market_found = False
    elif scenario == "changed_odds":
        adapter.price = 1.75
    elif scenario == "missing_market":
        adapter.market_found = False
    elif scenario == "betslip_mismatch":
        adapter.slip_line = 162.5

    payload = game()
    res = evaluate(payload, cfg=live, store=store, enabled=True,
                   unit_price=2.0, stats=store.today_stats())
    out = execute(res["candidate"], cfg=live, store=store, provider=provider,
                  game=payload)
    assert out["status"] in ("FAILED", "BLOCKED"), out
    assert out.get("error_code") == expected, out
    assert adapter.placed == 0
    rec = store.get_execution(res["candidate"]["execution_id"])
    assert rec["status"] in ("FAILED", "BLOCKED")


def test_end_to_end_unknown_submission_enters_reconciliation(
        monkeypatch, tmp_path):
    store = BettingStore(str(tmp_path / "e2e_unknown.db"))
    store.set_config("auto_betting_enabled", "true")
    store.set_unit_price(2.0)
    live = BettingConfig(dry_run=False, max_stake_per_bet=10.0,
                         max_bets_per_day=5, max_daily_exposure=50.0,
                         stake_units=1.0, expected_account_id=ACCOUNT_ID)
    provider, adapter = _live_provider(monkeypatch)
    adapter.place_status = "UNKNOWN"
    payload = game()
    res = evaluate(payload, cfg=live, store=store, enabled=True,
                   unit_price=2.0, stats=store.today_stats())
    out = execute(res["candidate"], cfg=live, store=store, provider=provider,
                  game=payload)
    assert out["status"] == "UNKNOWN"
    rec = store.get_execution(res["candidate"]["execution_id"])
    assert rec["status"] == "UNKNOWN"
    # UNKNOWN exposure counts and the game slot stays held
    assert store.today_stats()["amount"] >= rec["stake_amount"]
    assert store.has_blocking_execution(GID) is not None


def test_end_to_end_kill_switch_after_claim_blocks_before_browser(
        monkeypatch, tmp_path):
    store = BettingStore(str(tmp_path / "e2e_kill.db"))
    store.set_config("auto_betting_enabled", "true")
    store.set_unit_price(2.0)
    live = BettingConfig(dry_run=False, max_stake_per_bet=10.0,
                         max_bets_per_day=5, max_daily_exposure=50.0,
                         stake_units=1.0, expected_account_id=ACCOUNT_ID)
    provider, adapter = _live_provider(monkeypatch)
    payload = game()
    res = evaluate(payload, cfg=live, store=store, enabled=True,
                   unit_price=2.0, stats=store.today_stats())
    store.set_config("auto_betting_enabled", "false")
    out = execute(res["candidate"], cfg=live, store=store, provider=provider,
                  game=payload)
    assert out["status"] == "BLOCKED"
    assert adapter.clicked == 0 and adapter.placed == 0


def test_worker_restart_does_not_resubmit_an_accepted_game(
        monkeypatch, tmp_path):
    store = BettingStore(str(tmp_path / "restart.db"))
    store.set_config("auto_betting_enabled", "true")
    store.set_unit_price(2.0)
    cfg = BettingConfig(dry_run=True, max_stake_per_bet=10.0,
                        max_bets_per_day=5, max_daily_exposure=50.0,
                        stake_units=1.0)
    first = BettingWorker(cfg, store, lambda: [game()], poll_interval_s=3600)
    assert first.poll_once()["would_bet"] == 1
    # a NEW worker instance (process restart) over the same ledger
    second = BettingWorker(cfg, store, lambda: [game()], poll_interval_s=3600)
    summary = second.poll_once()
    assert summary["would_bet"] == 0
    assert store.today_stats()["bets"] == 1
    assert len(store.recent()) == 1


def test_ledger_records_the_complete_audit_trail(cfg, store):
    res = evaluate(game(current=163.5), cfg=cfg, store=store, enabled=True,
                   unit_price=2.0, stats=store.today_stats())
    execute(res["candidate"], cfg=cfg, store=store,
            provider=FakeBookmakerContractProvider(), game=game(current=163.5))
    rec = store.get_execution(res["candidate"]["execution_id"])
    for column in ("execution_id", "game_id", "alert_id", "checkpoint",
                   "triggered_line", "price", "unit_price", "stake_units",
                   "stake_amount", "requested_at_utc", "submitted_at_utc",
                   "provider_ref", "status", "day_utc"):
        assert column in rec, column
    assert rec["triggered_line"] == 164.5
    audit = store.audit_recent(execution_id=rec["execution_id"])
    assert audit, "execution must have an audit trail"


def _seed_unknown(store, game_id=GID) -> str:
    claimed, rec = store.claim({
        "execution_id": "bet-unknown-1", "idempotency_key": "ik-unknown-1",
        "game_id": game_id, "alert_id": f"{game_id}|75", "checkpoint": "75",
        "market": "TOTAL", "selection": "UNDER", "triggered_line": 164.5,
        "price": 1.90, "unit_price": 2.0, "stake_units": 1.0,
        "stake_amount": 2.0, "status": "UNKNOWN"})
    assert claimed
    store.update_status("bet-unknown-1", "UNKNOWN",
                        error_code="TRANSACTION_TIMEOUT")
    return "bet-unknown-1"


def test_reconciliation_confirms_only_with_a_bookmaker_order(store):
    from blm_v4.betting.reconcile import BettingReconciler
    _seed_unknown(store)

    class Provider:
        submissions = 0

        def find_order_by_idempotency(self, key):
            return {"provider_ref": "BK-REF-77", "idempotency_key": key}

    p = Provider()
    out = BettingReconciler(store, p).reconcile_once()
    assert out["confirmed"] == 1
    rec = store.get_execution("bet-unknown-1")
    assert rec["status"] == "ACCEPTED"
    assert rec["provider_ref"] == "BK-REF-77"
    assert rec["reconciliation_state"] == "CONFIRMED"
    assert p.submissions == 0                    # never resubmitted
    assert store.has_blocking_execution(GID) is not None


def test_reconciliation_never_assumes_a_missing_order_means_no_bet(store):
    from blm_v4.betting.reconcile import BettingReconciler
    _seed_unknown(store)

    class Provider:
        def find_order_by_idempotency(self, key):
            return None

    out = BettingReconciler(store, Provider()).reconcile_once()
    assert out["unresolved"] == 1
    rec = store.get_execution("bet-unknown-1")
    assert rec["status"] == "UNKNOWN"        # exposure keeps counting
    assert rec["reconciliation_state"] == "NO_ORDER_FOUND"
    assert store.today_stats()["amount"] >= 2.0


def test_reconciliation_without_a_lookup_leaves_the_record_unresolved(store):
    from blm_v4.betting.reconcile import BettingReconciler
    _seed_unknown(store)

    class Provider:
        pass

    out = BettingReconciler(store, Provider()).reconcile_once()
    assert out["lookup_available"] is False and out["unresolved"] == 1
    assert store.get_execution("bet-unknown-1")["status"] == "UNKNOWN"


def test_guard_accepts_matching_integer_account_and_blocks_mismatch():
    class P:
        requires_account_identity = True
        authenticated_account_id = ACCOUNT_ID
        configured_account_id = ACCOUNT_ID

    assert verify_account(P()) is None
    P.configured_account_id = ACCOUNT_ID + 1
    assert verify_account(P())
