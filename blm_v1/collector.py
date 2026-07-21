"""
BLM V1 — Bandwidth-Optimized Playwright Snapshot Collector

Scrapes live Cyber Basketball 2K26 from PokerBet with aggressive request
blocking to minimise residential proxy bandwidth.

Follows the lean-scraper skill template.
"""

from __future__ import annotations

import json
import logging
import re
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from playwright.sync_api import sync_playwright, Browser, Page, Route, Request

from blm_v1.database import upsert_game, insert_snapshot, get_live_game

logger = logging.getLogger(__name__)

# ── Configuration ───────────────────────────────────────────────────

SCRAPE_INTERVAL = 1.0
POKERBET_URL = (
    "https://www.pokerbet.co.za/en/sports/live/event-view/Basketball/World/18295203/"
    "cyber-basketball-2k26-matches/30346555/denver-nuggets-cyber-houston-rockets-cyber"
)
NAV_TIMEOUT = 30000

# ── Resource types to always abort ─────────────────────────────────

ALWAYS_BLOCK = {"image", "font", "media", "manifest", "ping"}

# URLs containing any of these patterns get aborted (analytics, ads, tracking)
BLOCK_URL_PATTERNS = re.compile(
    r"(google-analytics|googletagmanager|hotjar|mixpanel|"
    r"facebook\.com|facebook\.net|connect\.facebook|fbcdn|"
    r"doubleclick|adsystem|adservice|scorecardresearch|"
    r"amazon-adsystem|moatads|criteo|taboola|outbrain|"
    r"analytics\.|tracking\.|pixel\.|beacon\.|bat\.bing\.com)", re.I
)


# ── Bandwidth tracking ──────────────────────────────────────────────

class BandwidthTracker:
    """Track downloaded bytes by resource type for one scrape tick."""

    def __init__(self):
        self.reset()

    def reset(self):
        self._counts: dict[str, int] = defaultdict(int)
        self._bytes: dict[str, int] = defaultdict(int)
        self._blocked_count = 0
        self._blocked_bytes = 0

    def record(self, resource_type: str, size: int):
        self._counts[resource_type] += 1
        self._bytes[resource_type] += size

    def record_blocked(self, size: int):
        self._blocked_count += 1
        self._blocked_bytes += size

    @property
    def total_kb(self) -> float:
        return sum(self._bytes.values()) / 1024

    @property
    def saved_kb(self) -> float:
        return self._blocked_bytes / 1024

    def summary(self) -> str:
        return (
            f"DL={self.total_kb:.0f}KB saved={self.saved_kb:.0f}KB "
            f"reqs={sum(self._counts.values())} blocked={self._blocked_count}"
        )


_tracker = BandwidthTracker()


# ── Request interception handler ────────────────────────────────────

def _handle_route(route: Route, request: Request) -> None:
    """Intercept every request — block images, fonts, media, tracking."""
    url = request.url
    rtype = request.resource_type

    # Block images, fonts, media, manifests, pings unconditionally
    if rtype in ALWAYS_BLOCK:
        _tracker.record_blocked(len(request.headers.get("content-length", "0")))
        route.abort("blockedbyclient")
        return

    # Block analytics / ad / tracking URLs regardless of resource type
    if BLOCK_URL_PATTERNS.search(url):
        _tracker.record_blocked(len(request.headers.get("content-length", "0")))
        route.abort("blockedbyclient")
        return

    # Block third-party stylesheets (allow first-party)
    if rtype == "stylesheet" and "pokerbet" not in url and "betconstruct" not in url:
        _tracker.record_blocked(len(request.headers.get("content-length", "0")))
        route.abort("blockedbyclient")
        return

    # Allow everything else — JS, XHR, document, fetch, websocket, first-party CSS
    route.continue_()


# ── Text-based extraction ───────────────────────────────────────────

def extract_game_state(body_text: str) -> Optional[dict[str, Any]]:
    """Parse visible page text for Cyber Basketball scores, total line, spread."""
    if not body_text:
        return None

    lines = [l.strip() for l in body_text.split("\n") if l.strip()]

    home_team = away_team = None
    home_score = away_score = 0
    clock = None
    quarter = 1
    total_line = spread = None

    # Find two Cyber team names 3 lines apart (team / score / team / score)
    cyber = [
        l for l in lines
        if "Cyber" in l and len(l) < 60 and "Basketball" not in l and "2K26" not in l
    ]
    for i, line in enumerate(lines):
        if line in cyber and i + 3 < len(lines) and lines[i + 2] in cyber:
            home_team = lines[i]
            try:
                home_score = int(lines[i + 1])
            except (ValueError, IndexError):
                pass
            away_team = lines[i + 2]
            try:
                away_score = int(lines[i + 3])
            except (ValueError, IndexError):
                pass
            break

    if not home_team:
        return None

    # Quarter
    qmap = {"1st Quarter": 1, "2nd Quarter": 2, "3rd Quarter": 3, "4th Quarter": 4,
            "Half End": 2, "Half Time": 2, "Halftime": 2}
    for kw, q in qmap.items():
        if kw.lower() in body_text.lower():
            quarter = q
            break

    # Clock
    cm = re.search(r'\b(\d{1,2}:\d{2})\b', body_text)
    if cm:
        clock = cm.group(1)

    # Total line — look for "Total Points" section with valid over/under
    # Must match X.X or XXX format, not the match-winner-and-total combo markets
    total_section = re.search(
        r'Total Points\s*\n\s*(?:Over\s+Under\s+)?(\d{2,3}\.\d)\s+\d+\.\d+\s+\d+\.\d+',
        body_text
    )
    if total_section:
        total_line = float(total_section.group(1))

    # Spread
    sm = re.search(r'Points Handicap.*?([+-]\d+\.\d+)', body_text, re.DOTALL)
    if sm:
        spread = float(sm.group(1))

    return {
        "home_team": home_team, "away_team": away_team,
        "home_score": home_score, "away_score": away_score,
        "quarter": quarter, "clock": clock,
        "total_line": total_line, "spread": spread,
    }


# ── Collector ───────────────────────────────────────────────────────

class SnapshotCollector:
    """Bandwidth-optimised Playwright collector for PokerBet Cyber games."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._running = False
        self._browser: Optional[Browser] = None
        self._latest_state: Optional[dict] = None
        self._snapshot_count = 0
        self._game_id: Optional[str] = None

    @property
    def latest_state(self) -> Optional[dict]:
        return self._latest_state

    @property
    def snapshot_count(self) -> int:
        return self._snapshot_count

    @property
    def game_id(self) -> Optional[str]:
        return self._game_id

    def start(self) -> None:
        self._running = True
        try:
            with sync_playwright() as pw:
                self._browser = pw.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                )
                context = self._browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                    extra_http_headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-ZA,en;q=0.9",
                        "Accept-Encoding": "gzip, deflate, br",
                    },
                )
                page = context.new_page()
                page.route("**/*", _handle_route)

                # Navigate once — reuse page for all ticks
                _tracker.reset()
                t0 = time.monotonic()
                page.goto(POKERBET_URL, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
                logger.info(
                    "Navigated (%.1fs) | %s",
                    time.monotonic() - t0, _tracker.summary(),
                )
                page.wait_for_timeout(2000)  # React hydration

                while self._running:
                    try:
                        text = page.inner_text("body", timeout=5000)
                        state = extract_game_state(text)
                        if state:
                            self._store_snapshot(state)
                            logger.info(
                                "%s %d-%d %s | Q%d %s | Total=%s Spread=%s | %s",
                                state["home_team"], state["home_score"], state["away_score"],
                                state["away_team"], state["quarter"], state.get("clock", "?"),
                                state.get("total_line", "?"), state.get("spread", "?"),
                                _tracker.summary(),
                            )
                        else:
                            logger.debug("No game state — waiting")
                    except Exception:
                        logger.error("Tick error: %s", traceback.format_exc())
                    time.sleep(SCRAPE_INTERVAL)

        except Exception:
            logger.error("Collector crashed: %s", traceback.format_exc())
        finally:
            if self._browser:
                self._browser.close()

    def _store_snapshot(self, state: dict) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        gid = self._game_id or f"{state['home_team']}-vs-{state['away_team']}-{ts[:10]}"
        if not self._game_id:
            self._game_id = gid

        upsert_game(game_id=gid, home=state["home_team"], away=state["away_team"])
        insert_snapshot(
            game_id=gid, ts=ts, quarter=state["quarter"],
            clock=state.get("clock"),
            home_score=state["home_score"], away_score=state["away_score"],
            total_line=state.get("total_line"), spread=state.get("spread"),
        )
        self._latest_state = {**state, "timestamp": ts, "game_id": gid}
        self._snapshot_count += 1

    def stop(self) -> None:
        self._running = False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    c = SnapshotCollector()
    try:
        c.start()
    except KeyboardInterrupt:
        c.stop()
