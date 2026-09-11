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

FAST / SLOW SPLIT (2026-09-10 — hard 5s live-cycle requirement):

  FAST PATH (the live collection cycle, monotonic deadline scheduler,
  FAST_TICK_S=5s): lobby panel parse (score / period / clock / progress),
  list snapshots, WS MatchTotal ingestion (pushed by the feed itself on
  the socket thread), WS subscription refresh, market-freshness
  bookkeeping, persistence + the collector heartbeat.  Nothing here may
  wait on Playwright navigation; a healthy lobby parse is a single
  page.content() round-trip.

  SLOW PATH (a dedicated WORKER THREAD owning its own sync_playwright
  scope + its own browser + its own page): event-view navigation, page
  hydration waits, DOM enrichment, supplementary metadata AND new-game
  identity resolution (a brand-new game's first durable id — a ~7s row
  click + navigation that must never sit inside a 5s fast cycle; until
  resolved the game is tracked provisionally by its panel key and simply
  has no snapshots yet).  Sync Playwright objects are thread-affine, so
  the worker owns its browser lifecycle end-to-end; the fast loop and the
  worker communicate only through thread-safe state (the lock-guarded
  tracked map, the resolve queue, the round-request event) and the
  thread-safe SQLite stores.  The fast path NEVER joins or waits on the
  worker — a 1s, 10s, 60s or hung 130s event-view operation cannot
  stretch a fast cycle.

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
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

from playwright.sync_api import (
    Browser,
    Page,
    sync_playwright,
)
# TargetClosedError is the canonical playwright "page/browser went away"
# signal.  Recent playwright releases stopped re-exporting it from
# ``playwright.sync_api`` and keep it in ``playwright._impl._errors``;
# resolve it from whichever module actually exposes it so the collector
# import works across playwright versions (and degrade to a private
# sentinel that never matches rather than breaking import if it is
# renamed again — the catch sites then simply rely on their fallback
# Exception handling).
try:  # playwright >= 1.4x
    from playwright.sync_api import TargetClosedError
except ImportError:  # newer releases: not re-exported
    try:
        from playwright._impl._errors import TargetClosedError
    except ImportError:  # pragma: no cover - future rename
        class TargetClosedError(Exception):
            """Fallback sentinel if playwright stops exposing the class."""

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
# ── FAST / SLOW split (2026-09-10 — hard 5s live-cycle requirement) ──
# The FAST path is the actual live collection cadence: score, period,
# clock, progress, list snapshots, WS ingestion + subscription refresh,
# freshness bookkeeping and persistence.  It is scheduled on a monotonic
# DEADLINE grid (FAST_TICK_S between the START of consecutive fast
# cycles, measured — never sleep-after-work) and shares NOTHING with the
# slow event-view path except thread-safe handles (the lock-guarded
# tracked map, the resolve queue and the SQLite stores).  The fast
# cadence IS what the 5-second requirement is measured against;
# TICK_DEFAULT aliases it so the CLI default and the scheduler grid can
# never drift apart.
FAST_TICK_S = 5.0
TICK_DEFAULT = FAST_TICK_S
# Slow-path worker: ONE daemon thread owning its OWN sync_playwright
# scope + browser + page (sync Playwright objects are thread-affine —
# they must never cross threads).  The fast path only PASSES it work
# requests — never a Playwright object — and never joins or waits on it.
# The worker wakes ~every 2s, claims pending resolution/rotation work,
# and a round may take a minute; the fast cadence is unaffected.
SLOW_WORKER_POLL_S = 2.0
# New-game identity resolutions attempted per slow round (each is a
# ~7s row-click + navigation; two per round keeps a wave of new games
# from starving the market rotation for long).
RESOLVE_BATCH = 2
# A provisional (unresolved) game re-requests identity resolution at
# most this often (the row click can legitimately fail while the SPA
# hydrates; bounded retry, never a hot loop).
RESOLVE_RETRY_S = 30.0

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
# STEP 3 (2026-09-10): the slow (full event-view) path runs on its OWN
# PAGE in its OWN THREAD — fully decoupled from the fast cadence.  The
# old in-tick gate (EVENT_VIEW_EVERY_N fast ticks between slow runs) is
# retired: the fast loop now only requests a slow round when the last
# round STARTED at least EVENT_VIEW_MIN_INTERVAL_S ago, and the slow
# worker picks it up asynchronously.  A 6–20s (or hung 45s-timeout)
# event-view operation therefore cannot stretch a fast cycle.  Each slow
# round still captures up to MARKET_BATCH games, so the rotation capacity
# is unchanged (~3 games / ~20s round; a 40-game cohort rotates in well
# under the old ~11 min bound while the fast cadence stays at 5s).
EVENT_VIEW_MIN_INTERVAL_S = 10.0
# In-memory tick-timing ring (instrumentation): keep ~24h of samples at
# the fast cadence, then drop the oldest — a daemon must never grow
# without bound.  The summary is exposed via the collector state payload.
# 17280 x 5s = 24h of fast cycles.
TICK_STATS_MAX = 17280
# eu-swarm market observations: dedupe identical (game, line) frames within
# this window — the feed pushes every price change, so movements still land,
# but a game that stays flat is not spammed into the DB every second.
WS_MARKET_DEDUP_S = 30.0

# ── LIVE market freshness target (2026-09-10) ────────────────────────
# Requirement: every ACTIVE live game's SCRAPED market data is refreshed
# at ~30s.  The event-view rotation alone cannot deliver that — one
# sequential visit costs ~6-20s and the round-robin period for ~47 games
# measured ~20 min (mean tick cycle 36.7s x EVENT_VIEW_EVERY_N=2 x
# MARKET_BATCH=3).  The eu-swarm feed is per-SUBSCRIBED-EVENT, but the
# socket is authenticated once per page, so ONE socket holds market
# subscriptions for MANY games at once (verified live 2026-09-10: a
# second subscribe on the same socket delivered another game's
# MatchTotal).  This layer subscribes the fast page's swarm socket to
# every active game -> continuous per-game pushes, no navigation.
LIVE_MARKET_FRESH_TARGET_S = 30.0
# Re-send a game's subscription this often, so an expiring server-side
# subscription can never let an active game fall below target.
WS_SUB_REFRESH_S = 20.0
# Safety valve: cap games subscribed per sync pass (0 = no cap).
WS_SUB_MAX_GAMES = 0
# A game counts as ACTIVE in the freshness report only while the collector
# is still collecting it (lobby sighting or market observation inside the
# window).  A tracked game that has gone quiet longer than this is
# "dormant" — its age measures bookkeeping lag for an instance that has
# ended, not provider freshness — so it is reported separately instead of
# inflating the percentiles.
ACTIVE_GAME_WINDOW_S = 300.0

# Injected into every context BEFORE page scripts: keep a handle to the
# authenticated eu-swarm WebSocket so market subscriptions can be added
# for any game.  Static constants are preserved (app code compares
# readyState against WebSocket.OPEN).
_WS_CAPTURE_JS = """
(function () {
  try {
    var Orig = WebSocket;
    function Patched(url, protocols) {
      var ws = (protocols === undefined) ? new Orig(url) : new Orig(url, protocols);
      try { if (String(url).indexOf('swarm') >= 0) { window.__blmSwarm = ws; } } catch (e) {}
      return ws;
    }
    Patched.prototype = Orig.prototype;
    ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(function (k) {
      try { Patched[k] = Orig[k]; } catch (e) {}
    });
    window.WebSocket = Patched;
  } catch (e) {}
})();
"""

# The SPA's own single-event market request shape (captured from the live
# client).  Only `where.game.id` differs per game — the server pushes the
# full market tree (incl. MatchTotal) for every subscribed game.
_WS_SUBSCRIBE_WHAT: dict[str, Any] = {
    "sport": ["name"],
    "region": ["name"],
    "competition": ["name", "id"],
    "game": ["id", "stats", "info", "is_neutral_venue", "add_info_name",
             "text_info", "markets_count", "type", "start_ts",
             "is_stat_available", "team1_id", "team1_name", "team2_id",
             "team2_name", "last_event", "live_events", "match_length",
             "sport_alias", "sportcast_id", "region_alias", "is_blocked",
             "show_type", "game_number"],
    "market": ["id", "group_id", "group_name", "group_order", "type",
               "name_template", "sequence", "point_sequence", "market_type",
               "base", "name", "order", "display_key", "col_count",
               "express_id", "extra_info", "cashout", "is_new",
               "available_for_betbuilder", "has_early_payout"],
    "event": ["id", "type_1", "price", "name", "base", "home_value",
              "away_value", "display_column", "order", "type_id"],
}

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


def _percentiles(values: list[float]) -> dict[str, Optional[float]]:
    """P50/P95/P99/MAX (ms) over a list of DURATION samples (seconds).

    Nearest-rank percentiles on the sorted sample — never an average in
    disguise.  Empty sample → all None (a just-started collector reports
    no timing rather than a fabricated zero).
    """
    if not values:
        return {"p50": None, "p95": None, "p99": None, "max": None}
    vals = sorted(values)

    def _p(q: float) -> float:
        idx = min(len(vals) - 1, int(round(q * (len(vals) - 1))))
        return vals[idx] * 1000.0

    return {
        "p50": round(_p(0.50), 1),
        "p95": round(_p(0.95), 1),
        "p99": round(_p(0.99), 1),
        "max": round(vals[-1] * 1000.0, 1),
    }


def _tick_timing_summary(stats: dict[str, deque]) -> dict[str, Any]:
    """Reduce the bounded fast-cycle timing ring to a small status payload.

    Pure + testable.  The 5-second requirement is audited from the
    ACTUAL interval between consecutive fast-cycle STARTS
    (fast_cycle_interval_ms — start-to-start, never sleep-after-work)
    plus the split fast_work_ms / slow_event_view_ms / ws_subscription_ms /
    persistence_ms; every field carries P50/P95/P99/MAX nearest-rank
    percentiles so an overrun can never hide behind a mean.  The legacy
    mean_* keys are kept for continuity with existing dashboards.
    """
    def _mean_ms(key: str) -> Optional[float]:
        buf = stats.get(key)
        if not buf:
            return None
        return round((sum(buf) / len(buf)) * 1000.0, 1)

    out: dict[str, Any] = {"n": len(stats.get("work", []))}
    for key in ("fast_cycle_interval_ms", "fast_work_ms",
                "slow_event_view_ms", "ws_subscription_ms",
                "persistence_ms"):
        out[key] = _percentiles(list(stats.get(key, ())))
    out.update({
        "mean_work_ms": _mean_ms("work"),
        "mean_sleep_ms": _mean_ms("sleep"),
        "mean_cycle_ms": _mean_ms("cycle"),
        "mean_sub_ms": _mean_ms("sub"),
    })
    ev = stats.get("event")
    out["last_event_ms"] = round(ev[-1] * 1000.0, 1) if ev else None
    return out


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

        # tracked games: classification -> team-key -> PokerBetGame.
        # SHARED across the fast thread and the slow worker — every read/
        # write happens under _track_lock (the dict values themselves are
        # replaced, never mutated in place, so a reader under the lock
        # always sees a consistent game object).
        self._track_lock = threading.RLock()
        self._tracked: dict[str, dict[str, PokerBetGame]] = {
            cls.value: {} for cls in (Classification.CYBER_2K26, Classification.BETUAL_NBA)
        }
        self._unseen_ticks: dict[str, dict[str, int]] = {
            cls.value: {} for cls in (Classification.CYBER_2K26, Classification.BETUAL_NBA)
        }
        # Provisional games: discovered on the panel but not yet resolved
        # to a durable event id.  Keyed by the panel key; the slow worker
        # resolves them (row click -> event URL) off the fast path.
        self._pending_resolve: dict[str, float] = {}  # panel key -> last attempt (monotonic)
        self._resolved_ok = 0
        self._resolve_failures = 0
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
        # Bounded per-cycle duration ring (fast-work / slow event-view /
        # WS-subscription pass + the explicit 5s-requirement metrics),
        # summarized into the state payload.  Lives here, not in start(),
        # so EVERY entry point that drives _tick (--once, run_once, tests)
        # has it.
        self._tick_stats: dict[str, deque] = {
            key: deque(maxlen=TICK_STATS_MAX)
            for key in ("work", "sleep", "cycle", "event", "sub",
                        "fast_cycle_interval_ms", "fast_work_ms",
                        "slow_event_view_ms", "ws_subscription_ms",
                        "persistence_ms")}
        self._instances: dict[str, str] = {}  # base game_id -> current instance id
        self._running = False
        self._browser: Optional[Browser] = None
        self._pw: Any = None                  # active sync_playwright scope
        # STEP 3: SLOW event-view worker — its own thread + its own page.
        # The fast loop never navigates this page and never waits on the
        # worker: it only requests a round (thread-safe channel) when the
        # last round STARTED at least EVENT_VIEW_MIN_INTERVAL_S ago.  All
        # worker state is written by the worker alone.
        self._slow_page: Optional[Page] = None
        self._slow_pw: Any = None              # worker's own sync_playwright
        self._slow_browser: Optional[Browser] = None  # worker's OWN browser
        self._slow_browser_started_at: Optional[float] = None
        self._slow_thread: Optional[threading.Thread] = None
        self._slow_stop = threading.Event()
        self._slow_wake = threading.Event()   # a round was requested
        self._slow_job_pending = False        # fast -> worker round request
        self._slow_busy = False               # worker mid-round (diagnostics)
        self._slow_thread_error = ""
        self._slow_round_started_at: float = 0.0   # monotonic, worker writes
        self._last_slow_request_at: float = 0.0    # monotonic, fast writes
        self._slow_worker_started = False
        self._tick_no = 0                     # fast-loop tick counter
        self._empty_ticks = 0                 # consecutive empty-parses
        self._event_view_failures = 0         # consecutive unverified event views
        self._ws_market_last: dict[tuple[str, Optional[float]], str] = {}
        self._ws_snap_last: dict[str, str] = {}  # gid -> last bridge snapshot ts
        # LIVE market subscriptions (see LIVE_MARKET_FRESH_TARGET_S):
        # gid -> last subscribe ts.  ONE authenticated swarm socket holds
        # a market subscription per active game, so the feed pushes every
        # game's MatchTotal continuously instead of the slow rotation
        # visiting ~3 games per slow run.
        self._market_sub: dict[str, str] = {}
        self._market_sub_stats = {
            "passed": 0, "sent": 0, "games": 0, "errors": 0,
        }
        self._browser_started_at = 0.0
        self._started_at_iso = utcnow_iso()
        self._last_success_iso = ""
        self._last_error_iso = ""
        self._last_fast_started_at = 0.0      # monotonic (interval metric)
        self._fast_cycle_started_at_iso = ""  # last fast cycle start (heartbeat payload)
        self._fast_cycle_completed_at_iso = ""
        self._fast_cycle_overruns = 0         # cycles whose interval exceeded FAST_TICK_S
        self._prev_fast_started_at: Optional[float] = None  # interval baseline
        self._last_tick_page: Optional[Page] = None  # fast page (this process)
        # slow event-view timing (worker writes, summary reads)
        self._slow_round_durations: deque = deque(maxlen=TICK_STATS_MAX)
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

    def _new_context(self, browser: Browser):
        """A context with the swarm-socket capture hook installed.

        Every context (fast page, slow page, rotated) gets the hook so a
        market subscription can be added to its authenticated socket.
        """
        context = browser.new_context(**self._session_options())
        try:
            context.add_init_script(_WS_CAPTURE_JS)
        except Exception:
            logger.error("ws capture hook install failed:\n%s",
                         traceback.format_exc())
        return context

    def _new_session(self) -> Page:
        """Launch a fresh browser + context + page, land on the discovery page."""
        assert self._pw is not None, "sync_playwright scope not active"
        browser = self._pw.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        context = self._new_context(browser)
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
                        self._ingest_ws_observation(obs)
                except Exception:
                    # a malformed frame must never kill the collector
                    logger.debug("ws market frame error: %s",
                                 traceback.format_exc())
            ws.on("framereceived", on_frame)
        page.on("websocket", on_ws)

    def _ingest_ws_observation(self, obs: dict) -> None:
        """Persist one eu-swarm MatchTotal observation + its snapshot bridge.

        The feed keys frames to the event's BASE id while a live virtual
        replay is tracked as the current #iN instance — resolve through
        the SAME base → self._instances mapping the score/resolve path
        uses (never a second resolution system), then store the
        observation under the CURRENT instance id so it can never
        contaminate the completed first sim.  Unknown frames (no tracked
        game and no current instance) are safely ignored.  Dedup and the
        WS → snapshot bridge semantics are unchanged.
        """
        gid = obs["source_game_id"]
        last = self._ws_market_last.get((gid, obs["line_value"]))
        if last and _ts_age_s(last) < WS_MARKET_DEDUP_S:
            return
        self._ws_market_last[(gid, obs["line_value"])] = obs["captured_at"]
        game = self._find_tracked(gid)
        if game is None:
            cur = self._instances.get(gid)      # base -> current instance
            if cur:
                game = self._find_tracked(cur)
        if game is None:
            return
        # current-instance identity: never write a base-keyed frame back
        # onto the completed base game
        obs["source_game_id"] = game.source_game_id
        obs["game_id"] = self._game_db_id(game)
        try:
            self.store.upsert_market_observation(obs)
            # A pushed MatchTotal IS an observed market: arm the freshness
            # gate (and the freshness report) so the event-view rotation
            # never spends a visit re-fetching a game the socket already
            # covers.  Same key the slow path uses (tracked instance id).
            self._last_market_at[game.source_game_id] = obs["captured_at"]
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

    # ── LIVE market subscriptions (30s freshness) ────────────────

    def _active_games(self, window_s: Optional[float] = None) -> list[PokerBetGame]:
        """Tracked games that are not ended — the freshness universe.

        With ``window_s``, only games the collector is STILL collecting
        (lobby sighting or market observation inside the window) are
        returned; see ACTIVE_GAME_WINDOW_S.
        """
        out: list[PokerBetGame] = []
        with self._track_lock:
            for games in self._tracked.values():
                for g in games.values():
                    if g is None or getattr(g, "status", "") == "ended":
                        continue
                    if not g.source_game_id:
                        continue
                    if window_s is not None:
                        seen = (_ts_age_s(g.last_seen_at) if g.last_seen_at
                                else float("inf"))
                        obs = self._last_market_at.get(g.source_game_id)
                        obs_age = _ts_age_s(obs) if obs else float("inf")
                        if min(seen, obs_age) > window_s:
                            continue
                    out.append(g)
        return out

    def _market_subscribe_msg(self, game: PokerBetGame) -> Optional[str]:
        """The SPA's own market `get … subscribe:true` for ONE game.

        Only `where.game.id` differs per game — the same shape the live
        client sends when an event view opens, so the server pushes that
        game's full market tree (incl. MatchTotal) on the shared socket.
        Subscriptions are keyed to the event BASE id (the feed's own key),
        never a derived #iN instance id.
        """
        try:
            gid = int(self._base_id(game.source_game_id))
        except (TypeError, ValueError):
            return None
        where: dict[str, Any] = {"game": {"id": gid},
                                 "sport": {"alias": "Basketball"}}
        region = unquote((game.region or "").strip())
        if not region:
            region = unquote(DEFAULT_REGIONS.get(game.classification, ""))
        if region:
            where["region"] = {"alias": region}
        try:
            comp = int(game.competition_id) if game.competition_id else None
        except (TypeError, ValueError):
            comp = None
        if comp is not None:
            where["competition"] = {"id": comp}
        return json.dumps({
            "command": "get",
            "params": {"source": "betting", "what": _WS_SUBSCRIBE_WHAT,
                       "where": where, "subscribe": True},
            "rid": "blmsub-%s" % gid,
        })

    def _sync_market_subscriptions(self, page: Optional[Page]) -> None:
        """Keep a market subscription for EVERY active game on the fast
        page's authenticated swarm socket.

        This is the ~30s freshness mechanism.  The event-view rotation is
        capacity-bound (a few games per slow run), so market age was
        bounded by the round-robin period; a per-game subscription makes
        the feed PUSH each game's MatchTotal continuously, so the bound
        becomes the push cadence.  One evaluate call per pass; ingestion
        happens in the existing WS frame handler (unchanged semantics).
        """
        if page is None:
            return
        games = self._active_games()
        if WS_SUB_MAX_GAMES and len(games) > WS_SUB_MAX_GAMES:
            games = games[:WS_SUB_MAX_GAMES]
        batch: list[tuple[str, str]] = []
        for g in games:
            last = self._market_sub.get(g.source_game_id)
            if last is not None and _ts_age_s(last) < WS_SUB_REFRESH_S:
                continue
            msg = self._market_subscribe_msg(g)
            if msg:
                batch.append((g.source_game_id, msg))
        if not batch:
            return
        try:
            sent = page.evaluate(
                """(msgs) => {
                    const s = window.__blmSwarm;
                    if (!s || s.readyState !== 1) return -1;
                    let n = 0;
                    for (const m of msgs) { try { s.send(m); n++; } catch (e) {} }
                    return n;
                }""", [m for _, m in batch])
        except Exception:
            self._market_sub_stats["errors"] += 1
            return
        if sent is None or sent < 0:
            return                      # socket not open yet — retry next tick
        now = utcnow_iso()
        for gid, _ in batch:
            self._market_sub[gid] = now
        self._market_sub_stats["passed"] += 1
        self._market_sub_stats["sent"] += int(sent)
        self._market_sub_stats["games"] = len(games)

    def _freshness_summary(self) -> dict[str, Any]:
        """Age of the last OBSERVED market per ACTIVE game.

        Distinguishes the three cadences the operator must not conflate:
          - collection cadence  -> `subscription_refresh_s` (this layer)
          - market observation  -> the age percentiles below
          - frontend polling    -> the dashboard's own POLL_MS
        An age is the persisted observation time, never a fabricated or
        carried-forward value; a game with no observation is reported as
        `never_observed`, never as fresh.  Percentiles cover games the
        collector is still collecting; dormant tracked entries are counted
        separately so they cannot masquerade as provider staleness.
        """
        tracked = self._active_games()
        active = self._active_games(ACTIVE_GAME_WINDOW_S)
        ages: list[Optional[float]] = []
        for g in active:
            ts = self._last_market_at.get(g.source_game_id)
            ages.append(_ts_age_s(ts) if ts else None)
        known = sorted(a for a in ages if a is not None)
        n_known = len(known)

        def _pc(p: float) -> Optional[float]:
            if not n_known:
                return None
            return round(known[min(n_known - 1, int(n_known * p))], 1)

        def _share(pred) -> Optional[float]:
            if not n_known:
                return None
            return round(100.0 * sum(1 for a in known if pred(a)) / n_known, 1)

        return {
            "target_s": LIVE_MARKET_FRESH_TARGET_S,
            "collection_cadence_s": WS_SUB_REFRESH_S,
            "active_window_s": ACTIVE_GAME_WINDOW_S,
            "tracked_games": len(tracked),
            "dormant_games": len(tracked) - len(active),
            "active_games": len(ages),
            "with_market": n_known,
            "never_observed": len(ages) - n_known,
            "median_age_s": _pc(0.5),
            "p95_age_s": _pc(0.95),
            "max_age_s": round(known[-1], 1) if n_known else None,
            "pct_le_target": _share(lambda a: a <= LIVE_MARKET_FRESH_TARGET_S),
            "pct_gt_target": _share(lambda a: a > LIVE_MARKET_FRESH_TARGET_S),
            "pct_gt_60": _share(lambda a: a > 60),
            "pct_gt_120": _share(lambda a: a > 120),
            "subscriptions": {
                **self._market_sub_stats,
                "subscribed_now": sum(
                    1 for g in tracked if g.source_game_id in self._market_sub),
            },
        }

    def _fresh_context(self, reason: str) -> Page:
        """New context/page in the same browser (SPA state is per-context)."""
        try:
            if self._browser is None:
                return self._relaunch(reason)
            context = self._new_context(self._browser)
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
            # ── FAST-cycle heartbeat (5-second requirement audit) ──
            # ACTUAL timestamps + intervals of the fast path, never
            # configured values.  fast_cycle_overruns counts consecutive-
            # starts intervals that exceeded the FAST_TICK_S grid by more
            # than 0.5s; slow-path work can never stretch these.
            "fast_cycle": {
                "tick_s": self.tick_s,
                "started_at": self._fast_cycle_started_at_iso,
                "completed_at": self._fast_cycle_completed_at_iso,
                "overruns": self._fast_cycle_overruns,
                "pending_resolve": len(self._pending_resolve),
            },
            "slow_worker": {
                "alive": bool(self._slow_thread is not None
                              and self._slow_thread.is_alive()),
                "busy": self._slow_busy,
                "round_requested": self._slow_job_pending,
                "last_round_started_at": (
                    self._slow_round_started_at or None),
                "rounds": len(self._slow_round_durations),
                "resolve_ok": self._resolved_ok,
                "resolve_failures": self._resolve_failures,
                "error": self._slow_thread_error or None,
            },
            "tick_timing": _tick_timing_summary(self._tick_stats),
            # LIVE freshness: collection cadence vs observation age are
            # reported separately (see _freshness_summary).
            "market_freshness": self._freshness_summary(),
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
        # ── FAST deadline scheduler (2026-09-10: hard 5s live cadence) ──
        # The fast loop aims each cycle at the NEXT FAST_TICK_S boundary
        # from the PREVIOUS target: latency is absorbed INSIDE the
        # interval and never stacks on top of work, and when work overruns
        # (a hung page.content()) the next target advances by whole
        # intervals from the missed one — no sleep-after-work, no catch-up
        # burst, no overlapping cycles.  The SLOW worker thread (own
        # sync_playwright scope + browser) is started BEFORE the fast
        # session so a slow-path failure can never delay fast start-up.
        self._next_tick_target = 0.0
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
        # SLOW worker first: its failures are isolated from the fast loop
        self._start_slow_worker()
        try:
            with sync_playwright() as pw:
                self._pw = pw
                page = self._new_session()
                while self._running:
                    tick_start = time.monotonic()
                    self._last_fast_started_at = tick_start
                    self._fast_cycle_started_at_iso = utcnow_iso()
                    # schedule the NEXT target on the interval grid
                    if self._next_tick_target <= 0.0:
                        self._next_tick_target = tick_start + self.tick_s
                    prev_started = self._prev_fast_started_at
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
                    except TargetClosedError:
                        # the fast page's tab/browser died (SPA crash, OOM
                        # killer).  A dead tab is a FAST-path concern only —
                        # the slow worker owns its own browser and must
                        # never be dragged down with it.  Relaunch the fast
                        # session; the loop continues.
                        self.stats["errors"] += 1
                        self._last_error_iso = utcnow_iso()
                        logger.warning("fast page closed — relaunching: %s",
                                       exc)
                        try:
                            page = self._relaunch("fast page closed")
                        except Exception:
                            logger.error("relaunch failed, giving up:\n%s",
                                         traceback.format_exc())
                            self._running = False
                            break
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
                    self._prev_fast_started_at = tick_start
                    self._fast_cycle_completed_at_iso = utcnow_iso()
                    self._last_fast_completed_at = time.monotonic()
                    # SPA sessions degrade over hours — rotate regardless
                    if (time.monotonic() - self._browser_started_at
                            > BROWSER_MAX_LIFETIME_S):
                        page = self._relaunch("browser lifetime cap")
                    # target-boundary sleep: absorb latency, never stack
                    sleep_for = max(0.0, self._next_tick_target - time.monotonic())
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                    # advance the target by whole intervals past the missed one
                    while self._next_tick_target <= time.monotonic():
                        self._next_tick_target += self.tick_s
                    # ACTUAL interval metric: start-to-start (the 5-second
                    # requirement is measured between consecutive FAST
                    # cycle STARTS, independently of slow-path duration).
                    if prev_started is not None:
                        interval = tick_start - prev_started
                        self._tick_stats["fast_cycle_interval_ms"].append(
                            interval)
                        if interval > self.tick_s + 0.5:
                            self._fast_cycle_overruns += 1
                    self._tick_stats["sleep"].append(sleep_for)
                    self._tick_stats["cycle"].append(
                        time.monotonic() - tick_start)
        except Exception:
            logger.error("collector crashed:\n%s", traceback.format_exc())
        finally:
            self._stop_slow_worker()
            if self._browser:
                try:
                    self._browser.close()
                except Exception:
                    pass
            self._running = False

    def stop(self) -> None:
        self._running = False
        self._slow_stop.set()
        self._slow_wake.set()

    # ── SLOW worker (event-view path — own thread, own Playwright) ──

    def _start_slow_worker(self) -> None:
        """Start the dedicated slow-path worker thread.

        The worker owns its OWN sync_playwright scope and browser: sync
        Playwright objects are thread-affine and must never cross
        threads.  Communication with the fast loop is exclusively via
        thread-safe state: the lock-guarded tracked map, the pending-
        resolve dict, the round-request flag + Event, and the SQLite
        stores (each method takes its own connection under a lock).
        """
        if self._slow_worker_started:
            return
        self._slow_worker_started = True
        self._slow_thread = threading.Thread(
            target=self._slow_worker_main, name="blm-slow-worker",
            daemon=True)
        self._slow_thread.start()
        logger.info("slow event-view worker started")

    def _stop_slow_worker(self) -> None:
        self._slow_stop.set()
        self._slow_wake.set()
        t = self._slow_thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=10.0)

    def _slow_worker_main(self) -> None:
        """The slow path's whole lifecycle lives in THIS thread.

        Loop: wake (round requested / resolution pending / stop) → run at
        most one rotation round + up to RESOLVE_BATCH identity
        resolutions → sleep until the next wake.  Everything Playwright
        happens here, on this thread's own browser; a hung 45s-timeout
        navigation delays only the NEXT round, never the fast cadence.
        """
        try:
            with sync_playwright() as pw:
                self._slow_pw = pw
                try:
                    self._slow_browser = pw.chromium.launch(
                        headless=self.headless,
                        args=["--no-sandbox", "--disable-gpu",
                              "--disable-dev-shm-usage"],
                    )
                    self._ensure_slow_page()
                    logger.info("slow worker browser ready")
                except Exception:
                    self._slow_thread_error = traceback.format_exc()
                    logger.error("slow worker browser launch failed:\n%s",
                                 traceback.format_exc())
                while not self._slow_stop.is_set():
                    woke_for = "idle"
                    try:
                        # 1. identity resolutions first (young games need
                        #    their durable id + first snapshots early)
                        done = 0
                        while done < RESOLVE_BATCH:
                            claim = self._claim_pending_resolve()
                            if claim is None:
                                break
                            woke_for = "resolve"
                            cls, row = claim
                            ok = self._resolve_pending_on_worker(cls, row)
                            if not ok:
                                self._requeue_resolve(cls, row)
                            done += 1
                        # 2. the event-view rotation round (if requested)
                        if self._slow_job_pending:
                            self._slow_job_pending = False
                            woke_for = "round"
                            t0 = time.monotonic()
                            self._slow_round_started_at = t0
                            self._slow_busy = True
                            try:
                                self._capture_slow_market()
                            except TargetClosedError:
                                # slow page/browser died mid-round: NOT a
                                # fast-path concern — recreate the slow
                                # session on the next round.
                                logger.warning(
                                    "slow page closed mid-round — recreating")
                                self._slow_page = None
                            except Exception:
                                logger.error(
                                    "slow event-view capture error:\n%s",
                                    traceback.format_exc())
                            finally:
                                self._slow_busy = False
                                dur = time.monotonic() - t0
                                self._slow_round_durations.append(dur)
                                self._tick_stats["slow_event_view_ms"].append(dur)
                                self._tick_stats["event"].append(dur)
                        # slow-browser SPA degradation: rotate independently
                        if (self._slow_browser_started_at
                                and time.monotonic()
                                - self._slow_browser_started_at
                                > BROWSER_MAX_LIFETIME_S):
                            self._rotate_slow_browser()
                    except Exception:
                        # anything else in the worker must never kill the
                        # thread — log, brief backoff, continue.
                        self._slow_thread_error = traceback.format_exc()
                        logger.error("slow worker loop error:\n%s",
                                     traceback.format_exc())
                        self._slow_stop.wait(5.0)
                        continue
                    self._slow_wake.wait(SLOW_WORKER_POLL_S)
                    self._slow_wake.clear()
        except Exception:
            self._slow_thread_error = traceback.format_exc()
            logger.error("slow worker crashed:\n%s", traceback.format_exc())
        finally:
            try:
                if self._slow_browser is not None:
                    self._slow_browser.close()
            except Exception:
                pass
            logger.info("slow worker exited")

    def _requeue_resolve(self, cls: Classification, row: RowGame) -> None:
        """Put a failed identity resolution back with a fresh attempt
        stamp (bounded retry — the fast path re-queues at most once per
        RESOLVE_RETRY_S and the panel may drop the row at any time)."""
        with self._track_lock:
            key = (cls.value, f"{row.home_team}|{row.away_team}")
            self._pending_resolve[key] = {
                "queued_at": time.monotonic(),
                "attempted_at": time.monotonic(),
                "home": row.home_team, "away": row.away_team,
                "classification": cls.value,
            }
        self._resolve_failures += 1

    def _resolve_pending_on_worker(
            self, cls: Classification, row: RowGame) -> bool:
        """Resolve ONE provisional game on the slow worker's page.

        The row click navigates the SLOW page only; the fast page never
        leaves the lobby.  On success the durable game object is inserted
        into the tracked map under the lock (the fast path starts list-
        snapshotting it on its next cycle).
        """
        page = self._slow_page
        if page is None:
            return False
        key = f"{row.home_team}|{row.away_team}"
        try:
            if not self._ensure_comp_lobby(page, cls):
                logger.warning("resolve: %s lobby unreachable for %s — retried",
                               cls.value, key)
                return False
            if not self._click_panel_row(page, row):
                return False
            url = page.url
            tax = parse_event_url(url)
            if not tax:
                logger.warning("resolve: no event taxonomy after click: %s", url)
                return False
            text = page.inner_text("body", timeout=10000)
            home, away = self._authoritative_teams(row, tax, text)
            # dedup by durable identity (restart-safe, same as before)
            with self._track_lock:
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
                with self._track_lock:
                    self._tracked[cls_val].pop(old_key, None)
                    self._unseen_ticks[cls_val].pop(old_key, None)
                    new_key = f"{home}|{away}"
                    self._tracked[cls_val][new_key] = existing
                    self._unseen_ticks[cls_val][new_key] = 0
                    if existing.source_game_id not in self._market_queue:
                        self._market_queue.append(existing.source_game_id)
            else:
                game = self._build_game(cls, row, tax, url, home, away)
                new_id = self._restart_split_suffix(text, tax, game)
                if new_id:
                    game.source_game_id = new_id
                    self._instances[self._base_id(new_id)] = new_id
                    logger.info(
                        "restart-safe virtual replay split: %s -> %s",
                        tax["game_id"], new_id,
                    )
                gid = self.store.upsert_game(game)
                with self._track_lock:
                    self._tracked[cls.value][f"{home}|{away}"] = game
                    self._unseen_ticks[cls.value][f"{home}|{away}"] = 0
                    self._market_queue.append(game.source_game_id)
                self.stats["games_resolved"] += 1
                logger.info(
                    "resolved new %s game %s (%s vs %s) game_id=%s",
                    cls.value, gid, home, away, tax["game_id"],
                )
            # full market snapshot from the event page we're on (the
            # worker's first verified view of the game)
            with self._track_lock:
                game_now = self._tracked[cls.value].get(f"{home}|{away}")
            if game_now is not None:
                self._capture_event_state(page, cls, game_now, text)
            # back to the lobby for the next click
            self._goto(page, competition_url(cls, self.comp_ids))
            self._wait_panel(page)
            self._resolved_ok += 1
            return True
        except TargetClosedError:
            logger.warning("resolve: slow page closed for %s — recreating", key)
            self._slow_page = None
            return False
        except Exception:
            logger.error("resolve failed for %s:\n%s", key,
                         traceback.format_exc())
            return False
        finally:
            try:
                self._wait_panel(page)
            except Exception:
                pass

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

    # ── Main tick (FAST PATH — hard 5s live-cycle requirement) ───

    def _tick(self, page: Page) -> Page:
        """One FAST collection cycle.

        Fast-path contents ONLY: the single lobby ``page.content()``
        round-trip, list snapshots, WS subscription maintenance, freshness
        bookkeeping, persistence and the heartbeat.  Identity resolution
        and every event-view operation run on the SLOW worker thread —
        the fast path only queues the request and never waits for it, so
        a 1s / 10s / 60s / 130s slow operation cannot stretch this cycle.
        """
        t_tick = time.monotonic()
        self.stats["ticks"] += 1
        self._tick_no += 1
        logger.info("fast tick %d start (url=%s)", self.stats["ticks"], page.url)

        # 1. Parse the live panel (the fast path's ONLY Playwright
        #    round-trip; recovery navigations below are failure-only)
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
        to_snapshot: list[tuple[PokerBetGame, RowGame, object]] = []

        # 2. Snapshot the tracked state under the lock (pure dict reads —
        #    the worker mutates _tracked concurrently) and queue identity
        #    resolution for NEW rows.  The fast path NEVER clicks/navigates
        #    for a new game: resolution happens on the slow worker.
        with self._track_lock:
            for comp in comps:
                cls = comp.classification
                for row in comp.games:
                    key = f"{row.home_team}|{row.away_team}"
                    seen_keys[cls.value].add(key)
                    game = self._tracked[cls.value].get(key)
                    if game is None:
                        self._queue_resolve(cls, row)
                    else:
                        to_snapshot.append((game, row, cls))

        # 3. Persist list-level snapshots (DB work OUTSIDE the track lock;
        #    _store_list_snapshot re-takes it only around dict mutations)
        t_persist = time.monotonic()
        for game, row, cls in to_snapshot:
            try:
                self._store_list_snapshot(game, row, cls)
            except Exception:
                logger.error("list snapshot failed:\n%s",
                             traceback.format_exc())
        self._tick_stats["persistence_ms"].append(
            time.monotonic() - t_persist)

        # 3b. LIVE market freshness — keep a market subscription for
        #     EVERY active game on the fast page's authenticated socket
        #     so the feed pushes each game's MatchTotal continuously
        #     (target LIVE_MARKET_FRESH_TARGET_S).  Cheap: one evaluate.
        t0 = time.monotonic()
        try:
            self._sync_market_subscriptions(page)
        except Exception:
            logger.error("market subscription sync error:\n%s",
                         traceback.format_exc())
        sub_ms = time.monotonic() - t0
        self._tick_stats["sub"].append(sub_ms)
        self._tick_stats["ws_subscription_ms"].append(sub_ms)

        # 4. Mark unseen games ended + drop resolve requests for rows that
        #    vanished (bounded work: one locked pass over the maps)
        self._mark_ended(seen_keys)
        self._prune_pending_resolve(seen_keys)

        # 5. Request a SLOW round (event-view rotation) when due.  This is
        #    a flag + event set — never a join, never a wait: the worker
        #    picks it up on its own thread at its own pace.
        now_m = time.monotonic()
        if now_m - self._last_slow_request_at >= EVENT_VIEW_MIN_INTERVAL_S:
            self._last_slow_request_at = now_m
            self._slow_job_pending = True
            self._slow_wake.set()

        self.stats["games_seen"] = sum(len(v) for v in self._tracked.values())
        logger.info(
            "fast tick %d done in %.2fs: tracked=%d snapshots=%d errors=%d",
            self.stats["ticks"], time.monotonic() - t_tick,
            self.stats["games_seen"], self.stats["snapshots"],
            self.stats["errors"],
        )
        self._write_state(success=True)
        self._tick_stats["fast_work_ms"].append(time.monotonic() - t_tick)
        self._tick_stats["work"].append(time.monotonic() - t_tick)
        return page

    # ── Pending identity resolution (fast -> slow worker channel) ──

    def _queue_resolve(self, cls: Classification, row: RowGame) -> None:
        """Queue a NEW panel row for durable-identity resolution on the
        slow worker.  Called with _track_lock held (dict-only).  The retry
        gate (RESOLVE_RETRY_S) makes re-discovery before resolution a
        bounded no-op, never a hot loop."""
        key = (cls.value, f"{row.home_team}|{row.away_team}")
        entry = self._pending_resolve.get(key)
        now_m = time.monotonic()
        if entry is not None and (now_m - entry["queued_at"]) < RESOLVE_RETRY_S:
            return
        self._pending_resolve[key] = {
            "queued_at": now_m, "attempted_at": 0.0,
            "home": row.home_team, "away": row.away_team,
            "classification": cls.value,
        }
        self._slow_wake.set()

    def _prune_pending_resolve(self, seen_keys: dict[str, set[str]]) -> None:
        """Drop resolve requests whose panel row vanished (dict-only; safe
        under the lock)."""
        with self._track_lock:
            for key in list(self._pending_resolve):
                if key[1] not in seen_keys.get(key[0], set()):
                    del self._pending_resolve[key]

    def _claim_pending_resolve(
            self) -> Optional[tuple[Classification, RowGame]]:
        """Worker-side claim of ONE pending identity resolution.

        Lock-guarded pop; the claimed entry is re-inserted with a fresh
        attempt stamp on failure (by the worker) so the fast path's retry
        gate stays honest, and dropped on success."""
        with self._track_lock:
            if not self._pending_resolve:
                return None
            key = next(iter(self._pending_resolve))
            entry = self._pending_resolve.pop(key)
        cls = Classification(key[0])
        return cls, RowGame(home_team=entry["home"],
                            away_team=entry["away"])

    # ── Discovery & identity ─────────────────────────────────────

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
        self, cls: Classification, row: RowGame, tax: dict, url: str,
        home: str, away: str,
    ) -> PokerBetGame:
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
        with self._track_lock:
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
        with self._track_lock:
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
        with self._track_lock:
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
        with self._track_lock:
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
        """Create the dedicated SLOW event-view page on the WORKER'S OWN
        browser (sync Playwright objects are thread-affine — the worker
        thread owns its sync_playwright scope, browser and page end to
        end, and the fast loop never touches any of them).  The worker
        page attaches its own eu-swarm WS hook, so a verified event view
        on this page still bridges WS market frames exactly as before.
        SLOW-WORKER-ONLY: must be called from the worker thread."""
        try:
            if self._slow_browser is None:
                return
            context = self._slow_browser.contexts[0] if self._slow_browser.contexts \
                else self._new_context(self._slow_browser)
            page = context.new_page()
            self._slow_page = page
            self._slow_browser_started_at = time.monotonic()
            self._attach_ws_market_hook(page)
            # land on the discovery page so row clicks hydrate
            self._goto(page, competition_url(
                Classification.CYBER_2K26, self.comp_ids))
            self._wait_panel(page)
            logger.info("slow event-view page ready (worker browser)")
        except Exception:
            logger.error("slow page init failed:\n%s", traceback.format_exc())
            self._slow_page = None

    def _rotate_slow_browser(self) -> None:
        """Worker-side SPA-degradation rotation: close the worker's own
        browser and build a fresh one.  SLOW-WORKER-ONLY (worker thread)."""
        logger.warning("rotating slow worker browser (lifetime/SPA cap)")
        try:
            if self._slow_browser is not None:
                self._slow_browser.close()
        except Exception:
            pass
        self._slow_browser = None
        self._slow_page = None
        try:
            assert self._slow_pw is not None
            self._slow_browser = self._slow_pw.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-gpu",
                      "--disable-dev-shm-usage"],
            )
            self._ensure_slow_page()
        except Exception:
            logger.error("slow browser rotation failed:\n%s",
                         traceback.format_exc())
            self._slow_browser = None
            self._slow_page = None

    def _capture_slow_market(self) -> None:
        """Round-robin full event-view capture on the SLOW page — the
        decoupled equivalent of the old in-tick ``_capture_next_market``.
        Runs on the WORKER thread whenever the fast path requested a
        round (EVENT_VIEW_MIN_INTERVAL_S between round REQUESTS).  Only
        the slow page
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
                    self._slow_page = None      # worker recreates it

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

    def _click_panel_row(self, page: Page, row: RowGame) -> bool:
        """Open a PANEL ROW's event view by team names (resolver path).

        The SPA's event-view route only hydrates via an in-app row click;
        a direct goto of the event-view URL falls back to the lobby.
        SLOW-WORKER-ONLY (navigates the worker page)."""
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
                {"home": row.home_team, "away": row.away_team},
            )
            if not clicked:
                logger.warning(
                    "row not found for %s vs %s — skipping resolve",
                    row.home_team, row.away_team)
                return False
            page.wait_for_timeout(2500)  # event view hydration
            return True
        except Exception:
            logger.error("panel row click failed:\n%s",
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
        """Grace-based end detection (fast path).  DB writes happen
        OUTSIDE the track lock — only the map mutations are guarded."""
        with self._track_lock:
            to_end: list[PokerBetGame] = []
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
                            to_end.append(game)
                        # keep the game record; drop from live tracking
                        del games[key]
                        if game.source_game_id in self._market_queue:
                            self._market_queue.remove(game.source_game_id)
        for game in to_end:
            self.store.upsert_game(game)
            logger.info("game ended (disappeared): %s", game.source_game_id)
            self._finalize_clean(game)

    def _find_tracked(self, source_game_id: str) -> Optional[PokerBetGame]:
        """Lock-guarded scan (dict-only) — safe from any thread.  Callers
        that already hold the lock are fine: RLock re-entrance."""
        with self._track_lock:
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
