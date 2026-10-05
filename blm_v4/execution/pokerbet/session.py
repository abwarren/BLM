"""BLM EXECUTION — POKERBET SESSION (authentication + game URLs).

Authentication detection ONLY: BLM inspects the operator's already-
authenticated browser to answer "can I see the bookmaker signed-in?".
It never performs, assists or bypasses a login.

Also owns the canonical event-view URL for a game record — the same
``source_url`` shape the collector records, so the adapter can navigate
straight to a game by id.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote

from blm_v4.execution.pokerbet.browser import CdpBrowser

_BASE = "https://www.pokerbet.co.za"
# LEGACY fallback shape ONLY — never used when the game record carries its
# recorded ``source_url``.  This hardcodes the competition id ``1`` and an
# ``x`` tail and is NOT reliable; ``event_view_url_for`` is the canonical
# resolver (live-verified 2026-10-05: the real path is
# ``…/event-view/<Sport>/<Region>/<comp_id>/<slug>/<event_id>/<name>``).
EVENT_VIEW_URL = (
    _BASE + "/en/sports/live/event-view/Basketball/World/1/"
    "{classification_slug}/{game_id}/x")
RESULTS_URL = _BASE + "/en/sports/results?game={game_id}"


def event_view_url(game_id: str, classification: Optional[str] = None) -> str:
    """LEGACY fallback URL shape (hardcodes competition id + ``x`` tail).

    Kept for backward compatibility / tests only.  Real navigation MUST use
    ``event_view_url_for`` with the game's recorded ``source_url``.
    """
    slug = re.sub(
        r"[^a-z0-9]+", "-", (classification or "betual-nba").lower()
    ).strip("-")
    return EVENT_VIEW_URL.format(
        classification_slug=slug, game_id=quote(str(game_id)))


def event_view_url_for(record: dict) -> Optional[str]:
    """Canonical navigation URL: the RECORDED ``source_url`` (never rebuilt).

    Returns None when the record carries no source_url, so the adapter FAILS
    CLOSED instead of navigating to a fabricated endpoint.
    """
    su = str((record or {}).get("source_url") or "").strip()
    return su or None


def results_url(game_id: str) -> str:
    """The authoritative completed-results URL for a game id."""
    return RESULTS_URL.format(game_id=quote(str(game_id)))


def detect_authenticated(browser: CdpBrowser) -> bool:
    """True when the attached browser shows a signed-in PokerBet session.

    Detection is passive: the SIGN IN control being ABSENT is treated as
    authenticated.  No navigation is performed and no credential is ever
    handled — if the session is not signed in, the answer is simply
    ``False`` and the adapter refuses to proceed.
    """
    page = browser.page()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8_000)
    except Exception:
        pass
    try:
        signin = page.locator("text=/^(sign in|log ?in|register)$/i")
        return signin.count() == 0
    except Exception:
        return False
