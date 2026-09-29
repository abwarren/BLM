"""BLM EXECUTION — CDP BROWSER BRIDGE (phase ③).

Playwright over CDP, attaching to the OPERATOR's own already-
authenticated Chrome.  The authentication contract is absolute:

  * BLM never reads, stores, types or automates PokerBet credentials;
  * BLM never creates a second login path;
  * the session's authenticated cookies live in the operator's browser
    profile — BLM only ATTACHES to it via the CDP endpoint and reuses
    the context as-is.

The bridge is lazy and process-wide (one browser attachment per
process); ``close`` disconnects Playwright but never closes the
operator's browser or tabs beyond the ones BLM itself opened.
"""
from __future__ import annotations

import threading
from typing import Optional

from blm_v4.execution.adapter import AdapterUnavailable
from blm_v4.execution.config import ExecutionConfig


class CdpBrowser:
    """Attach to the operator's Chrome over CDP and hand out pages.

    ``connect`` raises ``AdapterUnavailable`` when the CDP endpoint is
    unreachable (browser closed / wrong port) — the engine's existing
    BROWSER_DISCONNECTED terminal path.
    """

    name = "pokerbet_cdp"

    def __init__(self, cdp_url: Optional[str] = None):
        self._cdp_url = (cdp_url or "").strip()
        self._lock = threading.Lock()
        self._pw = None                  # playwright instance
        self._browser = None             # attached browser
        self._context = None             # the operator's default context

    # ── lifecycle ─────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    def connect(self) -> "CdpBrowser":
        """Attach over CDP (idempotent).  Never launches a browser and
        never performs any login — if the operator's session is not
        authenticated, ``session`` reports that and nothing proceeds."""
        with self._lock:
            if self.connected:
                return self
            url = self._cdp_url or ExecutionConfig().cdp_url
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as e:              # pragma: no cover
                raise AdapterUnavailable(
                    "playwright not installed") from e
            try:
                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.connect_over_cdp(
                    url, timeout=10_000)
                # connect_over_cdp reuses the browser's DEFAULT context —
                # the operator's own profile, cookies and login state.
                contexts = self._browser.contexts
                self._context = contexts[0] if contexts else \
                    self._browser.new_context()
            except Exception as e:
                self._teardown()
                raise AdapterUnavailable(
                    f"CDP endpoint unreachable at {url}: {e}") from e
            return self

    def close(self) -> None:
        """Disconnect Playwright.  The operator's browser keeps running
        (we attached; we did not launch it)."""
        with self._lock:
            self._teardown()

    def _teardown(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._context = None
        self._pw = None

    # ── pages ─────────────────────────────────────────────────────────
    def page(self):
        """A live page in the operator's authenticated context — an
        existing PokerBet tab when one is open, otherwise a new tab."""
        self.connect()
        for p in self._context.pages:
            if "pokerbet.co.za" in (p.url or ""):
                return p
        return self._context.new_page()

    @property
    def context(self):
        self.connect()
        return self._context
