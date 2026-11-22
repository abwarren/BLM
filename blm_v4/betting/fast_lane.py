"""FAST LANE — decide the UNDER trade on the freshest captured row, not on
the 90-second /api/v4/live payload.

WHY THIS EXISTS (measured 2026-10-28, read-only against prod):

  * The trade gate is served from ``/api/v4/live``.  A build of that payload
    takes ~100 s on the production DB and the unit sets
    ``BLM_LIVE_CACHE_TTL_S=90``, so the worker decides on a payload between
    ~90 s and ~190 s old.
  * A TC-X qualifying moment is SHORT: over the last 7 days the cohort pull
    (tc-x) finds 102 runs, a MEDIAN run of 0 s (one captured row) and 33.4
    minutes of total qualifying time in a week.
  * Result: 4,246 betting passes since 2026-10-15 produced ONE candidate,
    while the same captured rows clear the same legs ~100 times in that
    window.  The signal is not wrong; it is invisible to a 90-190 s snapshot.

WHAT IT DOES: every ``interval_s`` it reads the freshest rows of
``clean_observations`` (indexed, milliseconds), evaluates the SAME legs from
``blm_v4.trade_window``, and hands the result to the SAME
``blm_v4.betting.executor.evaluate`` / ``execute`` and the SAME provider the
poll worker uses.

WHAT IT DOES NOT DO: it never changes a gate, a threshold, a stake, a limit or
the one-bet-per-game rule.  It only changes WHEN the decision is taken.  Every
executor gate runs again on the synthetic payload this module builds, including
the alert-freshness bound (``alert_max_age_s``), which is why the row it builds
from must be seconds old, not minutes.

Kill switch: reads ``store.is_enabled()`` fresh on every pass, exactly like the
worker.  ``FAST_LANE_ENABLED=0`` disables the loop at start-up.

STALE LINES (operator directive 2026-10-28): a captured market older than
``MAX_MARKET_AGE_S`` is NOT refused outright.  The book's live betslip is the
authority on line and price and the provider verifies both before it submits,
so the lane spends up to ``stale_attempts`` (default 3, env
``FAST_LANE_STALE_ATTEMPTS``) attempts on that game, at least ``retry_gap_s``
apart.  Every attempt is its own ledger row (``#rN`` identity), the cap is
counted from the ledger so a restart cannot reset it, and an AMBIGUOUS attempt
ends the game's retries immediately — that money may already have moved.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from blm_v4.betting.executor import evaluate, execute
from blm_v4.trade_window import (
    EXEC_MIN_PROGRESS_PCT,
    exec_alert_reason,
    exec_price_reason,
    exec_shortfall_reason,
    execution_window_reason,
    line_age_reason,
    score_reason,
    trade_competition_reason,
    trade_move_reason,
)
from blm_v4.betting import market_heat
from blm_v4.betting import qualify as _qualify
from blm_v4.live_analytics.under_fingerprints import evaluate_fingerprints
from blm_v2.telemetry.logging import get_logger

logger = get_logger("fast_lane")

#: Rows older than this are not "the current moment" — the executor's own
#: alert-freshness gate would refuse them anyway; not reading them keeps the
#: scan cheap and the audit honest.
DEFAULT_LOOKBACK_S = 120.0

#: A market older than this is not a live market (same bound the served
#: ``under_alert_eligibility`` uses for market_stale).
MAX_MARKET_AGE_S = 300.0

#: The per-competition league pace reference only moves when a game settles,
#: so one scan every ten minutes is enough and keeps the pass cheap.
PACE_REF_INTERVAL_S = 600.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(
            str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def legs(*, required_pace, avg_pace, actual_pace, progress_pct,
         total_points, under_odds, competition_slug,
         entry_line=None, open_line=None, league_avg_line=None,
         captured_at=None, market_captured_at=None) -> dict:
    """Every trade leg, in the order the served verdict ANDs them.

    Returns ``{leg_name: reason_or_None}``; ``None`` on every leg means the
    trade is armed.  This is the SAME set of functions the served
    ``under_alert_exec_armed`` flag composes — never a second definition.
    """
    return {
        "competition": trade_competition_reason(competition_slug),
        "pace": exec_alert_reason(required_pace, avg_pace, progress_pct),
        "shortfall": exec_shortfall_reason(required_pace, actual_pace),
        "window": execution_window_reason(progress_pct),
        "score": score_reason(total_points),
        "price": exec_price_reason(under_odds),
        "move": trade_move_reason(entry_line, open_line, league_avg_line),
        "line_age": line_age_reason(captured_at, market_captured_at),
    }


def first_refusal(leg_map: dict) -> Optional[str]:
    """The first failing leg's ``"<leg>:<reason>"``, or ``None`` when armed."""
    for name, reason in leg_map.items():
        if reason is not None:
            return f"{name}:{reason}"
    return None


class FastLaneWorker:
    """Fresh-row evaluator sharing the betting worker's executor and provider."""

    def __init__(self, *, prod_db_path: Path, clean_db_path: Path,
                 cfg, store, provider=None, interval_s: float = 3.0,
                 lookback_s: float = DEFAULT_LOOKBACK_S,
                 stale_attempts: int = 3,
                 log: Optional[Callable[..., None]] = None) -> None:
        self._prod_db = Path(prod_db_path)
        self._clean_db = Path(clean_db_path)
        self.cfg = cfg
        self.store = store
        # THREAD AFFINITY: Playwright sync objects belong to the thread that
        # created them.  The poll worker's provider is wired in ITS thread, so
        # calling it from this thread raises greenlet.error ("Cannot switch to
        # a different thread") on every balance read and turns a submit into
        # PROVIDER_AMBIGUOUS.  None means "build my own on this thread".
        self.provider = provider
        self._owns_provider = provider is None
        self.interval_s = float(interval_s)
        self.lookback_s = float(lookback_s)
        #: how many attempts a STALE captured line gets (operator: "retry 3
        #: times").  Counted from the ledger, so a restart cannot reset it.
        self.stale_attempts = max(1, int(stale_attempts))
        #: minimum seconds between two attempts on the same stale game
        self.retry_gap_s = 20.0
        self._last_attempt_at: dict[str, float] = {}
        #: market-gap monitor: own cadence, diagnostic only
        self.gap_interval_s = 30.0
        self.gap_threshold_s = 120.0
        self._gap_at = 0.0
        self._gap_logged: dict[str, float] = {}
        self._log = log or logger.info
        self._warn = getattr(logger, "warning", logger.info)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._slugs: dict[str, str] = {}
        #: classification per game, filled by the same games scan as _slugs —
        #: it selects the quarter length the Q3 segment is divided by
        self._cls: dict[str, str] = {}
        self._slugs_at = 0.0
        self._pace: dict = {}
        self._pace_at = 0.0
        self._rate: float = 0.0          # observed clean_observations rows/s
        self._rate_at: float = 0.0
        self._rate_id: int = 0
        self._last_pass_log = 0.0
        self.last_error: Optional[str] = None

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> "FastLaneWorker":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(
            target=self._run, name="blm-fast-lane", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        # build the provider HERE, on this thread, so its Playwright objects
        # are bound to this thread for the life of the lane.  The balance read
        # that follows is the proof the browser connection works from this
        # thread — the same transport a submit will use.
        try:
            p = self._provider()
            from blm_v4 import trade_window as _tw
            self._log("fast_lane_ready",
                      provider=getattr(p, "name", type(p).__name__),
                      balance=self._balance(),
                      pace_rung=getattr(_tw, "TRADE_MIN_PACE_RATIO", None))
        except Exception as e:
            self._warn("fast_lane_provider_wire_failed",
                       error=str(e)[:200])
        while not self._stop.wait(self.interval_s):
            try:
                self.pass_once()
                self.last_error = None
            except Exception as e:  # the lane NEVER dies noisy
                self.last_error = f"{type(e).__name__}: {e}"[:300]
                self._warn("fast_lane_error", error=self.last_error)

    # ── read-only references ─────────────────────────────────────────
    @staticmethod
    def _ro(path: Path) -> sqlite3.Connection:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _slug_for(self, source_game_id: str) -> Optional[str]:
        """competition_slug for a game (split ids resolve to the base game)."""
        base = source_game_id.split("#")[0]
        if base in self._slugs:
            return self._slugs[base]
        now = time.monotonic()
        if now - self._slugs_at > 300.0:
            try:
                conn = self._ro(self._prod_db)
                try:
                    for r in conn.execute(
                            "SELECT source_game_id, competition_slug, "
                            "       classification FROM games "
                            "WHERE competition_slug IS NOT NULL "
                            "AND competition_slug <> ''"):
                        self._slugs[r["source_game_id"]] = r["competition_slug"]
                        self._cls[r["source_game_id"]] = r["classification"]
                finally:
                    conn.close()
                self._slugs_at = now
            except Exception as e:
                self._warn("fast_lane_slug_load_failed", error=str(e)[:200])
        return self._slugs.get(base)

    def _pace_reference(self) -> dict:
        """{slug: {"avg_pace": float, "games": int}} — the SAME function the
        served payload uses, refreshed on its own clock."""
        now = time.monotonic()
        if self._pace and now - self._pace_at < PACE_REF_INTERVAL_S:
            return self._pace
        try:
            from blm_v4.live_analytics.competition_pace import (
                competition_pace_reference,
            )
            conn = self._ro(self._prod_db)
            try:
                ref = competition_pace_reference(conn)
            finally:
                conn.close()
            if ref:
                self._pace = ref
                self._pace_at = now
        except Exception as e:
            self._warn("fast_lane_pace_ref_failed", error=str(e)[:200])
        return self._pace

    def _q3_evidence(self, base: str) -> dict:
        """The game's OWN third-quarter evidence, exactly as the served
        payload carries it — so the strength×fingerprint gate can evaluate
        C3/C5/R2 instead of reading Q3 as UNAVAILABLE on every candidate.

        Two authorities, both imported rather than reimplemented (directive
        2026-11-15 §6):

          * ``api._q3_pace_reference()`` — the per-competition average Q3
            pace, served from the in-process ``PaceReferenceWorker``'s last
            completed scan (``{}`` while it warms up, never an inline scan:
            that would read the whole 115 GB archive on this thread);
          * ``api._q3_game_ppm()`` — the Q3 segment rule (cumulative score at
            the '3rd Quarter' end minus the '2nd Quarter' end over the
            classification's quarter minutes) over this game's own snapshot
            rows, read through ``idx_snapshots_game_ts``.

        Missing operands stay ``None``: the gate then treats Q3 as
        UNAVAILABLE and its own directive branch decides, so this can only
        ever ADD proof, never invent one.  Callers must not read a ``None``
        here as FALSE.
        """
        out: dict = {"q3_ppm": None, "league_q3_avg": None}
        try:
            from blm_v4.api import _q3_game_ppm, _q3_pace_reference
        except Exception as e:                        # import cycle / absence
            self._warn("fast_lane_q3_import_failed", error=str(e)[:200])
            return out
        try:
            slug = self._slug_for(base)
            ref = _q3_pace_reference() if slug else {}
            out["league_q3_avg"] = (ref.get(slug) or {}).get("avg_q3_pace")
        except Exception as e:
            self._warn("fast_lane_q3_ref_failed", error=str(e)[:200])
        try:
            conn = self._ro(self._prod_db)
            try:
                # Range bounds, not ``= ? OR LIKE ?||'#%'``: the OR form is
                # planned as a SCAN of the whole archive (verified on this DB),
                # the range uses idx_snapshots_source_ts and touches only this
                # game's rows.  The suffix match is then made exact in Python,
                # so a longer id sharing the prefix can never contaminate it.
                rows = conn.execute(
                    "SELECT source_game_id, period_label, home_score, "
                    "       away_score "
                    "  FROM snapshots "
                    " WHERE source_game_id >= ? AND source_game_id <= ? "
                    "   AND period_label IN ('2nd Quarter', '3rd Quarter') "
                    " ORDER BY captured_at", (base, base + "\uffff")).fetchall()
            finally:
                conn.close()
            keep = [r for r in rows
                    if r[0] == base or str(r[0]).startswith(base + "#")]
            # _q3_game_ppm reads dict keys; sqlite3.Row has no .get()
            out["q3_ppm"] = _q3_game_ppm(
                {"classification": self._cls.get(base)}, [dict(r) for r in keep])
        except Exception as e:
            self._warn("fast_lane_q3_segment_failed", error=str(e)[:200])
        return out

    # ── the fresh rows ───────────────────────────────────────────────
    def fresh_rows(self) -> list:
        """Captured rows from the last ``lookback_s`` that are inside the
        trade band, newest first — the whole point of the lane is that these
        are seconds old, not minutes.

        BOUNDED BY ID, not by ``captured_at``: the only index carrying time is
        ``(source_game_id, captured_at)``, so a plain ``captured_at >=`` filter
        falls back to a whole-table scan through ``idx_clean_obs_status``
        (measured 2.8 s per pass on prod).  Row ids are append-only, so
        ``id >= max_id - window`` is the same recent set for 15 ms.  The
        ``captured_at`` bound is still applied — the id window only makes it
        cheap, it never widens the result.
        """
        since = _iso(time.time() - self.lookback_s)
        conn = self._ro(self._clean_db)
        try:
            max_id = conn.execute(
                "SELECT MAX(id) FROM clean_observations").fetchone()[0] or 0
            # adaptive window: 3x the measured insert rate over the lookback,
            # never below 2,000 rows (the first pass has no rate yet)
            now = time.monotonic()
            if self._rate_at and max_id > self._rate_id:
                span = now - self._rate_at
                if span > 0:
                    self._rate = (max_id - self._rate_id) / span
            self._rate_at, self._rate_id = now, max_id
            window = int(max(2000.0, (self._rate or 0.0) * self.lookback_s * 3))
            return list(conn.execute(
                """SELECT source_game_id, captured_at, total_points,
                          progress_pct, required_pts_per_min,
                          actual_pts_per_min, live_total_line, under_price,
                          market_captured_at, status, terminal
                     FROM clean_observations
                    WHERE id >= ?
                      AND captured_at >= ?
                      AND status = 'VALID'
                      AND (terminal IS NULL OR terminal = 0)
                      AND progress_pct >= ? AND progress_pct <= 92
                      AND total_points >= 70
                      AND required_pts_per_min IS NOT NULL
                      AND actual_pts_per_min IS NOT NULL
                      AND under_price IS NOT NULL
                    ORDER BY captured_at DESC
                    LIMIT 400""", (max_id - window, since,
                                    int(EXEC_MIN_PROGRESS_PCT))))
        finally:
            conn.close()

    def build_game(self, row, slug: Optional[str], avg_pace, now: float,
                   attempt: int = 0, fingerprint: Optional[dict] = None) -> dict:
        """The executor's input contract, built from one captured row.

        ONLY the fields ``evaluate`` reads are populated.  Everything that
        decides the trade is re-read by the executor from this dict, so a
        wrong number here can never widen a gate: it can only send the
        executor down its own refusal path."""
        line = float(row["live_total_line"])
        price = float(row["under_price"])
        checkpoint = int(EXEC_MIN_PROGRESS_PCT)
        return {
            "game_id": row["source_game_id"].split("#")[0],
            "competition_slug": slug,
            "live": True,
            "live_reason": None,
            "projector": {
                "captured_at": row["captured_at"],
                "required_pts_per_min": float(row["required_pts_per_min"]),
                "actual_pts_per_min": float(row["actual_pts_per_min"]),
                "progress_pct": float(row["progress_pct"]),
                "current_total_points": float(row["total_points"]),
            },
            "market": {
                "total_line": line,
                "under_odds": price,
                "total_line_at": row["market_captured_at"],
            },
            # the alert the executor requires: it must carry the checkpoint
            # id and the frozen trigger line, and claim to be active — the
            # legs below are what actually earned that claim.
            "under_alert": {"active": True, "checkpoint": checkpoint,
                            "trigger_line": line},
            "under_alert_exec_armed": True,
            "under_alert_eligibility": {"eligible": True, "reason": None},
            # 0 = the normally-fresh path; >0 = a stale-line retry, which the
            # executor records under its own ``#rN`` identity
            "under_alert_attempt": int(attempt or 0),
            # the competition's league-average pace, for the strength tier of
            # the auto-bet qualification gate (directive 2026-11-15).
            "competition_pace": {"avg_pace": avg_pace},
            # Q3 evidence for the fingerprint half of that gate: the SAME
            # block shape the served payload carries, built from this game's
            # own snapshot segment and the competition's Q3 reference.  It is
            # present but empty when either operand is unprovable, so the
            # gate's ``q3_ratio is None`` branch (= UNAVAILABLE) applies
            # rather than being silently assumed.  Without this block a
            # Tier-B candidate could never be confirmed — every fast-lane
            # candidate would pass on the base rate.
            "under_alert_fingerprint": dict(fingerprint or {}),
            "under_alert_competition": {"slug": slug, "known": True,
                                        "reason": None},
            "under_alert_line_move": {"entry_line": line, "open_line": None,
                                      "league_avg_line": None,
                                      "move_norm": None, "reason": None},
        }

    def _fingerprint_block(self, row, avg) -> Optional[dict]:
        """The ``under_alert_fingerprint`` block for one captured row.

        Same operands, same evaluation function and therefore the same
        three-state verdicts as the served payload — C3/C5/R2 can be TRUE here
        now that the Q3 segment is supplied.  ``None`` (no block) only when the
        competition pace is unusable, which the gate then refuses as
        MISSING_INPUT, exactly as before.
        """
        if not isinstance(avg, (int, float)) or avg <= 0:
            return None
        base = str(row["source_game_id"]).split("#")[0]
        q3 = self._q3_evidence(base)
        block = evaluate_fingerprints(
            row["required_pts_per_min"], avg, q3["q3_ppm"],
            q3["league_q3_avg"], None, row["actual_pts_per_min"])
        # the pace authority ``inputs_from_game`` reads first, so the gate and
        # the audit record use the ratio this lane actually computed
        block["league_average_pace"] = avg
        return block

    # ── one pass ─────────────────────────────────────────────────────
    def pass_once(self) -> dict:
        summary = {"rows": 0, "games": 0, "armed": 0, "executed": 0,
                   "would_bet": 0, "refused": {}}
        # Operator directive 2026-11-19: a switched-OFF Auto-Bet must still
        # LIST the games that clear the trade legs — "off" is a fact about
        # execution, not about qualification.  The lane keeps evaluating the
        # legs while off and records each qualified game with reason
        # ``autobet_off``; the execution path is never reached, so nothing can
        # be priced, claimed or submitted.
        enabled = self.store.is_enabled()
        summary["autobet_off"] = not enabled
        rows = self.fresh_rows()
        summary["rows"] = len(rows)
        if not rows:
            # heartbeat: an empty slate and a dead lane look identical in the
            # journal otherwise, and that ambiguity is what hid the original
            # stall.  Throttled inside _log_pass.
            self._log_pass(summary)
            return summary
        pace_ref = self._pace_reference()
        stats = self.store.today_stats()
        unit_price = self.store.get_unit_price()
        balance = self._balance()
        summary["balance"] = (round(balance, 2)
                               if isinstance(balance, (int, float))
                               else None)
        try:
            from blm_v4.betting.api import set_balance as _set_balance
            _set_balance(balance)
        except Exception:
            pass
        now = time.time()
        # GLOBAL leg, evaluated once per pass: while unders are being beaten
        # market-wide every per-game leg can still pass on a game whose edge
        # is gone.  Cached inside the module, so this costs a read every 2 min.
        heat = market_heat.refusal(self._prod_db, self._clean_db, now)
        summary["market_heat"] = bool(heat)
        seen: set[str] = set()
        for row in rows:
            base = row["source_game_id"].split("#")[0]
            if base in seen:
                continue          # newest row per game only
            seen.add(base)
            summary["games"] += 1
            slug = self._slug_for(base)
            avg = (pace_ref.get(slug) or {}).get("avg_pace")
            leg_map = legs(
                required_pace=row["required_pts_per_min"], avg_pace=avg,
                actual_pace=row["actual_pts_per_min"],
                progress_pct=row["progress_pct"],
                total_points=row["total_points"],
                under_odds=row["under_price"],
                competition_slug=slug,
                captured_at=row["captured_at"],
                market_captured_at=row["market_captured_at"])
            refusal = heat or first_refusal(leg_map)
            market_age = None
            cap = _epoch(row["market_captured_at"])
            if cap is not None:
                market_age = now - cap
            stale = (refusal is None
                     and (market_age is None or market_age > MAX_MARKET_AGE_S))
            attempt = 0
            if stale and enabled:
                # operator directive 2026-10-28: a stale captured line is not
                # refused outright — retry it up to N times and let the book's
                # live betslip be the authority on line and price.  This is an
                # EXECUTION rule: while Auto-Bet is OFF the retry machinery is
                # skipped, so a stale market neither spends an attempt nor
                # hides the qualification.
                attempt, refusal = self._stale_attempt(base)
            if refusal is not None:
                key = refusal.split(":")[0]
                summary["refused"][key] = summary["refused"].get(key, 0) + 1
                continue
            summary["armed"] += 1
            if attempt:
                summary["retries"] = summary.get("retries", 0) + 1
            try:
                if self.store.has_placed_bet(base):
                    self._record_candidate(row, base, avg, attempt,
                                           outcome="NOT_PLACED",
                                           reason="already_placed")
                    continue
            except Exception:
                continue
            if not enabled:
                # Qualification is LISTED while OFF; execution never runs.
                self._record_candidate(row, base, avg, attempt,
                                       outcome="NOT_PLACED",
                                       reason="autobet_off")
                continue
            game = self.build_game(
                row, slug, avg, now, attempt=attempt,
                fingerprint=self._fingerprint_block(row, avg))
            # ── STRENGTH × FINGERPRINT QUALIFICATION (directive 2026-11-15) ──
            # The payload now carries the game's real Q3 segment, so Tier B is
            # decided by C3/C5/R2 rather than falling through the "Q3
            # unavailable" branch on every candidate: a Tier-B game whose Q3
            # is on record and fires nothing is REFUSED here exactly as the
            # poll worker refuses it.  Q3 that is genuinely unprovable (no 2nd/
            # 3rd-Quarter rows yet, no league reference) still reads
            # UNAVAILABLE and keeps the directive's base-rate branch.
            # Refused candidates never claim a slot.
            qualification = _qualify.check_game(game)
            if not qualification["qualified"]:
                key = f"autobet_gate:{qualification['reason']}"
                summary["refused"][key] = summary["refused"].get(key, 0) + 1
                self._record_candidate(row, base, avg, attempt,
                                       outcome="NOT_PLACED", reason=key)
                continue
            bal = balance if isinstance(balance, (int, float)) else None
            res = evaluate(
                game, cfg=self.cfg, store=self.store, enabled=True,
                unit_price=unit_price, stats=stats, balance=bal,
                game_enabled=self.store.is_game_enabled(base))
            if res["decision"] == "NO_BET":
                key = str(res.get("reason") or "unspecified")
                summary["refused"][key] = summary["refused"].get(key, 0) + 1
                self._record_candidate(row, base, avg, attempt,
                                       outcome="NOT_PLACED", reason=key)
                continue
            if res["decision"] == "WOULD_BET":       # dry run only
                summary["would_bet"] += 1
                cand = res["candidate"]
                # mirror the poll worker: record the outcome so the
                # dashboard's execution list shows the dry-run pass
                self.store.update_status(
                    cand["execution_id"], "ACCEPTED",
                    provider_ref=f"dryrun-{cand['execution_id']}",
                    error_message="DRY_RUN — no real bet submitted",
                    simulated_amount=cand["stake_amount"],
                    execution_state="WOULD_BET")
                self._log("fast_lane_would_bet",
                          game_id=base, line=row["live_total_line"],
                          price=row["under_price"],
                          progress=row["progress_pct"])
                stats = self.store.today_stats()
                continue
            # pin the pre-submit balance BEFORE the click: it is the only
            # book-side evidence a SUBMITTED-without-receipt outcome can be
            # judged by later (see reconcile_evidence.py)
            self.store.set_balance_before(res["candidate"]["execution_id"], bal)
            out = execute(res["candidate"], cfg=self.cfg,
                          store=self.store, provider=self._provider())
            placed = out.get("status") in ("ACCEPTED", "SUBMITTED")
            if placed:
                summary["executed"] += 1
            _cand = res["candidate"]
            self._record_candidate(
                row, base, avg, attempt,
                outcome="PLACED" if placed else "NOT_PLACED",
                reason=None if placed else (out.get("error_code")
                                            or (out.get("error_message") or "")[:160]
                                            or out.get("status")),
                checkpoint=_cand.get("checkpoint"),
                execution_id=_cand.get("execution_id"),
                execution_state=out.get("status"),
                stake_units=_cand.get("stake_units"),
                stake_amount=_cand.get("stake_amount"))
            self._log("fast_lane_execute", game_id=base,
                      line=row["live_total_line"], price=row["under_price"],
                      progress=row["progress_pct"], retry=attempt,
                      status=out.get("status"),
                      error_code=out.get("error_code"))
            stats = self.store.today_stats()
        # ── market-gap monitor (diagnostic, own clock, never a gate) ──
        nowm = time.monotonic()
        if (nowm - self._gap_at) >= self.gap_interval_s:
            self._gap_at = nowm
            summary["gaps"] = self._market_gap_check(sorted(seen))
        self._log_pass(summary)
        return summary

    # ── helpers ──────────────────────────────────────────────────────
    def _market_gap_check(self, game_ids: list) -> int:
        """How many of these games have a WS-vs-line market gap right now.

        DIAGNOSTIC ONLY, it decides nothing.  For each game it compares the
        age of the last WS ``MatchTotal`` row (market_observations) with the
        age of the last full-game ``Total Points`` row (betual_line_observations).
        When the line feed is more than ``gap_threshold_s`` fresher, the book
        has re-listed the total under a new ``market_id`` and one transport
        saw it while the other did not — the shape found on 2026-10-28 (event
        31381886: total absent 12:25:36-12:37:09, re-listed with a new id).

        Without this the only evidence is a stale capture, which cannot tell
        "the book pulled the market" from "we stopped listening"."""
        if not game_ids:
            return 0
        now = time.time()
        found = 0
        try:
            conn = self._ro(self._prod_db)
        except Exception:
            return 0
        try:
            for gid in game_ids:
                ws = conn.execute(
                    "SELECT captured_at FROM market_observations "
                    "WHERE source_game_id=? AND market_type='MatchTotal' "
                    "ORDER BY captured_at DESC LIMIT 1", (gid,)).fetchone()
                ln = conn.execute(
                    "SELECT captured_at FROM betual_line_observations "
                    "WHERE source_game_id=? AND market_name='Total Points' "
                    "ORDER BY captured_at DESC LIMIT 1", (gid,)).fetchone()
                ln_at = _epoch(ln["captured_at"]) if ln else None
                if ln_at is None:
                    continue          # no line feed for this event: not a gap
                ws_at = _epoch(ws["captured_at"]) if ws else None
                ws_age = None if ws_at is None else now - ws_at
                ln_age = now - ln_at
                if ws_age is not None and \
                        (ws_age - ln_age) <= self.gap_threshold_s:
                    continue
                last = self._gap_logged.get(gid)
                if last is not None and (now - last) < 600.0:
                    continue          # one line per game per ten minutes
                self._gap_logged[gid] = now
                found += 1
                self._log("market_gap", game_id=gid,
                          ws_age_s=None if ws_age is None else round(ws_age),
                          line_age_s=round(ln_age),
                          ws_last=(ws["captured_at"] if ws else None),
                          line_last=ln["captured_at"])
        except Exception as e:
            self._warn("market_gap_check_failed", error=str(e)[:200])
        finally:
            conn.close()
        return found

    def _stale_attempt(self, game_id: str) -> tuple:
        """The operator's stale-line rule (2026-10-28): up to N attempts.

        A stale captured market is not refused outright, because the book's
        LIVE betslip is the authority on line and price and the provider
        verifies both before it submits.  Each attempt is its own ledger row
        (``#rN`` idempotency key).  Two hard stops:
          * an AMBIGUOUS attempt means money may already have moved — the
            operator's rule is that it is never retried, so the game's
            retries end there;
          * the cap is counted from the ledger, so a restart cannot reset it.
        Returns ``(attempt_number, refusal_or_None)``.
        """
        try:
            if self.store.ambiguous_today(game_id):
                return 0, "eligibility:ambiguous_hold"
            used = self.store.attempts_today(game_id)
        except Exception:
            return 0, "eligibility:attempts_unverifiable"
        if used >= self.stale_attempts:
            return 0, "eligibility:market_stale_exhausted"
        # space the attempts: a stale row repeats every pass, and three DOM
        # round-trips inside nine seconds is hammering, not retrying
        last = self._last_attempt_at.get(game_id)
        if last is not None and (time.monotonic() - last) < self.retry_gap_s:
            return 0, "eligibility:retry_backoff"
        self._last_attempt_at[game_id] = time.monotonic()
        return used + 1, None

    def _provider(self):
        """The provider, built on first use in the calling thread."""
        if self.provider is None:
            from blm_v4.betting.provider import provider_from_config
            self.provider = provider_from_config(self.cfg)
        return self.provider

    # ── qualifying-bet ledger (operator directive 2026-11-02) ─────────
    @staticmethod
    def _progress_band(progress) -> int:
        """The 5-point progress band a candidate belongs to (75, 80, 85, 90)."""
        try:
            return int(float(progress) // 5 * 5)
        except (TypeError, ValueError):
            return -1

    def _record_candidate(self, row, game_id: str, avg_pace, attempt,
                          *, outcome: str, reason=None, checkpoint=None,
                          execution_id=None, execution_state=None,
                          stake_units=None, stake_amount=None) -> None:
        """Write ONE qualifying-bet row — placed or NOT placed.

        The operator's directive (2026-11-02): every bet the gate qualifies
        must be visible in the UI whether or not a slip was placed, because
        "no bet" and "bet the gate refused" are different facts and the
        execution ledger only ever holds the first.  This writes NOTHING to
        ``bet_executions``, claims no idempotency key and can never authorise
        a submission; it is a record of the decision the lane actually took.
        """
        req = row["required_pts_per_min"]
        act = row["actual_pts_per_min"]
        ratio = (req / avg_pace) if avg_pace else None
        self.store.record_candidate(dict(
            dedupe_key=(f"{game_id}:{self._progress_band(row['progress_pct'])}"
                        f":{int(attempt or 0)}"),
            at_utc=str(row["captured_at"]).replace(" ", "T"),
            game_id=game_id,
            checkpoint=checkpoint,
            line=row["live_total_line"],
            price=row["under_price"],
            progress_pct=row["progress_pct"],
            total_points=row["total_points"],
            required_pace=req,
            actual_pace=act,
            pace_ratio=ratio,
            shortfall=(req - act) if (req is not None and act is not None) else None,
            attempt=int(attempt or 0),
            outcome=outcome,
            reason=reason,
            execution_id=execution_id,
            execution_state=execution_state,
            stake_units=stake_units,
            stake_amount=stake_amount,
        ))

    def balance(self):
        """The provider balance this lane can read (None when unreadable)."""
        return self._balance()

    def _balance(self):
        try:
            fn = getattr(self._provider(), "account_balance", None)
        except Exception:
            return None
        if not callable(fn):
            return None
        try:
            return fn()
        except Exception:
            return None

    def _log_pass(self, summary: dict) -> None:
        """Silent unless something was armed or executed; otherwise at most
        one line a minute, so the lane cannot flood the journal."""
        now = time.monotonic()
        interesting = bool(summary.get("armed") or summary.get("executed")
                           or summary.get("would_bet") or summary.get("gaps"))
        if not interesting and (now - self._last_pass_log) < 60.0:
            return
        self._last_pass_log = now
        refused = summary.get("refused") or {}
        top = ", ".join(f"{k}={v}" for k, v in
                        sorted(refused.items(), key=lambda kv: -kv[1])[:5])
        self._log("fast_lane_pass", rows=summary.get("rows"),
                  games=summary.get("games"), armed=summary.get("armed"),
                  executed=summary.get("executed"),
                  would_bet=summary.get("would_bet"),
                  gaps=summary.get("gaps"),
                  balance=summary.get("balance"),
                  refused_top=top or None)
