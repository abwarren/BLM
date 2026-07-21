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

import re

from playwright.sync_api import sync_playwright, Page, Browser

from blm_v1.database import upsert_game, insert_snapshot, get_live_game

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────

SCRAPE_INTERVAL = 1.0  # seconds between scrape attempts
POKERBET_URL = "https://www.pokerbet.co.za/en/sports/live/event-view/Basketball/World/18295203/cyber-basketball-2k26-matches/30346555/denver-nuggets-cyber-houston-rockets-cyber"
NAV_TIMEOUT = 30000
SELECTOR_TIMEOUT = 5000

# ── DOM Selectors (centralised for maintainability) ─────────────

# BetConstruct event view — text-based extraction patterns
# The page renders a React SPA; we extract from visible text.

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


def scrape_betconstruct_event(page: Page) -> Optional[dict]:
    """Scrape a BetConstruct event view page using visible text extraction.

    BetConstruct renders its event view as a React SPA with obfuscated class
    names, making traditional DOM selectors unreliable.  Instead we extract the
    page's visible text and parse it with patterns derived from known layouts.
    """
    try:
        body_text = page.inner_text("body", timeout=5000)
    except Exception:
        logger.debug("Could not extract body text")
        return None

    if not body_text:
        return None

    # ── Extract team names ──────────────────────────────────────────
    # Pattern: "HOME_TEAM\nSCORE\nAWAY_TEAM\nSCORE" near a half/quarter marker
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]

    home_team = None
    away_team = None
    home_score = 0
    away_score = 0
    clock = None
    quarter = 1
    total_line = None
    spread = None

    # Look for Cyber Basketball team names - they end with "Cyber" but aren't headers
    # Filter out section headers like "Cyber Basketball. 2K26 Matches" or "Cyber Basketball"
    cyber_teams = [l for l in lines if "Cyber" in l and len(l) < 60
                   and "Basketball" not in l and "2K26" not in l]
    # Typical pattern: HOME_TEAM, SCORE, AWAY_TEAM, SCORE in sequence
    for i, line in enumerate(lines):
        if line in cyber_teams and (i + 2 < len(lines)) and lines[i + 2] in cyber_teams:
            # Found two cyber teams 3 lines apart — likely home/away/score pattern
            home_team = lines[i]
            try:
                home_score = int(lines[i + 1])
            except (ValueError, IndexError):
                home_score = 0
            away_team = lines[i + 2]
            try:
                away_score = int(lines[i + 3])
            except (ValueError, IndexError):
                away_score = 0
            break

    if not home_team:
        logger.debug("Could not find Cyber team names in page text")
        return None

    # ── Extract quarter / period ───────────────────────────────────
    quarter_keywords = {
        "1st Quarter": 1, "2nd Quarter": 2, "3rd Quarter": 3, "4th Quarter": 4,
        "Quarter 1": 1, "Quarter 2": 2, "Quarter 3": 3, "Quarter 4": 4,
        "Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4,
        "Half End": 2, "Half Time": 2, "Halftime": 2,
    }
    for kw, q in quarter_keywords.items():
        if kw.lower() in body_text.lower():
            quarter = q
            break

    # ── Extract clock ──────────────────────────────────────────────
    # Look for MM:SS or M:SS pattern
    clock_match = re.search(r'\b(\d{1,2}:\d{2})\b', body_text)
    if clock_match:
        clock = clock_match.group(1)

    # ── Extract total line ─────────────────────────────────────────
    # Look for "Total Points" section then find nearby numbers
    # Pattern: number.5 Over Odds Under Odds
    total_section = re.search(
        r'Total Points.*?(?:Over\s*Under)?\s*(\d{3}\.?\d*)\s+(\d+\.\d+)\s+(\d+\.\d+)',
        body_text, re.DOTALL
    )
    if total_section:
        total_line = float(total_section.group(1))

    # ── Extract spread ─────────────────────────────────────────────
    # Look for "Points Handicap" section then find +XX.X or -XX.X
    spread_match = re.search(
        r'Points Handicap.*?([+-]\d+\.\d+)',
        body_text, re.DOTALL
    )
    if spread_match:
        spread = float(spread_match.group(1))

    logger.info(
        "Scraped: %s %d - %d %s | Q%d %s | Total=%.1f Spread=%s",
        home_team, home_score, away_score, away_team,
        quarter, clock or "?", total_line or 0, spread or "?",
    )

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
                        state = scrape_betconstruct_event(page)
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
