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
import sqlite3
import time
import traceback
from collections import deque
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
from blm_v4.clean_metrics import CleanMetricsStore
from blm_v4.deviation import DeviationEngine
from blm_v4.pace_projector import PaceProjector
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
from blm_v4.ws_market import normalize_observations, parse_market_frame

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
# A game may vanish from the live-panel list transiently (SPA hiccup,
# rotation lag).  ENDED_GRACE_S is WALL-TIME; the tick-equivalent is
# derived per-instance (ENDED_GRACE_S / tick_s) so the tolerance is
# unchanged when the tick interval changes (3 ticks @20s = 60s; the
# same 60s must be 6 ticks @10s, never 3).
ENDED_GRACE_S = 60.0
TICK_DEFAULT = 10.0

# Market data is first-class live data.  The SPA's event view hydrates
# only via an in-app row click + ~2.5s wait; back-to-back clicks in one
# tick leave the SPA on blank views (observed: empty-team parses).  So
# capture up to MARKET_BATCH event views per slow run (proven path),
# bounded by per-game MARKET_REFRESH freshness.
# MARKET_BATCH 2 -> 3 (2026-09-09): each slow run is bounded work; three
# visits raise the rotation capacity from ~2 games/80s to ~3 games/50s
# (EVENT_VIEW_EVERY_N 3 -> 2 below), shrinking the worst-case rotation
# span for a ~40-game cohort from ~25min to ~11min.  Verified live that
# the fast score cadence stays well inside ACTIVE_GAME_STALENESS_S=180.
MARKET_BATCH = 3
# Per-game market refresh gate — MUST stay below the 300s freshness
# threshold (pace_projector.FRESH_LINE_SECONDS): a line captured on one
# visit has to still be inside the LIVE window when the next visit is
# due, so the rotation can refresh it before the dashboard flips the
# game to STALE.  480 violated that (480 > 300): a game visited just
# before its line aged out stayed un-refreshed for another 480s.
# 240 = half the threshold: even one missed slot still leaves a
# re-visit inside the LIVE window.  NOTE the gate is capacity-limited,
# not a guarantee: one slow page covers ~3 games/50s, so a 40-game
# cohort rotates in ~11min — genuinely stale-market stretches remain
# possible and are reported honestly (see market_collection in the
# collector state file).
MARKET_REFRESH_S = 240
# STEP 2: the slow (full event-view) path runs on its OWN page, gated to
# once every N fast ticks — decoupled from the score poll's page/state.
# N=2 (was 3): one event-view run every ~2 fast cycles (~50s observed);
# each run captures up to MARKET_BATCH games.  With ~40 tracked games
# that bounds a full rotation to ~11 min — the honest single-browser
# limit for one-thread WS subscription coverage (the eu-swarm feed only
# pushes the OPEN event's markets).  The attempt still runs
# synchronously inside that tick (a ~6-20s overrun that the
# target-boundary scheduler absorbs), so the score poll cadence on the
# other ticks is untouched; full async decoupling is future work.
EVENT_VIEW_EVERY_N = 2
# In-memory tick-timing ring (instrumentation): keep ~24h of samples at
# the 10s tick, then drop the oldest — a daemon must never grow without
# bound.  The summary is exposed via the collector state payload.
TICK_STATS_MAX = 8640
# eu-swarm market observations: dedupe identical (game, line) frames within
# this window — the feed pushes every price change, so movements still land,
# but a game that stays flat is not spammed into the DB every second.
WS_MARKET_DEDUP_S = 30.0

# Repeated-visit backoff (2026-09-09): with the gate armed only by an
# OBSERVED market (attempt != observation), a persistently failing game
# would otherwise be retried every slow run and consume the whole
# MARKET_BATCH.  After MARKET_ATTEMPT_STREAK_BACKOFF consecutive failed
# attempts, a game is retried at most once per MARKET_ATTEMPT_BACKOFF_S
# — bounded retry, still always in rotation (never removed), streak and
# attempts visible in the collector state payload.
MARKET_ATTEMPT_BACKOFF_S = 60.0
MARKET_ATTEMPT_STREAK_BACKOFF = 3

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


def _ts_age_s(iso_ts: str) -> float:
    """Seconds since an ISO timestamp (UTC). Returns +inf on parse error."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return float("inf")


_PERIOD_Q = {
    "1st Quarter": 1, "2nd Quarter": 2,
    "3rd Quarter": 3, "4th Quarter": 4,
}


def ws_matchtotal_snapshot(
    game: PokerBetGame, obs: dict,
) -> Optional[MarketObservation]:
    """Event-quality snapshot from a persisted eu-swarm MatchTotal obs.

    The WS stream for the OPEN event is the same observed bookmaker data
    as the event-view DOM (identity, score, period, O/U line + prices)
    but arrives with NO DOM parse — so a tracked game whose event page
    is open gets a complete score + market history even when the
    event-view DOM parse fails verification (the Betual/Cyber virtual
    pages regularly do).  Pure mapping of an already-persisted obs; the
    caller throttles per game and persists.  Returns None when the obs
    carries no line (defensive).
    """
    line = obs.get("line_value")
    if line is None:
        return None
    period = obs.get("period_label") or ""
    return MarketObservation(
        source=SOURCE_POKERBET,
        source_game_id=game.source_game_id,
        classification=game.classification,
        captured_at=obs["captured_at"],
        home_team=game.home_team,
        away_team=game.away_team,
        home_score=obs.get("home_score"),
        away_score=obs.get("away_score"),
        period_label=period or "",
        quarter=_PERIOD_Q.get(period),
        clock=obs.get("clock"),
        game_status=PokerBetCollector._infer_status(period) if period else "live",
        total_line=line,
        total_over_odds=obs.get("over_price"),
        total_under_odds=obs.get("under_price"),
        source_url=game.source_url,
        markets_json=json.dumps({
            "source": "ws",
            "market_type": obs.get("market_type"),
            "market_name": obs.get("market_name"),
            "line_value": line,
            "over_price": obs.get("over_price"),
            "under_price": obs.get("under_price"),
        }, default=str),
        raw_json=json.dumps(obs.get("raw") or {}, default=str),
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


def _tick_timing_summary(stats: dict[str, deque]) -> dict[str, Any]:
    """Reduce the bounded tick-timing ring to a small status payload.

    Pure + testable: means over the retained samples (ms), sample count,
    and the most recent slow event-view duration (ms).  Empty rings yield
    None means — a just-started collector reports no timing yet.
    """
    def _mean_ms(key: str) -> Optional[float]:
        buf = stats.get(key)
        if not buf:
            return None
        return round((sum(buf) / len(buf)) * 1000.0, 1)

    ev = stats.get("event")
    return {
        "n": len(stats.get("work", [])),
        "mean_work_ms": _mean_ms("work"),
        "mean_sleep_ms": _mean_ms("sleep"),
        "mean_cycle_ms": _mean_ms("cycle"),
        "last_event_ms": round(ev[-1] * 1000.0, 1) if ev else None,
    }


class PokerBetCollector:
    """Resilient multi-game PokerBet collector."""

    def __init__(
        self,
        store: Optional[PokerBetStore] = None,
        *,
        headless: bool = True,
        tick_s: float = TICK_DEFAULT,
        db_path: Optional[Path] = None,
    ):
        self.headless = headless
        self.tick_s = tick_s
        # disappearance tolerance in TICKS, derived from wall-time so a
        # tick-interval change never silently shrinks the grace window
        self._ended_grace_ticks = max(1, int(round(ENDED_GRACE_S / tick_s)))
        self.store = store or PokerBetStore(
            db_path or Path(__file__).resolve().parent.parent / "blm_pokerbet.db",
        )
        self.comp_ids = load_comp_ids()
        # Clean metrics database (statistical foundation): a SEPARATE DB
        # populated only from validated observations flowing through this
        # collector's quality/replay protections.  Failure-isolated — a
        # clean-metrics problem must never affect collection.
        self.clean_metrics: Optional[CleanMetricsStore] = None
        self._deviation: Optional[DeviationEngine] = None
        try:
            clean_path = Path(self.store.db_path).parent / "blm_metrics_clean.db"
            self.clean_metrics = CleanMetricsStore(clean_path)
            logger.info(
                "clean metrics db ready: %s (start %s)",
                clean_path, self.clean_metrics.started_at(),
            )
        except Exception:
            self.clean_metrics = None
            logger.error(
                "clean metrics db unavailable (collector continues):\n%s",
                traceback.format_exc(),
            )
        # Deviation / benchmark / z-score layer (Phase 2): reads the same
        # clean trajectory rows; failure-isolated like the rest of the
        # clean-metrics path.
        try:
            if self.clean_metrics is not None:
                self._deviation = DeviationEngine(self.clean_metrics.db_path)
        except Exception:
            self._deviation = None
            logger.error(
                "deviation benchmark layer unavailable (collector "
                "continues):\n%s",
                traceback.format_exc(),
            )

        # tracked games: classification -> team-key -> PokerBetGame
        self._tracked: dict[str, dict[str, PokerBetGame]] = {
            cls.value: {} for cls in (Classification.CYBER_2K26, Classification.BETUAL_NBA)
        }
        self._unseen_ticks: dict[str, dict[str, int]] = {
            cls.value: {} for cls in (Classification.CYBER_2K26, Classification.BETUAL_NBA)
        }
        self._market_queue: list[str] = []   # round-robin of source_game_ids
        self._last_market_at: dict[str, str] = {}  # gid -> last event-view capture ts
        # Diagnostic separation of ATTEMPT vs OBSERVATION (2026-09-09):
        # _last_market_at arms the refresh gate only when a visit actually
        # OBSERVED a market (persisted snapshot/WS bridge).  An attempt
        # that came back empty (parse failure, unverified view, lobby
        # down) must not re-arm the gate — otherwise a broken game is
        # skipped for a whole window with nothing observed.  Attempts are
        # tracked separately for monitoring only.
        self._last_market_attempt_at: dict[str, str] = {}
        self._attempt_streak: dict[str, int] = {}
        self._market_stats = {
            "attempts": 0, "observed": 0, "failed": 0,
            "skipped_fresh": 0, "skipped_fresh_arms": 0,
            "skipped_backoff": 0,
        }
        self._instances: dict[str, str] = {}  # base game_id -> current instance id
        self._running = False
        self._browser: Optional[Browser] = None
        self._pw: Any = None                  # active sync_playwright scope
        # STEP 2: separate SLOW event-view page (decoupled from the fast
        # score poll).  Single browser; the fast page holds the lobby and
        # the WS market hook; the slow page does the round-robin event-view
        # navigations so a slow nav never delays the ~10s score cadence.
        self._slow_page: Optional[Page] = None
        self._slow_browser_started_at: Optional[float] = None
        self._tick_no = 0                     # fast-loop tick counter
        self._empty_ticks = 0                 # consecutive empty-parses
        self._event_view_failures = 0         # consecutive unverified event views
        self._ws_market_last: dict[tuple[str, Optional[float]], str] = {}
        self._ws_snap_last: dict[str, str] = {}  # gid -> last bridge snapshot ts
        self._browser_started_at = 0.0
        self._started_at_iso = utcnow_iso()
        self._last_success_iso = ""
        self._last_error_iso = ""
        self.stats = {
            "ticks": 0, "games_seen": 0, "snapshots": 0,
            "games_resolved": 0, "reconciliations": 0, "errors": 0,
            "instances_split": 0, "games_ended_final": 0,
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
        self._attach_ws_market_hook(page)
        self._ensure_discovery_page(page)
        logger.info("new browser session started (age=%ds)", 0)
        return page

    def _attach_ws_market_hook(self, page: Page) -> None:
        """Capture the eu-swarm market feed — the independent market path.

        The BetConstruct SPA pushes the full live market tree (O/U total +
        Over/Under prices) over ``wss://eu-swarm-newm.pokerbet.co.za/`` for
        every game in the current competition.  This works WITHOUT the
        event-view DOM (no clicks, no hydration) — it is the fallback the
        market pipeline needs when the event-view route is broken.  Frames
        are parsed and persisted as market_observations rows.
        """
        def on_ws(ws):
            if "swarm" not in ws.url:
                return
            def on_frame(payload):
                try:
                    text = payload if isinstance(payload, str) else ""
                    if not text or "swarm" not in ws.url:
                        return
                    payloads = parse_market_frame(text)
                    if not payloads:
                        return
                    captured = utcnow_iso()
                    for obs in normalize_observations(payloads, captured):
                        if obs["market_type"] != "MatchTotal":
                            continue
                        gid = obs["source_game_id"]
                        last = self._ws_market_last.get((gid, obs["line_value"]))
                        if last and _ts_age_s(last) < WS_MARKET_DEDUP_S:
                            continue
                        self._ws_market_last[(gid, obs["line_value"])] = captured
                        game = self._find_tracked(gid)
                        if game is None:
                            continue
                        obs["game_id"] = self._game_db_id(game)
                        try:
                            self.store.upsert_market_observation(obs)
                        except Exception:
                            logger.error(
                                "market observation persist failed:\n%s",
                                traceback.format_exc())
                        # WS → snapshot bridge: the open event's feed is an
                        # observed capture of THIS game (score/period/line),
                        # independent of the DOM parse that keeps failing on
                        # virtual events.  Persist an event-quality snapshot
                        # (throttled per game) so the live card is complete
                        # and total_line reaches the API/dashboard even when
                        # the event-view parse is unverified.
                        try:
                            snap = ws_matchtotal_snapshot(game, obs)
                            last = self._ws_snap_last.get(gid)
                            if snap is not None and (
                                    last is None
                                    or _ts_age_s(last) >= WS_MARKET_DEDUP_S):
                                if self.store.insert_snapshot(
                                        self._game_db_id(game), snap):
                                    self._ws_snap_last[gid] = obs["captured_at"]
                                    self.stats["snapshots"] += 1
                                    self._record_clean(game, snap)
                        except Exception:
                            logger.error("ws snapshot bridge failed:\n%s",
                                         traceback.format_exc())
                except Exception:
                    # a malformed frame must never kill the collector
                    logger.debug("ws market frame error: %s",
                                 traceback.format_exc())
            ws.on("framereceived", on_frame)
        page.on("websocket", on_ws)

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
        self._slow_page = None            # old browser gone — recreate on next guard
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
            "tick_timing": _tick_timing_summary(self._tick_stats),
            # market-rotation diagnostics: ATTEMPT vs OBSERVED MARKET — a
            # served request is not a fresh observation; only "observed"
            # re-arms the freshness gate.  Per-game streaks expose
            # persistently failing games (bounded-backoff retries).
            "market_collection": {
                **self._market_stats,
                "queue_len": len(self._market_queue),
                "gate_seconds": MARKET_REFRESH_S,
                "streaks_over_backoff": sum(
                    1 for v in self._attempt_streak.values()
                    if v >= MARKET_ATTEMPT_STREAK_BACKOFF),
            },
            # crash/restart recovery: persist tracked games so a restart
            # does NOT trigger a full re-resolution storm (each new-game
            # resolve is a ~6s event-view nav that blocks the fast tick).
            "tracked_games": self._tracked_serializable(),
            "last_market_at": dict(self._last_market_at),
            "last_market_attempt_at": dict(self._last_market_attempt_at),
            "market_attempt_streaks": dict(self._attempt_streak),
        }
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception:
            logger.exception("state write failed")

    # ── Tracked-game persistence (restart recovery) ───────────────

    def _tracked_serializable(self) -> list[dict]:
        """Compact serialization of the tracked games (no ended games)."""
        out = []
        for cls_map in self._tracked.values():
            for g in cls_map.values():
                if g.status == "ended":
                    continue
                out.append({
                    "source_game_id": g.source_game_id,
                    "classification": g.classification,
                    "competition_id": g.competition_id,
                    "competition_slug": g.competition_slug,
                    "competition": g.competition,
                    "region": g.region,
                    "game_family": g.game_family,
                    "sport": g.sport,
                    "home_team": g.home_team, "away_team": g.away_team,
                    "game_slug": g.game_slug, "source_url": g.source_url,
                    "status": g.status,
                    "first_seen_at": g.first_seen_at,
                    "last_seen_at": g.last_seen_at,
                })
        return out

    def _restore_tracked(self) -> int:
        """Rebuild _tracked / _market_queue from the last state file.

        Returns the number of games restored.  A restored game whose
        fixture has since ended/replayed is handled by the existing
        instance-reset / mark-ended machinery on the next ticks.
        """
        try:
            raw = json.loads(STATE_FILE.read_text())
        except Exception:
            return 0
        games = raw.get("tracked_games") or []
        n = 0
        for d in games:
            try:
                g = PokerBetGame(**d)
            except Exception:
                continue
            cls_val = g.classification
            if cls_val not in self._tracked:
                continue
            key = f"{g.home_team}|{g.away_team}"
            if not key.strip("|") or key in self._tracked[cls_val]:
                continue
            self._tracked[cls_val][key] = g
            self._unseen_ticks[cls_val][key] = 0
            if g.source_game_id not in self._market_queue and g.source_url:
                self._market_queue.append(g.source_game_id)
            n += 1
        # Market-rotation recovery: an empty _last_market_at after a
        # restart re-opens every game's refresh gate simultaneously —
        # the rotation then hammers games whose line is already fresh
        # while starved games wait (the 2026-09-09 02:45Z restart left
        # 29/40 active games without any line until each was re-visited).
        # Restoring the per-game capture/attempt timestamps (and streaks)
        # keeps the rotation order and gates continuous across restarts.
        try:
            self._last_market_at.update(raw.get("last_market_at") or {})
            self._last_market_attempt_at.update(
                raw.get("last_market_attempt_at") or {})
            self._attempt_streak.update(raw.get("market_attempt_streaks") or {})
        except Exception:
            logger.warning("market rotation state restore failed")
        if n:
            logger.info("restored %d tracked games from state", n)
        return n

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        # ── True fixed-rate scheduling (STEP 2 clean-data cadence) ──
        # The tick loop is target-boundary: each cycle aims for the NEXT
        # tick boundary from the PREVIOUS target, so page/network/parse
        # latency is absorbed INSIDE the interval instead of stacking on
        # top of a full sleep.  When work overruns the target, the next
        # target advances by the interval from the missed one (no sleep,
        # no catch-up burst, no overlap).  Timing is a bounded ring
        # (TICK_STATS_MAX ≈ 24h @10s) summarized into the state payload.
        self._next_tick_target = 0.0
        self._tick_stats: dict[str, deque] = {
            key: deque(maxlen=TICK_STATS_MAX)
            for key in ("work", "sleep", "cycle", "event")}
        # restart recovery: restore tracked games so a crash/restart does
        # not force a full re-resolution storm in the fast path
        self._restore_tracked()
        # one-time backfill: pre-existing clean observations (already in
        # blm_metrics_clean.db from before this layer existed) enter the
        # deviation benchmark immediately; per-observation refreshes keep
        # it current afterwards.  Idempotent and failure-isolated.
        if self._deviation is not None:
            try:
                stats = self._deviation.refresh_all()
                logger.info(
                    "deviation benchmark backfill: %s",
                    {k: v for k, v in stats.items()},
                )
            except Exception:
                logger.error("deviation benchmark backfill failed:\n%s",
                             traceback.format_exc())
        try:
            with sync_playwright() as pw:
                self._pw = pw
                page = self._new_session()
                while self._running:
                    tick_start = time.monotonic()
                    # schedule the NEXT target on the interval grid
                    if self._next_tick_target <= 0.0:
                        self._next_tick_target = tick_start + self.tick_s
                    try:
                        page = self._tick(page)
                    except sqlite3.OperationalError as exc:
                        # Cross-process SQLite contention: the server's
                        # scorecard holds a long write transaction on
                        # blm_pokerbet.db (busy_timeout already waited 90s).
                        # NOT a browser fault — skip this tick and retry on
                        # the next boundary.  Relaunching would kill the WS
                        # market subscription and tracked state for no
                        # reason; the DOM is re-read fresh next tick, so no
                        # observation is lost by skipping.
                        self.stats["errors"] += 1
                        self._last_error_iso = utcnow_iso()
                        logger.warning("tick db lock — skipping tick: %s", exc)
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
                    # target-boundary sleep: absorb latency, never stack
                    sleep_for = max(0.0, self._next_tick_target - time.monotonic())
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                    # advance the target by whole intervals past the missed one
                    while self._next_tick_target <= time.monotonic():
                        self._next_tick_target += self.tick_s
                    self._tick_stats["work"].append(elapsed)
                    self._tick_stats["sleep"].append(sleep_for)
                    self._tick_stats["cycle"].append(
                        time.monotonic() - tick_start)
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
        self._tick_no += 1
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

        # 3. Slow path — round-robin full event-view capture, DECOUPLED.
        #    Runs on its own page (self._slow_page) on a coarse cadence
        #    (EVENT_VIEW_EVERY_N fast ticks), so a slow event-view nav
        #    (~6s click+load+parse) never delays the ~10s score poll.
        if self._slow_page is not None and self._tick_no % EVENT_VIEW_EVERY_N == 0:
            t0 = time.monotonic()
            try:
                self._capture_slow_market()
            except Exception:
                logger.error("slow event-view capture error:\n%s",
                             traceback.format_exc())
            self._tick_stats["event"].append(time.monotonic() - t0)
        elif self._slow_page is None:
            # ensure the slow page exists once the browser is up
            self._ensure_slow_page()

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
        # Identity guard: a lobby/foreign page must never drive a split —
        # only THIS fixture's own verified event view can.
        if not self._verified_event_view(game, ev):
            logger.warning(
                "restart-split text unverified for %s (teams %r/%r) — no split",
                tax["game_id"], ev.get("home_team"), ev.get("away_team"),
            )
            return None
        eh, ea = ev.get("home_score"), ev.get("away_score")
        if eh is None or ea is None:
            return None
        sig = self._detect_event_reset(
            game, int(eh), int(ea), ev.get("period_label"), ev.get("clock"))
        if not sig:
            return None
        new_id = self._next_instance_id(self._base_id(tax["game_id"]))
        prev = self.store.get_snapshots(game.source_game_id, limit=1)
        prev_row = prev[0] if prev else {}
        logger.info(
            "restart-safe virtual replay split: %s -> %s (path=restart "
            "signal=%s prev=%s-%s %s %s -> new=%s-%s %s %s)",
            tax["game_id"], new_id, sig,
            prev_row.get("home_score"), prev_row.get("away_score"),
            prev_row.get("period_label"), prev_row.get("clock"),
            eh, ea, ev.get("period_label"), ev.get("clock"),
        )
        return new_id

    def _authoritative_teams(self, row: RowGame, tax: dict, text: str) -> tuple[str, str]:
        """Pick the true team names for the event.

        Priority: event-view scoreboard (displayed truth) > URL slug
        (BetConstruct event identity) > panel row (may be stale).
        """
        home, away = row.home_team, row.away_team
        try:
            ev = parse_event_view(text)
            eh, ea = ev.get("home_team"), ev.get("away_team")
            # Only adopt event-view teams when they MATCH the clicked row —
            # a foreign page (lobby fallback) shows another game's teams,
            # which must never become this fixture's identity.
            if eh and ea and self._same_team(eh, home) and self._same_team(ea, away):
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

    def _detect_instance_reset(self, game: PokerBetGame, row: RowGame) -> Optional[str]:
        """Positive-evidence signal when the panel row shows a NEW virtual
        replay of the same fixture (Betual/Cyber games replay every ~5 min
        under the SAME BetConstruct event URL), else None.

        A reset is a score drop, a game-clock regression vs the game's
        last stored snapshot, or a post-final replay frame below the
        fixture's recorded final — all impossible within one game."""
        if row.home_score is None or row.away_score is None:
            return None
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
                            clock: Optional[str] = None) -> Optional[str]:
        """Return the positive-evidence signal ('score_drop' |
        'clock_regression' | 'post_final_replay') when an observed state
        (panel row OR verified event view) is a NEW virtual replay of the
        same fixture, else None.

        Three signals, all IMPOSSIBLE within one game:
          1. score drop: last total >= 30 and new total < 50% of it
          2. clock regression: the observed game phase is EARLIER than the
             stored last snapshot by > 2 game-minutes (Q4 -> Q1 is
             impossible within one game).  Needed because at 20s ticks the
             new replay's first row can already carry a score (e.g. 28-28)
             above the 50% score-drop threshold.
          3. post-final replay: the fixture ALREADY has a recorded final
             (game_results.final_total) and the observed total is strictly
             BELOW it.  A live game can never dip below its own recorded
             final, so the frame must be an EARLIER state of the finished
             fixture — the source re-listing the finished event with a
             frozen replay frame.  This covers the case the first two
             signals miss: a replay total close to the recorded final
             (no score_drop) with a blank/unparseable period label
             (no clock_regression) — observed 2026-09-05 as 123-118 @
             01:30 blank period after a 123-121 final (games 4859/4860).

        There is deliberately NO score-EXPLOSION signal: a forward jump
        (even > 15 pts in < 90s) is NOT positive evidence of a new replay —
        the live list feed lags the true game state by minutes (observed
        ~1x clock vs the ~7x virtual-game speed), so the event view
        legitimately shows a much later state of the SAME game.  Splitting
        on forward jumps fragmented every game into #iN churn; foreign
        content (the original explosion case) is instead rejected by the
        event-view identity guard (`_verified_event_view`), and a verified
        final state ends the game (`_is_final_state` -> `_end_game`).
        """
        if home is None or away is None:
            return None
        last = self.store.get_snapshots(game.source_game_id, limit=1)
        if not last:
            return None
        last_row = last[0]
        lh = last_row.get("home_score")
        la = last_row.get("away_score")
        if lh is None or la is None:
            return None
        cur = home + away
        prev = lh + la
        if prev >= 30 and cur < prev * 0.5:
            return "score_drop"
        last_el = self._elapsed_minutes(
            last_row.get("quarter"), last_row.get("clock"),
            last_row.get("period_label"))
        new_el = self._elapsed_minutes(None, clock, period_label)
        if last_el is not None and new_el is not None and new_el < last_el - 2.0:
            return "clock_regression"
        # post-final replay: recorded final exists and the observed total
        # is strictly below it — positive evidence of an earlier frame of
        # a fixture that has already finished (see docstring signal 3).
        final = self.store.get_recorded_final(game.source_game_id)
        if final is not None and final.get("final_total") is not None \
                and cur < final["final_total"]:
            return "post_final_replay"
        return None

    def _split_instance(self, game: PokerBetGame, row: RowGame, cls,
                        signal: Optional[str] = "unknown",
                        path: str = "list") -> PokerBetGame:
        """Mark the current game ended and start a fresh instance record
        with a distinct identity (base#iN) so replay snapshots never mix
        into the finished game's history.

        Every split is audited (instance_splits table + INFO log) with the
        tracked instance's last state vs the observation that triggered it,
        so churn can be distinguished from genuine replay boundaries."""
        base = self._base_id(game.source_game_id)
        new_id = self._next_instance_id(base)
        self._instances[base] = new_id

        prev = self.store.get_snapshots(game.source_game_id, limit=1)
        prev_row = prev[0] if prev else {}
        now_iso = utcnow_iso()
        try:
            self.store.insert_instance_split(
                created_at=now_iso, base_id=base, old_id=game.source_game_id,
                new_id=new_id, path=path, signal=signal or "unknown",
                prev_home=prev_row.get("home_score"),
                prev_away=prev_row.get("away_score"),
                prev_period=prev_row.get("period_label"),
                prev_clock=prev_row.get("clock"),
                prev_at=prev_row.get("captured_at"),
                new_home=row.home_score, new_away=row.away_score,
                new_period=row.period_label, new_clock=row.clock,
                new_at=now_iso,
            )
        except Exception:
            logger.error("split audit failed:\n%s", traceback.format_exc())

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
            "virtual replay split: %s -> %s (path=%s signal=%s "
            "prev=%s-%s %s %s @%s -> new=%s-%s %s %s)",
            base, new_id, path, signal,
            prev_row.get("home_score"), prev_row.get("away_score"),
            prev_row.get("period_label"), prev_row.get("clock"),
            (prev_row.get("captured_at") or "")[11:19] if prev_row.get("captured_at") else "-",
            row.home_score, row.away_score, row.period_label, row.clock,
        )
        return new_game

    def _store_list_snapshot(self, game: PokerBetGame, row: RowGame, comp) -> None:
        """Persist the list-level observation for a known game.

        Detects virtual-replay score resets: when the panel row shows a
        fresh instance of the same fixture, the finished game is ended
        and the snapshot is recorded under a NEW instance record so the
        two games never share a history.
        """
        if game.status == "ended":
            return
        sig = self._detect_instance_reset(game, row)
        if sig:
            game = self._split_instance(game, row, comp, signal=sig, path="list")
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
            self._record_clean(game, obs)
        self._unseen_ticks[game.classification][
            f"{row.home_team}|{row.away_team}"
        ] = 0

    def _record_clean(self, game: PokerBetGame, obs: MarketObservation) -> None:
        """Feed one VALIDATED observation into the clean metrics DB.

        Called only after ``insert_snapshot`` accepted the row (so exact
        capture duplicates are never re-recorded) and only for the live
        instance the collector wrote to — the replay protections already
        routed post-final frames to a fresh #iN instance, never back to
        the finished base.  Failure-isolated: any clean-metrics error is
        logged and swallowed so collection is unaffected."""
        if self.clean_metrics is None:
            return
        try:
            self.clean_metrics.record_snapshot_obs(game, obs, self.store)
            self._refresh_projections(game.source_game_id)
        except Exception:
            logger.error("clean metrics record failed:\n%s",
                         traceback.format_exc())

    def _refresh_projections(self, source_game_id: str) -> None:
        """Recompute the deterministic pace-trajectory rows for one game
        from its VALID clean observations (idempotent, failure-isolated).
        Called as observations arrive so the subsequent-observation
        linkage stays current; also called on finalize so
        final_settled_total updates when the game completes."""
        if self.clean_metrics is None:
            return
        try:
            PaceProjector().refresh_game(self.clean_metrics, source_game_id)
        except Exception:
            logger.error("pace projector refresh failed:\n%s",
                         traceback.format_exc())
        # Deviation benchmark: residuals + provisional z-scores for any new
        # eligible trajectory rows (idempotent, never rewrites history).
        if self._deviation is not None:
            try:
                self._deviation.refresh_game(source_game_id)
            except Exception:
                logger.error("deviation refresh failed:\n%s",
                             traceback.format_exc())

    def _finalize_clean(
        self, game: PokerBetGame, obs: Optional[MarketObservation] = None,
    ) -> None:
        """Record the final result on the clean game/instance record.

        ``obs`` carries the verified final scores when the event view
        observed the terminal state; otherwise the game ended unseen and
        the clean record is finalized as UNKNOWN (NULL finals)."""
        if self.clean_metrics is None:
            return
        try:
            self.clean_metrics.finalize(
                game.source_game_id,
                final_home=obs.home_score if obs is not None else None,
                final_away=obs.away_score if obs is not None else None,
                classification=game.classification,
            )
            self._refresh_projections(game.source_game_id)
        except Exception:
            logger.error("clean metrics finalize failed:\n%s",
                         traceback.format_exc())

    def _capture_event_state(
        self, page: Optional[Page], cls: Classification, game: PokerBetGame,
        event_text: str, end: bool = False,
    ) -> bool:
        """Capture full event-view market state + reconcile.

        Identity guard: the parsed scoreboard TEAMS must match the tracked
        game — lobby text and foreign events are NEVER stored (they would
        contaminate the history and, before the guard, fragmented every
        fixture).  With ``end=True`` the game is marked ended afterwards
        (a verified final state observed via the event view)."""
        try:
            parsed = parse_event_view(event_text)
            if not self._verified_event_view(game, parsed):
                self._event_view_failures += 1
                logger.warning(
                    "event view unverified for %s (parsed teams %r/%r) — "
                    "skipped (unverified=%d)",
                    game.source_game_id, parsed.get("home_team"),
                    parsed.get("away_team"), self._event_view_failures,
                )
                return False
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
                self._record_clean(game, obs)
            if page is not None:
                self._reconcile(game, page.url, event_text, parsed)
            if end:
                self._end_game(game)
                self._finalize_clean(game, obs)
            return True
        except Exception:
            logger.error("event capture failed:\n%s", traceback.format_exc())
            return False

    @staticmethod
    def _is_final_state(parsed: dict) -> bool:
        """True when a VERIFIED event view shows the game's completed final
        state: a 4th-quarter label with the panel's period-over sentinel
        clock (21:00), 00:00, or an explicit finished label."""
        p = (parsed.get("period_label") or "").lower()
        clock = (parsed.get("clock") or "").strip()
        if any(k in p for k in ("full time", "finished", "end of match", "match ended")):
            return True
        if "4th" not in p:
            return False
        return clock in ("21:00", "00:00", "0:00", "")

    def _end_game(self, game: PokerBetGame) -> None:
        """Mark a tracked game ended (verified final observed) and stop
        tracking it — the next replay of the fixture re-resolves as a new
        instance on its first positive-evidence drop/regression."""
        if game.status != "ended":
            game.status = "ended"
            self.store.upsert_game(game)
        cls_val = game.classification
        key = f"{game.home_team}|{game.away_team}"
        self._tracked.get(cls_val, {}).pop(key, None)
        self._unseen_ticks.get(cls_val, {}).pop(key, None)
        if game.source_game_id in self._market_queue:
            self._market_queue.remove(game.source_game_id)
        last = self.store.get_snapshots(game.source_game_id, limit=1)
        lr = last[0] if last else {}
        logger.info(
            "verified final for %s — game ended (final %s-%s %s %s)",
            game.source_game_id, lr.get("home_score"), lr.get("away_score"),
            lr.get("period_label"), lr.get("clock"),
        )
        self.stats["games_ended_final"] += 1

    @staticmethod
    def _first_team_total(parsed: dict, index: int) -> Optional[float]:
        vals = [v.get("line") for v in parsed["team_totals"].values()]
        if index < len(vals):
            return vals[index]
        return None

    def _never_line_gids(self) -> set[str]:
        """Tracked games that have NEVER had a verified MatchTotal market
        line persisted (no market_observations MatchTotal row AND no
        snapshot-carried total_line).  These are the games whose early
        checkpoints (10/20/30%) are at risk of missing the line — the
        slow event-view rotation prioritises them so their first line is
        captured as early as possible."""
        gids = set()
        for games in self._tracked.values():
            for g in games.values():
                if g.status == "ended":
                    continue
                gid = g.source_game_id
                if not gid or not g.source_url:
                    continue
                has_line = self.store.game_has_market_line(gid)
                if not has_line:
                    gids.add(gid)
        return gids

    def _ensure_slow_page(self) -> None:
        """Create (once) the dedicated SLOW event-view page in the same
        browser/context as the fast page — so the slow path shares the
        browser but never the fast page's URL/state.  Both pages attach
        their own eu-swarm WS hook (raw market feed stays independent)."""
        try:
            if self._pw is None or self._browser is None:
                return
            context = self._browser.contexts[0] if self._browser.contexts \
                else self._browser.new_context(**self._session_options())
            page = context.new_page()
            self._slow_page = page
            self._slow_browser_started_at = time.monotonic()
            self._attach_ws_market_hook(page)
            # land on the discovery page so row clicks hydrate
            self._goto(page, competition_url(
                Classification.CYBER_2K26, self.comp_ids))
            self._wait_panel(page)
            logger.info("slow event-view page ready")
        except Exception:
            logger.error("slow page init failed:\n%s", traceback.format_exc())
            self._slow_page = None

    def _capture_slow_market(self) -> None:
        """Round-robin full event-view capture on the SLOW page — the
        decoupled equivalent of the old in-tick ``_capture_next_market``.
        Runs once per EVENT_VIEW_EVERY_N fast ticks.  Only the slow page
        navigates; the fast page stays on the lobby for the score poll.
        ``_capture_event_state``/``_end_game`` may rotate the browser —
        on rotation the slow page is recreated on the next guard.

        Early-checkpoint priority (2026-09-04): the eu-swarm WS feed only
        pushes the OPEN event's markets (verified: every WS frame carries
        exactly one game), so a non-open game's ONLY line source is this
        event-view rotation.  The uniform 480s refresh gate let a
        newly-tracked young game sit at the back of the queue until it
        was already past the 10/20/30% checkpoints — the dominant cause
        of missing early market lines (65% of checkpoint rows had none).
        A game that has NEVER had a MatchTotal line captured is treated
        as not-yet-covered and is visited immediately (its first
        event-view capture happens on the next slow run, capturing the
        line as early as the game's current elapsed state), instead of
        waiting for the round-robin to reach it."""
        page = self._slow_page
        if page is None or not self._market_queue:
            return
        now = utcnow_iso()
        captured = 0
        # Snapshot of which games still lack any verified MatchTotal line.
        # Consulted per slow run (cheap: one indexed SELECT per candidate
        # only when the fast path found no line for that game).
        never_line = self._never_line_gids()
        for _ in range(len(self._market_queue)):
            if captured >= MARKET_BATCH:
                break
            if self._event_view_failures >= BROWSER_RELAUNCH_AFTER_EMPTY:
                logger.warning("event-view failures %d — rotating browser",
                               self._event_view_failures)
                self._relaunch("event-view failure storm")
                self._event_view_failures = 0
                self._slow_page = None          # recreate on next guard
                break
            gid = self._market_queue.pop(0)
            self._market_queue.append(gid)
            last = self._last_market_at.get(gid)
            # Early-checkpoint priority: a game that has NEVER had a
            # verified market total line is not-yet-covered — visit it
            # regardless of the freshness gate (unless we captured it
            # within the last few seconds).  This gets young games their
            # first line at the earliest possible checkpoint.
            if gid in never_line:
                if last and _ts_age_s(last) < 15:
                    self._market_stats["skipped_fresh_arms"] += 1
                    continue  # just visited; avoid a hot loop
            elif last and _ts_age_s(last) < MARKET_REFRESH_S:
                # Fresh-observation gate: armed ONLY by an actual observed
                # market capture (attempt != observation).  While the
                # gate is open (no successful observation yet) the game
                # is retried every slow run instead of being skipped for
                # a whole window with a broken/failed feed.
                self._market_stats["skipped_fresh"] += 1
                continue  # fresh OBSERVATION — leave room for others
            self._market_stats["attempts"] += 1
            last_attempt = self._last_market_attempt_at.get(gid)
            if (last_attempt and _ts_age_s(last_attempt) < MARKET_ATTEMPT_BACKOFF_S
                    and self._attempt_streak.get(gid, 0)
                    >= MARKET_ATTEMPT_STREAK_BACKOFF):
                self._market_stats["skipped_backoff"] += 1
                continue  # failing repeatedly — bounded retry, still queued
            game = self._find_tracked(gid)
            if game is None or not game.source_url:
                continue
            cls = Classification(game.classification)
            logger.info("slow event view for game %s", gid)
            try:
                self._last_market_attempt_at[gid] = now
                # The SPA event route hydrates only from the matching
                # lobby — a stale event page or foreign lobby makes every
                # row-click fail, starving whole leagues (CYBER_2K26).
                if not self._ensure_comp_lobby(page, cls):
                    logger.warning(
                        "slow event view: %s lobby unreachable for %s — skipped",
                        cls.value, gid)
                    self._market_stats["failed"] += 1
                    self._attempt_streak[gid] = self._attempt_streak.get(gid, 0) + 1
                    continue
                if not self._click_tracked_row(page, game):
                    self._market_stats["failed"] += 1
                    self._attempt_streak[gid] = self._attempt_streak.get(gid, 0) + 1
                    continue
                text = page.inner_text("body", timeout=10000)
                parsed = parse_event_view(text)
                if not self._verified_event_view(game, parsed):
                    # The eu-swarm feed subscribed by this visit is an
                    # independent, verified capture of the SAME game — when
                    # the WS→snapshot bridge persisted during the dwell the
                    # visit succeeded even though the DOM parse failed.
                    ws_at = self._ws_snap_last.get(gid)
                    ws_ok = ws_at is not None and _ts_age_s(ws_at) < 10
                    if not ws_ok:
                        self._event_view_failures += 1
                        self._market_stats["failed"] += 1
                        self._attempt_streak[gid] = self._attempt_streak.get(gid, 0) + 1
                        logger.warning(
                            "event view unverified for %s (parsed teams %r/%r) — "
                            "skipped (unverified=%d)",
                            gid, parsed.get("home_team"), parsed.get("away_team"),
                            self._event_view_failures)
                        continue
                    self._event_view_failures = 0
                    self._last_market_at[gid] = now
                    self._last_market_attempt_at[gid] = now
                    self._attempt_streak[gid] = 0
                    self._market_stats["observed"] += 1
                    captured += 1
                    logger.info(
                        "event view unverified for %s but WS bridge captured "
                        "market — visit counted", gid)
                    continue
                self._event_view_failures = 0
                eh, ea = parsed.get("home_score"), parsed.get("away_score")
                if eh is None or ea is None:
                    self._market_stats["failed"] += 1
                    self._attempt_streak[gid] = self._attempt_streak.get(gid, 0) + 1
                    continue
                prev = self.store.get_snapshots(game.source_game_id, limit=1)
                prev_tot = None
                if prev:
                    ph, pa = prev[0].get("home_score"), prev[0].get("away_score")
                    if ph is not None and pa is not None:
                        prev_tot = ph + pa
                cur = int(eh) + int(ea)
                if self._is_final_state(parsed) and (
                        prev_tot is None or cur >= prev_tot):
                    self._capture_event_state(page, cls, game, text, end=True)
                    captured += 1
                    continue
                sig = self._detect_event_reset(
                    game, int(eh), int(ea),
                    parsed.get("period_label"), parsed.get("clock"))
                if sig:
                    row = RowGame(home_score=int(eh), away_score=int(ea))
                    game = self._split_instance(
                        game, row, cls, signal=sig, path="event")
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
                    url_cls = classify_event_url(page.url)
                    if url_cls != Classification.UNKNOWN:
                        game.classification = url_cls.value
                        game.game_family = url_cls.game_family.value
                    self.store.upsert_game(game)
                self._capture_event_state(page, cls, game, text)
                captured += 1
                self._last_market_at[gid] = now
                self._last_market_attempt_at[gid] = now
                self._attempt_streak[gid] = 0
            except Exception:
                logger.error("slow market capture failed:\n%s",
                             traceback.format_exc())
                self._market_stats["failed"] += 1
                self._attempt_streak[gid] = self._attempt_streak.get(gid, 0) + 1
            finally:
                # back to the discovery page for the next slow capture
                try:
                    self._goto(page, competition_url(cls, self.comp_ids))
                    self._wait_panel(page)
                    page.wait_for_timeout(1500)
                except Exception:
                    logger.error("slow return-to-lobby failed:\n%s",
                                 traceback.format_exc())
                    self._slow_page = None      # recreate on next guard

    def _ensure_comp_lobby(self, page: Page, cls: Classification) -> bool:
        """Make sure ``page`` shows a live LOBBY containing ``cls`` rows.

        The SPA's event-view route hydrates only from an in-app row click
        on the matching lobby list.  A stale event page or a foreign
        competition's lobby makes every click fail — the cross-lobby
        starvation that left whole leagues (CYBER_2K26) with zero market
        capture for hours.  Reload the competition lobby until rows exist.
        """
        try:
            for _ in range(3):
                has_rows = page.evaluate(
                    "() => document.querySelectorAll("
                    "'.market-game-section').length > 0")
                if has_rows:
                    return True
                url = competition_url(cls, self.comp_ids)
                if not self._goto(page, url):
                    continue
                self._wait_panel(page)
                self._expand_target_sections(page)
            return False
        except Exception:
            logger.error("ensure comp lobby failed:\n%s",
                         traceback.format_exc())
            return False

    def _click_tracked_row(self, page: Page, game: PokerBetGame) -> bool:
        """Open ``game``'s event view by clicking its panel row.

        The SPA's event-view route only hydrates via an in-app row click;
        a direct goto of the event-view URL falls back to the lobby.
        """
        try:
            clicked = page.evaluate(
                """(names) => {
                    const rows = document.querySelectorAll('.market-game-section');
                    for (const r of rows) {
                        const rnames = [...r.querySelectorAll('.market-game-team-name')]
                            .map(n => n.textContent.trim());
                        if (rnames.length >= 2 && rnames[0] === names.home
                                && rnames[1] === names.away) {
                            r.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                {"home": game.home_team, "away": game.away_team},
            )
            if not clicked:
                logger.warning(
                    "row not found for %s (%s vs %s) — skipping event view",
                    game.source_game_id, game.home_team, game.away_team)
                return False
            page.wait_for_timeout(2500)  # event view hydration
            return True
        except Exception:
            logger.error("row click failed:\n%s", traceback.format_exc())
            return False

    @staticmethod
    def _same_team(a: Optional[str], b: Optional[str]) -> bool:
        return (a or "").strip().casefold() == (b or "").strip().casefold()

    def _verified_event_view(self, game: PokerBetGame, parsed: dict) -> bool:
        """True when the parsed page is THIS game's event view.

        The event-view scoreboard always carries the teams; the lobby page
        (goto fallback) has no scoreboard teams within the parse window and
        a different event carries different teams.  Only verified pages may
        be stored — unverified content previously attributed another game's
        score to every fixture and fragmented every instance.
        """
        ph, pa = parsed.get("home_team") or "", parsed.get("away_team") or ""
        return bool(ph and pa) \
            and self._same_team(ph, game.home_team) \
            and self._same_team(pa, game.away_team)

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
                if self._unseen_ticks[cls_val][key] >= self._ended_grace_ticks:
                    if game.status != "ended":
                        game.status = "ended"
                        self.store.upsert_game(game)
                        logger.info("game ended (disappeared): %s", game.source_game_id)
                        self._finalize_clean(game)
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
    ap.add_argument("--tick", type=float, default=TICK_DEFAULT, help="tick interval seconds")
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
