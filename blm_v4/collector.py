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
import re
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
from blm_v4.projection import clock_minutes
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

# Resilience: the BetConstruct SPA slowly degrades in long-lived sessions
# (the live-panel tree stops hydrating even though a fresh browser renders
# it fine).  When parsing keeps coming up empty, rotate the session:
#   FRESH_CONTEXT_AFTER_EMPTY     → new context/page in the same browser
#   BROWSER_RELAUNCH_AFTER_EMPTY  → full browser relaunch
#   BROWSER_MAX_LIFETIME_S        → force a fresh browser even on success
FRESH_CONTEXT_AFTER_EMPTY = 3
BROWSER_RELAUNCH_AFTER_EMPTY = 10
BROWSER_MAX_LIFETIME_S = 3600.0

STATE_DIR = Path(__file__).resolve().parent / "state"
COMP_IDS_FILE = STATE_DIR / "comp_ids.json"
STATE_FILE = STATE_DIR / "collector_state.json"

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
        self._instances: dict[str, str] = {}  # base game_id -> current instance id
        self._running = False
        self._browser: Optional[Browser] = None
        self._pw: Any = None                  # active sync_playwright scope
        self._empty_ticks = 0                 # consecutive empty-parses
        self._browser_started_at = 0.0
        self._started_at_iso = utcnow_iso()
        self._last_success_iso = ""
        self._last_error_iso = ""
        self.stats = {
            "ticks": 0, "games_seen": 0, "snapshots": 0,
            "games_resolved": 0, "reconciliations": 0, "errors": 0,
            "instances_split": 0,
        }

    # ── Lifecycle ────────────────────────────────────────────────

    def _session_options(self) -> dict:
        """Browser context options shared by every session."""
        return {
            "viewport": {"width": 1600, "height": 900},
            "user_agent": _USER_AGENT,
            "locale": "en-ZA",
            "extra_http_headers": {"Accept-Language": "en-ZA,en;q=0.9"},
        }

    def _new_session(self) -> Page:
        """Launch a fresh browser + context + page, land on the discovery page."""
        assert self._pw is not None, "sync_playwright scope not active"
        browser = self._pw.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(**self._session_options())
        page = context.new_page()
        self._browser = browser
        self._browser_started_at = time.monotonic()
        self._ensure_discovery_page(page)
        logger.info("new browser session started (age=%ds)", 0)
        return page

    def _fresh_context(self, reason: str) -> Page:
        """New context/page in the same browser (SPA state is per-context)."""
        try:
            if self._browser is None:
                return self._relaunch(reason)
            context = self._browser.new_context(**self._session_options())
            page = context.new_page()
            self._empty_ticks = 0
            self._ensure_discovery_page(page)
            logger.warning("fresh context created: %s", reason)
            return page
        except Exception:
            logger.error("fresh context failed:\n%s", traceback.format_exc())
            return self._relaunch(reason)

    def _relaunch(self, reason: str) -> Page:
        """Close the browser and start a completely fresh session."""
        logger.warning("relaunching browser: %s", reason)
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        self._browser = None
        self._empty_ticks = 0
        try:
            return self._new_session()
        except Exception:
            logger.error("relaunch failed:\n%s", traceback.format_exc())
            raise

    def _write_state(self, *, success: bool) -> None:
        """Heartbeat file the dashboard API reads for collector status."""
        now_iso = utcnow_iso()
        if success:
            self._last_success_iso = now_iso
        state = {
            "tick": self.stats["ticks"],
            "status": "running" if success else "stalled",
            "started_at": self._started_at_iso,
            "last_tick_at": now_iso,
            "last_success_at": self._last_success_iso,
            "last_error_at": self._last_error_iso,
            "consecutive_empty_ticks": self._empty_ticks,
            "browser_age_s": round(time.monotonic() - self._browser_started_at, 1)
            if self._browser_started_at else 0,
            "games_tracked": self.stats["games_seen"],
            "snapshots_total": self.stats["snapshots"],
            "games_resolved": self.stats["games_resolved"],
            "reconciliations": self.stats["reconciliations"],
            "errors": self.stats["errors"],
        }
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception:
            logger.exception("state write failed")

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            with sync_playwright() as pw:
                self._pw = pw
                page = self._new_session()
                while self._running:
                    tick_start = time.monotonic()
                    try:
                        page = self._tick(page)
                    except Exception:
                        self.stats["errors"] += 1
                        self._last_error_iso = utcnow_iso()
                        logger.error("tick error:\n%s", traceback.format_exc())
                        try:
                            page = self._relaunch("tick error")
                        except Exception:
                            logger.error("relaunch failed, giving up:\n%s",
                                         traceback.format_exc())
                            self._running = False
                            break
                    # SPA sessions degrade over hours — rotate regardless
                    if (time.monotonic() - self._browser_started_at
                            > BROWSER_MAX_LIFETIME_S):
                        page = self._relaunch("browser lifetime cap")
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
            page.wait_for_timeout(4000)  # React hydration (panel ~4s)
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

    def _expand_target_sections(self, page: Page) -> None:
        """Expand the left-panel tree (Basketball → World/Cyber, Virtual/Betual).

        The BetConstruct SPA renders the live sport tree collapsed; the
        competition sections only appear after expanding the relevant
        headers.  Click every header whose title matches a target.
        """
        try:
            page.evaluate(
                """() => {
                    const targets = ['Basketball', 'E-Basketball', 'World',
                                     'Cyber Basketball', 'Virtual Matches', 'Betual NBA'];
                    const heads = document.querySelectorAll('.sp-s-l-head-bc');
                    for (const h of heads) {
                        const t = (h.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (targets.some(x => t.includes(x))) {
                            const expanded = h.getAttribute('aria-expanded');
                            if (expanded !== 'true') h.click();
                        }
                    }
                }"""
            )
            page.wait_for_timeout(2000)
        except Exception:
            logger.error("expand sections failed:\n%s", traceback.format_exc())

    def _ensure_discovery_page(self, page: Page) -> None:
        """Land on a live page whose left panel renders all live games."""
        url = competition_url(Classification.CYBER_2K26, self.comp_ids)
        if not self._goto(page, url):
            url = BASKETBALL_LIVE_URL
            self._goto(page, url)
        self._wait_panel(page)
        self._expand_target_sections(page)

    def _recover(self, page: Page) -> Page:
        """Legacy single-tick recovery — now superseded by session rotation."""
        return self._relaunch("recover requested")

    # ── Main tick ────────────────────────────────────────────────

    def _tick(self, page: Page) -> Page:
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
            self._empty_ticks += 1
            self._write_state(success=False)
            logger.warning(
                "still no relevant competitions on page "
                "(consecutive empties: %d)",
                self._empty_ticks,
            )
            if self._empty_ticks >= BROWSER_RELAUNCH_AFTER_EMPTY:
                return self._relaunch(
                    f"{self._empty_ticks} consecutive empty parses",
                )
            if self._empty_ticks >= FRESH_CONTEXT_AFTER_EMPTY:
                return self._fresh_context(
                    f"{self._empty_ticks} consecutive empty parses",
                )
            return page
        self._empty_ticks = 0

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
        self._write_state(success=True)
        return page

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
                """(names) => {
                    const rows = document.querySelectorAll('.market-game-section');
                    for (const r of rows) {
                        const rnames = [...r.querySelectorAll('.market-game-team-name')]
                            .map(n => n.textContent.trim());
                        if (rnames.length >= 2 && rnames[0] === names.home && rnames[1] === names.away) {
                            return true;
                        }
                    }
                    return false;
                }""",
                {"home": row.home_team, "away": row.away_team},
            )
            if not el:
                logger.warning("row not found for %s vs %s", row.home_team, row.away_team)
                return None
            page.evaluate(
                """(names) => {
                    const rows = document.querySelectorAll('.market-game-section');
                    for (const r of rows) {
                        const rnames = [...r.querySelectorAll('.market-game-team-name')]
                            .map(n => n.textContent.trim());
                        if (rnames.length >= 2 && rnames[0] === names.home && rnames[1] === names.away) {
                            r.click();
                            return;
                        }
                    }
                }""",
                {"home": row.home_team, "away": row.away_team},
            )
            page.wait_for_timeout(2500)
            url = page.url
            tax = parse_event_url(url)
            if not tax:
                logger.warning("no event taxonomy after click: %s", url)
                return None
            text = page.inner_text("body", timeout=10000)

            # Authoritative teams: event-view scoreboard > URL slug > panel row.
            # The panel row can be STALE during fast game rotation (the row
            # text lags the actual event), so never trust it alone.
            home, away = self._authoritative_teams(row, tax, text)

            # Dedup by durable identity (source_game_id): the same event may be
            # re-discovered from a refreshed row — update, don't duplicate.
            # Virtual replays: the URL base id maps to the current instance id.
            existing = self._find_tracked(tax["game_id"])
            if existing is None:
                cur = self._instances.get(tax["game_id"])
                if cur:
                    existing = self._find_tracked(cur)
            if existing is not None:
                cls_val = existing.classification
                old_key = f"{existing.home_team}|{existing.away_team}"
                if (existing.home_team, existing.away_team) != (home, away):
                    existing.home_team, existing.away_team = home, away
                    self.store.upsert_game(existing)
                    logger.info(
                        "updated teams for %s -> %s vs %s",
                        tax["game_id"], home, away,
                    )
                self._tracked[cls_val].pop(old_key, None)
                self._unseen_ticks[cls_val].pop(old_key, None)
                new_key = f"{home}|{away}"
                self._tracked[cls_val][new_key] = existing
                self._unseen_ticks[cls_val][new_key] = 0
                if existing.source_game_id not in self._market_queue:
                    self._market_queue.append(existing.source_game_id)
                return existing, text

            game = self._build_game(comp, row, tax, url, home, away)
            # Restart-safe virtual replay identity: after a collector restart
            # _tracked is empty, so a fixture with existing DB history gets
            # re-resolved as "new" — if the DB's last snapshot is a finished
            # game and the observed state is a NEW replay (score drop or
            # clock regression), start a fresh #iN instance instead of
            # contaminating the finished row.
            new_id = self._restart_split_suffix(text, tax, game)
            if new_id:
                game.source_game_id = new_id
                self._instances[self._base_id(new_id)] = new_id
                logger.info(
                    "restart-safe virtual replay split: %s -> %s",
                    tax["game_id"], new_id,
                )
            gid = self.store.upsert_game(game)
            key = f"{home}|{away}"
            self._tracked[comp.classification.value][key] = game
            self._unseen_ticks[comp.classification.value][key] = 0
            self._market_queue.append(game.source_game_id)
            self.stats["games_resolved"] += 1
            logger.info(
                "resolved new %s game %s (%s vs %s) game_id=%s",
                comp.classification.value, gid, home, away,
                tax["game_id"],
            )
            return game, text
        except Exception:
            logger.error("resolve failed:\n%s", traceback.format_exc())
            return None

    def _restart_split_suffix(self, text: str, tax: dict,
                              game: PokerBetGame) -> Optional[str]:
        """If the fixture already has DB history and the observed event-view
        state is a NEW virtual replay, return the fresh instance id.

        The in-memory _tracked/_instances maps don't survive a collector
        restart, so a re-resolved fixture would otherwise append the new
        replay's snapshots to the finished game's DB row."""
        if not self.store.get_game(tax["game_id"]):
            return None
        ev = parse_event_view(text)
        eh, ea = ev.get("home_score"), ev.get("away_score")
        if eh is None or ea is None:
            return None
        if not self._detect_event_reset(
                game, int(eh), int(ea),
                ev.get("period_label"), ev.get("clock")):
            return None
        return self._next_instance_id(self._base_id(tax["game_id"]))

    def _authoritative_teams(self, row: RowGame, tax: dict, text: str) -> tuple[str, str]:
        """Pick the true team names for the event.

        Priority: event-view scoreboard (displayed truth) > URL slug
        (BetConstruct event identity) > panel row (may be stale).
        """
        home, away = row.home_team, row.away_team
        try:
            ev = parse_event_view(text)
            eh, ea = ev.get("home_team"), ev.get("away_team")
            if eh and ea:
                home, away = eh, ea
        except Exception:
            pass
        slug = tax.get("game_slug") or ""
        home_slug, away_slug = slugify_team(home), slugify_team(away)
        if slug and (not slug.startswith(home_slug) or not slug.endswith(away_slug)):
            logger.warning(
                "teams inconsistent with slug %s (%s vs %s) — deriving from slug",
                slug, home, away,
            )
            if slug.startswith(home_slug):
                rest = slug[len(home_slug):].lstrip("-")
                if rest:
                    away = rest.replace("-", " ").title()
            elif slug.endswith(away_slug):
                head = slug[: len(slug) - len(away_slug)].rstrip("-")
                if head:
                    home = head.replace("-", " ").title()
        return home, away

    def _build_game(
        self, comp, row: RowGame, tax: dict, url: str, home: str, away: str,
    ) -> PokerBetGame:
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
            home_team=home,
            away_team=away,
            game_slug=tax["game_slug"],
            source_url=url,
            status="live",
        )

    # ── Snapshot capture ─────────────────────────────────────────

    def _next_instance_id(self, base: str) -> str:
        """Next free virtual-instance id for a fixture.

        Skips suffixes that already exist in the DB: after a collector
        restart a re-resolved fixture must continue at base#i3, not
        re-create base#i1 (which a previous process may already have
        recorded a finished game under)."""
        n = 0
        pat = re.compile(rf"^{re.escape(base)}#i(\d+)$")
        for gid in self.store.list_instance_ids(base):
            m = pat.match(gid)
            if m:
                n = max(n, int(m.group(1)))
        return f"{base}#i{n + 1}"

    @staticmethod
    def _base_id(gid: str) -> str:
        """Strip the virtual-instance suffix from a source_game_id."""
        return re.sub(r"#i\d+$", "", gid or "")

    def _detect_instance_reset(self, game: PokerBetGame, row: RowGame) -> bool:
        """True when the panel row shows a NEW virtual replay of the same
        fixture (Betual/Cyber games replay every ~5 min under the SAME
        BetConstruct event URL).  A reset is a large score drop or a
        game-clock regression vs the game's last stored snapshot."""
        if row.home_score is None or row.away_score is None:
            return False
        return self._detect_event_reset(
            game, row.home_score, row.away_score, row.period_label, row.clock)

    @staticmethod
    def _elapsed_minutes(quarter: Optional[int], clock: Optional[str],
                         period_label: Optional[str] = None) -> Optional[float]:
        """Game-clock position in minutes (0..40). Falls back to the
        period label when the structured quarter is missing."""
        q = quarter
        if q is None and period_label:
            p = (period_label or "").lower()
            m = re.search(r"(\d)(?:st|nd|rd|th)?\s*quarter", p)
            if m:
                q = int(m.group(1))
            elif p.startswith("half"):
                q = 2
        return clock_minutes(q, clock)

    def _detect_event_reset(self, game: PokerBetGame, home: int, away: int,
                            period_label: Optional[str] = None,
                            clock: Optional[str] = None) -> bool:
        """True when an observed state (panel row OR event view) is a NEW
        virtual replay of the same fixture.

        Two independent signals:
          1. score drop: last total >= 30 and new total < 50% of it
          2. clock regression: the observed game phase is EARLIER than the
             stored last snapshot by > 2 game-minutes (Q4 -> Q1 is
             impossible within one game).  Needed because at 20s ticks the
             new replay's first row can already carry a score (e.g. 28-28)
             above the 50% score-drop threshold.
        """
        if home is None or away is None:
            return False
        last = self.store.get_snapshots(game.source_game_id, limit=1)
        if not last:
            return False
        last_row = last[0]
        lh = last_row.get("home_score")
        la = last_row.get("away_score")
        if lh is None or la is None:
            return False
        cur = home + away
        prev = lh + la
        if prev >= 30 and cur < prev * 0.5:
            return True
        last_el = self._elapsed_minutes(
            last_row.get("quarter"), last_row.get("clock"),
            last_row.get("period_label"))
        new_el = self._elapsed_minutes(None, clock, period_label)
        if last_el is not None and new_el is not None and new_el < last_el - 2.0:
            return True
        # 3. score explosion within a short wall-clock window: a jump of
        #    > 15 points in < 90s is impossible for a real game — the
        #    observation is a DIFFERENT virtual replay of the same fixture
        #    (observed live: Q1 19-14 -> '4th Quarter 21:00 62-71' in 11s).
        if prev > 0 and cur - prev > 15:
            last_ts = last_row.get("captured_at")
            try:
                last_dt = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
                gap = (datetime.now(timezone.utc) - last_dt).total_seconds()
                if 0 <= gap <= 90:
                    return True
            except Exception:
                pass
        return False

    def _split_instance(self, game: PokerBetGame, row: RowGame, cls) -> PokerBetGame:
        """Mark the current game ended and start a fresh instance record
        with a distinct identity (base#iN) so replay snapshots never mix
        into the finished game's history."""
        base = self._base_id(game.source_game_id)
        new_id = self._next_instance_id(base)
        self._instances[base] = new_id

        # end the old game
        if game.status != "ended":
            game.status = "ended"
            self.store.upsert_game(game)
        cls_val = cls.value
        key = f"{game.home_team}|{game.away_team}"
        self._tracked[cls_val].pop(key, None)
        self._unseen_ticks[cls_val].pop(key, None)
        if game.source_game_id in self._market_queue:
            self._market_queue.remove(game.source_game_id)

        # fresh instance record (same fixture, new identity)
        new_game = game.model_copy(update={
            "source_game_id": new_id,
            "status": "live",
            "first_seen_at": utcnow_iso(),
            "last_seen_at": utcnow_iso(),
        })
        self.store.upsert_game(new_game)
        self._tracked[cls_val][key] = new_game
        self._unseen_ticks[cls_val][key] = 0
        self._market_queue.append(new_id)
        self.stats["instances_split"] += 1
        logger.info(
            "virtual replay split: %s -> %s (new instance %s-%s)",
            base, new_id, row.home_score, row.away_score,
        )
        return new_game

    def _store_list_snapshot(self, game: PokerBetGame, row: RowGame, comp) -> None:
        """Persist the list-level observation for a known game.

        Detects virtual-replay score resets: when the panel row shows a
        fresh instance of the same fixture, the finished game is ended
        and the snapshot is recorded under a NEW instance record so the
        two games never share a history.
        """
        if self._detect_instance_reset(game, row):
            game = self._split_instance(game, row, comp)
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
            # Virtual replay protection: the event URL now serves a NEW
            # instance of the same fixture.  Detect the score reset BEFORE
            # writing and split the instance, so this event-view snapshot
            # lands in the fresh game's history — never the finished one's.
            eh, ea = parsed.get("home_score"), parsed.get("away_score")
            if eh is not None and ea is not None \
                    and self._detect_event_reset(
                        game, int(eh), int(ea),
                        parsed.get("period_label"), parsed.get("clock")):
                row = RowGame(home_score=int(eh), away_score=int(ea))
                game = self._split_instance(game, row,
                                            Classification(game.classification))
            # refresh identity from the URL taxonomy (URL changes handled);
            # keep any virtual-instance suffix (base#iN) — the suffix is
            # the instance identity, the URL base is just the fixture.
            tax = parse_event_url(page.url)
            if tax:
                base = self._base_id(game.source_game_id)
                suffix = game.source_game_id[len(base):]
                if tax["game_id"] != base:
                    game.source_game_id = tax["game_id"] + suffix
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
        collector._pw = pw
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
        collector._browser_started_at = time.monotonic()
        collector._running = True
        try:
            collector._ensure_discovery_page(page)
            for _ in range(max_ticks):
                page = collector._tick(page)
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
