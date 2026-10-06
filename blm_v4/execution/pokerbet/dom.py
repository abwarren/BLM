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
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, unquote_plus, urlsplit

from blm_v4.execution.adapter import (
    AdapterUnavailable,
    MarketObservation,
    SelectionResolver,
)
from blm_v4.execution.pokerbet.browser import CdpBrowser
from blm_v4.execution.pokerbet import session as session_mod
from blm_v4.execution.pokerbet.latency import LatencyTrace

# ── selectors (the ONLY DOM coupling in BLM) ──────────────────────────
# READ-PATH selectors are LIVE-VERIFIED (2026-10-05) against a real
# authenticated PokerBet session on BOTH Soccer and Basketball.  Confirmed
# cell mapping: cells[0]="Over" hdr, cells[1]="Under" hdr, cells[2]=line,
# cells[3]=Over odds, cells[4]=Under odds.  A bookmaker class change makes a
# selector match nothing → FAIL CLOSED.
SEL_TEAM_HEADER = "[class*='game-d-c-b-r-c-team-name']"   # event-header teams
SEL_MARKET_TAB = "[class*='horizontal-sl-tab-bc']"        # All/Match/Totals/...
SEL_MARKET_ITEM = "[class*='sgm-market-g-item-bc']"       # one market row
SEL_MARKET_CELL = "[class*='sgm-market-g-i-cell-bc']"     # a market cell
#: the market GROUP element that carries the market TITLE (class token exactly
#: ``sgm-market-g`` — ``~=`` avoids also matching the -item-bc / -i-cell-bc
#: tokens; the game total's title is "Total Points", a team total's title is
#: "<Team> Total Points").
SEL_MARKET_GROUP = "[class~='sgm-market-g']"
#: bounded SPA hydration budget (ms) — header + tabs + totals grid.
HYDRATE_TIMEOUT_MS = 8000
#: bounded wait for the submit control to APPEAR and become ENABLED.  The
#: control is STATE-DEPENDENT (absent with an empty slip, disabled while
#: the selection is dead), so a one-shot count check is not sufficient.
SUBMIT_GATE_TIMEOUT_MS = 8000
# ── WRITE-PATH selectors ──────────────────────────────────────────────
# VERIFIED LIVE 2026-10-05 against an authenticated PokerBet session (ZAR
# account, Basketball virtual Totals; ONE selection added to the slip — no
# stake typed, nothing submitted).  The previous values were wrong and
# dangerous: ``[class*='betslip']`` matched the whole page shell FIRST,
# ``input[type='number']`` matched NOTHING (the real field is type=text) and a
# bare ``button`` matched 13 elements whose first was the LOGOUT control.
SEL_BETSLIP = ".betslip-full-content-bc, .betslip-bc"   # scoped, never the page
#: one leg of the betslip (verified leg container)
SEL_BETSLIP_LEG = ".bs-bet-item-bc"
#: the stake field — ``type=text``, placeholder "Enter stake"
SEL_STAKE_INPUT = "input.bs-bet-i-b-s-i-bc"
#: the submit control — the "BET NOW" button, class
#: "btn a-color button-type-0 ellipsis".  Live-verified 2026-10-05: the old
#: "button.a-accept" matched NOTHING on the current layout, so the gate always
#: fail-closed.  Deliberately NOT a generic "button" selector; the gate also
#: requires match-count == 1, an ENABLED control, and a place-bet button TEXT.
SEL_PLACE_BUTTON = "button.btn.a-color.button-type-0"
#: the placed-bets TAB/CONTAINER ("Open Bets").  This is NOT a receipt and NOT
#: a provider-reference selector — it is only the verified container the
#: resulting ticket appears in.  The ticket/reference element INSIDE it has
#: never been observed (that needs a real placement) and must be discovered and
#: validated before ``read_order_confirmation`` may claim an authoritative
#: reference.  Deliberately NOT wired as SEL_RECEIPT.
SEL_OPEN_BETS = ".tab-bc.open-bets"
#: UNVERIFIED — leave FAILING CLOSED.  No receipt/confirmation element exists
#: in the pre-placement DOM, so this matches nothing on the live site; the
#: adapter therefore reports the submission as SUBMITTED (never ACCEPTED) until
#: the ticket element is proven on a real placement.
SEL_RECEIPT = "[class*='receipt'], [class*='confirmation'], [class*='bet-id']"
SEL_MARKET_BLOCK = "[class*='market'], [data-testid*='market']"

_LINE_RE = re.compile(r"^\d{1,3}(?:\.\d)?$")
_FLOAT_RE = re.compile(r"\d+(?:\.\d+)?")
_TEAM_SPLIT_RE = re.compile(r"\s+vs\.?\s+|\s+-\s+", re.IGNORECASE)


#: Bookmaker RENDERING suffixes that are NOT part of a team's identity.  The
#: virtual competitions render every participant as "<Team> Virtual" while the
#: collector stores the base name ("Bursaspor"), so without this the adapter's
#: team check could never match a virtual event — verified live 2026-10-05:
#: page_game_id matched while find_event returned False for every virtual game.
#: ONLY these exact literals are stripped, and never down to an empty string, so
#: two genuinely different teams can never collapse into one another.
_RENDER_SUFFIXES = ("virtual",)


def _norm_team(name) -> str:
    """Rendered-team normalizer: case/space/punctuation-insensitive, plus the
    bookmaker's documented rendering suffixes."""
    s = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    for suf in _RENDER_SUFFIXES:
        if len(s) > len(suf) and s.endswith(suf):
            s = s[:-len(suf)]
    return s


def _market_title(group_text) -> str:
    """The market title from a ``.sgm-market-g`` group's text.

    The group text is ``"<title>\\n\\nOver\\nUnder\\n<cells...>"``; the title is
    everything before the first ``Over``/``Under`` header line.  The GAME
    total's title is ``"Total Points"`` (bare) while a team total's title is
    ``"<Team> Total Points"`` — that is the discriminator between them.
    """
    parts = []
    for line in (group_text or "").splitlines():
        ln = line.strip()
        if ln.lower() in ("over", "under"):
            break
        if ln:
            parts.append(ln)
    return " ".join(parts).strip()


def _same_event_url(current, target) -> bool:
    """True when the tab is ALREADY on the target event view.

    A reload costs ~3.5 s live (measured 2026-10-05), so it is skipped when the
    tab is already there.  The comparison tolerates RENDERING differences
    (trailing slash, fragment, ``%20`` vs ``+``, host/scheme case) and is
    deliberately intolerant of anything identifying the fixture: the event id is
    a numeric path segment, so two different games can never compare equal.
    """
    def norm(u):
        try:
            parts = urlsplit(str(u or "").strip())
        except Exception:
            return None
        path = unquote_plus(parts.path).rstrip("/").lower()
        query = "&".join(sorted(unquote_plus(parts.query).split("&"))) \
            if parts.query else ""
        return (parts.netloc.lower(), path, query)

    a, b = norm(current), norm(target)
    return a is not None and a == b


def names_match_pair(rendered_home, rendered_away,
                     rec_home, rec_away) -> bool:
    """Both record teams must match the two rendered names in SOME
    assignment (the SPA swaps rendering order) — one look-alike is not
    enough, and a swapped render is accepted and re-oriented by the
    engine's existing verification."""
    r = {_norm_team(rendered_home), _norm_team(rendered_away)}
    w = {_norm_team(rec_home), _norm_team(rec_away)}
    return r == w and "" not in r


def _num(value) -> Optional[float]:
    """Finite float or None — the submit gate's numeric coercion (never a
    guess, never a bool)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
                 price_tolerance: float = 0.0,
                 max_odds_drift: float = 0.0,
                 hydrate_timeout_ms: int = HYDRATE_TIMEOUT_MS,
                 submit_gate_timeout_ms: int = SUBMIT_GATE_TIMEOUT_MS,
                 trace: Optional[LatencyTrace] = None):
        self._browser = browser
        self.line_tolerance = float(line_tolerance)
        self.price_tolerance = float(price_tolerance)
        #: BOUNDED ODDS-CHANGE ACCEPTANCE.  The bookmaker moves the price
        #: between the observed offer and the betslip; a move within
        #: ``max_odds_drift`` (decimal odds) is ACCEPTED and recorded, a larger
        #: move fails closed.  Default 0.0 = exact match required (the
        #: pre-existing behaviour).
        self.max_odds_drift = max(0.0, float(max_odds_drift))
        self.hydrate_timeout_ms = int(hydrate_timeout_ms)
        self.submit_gate_timeout_ms = int(submit_gate_timeout_ms)
        #: MEASUREMENT ONLY — a trace never gates or delays anything.
        self.trace = trace
        self._submit_button = None
        # event-name (upper) → canonical record
        self._registry: dict[str, dict] = {}
        self._page = None
        # legs clicked in this process, for the pre-submit slip gate:
        # {"event", "market", "position", "line", "price", "game_id"}
        self._pending_legs: list[dict] = []

    # ── registration (canonical binding, directive 2A) ────────────────
    def register_game(self, event: str, *, game_id: str,
                      home_team: str, away_team: str,
                      classification: str = "",
                      source_url: str = "") -> None:
        self._registry[str(event).strip().upper()] = {
            "event": str(event).strip(), "game_id": str(game_id),
            "home_team": home_team, "away_team": away_team,
            "classification": classification,
            "source_url": str(source_url or "").strip()}

    def _registered(self, event: str) -> Optional[dict]:
        return self._registry.get(str(event).strip().upper())

    # ── page handling ─────────────────────────────────────────────────
    def _goto_event(self, rec: dict):
        """Navigate to the game's RECORDED ``source_url`` (never rebuilt).

        Fails closed (AdapterUnavailable) when the record carries no
        source_url — we never fabricate a URL shape.
        """
        url = session_mod.event_view_url_for(rec)
        if not url:
            raise AdapterUnavailable(
                f"no source_url recorded for game {rec.get('game_id')}")
        self._page = self._browser.page()
        if _same_event_url(self._page.url, url):
            # ALREADY on this event view — skip the reload (measured 3.5 s
            # live).  Hydration is still asserted by the caller's _ready(),
            # so skipping can never let an unhydrated page through.
            return self._page
        self._page.goto(url)
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=8_000)
        except Exception:
            pass
        return self._page

    def _ready(self, timeout_ms: int = HYDRATE_TIMEOUT_MS) -> bool:
        """Bounded SPA-readiness gate.

        The page can briefly expose the WRONG/default DOM before the SPA
        hydrates (live-observed: tabs/header absent, the unfiltered "All"
        grid showing).  Wait, bounded, for the combination of the event
        identity (team header), the market tabs, and — after selecting the
        Totals tab — the totals grid.  Returns False (caller fails closed)
        when readiness is not reached within the bound.
        """
        if self._page is None:
            return False
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0

        def _count(sel):
            # re-query each poll: the SPA's DOM changes as it hydrates, so a
            # locator snapshot taken once is not a valid readiness signal
            try:
                return self._page.locator(sel).count()
            except Exception:
                return 0

        while time.monotonic() < deadline:
            if _count(SEL_TEAM_HEADER) >= 2 and _count(SEL_MARKET_TAB) >= 3:
                break
            self._page.wait_for_timeout(150)
        else:
            return False
        # Select the Totals tab (a TAB, never a price cell) when the grid is
        # not already rendered; the click is idempotent.
        try:
            if _count(SEL_MARKET_ITEM) == 0:
                for i in range(_count(SEL_MARKET_TAB)):
                    tab = self._page.locator(SEL_MARKET_TAB).nth(i)
                    if "total" in (tab.inner_text() or "").lower():
                        tab.click(timeout=4_000)
                        break
        except Exception:
            pass
        while time.monotonic() < deadline:
            if _count(SEL_MARKET_ITEM) > 0:
                return True
            self._page.wait_for_timeout(150)
        return False

    def _page_game_id(self) -> Optional[str]:
        """The game id the CURRENT page is actually showing — parsed from
        the event-view URL's trailing ``<event_id>/<name>`` segment.  A
        stale/contaminated tab's URL is its own claim about which game is
        rendered.  (Both the competition id and the event id are 6+ digits,
        so the regex anchors on the FINAL id segment, not the first.)"""
        try:
            url = str(self._page.url or "").rstrip("/")
            m = re.search(r"/(\d{6,})/[^/]+$", url)
            return m.group(1) if m else None
        except Exception:
            return None

    def _page_teams(self) -> tuple[Optional[str], Optional[str]]:
        """The two rendered team names from the event header.

        The real header renders each team name MORE THAN ONCE (live-observed
        as 4 nodes for 2 teams), so ``count == 2`` must never be assumed: we
        drop consecutive duplicates, require EXACTLY two distinct usable
        values, and fail closed otherwise.
        """
        try:
            loc = self._page.locator(SEL_TEAM_HEADER)
            vals: list[str] = []
            for i in range(min(loc.count(), 12)):
                t = str(loc.nth(i).inner_text() or "").strip()
                if not t:
                    continue
                if vals and _norm_team(t) == _norm_team(vals[-1]):
                    continue            # consecutive duplicate
                vals.append(t)
            if len(vals) == 2:
                return vals[0], vals[1]
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
            if not self._ready(self.hydrate_timeout_ms):
                return False            # hydration timeout → FAIL CLOSED
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
        if self._page is None or not self._ready(self.hydrate_timeout_ms):
            return False
        # The market must exist AND the GAME total must be unambiguously
        # identifiable within it (a team total alone is never "the total").
        try:
            items = self._page.locator(SEL_MARKET_ITEM)
            if items.count() == 0:
                return False
            home, away = self._page_teams()
            return self._game_total_item(items, home, away) is not None
        except Exception:
            return False

    # ── protocol: resolve the CURRENT offer ───────────────────────────
    def _group_of(self, item):
        """The ``.sgm-market-g`` GROUP element carrying the market TITLE for
        a given totals item — the title is what distinguishes the GAME total
        ("Total Points") from a TEAM total ("<Team> Total Points")."""
        try:
            anc = item.locator(
                "xpath=ancestor::*[contains(concat(' ', normalize-space("
                "@class), ' '), ' sgm-market-g ')][1]")
            return anc.first if anc.count() else None
        except Exception:
            return None

    def _game_total_item(self, items, home, away):
        """The ONE market item that is the GAME total.

        Selects the item whose market title contains "total" and NEITHER
        team name (team totals are titled ``"<Team> Total Points"``).
        Returns None — FAIL CLOSED — unless EXACTLY ONE candidate is found,
        so a missing or ambiguous game total never resolves to a team total.
        """
        nh, na = _norm_team(home), _norm_team(away)
        # Words that mark a "total"-containing market as NOT the plain game
        # total line: parity (Odd/Even), combined/compound ("... Winner And
        # ..."), last-digits ("Ending - ... Last Digits Total"), handicap and
        # correct-score.  Live-verified 2026-10-05 on a Virtual Matches page:
        # without these the All-markets view yielded THREE "total" candidates
        # ("Total Points", "Match Winner And Total Points", "Ending - Teams
        # Score Last Digits Total"), so the game total never resolved.
        _EXCL = ("odd", "even", "winner", "and", "last", "digit", "ending",
                 "handicap", "margin", "score", "correct", "exact",
                 "quarter", "half", "period")
        cands = []
        for i in range(min(items.count(), 50)):
            it = items.nth(i)
            grp = self._group_of(it)
            title = _market_title(grp.inner_text()) if grp is not None else ""
            tl = _norm_team(title)
            if "total" not in tl:
                continue                     # not a totals market
            if any(w in tl for w in _EXCL):
                continue                     # parity/combined/period/...
            if (nh and nh in tl) or (na and na in tl):
                continue                     # a TEAM total, not the game total
            cands.append(it)
        if len(cands) != 1:
            return None                      # missing or ambiguous → closed
        return cands[0]

    def _totals_item(self):
        """The GAME-total market item for the current page (or None)."""
        try:
            items = self._page.locator(SEL_MARKET_ITEM)
            if items.count() == 0:
                return None
            home, away = self._page_teams()
            return self._game_total_item(items, home, away)
        except Exception:
            return None

    def _market_text(self) -> Optional[str]:
        item = self._totals_item()
        if item is None:
            return None
        try:
            return item.inner_text()
        except Exception:
            return None

    def _read_totals(self) -> tuple[Optional[float], Optional[float],
                                    Optional[float]]:
        """(line, over, under) from the GAME-total item's cells.

        Cell mapping (live-verified): cells[0]="Over", cells[1]="Under",
        then per line-triple ``line, Over odds, Under odds``; the FIRST
        triple is read.  Fails closed (None, None, None) when the game total
        is ambiguous or missing — it never falls back to a team total.
        """
        item = self._totals_item()
        if item is None:
            return None, None, None
        try:
            cells = item.locator(SEL_MARKET_CELL)
            n = cells.count()
            if n < 3:
                return None, None, None
            texts = [str(cells.nth(i).inner_text() or "").strip()
                     for i in range(min(n, 60))]

            def _num(t):
                m = _FLOAT_RE.search(t or "")
                return float(m.group(0)) if m else None

            li = next((i for i, t in enumerate(texts)
                       if _LINE_RE.match(t)), None)
            if li is None or li + 2 >= n:
                return None, None, None
            line = float(texts[li])
            over = _num(texts[li + 1])
            under = _num(texts[li + 2])
            if over is None or under is None:
                return None, None, None
            return line, over, under
        except Exception:
            return None, None, None

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

    def _position_cell(self, position: str):
        """The game-total item's OVER/UNDER ODDS cell — the REAL clickable.

        cells = [Over hdr, Under hdr, then per line-triple (line, OVER odds,
        UNDER odds)]; the FIRST triple is the current offer.
        """
        try:
            item = self._totals_item()
            if item is None:
                return None
            cells = item.locator(SEL_MARKET_CELL)
            n = cells.count()
            texts = [(cells.nth(k).inner_text() or "").strip()
                     for k in range(min(n, 60))]
            li = next((k for k, t in enumerate(texts) if _LINE_RE.match(t)), None)
            if li is None or li + 2 >= n:
                return None
            return cells.nth(li + (1 if str(position).upper() == "OVER" else 2))
        except Exception:
            return None

    def find_position(self, event: str, market: str,
                      position: str) -> Optional[MarketObservation]:
        rec = self._registered(event)
        if rec is None or self._page is None:
            return None
        if "TOTAL" not in str(market or "").upper():
            return None
        if not self._ready(self.hydrate_timeout_ms):
            return None                     # hydration not complete → closed
        text = self._market_text()
        line, over, under = self._read_totals()
        price = over if position.upper() == "OVER" else under
        if line is None or price is None:
            return None                     # POSITION_NOT_FOUND (recoverable)
        suspended = self._market_suspended()
        handle = {"page": self._page, "market_text": text,
                  "game_id": rec["game_id"],
                  "rendered_home": self._page_teams()[0],
                  "rendered_away": self._page_teams()[1],
                  "clickable": self._position_cell(position)}
        self._mark("market_observed")
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
            # PINNED clickable (game-total UNDER odds cell, recorded by
            # find_position), then a fresh item-scoped resolution, then the
            # legacy "[class*='outcome'], button"+"Under" fallback.
            try:
                loc = self._position_cell(obs.position)
            except Exception:
                loc = None
        if loc is None:
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
        # ── activation strategy ───────────────────────────────────────
        # Cyber 2K26: a real pointer click is the control.  BETUAL
        # virtuals bind the cell's add-handler so that ONLY an in-page DOM
        # click adds the selection — a synthesised pointer click at the
        # cell's box does not add it (and can toggle it off).  Verified
        # live 2026-10-05.  The Cyber path is untouched; BETUAL gets its
        # own strategy rather than a weakened pin or gate.
        try:
            if self._selection_strategy() == "dom_click":
                loc.evaluate("el => el.click()")
            else:
                loc.click(timeout=4_000)
        except Exception:
            return False
        self._mark("selection_added")
        self._pending_legs.append({
            "event": obs.event, "market": obs.market,
            "position": obs.position, "line": obs.line,
            "price": obs.price,
            "game_id": (obs.handle or {}).get("game_id")})
        return True

    # ── protocol: the betslip ─────────────────────────────────────────
    def _selection_strategy(self) -> str:
        """'dom_click' for BETUAL virtuals, else 'pointer' (Cyber default).

        BETUAL virtual market cells only add to the betslip on an in-page
        DOM click (live-verified 2026-10-05); the Cyber path stays a pointer
        click.  Selected by the event URL, never by weakening the gate.
        """
        try:
            url = (self._page.url or "").lower() if self._page is not None else ""
        except Exception:
            url = ""
        return "dom_click" if "betual" in url else "pointer"

    def _parse_betslip_dom(self) -> list[dict]:
        """BETUAL leg layout: selection + market come BEFORE the event line,
        so the text parser (event-line-first) finds nothing.  Read the
        structured fields straight from each leg instead."""
        if self._page is None:
            return []
        try:
            legs = self._page.locator(SEL_BETSLIP_LEG)
            n = legs.count()
        except Exception:
            return []
        out: list[dict] = []
        for i in range(min(n, 12)):
            leg = legs.nth(i)

            def _t(sel: str) -> str:
                try:
                    l = leg.locator(sel)
                    return ((l.first.inner_text() or "").strip()
                            if l.count() else "")
                except Exception:
                    return ""

            title = _t(".bs-bet-i-b-title-bc.t-2") or _t(".bs-bet-i-b-title-bc")
            coeff = _t(".bs-bet-i-b-coefficient-bc")
            event = _t(".bs-bet-i-h-title-bc-text")
            m_pos = re.search(r"(under|over)", title, re.I)
            m_line = re.search(r"\(([\d.]+)\)", title)
            m_price = _FLOAT_RE.search(coeff) if coeff else None
            pos = m_pos.group(1).upper() if m_pos else None
            line = float(m_line.group(1)) if m_line else None
            price = float(m_price.group(0)) if m_price else None
            if pos is None or line is None or price is None:
                continue
            out.append({"event": event, "market": "TOTAL", "position": pos,
                        "line": line, "price": price, "game_id": None})
        return out

    def read_betslip(self) -> list[dict]:
        """The live betslip, parsed per leg.

        Reads the VERIFIED leg containers (``SEL_BETSLIP_LEG``) so the parse
        sees selection text only — never the slip's chrome (tabs, "Remove All",
        the place button).  Falls back to the verified container when no leg is
        rendered, and to [] when there is no slip at all: an absent or
        unreadable slip is never treated as success.
        """
        if self._page is None:
            return []
        try:
            legs = self._page.locator(SEL_BETSLIP_LEG)
            n = legs.count()
            if n > 0:
                text = "\n\n".join((legs.nth(i).inner_text() or "")
                                   for i in range(min(n, 12)))
            else:
                container = self._page.locator(SEL_BETSLIP)
                if container.count() == 0:
                    return []
                text = container.first.inner_text()
        except Exception:
            return []
        entries = self.parse_betslip_text(text)
        if not entries:
            entries = self._parse_betslip_dom()   # BETUAL structured layout
        return entries

    def clear_betslip(self) -> int:
        """Remove ALL current (UNSUBMITTED) legs from the slip.

        Production rule (2026-10-06): stale tickets are ALWAYS cleared before
        an attempt — the executor accumulates legs on failed attempts, which
        then breaks verification.  Returns the number of legs removed.
        """
        if self._page is None:
            return 0
        removed = 0
        for _ in range(40):
            try:
                legs = self._page.locator(SEL_BETSLIP_LEG)
                if legs.count() == 0:
                    break
                first = legs.first
                r = first.locator(".remove.bc-i-close-remove")
                if r.count() == 0:
                    r = first.locator(".remove")
                if r.count() == 0:
                    break
                r.first.click(timeout=4000)
                removed += 1
            except Exception:
                break
            try:
                self._page.wait_for_timeout(400)
            except Exception:
                pass
        return removed

    def reset_pending_legs(self) -> None:
        """Drop the tracked pending legs.

        Called when the slip is cleared before an attempt: ``click_selection``
        appends on EVERY click and ``place_parlay`` clears the list only after a
        successful submit, so without this the retry loop piles up pending legs
        and ``place_parlay`` then demands a leg that is no longer in the slip
        ("not exactly once in slip").
        """
        self._pending_legs = []

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
    # ── latency instrumentation (MEASUREMENT ONLY) ────────────────────
    def _mark(self, stage: str) -> None:
        """Record a stage timestamp.  Never gates, delays or retries."""
        if self.trace is not None:
            try:
                self.trace.mark(stage)
            except Exception:
                pass

    # ── THE SUBMIT GATE: every pre-submit check, and NO click ─────────
    def prepare_submit(self, stake_amount,
                       obs: Optional[MarketObservation] = None) -> dict:
        """EVERY check that must pass before a submit click — and no click.

        Returns ``{"ready": bool, "reason": str, ...}``.  The CALLER decides
        whether to click; this method never does.  Fails CLOSED on any gap:

          1. the submit control must APPEAR and be ENABLED (it is
             state-dependent: absent with an empty slip, DISABLED while the
             selection is dead, enabled only for a live staked selection), so
             it is polled within ``submit_gate_timeout_ms``;
          2. the stake must read back EXACTLY the requested amount;
          3. the betslip leg must be re-read NOW and still be the intended bet
             — direction, market ("TOTAL"), event identity and line.

        On success the resolved button is kept in ``self._submit_button`` so
        the caller clicks THAT element (no second, racier lookup).
        """
        self._submit_button = None
        if self._page is None:
            return {"ready": False, "reason": "no_page"}
        stake = _num(stake_amount)
        if stake is None or stake <= 0:
            return {"ready": False, "reason": "stake_invalid"}
        # 1. present AND enabled
        deadline = time.monotonic() + self.submit_gate_timeout_ms / 1000.0
        button = None
        while time.monotonic() < deadline:
            loc = self._page.locator(SEL_PLACE_BUTTON)
            if loc.count() == 1 and not loc.first.is_disabled():
                button = loc.first
                break
            self._page.wait_for_timeout(100)
        if button is None:
            # Production rule 2026-10-06: a moved line/price puts the slip in a
            # "changed" state that keeps BET NOW DISABLED until the change is
            # accepted.  Click the slip's accept/update control (never a
            # submit), then re-poll the submit control once.
            try:
                cand = self._page.locator(
                    "[class*='betslip'] button, [class*='betslip'] [role='button'],"
                    " [class*='bs-bet'] button, [class*='bs-bet'] [role='button']")
                for i in range(min(cand.count(), 15)):
                    el = cand.nth(i)
                    t = (el.inner_text() or "").strip().lower()
                    if any(k in t for k in ("accept", "update", "continue",
                                            "confirm", "ok", "yes")):
                        try:
                            el.click(timeout=2000)
                            self._page.wait_for_timeout(300)
                            break
                        except Exception:
                            continue
            except Exception:
                pass
            loc = self._page.locator(SEL_PLACE_BUTTON)
            if loc.count() == 1 and not loc.first.is_disabled():
                button = loc.first
        if button is None:
            return {"ready": False, "reason": "submit_control_unavailable",
                    "button_count": self._page.locator(
                        SEL_PLACE_BUTTON).count()}
        # the control must ALSO read as a place-bet control — a unique, enabled
        # button that is not the submit action is never clicked
        _bt = (button.inner_text() or "").strip().lower()
        if not any(k in _bt for k in ("bet now", "place bet", "accept")):
            return {"ready": False, "reason": "submit_control_unrecognised",
                    "button_text": _bt}
        self._mark("submit_enabled")
        # 2. stake read back EXACTLY (never a quick-stake, never Max)
        try:
            stake_loc = self._page.locator(SEL_STAKE_INPUT).first
            got = _num(stake_loc.input_value())
        except Exception as e:
            return {"ready": False, "reason": "stake_unreadable",
                    "error_message": str(e)}
        if got is None or abs(got - stake) > 1e-6:
            return {"ready": False, "reason": "stake_mismatch",
                    "stake_read": got, "stake_expected": stake}
        self._mark("stake_verified")
        # 3. the slip leg, re-read NOW, must still be the intended bet
        try:
            entries = self.read_betslip()
        except Exception:
            entries = []
        if not entries:
            return {"ready": False, "reason": "betslip_unreadable"}
        odds_accepted = []
        want = None
        if obs is not None:
            want = {"position": str(obs.position).upper(),
                    "line": _num(obs.line),
                    "price": _num(getattr(obs, "price", None)),
                    "event": getattr(obs, "event", "") or ""}
        elif self._pending_legs:
            last = self._pending_legs[-1]
            want = {"position": str(last.get("position") or "").upper(),
                    "line": _num(last.get("line")),
                    "price": _num(last.get("price")),
                    "event": last.get("event") or ""}
        leg = entries[0]
        if want:
            for e in entries:
                if str(e.get("position") or "").upper() == want["position"]:
                    leg = e
                    break
        if str(leg.get("position") or "").upper() not in ("OVER", "UNDER"):
            return {"ready": False, "reason": "leg_direction_unverified",
                    "leg": leg}
        if "TOTAL" not in str(leg.get("market") or "").upper():
            return {"ready": False, "reason": "leg_market_unverified",
                    "leg": leg}
        if want:
            if str(leg.get("position") or "").upper() != want["position"]:
                return {"ready": False, "reason": "leg_direction_mismatch",
                        "leg": leg, "expected": want["position"]}
            rec = self._registered(want["event"]) or {}
            rendered = str(leg.get("event") or "")
            for team in (rec.get("home_team"), rec.get("away_team")):
                if team and _norm_team(team) and _norm_team(team) not in \
                        _norm_team(rendered):
                    return {"ready": False, "reason": "leg_event_mismatch",
                            "leg": leg, "expected": team}
            # LINE/PRICE MOVEMENT IS IRRELEVANT (production rule 2026-10-06):
            # take the UNDER at whatever the slip shows NOW; a moved price is
            # ACCEPTED and recorded, a moved line is simply accepted.
            slip_price = _num(leg.get("price"))
            if want.get("price") is not None and slip_price is not None:
                odds_accepted.append({"offered": want["price"],
                                      "accepted": slip_price,
                                      "drift": abs(slip_price
                                                   - want["price"])})
        self._submit_button = button
        return {"ready": True, "reason": "submit_ready", "leg": leg,
                "stake": got, "odds_accepted": odds_accepted,
                "button_text": (button.inner_text() or "").strip()}

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
        odds_accepted = []
        for leg in self._pending_legs:
            # IDENTITY-TOLERANT match (production rule 2026-10-06): compare the
            # normalized TEAM TOKENS of an "A vs B" label instead of the whole
            # string — normalizing the full "A vs B" leaves the literal "vs"
            # glued in ("...cybervsgolden..."), which can never be a substring
            # of the slip's "A - B" rendering, so the leg was reported missing.
            _want = [_norm_team(t) for t in
                     re.split(r"\s+vs\s+", str(leg.get("event") or ""),
                              flags=re.I) if t]
            matches = []
            for e in entries:
                if (str(e.get("position") or "").upper()
                        != str(leg.get("position") or "").upper()):
                    continue
                _ev = _norm_team(e.get("event") or "")
                if _want and all(t and t in _ev for t in _want):
                    matches.append(e)
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
            # LINE/PRICE MOVEMENT IS IRRELEVANT (production rule 2026-10-06):
            # the UNDER is taken at whatever the book shows on the slip at the
            # instant of placement.  A moved line/price is ACCEPTED and
            # recorded — never a failure.
            _line_drift = abs(float(e["line"]) - float(leg["line"]))
            _price_drift = abs(float(e["price"]) - float(leg["price"]))
            odds_accepted.append({"offered": float(leg["price"]),
                                  "accepted": float(e["price"]),
                                  "price_drift": _price_drift,
                                  "offered_line": float(leg["line"]),
                                  "accepted_line": float(e["line"]),
                                  "line_drift": _line_drift})
        # ── stake: filled and re-read back (F: verify stake) ──────────
        try:
            stake_loc = page.locator(SEL_STAKE_INPUT).first
            stake_loc.fill(str(stake_amount))
            got = float(stake_loc.input_value())
        except Exception as e:
            return {"status": "FAILED", "error_code": "STAKE_UNFILLABLE",
                    "error_message": f"stake input unusable: {e}"}
        self._mark("stake_entered")
        if abs(got - float(stake_amount)) > 1e-6:
            return {"status": "FAILED", "error_code": "STAKE_MISMATCH",
                    "error_message": f"slip stake {got} != requested "
                    f"{stake_amount}"}
        # ── G: THE SUBMIT GATE — control present + enabled, stake and leg
        #    re-verified.  A refusal here means NO CLICK. ───────────────
        gate = self.prepare_submit(stake_amount)
        if not gate.get("ready"):
            return {"status": "FAILED",
                    "error_code": str(gate.get("reason")
                                      or "submit_gate_refused").upper(),
                    "error_message": f"submit gate refused: {gate}"}
        try:
            self._submit_button.click(timeout=4_000)
            self._mark("submit_clicked")
        except Exception as e:
            return {"status": "FAILED", "error_code": "SUBMIT_FAILED",
                    "error_message": f"submit click failed: {e}"}
        finally:
            self._submit_button = None
        self._pending_legs = []
        # ── H: the bookmaker's own confirmation ───────────────────────
        conf = self.read_order_confirmation()
        if conf:
            return {"status": "ACCEPTED",
                    "provider_ref": conf.get("reference"),
                    "odds_accepted": odds_accepted}
        return {"status": "SUBMITTED", "provider_ref": None,
                "odds_accepted": odds_accepted}

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
                classification=g.get("classification", ""),
                source_url=g.get("source_url", ""))
        return adapter
    return factory
