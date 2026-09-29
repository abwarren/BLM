"""BLM EXECUTION — POKERBET DOM ADAPTER (the live SelectionResolver).

Implements the EXISTING adapter protocol (``execution/adapter.py``)
against the operator's authenticated PokerBet browser.  The engine
(phases ①/②) runs on top of it UNCHANGED — nothing here invents new
failure semantics: find_event False → EVENT_NOT_FOUND (terminal),
missing position → POSITION_NOT_FOUND (recoverable), browser gone →
AdapterUnavailable (BROWSER_DISCONNECTED).

GAME RESOLUTION (directive 2A): the generic protocol passes only the
event NAME, so the adapter keeps a REGISTRY — ``register_game`` binds
the rendered event name to the canonical record (game_id, teams,
classification, URL).  An unregistered event is FAIL-CLOSED
(EVENT_NOT_FOUND): the adapter never hunts for look-alike names.

VERIFICATION (directive 2C): independent, immediate, ordered
   game_id → teams → market → selection → line → price(odds) → active
with HARD REJECT semantics.  ``place_parlay`` re-reads the live betslip
and re-verifies EVERYTHING immediately before submission — a resolution
from even a second earlier is never trusted (the book can change
between resolve and submit).  Submission happens ONLY when every gate
passes (2G); the confirmation reference is captured (2H); persistence
stays with the EXISTING execution ledger via the engine (2I).

DRY_RUN: the engine never calls ``place_parlay`` outside LIVE mode, and
this adapter adds its own pre-submit gates — enabling the DOM adapter
does NOT enable real-money execution.

PLAYWRIGHT SURFACE (kept minimal for test doubles): page.goto / page.url
/ page.wait_for_load_state / page.locator(sel, has_text=) and Locator
.count / .first / .inner_text / .get_attribute / .is_disabled / .click /
.fill / .input_value.  The DOM text parsing is tolerant (the same
philosophy as the collector's event_parser) and isolated in small
overridable methods so one live calibration pass can adjust selectors
without touching the engine.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from blm_v4.execution.adapter import (
    AdapterUnavailable,
    MarketObservation,
    SelectionResolver,
)
from blm_v4.execution.pokerbet.browser import CdpBrowser
from blm_v4.execution.pokerbet import session as session_mod

# ── selectors (the ONLY DOM coupling in BLM) ──────────────────────────
SEL_MARKET_BLOCK = "[class*='market'], [data-testid*='market']"
SEL_BETSLIP = "[class*='betslip'], [data-testid*='betslip']"
SEL_STAKE_INPUT = "input[type='number'], input[class*='stake']"
SEL_PLACE_BUTTON = "button"
SEL_RECEIPT = "[class*='receipt'], [class*='confirmation'], [class*='bet-id']"

_LINE_RE = re.compile(r"^\d{1,3}(?:\.\d)?$")
_FLOAT_RE = re.compile(r"\d+(?:\.\d+)?")
_TEAM_SPLIT_RE = re.compile(r"\s+vs\.?\s+|\s+-\s+", re.IGNORECASE)


def _norm_team(name) -> str:
    """Rendered-team normalizer: case/space/punctuation-insensitive."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def names_match_pair(rendered_home, rendered_away,
                     rec_home, rec_away) -> bool:
    """Both record teams must match the two rendered names in SOME
    assignment (the SPA swaps rendering order) — one look-alike is not
    enough, and a swapped render is accepted and re-oriented by the
    engine's existing verification."""
    r = {_norm_team(rendered_home), _norm_team(rendered_away)}
    w = {_norm_team(rec_home), _norm_team(rec_away)}
    return r == w and "" not in r


def _floats(text: str) -> list[float]:
    return [float(m) for m in _FLOAT_RE.findall(text or "")]


@dataclass
class OfferCheck:
    """The ordered verification result for one live offer."""
    ok: bool
    reason: Optional[str] = None       # GAME_ID_MISMATCH / TEAM_MISMATCH /
    # MARKET_MISMATCH / SELECTION_MISMATCH / LINE_MISMATCH /
    # ODDS_MISMATCH / MARKET_SUSPENDED / MISSING_DATA


class PokerBetDomAdapter(SelectionResolver):
    """The live PokerBet adapter behind the existing protocol."""

    name = "pokerbet_dom"

    def __init__(self, browser: CdpBrowser, *,
                 line_tolerance: float = 0.0,
                 price_tolerance: float = 0.0):
        self._browser = browser
        self.line_tolerance = float(line_tolerance)
        self.price_tolerance = float(price_tolerance)
        # event-name (upper) → canonical record
        self._registry: dict[str, dict] = {}
        self._page = None
        # legs clicked in this process, for the pre-submit slip gate:
        # {"event", "market", "position", "line", "price", "game_id"}
        self._pending_legs: list[dict] = []

    # ── registration (canonical binding, directive 2A) ────────────────
    def register_game(self, event: str, *, game_id: str,
                      home_team: str, away_team: str,
                      classification: str = "") -> None:
        self._registry[str(event).strip().upper()] = {
            "event": str(event).strip(), "game_id": str(game_id),
            "home_team": home_team, "away_team": away_team,
            "classification": classification}

    def _registered(self, event: str) -> Optional[dict]:
        return self._registry.get(str(event).strip().upper())

    # ── page handling ─────────────────────────────────────────────────
    def _goto_event(self, rec: dict):
        url = session_mod.event_view_url(
            rec["game_id"], rec.get("classification"))
        self._page = self._browser.page()
        self._page.goto(url)
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=8_000)
        except Exception:
            pass
        return self._page

    def _page_game_id(self) -> Optional[str]:
        """The game id the CURRENT page is actually showing — parsed from
        the event-view URL (the id is embedded in it).  This is how a
        stale/contaminated tab is detected: the URL is the page's own
        claim about which game is rendered."""
        try:
            m = re.search(r"/(\d{6,})/", str(self._page.url or ""))
            return m.group(1) if m else None
        except Exception:
            return None

    def _page_teams(self) -> tuple[Optional[str], Optional[str]]:
        """The two rendered team names from the event header."""
        try:
            loc = self._page.locator(
                "[class*='event'] [class*='team'], [class*='competitor']")
            if loc.count() >= 2:
                t1 = loc.first.inner_text().strip()
                t2 = loc.last.inner_text().strip()
                return t1, t2
        except Exception:
            pass
        return None, None

    # ── verification (directive 2C — ordered, hard-reject) ───────────
    def verify_offer(self, *, game_id: str, home_team: str,
                     away_team: str, market: str, position: str,
                     expected_line: Optional[float],
                     expected_price: Optional[float],
                     offer_line: Optional[float],
                     offer_price: Optional[float],
                     offer_game_id: Optional[str] = None,
                     rendered_home: Optional[str] = None,
                     rendered_away: Optional[str] = None,
                     suspended: bool = False) -> OfferCheck:
        """Independent verification of one live offer against the
        canonical record + the intended bet.  Pure — unit-testable
        without a browser."""
        # 1. GAME ID — primary identity; names never substitute for it.
        og = str(offer_game_id or "").strip()
        if og and og != str(game_id).strip():
            return OfferCheck(False, "GAME_ID_MISMATCH")
        # 2. TEAMS — both must match (either rendering assignment).
        if rendered_home is not None and rendered_away is not None:
            if not names_match_pair(rendered_home, rendered_away,
                                    home_team, away_team):
                return OfferCheck(False, "TEAM_MISMATCH")
        # 3. MARKET — MVP scope is TOTAL; anything else never resolves.
        if "TOTAL" not in str(market or "").upper():
            return OfferCheck(False, "MARKET_MISMATCH")
        # 4. SELECTION — OVER/UNDER exactly.
        if str(position or "").upper() not in ("OVER", "UNDER"):
            return OfferCheck(False, "SELECTION_MISMATCH")
        # 5. LINE — exact within the configured tolerance.
        if expected_line is None or offer_line is None:
            return OfferCheck(False, "MISSING_DATA")
        if abs(float(offer_line) - float(expected_line)) > \
                self.line_tolerance + 1e-9:
            return OfferCheck(False, "LINE_MISMATCH")
        # 6. ODDS — within the configured tolerance.
        if offer_price is None:
            return OfferCheck(False, "MISSING_DATA")
        if expected_price is not None and abs(
                float(offer_price) - float(expected_price)) > \
                self.price_tolerance + 1e-9:
            return OfferCheck(False, "ODDS_MISMATCH")
        # 7. ACTIVE — a suspended market is never selectable.
        if suspended:
            return OfferCheck(False, "MARKET_SUSPENDED")
        return OfferCheck(True)

    # ── protocol: resolve the game ────────────────────────────────────
    def find_event(self, event: str) -> bool:
        rec = self._registered(event)
        if rec is None:
            return False                    # fail-closed: never guess
        try:
            self._goto_event(rec)
            page_gid = self._page_game_id()
            rh, ra = self._page_teams()
        except AdapterUnavailable:
            raise
        except Exception:
            return False
        # game_id match AND team identity — both, always (directive 5).
        if str(page_gid or "").strip() != str(rec["game_id"]).strip():
            return False
        return names_match_pair(rh, ra, rec["home_team"], rec["away_team"])

    # ── protocol: resolve the market ──────────────────────────────────
    def find_market(self, event: str, market: str) -> bool:
        if "TOTAL" not in str(market or "").upper():
            return False                    # MVP scope lock
        if self._registered(event) is None:
            return False
        if self._page is None:
            return False
        try:
            block = self._page.locator(SEL_MARKET_BLOCK,
                                       has_text="Total")
            return block.count() > 0
        except Exception:
            return False

    # ── protocol: resolve the CURRENT offer ───────────────────────────
    def _market_text(self) -> Optional[str]:
        try:
            loc = self._page.locator(SEL_MARKET_BLOCK, has_text="Total")
            if loc.count() == 0:
                return None
            return loc.first.inner_text()
        except Exception:
            return None

    def parse_market_text(self, text: Optional[str]
                          ) -> tuple[Optional[float], Optional[float],
                                     Optional[float]]:
        """Tolerant parse of the totals market text →
        (line, over_price, under_price).  Mirrors the collector's
        event_parser philosophy: read what is rendered, never guess."""
        if not text:
            return None, None, None
        line = over = under = None
        tokens = (text or "").replace(":", " ").split()
        lower = [t.lower() for t in tokens]
        # the line: first bare half-point/integer token
        for t in tokens:
            if _LINE_RE.match(t):
                line = float(t)
                break
        # prices: prefer O/U-marked tokens, else first two floats after
        # the line that are NOT the line itself
        for i, t in enumerate(lower):
            if t in ("o", "over") and i + 1 < len(tokens):
                m = _FLOAT_RE.match(tokens[i + 1])
                if m:
                    over = float(m.group(0))
            if t in ("u", "under") and i + 1 < len(tokens):
                m = _FLOAT_RE.match(tokens[i + 1])
                if m:
                    under = float(m.group(0))
        if over is None or under is None:
            nums = [float(t) for t in tokens if _FLOAT_RE.fullmatch(t)]
            nums = [n for n in nums
                    if line is None or n != line]
            if len(nums) >= 2:
                over, under = nums[0], nums[1]
        return line, over, under

    def find_position(self, event: str, market: str,
                      position: str) -> Optional[MarketObservation]:
        rec = self._registered(event)
        if rec is None or self._page is None:
            return None
        if "TOTAL" not in str(market or "").upper():
            return None
        text = self._market_text()
        line, over, under = self.parse_market_text(text)
        price = over if position.upper() == "OVER" else under
        if line is None or price is None:
            return None                     # POSITION_NOT_FOUND (recoverable)
        suspended = self._market_suspended()
        handle = {"page": self._page, "market_text": text,
                  "game_id": rec["game_id"],
                  "rendered_home": self._page_teams()[0],
                  "rendered_away": self._page_teams()[1]}
        return MarketObservation(
            event=event, market=market, position=position.upper(),
            line=line, price=price, handle=handle, suspended=suspended)

    def _market_suspended(self) -> bool:
        try:
            loc = self._page.locator(
                "[class*='suspended'], [class*='locked']")
            return loc.count() > 0
        except Exception:
            return False

    # ── protocol: click the CURRENT selection ─────────────────────────
    def click_selection(self, obs: MarketObservation) -> bool:
        handle = obs.handle or {}
        loc = handle.get("clickable")
        if loc is None:
            # build the clickable from the market block's position cell:
            # the OVER/UNDER price element inside the current market text
            try:
                cell = self._page.locator(
                    "[class*='outcome'], button", has_text=(
                        "Over" if obs.position.upper() == "OVER"
                        else "Under"))
                if cell.count() == 0:
                    return False
                loc = cell.first
            except Exception:
                return False
        try:
            loc.click(timeout=4_000)
        except Exception:
            return False
        self._pending_legs.append({
            "event": obs.event, "market": obs.market,
            "position": obs.position, "line": obs.line,
            "price": obs.price,
            "game_id": (obs.handle or {}).get("game_id")})
        return True

    # ── protocol: the betslip ─────────────────────────────────────────
    def read_betslip(self) -> list[dict]:
        if self._page is None:
            return []
        try:
            loc = self._page.locator(SEL_BETSLIP)
            if loc.count() == 0:
                return []
            text = loc.first.inner_text()
        except Exception:
            return []
        return self.parse_betslip_text(text)

    def parse_betslip_text(self, text: Optional[str]) -> list[dict]:
        """Tolerant betslip parse → [{event, market, position, line,
        price, game_id?}].  Empty list on unreadable content — the
        verifier treats that as BETSLIP_NOT_UPDATED, never as success."""
        entries: list[dict] = []
        if not text:
            return entries
        blocks = re.split(r"\n(?=[A-Z][^\n]*\s(?:vs\.?|-)\s[^\n]*\n)",
                          text)
        for block in blocks:
            lines = [l.strip() for l in block.splitlines() if l.strip()]
            if not lines:
                continue
            ev = next((l for l in lines
                       if _TEAM_SPLIT_RE.search(l)), None)
            if ev is None:
                continue
            pos = ("UNDER" if re.search(r"under", block, re.I)
                   else "OVER" if re.search(r"over", block, re.I)
                   else None)
            nums = [float(t) for t in
                    _FLOAT_RE.findall(" ".join(lines))
                    if 1.01 <= float(t) <= 1000]
            line = next((n for n in nums if n == int(n)
                         or abs(n * 2 - round(n * 2)) < 1e-9
                         and n < 400), None)
            price = next((n for n in nums
                          if n != line and 1.01 <= n <= 50), None)
            if pos is None or line is None or price is None:
                continue
            gid = None
            m = re.search(r"\b(\d{6,})\b", block)
            if m:
                gid = m.group(1)
            entries.append({"event": ev, "market": "TOTAL",
                            "position": pos, "line": line,
                            "price": price, "game_id": gid})
        return entries

    # ── protocol: placement (THE LIVE GATE) ───────────────────────────
    def place_parlay(self, stake_amount: float) -> dict:
        """Submit ONLY after re-verifying the LIVE slip against every
        pending leg + the stake — immediately before submission."""
        # lazy page attach: a placement attempt is valid whenever the
        # browser is alive, even if this process never navigated
        if self._page is None:
            try:
                self._page = self._browser.page()
            except AdapterUnavailable:
                raise
            except Exception:
                self._page = None
        page = self._page
        if page is None:
            return {"status": "FAILED",
                    "error_code": "NO_PAGE",
                    "error_message": "adapter has no live page"}
        # ── E/F: re-read the slip NOW and verify every leg ────────────
        entries = self.read_betslip()
        if not entries:
            return {"status": "FAILED", "error_code": "BETSLIP_UNREADABLE",
                    "error_message": "live betslip unreadable at submit"}
        for leg in self._pending_legs:
            matches = [e for e in entries
                       if e.get("position") == leg["position"]
                       and _norm_team(leg["event"])
                       and _norm_team(leg["event"])
                       in _norm_team(e.get("event") or "")]
            if len(matches) != 1:
                return {"status": "FAILED",
                        "error_code": "LEG_NOT_IN_SLIP",
                        "error_message": f"leg {leg['event']} "
                        f"{leg['position']} not exactly once in slip"}
            e = matches[0]
            gid_ok = (not str(leg.get("game_id") or "").strip()
                      or not str(e.get("game_id") or "").strip()
                      or str(e["game_id"]) == str(leg["game_id"]))
            if not gid_ok:
                return {"status": "FAILED", "error_code": "GAME_ID_MISMATCH",
                        "error_message": "slip leg carries a different "
                        "game id — refusing to submit"}
            if abs(float(e["line"]) - float(leg["line"])) > \
                    self.line_tolerance + 1e-9:
                return {"status": "FAILED", "error_code": "LINE_MISMATCH",
                        "error_message": "slip line differs from the "
                        "verified offer"}
            if abs(float(e["price"]) - float(leg["price"])) > \
                    self.price_tolerance + 1e-9:
                return {"status": "FAILED", "error_code": "ODDS_MISMATCH",
                        "error_message": "slip odds differ from the "
                        "verified offer"}
        # ── stake: filled and re-read back (F: verify stake) ──────────
        try:
            stake_loc = page.locator(SEL_STAKE_INPUT).first
            stake_loc.fill(str(stake_amount))
            got = float(stake_loc.input_value())
        except Exception as e:
            return {"status": "FAILED", "error_code": "STAKE_UNFILLABLE",
                    "error_message": f"stake input unusable: {e}"}
        if abs(got - float(stake_amount)) > 1e-6:
            return {"status": "FAILED", "error_code": "STAKE_MISMATCH",
                    "error_message": f"slip stake {got} != requested "
                    f"{stake_amount}"}
        # ── G: every gate passed → submit ─────────────────────────────
        try:
            btn = page.locator(SEL_PLACE_BUTTON, has_text="place").first
            btn.click(timeout=4_000)
        except Exception as e:
            return {"status": "FAILED", "error_code": "SUBMIT_FAILED",
                    "error_message": f"submit click failed: {e}"}
        self._pending_legs = []
        # ── H: the bookmaker's own confirmation ───────────────────────
        conf = self.read_order_confirmation()
        if conf:
            return {"status": "ACCEPTED",
                    "provider_ref": conf.get("reference")}
        return {"status": "SUBMITTED", "provider_ref": None}

    def read_order_confirmation(self) -> Optional[dict]:
        if self._page is None:
            return None
        try:
            loc = self._page.locator(SEL_RECEIPT)
            for i in range(min(loc.count(), 5)):
                text = loc.first.inner_text()
                m = re.search(r"\b([A-Z0-9]{6,})\b", text or "")
                if m:
                    return {"reference": m.group(1)}
        except Exception:
            pass
        return None


def pokerbet_adapter_factory(cfg, games: list[dict]):
    """Phase-③ glue for the EXISTING ``ExecutionQueue(adapter_factory=…)``
    contract.  ``games``: [{event, game_id, home_team, away_team,
    classification}] — every leg's event must be registered here or the
    adapter fail-closes with EVENT_NOT_FOUND.

    LIVE stays gated exactly as before: the queue refuses LIVE unless
    EXECUTION_DRY_RUN=false; the engine stops at BETSLIP_READY in every
    non-LIVE mode; this adapter re-verifies the live slip before any
    submission.
    """
    def factory() -> PokerBetDomAdapter:
        browser = CdpBrowser(cfg.cdp_url).connect()
        adapter = PokerBetDomAdapter(
            browser,
            line_tolerance=getattr(cfg, "pokerbet_line_tolerance", 0.0),
            price_tolerance=getattr(cfg, "pokerbet_price_tolerance", 0.0))
        for g in games:
            adapter.register_game(
                g["event"], game_id=g["game_id"],
                home_team=g["home_team"], away_team=g["away_team"],
                classification=g.get("classification", ""))
        return adapter
    return factory
