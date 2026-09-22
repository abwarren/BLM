"""BLM EXECUTION — MARKET OBSERVATIONS + ADAPTER PROTOCOL.

A *market observation* is one snapshot of what the bookmaker CURRENTLY
offers for a selection identity (event + market + position): the current
line, the current price, and the element handle that must be clicked to
select it.  Observations are captured fresh at execution time — they are
never persisted as the selection's identity.

The adapter protocol is the ONLY surface the generic engine knows.
Bookmaker-specific DOM logic lives entirely behind it, so the matrix
engine stays independent of bookmaker DOM changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class MarketObservation:
    """The CURRENT bookmaker offer for one selection identity.

    ``line``/``price`` are read at execution time, immediately before a
    click.  ``handle`` is the clickable element (opaque to the engine —
    only the adapter ever touches it).
    """

    event: str
    market: str
    position: str
    line: Optional[float]
    price: Optional[float]
    handle: object = None
    suspended: bool = False


class AdapterUnavailable(Exception):
    """The bookmaker/browser cannot be reached at all (CDP down, tab
    closed).  Non-recoverable at the leg level → BROWSER_DISCONNECTED."""


class SelectionResolver(ABC):
    """The bookmaker adapter contract (directive §12 / §15).

    The generic engine calls these; the PokerBet adapter implements
    them against the live DOM.  Every call is a FRESH read of the
    current page state — nothing is cached across retries.
    """

    name: str = "abstract"

    @abstractmethod
    def find_event(self, event: str) -> bool:
        """Locate the event in the bookmaker UI.  False → EVENT_NOT_FOUND
        (terminal for the job — never guess or force a wrong event)."""

    @abstractmethod
    def find_market(self, event: str, market: str) -> bool:
        """Locate the market within the event.  False → MARKET_NOT_FOUND."""

    @abstractmethod
    def find_position(self, event: str, market: str,
                      position: str) -> Optional[MarketObservation]:
        """Resolve the CURRENT selection for (event, market, position).

        Returns None only when the position genuinely does not exist
        right now (POSITION_NOT_FOUND — recoverable: the engine
        re-resolves after a delay).  The returned line/price MUST be
        read from the live DOM, never from any stored value."""

    @abstractmethod
    def click_selection(self, obs: MarketObservation) -> bool:
        """Click the CURRENT selection element.  Returns whether the
        click was dispatched — NEVER whether the betslip updated
        (that is the verifier's job).  False → CLICK_NOT_REGISTERED."""

    @abstractmethod
    def read_betslip(self) -> list[dict]:
        """Parse the CURRENT betslip into entries:
        ``{event, market, position, line, price}``.  Empty list on any
        parse failure — the verifier treats an unreadable slip as
        BETSLIP_NOT_UPDATED, never as success."""

    @abstractmethod
    def place_parlay(self, stake_amount: float) -> dict:
        """Submit the parlay.  Returns ``{"status": ...}`` with status in
        SUBMITTED / ACCEPTED / REJECTED / FAILED / UNKNOWN — an honest
        answer, never an assumption that a click succeeded.  In
        non-LIVE modes the implementation must refuse to call this."""

    @abstractmethod
    def read_order_confirmation(self) -> Optional[dict]:
        """After placement, look for the bookmaker's own confirmation
        (receipt/reference).  None until the bookmaker actually
        accepted the order."""
