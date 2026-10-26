"""Test-only historical rescore → the cohort ledger (strategy doc §4.1).

Builds a derived SQLite file (``cohort_rescore.db``) with ONE row per evaluated
observation, evaluated with :func:`blm_v4.cohorts.evaluate` under the frozen rule
version.  Source databases are opened ``mode=ro`` and never written.

Temporal firewall
-----------------
Only data at or before the observation is used:
  * progress from the observation's own quarter/clock (classification-aware
    quarter length, so a 12-minute CYBER quarter would not be mis-scaled),
  * required/actual pace from that row's score and its own live line,
  * the league reference from games whose ``result_at`` <= the observation
    (``--league-reference=prior``, default).  ``full`` exists only as a
    production-parity sensitivity mode and is recorded per row.
  * settlement (``game_results``) is joined AFTER evaluation, and scores against
    the FROZEN trigger line captured on the firing row.

Actual firing vs checkpoint
---------------------------
No alert ledger is persisted in this system (alerts are derived live), so a
firing is RECONSTRUCTED: the first observation of a game where the production
alert condition held (progress >= 75, required > league*1.04, live line+price).
Every other qualifying observation is ``checkpoint`` and is never counted as a
fired alert.  Unprovable inputs are stored as UNKNOWN with a reason code.

Idempotent: PK (source_game_id, observed_at, rule_version, source_type),
INSERT OR REPLACE — re-running converges to identical rows and counts.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from blm_v4.cohorts import evaluate, spec
from blm_v4.cohorts.metrics import settle_under
try:  # newer trees expose a classification-aware duration helper
    from blm_v4.projection import duration_for as _duration_for
except ImportError:  # this branch predates it — same 10/40 basis for BETUAL_NBA
    from blm_v4.projection import FULL_GAME_MINUTES, QUARTER_MINUTES

    def _duration_for(classification):
        return (QUARTER_MINUTES, FULL_GAME_MINUTES)


def duration_for(classification):
    """(quarter_minutes, full_game_minutes) for a classification."""
    return _duration_for(classification)

ALERT_PROGRESS_PCT = 75.0
ALERT_MARGIN = 1.04

_RE_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})'?$")

COLUMNS = (
    "source_game_id", "observed_at", "rule_version", "source_type",
    "competition_slug", "classification", "status", "qualifying_cohorts",
    "tightest_cohort", "reason_codes", "progress_pct", "scored_points",
    "required_ppm", "actual_ppm", "league_ppm", "pace_ratio", "shortfall",
    "under_price", "trigger_line", "trigger_observed_at", "league_source",
    "final_total", "final_status", "outcome", "settlement_source", "evaluated_at",
)

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS cohort_ledger (
    source_game_id   TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    rule_version     TEXT NOT NULL,
    source_type      TEXT NOT NULL,
    competition_slug TEXT,
    classification   TEXT,
    status           TEXT NOT NULL,
    qualifying_cohorts TEXT NOT NULL,
    tightest_cohort  TEXT,
    reason_codes     TEXT NOT NULL,
    progress_pct     REAL,
    scored_points    INTEGER,
    required_ppm     REAL,
    actual_ppm       REAL,
    league_ppm       REAL,
    pace_ratio       REAL,
    shortfall        REAL,
    under_price      REAL,
    trigger_line     REAL,
    trigger_observed_at TEXT,
    league_source    TEXT,
    final_total      REAL,
    final_status     TEXT,
    outcome          TEXT,
    settlement_source TEXT,
    evaluated_at     TEXT NOT NULL,
    PRIMARY KEY (source_game_id, observed_at, rule_version, source_type)
);
CREATE INDEX IF NOT EXISTS idx_ledger_cohort ON cohort_ledger(tightest_cohort, source_type);
CREATE INDEX IF NOT EXISTS idx_ledger_time ON cohort_ledger(observed_at);
CREATE TABLE IF NOT EXISTS rescore_runs (
    run_at TEXT PRIMARY KEY, rule_version TEXT, window_start TEXT, window_end TEXT,
    league_reference TEXT, observations INTEGER, evaluable INTEGER,
    unknown INTEGER, fired INTEGER, checkpoints INTEGER, reconciliation TEXT
);
"""


def _connect_ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    con.row_factory = sqlite3.Row
    return con


def elapsed_minutes(quarter, clock, q_min: float) -> Optional[float]:
    """Elapsed game minutes at an observation, using THIS classification's quarter."""
    try:
        q = int(quarter)
    except (TypeError, ValueError):
        return None
    if q is None or q < 1:
        return None
    c = (clock or "").strip()
    m = _RE_CLOCK.match(c)
    if m:
        mm, ss = int(m.group(1)), int(m.group(2))
        contrib = max(0.0, q_min - mm - ss / 60.0)   # sentinel clocks clamp to 0
        return round((q - 1) * q_min + contrib, 2)
    try:
        return round((q - 1) * q_min + float(c.rstrip("'`")), 2)
    except Exception:
        return None


def _settlements(con: sqlite3.Connection) -> dict:
    return {
        r["source_game_id"]: (r["final_total"], r["final_result_status"], r["result_source"])
        for r in con.execute(
            "SELECT source_game_id, final_total, final_result_status, result_source"
            " FROM game_results WHERE final_result_status = 'OK'")
    }


def _series(con: sqlite3.Connection) -> dict:
    """{slug: sorted [(result_at, pace)]} from settled games (for prior-only lookups)."""
    out: dict[str, list[tuple[str, float]]] = {}
    for r in con.execute(
        "SELECT g.competition_slug AS slug, g.classification AS cls,"
        "       gr.result_at AS at, gr.final_total AS total "
        "FROM game_results gr JOIN games g ON g.source_game_id = gr.source_game_id "
        "WHERE gr.final_result_status = 'OK' AND gr.final_total IS NOT NULL"
        "  AND gr.result_at IS NOT NULL AND g.competition_slug IS NOT NULL"
    ):
        _q, full = duration_for(r["cls"])
        if not full or not r["total"]:
            continue
        out.setdefault(r["slug"], []).append((r["at"], float(r["total"]) / full))
    for slug in out:
        out[slug].sort()
    return out


def _prior_mean(series: dict, slug: str, at: str) -> Optional[float]:
    pts = series.get(slug) or []
    if not pts:
        return None
    i = bisect.bisect_right([p[0] for p in pts], at)      # strictly <= at
    if i == 0:
        return None
    return sum(p[1] for p in pts[:i]) / i


def _full_sample(con: sqlite3.Connection) -> dict[str, float]:
    acc: dict[str, list[float]] = {}
    for r in con.execute(
        "SELECT g.competition_slug AS slug, g.classification AS cls,"
        "       gr.final_total AS total "
        "FROM game_results gr JOIN games g ON g.source_game_id = gr.source_game_id "
        "WHERE gr.final_result_status = 'OK' AND gr.final_total IS NOT NULL"
    ):
        _q, full = duration_for(r["cls"])
        if full and r["total"]:
            acc.setdefault(r["slug"], []).append(float(r["total"]) / full)
    return {k: sum(v) / len(v) for k, v in acc.items() if v}


def evaluate_game(game, rows: Iterable, *, series: dict, full_avg: dict,
                  league_mode: str) -> list[dict]:
    """Evaluate one game chronologically; never sees settlement or later rows."""
    slug = game["competition_slug"]
    cls = game["classification"]
    q_min, full = duration_for(cls)
    out: list[dict] = []
    fired_seen = False
    for r in rows:
        elapsed = elapsed_minutes(r["quarter"], r["clock"], q_min)
        progress = (min(100.0, max(0.0, elapsed / full * 100.0))
                    if (elapsed is not None and full) else None)
        score = (r["home_score"] or 0) + (r["away_score"] or 0)
        remaining = (full - elapsed) if (elapsed is not None and full) else None
        line = r["total_line"]
        required = ((float(line) - score) / remaining
                    if (line is not None and remaining is not None and remaining > 0) else None)
        actual = (score / elapsed) if (elapsed and elapsed > 0) else None
        if league_mode == "prior":
            league, league_source = _prior_mean(series, slug, r["captured_at"]), "prior_only"
        else:
            league, league_source = full_avg.get(slug), "full_sample"

        ev = evaluate({
            "progress_pct": progress,
            "scored_points": score,
            "competition_slug": slug,
            "required_pts_per_min": required,
            "actual_pts_per_min": actual,
            "league_avg_pts_per_min": league,
            "under_price": r["total_under_odds"],
            "line_move_enabled": False,
        })

        is_firing = bool(
            not fired_seen and progress is not None and progress >= ALERT_PROGRESS_PCT
            and required is not None and league and required > league * ALERT_MARGIN
            and r["total_under_odds"] is not None
        )
        if is_firing:
            fired_seen = True

        # The LEDGER keeps: every firing, every qualifying checkpoint, and UNKNOWN
        # rows once the game is in the alert band (data-quality evidence).
        # NOT_QUALIFIED rows are counted for reconciliation but not stored.
        store = bool(is_firing or ev.status == "QUALIFIED"
                     or (ev.status == "UNKNOWN" and progress is not None
                         and progress >= ALERT_PROGRESS_PCT))

        out.append({"_store": store,
            "source_game_id": game["source_game_id"],
            "observed_at": r["captured_at"],
            "rule_version": ev.rule_version,
            "source_type": "reconstructed_firing" if is_firing else "checkpoint",
            "competition_slug": slug,
            "classification": cls,
            "status": ev.status,
            "qualifying_cohorts": json.dumps(list(ev.qualifying_cohorts)),
            "tightest_cohort": ev.tightest_cohort,
            "reason_codes": json.dumps(list(ev.reason_codes)),
            "progress_pct": ev.progress_pct,
            "scored_points": ev.scored_points,
            "required_ppm": ev.required_pts_per_min,
            "actual_ppm": ev.actual_pts_per_min,
            "league_ppm": ev.league_avg_pts_per_min,
            "pace_ratio": ev.pace_ratio,
            "shortfall": ev.shortfall,
            "under_price": ev.under_price,
            "trigger_line": float(line) if line is not None else None,
            "trigger_observed_at": r["captured_at"],
            "league_source": league_source,
            "final_total": None, "final_status": None, "outcome": None,
            "settlement_source": None,
            "evaluated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return out


def run(pokerbet_db: str, out_db: str, *, league_mode: str = "prior",
        run_at: Optional[str] = None, include_cyber: bool = False,
        verbose: bool = True) -> dict:
    Path(out_db).parent.mkdir(parents=True, exist_ok=True)
    src = _connect_ro(pokerbet_db)
    dst = sqlite3.connect(out_db, timeout=60)
    dst.executescript(LEDGER_DDL)

    full_avg = _full_sample(src) if league_mode != "prior" else {}
    series = _series(src)
    settled = _settlements(src)

    games = src.execute(
        "SELECT id, source_game_id, competition_slug, classification FROM games "
        "WHERE competition_slug IS NOT NULL AND competition_slug <> '' "
        "ORDER BY first_seen_at").fetchall()

    rec = dict(observations=0, evaluable=0, unknown=0, fired=0, checkpoints=0,
               not_qualified=0, stored=0, qualified_by_cohort={}, games=0,
               settled_firings=0, under=0, over=0, push=0, pending=0,
               excluded_cyber=0, unique_qualified_games=0, earliest=None, latest=None)
    qgames: set[str] = set()

    for g in games:
        if not include_cyber and (g["competition_slug"] or "") in spec.EXCLUDED_COMPETITIONS:
            rec["excluded_cyber"] += 1
            continue
        rows = src.execute(
            "SELECT * FROM snapshots WHERE game_id = ? AND home_score IS NOT NULL"
            " AND away_score IS NOT NULL ORDER BY captured_at", (g["id"],)).fetchall()
        if not rows:
            continue
        rec["games"] += 1
        for e in evaluate_game(g, rows, series=series, full_avg=full_avg,
                               league_mode=league_mode):
            rec["observations"] += 1
            rec["earliest"] = min(filter(None, [rec["earliest"], e["observed_at"]]))
            rec["latest"] = max(filter(None, [rec["latest"], e["observed_at"]]))
            if e["status"] == "UNKNOWN":
                rec["unknown"] += 1
            elif e["status"] == "NOT_QUALIFIED":
                rec["not_qualified"] += 1
            else:
                rec["evaluable"] += 1
            if e["source_type"] == "reconstructed_firing":
                rec["fired"] += 1
                fin = settled.get(e["source_game_id"])          # AFTER evaluation
                if fin and fin[0] is not None:
                    e["final_total"], e["final_status"], e["settlement_source"] = fin
                    e["outcome"] = settle_under(e["trigger_line"], fin[0])
                    rec["settled_firings"] += 1
                    key = {"UNDER": "under", "OVER": "over", "PUSH": "push"}.get(
                        e["outcome"], "pending")
                    rec[key] += 1
                    if e["outcome"] in ("UNDER", "OVER", "PUSH"):
                        for code in json.loads(e["qualifying_cohorts"]):
                            qgames.add(e["source_game_id"])
                else:
                    rec["pending"] += 1
            elif e["status"] == "QUALIFIED":
                rec["checkpoints"] += 1
            for code in json.loads(e["qualifying_cohorts"]):
                if e["source_type"] == "reconstructed_firing":
                    rec["qualified_by_cohort"][code] = (
                        rec["qualified_by_cohort"].get(code, 0) + 1)
            if not e.get("_store"):
                continue
            rec["stored"] += 1
            placeholders = ",".join("?" * len(COLUMNS))
            dst.execute(f"INSERT OR REPLACE INTO cohort_ledger ({','.join(COLUMNS)})"
                        f" VALUES ({placeholders})", [e[k] for k in COLUMNS])

    rec["unique_qualified_games"] = len({g.split("|")[0] for g in qgames})
    dst.execute("INSERT OR REPLACE INTO rescore_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 spec.RULE_VERSION, rec["earliest"], rec["latest"], league_mode,
                 rec["observations"], rec["evaluable"], rec["unknown"], rec["fired"],
                 rec["checkpoints"], json.dumps(rec, sort_keys=True)))
    dst.commit()
    dst.close()
    src.close()
    if verbose:
        print(json.dumps(rec, indent=2, sort_keys=True))
    return rec


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pokerbet-db", default="/srv/blm/data/blm_pokerbet.db")
    ap.add_argument("--out", default="/srv/blm/data/derived/cohort_rescore.db")
    ap.add_argument("--league-reference", choices=("prior", "full"), default="prior")
    ap.add_argument("--include-cyber", action="store_true")
    args = ap.parse_args(argv)
    if not os.path.exists(args.pokerbet_db):
        print(f"missing source DB: {args.pokerbet_db}", file=sys.stderr)
        return 2
    run(args.pokerbet_db, args.out, league_mode=args.league_reference,
        include_cyber=args.include_cyber)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
