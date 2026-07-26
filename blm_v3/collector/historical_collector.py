"""
BLM V3 — Historical High-Frequency Collector.

Attaches to PokerBet via Playwright at configurable intervals (default 250ms)
and writes every market observation to ``blm_historical.db`` with derived
metrics (pace, movement deltas) computed at write time.

Architecture
------------
The collector runs Playwright in a daemon thread (same pattern as V1) at a
higher tick rate.  Each tick:

  1. Scrapes the visible page text via ``page.inner_text("body")``
  2. Parses game state (teams, score, clock, quarter, line, spread)
  3. Computes movement deltas (line/odds/spread change since last observation)
  4. Computes pace metrics (possessions/min, projected total)
  5. Writes the full historical snapshot to ``blm_historical.db``
  6. Updates the games table (upsert with snapshot count)

Rate limiting detection:
  - If the page DOM hasn't changed after N consecutive polls, increase interval
  - A ``MUTATION_OBSERVER`` mode checks for DOM mutations rather than timer
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

from blm_v3.collector.movement_tracker import MovementTracker
from blm_v3.collector.pace_calculator import compute_pace_metrics
from blm_v3.historical.config import (
    DEFAULT_COLLECT_INTERVAL_MS,
    FALLBACK_COLLECT_INTERVAL_MS,
)
from blm_v3.historical.database import HistoricalDatabase as _HistoricalDatabase
from blm_v3.historical.models import (
    HistoricalSnapshot,
    GameModel,
    GameStatus,
    _now_iso,
    _uuid7,
)

logger = logging.getLogger(__name__)

# ─── Constants ─────────────────────────────────────────────────────

POKERBET_URL: str = (
    "https://www.pokerbet.co.za/en/sports/live/event-view/Basketball/World/18295203/"
    "cyber-basketball-2k26-matches/30346555/denver-nuggets-cyber-houston-rockets-cyber"
)
"""Default PokerBet URL for Cyber Basketball 2K26."""

NAV_TIMEOUT_MS: int = 30000
"""Page navigation timeout."""

RATE_LIMIT_STRIKES: int = 5
"""Consecutive identical-state polls before degrading interval."""

DEGRADE_INTERVAL_MS: int = 2000
"""Maximum degraded interval when rate limiting is suspected."""

MAX_FROZEN_POLLS: int = 30
"""Consecutive polls with zero changes before stopping collector."""


# ── Text extraction ─────────────────────────────────────────────────


def extract_game_state(body_text: Optional[str]) -> Optional[dict[str, Any]]:
    """Parse visible PokerBet page text for Cyber Basketball game state.

    Returns a dict with keys matching ``HistoricalSnapshot`` fields, or
    ``None`` if no game data is found on the page.

    This is a refined version of ``blm_v1.collector.extract_game_state``
    with additional field extraction for odds and team totals.
    """
    if not body_text:
        return None

    lines = [l.strip() for l in body_text.split("\n") if l.strip()]

    home_team = away_team = None
    home_score = away_score = 0
    clock: Optional[str] = None
    quarter = 1
    total_line: Optional[float] = None
    spread: Optional[float] = None
    over_odds: Optional[float] = None
    under_odds: Optional[float] = None
    possession: Optional[str] = None

    # ── Team names + scores ──────────────────────────────────────
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

    # ── Quarter ──────────────────────────────────────────────────
    qmap = {
        "1st Quarter": 1, "2nd Quarter": 2, "3rd Quarter": 3, "4th Quarter": 4,
        "Half End": 2, "Half Time": 2, "Halftime": 2, "Half Time": 2,
    }
    for kw, q in qmap.items():
        if kw.lower() in body_text.lower():
            quarter = q
            break

    # ── Clock ────────────────────────────────────────────────────
    cm = re.search(r"\b(\d{1,2}:\d{2})\b", body_text)
    if cm:
        clock = cm.group(1)

    # ── Total line ───────────────────────────────────────────────
    total_section = re.search(
        r"Total Points\s*\n\s*(?:Over\s+Under\s+)?(\d{2,3}\.\d)\s+\d+\.\d+\s+\d+\.\d+",
        body_text,
    )
    if total_section:
        total_line = float(total_section.group(1))

    # ── Spread ───────────────────────────────────────────────────
    sm = re.search(r"Points Handicap.*?([+-]\d+\.\d+)", body_text, re.DOTALL)
    if sm:
        spread = float(sm.group(1))

    # ── Odds (decimal) ───────────────────────────────────────────
    # Total Points section often has: Total X.X  Over 1.91  Under 1.91
    odds_over = re.search(
        r"Total Points\s*\n\s*(?:Over\s+Under\s+)?\d+\.\d+\s+(\d+\.\d+)\s+(\d+\.\d+)",
        body_text,
    )
    if odds_over:
        try:
            over_odds = float(odds_over.group(1))
            under_odds = float(odds_over.group(2))
        except (ValueError, IndexError):
            pass

    return {
        "home_team": home_team,
        "away_team": away_team,
        "home_score": home_score,
        "away_score": away_score,
        "quarter": quarter,
        "clock": clock,
        "total_line": total_line,
        "spread": spread,
        "over_odds": over_odds,
        "under_odds": under_odds,
        "possession": possession,
    }


# ── Collector ───────────────────────────────────────────────────────


class HistoricalCollector:
    """High-frequency Playwright collector for the historical database.

    Runs in a daemon thread, scraping PokerBet at configurable intervals
    (default 250ms) and writing directly to ``blm_historical.db`` with
    all derived metrics pre-computed.

    Usage::

        collector = HistoricalCollector()
        collector.start()  # blocks until stopped

    Or in a thread::

        t = threading.Thread(target=collector.start, daemon=True)
        t.start()
    """

    def __init__(
        self,
        *,
        db_path: Optional[str] = None,
        interval_ms: int = DEFAULT_COLLECT_INTERVAL_MS,
        headless: bool = True,
        pokerbet_url: Optional[str] = None,
        historical_db: Optional[_HistoricalDatabase] = None,
    ):
        self._interval_ms = max(100, interval_ms)
        self._headless = headless
        self._url = pokerbet_url or POKERBET_URL

        # Database — injected or self-created
        if historical_db:
            self._db = historical_db
        else:
            from pathlib import Path
            dbp = Path(db_path) if db_path else None
            self._db = _HistoricalDatabase(db_path=dbp)

        # State tracking
        self._running = False
        self._latest_state: Optional[dict[str, Any]] = None
        self._snapshot_count = 0
        self._game_id: Optional[str] = None
        self._last_db_snapshot: Optional[dict[str, Any]] = None
        self._consecutive_nochange = 0
        self._current_interval_ms = self._interval_ms
        self._max_snapshots = 0  # 0 = unlimited

        # Thread handle
        self._thread: Optional[threading.Thread] = None

    # ── Properties ───────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def latest_state(self) -> Optional[dict[str, Any]]:
        return self._latest_state

    @property
    def snapshot_count(self) -> int:
        return self._snapshot_count

    @property
    def game_id(self) -> Optional[str]:
        return self._game_id

    @property
    def current_interval_ms(self) -> int:
        return self._current_interval_ms

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        """Start the collector loop.  Blocks until the game ends or
        ``stop()`` is called.

        Designed to run in a daemon thread::

            t = threading.Thread(target=collector.start, daemon=True)
            t.start()
        """
        if self._running:
            logger.warning("HistoricalCollector already running")
            return

        self._running = True
        self._db.ensure_initialized()

        # We re-import here to avoid forcing Playwright as a dependency
        # for modules that never collect (e.g. the API server)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error(
                "playwright is not installed.  "
                "Run: pip install playwright && playwright install chromium"
            )
            self._running = False
            return

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=self._headless,
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                )
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                    extra_http_headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-ZA,en;q=0.9",
                    },
                )
                page = context.new_page()

                logger.info("HistoricalCollector: navigating to %s", self._url)
                page.goto(self._url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)  # React hydration

                logger.info(
                    "HistoricalCollector: started at %dms interval",
                    self._current_interval_ms,
                )

                # ── Main loop ─────────────────────────────────
                while self._running:
                    tick_start = time.monotonic()

                    try:
                        text = page.inner_text("body", timeout=5000)
                        state = extract_game_state(text)

                        if state:
                            self._process_state(state)
                        else:
                            logger.debug("No game state on page — waiting")
                            self._consecutive_nochange += 1

                    except Exception:
                        logger.error("Tick error: %s", traceback.format_exc())
                        self._consecutive_nochange += 1

                    # ── Rate limiting detection ────────────────
                    if self._consecutive_nochange >= RATE_LIMIT_STRIKES:
                        old = self._current_interval_ms
                        self._current_interval_ms = min(
                            self._current_interval_ms * 2, DEGRADE_INTERVAL_MS
                        )
                        if old != self._current_interval_ms:
                            logger.warning(
                                "Rate limiting suspected: degraded interval to %dms",
                                self._current_interval_ms,
                            )

                    # ── Frozen market detection ────────────────
                    if self._consecutive_nochange >= MAX_FROZEN_POLLS:
                        logger.info(
                            "No changes after %d polls — stopping collector",
                            MAX_FROZEN_POLLS,
                        )
                        break

                    # ── Sleep for remaining interval ───────────
                    elapsed_ms = (time.monotonic() - tick_start) * 1000
                    sleep_ms = max(10, self._current_interval_ms - elapsed_ms)
                    time.sleep(sleep_ms / 1000.0)

                browser.close()

        except Exception:
            logger.error("Collector crashed: %s", traceback.format_exc())
        finally:
            self._running = False
            logger.info(
                "HistoricalCollector stopped: %d snapshots collected",
                self._snapshot_count,
            )

    def stop(self) -> None:
        """Signal the collector loop to stop."""
        self._running = False

    def start_in_thread(self) -> threading.Thread:
        """Start the collector in a daemon thread.  Returns the thread."""
        t = threading.Thread(target=self.start, daemon=True)
        t.name = "historical-collector"
        t.start()
        self._thread = t
        return t

    # ── State processor ─────────────────────────────────────────

    def _process_state(self, state: dict[str, Any]) -> None:
        """Process a scraped state: build the historical snapshot and write it."""
        ts = _now_iso()
        home = state.get("home_score", 0)
        away = state.get("away_score", 0)

        # Generate game ID on first observation
        if not self._game_id:
            self._game_id = (
                f"{state.get('home_team', 'Home')}-vs-"
                f"{state.get('away_team', 'Away')}-{ts[:10]}"
            )
        gid = self._game_id

        # Build the current snapshot dict
        curr = {
            "home_score": home,
            "away_score": away,
            "total_score": home + away,
            "score_difference": home - away,
            "quarter": state.get("quarter", 1),
            "clock": state.get("clock"),
            "total_line": state.get("total_line"),
            "total_line_raw": state.get("total_line"),
            "spread": state.get("spread"),
            "spread_raw": state.get("spread"),
            "over_odds": state.get("over_odds"),
            "under_odds": state.get("under_odds"),
            "possession": state.get("possession"),
        }

        # ── Movement deltas ────────────────────────────────────
        if self._last_db_snapshot is not None:
            deltas = MovementTracker.compute(self._last_db_snapshot, curr)
            curr.update(deltas)

            # Check for unchanged state
            if (
                deltas.get("line_delta") is not None
                and abs(deltas["line_delta"]) < 0.01
                and curr.get("home_score") == self._last_db_snapshot.get("home_score")
                and curr.get("away_score") == self._last_db_snapshot.get("away_score")
            ):
                self._consecutive_nochange += 1
            else:
                self._consecutive_nochange = 0
                self._current_interval_ms = self._interval_ms  # restore if degraded
        else:
            self._consecutive_nochange = 0

        # ── Pace metrics ───────────────────────────────────────
        if self._last_db_snapshot is not None:
            pace = compute_pace_metrics(self._last_db_snapshot, curr)
            curr.update(pace)
        else:
            curr["possessions"] = home + away
            curr["possessions_per_min"] = None
            curr["projected_possessions"] = None
            curr["projected_total"] = None

        # ── Build the full HistoricalSnapshot ──────────────────
        snapshot = HistoricalSnapshot(
            game_id=gid,
            timestamp=ts,
            quarter=curr.get("quarter", 1),
            clock=curr.get("clock"),
            possession=curr.get("possession"),
            home_score=home,
            away_score=away,
            score_difference=home - away,
            total_score=home + away,
            total_line=curr.get("total_line"),
            spread=curr.get("spread"),
            total_line_raw=curr.get("total_line_raw"),
            spread_raw=curr.get("spread_raw"),
            over_odds=curr.get("over_odds"),
            under_odds=curr.get("under_odds"),
            line_delta=curr.get("line_delta"),
            odds_delta=curr.get("odds_delta"),
            spread_delta=curr.get("spread_delta"),
            possessions=curr.get("possessions"),
            possessions_per_min=curr.get("possessions_per_min"),
            projected_possessions=curr.get("projected_possessions"),
            projected_total=curr.get("projected_total"),
            trap_meter=curr.get("trap_meter"),
            tt_modifier=curr.get("tt_modifier"),
            inflation_index=curr.get("inflation_index"),
            compression_index=curr.get("compression_index"),
            momentum=curr.get("momentum"),
            regression_prob=curr.get("regression_prob"),
            fair_total=curr.get("fair_total"),
            expected_total=curr.get("expected_total"),
            variance=curr.get("variance"),
            volatility=curr.get("volatility"),
            confidence=curr.get("confidence"),
            raw_json=json.dumps({
                "state": state,
                "computed": {
                    k: curr.get(k) for k in [
                        "line_delta", "odds_delta", "spread_delta",
                        "possessions_per_min", "projected_possessions",
                        "projected_total",
                    ]
                },
            }),
        )

        # ── Write to database ──────────────────────────────────
        snap_dict = snapshot.to_db_dict()

        try:
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                # Upsert game
                game = GameModel(
                    id=gid,
                    home_team=state.get("home_team", ""),
                    away_team=state.get("away_team", ""),
                    status=GameStatus.LIVE,
                )
                loop.run_until_complete(self._db.save_game(game.to_db_dict()))

                # Insert snapshot
                snap_id = loop.run_until_complete(
                    self._db.insert_snapshot(snap_dict)
                )

                # Update snapshot count on game
                cnt = loop.run_until_complete(self._db.count_snapshots(gid))
                game.total_snapshots = cnt
                loop.run_until_complete(self._db.save_game(game.to_db_dict()))
            finally:
                loop.close()
        except Exception:
            logger.error("DB write failed: %s", traceback.format_exc())
            return

        # Update tracking state
        self._last_db_snapshot = curr
        self._latest_state = state
        self._snapshot_count += 1

        if self._snapshot_count % 100 == 0:
            logger.info(
                "HistoricalCollector: %d snapshots | %s %d-%d %s | "
                "Q%d %s | Total=%s Spread=%s | %dms interval",
                self._snapshot_count,
                state.get("home_team", "?"), home, away,
                state.get("away_team", "?"),
                state.get("quarter", 1), state.get("clock", "?"),
                state.get("total_line", "?"), state.get("spread", "?"),
                self._current_interval_ms,
            )
