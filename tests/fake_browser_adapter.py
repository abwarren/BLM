"""BLM EXECUTION — FAKE BROWSER ADAPTER (test double).

A deterministic in-memory PokerBet stand-in implementing the full
SelectionResolver protocol.  Tests script its behaviour:

  * the market catalogue (lines/prices per event) can be MUTATED between
    actions — that is how the mandatory scenario is driven: create the
    selection at 164.5 @ 1.90, change the book to 166.5 @ 1.83, run,
    and assert the system resolved and selected 166.5 @ 1.83;
  * failure injection: n-th click can fail, the slip can drop a leg,
    the slip can lag N reads behind, events/markets/positions can
    vanish, the adapter can raise AdapterUnavailable;
  * every click is recorded with the line/price ON THE HANDLE at click
    time — the assertions prove WHICH selection was clicked.
"""
from __future__ import annotations

from typing import Callable, Optional

from blm_v4.execution.adapter import (
    AdapterUnavailable,
    MarketObservation,
    SelectionResolver,
)

ACCEPTED = "ACCEPTED"
SUBMITTED = "SUBMITTED"
REJECTED = "REJECTED"
FAILED = "FAILED"


class FakeBrowserAdapter(SelectionResolver):
    name = "fake_browser"

    def __init__(self):
        # catalogue: event -> {"market": {"line": float, "over": float,
        # "under": float, "suspended": bool}}
        self.catalogue: dict[str, dict] = {}
        self.slip: list[dict] = []          # betslip entries
        self.clicks: list[dict] = []        # audit of every click
        self.placements: list[dict] = []
        self.confirmation: Optional[dict] = None

        # scripted failures
        self.click_fail_on: Optional[int] = None    # 1-based click number
        self.slip_lag_reads: int = 0                # slip returns [] for
        # the next N read_betslip calls (delayed update simulation)
        self.drop_leg_on_click: Optional[int] = None  # click succeeds but
        # the leg never appears (betslip verification failure)
        self.duplicate_leg_on_click: Optional[int] = None
        self.missing_events: set[str] = set()
        self.missing_markets: set[str] = set()
        self.missing_positions: set[str] = set()    # "event|position"
        self.suspended_positions: set[str] = set()
        self.adapt_down: bool = False
        self.place_status: str = ACCEPTED
        self.place_reject_reason: Optional[str] = None
        self.on_before_click: Optional[Callable[[], None]] = None
        self._click_no = 0
        self._slip_reads = 0

    # ── helpers to script the world ────────────────────────────────────
    def set_market(self, event: str, line: float, over: float,
                   under: float) -> None:
        self.catalogue.setdefault(event, {"market": {}})
        self.catalogue[event]["market"] = {
            "line": line, "over": over, "under": under,
            "suspended": False}

    def move_market(self, event: str, line: float, over: float,
                    under: float) -> None:
        """The bookmaker CHANGES the current offer (line and/or price)."""
        self.set_market(event, line, over, under)

    def _mkt(self, event: str) -> dict:
        return self.catalogue[event]["market"]

    # ── protocol implementation ────────────────────────────────────────
    def find_event(self, event: str) -> bool:
        if self.adapt_down:
            raise AdapterUnavailable("fake adapter down")
        return (event in self.catalogue
                and event not in self.missing_events)

    def find_market(self, event: str, market: str) -> bool:
        if self.adapt_down:
            raise AdapterUnavailable("fake adapter down")
        if event not in self.catalogue:
            return False
        if f"{event}|{market}" in self.missing_markets:
            return False
        return "market" in self.catalogue[event]

    def find_position(self, event: str, market: str,
                      position: str) -> Optional[MarketObservation]:
        if self.adapt_down:
            raise AdapterUnavailable("fake adapter down")
        if f"{event}|{position}" in self.missing_positions:
            return None
        m = self._mkt(event)
        price = m.get(position.lower())
        if price is None:
            return None
        return MarketObservation(
            event=event, market=market, position=position,
            line=m["line"], price=price,
            handle={"event": event, "position": position,
                    "line": m["line"], "price": price},
            suspended=(f"{event}|{position}" in self.suspended_positions
                       or bool(m.get("suspended"))))

    def click_selection(self, obs: MarketObservation) -> bool:
        if self.adapt_down:
            raise AdapterUnavailable("fake adapter down")
        if self.on_before_click:
            self.on_before_click()
        self._click_no += 1
        h = obs.handle or {}
        entry = {
            "n": self._click_no, "event": obs.event,
            "market": obs.market, "position": obs.position,
            "line": h.get("line", obs.line),
            "price": h.get("price", obs.price),
        }
        self.clicks.append(entry)
        if self.click_fail_on is not None \
                and self._click_no == self.click_fail_on:
            return False  # click not registered
        if self.drop_leg_on_click is not None \
                and self._click_no == self.drop_leg_on_click:
            return True   # click "succeeded" but the slip never updates
        if self.duplicate_leg_on_click is not None \
                and self._click_no == self.duplicate_leg_on_click:
            self.slip.append(dict(entry))
            self.slip.append(dict(entry))
            return True
        # normal path: the leg appears in the slip with the CLICKED values
        self.slip.append(dict(entry))
        return True

    def read_betslip(self) -> list[dict]:
        if self.adapt_down:
            raise AdapterUnavailable("fake adapter down")
        self._slip_reads += 1
        if self.slip_lag_reads > 0:
            self.slip_lag_reads -= 1
            return []  # the slip has not caught up yet
        return [dict(e) for e in self.slip]

    def place_parlay(self, stake_amount: float) -> dict:
        if self.adapt_down:
            raise AdapterUnavailable("fake adapter down")
        self.placements.append({"stake": stake_amount,
                                "status": self.place_status})
        if self.place_status in (REJECTED, FAILED):
            return {"status": self.place_status,
                    "error_code": self.place_status,
                    "error_message": self.place_reject_reason
                    or "scripted rejection"}
        if self.place_status == SUBMITTED:
            return {"status": SUBMITTED, "provider_ref": None}
        return {"status": ACCEPTED,
                "provider_ref": f"fake-ref-{len(self.placements)}"}

    def read_order_confirmation(self) -> Optional[dict]:
        if self.adapt_down:
            raise AdapterUnavailable("fake adapter down")
        return self.confirmation
