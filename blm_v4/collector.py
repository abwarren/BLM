"""
BLM V4 — PokerBet Resilient Collector.

Playwright orchestrator that:
  1. Connects to PokerBet (BetConstruct SPA)
  2. Discovers live basketball games from the live panel
  3. Classifies each as CYBER_2K26 or BETUAL_NBA from the actual
     PokerBet/BetConstruct taxonomy (competition section + URL)
  4. Resolves durable identity: source=PokerBet + source_game_id
     (the BetConstruct event ID from the event-view URL)
  5. Captures market state (list-level every tick; full event-view
     markets on rotation) and persists timestamped snapshots
  6. Reconciles each captured game against its BetConstruct URL
     taxonomy + rendered state
  7. Tracks games across ticks — handles refreshes, odds movement,
     completion, new/disappearing games, duplicate discovery, URL
     changes and transient network failures

Run standalone:  python -m blm_v4.collector [--once] [--tick 20]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Browser, Page, sync_playwright

from blm_v4.classifications import (
    BETUAL_COMPETITION,
    CYBER_COMPETITION,
    Classification,
    canonical_competition_name,
    classify_event_url,
    parse_event_url,
    slugify_team,
)
from blm_v4.discovery import (
    RowGame,
    discover_competitions,
    find_relevant_competitions,
)
from blm_v4.event_parser import parse_event_view
from blm_v4.models import (
    SOURCE_POKERBET,
    MarketObservation,
    PokerBetGame,
    utcnow_iso,
)
from blm_v4.reconcile import reconcile_event
from blm_v4.storage import PokerBetStore

logger = logging.getLogger("blm_v4.collector")

LIVE_BASE = "https://www.pokerbet.co.za/en/sports/live"
BASKETBALL_LIVE_URL = f"{LIVE_BASE}/Basketball"

DEFAULT_COMP_IDS: dict[str, str] = {
    Classification.CYBER_2K26.value: "18295203",
    Classification.BETUAL_NBA.value: "18296756",
}
DEFAULT_COMP_SLUGS: dict[str, str] = {
    Classification.CYBER_2K26.value: "cyber-basketball-2k26-matches",
    Classification.BETUAL_NBA.value: "betual-nba",
}
DEFAULT_REGIONS: dict[str, str] = {
    Classification.CYBER_2K26.value: "World",
    Classification.BETUAL_NBA.value: "Virtual%20Matches",
}

NAV_TIMEOUT = 45000
PANEL_WAIT_S = 8.0
ENDED_GRACE_TICKS = 3

STATE_DIR = Path(__file__).resolve().parent / "state"
COMP_IDS_FILE = STATE_DIR / "comp_ids.json"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def competition_url(cls: Classification, comp_ids: dict[str, str]) -> str:
    key = cls.value
    cid = comp_ids.get(key) or DEFAULT_COMP_IDS[key]
    slug = DEFAULT_COMP_SLUGS[key]
    region = DEFAULT_REGIONS[key]
    return f"{LIVE_BASE}/event-view/Basketball/{region}/{cid}/{slug}/"


def load_comp_ids() -> dict[str, str]:
    try:
        if COMP_IDS_FILE.exists():
            data = json.loads(COMP_IDS_FILE.read_text())
            return {
                k: str(v) for k, v in data.items() if k in DEFAULT_COMP_IDS
            }
    except Exception:
        logger.exception("comp_ids load failed")
    return dict(DEFAULT_COMP_IDS)


def save_comp_ids(comp_ids: dict[str, str]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        COMP_IDS_FILE.write_text(json.dumps({
            **comp_ids, "updated_at": utcnow_iso(),
        }, indent=2))
    except Exception:
        logger.exception("comp_ids save failed")


class PokerBetCollector:
    """Resilient multi-game PokerBet collector."""

    def __init__(
        self,
        store: Optional[PokerBetStore] = None,
        *,
        headless: bool = True,
        tick_s: float = 20.0,
        db_path: Optional[Path] = None,
    ):
        self.headless = headless
        self.tick_s = tick_s
        self.store = store or PokerBetStore(
            db_path or Path(__file__).resolve().parent.parent / "blm_pokerbet.db",
        )
        self.comp_ids = load_comp_ids()

        # tracked games: classification -> team-key -> PokerBetGame
        self._tracked: dict[str, dict[str, PokerBetGame]] = {
            cls.value: {} for cls in (Classification.CYBER_2K26, Classification.BETUAL_NBA)
        }
        self._unseen_ticks: dict[str, dict[str, int]] = {
            cls.value: {} for cls in (Classification.CYBER_2K26, Classification.BETUAL_NBA)
        }
        self._market_queue: list[str] = []   # round-robin of source_game_ids
        self._running = False
        self._browser: Optional[Browser] = None
        self.stats = {
            "ticks": 0, "games_seen": 0, "snapshots": 0,
            "games_resolved": 0, "reconciliations": 0, "errors": 0,
        }

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            with sync_playwright() as pw:
                self._browser = pw.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                )
                context = self._browser.new_context(
                    viewport={"width": 1600, "height": 900},
                    user_agent=_USER_AGENT,
                    locale="en-ZA",
                    extra_http_headers={
                        "Accept-Language": "en-ZA,en;q=0.9",
                    },
                )
                page = context.new_page()
                self._ensure_discovery_page(page)
                while self._running:
                    tick_start = time.monotonic()
                    try:
                        self._tick(page)
                    except Exception:
                        self.stats["errors"] += 1
                        logger.error("tick error:\n%s", traceback.format_exc())
                        self._recover(page)
                    elapsed = time.monotonic() - tick_start
                    sleep_for = self.tick_s - elapsed
                    if sleep_for > 0:
                        time.sleep(sleep_for)
        except Exception:
            logger.error("collector crashed:\n%s", traceback.format_exc())
        finally:
            if self._browser:
                try:
                    self._browser.close()
                except Exception:
                    pass
            self._running = False

    def stop(self) -> None:
        self._running = False

    # ── Navigation helpers ───────────────────────────────────────

    def _goto(self, page: Page, url: str) -> bool:
        try:
            page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)  # React hydration
            return True
        except Exception:
            logger.error("goto failed %s:\n%s", url, traceback.format_exc())
            return False

    def _wait_panel(self, page: Page) -> bool:
        try:
            page.wait_for_selector(
                ".market-game-section, .sp-s-l-head-bc",
                timeout=15000,
            )
            return True
        except Exception:
            return False

    def _ensure_discovery_page(self, page: Page) -> None:
        """Land on a live page whose left panel renders all live games."""
        url = competition_url(Classification.CYBER_2K26, self.comp_ids)
        if not self._goto(page, url):
            url = BASKETBALL_LIVE_URL
            self._goto(page, url)
        self._wait_panel(page)

    def _recover(self, page: Page) -> None:
        """After a tick error, re-establish the discovery page."""
        try:
            self._ensure_discovery_page(page)
        except Exception:
            logger.error("recover failed:\n%s", traceback.format_exc())

    # ── Main tick ────────────────────────────────────────────────

    def _tick(self, page: Page) -> None:
        self.stats["ticks"] += 1
        logger.info("tick %d start (url=%s)", self.stats["ticks"], page.url)

        # 1. Parse the live panel (all competitions + game rows)
        html = page.content()
        comps = find_relevant_competitions(html)
        if not comps:
            logger.warning("no relevant competitions found — refreshing page")
            self._ensure_discovery_page(page)
            html = page.content()
            comps = find_relevant_competitions(html)
        if not comps:
            logger.warning("still no relevant competitions on page")
            return

        seen_keys: dict[str, set[str]] = {
            comp.classification.value: set() for comp in comps
        }

        # 2. Process games per competition
        for comp in comps:
            cls = comp.classification
            for row in comp.games:
                key = f"{row.home_team}|{row.away_team}"
                seen_keys[cls.value].add(key)
                game = self._tracked[cls.value].get(key)
                if game is None:
                    # new game → resolve durable identity via click
                    resolved = self._resolve_new_game(page, comp, row)
                    if resolved is None:
                        continue
                    game, event_text = resolved
                    # full market snapshot from the event page we're on
                    self._capture_event_state(page, cls, game, event_text)
                    # return to discovery page
                    self._goto(page, competition_url(cls, self.comp_ids))
                    self._wait_panel(page)
                else:
                    self._store_list_snapshot(game, row, cls)

        # 3. Round-robin full event-view capture for tracked games
        self._capture_next_market(page)

        # 4. Mark unseen games ended
        self._mark_ended(seen_keys)

        self.stats["games_seen"] = sum(len(v) for v in self._tracked.values())
        logger.info(
            "tick %d done: tracked=%d snapshots=%d errors=%d",
            self.stats["ticks"], self.stats["games_seen"],
            self.stats["snapshots"], self.stats["errors"],
        )

    # ── Discovery & identity ─────────────────────────────────────

    def _resolve_new_game(
        self, page: Page, comp, row: RowGame,
    ) -> Optional[tuple[PokerBetGame, str]]:
        """Click the game row → read the event-view URL → build identity.

        Returns (PokerBetGame, event_page_text) or None on failure.
        """
        try:
            # find the row element by team names
            el = page.evaluate(
                """(home, away) => {
                    const rows = document.querySelectorAll('.market-game-section');
                    for (const r of rows) {
                        const names = [...r.querySelectorAll('.market-game-team-name')]
                            .map(n => n.textContent.trim());
                        if (names.length >= 2 && names[0] === home && names[1] === away) {
                            return true;
                        }
                    }
                    return false;
                }""",
                row.home_team, row.away_team,
            )
            if not el:
                logger.warning("row not found for %s vs %s", row.home_team, row.away_team)
                return None
            page.evaluate(
                """(home, away) => {
                    const rows = document.querySelectorAll('.market-game-section');
                    for (const r of rows) {
                        const names = [...r.querySelectorAll('.market-game-team-name')]
                            .map(n => n.textContent.trim());
                        if (names.length >= 2 && names[0] === home && names[1] === away) {
                            r.click();
                            return;
                        }
                    }
                }""",
                row.home_team, row.away_team,
            )
            page.wait_for_timeout(2500)
            url = page.url
            tax = parse_event_url(url)
            if not tax:
                logger.warning("no event taxonomy after click: %s", url)
                return None
            game = self._build_game(comp, row, tax, url)
            gid = self.store.upsert_game(game)
            self._tracked[comp.classification.value][
                f"{row.home_team}|{row.away_team}"
            ] = game
            self._unseen_ticks[comp.classification.value][
                f"{row.home_team}|{row.away_team}"
            ] = 0
            self._market_queue.append(game.source_game_id)
            self.stats["games_resolved"] += 1
            logger.info(
                "resolved new %s game %s (%s vs %s) game_id=%s",
                comp.classification.value, gid, row.home_team, row.away_team,
                tax["game_id"],
            )
            text = page.inner_text("body", timeout=10000)
            return game, text
        except Exception:
            logger.error("resolve failed:\n%s", traceback.format_exc())
            return None

    def _build_game(self, comp, row: RowGame, tax: dict, url: str) -> PokerBetGame:
        cls = comp.classification
        return PokerBetGame(
            source=SOURCE_POKERBET,
            source_game_id=tax["game_id"],
            competition_id=tax["competition_id"],
            competition_slug=tax["competition_slug"],
            competition=canonical_competition_name(cls),
            region=tax["region"],
            game_family=cls.game_family.value,
            classification=cls.value,
            sport=tax["sport"].lower(),
            home_team=row.home_team,
            away_team=row.away_team,
            game_slug=tax["game_slug"],
            source_url=url,
            status="live",
        )

    # ── Snapshot capture ─────────────────────────────────────────

    def _store_list_snapshot(self, game: PokerBetGame, row: RowGame, comp) -> None:
        """Persist the list-level observation for a known game."""
        obs = MarketObservation(
            source=SOURCE_POKERBET,
            source_game_id=game.source_game_id,
            classification=game.classification,
            captured_at=utcnow_iso(),
            home_team=row.home_team or game.home_team,
            away_team=row.away_team or game.away_team,
            home_score=row.home_score,
            away_score=row.away_score,
            period_label=row.period_label,
            clock=row.clock,
            game_status=self._infer_status(row.period_label),
            w1_odds=row.w1_odds,
            w2_odds=row.w2_odds,
            spread_indicator=row.spread_indicator,
            source_url=game.source_url,
            raw_json=json.dumps(row.__dict__, default=str),
        )
        row_id = self.store.insert_snapshot(self._game_db_id(game), obs)
        if row_id:
            self.stats["snapshots"] += 1
        self._unseen_ticks[game.classification][
            f"{row.home_team}|{row.away_team}"
        ] = 0

    def _capture_event_state(
        self, page: Page, cls: Classification, game: PokerBetGame, event_text: str,
    ) -> None:
        """Capture full event-view market state + reconcile."""
        try:
            parsed = parse_event_view(event_text)
            obs = MarketObservation(
                source=SOURCE_POKERBET,
                source_game_id=game.source_game_id,
                classification=game.classification,
                captured_at=utcnow_iso(),
                home_team=parsed["home_team"] or game.home_team,
                away_team=parsed["away_team"] or game.away_team,
                home_score=parsed["home_score"],
                away_score=parsed["away_score"],
                period_label=parsed["period_label"],
                quarter=parsed["quarter"],
                clock=parsed["clock"],
                game_status=self._infer_status(parsed["period_label"]),
                total_line=parsed["total"].get("first_line") if parsed["total"] else None,
                total_over_odds=(
                    parsed["total"].get("over_odds") if parsed["total"] else None
                ),
                total_under_odds=(
                    parsed["total"].get("under_odds") if parsed["total"] else None
                ),
                spread=(
                    parsed["handicap"].get("first_home_line")
                    if parsed["handicap"] else None
                ),
                spread_home_odds=(
                    parsed["handicap"].get("first_home_odds")
                    if parsed["handicap"] else None
                ),
                spread_away_odds=(
                    parsed["handicap"].get("first_away_odds")
                    if parsed["handicap"] else None
                ),
                home_total_line=(
                    parsed["team_totals"].get(game.home_team, {}).get("line")
                    or self._first_team_total(parsed, 0)
                ),
                away_total_line=(
                    parsed["team_totals"].get(game.away_team, {}).get("line")
                    or self._first_team_total(parsed, 1)
                ),
                w1_odds=(
                    parsed["match_winner"].get("home_odds")
                    if parsed["match_winner"] else None
                ),
                w2_odds=(
                    parsed["match_winner"].get("away_odds")
                    if parsed["match_winner"] else None
                ),
                source_url=game.source_url,
                markets_json=parsed["markets_json"],
                raw_json=parsed["raw_json"],
            )
            row_id = self.store.insert_snapshot(self._game_db_id(game), obs)
            if row_id:
                self.stats["snapshots"] += 1
            self._reconcile(game, page.url, event_text, parsed)
        except Exception:
            logger.error("event capture failed:\n%s", traceback.format_exc())

    @staticmethod
    def _first_team_total(parsed: dict, index: int) -> Optional[float]:
        vals = [v.get("line") for v in parsed["team_totals"].values()]
        if index < len(vals):
            return vals[index]
        return None

    def _capture_next_market(self, page: Page) -> None:
        """Visit the next tracked game's event view (round-robin)."""
        if not self._market_queue:
            return
        gid = self._market_queue.pop(0)
        self._market_queue.append(gid)
        game = self._find_tracked(gid)
        if game is None or not game.source_url:
            return
        logger.info("capturing event view for game %s", gid)
        if not self._goto(page, game.source_url):
            return
        if not self._wait_panel(page):
            logger.warning("event page for %s didn't render", gid)
            return
        try:
            text = page.inner_text("body", timeout=10000)
            parsed = parse_event_view(text)
            # refresh identity from the URL taxonomy (URL changes handled)
            tax = parse_event_url(page.url)
            if tax:
                game.source_game_id = tax["game_id"]
                game.competition_id = tax["competition_id"]
                game.competition_slug = tax["competition_slug"]
                game.game_slug = tax["game_slug"]
                game.source_url = page.url
                cls = classify_event_url(page.url)
                if cls != Classification.UNKNOWN:
                    game.classification = cls.value
                    game.game_family = cls.game_family.value
                self.store.upsert_game(game)
            self._capture_event_state(page, Classification(game.classification), game, text)
        except Exception:
            logger.error("market capture failed:\n%s", traceback.format_exc())
        # return to the discovery page for the next tick
        self._goto(page, competition_url(
            Classification(game.classification), self.comp_ids,
        ))
        self._wait_panel(page)

    def _reconcile(self, game: PokerBetGame, url: str, page_text: str, parsed: dict) -> None:
        try:
            rec = reconcile_event(url, page_text, {
                "source_game_id": game.source_game_id,
                "classification": game.classification,
                "competition": game.competition,
                "home_team": game.home_team,
                "away_team": game.away_team,
                "parsed": parsed,
            })
            self.store.record_reconciliation(
                source_game_id=game.source_game_id,
                classification=game.classification,
                bc_event_id=str(rec.get("bc_event_id") or ""),
                bc_event_name=str(rec.get("bc_event_name") or ""),
                bc_competition_id=(
                    str(rec.get("bc_competition_id")) if rec.get("bc_competition_id") else None
                ),
                bc_url=url,
                checks=rec["checks"],
                result=rec["result"],
            )
            self.stats["reconciliations"] += 1
            if rec["result"] != "matched":
                logger.warning(
                    "RECONCILIATION MISMATCH %s: %s",
                    game.source_game_id, rec["failures"],
                )
        except Exception:
            logger.error("reconcile failed:\n%s", traceback.format_exc())

    # ── Game lifecycle ───────────────────────────────────────────

    def _mark_ended(self, seen_keys: dict[str, set[str]]) -> None:
        for cls_val, games in self._tracked.items():
            for key, game in list(games.items()):
                if key in seen_keys.get(cls_val, set()):
                    self._unseen_ticks[cls_val][key] = 0
                    continue
                self._unseen_ticks[cls_val][key] = (
                    self._unseen_ticks[cls_val].get(key, 0) + 1
                )
                if self._unseen_ticks[cls_val][key] >= ENDED_GRACE_TICKS:
                    if game.status != "ended":
                        game.status = "ended"
                        self.store.upsert_game(game)
                        logger.info("game ended (disappeared): %s", game.source_game_id)
                    # keep the game record; drop from live tracking
                    del games[key]
                    if game.source_game_id in self._market_queue:
                        self._market_queue.remove(game.source_game_id)

    def _find_tracked(self, source_game_id: str) -> Optional[PokerBetGame]:
        for games in self._tracked.values():
            for game in games.values():
                if game.source_game_id == source_game_id:
                    return game
        return None

    def _game_db_id(self, game: PokerBetGame) -> int:
        rec = self.store.get_game(game.source_game_id)
        return int(rec["id"]) if rec else 0

    @staticmethod
    def _infer_status(period_label: str) -> str:
        p = (period_label or "").lower()
        if "half" in p or "quarter" in p:
            return "halftime" if p.startswith("half") else "live"
        if p in ("ended", "finished", "full time"):
            return "ended"
        return "live"


def run_once(collector: PokerBetCollector, headless: bool = True, max_ticks: int = 1) -> dict:
    """Run N ticks synchronously (for verification / testing)."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1600, "height": 900},
            user_agent=_USER_AGENT,
            locale="en-ZA",
        )
        page = context.new_page()
        collector._browser = browser
        collector._running = True
        try:
            collector._ensure_discovery_page(page)
            for _ in range(max_ticks):
                collector._tick(page)
        finally:
            collector._running = False
            browser.close()
    return collector.stats


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="BLM PokerBet collector")
    ap.add_argument("--once", action="store_true", help="run a single tick and exit")
    ap.add_argument("--ticks", type=int, default=1, help="ticks for --once")
    ap.add_argument("--tick", type=float, default=20.0, help="tick interval seconds")
    ap.add_argument("--headed", action="store_true", help="run headed (debug)")
    ap.add_argument(
        "--db", type=str, default=None, help="sqlite db path",
    )
    args = ap.parse_args()

    collector = PokerBetCollector(
        headless=not args.headed, tick_s=args.tick,
        db_path=Path(args.db) if args.db else None,
    )
    if args.once:
        stats = run_once(collector, headless=not args.headed, max_ticks=args.ticks)
        print(json.dumps(stats, indent=2))
        return
    try:
        collector.start()
    except KeyboardInterrupt:
        collector.stop()


if __name__ == "__main__":
    main()
