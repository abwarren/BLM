"""
BLM V4 — SCHEDULED RESULTS VERIFICATION (reconciliation audit).

WHY THIS EXISTS
---------------
The pipeline RECORDS a final for every finished game (``game_results``):
the live DOM while the market is up, the PokerBet results page / its
backing swarm feed when it disappears.  Nothing has ever AUDITED those
recorded finals against the source *after the fact*.  A drifted, stale or
mistaken verdict therefore sits in the DB forever while every downstream
reader (win rate, scorecard, operator dashboard) treats it as truth.

This module is the missing VERIFICATION pass: on a schedule it re-reads
the finished games' finals from the PokerBet results page — i.e. the
SAME authoritative source the recorded verdict came from — and reports
every recorded final that DISAGREES with it.

THE "PAGE'S FINALS" SOURCE
--------------------------
https://www.pokerbet.co.za/en/sports/results is a React SPA whose rows are
NOT in the HTML: the page renders rows fetched over

    wss://eu-swarm-newm.pokerbet.co.za/        (``get_result_games``)

so the feed IS the page's own data layer.  Two facts make the feed the
correct source for a verifier and the DOM the wrong one:

  * the DOM rows carry NO game id at all — identity can only be guessed
    from team names + a footer time, which is exactly how the pre-2026-09-24
    code attached foreign fixtures (739 impossible finals measured);
  * the feed answers ``get_result_games{game_id}`` by game id EXACTLY and
    returns a structured four-quarter score line.

The verifier is therefore built around a pluggable :class:`FinalsSource`;
the shipped default reads the feed (the page's own source) and a
DOM-rows source (reusing the repo's proven row matcher) is available as an
alternative.  Both are READ-ONLY.

DEFAULT = REPORT-ONLY
---------------------
By default the verifier WRITES NO ROWS.  It produces a report — a JSON
file under ``BLM_DATA_ROOT`` plus a log line — listing, per mismatch:
``(source_game_id, teams, DB total, page total, delta, result_at)``.  A
correction mode exists but is OFF behind an explicit flag
(``RECONCILE_VERIFY_APPLY=false`` by default); the operator turns it on
deliberately.

BOUNDARY (do not cross)
-----------------------
This module does NOT import, call, subclass or modify the existing
``result_reconciler`` — its authority / immutability contract and its
OK/UNKNOWN writers are untouched.  The verifier only READS ``game_results``
and, in the (opt-in) apply mode, corrects the recorded FINALS of rows that
already hold one; it never fabricates a final and never touches a row
without one.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

logger = logging.getLogger("blm_v4.result_verifier")

# ── env-configurable knobs (names pinned by the directive) ────────────
#: How many days back (on ``result_at``) to verify.  Operator-settable.
DEFAULT_WINDOW_DAYS = 3
#: Worker cadence, seconds.  Operator-settable.
DEFAULT_INTERVAL_S = 3600.0
#: Report file name written under BLM_DATA_ROOT.
REPORT_FILENAME = "reconcile_verify_report.json"
#: provenance stamped on a corrected row in apply mode — the same
#: vocabulary the rest of the pipeline uses for a page-derived final.
CORRECTION_SOURCE = "RESULTS_PAGE"
#: the results page the feed backs (documented provenance for reports).
RESULTS_PAGE_URL = "https://www.pokerbet.co.za/en/sports/results"


def window_days_from_env() -> int:
    try:
        return max(1, int(os.environ.get("RECONCILE_VERIFY_DAYS",
                                         str(DEFAULT_WINDOW_DAYS))))
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_DAYS


def interval_from_env() -> float:
    try:
        return float(os.environ.get("RECONCILE_VERIFY_INTERVAL_S",
                                    str(DEFAULT_INTERVAL_S)))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S


def apply_from_env() -> bool:
    """Correction mode flag.  DEFAULTS OFF — only an explicit truthy value
    enables writes.  Absent / '0' / 'false' / 'no' / '' all mean OFF."""
    return os.environ.get("RECONCILE_VERIFY_APPLY", "").strip().lower() \
        in ("1", "true", "yes", "on")


def report_path(data_root: Path | str) -> Path:
    return Path(data_root) / REPORT_FILENAME


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _cutoff_iso(window_days: int) -> str:
    """The window's lower bound in the SAME stamp format ``result_at`` uses
    (ISO with a 'T'), so a string comparison is correct — the documented
    'T'-vs-space trap is avoided by building both sides identically."""
    return (datetime.now(timezone.utc)
            - timedelta(days=int(window_days))).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ")


def _date_from(day: str) -> "datetime.date":
    """'YYYY-MM-DD' (or an ISO stamp's prefix) -> a date."""
    return datetime.strptime(str(day)[:10], "%Y-%m-%d").date()


# ── the comparison (pure — unit-testable without any DB or network) ───

def compare_final(db: dict, page: Optional[dict]) -> Optional[dict]:
    """Compare one recorded final against its page final.

    Returns ``None`` when the page AGREES, or when the page has NO final
    for this game (an ABSENCE is not a disagreement — reported separately
    by the caller).  Otherwise returns the mismatch record with exactly the
    fields the report contract names plus the per-side detail:

        {"source_game_id", "home_team", "away_team",
         "db_total", "page_total", "delta", "result_at",
         "db_home", "db_away", "page_home", "page_away", "status",
         "classification", "teams_match"}
    """
    if page is None:
        return None
    db_h = db.get("final_home")
    db_a = db.get("final_away")
    pg_h = page.get("home_score")
    pg_a = page.get("away_score")
    if pg_h is None or pg_a is None:
        # a page entry WITHOUT a usable score cannot disagree — it is a
        # non-observation, never a mismatch (fail closed: not a verdict)
        return None
    db_total = db.get("final_total")
    if db_total is None and db_h is not None and db_a is not None:
        db_total = int(db_h) + int(db_a)
    pg_total = page.get("final_total")
    if pg_total is None:
        pg_total = int(pg_h) + int(pg_a)
    if (int(db_h), int(db_a)) == (int(pg_h), int(pg_a)):
        return None
    return {
        "source_game_id": db.get("source_game_id"),
        "home_team": db.get("home_team"),
        "away_team": db.get("away_team"),
        "db_total": db_total,
        "page_total": pg_total,
        "delta": int(pg_total) - int(db_total),
        "result_at": db.get("result_at"),
        "db_home": db_h,
        "db_away": db_a,
        "page_home": pg_h,
        "page_away": pg_a,
        "status": db.get("final_result_status"),
        "classification": db.get("classification"),
        "teams_match": _teams_match(db, page),
        # Fixture-instance context (informational).  The feed is keyed by
        # game_id, but a virtual league reuses one id for a slot replayed
        # all day — when the page's fixture START is inconsistent with this
        # record's own observation window, the feed may be answering a
        # DIFFERENT instance.  Surfaced so the operator can triage a
        # mismatch as a genuine drift vs a replay-slot answer.
        "page_start": page.get("start_time"),
        "db_last_seen": db.get("last_seen_at"),
        "start_time_consistent": _start_time_consistent(db, page),
    }


def _start_time_consistent(db: dict, page: dict) -> Optional[bool]:
    """The reconciler's OWN fixture-start rule (reused, not re-implemented):
    is the page fixture's start inside this record's observation window?
    None when either side carries no time (check inapplicable)."""
    try:
        from blm_v4.result_reconciler import _fixture_start_consistent
        return _fixture_start_consistent(
            page.get("start_time"), db.get("first_seen_at"),
            db.get("last_seen_at"), db.get("classification"))
    except Exception:
        return None


def _slug(name: Optional[str]) -> str:
    if not name:
        return ""
    return "".join(ch for ch in str(name).strip().lower()
                   if ch.isalnum())


def _teams_match(db: dict, page: dict) -> Optional[bool]:
    """Informational only: the feed is keyed by game id, so a name mismatch
    never invalidates the comparison — it is surfaced so an operator can
    spot a replay-slot collision.  None when a name is unavailable."""
    dh, da = _slug(db.get("home_team")), _slug(db.get("away_team"))
    ph, pa = _slug(page.get("home_team")), _slug(page.get("away_team"))
    if not (dh and da and ph and pa):
        return None
    def _pair(a: str, b: str) -> bool:
        if a == b:
            return True
        s, l = (a, b) if len(a) <= len(b) else (b, a)
        return len(s) >= 4 and s in l
    return (_pair(dh, ph) and _pair(da, pa)) \
        or (_pair(dh, pa) and _pair(da, ph))


# ── the report (pure builder — the shape tests assert on) ─────────────

def build_report(*, generated_at: str, window_days: int, apply: bool,
                 source: str, reachable: bool, games_checked: int,
                 mismatches: list[dict], unmatched: list[dict],
                 errors: Optional[list[str]] = None) -> dict:
    """Assemble the verification report payload."""
    return {
        "generated_at": generated_at,
        "results_page": RESULTS_PAGE_URL,
        "source": source,
        "reachable": bool(reachable),
        "apply": bool(apply),
        "window_days": int(window_days),
        "games_checked": int(games_checked),
        "mismatch_count": len(mismatches),
        "unmatched_count": len(unmatched),
        "mismatches": list(mismatches),
        "unmatched": list(unmatched),
        "errors": list(errors or []),
    }


# ── page-finals sources (READ-ONLY) ───────────────────────────────────

class FinalsSource(Protocol):
    """A read-only source of finished-game finals keyed by game id."""

    name: str

    def fetch_finals(self, records: list[dict]) -> dict[str, Optional[dict]]:
        ...


class SwarmFinalsSource:
    """DEFAULT: the swarm feed that BACKS the results page.

    The page renders this feed; the feed answers by ``game_id`` exactly and
    returns the structured four-quarter score line.  A whole DAY costs ONE
    round trip (``get_result_games`` bulk, measured ~1000+ results per
    frame), so the verifier pulls one frame per day in the window instead of
    one request per game — a 3-day window is ~5 frames, not ~2000 calls.
    A missing entry is an ABSENCE (``None``), never a failure; a pass in
    which NO day could be fetched raises, so the verifier reports the source
    unreachable rather than silently marking every game unmatched.
    """

    name = "swarm_feed"

    def __init__(self, client: Any = None, *, sport_ids: tuple = (3,),
                 log: Any = None):
        self._client = client
        self._sport_ids = tuple(sport_ids or (3,))
        self._log = log or logger

    def _ensure(self):
        if self._client is None:
            from blm_v4.swarm_results import SwarmResultsClient
            self._client = SwarmResultsClient(log=self._log)
        return self._client

    def fetch_finals(self, records: list[dict]) -> dict[str, Optional[dict]]:
        days = sorted({str(r.get("result_at"))[:10] for r in records
                       if r.get("result_at")})
        if not days:
            return {}
        # widen by a day each side: the feed indexes by FIXTURE date, which
        # can precede the recording stamp (result_at) across midnight UTC.
        lo = _date_from(days[0]) - timedelta(days=1)
        hi = _date_from(days[-1]) + timedelta(days=1)
        client = self._ensure()
        index: dict[str, dict] = {}
        ok_days = 0
        day = lo
        while day <= hi:
            for sid in self._sport_ids:
                try:
                    for g in client.fetch_results_day(day.isoformat(),
                                                      sport_id=sid) or []:
                        gid = str(g.get("game_id") or "")
                        if gid:
                            index[gid] = g
                    ok_days += 1
                except Exception:
                    self._log.warning("swarm_day_fetch_failed day=%s sport=%s",
                                      day.isoformat(), sid)
            day += timedelta(days=1)
        if ok_days == 0:
            raise RuntimeError("swarm feed returned no day for the window")
        out: dict[str, Optional[dict]] = {}
        for rec in records:
            gid = str(rec.get("source_game_id") or "")
            base = gid.split("#")[0]
            out[gid] = index.get(gid) or index.get(base)
        return out

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass


class ResultsPageFinalsSource:
    """ALTERNATIVE: parse the rendered results page's rows.

    Reuses the repo's OWN fetcher (``PlaywrightResultsFetcher``) and row
    matcher (``result_reconciler.select_matching_row``) — no second copy of
    the identity logic.  The page carries no game id, so identity comes from
    the row's teams + fixture time, exactly as the reconciler decides it.
    Never the default: the DOM path is the weaker proof (see the module
    docstring).
    """

    name = "results_page_dom"

    def __init__(self, fetcher: Any = None, *, log: Any = None):
        self._fetcher = fetcher
        self._log = log or logger

    def _ensure(self):
        if self._fetcher is None:
            from blm_v4.results_fetcher import PlaywrightResultsFetcher
            self._fetcher = PlaywrightResultsFetcher(log=self._log)
        return self._fetcher

    def fetch_finals(self, records: list[dict]) -> dict[str, Optional[dict]]:
        from blm_v4.result_reconciler import select_matching_row
        fetcher = self._ensure()
        out: dict[str, Optional[dict]] = {}
        for rec in records:
            gid = str(rec.get("source_game_id") or "")
            if not gid:
                continue
            try:
                rows = fetcher.fetch_results_rows(gid)
            except Exception:
                rows = None
            if not rows:
                out[gid] = None
                continue
            match = select_matching_row([dict(r) for r in rows], rec)
            out[gid] = match.get("parsed") if match.get("matched") else None
        return out

    def close(self) -> None:
        if self._fetcher is not None:
            try:
                self._fetcher.close()
            except Exception:
                pass


# ── the verifier ──────────────────────────────────────────────────────

class ResultVerifier:
    """One verification pass: DB finals vs the page's finals."""

    def __init__(self, db_path: Path | str, *,
                 source: Optional[FinalsSource] = None,
                 window_days: int = DEFAULT_WINDOW_DAYS,
                 apply: bool = False,
                 data_root: Path | str | None = None,
                 log: Optional[logging.Logger] = None):
        self._db_path = Path(db_path)
        self._source = source or SwarmFinalsSource(log=log)
        self._window_days = max(1, int(window_days))
        self._apply = bool(apply)
        self._data_root = Path(data_root) if data_root else self._db_path.parent
        self._log = log or logger
        self._lock = threading.Lock()

    # -- db -------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def load_db_finals(self, conn: sqlite3.Connection) -> list[dict]:
        """The recorded finals inside the window, with their teams.

        A row is a "recorded final" when BOTH ``final_home`` and
        ``final_away`` are present; its ``result_at`` must fall inside the
        window.  Team names come from ``games`` (base id — the ``#iN`` replay
        suffix is stripped, matching the reconciler's own chain keying).
        """
        cutoff = _cutoff_iso(self._window_days)
        base = ("CASE WHEN instr(r.source_game_id, '#') > 0 THEN "
                "substr(r.source_game_id, 1, instr(r.source_game_id,'#') - 1) "
                "ELSE r.source_game_id END")
        rows = conn.execute(
            f"""
            SELECT r.source_game_id, r.classification,
                   r.final_home, r.final_away, r.final_total, r.result_at,
                   r.final_result_status, r.result_source,
                   g.home_team, g.away_team,
                   g.first_seen_at, g.last_seen_at
              FROM game_results r
              LEFT JOIN games g ON g.source_game_id = {base}
             WHERE r.final_home IS NOT NULL AND r.final_away IS NOT NULL
               AND r.result_at >= ?
             ORDER BY r.result_at DESC
            """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    # -- one pass -------------------------------------------------------
    def run_pass(self) -> dict:
        """Verify every recorded final in the window.  NEVER raises for a
        per-run problem: an unreachable source yields reachable=False and
        the pass still returns a report (the worker must not die)."""
        errors: list[str] = []
        records: list[dict] = []
        try:
            conn = self._connect()
            try:
                records = self.load_db_finals(conn)
            finally:
                conn.close()
        except Exception as exc:                       # DB read failed
            errors.append(f"db_read_failed: {exc}")
            return self._finalise(False, 0, [], [], errors)

        page: dict[str, Optional[dict]] = {}
        reachable = True
        if records:
            try:
                page = self._source.fetch_finals(records) or {}
            except Exception as exc:                   # source unreachable
                reachable = False
                errors.append(f"source_unreachable: {type(exc).__name__}: "
                              f"{str(exc)[:200]}")

        mismatches: list[dict] = []
        unmatched: list[dict] = []
        for rec in records:
            gid = str(rec["source_game_id"])
            if not reachable:
                break
            page_final = page.get(gid)
            if page_final is None:
                unmatched.append({
                    "source_game_id": gid,
                    "home_team": rec.get("home_team"),
                    "away_team": rec.get("away_team"),
                    "db_total": rec.get("final_total"),
                    "result_at": rec.get("result_at"),
                    "reason": "no page final for this game id",
                })
                continue
            mm = compare_final(rec, page_final)
            if mm is not None:
                mismatches.append(mm)

        if self._apply and reachable and mismatches:
            self._apply_corrections(mismatches)

        return self._finalise(reachable, len(records), mismatches,
                              unmatched, errors)

    # -- apply (opt-in) -------------------------------------------------
    def _apply_corrections(self, mismatches: list[dict]) -> None:
        """Correct recorded finals to the page's values.  ONLY rows that
        already hold a final are touched (never fabricated), and only when
        the operator has explicitly enabled apply mode."""
        with self._lock:
            conn = self._connect()
            try:
                for mm in mismatches:
                    conn.execute(
                        """
                        UPDATE game_results
                           SET final_home = ?, final_away = ?,
                               final_total = ?, result_source = ?
                         WHERE source_game_id = ?
                           AND final_home IS NOT NULL
                           AND final_away IS NOT NULL
                        """,
                        (mm["page_home"], mm["page_away"], mm["page_total"],
                         CORRECTION_SOURCE, mm["source_game_id"]))
                    mm["applied"] = True
                conn.commit()
            finally:
                conn.close()

    # -- report ---------------------------------------------------------
    def _finalise(self, reachable: bool, checked: int,
                  mismatches: list[dict], unmatched: list[dict],
                  errors: list[str]) -> dict:
        report = build_report(
            generated_at=_utcnow(), window_days=self._window_days,
            apply=self._apply, source=getattr(self._source, "name", "unknown"),
            reachable=reachable, games_checked=checked,
            mismatches=mismatches, unmatched=unmatched, errors=errors)
        self._write_report(report)
        self._log.info(
            "results_verification checked=%d mismatches=%d unmatched=%d "
            "reachable=%s apply=%s",
            checked, report["mismatch_count"], report["unmatched_count"],
            reachable, self._apply)
        return report

    def _write_report(self, report: dict) -> None:
        try:
            self._data_root.mkdir(parents=True, exist_ok=True)
            p = report_path(self._data_root)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(report, indent=1, default=str))
            tmp.replace(p)
        except Exception:
            self._log.exception("results_verification_report_write_failed")


# ── daemon wrapper (mirrors ResultReconcilerWorker's shape) ───────────

class ResultVerifierWorker:
    """Background verification loop — a daemon sibling of the other
    workers.  Cadence is env-tunable (RECONCILE_VERIFY_INTERVAL_S, default
    3600).  Every tick fails SOFT: a pass that cannot reach the source is
    logged and the next tick retries; the thread never dies."""

    def __init__(self, db_path: Path | str, *,
                 interval_s: float = DEFAULT_INTERVAL_S,
                 window_days: Optional[int] = None,
                 apply: Optional[bool] = None,
                 data_root: Path | str | None = None,
                 source: Optional[FinalsSource] = None,
                 log: Optional[logging.Logger] = None):
        self._log = log or logger
        self._interval_s = float(interval_s)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._verifier = ResultVerifier(
            db_path,
            source=source,
            window_days=(window_days if window_days is not None
                         else window_days_from_env()),
            apply=(apply if apply is not None else apply_from_env()),
            data_root=(data_root or os.environ.get("BLM_DATA_ROOT")
                       or Path(db_path).parent),
            log=self._log)

    @property
    def verifier(self) -> ResultVerifier:
        return self._verifier

    def run_once(self) -> dict:
        return self._verifier.run_pass()

    def loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                self._log.exception("results_verification_pass_failed")
            self._stop.wait(self._interval_s)

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(
            target=self.loop, name="blm-result-verifier", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        try:
            self._verifier._source.close()
        except Exception:
            pass
