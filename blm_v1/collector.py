"""
BLM V1 — Playwright Snapshot Collector

Scrapes live Cyber Basketball 2K26 from PokerBet and writes snapshots to SQLite.
Runs in a background thread.
"""

import json
import logging
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

from playwright.sync_api import sync_playwright, Page, Browser

from blm_v1.database import upsert_game, insert_snapshot, get_live_game

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────

SCRAPE_INTERVAL = 1.0  # seconds between scrape attempts
POKERBET_URL = "https://www.pokerbet.co.za/sports/basketball/cyber-basketball"
NAV_TIMEOUT = 30000
SELECTOR_TIMEOUT = 5000

# ── DOM Selectors (centralised for maintainability) ─────────────

SEL = {
    "game_cards": '[class*="game-card"], [class*="match-card"], [data-testid*="game"], article',
    "home_team": '[class*="home"] [class*="name"], [class*="participant"]:first-child [class*="name"]',
    "away_team": '[class*="away"] [class*="name"], [class*="participant"]:last-child [class*="name"]',
    "home_score": '[class*="home"] [class*="score"], [class*="score"]:nth-child(1)',
    "away_score": '[class*="away"] [class*="score"], [class*="score"]:nth-child(2)',
    "clock": '[class*="clock"], [class*="timer"], [class*="period"]',
    "quarter": '[class*="quarter"], [class*="period"]',
    "total_line": '[class*="total"], [class*="over-under"]',
    "spread": '[class*="spread"], [class*="handicap"]',
}


def extract_text(page: Page, selector: str, timeout: int = 3000) -> Optional[str]:
    """Safely extract text from a DOM element."""
    try:
        el = page.query_selector(selector)
        if el:
            return el.inner_text(timeout=timeout).strip()
    except Exception:
        pass
    return None


def extract_int(page: Page, selector: str) -> Optional[int]:
    val = extract_text(page, selector)
    if val:
        try:
            return int(''.join(c for c in val if c.isdigit() or c == '-'))
        except ValueError:
            pass
    return None


def extract_float(page: Page, selector: str) -> Optional[float]:
    val = extract_text(page, selector)
    if val:
        try:
            return float(val.replace(',', ''))
        except ValueError:
            pass
    return None


def scrape_game_state(page: Page) -> Optional[dict]:
    """
    Attempt to scrape the current game state from the page.
    Returns a dict with keys matching the snapshot schema, or None.
    """
    try:
        # Try to find the game card
        game_card = page.query_selector(SEL["game_cards"])
        if not game_card:
            logger.debug("No game card found on page")
            return None

        home_team = extract_text(page, SEL["home_team"]) or "Home"
        away_team = extract_text(page, SEL["away_team"]) or "Away"
        home_score = extract_int(page, SEL["home_score"]) or 0
        away_score = extract_int(page, SEL["away_score"]) or 0
        clock = extract_text(page, SEL["clock"])
        quarter_text = extract_text(page, SEL["quarter"])

        quarter = 1
        if quarter_text:
            for q in range(1, 5):
                if str(q) in quarter_text:
                    quarter = q
                    break

        total_line = extract_float(page, SEL["total_line"])
        spread = extract_float(page, SEL["spread"])

        return {
            "home_team": home_team,
            "away_team": away_team,
            "home_score": home_score,
            "away_score": away_score,
            "quarter": quarter,
            "clock": clock,
            "total_line": total_line,
            "spread": spread,
        }
    except Exception as e:
        logger.warning(f"Scrape failed: {e}")
        return None


class SnapshotCollector:
    """Continuously scrapes PokerBet and stores snapshots."""

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

    def start(self):
        self._running = True
        try:
            with sync_playwright() as p:
                self._browser = p.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-gpu"]
                )
                context = self._browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    )
                )
                page = context.new_page()
                page.goto(POKERBET_URL, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
                logger.info(f"Navigated to {POKERBET_URL}")

                while self._running:
                    try:
                        state = scrape_game_state(page)
                        if state:
                            self._store_snapshot(state)
                        else:
                            logger.debug("No game state scraped — waiting")
                    except Exception:
                        logger.error(f"Scrape error: {traceback.format_exc()}")

                    time.sleep(SCRAPE_INTERVAL)

        except Exception:
            logger.error(f"Collector crashed: {traceback.format_exc()}")
        finally:
            if self._browser:
                self._browser.close()

    def _store_snapshot(self, state: dict):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        # Derive a game ID from team names
        game_id = self._game_id or f"{state['home_team']}-vs-{state['away_team']}-{ts[:10]}"
        if not self._game_id:
            self._game_id = game_id

        upsert_game(
            game_id=game_id,
            home=state["home_team"],
            away=state["away_team"],
        )
        insert_snapshot(
            game_id=game_id,
            ts=ts,
            quarter=state["quarter"],
            clock=state.get("clock"),
            home_score=state["home_score"],
            away_score=state["away_score"],
            total_line=state.get("total_line"),
            spread=state.get("spread"),
        )

        self._latest_state = {**state, "timestamp": ts, "game_id": game_id}
        self._snapshot_count += 1

    def stop(self):
        self._running = False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    collector = SnapshotCollector()
    try:
        collector.start()
    except KeyboardInterrupt:
        collector.stop()
        logger.info("Collector stopped")
