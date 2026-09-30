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
import contextlib
import signal
import socket
import sqlite3
import time
import traceback
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
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
    normalize_betual_team,
    parse_event_url,
    slugify_team,
)
from blm_v4.betual_dataset import (
    BetualDataset,
    BetualDatasetError,
    safe_record,
)
from blm_v4.clean_metrics import CleanMetricsStore
from blm_v4.deviation import DeviationEngine
from blm_v4.performance import PERFORMANCE
from blm_v4.pace_projector import PaceProjector
from blm_v4.discovery import (
    RowGame,
    discover_competitions,
    find_relevant_competitions,
)
from blm_v4.event_parser import parse_event_view
from blm_v4.projection import clock_minutes, duration_for
from blm_v4.models import (
    SOURCE_POKERBET,
    MarketObservation,
    PokerBetGame,
    utcnow_iso,
)
from blm_v4.reconcile import reconcile_event
from blm_v4.storage import PokerBetStore
from blm_v4.ws_market import normalize_observations, parse_market_frame
from blm_v4.quarter_validation import validate_quarter_scores

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

# ── SELF-WATCHDOG (2026-09-26: the collector must never be down) ──────
# A watchdog that runs INSIDE the process it guards cannot fire while that
# process is wedged, so liveness is signalled to systemd (sd_notify,
# WatchdogSec) from the FAST path, and a completely dead process is
# covered by Restart=always in the unit.  The failure this guards:
# a Playwright/SPA call blocks far longer than any in-process timeout
# (measured 6.8-minute gap between fast ticks during browser rotation,
# 2026-09-26 00:57→01:03) — the process is "alive" but no observation
# lands for minutes, and nothing inside the process can report it.
#
# gating (main collector only, NOT tests / run_once diagnostics):
#   * WATCHDOG_USEC set (systemd's watchdog interval, in microseconds)
#   * NOTIFY_SOCKET set (systemd supplies the socket for the unit)
#   * --tick >= self-watchdog cadence (diagnostic fast loops excluded)
#   * --once runs via run_once() and never calls start(), so it can
#     never arm the thread
SD_NOTIFY_AVAILABLE = True  # raw socket implementation — no dependency


def _sd_notify(state: str) -> bool:
    """Minimal systemd sd_notify (UNIX datagram, sd_notify(3) protocol).

    Dependency-free on purpose: the vendor `sdnotify` package is not
    installed here and the whole protocol is one datagram to the socket
    systemd supplies in NOTIFY_SOCKET.  Abstract-namespace sockets
    (leading '@') are supported per the spec.
    """
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return False
    if sock_path.startswith("@"):
        sock_path = "\0" + sock_path[1:]
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(sock_path)
            sock.sendall(state.encode("utf-8"))
        finally:
            sock.close()
        return True
    except Exception:
        # bulletproof: the watchdog must never die from a notification
        # failure.  OSError (bad path, connection refused, DNS resolution
        # failure on a malformed path) AND any other unexpected error must
        # be silently swallowed so the watchdog thread stays alive.
        return False


WATCHDOG_POLL_DEFAULT_S = 5.0  # check cadence; notify on every pass

# Bounded startup grace (2026-09-29 forensic follow-up): while NO fast
# cycle has ever completed, keep pinging for at most this long — capped
# at runtime at half of systemd's WatchdogSec — so a slow-but-alive
# first cycle under host load is not rewarded with a systemd kill.  The
# 2026-09-29 restart storms each killed a process inside its first ~60s
# (kill = last completed cycle + FAST_LIVENESS_FACTOR × FAST_TICK_S +
# WatchdogSec ≈ 120s); the grace re-covers exactly that fragile minute
# without weakening steady-state cycle-gated liveness.
WATCHDOG_STARTUP_GRACE_S = 60.0


def _startup_grace_seconds(watchdog_usec: Optional[str]) -> float:
    """Bounded startup grace: min(WATCHDOG_STARTUP_GRACE_S, WatchdogSec/2).

    ``watchdog_usec`` is the raw WATCHDOG_USEC environment value (string
    microseconds, or None when unset).  Unknown/invalid values return
    the un-capped default — the caller only pings MORE, never less.
    """
    grace = WATCHDOG_STARTUP_GRACE_S
    try:
        watchdog_sec_s = float(watchdog_usec) / 1e6  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return grace
    if watchdog_sec_s > 0.0:
        grace = min(grace, watchdog_sec_s / 2.0)
    return grace


def _block_deadline_signal() -> None:
    """Block the capture-deadline SIGALRM in the calling thread.

    ``signal.setitimer`` arms ONE process-wide real-time timer, and the
    kernel may deliver the signal to ANY thread that does not block it —
    not necessarily the thread that armed it.  The handler must only ever
    run on the capturing thread, so every other collector thread blocks it
    here.  See ``_page_content_timed`` for why a handler must not raise.
    """
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})


@contextlib.contextmanager
def _alarm_blocked() -> "Iterator[None]":
    """Run a block with the capture-deadline SIGALRM blocked, restoring
    the calling thread's PRIOR mask on exit.

    59f6791 made the deadline handler flag-only, so a late alarm can no
    longer abort a SQLite commit — but it can still fire while a WS
    frame callback (dispatched NESTED on the capturing thread while
    page.content() is in flight) is inside a store call: the flag write
    is harmless, yet the same delivery could land between the armed
    window and the finally that disarms it.  Persisting under a blocked
    mask keeps every store call's read-modify-write sequences atomic
    with respect to the alarm regardless of which thread runs them.

    The prior mask is RESTORED, not reset: the slow worker permanently
    blocks SIGALRM (_slow_worker_main calls _block_deadline_signal), so
    blindly unblocking would weaken that guarantee.  On threads without
    pthread_sigmask this is a no-op.
    """
    if not hasattr(signal, "pthread_sigmask"):
        yield
        return
    old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)
# A fast cycle is "fresh" when it completed within this multiple of the
# 5s tick.  6× = 30s: two full missed cycles plus margin, far below the
# minutes-long wedges observed, and below typical WatchdogSec values.
FAST_LIVENESS_FACTOR = 6.0
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

# ── Quarter-specific collection (directive 2026-09-22, DATA COLLECTION
# ONLY) ─────────────────────────────────────────────────────────────
# Raw-frame retention throttle: market frames for the SAME game arrive on
# nearly every WS message; storing each verbatim would dominate disk.  One
# frame per game per this window is retained (plus EVERY parse failure,
# which is never throttled).  0 disables the throttle entirely.
try:
    WS_RAW_FRAME_KEEP_S = abs(float(
        os.environ.get("BLM_WS_RAW_FRAME_KEEP_S", "300") or 0))
except (TypeError, ValueError):
    WS_RAW_FRAME_KEEP_S = 300.0

# Total-market types whose base/line is the GAME total (basketball).
# Everything else with an O/U base is treated as period-specific until
# proven otherwise — the source's exact period taxonomy is deliberately
# NOT assumed (§1: identify what the source actually exposes).
GAME_TOTAL_MARKET_TYPES = {"MatchTotal"}

# betting-agnostic period inference from a market NAME (English source
# wording; unknown names map to quarter_unknown — never guessed).
_QNUM_RE = re.compile(r"\b(?:1st|2nd|3rd|4th)\b")
_QNAME_MAP = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4}
_QNAMES = {"first", "second", "third", "fourth"}
_QNAME_NUM = {"first": 1, "second": 2, "third": 3, "fourth": 4}


def infer_market_period(name: Optional[str]):
    """(market_period, period_number) inferred from a market NAME.

    Q1..Q4 only when the name unambiguously says so; otherwise
    ('quarter_unknown', None) — NULL is preferable to an invented period
    (§5).  Everything else (odd/even, race-to-N, handicap etc.) gets
    ('other', None).
    """
    n = (name or "").strip()
    low = n.lower()
    if "quarter" in low or "period" in low:
        m = _QNUM_RE.search(n)
        if m:
            q = _QNAME_MAP[m.group(0)]
            return (f"Q{q}", q)
        for word, num in _QNAME_NUM.items():
            if word in low:
                return (f"Q{num}", num)
        return ("quarter_unknown", None)
    return ("other", None)

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
        home_team=normalize_betual_team(game.home_team),
        away_team=normalize_betual_team(game.away_team),
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
                "persistence_ms",
                # PHASE 2 buckets — full percentile exposure so the
                # overrun component can never hide behind a mean.
                "tick_total_ms", "game_discovery_ms",
                "page_content_ms", "score_parse_ms",
                "board_parse_ms", "line_parse_ms", "db_write_ms",
                "retry_ms", "ended_game_ms"):
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
        # Phase 5b: in-process cache for source_game_id → database id.
        # Games are inserted once and never re-inserted, so the mapping is
        # stable for the process lifetime.  Eliminates 25,000+ per-tick
        # SQL calls to get_game().
        self._game_id_cache: dict[str, int] = {}
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
                        "persistence_ms",
                        # Capture-efficiency instrumentation (directive
                        # 2026-09-30 PHASE 2): per-component tick timing so
                        # the overrun source is MEASURED, never assumed.
                        "tick_total_ms", "game_discovery_ms",
                        "page_content_ms", "score_parse_ms",
                        "board_parse_ms", "line_parse_ms", "db_write_ms",
                        "retry_ms", "ended_game_ms")}
        # ── Capture-efficiency state (directive 2026-09-30) ──────────
        # PHASE 3 lifecycle cache: gids whose capture stage is DONE.
        # 'ended' on the game object == DONE; this cache also holds gids
        # that left tracking via _mark_ended's disappeared path, so the
        # WS bridge can suppress them even after _tracked deletion.
        # In-memory only, tiny (one short str per ended game).
        self._end_state_terminal: dict[str, str] = {}
        # PHASE 4 bounded NULL-score recovery bookkeeping.
        self._null_score_streak: dict[str, int] = {}     # gid -> consecutive unrecovered retries
        self._null_score_backoff_until: dict[str, int] = {}  # gid -> tick_no until which re-reads are skipped
        self._null_incomplete_marked: set[str] = set()   # gids logged as incomplete in the current episode
        self._null_recovery_stats = {
            "attempts": 0, "recovered": 0, "incomplete_marked": 0,
            "backoff_skips": 0, "budget_skips": 0,
        }
        self._ws_lifecycle_suppressed = 0
        self._instances: dict[str, str] = {}  # base game_id -> current instance id
        # ── Phase 3 P2 — deviation dirty gating ──────────────────────
        # A game is "dirty" when an accepted clean observation (or a
        # finalization) has changed the deviation inputs since the last
        # time deviation.refresh_game ran for that game.  The gate:
        #   1. Mark dirty in _record_clean_impl / _finalize_clean.
        #   2. Skip deviation refresh inside _refresh_projections when NOT dirty.
        #   3. Flush all dirty games ONCE per tick at the end of _tick_body.
        #   4. Clear dirty flag only after a successful flush.
        # This collapses N per-snapshot deviation refreshes per game per
        # tick into at most one refresh per game per tick (or per
        # finalization), matching the Phase 3 P2 acceptance criteria
        # (deviation.refresh_game call count must fall materially from the
        # 22,421-call / 175,783 ms Phase 2 baseline).
        self._deviation_dirty: set[str] = set()
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
        # Slow-page self-heal guard (2026-09-13 incident): the worker must
        # never spin with ``_slow_page is None`` while resolve work is
        # pending.  _slow_page_ensure_failed_until throttles page-recreation
        # attempts after a failure (backoff, not a 2s hot loop);
        # _slow_page_warn is the last throttled zombie diagnostic.
        self._slow_page_ensure_failed_until = 0.0   # monotonic backoff gate
        self._slow_page_warn = {"ts": 0.0, "n": 0}  # last warn ts + suppressed
        self._slow_round_started_at: float = 0.0   # monotonic, worker writes
        self._last_slow_request_at: float = 0.0    # monotonic, fast writes
        self._slow_worker_started = False
        self._tick_no = 0                     # fast-loop tick counter
        self._empty_ticks = 0                 # consecutive empty-parses
        self._event_view_failures = 0         # consecutive unverified event views
        self._ws_market_last: dict[tuple[str, Optional[float]], str] = {}
        self._ws_snap_last: dict[str, str] = {}  # gid -> last bridge snapshot ts
        # quarter-collection state (directive 2026-09-22)
        self._ws_raw_last: dict[str, str] = {}  # gid -> last raw-frame ts
        self._q_parse_failures = 0
        self._q_anomalies = 0
        self._q_market_obs = 0
        self._q_score_obs = 0
        # Betual-only dataset (directive 2026-09-22, DATA COLLECTION
        # ONLY): internal-timer dataset recorder.  Hooks into the SAME
        # polling paths (no second polling loop, §16); failure-isolated;
        # feeds no decision path (§19).
        self.betual = BetualDataset(self.store)
        # Phase 5a: initialize the incremental betual_line distinct-game counter.
        # One-time setup — computes initial COUNT DISTINCT then switches to
        # incremental trigger-based updates for all future inserts.
        try:
            self.store._ensure_betual_line_counter()
        except Exception:
            logger.error(
                "Phase 5a betual_line counter init failed (non-fatal):\\n%s",
                traceback.format_exc())
        # game start evidence from the swarm feed (base id -> start_ts
        # epoch seconds + when first observed) — §2's authoritative start
        # source; None until the feed exposes it.
        self._betual_start_ts: dict[str, tuple[float, float]] = {}
        # last observed source quarter per game — the transition detector's
        # previous-quarter cache (§11)
        self._betual_last_quarter: dict[str, Optional[int]] = {}
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
            "final_capture_retries": 0,
        }
        # NO FINAL defect (2026-09-22): the last look at a game can be a
        # degenerate SPA row (scores/period NULL) — the list page renders
        # blanks before the row disappears — so the scorecard grades
        # UNKNOWN with NULL finals forever and the dashboard renders NO
        # FINAL.  A game at grace expiry whose tail cannot prove a final
        # gets ONE bounded final-capture window: it stays tracked and is
        # prioritized in the slow event-view rotation (the verified DOM
        # path returns real scores when the list page renders blank).
        # BLM_FINAL_CAPTURE_GRACE_S=0 restores the old behavior exactly.
        try:
            self._final_capture_grace_s = abs(float(
                os.environ.get("BLM_FINAL_CAPTURE_GRACE_S", "120") or 0))
        except (TypeError, ValueError):
            self._final_capture_grace_s = 120.0
        self._final_capture_gids: set[str] = set()    # window currently open
        self._final_capture_armed: set[str] = set()   # armed exactly once
        self._final_capture_until: dict[str, float] = {}

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

        Quarter-specific collection (directive 2026-09-22, DATA COLLECTION
        ONLY): every raw frame is retained (throttled per game; parse
        failures ALWAYS), and every O/U market the feed pushes is
        normalized — game totals to market_observations exactly as before,
        everything else (quarter totals etc.) to quarter_market_observations
        with the raw market entry preserved.  Nothing here feeds alerts,
        fingerprints, betting or thresholds.
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
                        # §5: the raw frame survives even when parsing
                        # finds nothing — a format change must never be
                        # silently dropped.
                        self._retain_raw_frame(ws.url, text, "parsed", 0)
                        return
                    # §1 gate snapshot: which of the frame's games are
                    # tracked BETUAL games (the dataset is Betual-only).
                    payload_game_index: dict[str, PokerBetGame] = {}
                    for p in payloads:
                        base_gid = self._base_id(str(p.get("game_id") or ""))
                        g = (self._find_tracked(str(p.get("game_id")))
                             or self._find_tracked(base_gid)
                             or self._find_tracked(
                                 self._instances.get(base_gid, "")))
                        if g is not None:
                            payload_game_index[str(p["game_id"])] = g
                    captured = utcnow_iso()
                    for p in payloads:
                        # §5: retain the verbatim frame (throttled per
                        # game) so a future format change can be
                        # forensically re-parsed from history.
                        self._retain_raw_frame(
                            ws.url, text, "parsed", len(payloads),
                            gid=p["game_id"])
                        # §2 start evidence: the game object's start_ts is
                        # the authoritative game start — adopted once per
                        # game, persisted for restart recovery.
                        frame_game = payload_game_index.get(p["game_id"])
                        if frame_game is not None:
                            self._betual_maybe_start_ts(frame_game,
                                                        p.get("start_ts"))
                    for obs in normalize_observations(payloads, captured):
                        if obs["market_type"] in GAME_TOTAL_MARKET_TYPES:
                            if obs["market_type"] != "MatchTotal":
                                continue
                            self._ingest_ws_observation(obs)
                            # §6 full-game line: also feed the Betual
                            # dataset (line-movement fields included).
                            frame_game = payload_game_index.get(
                                obs["source_game_id"])
                            if frame_game is not None:
                                try:
                                    self._betual_record_line(
                                        frame_game, obs, "full_game")
                                except BetualDatasetError:
                                    pass    # dataset is BETUAL-only (§1)
                                except Exception:
                                    logger.error(
                                        "betual full-game line failed:\n%s",
                                        traceback.format_exc())
                        else:
                            self._ingest_quarter_market_observation(obs)
                except Exception:
                    # a malformed frame must never kill the collector
                    self._q_parse_failures += 1
                    try:
                        self._retain_raw_frame(
                            ws.url, text, "parse_failed", None,
                            error=traceback.format_exc())
                    except Exception:
                        pass
                    logger.debug("ws market frame error: %s",
                                 traceback.format_exc())
            ws.on("framereceived", on_frame)
        page.on("websocket", on_ws)

    def _retain_raw_frame(
        self, ws_url: str, text: str, parse_status: str,
        game_count: Optional[int], error: Optional[str] = None,
        *, gid: Optional[str] = None,
    ) -> None:
        """Persist one raw WS frame (throttled per game; failures never).
        """
        if parse_status != "parse_failed" and WS_RAW_FRAME_KEEP_S > 0:
            key = gid or ws_url
            last = self._ws_raw_last.get(key)
            if last and _ts_age_s(last) < WS_RAW_FRAME_KEEP_S:
                return
            self._ws_raw_last[key] = utcnow_iso()
        self.store.insert_ws_raw_frame(
            captured_at=utcnow_iso(), ws_url=ws_url, byte_size=len(text),
            parse_status=parse_status, game_count=game_count,
            raw_json=text, error=error)

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
        # ── PHASE 3 (capture-efficiency directive 2026-09-30) ────────
        # Explicit game lifecycle: LIVE -> FINALIZING -> DONE.
        #   FINALIZING: the final-capture window (_final_capture_gids) —
        #     the game's terminal capture is still in progress; WS frames
        #     for it are STILL accepted.
        #   DONE: the game object is 'ended' OR the gid has left tracking
        #     via the disappeared path — WS capture stops.  A DONE game
        #     must not consume collector budget: this was the dominant
        #     post-final capture waste (game 31075046: 294 snapshots in
        #     the ~31 min AFTER result_at, 2026-09-30 audit).
        if game.status == "ended" or (
                game.source_game_id in self._end_state_terminal):
            self._ws_lifecycle_suppressed += 1
            logger.debug(
                "ws bridge suppressed (game DONE): %s", game.source_game_id)
            return
        if game.source_game_id in self._final_capture_gids:
            pass  # FINALIZING: final capture in progress — frames accepted
        # current-instance identity: never write a base-keyed frame back
        # onto the completed base game
        obs["source_game_id"] = game.source_game_id
        obs["game_id"] = self._game_db_id(game)
        try:
            with _alarm_blocked():
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
                with _alarm_blocked():
                    if self.store.insert_snapshot(
                            self._game_db_id(game), snap):
                        self._ws_snap_last[gid] = obs["captured_at"]
                        self.stats["snapshots"] += 1
                        self._record_clean(game, snap)
        except Exception:
            logger.error("ws snapshot bridge failed:\n%s",
                         traceback.format_exc())

    # ── LIVE market subscriptions (30s freshness) ────────────────

    def _ingest_quarter_market_observation(self, obs: dict) -> None:
        """Persist one NON-game-total O/U market observation (e.g. a
        quarter total) pushed by the feed.

        Directive 2026-09-22 §1/§2: the source's period taxonomy is NOT
        assumed — the market NAME is inspected and unambiguous quarter
        wording maps to Q1..Q4; anything else is stored with
        market_period='other' or 'quarter_unknown' exactly as observed.
        The raw market entry (events, ids, prices) is preserved verbatim.
        This NEVER feeds alerts, fingerprints, betting or thresholds.
        """
        gid = obs["source_game_id"]
        raw = obs.get("raw") or {}
        market_id = str(raw.get("market_id") or "")
        if not market_id:
            return
        if not obs.get("market_type"):
            # Defense-in-depth for the quarter-market NOT NULL violation
            # (2026-09-29): normalize_observations now drops unclassifiable
            # rows at the source, but historical frames (or any future
            # producer) must never reach the store with a NULL market_type.
            logger.warning(
                "quarter market dropped: NULL market_type (market_id=%s)",
                market_id)
            return
        game = self._find_tracked(gid)
        if game is None:
            cur = self._instances.get(gid)      # base -> current instance
            if cur:
                game = self._find_tracked(cur)
        if game is None:
            return
        period, qnum = infer_market_period(obs.get("market_name"))
        # Betual-only dataset (§1/§6/§7): quarter-total lines feed the
        # prospective dataset with line-movement fields; failure-isolated
        # and decision-free (§19).
        try:
            self._betual_record_line(game, obs, period or "other")
        except BetualDatasetError:
            pass                      # non-Betual: dataset is BETUAL-only
        except Exception:
            logger.error("betual line record failed:\n%s",
                         traceback.format_exc())
        try:
            with _alarm_blocked():
                self.store.insert_quarter_market_observation({
                    "game_id": self._game_db_id(game),
                "source_game_id": game.source_game_id,
                "classification": game.classification,
                "captured_at": obs["captured_at"],
                "market_id": market_id,
                "market_type": obs["market_type"],
                "market_name": obs.get("market_name"),
                "market_period": period,
                "period_number": qnum,
                "line_value": obs.get("line_value"),
                "over_price": obs.get("over_price"),
                "under_price": obs.get("under_price"),
                "home_score": obs.get("home_score"),
                "away_score": obs.get("away_score"),
                "period_label": obs.get("period_label"),
                "clock": obs.get("clock"),
                "raw": raw,
            })
            self._q_market_obs += 1
        except Exception:
            logger.error("quarter market observation persist failed:\n%s",
                         traceback.format_exc())

    def _record_quarter_scores(self, game: PokerBetGame,
                               parsed: dict) -> None:
        """Record one quarter-score observation from a VERIFIED event-view
        parse and run chronological validation (§4).

        The event-view scoreboard's per-quarter breakdown is the only
        quarter-score source in production (WS frames carry no quarter
        scores — audited 2026-09-22).  Values are stored EXACTLY as the
        source presented them; anomalies are flagged, never corrected.
        All-NULL rows are still recorded: they are the coverage
        denominator and must never be hidden (§9).
        """
        qs = parsed.get("quarter_scores") or []
        qso = {
            "game_id": self._game_db_id(game),
            "source_game_id": game.source_game_id,
            "classification": game.classification,
            "captured_at": utcnow_iso(),
            "period_label": parsed.get("period_label"),
            "quarter": parsed.get("quarter"),
            "clock": parsed.get("clock"),
            "home_score": parsed.get("home_score"),
            "away_score": parsed.get("away_score"),
            "source_path": "event_view",
            "raw": {"quarter_scores": [list(p) for p in qs]},
        }
        for i, (h, a) in enumerate(qs[:4], start=1):
            qso[f"q{i}_home_score"] = h
            qso[f"q{i}_away_score"] = a
        try:
            with _alarm_blocked():
                prev = self.store.last_quarter_score_observation(
                    game.source_game_id)
                self.store.insert_quarter_score_observation(qso)
                self._q_score_obs += 1
                for an in validate_quarter_scores(qso, prev):
                    self._q_anomalies += 1
                    self.store.record_quarter_anomaly(
                        source_game_id=game.source_game_id,
                        captured_at=qso["captured_at"],
                        check_name=an["check"],
                        detail=an["detail"],
                        anomaly=an,
                    )
                    logger.warning(
                        "quarter anomaly %s for %s: %s",
                        an["check"], game.source_game_id, an["detail"])
        except Exception:
            logger.error("quarter score observation persist failed:\n%s",
                         traceback.format_exc())

    # ── Betual-only dataset (directive 2026-09-22, DATA COLLECTION
    # ── ONLY).  All methods failure-isolated; nothing here feeds
    # ── alerts, fingerprints, betting or thresholds (§19).

    def _betual_persist_timer(self, game: PokerBetGame) -> None:
        """Persist the §13 anchor + latest caches for one game (called
        after each successful dataset write; failure-isolated)."""
        rec = self.betual.game(game.source_game_id, game.classification)
        if rec is None:
            return
        try:
            with _alarm_blocked():
                home, away = (rec.prev_score if rec.prev_score else (None, None))
                self.store.upsert_betual_timer(
                    source_game_id=game.source_game_id,
                    game_start_wall=rec.anchors.game_start_wall,
                    observed_at_wall=rec.anchors.observed_at_wall,
                    quarter_seconds=rec.anchors.quarter_seconds,
                    break_seconds=rec.anchors.break_seconds,
                    timer_model=rec.anchors.model,
                    last_home=home, last_away=away,
                    last_line=rec.prev_line, last_line_at=rec.prev_line_at,
                    last_capture_at=rec.last_capture_at)
        except Exception:
            logger.error("betual timer persist failed:\n%s",
                         traceback.format_exc())

    def _betual_record_line(self, game: PokerBetGame, obs: dict,
                            period: str) -> None:
        """Route one non-game-total O/U observation into the Betual
        dataset (raises BetualDatasetError for non-Betual games)."""
        rec = self.betual.game(game.source_game_id, game.classification)
        if rec is None:
            raise BetualDatasetError("non-Betual game refused")
        raw = obs.get("raw") or {}
        safe_record(self.store, self.betual.record_line_observation,
                    rec,
                    source_game_id=game.source_game_id,
                    classification=game.classification,
                    captured_at=obs["captured_at"],
                    market_id=str(raw.get("market_id") or ""),
                    market_name=obs.get("market_name"),
                    period=period,
                    line=obs.get("line_value"),
                    home=obs.get("home_score"), away=obs.get("away_score"),
                    quarter=obs.get("quarter"),
                    displayed_clock=obs.get("clock"),
                    over_price=obs.get("over_price"),
                    under_price=obs.get("under_price"),
                    raw=raw)
        self._betual_persist_timer(game)

    def _betual_maybe_start_ts(self, game: PokerBetGame,
                               start_ts: Optional[float]) -> None:
        """Adopt the swarm feed's start_ts as the authoritative game
        start (§2) — once, first-evidence-wins, persisted for restart
        recovery (§13)."""
        if not start_ts:
            return
        prev = self._betual_start_ts.get(game.source_game_id)
        now_wall = time.time()
        if prev is None:
            self._betual_start_ts[game.source_game_id] = (
                float(start_ts), now_wall)
            prev = (float(start_ts), now_wall)
        rec = self.betual.game(game.source_game_id, game.classification)
        if rec is None:
            return
        start_wall, seen_wall = prev
        elapsed_at_join = max(0.0, seen_wall - start_wall)
        rec.adopt_start_evidence(start_wall, seen_wall,
                                 elapsed_at_adoption=elapsed_at_join)
        self._betual_persist_timer(game)

    def _betual_record_score(self, game: PokerBetGame, parsed: dict,
                             captured_at: str) -> None:
        """Record one Betual score observation from a VERIFIED event-view
        parse (internal-timer fields; §5 point-in-time derivation)."""
        rec = self.betual.game(game.source_game_id, game.classification)
        if rec is None:
            return
        qs = parsed.get("quarter_scores") or []
        # cumulative pairs at each quarter end, exactly as observed
        cum: list[tuple[Any, Any]] = []
        h = a = 0
        for qh, qa in qs[:4]:
            if qh is None or qa is None:
                break
            h += int(qh)
            a += int(qa)
            cum.append((h, a))
        safe_record(self.store, self.betual.record_score_observation,
                    rec,
                    source_game_id=game.source_game_id,
                    classification=game.classification,
                    captured_at=captured_at,
                    home=parsed.get("home_score"),
                    away=parsed.get("away_score"),
                    quarter=parsed.get("quarter"),
                    displayed_clock=parsed.get("clock"),
                    period_label=parsed.get("period_label"),
                    q_scores=cum,
                    raw={"quarter_scores": [list(p) for p in qs]})
        prev_q = self._betual_last_quarter.get(game.source_game_id)
        new_q = parsed.get("quarter")
        self._betual_maybe_transition(
            game, rec, captured_at, prev_q, new_q,
            parsed.get("home_score"), parsed.get("away_score"),
            parsed, cum)
        self._betual_last_quarter[game.source_game_id] = (
            new_q if new_q in (1, 2, 3, 4) else prev_q)
        self._betual_persist_timer(game)

    def _betual_maybe_transition(
        self, game: PokerBetGame, rec, captured_at: str,
        prev_q: Optional[int], new_q: Optional[int],
        home: Optional[int], away: Optional[int],
        parsed: dict, cum: list,
    ) -> None:
        """§11: capture the high-quality transition snapshot when the
        verified event view shows the quarter advanced one step."""
        if prev_q not in (1, 2, 3) or new_q != prev_q + 1:
            return
        total = parsed.get("total", {}) or {}
        full_line = total.get("first_line")
        safe_record(self.store, self.betual.maybe_transition,
                    rec,
                    source_game_id=game.source_game_id,
                    classification=game.classification,
                    captured_at=captured_at,
                    prev_quarter=prev_q, new_quarter=new_q,
                    home=home, away=away,
                    full_game_line=full_line,
                    quarter_line=None,       # quarter lines: WS path only
                    line_previous=rec.prev_line,
                    displayed_clock=parsed.get("clock"),
                    q_scores=cum,
                    raw={"period_label": parsed.get("period_label")})

    def _betual_end_game(self, game: PokerBetGame,
                         parsed: Optional[dict] = None) -> None:
        """§12: record the game-end row.  Evidence distinguishes a
        source-proven final (verified event view) from a disappearance
        (NO-FINAL diagnosis data) — internal-timer expiry is never final."""
        rec = self.betual.game(game.source_game_id, game.classification)
        if rec is None:
            return
        if parsed is not None:
            qs = parsed.get("quarter_scores") or []
            cum: list[tuple[Any, Any]] = []
            h = a = 0
            for qh, qa in qs[:4]:
                if qh is None or qa is None:
                    break
                h += int(qh)
                a += int(qa)
                cum.append((h, a))
            evidence, settle = "observed_final", parsed.get("period_label")
            home, away = parsed.get("home_score"), parsed.get("away_score")
            clock = parsed.get("clock")
            quarter = parsed.get("quarter")
        else:
            cum, evidence, settle = [], "disappeared", None
            home = away = clock = quarter = None
            # §12: a disappeared game's last known scores — read-only
            last = self.store.get_snapshots(game.source_game_id, limit=1)
            if last:
                home, away = last[0].get("home_score"), last[0].get(
                    "away_score")
        safe_record(self.store, self.betual.record_game_end,
                    rec,
                    source_game_id=game.source_game_id,
                    classification=game.classification,
                    captured_at=utcnow_iso(),
                    home=home, away=away, quarter=quarter,
                    displayed_clock=clock,
                    full_game_line=None,
                    settlement_state=settle,
                    end_evidence=evidence,
                    q_scores=cum,
                    raw={"source_url": game.source_url})

    def _betual_restore_state(self) -> None:
        """§13 restart recovery: rebuild Betual timer state from the
        persisted anchors so the internal clock is NOT reset to zero."""
        try:
            rows = self.store.list_betual_timers()
        except Exception:
            logger.error("betual restore list failed:\n%s",
                         traceback.format_exc())
            return
        for r in rows:
            try:
                gid = r["source_game_id"]
                game = self._find_tracked(gid)
                cls = game.classification if game else "BETUAL_NBA"
                rec = self.betual.restore(
                    gid, cls,
                    game_start_wall=r.get("game_start_wall"),
                    observed_at_wall=r.get("observed_at_wall"),
                    quarter_seconds=r.get("quarter_seconds"),
                    timer_model=r.get("timer_model"),
                    last_home=r.get("last_home"),
                    last_away=r.get("last_away"),
                    last_line=r.get("last_line"),
                    last_line_at=r.get("last_line_at"))
                if rec is not None and r.get("game_start_wall"):
                    self._betual_start_ts[gid] = (
                        r["game_start_wall"],
                        r.get("observed_at_wall") or time.time())
            except Exception:
                logger.error("betual restore row failed:\n%s",
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
        # NOTE: this also clears the WORKER's page handle if the worker's
        # browser object was already gone; the worker's own self-heal guard
        # (_ensure_worker_page, top of its loop) recreates the slow page —
        # there is no main-thread mechanism to do it for the worker.
        self._slow_page = None            # worker's guard recreates it
        self._empty_ticks = 0
        try:
            return self._new_session()
        except Exception:
            logger.error("relaunch failed:\n%s", traceback.format_exc())
            raise

    def _write_state(self, *, success: bool) -> None:
        with PERFORMANCE.measure("collector.write_state"):
            return self._write_state_impl(success=success)

    def _write_state_impl(self, *, success: bool) -> None:
        """Heartbeat file the dashboard API reads for collector status."""
        now_iso = utcnow_iso()
        if success:
            self._last_success_iso = now_iso
        with PERFORMANCE.measure("collector.state_metrics"):
            betual_metrics = self.store.betual_collection_metrics()
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
                # Health invariant (2026-09-13 zombie incident): alive +
                # pending work + no page == degraded worker.  Observable
                # in the state file so a pageless zombie can be detected
                # without reading the journal.
                "page_available": self._slow_page is not None,
                "page_recreate_backoff_s": max(
                    0.0, round(self._slow_page_ensure_failed_until
                               - time.monotonic(), 1)),
                "degraded": bool(
                    self._slow_thread is not None
                    and self._slow_thread.is_alive()
                    and self._slow_page is None
                    and len(self._pending_resolve) > 0),
            },
            "tick_timing": _tick_timing_summary(self._tick_stats),
            "performance": PERFORMANCE.snapshot(),
            # LIVE freshness: collection cadence vs observation age are
            # reported separately (see _freshness_summary).
            "market_freshness": self._freshness_summary(),
            # Capture-efficiency counters (directive 2026-09-30):
            # PHASE 3 lifecycle suppression + PHASE 4 bounded NULL-score
            # recovery — pure counters, no decision logic reads them.
            "null_score_recovery": dict(self._null_recovery_stats),
            "ws_lifecycle_suppressed": self._ws_lifecycle_suppressed,
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
            # Quarter-specific collection (directive 2026-09-22 §9):
            # in-process counters — failures and anomalies are REPORTED,
            # never hidden.  Persistent coverage % lives in
            # store.quarter_collection_metrics().
            "quarter_collection": {
                "score_observations": self._q_score_obs,
                "market_observations": self._q_market_obs,
                "ws_parse_failures": self._q_parse_failures,
                "anomalies": self._q_anomalies,
            },
            # Betual-only dataset (§18): per-game coverage, line-movement
            # volume, transitions, finalization success, NO-FINAL count,
            # parse failures and clock diagnostics — read-only aggregates.
            "betual_dataset": betual_metrics,
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
            # Write to a temp file first, then rename — atomic on the same
            # filesystem.  But more importantly: if the write takes >5s
            # (1.7MB JSON, DB lock contention), skip it so the fast loop
            # is never blocked by state persistence.
            json_bytes = json.dumps(state, indent=2).encode("utf-8")
            tmp_path = STATE_FILE.with_suffix(".tmp")
            if len(json_bytes) > 10_000_000:
                logger.warning("state too large (%d bytes) — skipping write",
                               len(json_bytes))
                return
            t0 = time.monotonic()
            tmp_path.write_bytes(json_bytes)
            elapsed = time.monotonic() - t0
            if elapsed > 5.0:
                logger.warning(
                    "state write took %.1fs — truncating to tracked_games only",
                    elapsed)
                # Rebuild a minimal state with just the recovery-critical
                # fields and retry the write.
                minimal = {
                    "tick": state["tick"],
                    "status": state["status"],
                    "started_at": state["started_at"],
                    "last_tick_at": now_iso,
                    "last_success_at": state["last_success_at"],
                    "last_error_at": state["last_error_at"],
                    "tracked_games": state["tracked_games"],
                    "last_market_at": state["last_market_at"],
                    "last_market_attempt_at": state["last_market_attempt_at"],
                    "market_attempt_streaks": state["market_attempt_streaks"],
                }
                tmp_path.write_text(json.dumps(minimal, indent=2))
            tmp_path.rename(STATE_FILE)
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
            g.home_team, g.away_team = PokerBetCollector._canonical_teams(
                g.home_team, g.away_team, cls_val)
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

    def _deviation_backfill(self) -> None:
        """Phase 5c: one-time background backfill of historical clean
        observations into the deviation benchmark layer.

        Called once at collector start in a daemon thread so the fast loop
        starts immediately.  Idempotent and failure-isolated — a failure
        here must never prevent the collector from running.
        """
        _block_deadline_signal()
        try:
            if self._deviation is not None:
                stats = self._deviation.refresh_all()
                logger.info(
                    "deviation backfill complete: %d games, %d added, %d existing",
                    stats.get("games", 0),
                    stats.get("added", 0),
                    stats.get("existing", 0),
                )
        except Exception:
            logger.error(
                "deviation backfill failed (collector continues):\n%s",
                traceback.format_exc(),
            )

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._last_fast_completed_at = 0.0  # monotonic; 0 = never
        # Watchdog completeness gate (2026-09-29 correctness audit): the
        # loop-level completion marker used to be set on EVERY iteration —
        # including ticks that raised (db-lock skip, relaunch) or took an
        # early return inside _tick_body (fresh-context recycle, empty
        # parse).  A wedge that reliably produced exceptions therefore
        # fed the dog while producing zero observations.  Set ONLY when
        # _tick_body ran to its end (see the flag set next to the real
        # completion block inside _tick_body); the loop-level marker is
        # now gated on it.
        self._tick_completed_fully = False
        # External-throttle exception: a db-lock skip defers persistence
        # but the loop is provably alive (browser, parsing, WS all fine);
        # the code deliberately does NOT relaunch on cross-process SQLite
        # contention (see the db-lock handler below), so it must not
        # starve the dog either — a restart would kill the WS market
        # subscription and re-enter the same lock.  Feeding on THIS path
        # is documented policy, not the completeness hole fix 2 closes.
        self._tick_liveness_only = False
        self._watchdog_enabled = (
            SD_NOTIFY_AVAILABLE
            and bool(os.environ.get("WATCHDOG_USEC"))
            and bool(os.environ.get("NOTIFY_SOCKET"))
            and self.tick_s >= WATCHDOG_POLL_DEFAULT_S
        )
        if self._watchdog_enabled:
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop, name="blm-watchdog", daemon=True)
            self._watchdog_thread.start()
        elif os.environ.get("WATCHDOG_USEC"):
            logger.warning(
                "WATCHDOG_USEC set but self-watchdog disabled "
                "(NOTIFY_SOCKET=%s tick=%s)",
                bool(os.environ.get("NOTIFY_SOCKET")), self.tick_s)
        # NOTE (Type=notify semantics, 2026-09-26 review): READY=1 is
        # deliberately NOT sent here.  It is sent once initialization has
        # SUCCEEDED — slow worker, browser session, tracked-state restore
        # — immediately before the fast loop (see below).  A process that
        # hangs during startup never sends READY and is killed at
        # TimeoutStartSec; watchdog pings are the separate WATCHDOG=1
        # channel in _watchdog_loop and never substitute for READY.
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
        # §13 Betual restart recovery: rebuild the internal-timer state
        # from the persisted wall anchors — the game clock is NEVER reset
        # to zero by a restart.
        self._betual_restore_state()
        # one-time backfill: pre-existing clean observations enter the
        # deviation benchmark.  Phase 5c: run this in a background thread so
        # the fast loop starts immediately — the per-game dirty gating
        # (Phase 3 P2) handles live updates; the backfill merely catches
        # historical rows that were inserted before this layer existed.
        # Idempotent and failure-isolated.
        if self._deviation is not None:
            self._deviation_backfill_thread = threading.Thread(
                target=self._deviation_backfill, name="blm-deviation-backfill",
                daemon=True)
            self._deviation_backfill_thread.start()
        # Slow worker first: its failures are isolated from the fast loop
        self._start_slow_worker()
        try:
            with sync_playwright() as pw:
                self._pw = pw
                page = self._new_session()
                # READY=1: initialization SUCCEEDED (slow worker up,
                # browser session up, tracked state restored).  From this
                # point liveness is reported exclusively via WATCHDOG=1
                # pings from _watchdog_loop, gated on fresh fast cycles
                # (with the one bounded startup-grace exception documented
                # in _watchdog_loop — it ends at the first completed
                # cycle).
                _sd_notify("READY=1")
                while self._running:
                    tick_start = time.monotonic()
                    self._last_fast_started_at = tick_start
                    self._fast_cycle_started_at_iso = utcnow_iso()
                    # schedule the NEXT target on the interval grid
                    if self._next_tick_target <= 0.0:
                        self._next_tick_target = tick_start + self.tick_s
                    prev_started = self._prev_fast_started_at
                    # Reset BEFORE the tick runs: an exception raised
                    # between here and _tick_body's own entry-reset must
                    # not inherit the previous pass's completed flag.
                    self._tick_completed_fully = False
                    self._tick_liveness_only = False
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
                        # liveness-only: keep the dog fed (see the gate
                        # comment in start()); nothing else advances
                        self._tick_liveness_only = True
                        logger.warning("tick db lock — skipping tick: %s", exc)
                    except TargetClosedError as exc:
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
                    if self._tick_completed_fully or self._tick_liveness_only:
                        self._fast_cycle_completed_at_iso = utcnow_iso()
                        self._last_fast_completed_at = time.monotonic()
                    else:
                        logger.info(
                            "tick %s did NOT reach completion — watchdog "
                            "liveness marker not advanced",
                            getattr(self, "stats", {}).get("ticks", "?"))
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

    # ── SELF-WATCHDOG (see WATCHDOG_* note above FAST_TICK_S) ──────

    def _watchdog_loop_body(self, deadline: float, starving: bool) -> None:
        """Single poll iteration of the self-watchdog.

        A fast cycle counts as fresh when it COMPLETED (not merely
        started) within FAST_LIVENESS_FACTOR × tick_s.  Wedged (blocked
        in a Playwright/SPA call), crashed-without-exit, or starved
        states all stop completion; the dog starves; systemd restarts
        the unit (Restart=always) — the whole unit, browser included,
        which an in-process relaunch cannot guarantee.

        Protocol: the ping is ``WATCHDOG=1`` (that is what resets
        systemd's WatchdogSec timer — STATUS= alone does not), with a
        human-readable STATUS line attached.

        The watchdog thread must NEVER die from a notification failure or
        any other unexpected error — a dead watchdog thread is invisible
        to systemd and causes the same SIGABRT restart loop as a wedged
        fast path.  Every exception in the loop body is swallowed.

        Pinging is strictly conditional on a COMPLETED fast cycle, with
        ONE bounded exception: the startup grace window (see the
        WATCHDOG_* note above FAST_TICK_S).  With Type=notify, systemd
        arms WatchdogSec only after READY=1, and the first fast cycle of
        a fresh process is its most fragile — the 2026-09-29 restart
        storms each killed a process inside its first ~60s under host
        load.  The grace window re-covers exactly that minute; it is
        BOUNDED (min(WATCHDOG_STARTUP_GRACE_S, WatchdogSec / 2)) and
        ends at the first completed cycle, so steady-state liveness
        remains gated on completed fast cycles and a wedged fast path is
        still killed at WatchdogSec expiry.
        """
        last = self._last_fast_completed_at
        now = time.monotonic()
        alive = (
            last > 0.0
            and (now - last) <= deadline
        )
        in_grace = (
            last <= 0.0
            and getattr(self, "_startup_deadline", 0.0) > 0.0
            and now < self._startup_deadline
        )
        if alive:
            if starving:
                logger.info("watchdog: fast cycles fresh again — "
                            "resuming WATCHDOG=1 pings")
            self._starving_now = False
            # WATCHDOG=1 is THE heartbeat — it is what resets
            # systemd's WatchdogSec timer.  STATUS= is annotation
            # only and must never be treated as the heartbeat.
            _sd_notify(
                f"WATCHDOG=1\n"
                f"STATUS=fast cycle complete "
                f"{now - last:.1f}s ago")
        elif in_grace:
            self._starving_now = False
            # Bounded startup grace: a fresh process has not completed
            # its first fast cycle yet — usually browser warmup under
            # load, not a wedge.  Ping so systemd's newly armed
            # WatchdogSec is not allowed to kill the unit for being slow
            # in the one minute every 2026-09-29 restart storm exploited.
            # The window expires (see _watchdog_loop) and ends outright
            # at the first completed cycle.
            _sd_notify(
                "WATCHDOG=1\n"
                "STATUS=startup grace — no completed fast cycle yet")
        else:
            self._starving_now = True
            if not starving:
                logger.warning(
                    "watchdog: no completed fast cycle within %.0fs — "
                    "STOPPING WATCHDOG=1; systemd restarts the unit "
                    "at WatchdogSec expiry", deadline)

    def _watchdog_loop(self) -> None:
        """Pet systemd's watchdog ONLY while the fast path is provably alive.

        A fast cycle counts as fresh when it COMPLETED (not merely
        started) within FAST_LIVENESS_FACTOR × tick_s.  Wedged (blocked
        in a Playwright/SPA call), crashed-without-exit, or starved
        states all stop completion; the dog starves; systemd restarts
        the unit (Restart=always) — the whole unit, browser included,
        which an in-process relaunch cannot guarantee.

        ONE bounded exception: until the FIRST fast cycle completes but
        no longer than _startup_grace_seconds (min(60s, WatchdogSec/2),
        see the WATCHDOG_* note above FAST_TICK_S), the dog is still
        fed — a slow first cycle under host load must not be rewarded
        with a systemd kill.  The window ends at the first completed
        cycle, after which liveness is gated on completed cycles
        exclusively.

        The watchdog thread must NEVER die from a notification failure or
        any other unexpected error — a dead watchdog thread is invisible
        to systemd and causes the same SIGABRT restart loop as a wedged
        fast path.  Every exception in the loop body is swallowed.
        """
        _block_deadline_signal()
        deadline = FAST_TICK_S * FAST_LIVENESS_FACTOR  # 30 s threshold
        # Bounded startup grace (2026-09-29 forensic follow-up): ping
        # while the process has NEVER completed a fast cycle and start-up
        # is younger than min(WATCHDOG_STARTUP_GRACE_S, WatchdogSec / 2).
        # After that — or after the first completed cycle — liveness is
        # gated on completed fast cycles exclusively.
        self._startup_deadline = time.monotonic() + _startup_grace_seconds(
            os.environ.get("WATCHDOG_USEC"))
        self._starving_now = False
        logger.info(
            "self-watchdog active: fast-liveness deadline %.0fs, "
            "startup grace until +%.0fs",
            deadline, self._startup_deadline - time.monotonic())
        starving = False
        while not self._watchdog_stop.wait(WATCHDOG_POLL_DEFAULT_S):
            try:
                self._watchdog_loop_body(deadline, starving)
                # one STOPPING warning per starvation episode, not one
                # per poll pass (the pre-fix flag was never propagated)
                starving = self._starving_now
            except Exception:
                # The watchdog thread must never die.  Any unexpected error
                # (including _sd_notify failures that escape its internal
                # try/except) is silently swallowed so the thread survives.
                logger.exception("watchdog loop body error — continuing")

    def stop(self) -> None:
        self._running = False
        stop_evt = getattr(self, "_watchdog_stop", None)
        if stop_evt is not None:
            stop_evt.set()
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
        _block_deadline_signal()
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
                        # 0. SELF-HEAL GUARD (2026-09-13 incident): before
                        #    any resolution work, make sure a usable slow
                        #    event-view page exists.  The fast-path
                        #    _relaunch() clears _slow_page from the MAIN
                        #    thread ("recreate on next guard" — this IS
                        #    that guard); without it the worker stayed
                        #    alive-but-pageless for ~95 minutes while every
                        #    new game silently failed to resolve.
                        try:
                            self._ensure_worker_page()
                        except self.PageUnavailable:
                            # Recreation failed — bounded backoff so this
                            # never becomes a 2s launch hot loop.
                            self._slow_stop.wait(2.0)
                            continue
                        # 1. identity resolutions first (young games need
                        #    their durable id + first snapshots early)
                        done = 0
                        while done < RESOLVE_BATCH:
                            claim = self._claim_pending_resolve()
                            if claim is None:
                                break
                            woke_for = "resolve"
                            cls, row = claim
                            try:
                                ok = self._resolve_pending_on_worker(cls, row)
                            except self.PageUnavailable:
                                # Page died under us mid-batch: requeue,
                                # drop the dead handle and retry after the
                                # guard backoff — never spin.
                                self._requeue_resolve(cls, row)
                                self._warn_slow_page_unavailable(
                                    "slow page unavailable mid-resolve-batch")
                                self._slow_page = None
                                break
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
            # 2026-09-13 incident: this used to be a SILENT False — every
            # requeue incremented resolve_failures with zero journal noise
            # while the worker ran pageless for ~95 minutes.  Make it
            # visible (throttled) and let the worker loop self-heal.
            self._warn_slow_page_unavailable("resolve claim with no page")
            raise self.PageUnavailable("no slow page")
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
            home, away = self._authoritative_teams(row, tax, text, cls)
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
            logger.warning("goto failed — requesting fresh context")
            return
        self._expand_target_sections(page)

    # ── page.content() timeout wrapper ───────────────────────────────
    # Playwright's sync page.content() can hang indefinitely when the
    # browser renderer is wedged (e.g. a slow JS handler, an infinite
    # React re-render loop, or a dead CDP channel).  We run it in a
    # side thread with a hard timeout so the fast loop is never blocked
    # by a single stuck page operation.  Thread.join(timeout=X) is the
    # standard Python mechanism — we cannot kill the thread, but we CAN
    # stop waiting for it and recover the page.

    PAGE_CONTENT_TIMEOUT_S = 8.0  # generous: tick budget is 5s but this
                                   # is a safety net, not the normal path
    # ── PHASE 5 TIMEOUT BUDGET (2026-09-27) ──────────────────────────
    # Centralized timeout + tick budget policy.  Every Playwright page
    # operation in the fast path is bounded; the tick itself has a soft
    # target and a hard ceiling.  These constants are the single source
    # of truth — no magic numbers anywhere else in the collector.
    #
    #              task                  timeout    why
    #   page.content() (tick)    →     3s      single fast-path round-trip;
    #                                          the tick target is 5s, but the
    #                                          page operation must leave room for
    #                                          parse + persist + heartbeat
    #   page.content() (retry)   →     5s      second attempt after a failed
    #                                          parse; more generous than the
    #                                          first because the browser may be
    #                                          recovering from a thin parse
    #   recovery (fresh context) →     5s      the recovery path replaces the
    #                                          page; it must complete well inside
    #                                          the tick ceiling so the next cycle
    #                                          is not pushed
    #   retry page.content() (after recovery) → 3s — same as the first tick attempt
    #   page-capture budget      →    12s      total wall-clock spent inside
    #                                          page.content() calls per tick
    #                                          (tick + retry); exhausts → fresh
    #                                          context to prevent a wedged browser
    #                                          from consuming the whole tick
    #   tick target              →    10s      SOFT target: the collector aims
    #                                          to complete each fast cycle in
    #                                          this window (relaxed from the
    #                                          original 5s hard requirement as
    #                                          the Phase 5 optimization matures)
    #   tick hard ceiling        →    30s      HARD ceiling: a tick that has
    #                                          not completed within this window
    #                                          is forcibly terminated and the
    #                                          page is recycled; this prevents
    #                                          a single stuck operation from
    #                                          stalling the entire collection
    #                                          pipeline for minutes

    PAGE_CONTENT_TICK_TIMEOUT_S = 3.0
    PAGE_CONTENT_RETRY_TIMEOUT_S = 5.0
    RECOVERY_TIMEOUT_S = 5.0
    PAGE_CAPTURE_BUDGET_S = 12.0
    TICK_TARGET_S = 10.0
    TICK_HARD_CEILING_S = 30.0

    # ── Capture-efficiency knobs (directive 2026-09-30, PHASE 4) ──
    # NULL-score recovery is BOUNDED on every axis: at most this many
    # games re-read per tick, each at most once, only while the
    # page-capture budget lasts; a game that stays NULL after this many
    # consecutive retry episodes is marked incomplete and backed off.
    NULL_SCORE_RECOVER_MAX_PER_TICK = 2
    NULL_SCORE_INCOMPLETE_AFTER = 3
    NULL_SCORE_BACKOFF_TICKS = 30

    def _page_content_timed(
        self, page: Page, label: str = "", timeout_s: Optional[float] = None,
    ) -> str | None:
        """Return page.content() or None if it overran its budget.

        Runs page.content() on the SAME thread that owns the sync_playwright
        context (the main fast-path thread).  The thread-spawning version
        crossed Playwright's greenlet thread-affinity boundary and produced
        ``greenlet.error: cannot switch to a different thread`` on every
        call (reproducer-confirmed), so the deadline is armed as SIGALRM on
        the calling thread — no cross-thread greenlet switch is attempted.

        The handler FLAGS an overrun; it must never RAISE.  Playwright
        dispatches WebSocket frame callbacks NESTED on this same thread
        while page.content() is in flight (each thread owns a socket), and
        those callbacks write to SQLite.  A raising handler was landing
        inside ``upsert_betual_timer``'s ``conn.commit()`` (159 events to
        2026-09-29): it aborted the write mid-transaction AND consumed the
        alarm, so the deadline silently stopped being enforced (a 3.0s
        budget measured 5.9s) and a late raise could equally land in the
        tick's own persistence phase.  Flagging keeps the error out of
        every frame but the call site's own.

        An overrun is REPORTED, not punished: the capture is still returned.
        Discarding it would route every over-budget capture through
        ``_fresh_context`` (a context recycle plus a discovery navigation) —
        measured at roughly 3–5% of ticks, and exactly the path that hung for
        85s before the 03:04:06Z watchdog restart.  The spend-based budget at
        the call site and, for a page wedged outright, the self-watchdog →
        systemd restart remain the recovery mechanisms (see the WATCHDOG_*
        note above FAST_TICK_S; ``tick_deadline`` in ``_tick`` is currently
        computed but never used).  A failed capture still returns None.

        ``timeout_s`` overrides the instance default when provided — used
        by the tick budget to shrink the per-call deadline as the budget
        burns down.
        """
        url = getattr(page, "url", "unknown")
        tl = timeout_s if timeout_s is not None else self.PAGE_CONTENT_TICK_TIMEOUT_S
        t_start = time.monotonic()
        logger.info("page.content() START [%s] url=%s timeout=%.1fs", label, url, tl)

        self._capture_overran = False

        def _on_alarm(signum: int, frame: Any) -> None:
            self._capture_overran = True

        old_handler = signal.signal(signal.SIGALRM, _on_alarm)
        try:
            signal.setitimer(signal.ITIMER_REAL, tl)
            try:
                html = page.content()
            except Exception as e:
                logger.error(
                    "page.content() EXCEPTION [%s] url=%s: %s",
                    label, url, e,
                )
                return None
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)

        spend = time.monotonic() - t_start
        if self._capture_overran:
            logger.warning(
                "page.content() OVERRAN its %.1fs budget (took %.3fs) [%s] "
                "url=%s — capture kept; wedge recovery is the watchdog's",
                tl, spend, label, url,
            )
        logger.info(
            "page.content() COMPLETE [%s] url=%s took=%.3fs",
            label, url, spend,
        )
        return html

    def _recover(self, page: Page) -> Page:
        """Legacy single-tick recovery — now superseded by session rotation."""
        return self._relaunch("recover requested")

    # ── Main tick (FAST PATH — hard 5s live-cycle requirement) ───

    def _tick(self, page: Page) -> Page:
        """Measure a complete fast cycle, including early returns."""
        with PERFORMANCE.tick_scope():
            with PERFORMANCE.measure("collector.tick_duration"):
                return self._tick_body(page)

    def _tick_body(self, page: Page) -> Page:
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
        # Watchdog completeness gate: reset at entry so any early return
        # (fresh context, empty parse, exception escape) leaves it False —
        # only a full pass through persistence, subscriptions, rotation
        # bookkeeping, deviation flush and the state write reaches the
        # completion block that sets it True.
        self._tick_completed_fully = False
        logger.info("fast tick %d start (url=%s)", self.stats["ticks"], page.url)

        # ── PHASE 5 TICK BUDGET ───────────────────────────────────────
        # page_capture_budget_remaining tracks how much of the 12 s budget
        # is left for page.content() calls this tick.  Starts at full and
        # decrements by actual wall-clock spend (not the deadline).
        page_capture_budget_remaining = self.PAGE_CAPTURE_BUDGET_S
        # tick hard ceiling: if the tick has not completed by this wall-
        # clock deadline, recycle the page so the pipeline does not stall.
        tick_deadline = t_tick + self.TICK_HARD_CEILING_S

        # 1. Parse the live panel (the fast path's ONLY Playwright
        #    round-trip; recovery navigations below are failure-only)
        # Use timed wrapper so a wedged page never blocks the fast loop.
        t_capture = time.monotonic()
        html = self._page_content_timed(page, "tick", timeout_s=self.PAGE_CONTENT_TICK_TIMEOUT_S)
        if html is None:
            logger.warning("page.content() timed out — requesting fresh context")
            return self._fresh_context("page.content() timed out")
        spend = time.monotonic() - t_capture
        self._tick_stats["page_content_ms"].append(spend)
        page_capture_budget_remaining -= spend
        logger.debug("tick page.content() used %.3fs, %.3fs budget remaining",
                     spend, page_capture_budget_remaining)
        t_board = time.monotonic()
        with PERFORMANCE.measure("collector.competition_parsing"):
            comps = find_relevant_competitions(html)
        self._tick_stats["board_parse_ms"].append(
            time.monotonic() - t_board)
        if not comps:
            logger.warning("no relevant competitions found — refreshing page")
            self._ensure_discovery_page(page)
            if page_capture_budget_remaining <= 0:
                logger.warning("page-capture budget exhausted — fresh context")
                return self._fresh_context("page-capture budget exhausted")
            t_retry = time.monotonic()
            html = self._page_content_timed(page, "retry", timeout_s=self.PAGE_CONTENT_RETRY_TIMEOUT_S)
            if html is None:
                logger.warning("retry page.content() timed out — requesting fresh context")
                return self._fresh_context("page.content() timed out (retry)")
            spend = time.monotonic() - t_retry
            # bucketed as retry_ms (NOT page_content_ms) so the two
            # capture classes stay separable in the percentiles
            self._tick_stats["retry_ms"].append(spend)
            page_capture_budget_remaining -= spend
            logger.debug("retry page.content() used %.3fs, %.3fs budget remaining",
                         spend, page_capture_budget_remaining)
            t_board = time.monotonic()
            with PERFORMANCE.measure("collector.competition_parsing"):
                comps = find_relevant_competitions(html)
            self._tick_stats["board_parse_ms"].append(
                time.monotonic() - t_board)
        if not comps:
            self._empty_ticks += 1
            # An empty panel is a COMPLETE cycle that found nothing to
            # collect — not an incomplete one.  The loop is demonstrably
            # cycling (capture, parse, state write all ran); feeding the
            # dog here is honest.  Counting it as incomplete would turn a
            # genuine site-side empty period into a systemd restart
            # storm; the state file's consecutive_empty_ticks +
            # status=stalled are the observability signal for emptiness.
            self._tick_completed_fully = True
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
        t_snapshot = time.monotonic()
        with PERFORMANCE.measure("collector.tracked_row_processing") as row_measure:
            with self._track_lock:
                for comp in comps:
                    cls = comp.classification
                    for row in comp.games:
                        row_measure.add("rows_seen")
                    # Canonical identity boundary: Betual rows are
                    # normalized ONCE at discovery so the panel key, the
                    # tracked key, the DB record and the API payload all
                    # share the canonical name (Betual's "Virtual"
                    # presentation marker stripped; a no-op for Cyber /
                    # conventional names).  Idempotent.
                    row.home_team, row.away_team = self._canonical_teams(
                        row.home_team, row.away_team, cls.value)
                    key = f"{row.home_team}|{row.away_team}"
                    seen_keys[cls.value].add(key)
                    game = self._tracked[cls.value].get(key)
                    if game is None:
                        self._queue_resolve(cls, row)
                    else:
                        to_snapshot.append((game, row, cls))
        self._tick_stats["game_discovery_ms"].append(
            time.monotonic() - t_snapshot)
        logger.info("tick %d tracked_row_processing took %.3fs", self.stats["ticks"], time.monotonic() - t_snapshot)

        # 2b. PHASE 4 — bounded NULL-score recovery.  Rows whose scores
        # rendered blank get AT MOST ONE immediate re-read each, for at
        # most NULL_SCORE_RECOVER_MAX_PER_TICK games per tick, only while
        # the page-capture budget lasts, and never for a game inside its
        # backoff window.  A game that stays NULL is marked incomplete
        # (logged + counted, persisted exactly as before — the observation
        # semantics are unchanged) and backed off so one blank-rendering
        # game can never monopolize the tick's capture budget.
        (to_snapshot, page_capture_budget_remaining) = (
            self._recover_null_scores(
                to_snapshot, page, page_capture_budget_remaining))

        # 3. Persist list-level snapshots (DB work OUTSIDE the track lock;
        #    _store_list_snapshot re-takes it only around dict mutations)
        t_persist = time.monotonic()
        for game, row, cls in to_snapshot:
            try:
                self._store_list_snapshot(game, row, cls)
            except Exception:
                logger.error("list snapshot failed:\n%s",
                             traceback.format_exc())
        logger.info("tick %d persistence took %.3fs (%d games)", self.stats["ticks"], time.monotonic() - t_persist, len(to_snapshot))
        self._tick_stats["persistence_ms"].append(
            time.monotonic() - t_persist)

        # 3b. LIVE market freshness — keep a market subscription for
        #     EVERY active game on the fast page's authenticated socket
        #     so the feed pushes each game's MatchTotal continuously
        #     (target LIVE_MARKET_FRESH_TARGET_S).  Cheap: one evaluate.
        t_sub = time.monotonic()
        try:
            self._sync_market_subscriptions(page)
        except Exception:
            logger.error("market subscription sync error:\n%s",
                         traceback.format_exc())
        logger.info("tick %d market_subscriptions took %.3fs", self.stats["ticks"], time.monotonic() - t_sub)
        sub_ms = time.monotonic() - t_sub
        self._tick_stats["sub"].append(sub_ms)
        self._tick_stats["ws_subscription_ms"].append(sub_ms)

        # 4. Mark unseen games ended + drop resolve requests for rows that
        #    vanished (bounded work: one locked pass over the maps)
        t_end = time.monotonic()
        self._mark_ended(seen_keys)
        self._prune_pending_resolve(seen_keys)
        self._tick_stats["ended_game_ms"].append(time.monotonic() - t_end)
        logger.info("tick %d mark_ended+prune took %.3fs", self.stats["ticks"], time.monotonic() - t_end)

        # 5. Request a SLOW round (event-view rotation) when due.  This is
        #    a flag + event set — never a join, never a wait: the worker
        #    picks it up on its own thread at its own pace.
        now_m = time.monotonic()
        if now_m - self._last_slow_request_at >= EVENT_VIEW_MIN_INTERVAL_S:
            self._last_slow_request_at = now_m
            self._slow_job_pending = True
            self._slow_wake.set()

        self.stats["games_seen"] = sum(len(v) for v in self._tracked.values())
        self._tick_stats["tick_total_ms"].append(
            time.monotonic() - t_tick)
        logger.info(
            "fast tick %d done in %.2fs: tracked=%d snapshots=%d errors=%d"
            "%s",
            self.stats["ticks"], time.monotonic() - t_tick,
            self.stats["games_seen"], self.stats["snapshots"],
            self.stats["errors"],
            (f" — page-capture budget remaining: %.3fs"
             % page_capture_budget_remaining) if page_capture_budget_remaining > 0 else "",
        )
        # Phase 3 P2 — flush all dirty games through the deviation
        # benchmark ONCE per tick (after all observations for this tick
        # have landed).  This is the sole deviation refresh point per tick
        # per game; _refresh_projections skips the deviation layer for
        # clean games.
        # IMPORTANT: set _last_fast_completed_at BEFORE the blocking state
        # write so the watchdog thread sees a completed cycle even if the
        # state file write stalls (1.7MB JSON on every tick, DB lock
        # contention with the deviation backfill thread).
        # Watchdog completeness gate: the tick has NOW done its real work
        # (capture, parse, persistence, subscriptions, ended-marking).
        # Set before the deviation flush + state write so the watchdog
        # sees a completed cycle even if those tail steps stall (they are
        # failure-isolated and re-run next tick); this preserves the
        # 59f6791 placement rationale documented below.
        self._tick_completed_fully = True
        self._last_fast_completed_at = time.monotonic()
        self._fast_cycle_completed_at_iso = utcnow_iso()
        t_deviation = time.monotonic()
        try:
            self._flush_deviation_dirty()
        except Exception:
            logger.error("deviation dirty flush error:\n%s",
                         traceback.format_exc())
        logger.info("tick %d deviation_flush took %.3fs", self.stats["ticks"], time.monotonic() - t_deviation)
        t_state = time.monotonic()
        self._write_state(success=True)
        logger.info("tick %d state_write took %.3fs", self.stats["ticks"], time.monotonic() - t_state)
        self._tick_stats["fast_work_ms"].append(time.monotonic() - t_tick)
        self._tick_stats["work"].append(time.monotonic() - t_tick)
        return page

    def _recover_null_scores(
            self,
            to_snapshot: list,
            page: "Page",
            budget_remaining: float,
    ) -> tuple[list, float]:
        """PHASE 4 — bounded NULL-score recovery (directive 2026-09-30).

        Rows whose scores rendered blank get AT MOST ONE immediate re-read
        each, for at most NULL_SCORE_RECOVER_MAX_PER_TICK games per tick,
        only while the page-capture budget lasts, and never for a game
        inside its backoff window.  A game that stays NULL after
        NULL_SCORE_INCOMPLETE_AFTER consecutive attempts is marked
        INCOMPLETE (logged + counted) and backed off for
        NULL_SCORE_BACKOFF_TICKS ticks so one blank-rendering game can
        never monopolize the tick's capture budget.  Returns the (possibly
        score-filled) snapshot list and the remaining budget.  Persisted
        exactly as before on failure — observation semantics unchanged.
        Pure bookkeeping + one timed DOM read per candidate.
        """
        null_recovered: list[tuple[PokerBetGame, RowGame, object]] = []
        null_candidates = [
            (g, r, c) for (g, r, c) in to_snapshot
            if r.home_score is None or r.away_score is None
        ]
        if not null_candidates:
            return to_snapshot, budget_remaining
        t_null = time.monotonic()
        try:
            recover_budget = self.NULL_SCORE_RECOVER_MAX_PER_TICK
            for game, row, cls in null_candidates:
                if recover_budget <= 0:
                    break
                gid = game.source_game_id
                if self._null_score_backoff_until.get(gid, 0) > self._tick_no:
                    self._null_recovery_stats["backoff_skips"] += 1
                    continue
                if budget_remaining <= 0:
                    self._null_recovery_stats["budget_skips"] += 1
                    break
                t_ncap = time.monotonic()
                html_n = self._page_content_timed(
                    page, "null-retry",
                    timeout_s=min(self.PAGE_CONTENT_RETRY_TIMEOUT_S,
                                  max(0.5, budget_remaining)))
                n_spend = time.monotonic() - t_ncap
                budget_remaining -= n_spend
                if html_n is None:
                    break
                self._null_recovery_stats["attempts"] += 1
                recover_budget -= 1
                n_comps = find_relevant_competitions(html_n)
                n_row = None
                for n_comp in n_comps:
                    if n_comp.classification != cls:
                        continue
                    want = f"{row.home_team}|{row.away_team}"
                    for cand in n_comp.games:
                        if (f"{cand.home_team}|{cand.away_team}" == want
                                and cand.home_score is not None
                                and cand.away_score is not None):
                            n_row = cand
                            break
                    if n_row is not None:
                        break
                if n_row is not None:
                    self._null_recovery_stats["recovered"] += 1
                    self._null_score_streak.pop(gid, None)
                    self._null_incomplete_marked.discard(gid)
                    null_recovered.append((game, n_row, cls))
                    logger.info(
                        "null-score recovery: %s scores filled from re-read "
                        "(%s-%s)", gid, n_row.home_score, n_row.away_score)
                else:
                    streak = self._null_score_streak.get(gid, 0) + 1
                    self._null_score_streak[gid] = streak
                    if streak >= self.NULL_SCORE_INCOMPLETE_AFTER:
                        self._null_score_backoff_until[gid] = (
                            self._tick_no + self.NULL_SCORE_BACKOFF_TICKS)
                        if gid not in self._null_incomplete_marked:
                            self._null_incomplete_marked.add(gid)
                            self._null_recovery_stats["incomplete_marked"] += 1
                            logger.warning(
                                "null-score INCOMPLETE: %s still blank after "
                                "%d bounded retries — marked incomplete, "
                                "re-reads backed off %d ticks",
                                gid, streak, self.NULL_SCORE_BACKOFF_TICKS)
        finally:
            self._tick_stats["score_parse_ms"].append(
                time.monotonic() - t_null)
        for game, n_row, cls in null_recovered:
            for i, (g, r, _c) in enumerate(to_snapshot):
                if g.source_game_id == game.source_game_id:
                    to_snapshot[i] = (g, n_row, cls)
                    break
        return to_snapshot, budget_remaining

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

    def _authoritative_teams(self, row: RowGame, tax: dict, text: str,
                             cls: Optional[Classification] = None
                             ) -> tuple[str, str]:
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
        # Betual canonicalization boundary: authoritative persisted names
        # never carry the "Virtual" presentation marker (Betual-origin
        # data only).
        return self._canonical_teams(
            home, away, cls.value if cls is not None else None)

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
        # HALFTIME-CONTINUITY GUARD (defect 30964771, 2026-09-20): a frame
        # whose team scores are IDENTICAL to the stored last row's is the
        # SAME GAME's frozen state, never a new replay.  The half-end
        # boundary is exactly where this happens: the panel freezes on the
        # Q2-end score (47-61) and displays the break clock ("Half End"
        # 12:00), which parses as mid-Q2 and made the OLD code read a
        # legitimate continuation as `clock_regression` — splitting the
        # game and orphaning its second half (and the final) into a #iN
        # sibling that settlement and the alert panel never bridge.  A
        # genuine replay is never observed at the identical both-team score
        # (it restarts at 0-0 or a low early-game total — signal 1's 50%
        # threshold or the signals below catch it; a replay frame at the
        # exact stored score would split one tick later, at a negligible
        # one-frame pollution cost vs losing the whole game's final).
        if home == lh and away == la:
            return None
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
        with PERFORMANCE.measure("collector.store_list_snapshot"):
            return self._store_list_snapshot_impl(game, row, comp)

    def _store_list_snapshot_impl(self, game: PokerBetGame, row: RowGame, comp) -> None:
        """Persist the list-level observation for a known game.

        Detects virtual-replay score resets: when the panel row shows a
        fresh instance of the same fixture, the finished game is ended
        and the snapshot is recorded under a NEW instance record so the
        two games never share a history.
        """
        t_dbw = time.monotonic()
        if game.status == "ended":
            return
        with PERFORMANCE.measure("collector.instance_reset_check"):
            sig = self._detect_instance_reset(game, row)
        if sig:
            game = self._split_instance(game, row, comp, signal=sig, path="list")
        obs = MarketObservation(
            source=SOURCE_POKERBET,
            source_game_id=game.source_game_id,
            classification=game.classification,
            captured_at=utcnow_iso(),
            home_team=(self._canonical_teams(
                row.home_team or game.home_team,
                row.away_team or game.away_team,
                game.classification)[0]),
            away_team=(self._canonical_teams(
                row.home_team or game.home_team,
                row.away_team or game.away_team,
                game.classification)[1]),
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
        with PERFORMANCE.measure("collector.game_db_lookup"):
            game_id = self._game_db_id(game)
        t_dbw = time.monotonic()
        with PERFORMANCE.measure("collector.insert_snapshot") as insert_measure:
            row_id = self.store.insert_snapshot(game_id, obs)
            insert_measure.add("sql_calls", 2)  # duplicate SELECT + INSERT attempt
        self._tick_stats["db_write_ms"].append(
            time.monotonic() - t_dbw)
        if row_id:
            self.stats["snapshots"] += 1
            self._record_clean(game, obs)
        with self._track_lock:
            self._unseen_ticks[game.classification][
                f"{row.home_team}|{row.away_team}"
            ] = 0

    def _record_clean(self, game: PokerBetGame, obs: MarketObservation) -> None:
        with PERFORMANCE.measure("collector.record_clean") as timing:
            timing.add("accepted_snapshots")
            return self._record_clean_impl(game, obs)

    def _record_clean_impl(self, game: PokerBetGame, obs: MarketObservation) -> None:
        """Feed one VALIDATED observation into the clean metrics DB.

        Called only after ``insert_snapshot`` accepted the row (so exact
        capture duplicates are never re-recorded) and only for the live
        instance the collector wrote to — the replay protections already
        routed post-final frames to a fresh #iN instance, never back to
        the finished base.  Failure-isolated: any clean-metrics error is
        logged and swallowed so collection is unaffected.

        Phase 3 P2 — deviation dirty gating: marks the game dirty so the
        deviation benchmark is refreshed exactly ONCE per tick (in the
        end-of-tick flush) rather than once per accepted snapshot.  The
        pace projection refresh still runs immediately so the subsequent-
        observation linkage stays current within the tick.
        """
        if self.clean_metrics is None:
            return
        try:
            self.clean_metrics.record_snapshot_obs(game, obs, self.store)
            # Mark dirty BEFORE _refresh_projections so the pace projector
            # still runs immediately (for subsequent-observation linkage),
            # but the deviation refresh is deferred to the end-of-tick flush.
            self._deviation_dirty.add(game.source_game_id)
            self._refresh_projections(game.source_game_id)
        except Exception:
            logger.error("clean metrics record failed:\n%s",
                         traceback.format_exc())

    def _refresh_projections(self, source_game_id: str) -> None:
        """Recompute the deterministic pace-trajectory rows for one game
        from its VALID clean observations (idempotent, failure-isolated).
        Called as observations arrive so the subsequent-observation
        linkage stays current; also called on finalize so
        final_settled_total updates when the game completes.

        Phase 3 P2 — deviation dirty gating: this method runs ONLY the
        pace projection (always needed for subsequent-observation linkage).
        The deviation benchmark refresh is deliberately NOT called here —
        it is deferred to ``_flush_deviation_dirty`` (called once per tick
        in ``_tick_body``) or called directly from ``_finalize_clean``
        when the game is ending.  This ensures deviation.refresh_game is
        called at most once per game per tick, regardless of how many
        observations arrive in that tick.
        """
        if self.clean_metrics is None:
            return
        try:
            PaceProjector().refresh_game(self.clean_metrics, source_game_id)
        except Exception:
            logger.error("pace projector refresh failed:\n%s",
                         traceback.format_exc())

    def _flush_deviation_dirty(self) -> None:
        """Phase 3 P2 — flush dirty games through the deviation benchmark.

        Called ONCE per tick (at the end of ``_tick_body``) after all
        observations for the tick have been accepted.  This collapses
        N per-snapshot deviation refreshes per game into at most ONE
        refresh per game per tick, eliminating the dominant work
        amplification seen in the Phase 2 baseline (51.2 refreshes per
        accepted snapshot → 1 per dirty game per tick).

        Games are removed from ``_deviation_dirty`` only after a
        SUCCESSFUL refresh so a transient failure retries on the next
        tick rather than silently losing the residual.  Failure-isolated:
        a crash during flush is logged and does not affect collection.
        """
        if self._deviation is None or not self._deviation_dirty:
            return
        # Snapshot the set so new marks that arrive concurrently (from
        # the WS frame handler on the main thread) are not cleared
        # until they have their own flush.
        to_flush = set(self._deviation_dirty)
        for gid in to_flush:
            try:
                self._deviation.refresh_game(gid)
                self._deviation_dirty.discard(gid)
            except Exception:
                logger.error("deviation flush failed for %s:\n%s",
                             gid, traceback.format_exc())

    def _finalize_clean(
        self, game: PokerBetGame, obs: Optional[MarketObservation] = None,
    ) -> None:
        """Record the final result on the clean game/instance record.

        ``obs`` carries the verified final scores when the event view
        observed the terminal state; otherwise the game ended unseen and
        the clean record is finalized as UNKNOWN (NULL finals).

        Phase 3 P2: calls deviation.refresh_game IMMEDIATELY (not deferred)
        because the game is ending — it will be dropped from tracking after
        this call so the end-of-tick flush would never see it.  The pace
        projection is updated unconditionally via _refresh_projections.
        """
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
            # Deviation refresh: run immediately on finalization (game is
            # ending and will be untracked; the end-of-tick flush won't see
            # it).  Remove from dirty set so the flush skips it (it was
            # already processed here).
            if self._deviation is not None:
                try:
                    self._deviation.refresh_game(game.source_game_id)
                    self._deviation_dirty.discard(game.source_game_id)
                except Exception:
                    logger.error("deviation finalize refresh failed:\n%s",
                                 traceback.format_exc())
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
                home_team=(self._canonical_teams(
                    parsed["home_team"] or game.home_team,
                    parsed["away_team"] or game.away_team,
                    game.classification)[0]),
                away_team=(self._canonical_teams(
                    parsed["home_team"] or game.home_team,
                    parsed["away_team"] or game.away_team,
                    game.classification)[1]),
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
            # Quarter-score collection (directive 2026-09-22, DATA
            # COLLECTION ONLY) — record exactly what the verified event
            # view presented, validated and flagged, never corrected.
            self._record_quarter_scores(game, parsed)
            # Betual-only dataset (§1): internal-timer score observation
            # from the SAME verified parse — failure-isolated, and
            # nothing here feeds any decision path (§19).
            try:
                self._betual_record_score(game, parsed, obs.captured_at)
            except Exception:
                logger.error("betual score record failed:\n%s",
                             traceback.format_exc())
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
        # PHASE 3: lifecycle LIVE -> FINALIZING -> DONE — a verified final
        # is the DONE transition; the WS bridge stops accepting frames.
        self._end_state_terminal[game.source_game_id] = "ended"
        cls_val = game.classification
        key = f"{game.home_team}|{game.away_team}"
        with self._track_lock:
            self._tracked.get(cls_val, {}).pop(key, None)
            self._unseen_ticks.get(cls_val, {}).pop(key, None)
            if game.source_game_id in self._market_queue:
                self._market_queue.remove(game.source_game_id)
            # a verified final supersedes any open final-capture window
            self._final_capture_gids.discard(game.source_game_id)
            self._final_capture_until.pop(game.source_game_id, None)
            self._final_capture_armed.discard(game.source_game_id)
        last = self.store.get_snapshots(game.source_game_id, limit=1)
        lr = last[0] if last else {}
        logger.info(
            "verified final for %s — game ended (final %s-%s %s %s)",
            game.source_game_id, lr.get("home_score"), lr.get("away_score"),
            lr.get("period_label"), lr.get("clock"),
        )
        self.stats["games_ended_final"] += 1
        # §12 Betual game-end capture (evidence='observed_final' via the
        # event-view path; a disappearance records evidence='disappeared').
        try:
            self._betual_end_game(game)
        except Exception:
            logger.error("betual game end failed:\n%s", traceback.format_exc())
        try:
            self.betual.discard(game.source_game_id)
        except Exception:
            pass

    @staticmethod
    def _first_team_total(parsed: dict, index: int) -> Optional[float]:
        vals = [v.get("line") for v in parsed["team_totals"].values()]
        if index < len(vals):
            return vals[index]
        return None

    # ── PHASE 5 — checkpoint-aware scheduling (directive 2026-09-30) ─

    # Progress tier of a tracked game, computed WITHOUT touching any
    # checkpoint definition: pure arithmetic over the clean trajectory.
    #   1  checkpoint-critical  — latest clean projection is at/after an
    #                            alert checkpoint boundary (75/50) and the
    #                            closest one is still ahead (within 2
    #                            game-minutes) — captures feed LIVE alert
    #                            evaluation right now
    #   2  active-valid         — a clean projection exists and its
    #                            captured_at is fresh (< 40s)
    #   3  normal-live          — tracked + no fresh clean state (young
    #                            games, projections disabled, etc.)
    #   4  stale/uncertain      — clean state exists but is older than
    #                            PROGRESS_STALENESS_S (state drifted)
    #   5  finalizing           — inside the final-capture window
    #   6  DONE                 — ended / terminal
    # Defaults to tier 3 on ANY error — scheduling degrades gracefully.
    PROGRESS_STALENESS_S = 40.0
    CHECKPOINT_CRITICAL_LOOKAHEAD_MIN = 2.0

    def _progress_tier(self, gid: str) -> tuple:
        try:
            if (gid in self._final_capture_gids
                    or gid in self._final_capture_armed):
                return (5, 0.0)
            game = self._find_tracked(gid)
            if (game is None or game.status == "ended"
                    or gid in self._end_state_terminal):
                return (6, 0.0)
            with self._track_lock:
                eff_gid = self._instances.get(gid, gid)
            if self.clean_metrics is None:
                return (3, 0.0)
            row = self.clean_metrics.db.execute(
                "SELECT captured_at, progress_pct FROM clean_projections "
                "WHERE source_game_id=? ORDER BY captured_at DESC LIMIT 1",
                (eff_gid,)).fetchone()
            if row is None:
                return (3, 0.0)
            from datetime import datetime, timezone
            cap = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - cap).total_seconds()
            if age > self.PROGRESS_STALENESS_S:
                return (4, 0.0)
            prog = row[1]
            if prog is not None:
                # The alert's checkpoint set, expressed as a boundary list
                # (semantic constants stay in live_analytics.under_alert —
                # checkpoint DEFINITIONS are not modified).
                boundaries = [b for b in (75, 50) if b > float(prog)]
                if boundaries:
                    nxt = min(boundaries)
                    if float(prog) >= 50.0 and (
                            nxt - float(prog)) * self._full_game_minutes(
                                eff_gid) / 100.0 <= self.CHECKPOINT_CRITICAL_LOOKAHEAD_MIN:
                        return (1, nxt)
            return (2, 0.0)
        except Exception:
            return (3, 0.0)

    def _full_game_minutes(self, gid: str) -> float:
        """Full game length in minutes for the game's classification
        (Betual 4x10, Cyber 4x12).  Read from the DURATION basis the pace
        projector itself uses (projection.duration_for), never redefined
        here.  Falls back to 48 (NBA-length) on any error."""
        try:
            from blm_v4.projection import duration_for
            game = self._find_tracked(gid)
            cls_val = game.classification if game is not None else None
            return float(duration_for(cls_val)[1])
        except Exception:
            return 48.0

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

    def _sort_market_queue_checkpoint_aware(
            self, never_line: set[str]) -> None:
        """PHASE 5 — order the market-rotation queue by capture priority.

        never-line games first (early-checkpoint line coverage, existing
        behavior verbatim), then checkpoint-critical (tier 1), then the
        remaining tiers (2 active-valid, 3 normal-live, 4 stale/uncertain,
        5 finalizing, 6 DONE).  Stable on gid within a tier.  No checkpoint
        definition is consulted or changed — tiers are pure arithmetic over
        the clean trajectory (_progress_tier).
        """
        def _sort_key(gid: str) -> tuple:
            if gid in never_line:
                return (0, gid)      # never-line priority preserved verbatim
            tier, _sub = self._progress_tier(gid)
            return (tier, gid)
        self._market_queue.sort(key=_sort_key)

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

    # ── Slow-page self-heal (2026-09-13 zombie-worker incident) ──────

    class PageUnavailable(RuntimeError):
        """The worker's slow event-view page could not be (re)created.

        Raised by _ensure_worker_page so the worker loop can apply a
        bounded backoff instead of hot-looping launches; never escapes
        the worker thread."""

    def _ensure_worker_page(self) -> None:
        """WORKER-ONLY guard: guarantee a usable slow event-view page.

        Uses the EXISTING slow-browser lifecycle machinery only —
        _rotate_slow_browser for a missing worker browser (it re-launches
        and rebuilds the page), _ensure_slow_page for a missing page on a
        live browser.  The fast-path _relaunch() is deliberately NOT
        called from here: Playwright sync objects are thread-affine and
        the fast browser belongs to the main thread (pinned by
        tests/test_collector_crash_recovery.py).

        Raises PageUnavailable when the page still cannot be created so
        the caller applies its backoff.  A healthy page returns quickly
        (no-op) and never launches anything.
        """
        if self._slow_page is not None:
            return
        if self._slow_stop.is_set():
            raise self.PageUnavailable("worker stopping")
        now_m = time.monotonic()
        if now_m < self._slow_page_ensure_failed_until:
            # Inside the post-failure backoff window: fail fast WITHOUT
            # spamming launches (the loop sleeps 2s per attempt).
            raise self.PageUnavailable("page recreation backing off")
        logger.warning(
            "slow worker page unavailable — recreating (pending_resolve "
            "context preserved)")
        try:
            if self._slow_browser is None:
                self._rotate_slow_browser()      # relaunch + _ensure_slow_page
            else:
                self._ensure_slow_page()         # page on the live browser
        except Exception:
            logger.error("slow page recreation failed:\n%s",
                         traceback.format_exc())
        if self._slow_page is None:
            # Bounded backoff: 5s doubling to 60s.  The throttled zombie
            # diagnostic (_warn_slow_page_unavailable) keeps the state
            # visible without a 2s log storm.
            prev = max(0.0, self._slow_page_ensure_failed_until - now_m)
            delay = min(60.0, max(5.0, prev * 2.0))
            self._slow_page_ensure_failed_until = now_m + delay
            self._warn_slow_page_unavailable()
            raise self.PageUnavailable(
                f"slow page still unavailable (retry in {delay:.0f}s)")
        self._slow_page_ensure_failed_until = 0.0
        logger.info("slow worker page recovered")

    def _warn_slow_page_unavailable(self, detail: str = "") -> None:
        """Throttled zombie-state diagnostic: at most once per 60s, with
        the suppression count folded into the next emission, so a
        pageless worker can never again masquerade as normal operation
        in a silent journal."""
        now_m = time.monotonic()
        w = self._slow_page_warn
        if now_m - w["ts"] < 60.0:
            w["n"] += 1
            return
        suppressed = w["n"]
        w["ts"], w["n"] = now_m, 0
        with self._track_lock:
            pending = len(self._pending_resolve)
        logger.warning(
            "slow resolve page unavailable%s: worker_alive=%s "
            "browser=%s pending_resolve=%d queue_len=%d%s",
            f" ({detail})" if detail else "",
            bool(self._slow_thread is not None and self._slow_thread.is_alive()),
            self._slow_browser is not None,
            pending, len(self._market_queue),
            f" [suppressed {suppressed} repeats]" if suppressed else "",
        )

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
        # PHASE 5 — checkpoint-aware ordering: the WHOLE round visits
        # checkpoint-critical games FIRST (their captures feed live alert
        # evaluation at the 75%/50% boundaries), then everyone else in the
        # existing never-line-first + freshness-gated rotation order.
        # One indexed SELECT per game, once per round — negligible against
        # a multi-second per-game event-view visit.
        try:
            self._sort_market_queue_checkpoint_aware(never_line)
        except Exception:
            logger.error("progress-tier sort failed (rotation continues "
                         "in queue order):\n%s", traceback.format_exc())
        for _ in range(len(self._market_queue)):
            if captured >= MARKET_BATCH:
                break
            if self._event_view_failures >= BROWSER_RELAUNCH_AFTER_EMPTY:
                logger.warning("event-view failures %d — rotating browser",
                               self._event_view_failures)
                # worker thread: rotate the WORKER'S OWN browser.  The
                # fast-path _relaunch() drives self._pw, whose greenlet
                # belongs to the main thread — calling it here raises
                # greenlet.error("Cannot switch to a different thread")
                # AND closes the fast browser on the way out.
                self._rotate_slow_browser()
                self._event_view_failures = 0
                break
            gid = self._market_queue.pop(0)
            self._market_queue.append(gid)
            last = self._last_market_at.get(gid)
            # NO FINAL fix: a game inside its final-capture window is
            # visited REGARDLESS of the freshness gate — the whole point
            # is to capture the terminal state before the window ends.
            final_capture = gid in self._final_capture_gids
            # Early-checkpoint priority: a game that has NEVER had a
            # verified market total line is not-yet-covered — visit it
            # regardless of the freshness gate (unless we captured it
            # within the last few seconds).  This gets young games their
            # first line at the earliest possible checkpoint.
            if gid in never_line:
                if last and _ts_age_s(last) < 15:
                    self._market_stats["skipped_fresh_arms"] += 1
                    continue  # just visited; avoid a hot loop
            elif self._progress_tier(gid)[0] == 1:
                pass  # checkpoint-critical: exempt from the freshness gate
            elif (last and _ts_age_s(last) < MARKET_REFRESH_S
                  and not final_capture):
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
            if game is None or game.status == "ended" or not game.source_url:
                # 2026-09-13 incident: untracked gids (ended by
                # _mark_ended before this visit, pruned, or lost across a
                # restart) used to be CYCLED FOREVER — every cycle burned
                # an attempt, drove _event_view_failures toward the
                # browser-rotation storm, and let dead IDs starve live
                # games behind them.  Tracked-game state is the authority:
                # if it says the game is gone (or terminal), drop the
                # queue entry.  Legitimately tracked live games are never
                # touched by this branch.
                self._market_stats["dropped_untracked"] = (
                    self._market_stats.get("dropped_untracked", 0) + 1)
                logger.info(
                    "market queue: dropping untracked gid %s (no live "
                    "tracked game) — kept legitimate pending games", gid)
                try:
                    self._market_queue.remove(gid)
                except ValueError:
                    pass
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
                # PHASE 2: line-parse timing (event-view scoreboard +
                # market text -> structured parse) is its own bucket so a
                # slow parse is never misattributed to page capture.
                t_lp = time.monotonic()
                parsed = parse_event_view(text)
                self._tick_stats["line_parse_ms"].append(
                    time.monotonic() - t_lp)
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
        """Make sure ``page`` shows a live LOBBY containing rows for ``cls``.

        The SPA's event-view route hydrates only from an in-app row click
        on the matching lobby list.  A stale event page or a foreign
        competition's lobby makes every click fail — the cross-lobby
        starvation that left whole leagues (CYBER_2K26, BETUAL_NBA) with
        zero market capture for hours.

        2026-09-12 fix (identity-resolution starvation): the old check
        accepted ANY rendered row (``.market-game-section`` exists) and
        only reloaded when the page had NO rows at all, so a degraded
        session that still rendered other sports' sections passed the
        check while the target competition's rows were missing — every
        subsequent row click failed (measured 2026-09-12: ~23k "row not
        found" resolve failures vs 7 successes, successful resolves
        clustered only right after hourly browser rotations).  The check
        now counts rows inside the section matching THIS competition and
        re-expands/reloads until those rows actually exist.
        """
        try:
            # Header needles are deliberately tight so neighbouring
            # virtual sections cannot satisfy the wrong classification
            # ("Betual England Premier League" football, "Cyber Tennis
            # AO2" — both render under expanded Virtual/Cyber trees).
            needles = (["cyber basketball", "2k26"]
                       if cls is Classification.CYBER_2K26
                       else ["betual nba"])
            for _ in range(3):
                has_rows = page.evaluate(
                    """(needles) => {
                        const sections = document.querySelectorAll('.sp-sub-list-bc');
                        for (const s of sections) {
                            const head = s.querySelector('.sp-s-l-head-bc');
                            if (!head) continue;
                            const titles = [...head.querySelectorAll('p.sp-s-l-h-title-bc')]
                                .map(p => (p.textContent || '').trim().toLowerCase());
                            const t = titles.join(' ');
                            if (!needles.some(n => t.includes(n))) continue;
                            const rows = [...s.querySelectorAll('.market-game-section')].filter(r => {
                                let p = r.parentElement;
                                while (p) {
                                    if (p.classList && p.classList.contains('sp-sub-list-bc')) return p === s;
                                    p = p.parentElement;
                                }
                                return false;
                            });
                            if (rows.length > 0) return rows.length;
                        }
                        return 0;
                    }""", needles)
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
                    const stripV = (s) => (s || '').trim()
                        .replace(/^virtual\\s+/i, '')
                        .replace(/\\s+virtual$/i, '')
                        .replace(/\\s+/g, ' ').toLowerCase();
                    const want = [stripV(names.home), stripV(names.away)];
                    const rows = document.querySelectorAll('.market-game-section');
                    for (const r of rows) {
                        const rnames = [...r.querySelectorAll('.market-game-team-name')]
                            .map(n => stripV(n.textContent));
                        if (rnames.length >= 2 && rnames[0] === want[0]
                                && rnames[1] === want[1]) {
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
                    const stripV = (s) => (s || '').trim()
                        .replace(/^virtual\\s+/i, '')
                        .replace(/\\s+virtual$/i, '')
                        .replace(/\\s+/g, ' ').toLowerCase();
                    const want = [stripV(names.home), stripV(names.away)];
                    const rows = document.querySelectorAll('.market-game-section');
                    for (const r of rows) {
                        const rnames = [...r.querySelectorAll('.market-game-team-name')]
                            .map(n => stripV(n.textContent));
                        if (rnames.length >= 2 && rnames[0] === want[0]
                                && rnames[1] === want[1]) {
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
        """Marker-tolerant team equality.

        Compares casefolded names after stripping Betual's "Virtual"
        presentation marker from BOTH sides, so a canonical stored name
        ("Lakers") still matches the source-rendered row ("Lakers
        Virtual") and vice versa.  Only the COMPARISON is tolerant —
        identity values themselves are normalized at the dedicated
        boundaries (_canonical_teams), never rewritten here.
        """
        def _norm(x: Optional[str]) -> str:
            return normalize_betual_team(x).casefold()
        return _norm(a) == _norm(b)

    @staticmethod
    def _canonical_teams(
        home: Optional[str], away: Optional[str],
        classification: Optional[str] = None,
    ) -> tuple[str, str]:
        """Canonical (home, away) names at BLM's identity boundary.

        Strips Betual's "Virtual" presentation marker from both names
        (prefix or suffix rendering, whitespace-tolerant) — applied ONLY
        to Betual-origin data (classification gate; a None
        classification normalizes, matching the historical single-family
        pipeline).  The strip is a strict no-op for names without the
        marker — Cyber 2K26 teams end in "Cyber" and conventional teams
        never carry it — so no arbitrary rewriting is possible.
        Deterministic and idempotent: applying it twice equals applying
        it once.
        """
        if classification is not None and classification != "BETUAL_NBA":
            return (home or "", away or "")
        return normalize_betual_team(home), normalize_betual_team(away)

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

    def _tail_provable_final(self, gid: str) -> bool:
        """True when the stored snapshot tail can already prove a final —
        the newest FULLY-SCORED row is a final-labeled or 4th-quarter
        ending by the SAME rules the scorecard's ``_final_result`` uses.
        Degenerate (NULL-score) rows are skipped: they carry no state.
        A mid-quarter scored tail is NOT provable — that is exactly the
        case the final-capture window exists for."""
        try:
            rows = self.store.get_snapshots(gid, limit=25)
        except Exception:
            return True          # fail safe: behave exactly as before
        scored = None
        for r in rows:           # newest-first
            if r.get("home_score") is not None and r.get("away_score") is not None:
                scored = r
                break
        if scored is None:
            return False         # nothing scored: try the window
        period = (scored.get("period_label") or "").lower()
        clock = (scored.get("clock") or "").strip()
        if any(k in period for k in ("full time", "finished",
                                     "end of match", "match ended")):
            return True
        quarter = scored.get("quarter")
        if period.startswith("4th") or (quarter is not None and quarter >= 4):
            q_min, full = duration_for(
                scored.get("classification") or None)
            el = clock_minutes(quarter if quarter is not None else 4,
                               clock, q_min) if clock else None
            if clock in ("00:00", "0:00", "") or el is None \
                    or el >= full - 2.0:
                return True
        return False

    def _mark_ended(self, seen_keys: dict[str, set[str]]) -> None:
        """Grace-based end detection (fast path).  DB writes happen
        OUTSIDE the track lock — only the map mutations are guarded.

        NO FINAL fix (2026-09-22): at grace expiry a game whose tail
        cannot prove a final is kept tracked for ONE bounded final-
        capture window instead of being ended on a degenerate tail; the
        slow rotation is asked to capture its event view (verified scores
        even when the list page renders blank).  Window expiry (or
        BLM_FINAL_CAPTURE_GRACE_S=0, or an already-provable tail) runs
        the normal ended path unchanged."""
        now_m = time.monotonic()
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
                    if self._unseen_ticks[cls_val][key] < self._ended_grace_ticks:
                        continue
                    gid = game.source_game_id
                    until = self._final_capture_until.get(gid)
                    if (game.status != "ended"
                            and self._final_capture_grace_s > 0
                            and gid not in self._final_capture_armed
                            and not self._tail_provable_final(gid)):
                        # arm ONCE: keep tracked, prioritize the capture
                        self._final_capture_armed.add(gid)
                        self._final_capture_gids.add(gid)
                        self._final_capture_until[gid] = (
                            now_m + self._final_capture_grace_s)
                        if gid in self._market_queue:
                            self._market_queue.remove(gid)
                        self._market_queue.insert(0, gid)
                        self.stats["final_capture_retries"] += 1
                        logger.info(
                            "final-capture window armed for %s (tail cannot "
                            "prove a final) — keeping tracked %.0fs",
                            gid, self._final_capture_grace_s)
                        continue
                    if until is not None and now_m < until:
                        continue     # window still open — keep tracked
                    if game.status != "ended":
                        game.status = "ended"
                        to_end.append(game)
                    # PHASE 3: the game has left the live capture set —
                    # record it terminal so the WS bridge suppresses any
                    # further frames for it (lifecycle DONE).
                    self._end_state_terminal[game.source_game_id] = "ended"
                    # keep the game record; drop from live tracking
                    del games[key]
                    if game.source_game_id in self._market_queue:
                        self._market_queue.remove(game.source_game_id)
                    self._final_capture_gids.discard(gid)
                    self._final_capture_until.pop(gid, None)
                    self._final_capture_armed.discard(gid)
                    # §12: a game that left the panel WITHOUT a proven
                    # final — the NO-FINAL diagnosis data (evidence=
                    # 'disappeared', never recorded as final).
                    ended_game = game
                    try:
                        self._betual_end_game(ended_game)
                    except Exception:
                        logger.error("betual disappeared-end failed:\n%s",
                                     traceback.format_exc())
                    try:
                        self.betual.discard(game.source_game_id)
                    except Exception:
                        pass
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
        # Phase 5b: cache source_game_id → db id in-process to eliminate
        # the 25,000+ per-tick SQL calls.  Games are inserted once and
        # never re-inserted, so the mapping is stable for the process lifetime.
        sgid = game.source_game_id
        cached = self._game_id_cache.get(sgid)
        if cached is not None:
            return cached
        with PERFORMANCE.measure("sql.main.get_game") as timing:
            rec = self.store.get_game(sgid)
            timing.add("sql_calls")
            db_id = int(rec["id"]) if rec else 0
        self._game_id_cache[sgid] = db_id
        return db_id

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
