"""LIVE ALERT STATS — the fired-alert cohort, its fingerprints, the LEAGUE
STANDINGS, and the ALERT-TIMING WINDOW.

WHAT THIS ANSWERS (operator questions, in order of frequency)

  1. "Of all the games where the alert fired, what % ended UNDER?"
  2. "Which fingerprints were present, and how did each perform?"
  3. "WHICH LEAGUES ARE UNDERPERFORMING?" — the standings.
  4. "Are the alerts firing too late to be actionable?" — the window.

THE COHORT (one definition, stated once, used by every section)

    progress_pct >= 75
    AND required_pts_per_min > league_average_pace * 1.04     (STRICT)

one observation per game — the CLOSEST to 75% inside the window, non-terminal
— and only games with an AUTHORITATIVE settled final
(``game_results.final_result_status='OK'``).  This is character-for-character
the construction of ``scripts/parity_under_trigger_2026-09-14.py`` (the run
that reproduces the historical ~69.89% cell), so the numbers on the stats tab
tie back to the audited artifact rather than to a new definition.

THE ALERT-TIMING WINDOW (directive: "alerts by the 6th minute of Q3 and no
later than the 4th minute of Q4")

Expressed in GAME-CLOCK, never as a flat progress percent — the same minute of
the same quarter is a different percentage in each classification::

    floor    = 2 * quarter_minutes + 6      (6th minute of Q3)
    ceiling  = 3 * quarter_minutes + 4      (4th minute of Q4)

    BETUAL_NBA (10-min quarters, 40-min game):  26 min = 65.00% / 34 min = 85.00%
    CYBER_2K26 (12-min quarters, 48-min game):  30 min = 62.50% / 40 min = 83.33%

MEASURED, NOT ASSUMED (see ``late`` in the payload).  On the settled archive the
alerts ABOVE the ceiling are not the bad ones: the ceiling drops 14 alerts that
ran 78.57% UNDER, against 66.5% for those kept — so enforcing it costs ~0.1pp
on the pooled rate and takes EuroLeague's best-alerting set (9 of the 14).  And
moving the FLOOR earlier is far more expensive: alerts whose first firing
moment sits below 75% run ~61% UNDER against ~89% for those at/after 75%
(N=2433 vs N=3196), i.e. the earlier alerts DILUTE the book.  Lateness is an
EXECUTABILITY concern (can the bet still be placed), not an accuracy one.  This
module therefore REPORTS the window and enforces nothing.

SEGMENTATION BY COMPETITION, NOT BY CLASSIFICATION.  This is load-bearing.
``classification`` (BETUAL_NBA / CYBER_2K26) is the PROVIDER FAMILY, not the
league: BETUAL_NBA bundles five distinct competitions (NBA, TBSL, EuroLeague,
CBA, KBL) — see :mod:`blm_v4.live_analytics.league`, which audits exactly this
("TSBL games display as 'Betual NBA'").  Grouping this cohort by
classification once produced a false negative ("EuroLeague is not affected");
grouped by ``competition_slug`` it is the 2nd largest unresolved cohort.  The
standings here are keyed on ``games.competition_slug``, the authoritative
identifier.

THE STANDINGS TEST.  Raw UNDER% alone cannot rank leagues: a small sample can
read 100% or 0%.  Each league therefore carries a WILSON 95% interval, and the
verdict column is the honest question — is the interval's LOWER bound above
50%?  If it is not, that league's rate is NOT distinguishable from a coin flip
at this sample size, however good or bad the point estimate looks.
``selection_pct`` (how often the league fires at all) is reported beside it: a
league that fires often AND wins rarely is the real problem, while one that
fires rarely AND wins rarely is simply weak.

READ-ONLY.  Both databases are opened ``mode=ro``.  This module never writes,
never opens a network socket, and never touches a gate: fingerprint_count
remains RECORDED, not a threshold, and the window is reported, not enforced.

CACHING.  ``compute_stats`` is a full-history scan (~360k observation rows plus
a grouped snapshot read) — around 5 s.  Far too expensive for the 5 s dashboard
poll, so ``get_stats`` wraps it in a process-level TTL cache and the API route
calls it off the event loop.  The cache is keyed on both database identities,
so a different DB (tests) never serves another DB's numbers.
"""
from __future__ import annotations

import math
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from blm_v4.live_analytics.competition_pace import (  # noqa: E402
    competition_pace_reference)
from blm_v4.live_analytics.fingerprint_c5 import (  # noqa: E402
    q3_pace_reference)
from blm_v4.live_analytics.under_alert import (  # noqa: E402
    ALERT_PROGRESS_PCT, REQUIRED_MARGIN)
from blm_v4.live_analytics.under_fingerprints import (  # noqa: E402
    FINGERPRINT_KEYS, FINGERPRINT_LABELS, evaluate_fingerprints)
from blm_v4.projection import duration_for  # noqa: E402

#: The observation window the historical sweep used.  The single observation
#: per game is the one CLOSEST to ALERT_PROGRESS_PCT inside it.  Kept at
#: [65,85] deliberately: that is the window
#: ``scripts/parity_under_trigger_2026-09-14.py`` used to reproduce the audited
#: ~69.89% cell, so the headline here ties back to that artifact.  (The
#: directive's "6th minute of Q3" = 65.00% BETUAL / 62.50% CYBER and "4th minute
#: of Q4" = 85.00% / 83.33% — the window is reported per classification in the
#: `window` block; see `window_minutes`.)
WINDOW_LO = 65.0
WINDOW_HI = 85.0

#: "no later than the 4th minute of Q4" — minutes played INTO Q4.
LATE_BOUND_Q4_MINUTE = 4.0
#: "by the 6th minute of Q3" — minutes played INTO Q3.
EARLY_BOUND_Q3_MINUTE = 6.0
#: The floor of the firing scan.  Set below the 6th-minute-of-Q3 bound so the
#: scan captures firing moments that are genuinely EARLY (a game can clear the
#: required-pace bar well before Q3), which is the whole point of measuring the
#: window rather than assuming it.
FIRING_SCAN_LO = 40.0

#: Operator-facing league labels.  ``games.competition`` (the display name) is
#: UNRELIABLE — TSBL games display as "Betual NBA" — so labels are mapped from
#: the authoritative ``competition_slug`` and anything unmapped falls back to
#: the slug itself rather than being hidden.
LEAGUE_LABELS = {
    "betual-nba": "NBA",
    "betual-tbsl": "TBSL",
    "betual-euroleague": "EuroLeague",
    "betual-cba": "CBA",
    "betual-kbl": "KBL",
    "cyber-basketball-2k26-matches": "CYBER 2K26",
    "club-friendlies": "Club Friendlies",
    "kbl-cup": "KBL Cup",
}

_COHORT_MODEL = ("fired-alert-cohort/2 (progress>=75 AND required>league_avg*1.04, "
                 "closest-to-75 obs, settled OK; window reported in game-clock)")


def window_minutes(classification: Optional[str]) -> tuple[float, float, float]:
    """(floor_minutes, ceiling_minutes, full_minutes) for a classification.

    The timing window in GAME CLOCK — the only classification-safe basis.
    A flat progress percent would mean different clock minutes per league.
    """
    quarter, full = duration_for(classification)
    q = float(quarter or 0.0)
    f = float(full or 0.0)
    if q <= 0 or f <= 0:
        return 0.0, 0.0, 0.0
    return (2 * q + EARLY_BOUND_Q3_MINUTE,
            3 * q + LATE_BOUND_Q4_MINUTE, f)


def _fin(x: Any) -> Optional[float]:
    """Finite float or None (booleans excluded — never a number)."""
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _wilson(k: int, n: int, z: float = 1.96
            ) -> tuple[Optional[float], Optional[float]]:
    """Wilson score interval for a binomial proportion, in PERCENT.

    Preferred over the normal approximation because the standings contain small
    samples (and rates near 0/1), where the normal interval runs outside
    [0,100] and understates uncertainty.  (None, None) for an empty sample.
    """
    if n <= 0:
        return None, None
    p = k / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (100.0 * max(0.0, centre - half),
            100.0 * min(1.0, centre + half))


def _ro(path: Path) -> sqlite3.Connection:
    """Read-only connection.  The stats layer never writes."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=1")
    except sqlite3.Error:
        pass
    return conn


def _rate_block(recs: list[dict]) -> dict:
    """The ONE place a hit-rate block is shaped, so every table agrees."""
    n = len(recs)
    k = sum(1 for r in recs if r["outcome"] == "UNDER")
    lo, hi = _wilson(k, n)
    return {
        "n": n, "under": k,
        "over": sum(1 for r in recs if r["outcome"] == "OVER"),
        "push": sum(1 for r in recs if r["outcome"] == "PUSH"),
        "under_pct": round(100.0 * k / n, 2) if n else None,
        "wilson_lo": round(lo, 2) if lo is not None else None,
        "wilson_hi": round(hi, 2) if hi is not None else None,
    }


def _q3_map(conn_c: sqlite3.Connection) -> dict[str, float]:
    """{game_id: (q3_end - q2_end)} for the WHOLE archive, in ONE pass.

    The Q3 segment is the cumulative score at the Q3 end minus the cumulative
    at the Q2 end; the cumulative only rises within a segment, so the segment
    MAX is its end state — the construction ``api._q3_game_ppm`` and
    ``fingerprint_c5.q3_pace_reference`` both use.

    Deliberately ONE grouped scan rather than a per-game or IN-list lookup: the
    snapshot table has ~1.76 M rows and is indexed on (game, captured_at), so a
    bounded IN-list still costs a scan PER CHUNK (measured: 3 chunked queries
    over a 1,000-id cohort dominated the whole computation at ~9 s).  One pass
    is measured at ~1.6 s regardless of cohort size.

    The id is stripped of its ``#iN`` instance suffix FIRST: a game's snapshots
    can be spread across base + instance rows, and grouping on the raw id
    partitions one fixture into several incomplete series (a treated id had 0
    rows under its bare form but a valid Q2/Q3 pair under its instance).

    Rows are read POSITIONALLY on purpose (see ``competition_pace``): this
    helper must not depend on the caller having set ``row_factory``.
    """
    out: dict[str, float] = {}
    try:
        rows = conn_c.execute(
            """SELECT substr(source_game_id, 1,
                      CASE WHEN instr(source_game_id,'#')>0
                           THEN instr(source_game_id,'#')-1
                           ELSE length(source_game_id) END) AS base,
                      MAX(CASE WHEN TRIM(COALESCE(period_label,''))='2nd Quarter'
                               THEN home_score + away_score END) AS q2_end,
                      MAX(CASE WHEN TRIM(COALESCE(period_label,''))='3rd Quarter'
                               THEN home_score + away_score END) AS q3_end
                 FROM clean_snapshots
                WHERE TRIM(COALESCE(period_label,'')) IN
                      ('2nd Quarter','3rd Quarter')
                GROUP BY base""").fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        q2, q3 = _fin(r[1]), _fin(r[2])
        if q2 is None or q3 is None or q3 < q2:
            continue
        out[r[0]] = q3 - q2
    return out


def _first_firing_scan(conn_c: sqlite3.Connection,
                       avg_by_slug: dict, settled: dict) -> list[dict]:
    """The FIRST moment each game's alert condition became true — the moment
    the alert ACTUALLY fires in production.

    WHY THIS IS A SEPARATE PASS.  The cohort's observation is deliberately the
    one CLOSEST to 75%, so by construction it sits at ~75% progress and can
    never show whether an alert fired LATE.  Answering "are alerts too late?"
    needs each game's first qualifying moment instead, which may sit anywhere
    from the first minutes of Q3 to well into Q4.

    STREAMING, NOT LOADED.  The scan spans progress >= FIRING_SCAN_LO (captures
    firing moments from well before the cohort window) over a table of ~1.27 M
    rows, and this box is memory-pressured, so rows are consumed as a
    generator ordered by (game, captured_at) and only ONE record per game is
    retained.  The first qualifying row for a game ends its processing — later
    rows for the same game are skipped without being materialised.

    Rows are read POSITIONALLY (see ``competition_pace``) so this helper does
    not depend on the caller having set ``row_factory``.

    Read-only.  A missing league reference or non-finite operand simply yields
    no firing for that game, never a guessed one.
    """
    out: list[dict] = []
    cur_gid = None
    found_for_cur = False
    try:
        cursor = conn_c.execute(
            "SELECT source_game_id, progress_pct, elapsed_game_minutes, "
            "       current_total_points, remaining_game_minutes, "
            "       live_total_line, market_status, market_age_seconds "
            "  FROM clean_projections "
            " WHERE progress_pct >= ? AND progress_pct < 100 "
            "   AND (terminal IS NULL OR terminal = 0) "
            "   AND current_total_points IS NOT NULL "
            "   AND remaining_game_minutes > 0 "
            "   AND live_total_line IS NOT NULL "
            " ORDER BY source_game_id, captured_at",
            (FIRING_SCAN_LO,))
        for r in cursor:
            gid = str(r[0]).split("#")[0]
            if gid != cur_gid:
                cur_gid, found_for_cur = gid, False
            if found_for_cur or gid not in settled:
                continue
            meta = settled[gid]
            avg = avg_by_slug.get(meta[1])
            if avg is None or avg <= 0:
                found_for_cur = True       # no reference -> never fires
                continue
            tot, rem, line = _fin(r[3]), _fin(r[4]), _fin(r[5])
            if tot is None or rem is None or line is None or rem <= 0:
                continue
            req = (line - tot) / rem
            if req > avg * REQUIRED_MARGIN:
                mkt = r[6] or ""
                age = _fin(r[7])
                out.append({
                    "gid": gid, "slug": meta[1], "cls": meta[2],
                    "elapsed": _fin(r[2]), "prog": _fin(r[1]),
                    "req": round(req, 4), "avg": avg,
                    # the line and score AT THE FIRING MOMENT — the outcome
                    # must be settled against the line this alert carried,
                    # never against the cohort observation's line
                    "line": line, "tot": tot,
                    "fresh": not (mkt in ("STALE", "MISSING")
                                  or (age is not None and age > 300.0)),
                })
                found_for_cur = True
    except sqlite3.Error:
        return out
    return out


def _dirty_key(prod_path: Path) -> tuple:
    """A CHEAP change-detector for the stats payload: (settled count, last
    settled timestamp).

    WHY THIS EXISTS.  ``compute_stats`` is a full-history rescan (~10-30 s of
    CPU and disk, dominated by the two group-bys over the ~1.76 M-row snapshot
    table).  Its result depends only on the SETTLED games — the cohort is
    restricted to authoritative finals — so when no new game has settled, a
    rescan can only reproduce the payload byte-for-byte.  On a box that is
    already IO- and memory-pressured, re-running it every 60 s is pure waste.
    This key costs ~6 ms (measured) and lets the worker skip the scan entirely
    when nothing has changed.

    Deliberately NOT a copy of the cohort's full filter chain.  It is a
    change-DETECTOR, not an authority: it only has to notice that the settled
    set moved.  ``StatsWorker.max_age_s`` is the backstop that guarantees a
    genuine rescan even if this key ever fails to notice something.

    Returns a tuple that compares by value; raises ``sqlite3.Error`` if the
    database cannot be read, which the caller treats as "changed" (fail safe:
    scan rather than indefinitely serve a payload we cannot vouch for).
    """
    conn = _ro(prod_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*), MAX(result_at) FROM game_results "
            " WHERE final_result_status='OK' "
            "   AND final_total IS NOT NULL AND final_total>0").fetchone()
        return (int(row[0] or 0), str(row[1] or ""))
    finally:
        conn.close()


def compute_stats(prod_path: Path, clean_path: Path) -> dict:
    """Build the whole stats payload.  Read-only; no gate is touched.

    Raises ``sqlite3.Error`` if a database is unreadable — the caller decides
    how to degrade.  A game missing an operand is EXCLUDED and counted, never
    guessed.
    """
    t0 = time.monotonic()
    conn_p, conn_c = _ro(prod_path), _ro(clean_path)
    try:
        # ── 1. settled finals (the authoritative outcome side) ──────────
        settled: dict[str, tuple] = {}
        for r in conn_p.execute(
                "SELECT gr.source_game_id AS gid, gr.final_total AS ft, "
                "       g.competition_slug AS slug, g.classification AS cls "
                "  FROM game_results gr JOIN games g "
                "    ON g.source_game_id = gr.source_game_id "
                " WHERE gr.final_result_status='OK' "
                "   AND gr.final_total IS NOT NULL AND gr.final_total>0 "
                "   AND g.competition_slug IS NOT NULL "
                "   AND g.competition_slug<>''"):
            ft = _fin(r["ft"])
            if ft is not None:
                settled[r["gid"]] = (ft, r["slug"], r["cls"])

        # ── 2. the league pace reference (per competition, settled OK) ──
        ref = competition_pace_reference(conn_p)
        avg_by_slug = {s: _fin(v.get("avg_pace")) for s, v in ref.items()}
        ref_games = {s: int(v.get("games") or 0) for s, v in ref.items()}

        # ── 3. one observation per game: closest to 75% in the window ───
        best: dict[str, tuple] = {}
        n_window_rows = 0
        for r in conn_c.execute(
                "SELECT source_game_id AS gid, progress_pct AS prog, "
                "       elapsed_game_minutes AS el, "
                "       current_total_points AS tot, "
                "       remaining_game_minutes AS rem, "
                "       live_total_line AS line, recent_pace_3m AS r3, "
                "       actual_pts_per_min AS act, market_status AS mkt, "
                "       market_age_seconds AS age "
                "  FROM clean_projections "
                " WHERE progress_pct >= ? AND progress_pct <= ? "
                "   AND progress_pct < 100 "
                "   AND (terminal IS NULL OR terminal = 0) "
                "   AND current_total_points IS NOT NULL "
                "   AND remaining_game_minutes > 0 "
                "   AND live_total_line IS NOT NULL",
                (WINDOW_LO, WINDOW_HI)):
            n_window_rows += 1
            gid = r["gid"].split("#")[0]
            tot, rem, line = _fin(r["tot"]), _fin(r["rem"]), _fin(r["line"])
            prog = _fin(r["prog"])
            if tot is None or rem is None or line is None or rem <= 0 \
                    or prog is None:
                continue
            d = abs(prog - ALERT_PROGRESS_PCT)
            prev = best.get(gid)
            if prev is None or d < prev[0]:
                best[gid] = (d, prog, _fin(r["el"]), tot, rem, line,
                             _fin(r["r3"]), _fin(r["act"]),
                             (r["mkt"] or ""), _fin(r["age"]))

        # ── 4. the cohort: fired AND settled ────────────────────────────
        universe: dict[str, dict] = {}
        cohort: list[dict] = []
        for gid, (d, prog, el, tot, rem, line, r3, act, mkt, age) in best.items():
            meta = settled.get(gid)
            if meta is None:
                continue                 # unsettled -> cannot be scored
            ft, slug, cls = meta
            avg = avg_by_slug.get(slug)
            if avg is None or avg <= 0:
                continue                 # no reference -> nothing claimed
            req = round((line - tot) / rem, 4)
            fresh = not (mkt in ("STALE", "MISSING")
                         or (age is not None and age > 300.0))
            floor, ceiling, full = window_minutes(cls)
            # where in the alert window this firing moment sits
            if el is None or ceiling <= 0:
                timing = "unknown"
            elif el > ceiling:
                timing = "late"          # after the 4th minute of Q4
            elif el < floor:
                timing = "early"         # before the 6th minute of Q3
            else:
                timing = "in_window"
            rec = {"gid": gid, "slug": slug, "cls": cls, "req": req,
                   "avg": avg, "line": line, "final": ft, "prog": prog,
                   "elapsed": el, "fresh": fresh, "r3": r3, "act": act,
                   "timing": timing, "floor_min": floor,
                   "ceiling_min": ceiling,
                   "ratio": (req / (avg * REQUIRED_MARGIN) if avg else None)}
            universe[gid] = rec
            # COHORT = the trigger CONDITION alone (progress + required-pace),
            # deliberately WITHOUT the live-market eligibility gate — this is
            # the population the audited parity artifact measured, so the
            # headline rate ties back to it.  The actionable (LIVE-market)
            # subset is reported separately in `actionable`.
            if prog >= ALERT_PROGRESS_PCT and req > avg * REQUIRED_MARGIN:
                cohort.append(rec)

        # ── 5. outcome + fingerprints for every cohort member ───────────
        q3_raw = _q3_map(conn_c)
        q3ref = q3_pace_reference(conn_p)
        for r in cohort:
            line, ft = r["line"], r["final"]
            r["outcome"] = ("UNDER" if ft < line
                            else "OVER" if ft > line else "PUSH")
            q3 = None
            delta = q3_raw.get(r["gid"])
            if delta is not None:
                qm, _full = duration_for(r["cls"])
                if qm and qm > 0:
                    q3 = delta / float(qm)
            r["q3"] = q3
            r["fp"] = evaluate_fingerprints(
                r["req"], r["avg"], q3,
                (q3ref.get(r["slug"]) or {}).get("avg_q3_pace"),
                r["r3"], r["act"])

        n = len(cohort)
        overall = _rate_block(cohort)
        actionable = [r for r in cohort if r["fresh"]]

        # ── 5b. THE FIRINGS — each game's FIRST qualifying moment ───────
        # Computed BEFORE the league table because the league timing columns
        # come from firings, not from the cohort (whose observation is pinned
        # at ~75% by construction and therefore cannot show lateness).
        firings = _first_firing_scan(conn_c, avg_by_slug, settled)
        for f in firings:
            floor_m, ceiling_m, _full = window_minutes(f["cls"])
            el = f["elapsed"]
            if el is None or ceiling_m <= 0:
                f["timing"] = "unknown"
            elif el > ceiling_m:
                f["timing"] = "late"       # after the 4th minute of Q4
            elif el < floor_m:
                f["timing"] = "early"      # before the 6th minute of Q3
            else:
                f["timing"] = "in_window"
            # outcome, settled against the line IN FORCE AT THE FIRING MOMENT
            f["outcome"] = None
            meta = settled.get(f["gid"])
            if meta is not None and f["line"] is not None:
                ft = meta[0]
                f["outcome"] = ("UNDER" if ft < f["line"]
                                else "OVER" if ft > f["line"] else "PUSH")

        # ── 6. LEAGUE STANDINGS — weakest first ─────────────────────────
        by_league: dict[str, dict] = defaultdict(
            lambda: {"recs": [], "universe": 0})
        for r in universe.values():
            by_league[r["slug"]]["universe"] += 1
        for r in cohort:
            by_league[r["slug"]]["recs"].append(r)

        # per-league timing, from the FIRINGS (the cohort's own observation is
        # pinned near 75% and could never show lateness)
        firing_by_slug: dict[str, list] = defaultdict(list)
        for f in firings:
            firing_by_slug[f["slug"]].append(f)

        leagues = []
        for slug, d in by_league.items():
            recs = d["recs"]
            if not recs:
                continue
            ref_avg = avg_by_slug.get(slug)
            blk = _rate_block(recs)
            wlo, whi = blk["wilson_lo"], blk["wilson_hi"]
            ratios = sorted(r["ratio"] for r in recs if r["ratio"] is not None)
            med = ratios[len(ratios) // 2] if ratios else None
            uni = d["universe"]
            lf = firing_by_slug.get(slug, [])
            lf_late = [f for f in lf if f["timing"] == "late"]
            lf_late_scored = [f for f in lf_late if f["outcome"]]
            lf_early = [f for f in lf if f["timing"] == "early"]
            # verdict: is the interval's LOWER bound above a coin flip?
            if wlo is None or whi is None:
                verdict = "NO SAMPLE"
            elif wlo > 50.0:
                verdict = "PROFITABLE"
            elif whi < 50.0:
                verdict = "LOSING"
            else:
                verdict = "COIN FLIP"
            leagues.append({
                "slug": slug, "label": LEAGUE_LABELS.get(slug, slug),
                **blk,
                "verdict": verdict,
                "vs_50": round(wlo - 50.0, 2) if wlo is not None else None,
                "median_req_bar": round(med, 3) if med is not None else None,
                "universe": uni,
                "selection_pct": round(100.0 * len(recs) / uni, 2) if uni else None,
                "ref_avg_pace": (round(float(ref_avg), 4)
                                 if ref_avg is not None else None),
                "ref_games": ref_games.get(slug, 0),
                "firings_n": len(lf),
                "late_n": len(lf_late),
                "late_under_pct": (round(
                    100.0 * sum(1 for f in lf_late_scored
                                if f["outcome"] == "UNDER")
                    / len(lf_late_scored), 2) if lf_late_scored else None),
                "early_n": len(lf_early),
            })
        # weakest first — the point of the table
        leagues.sort(key=lambda x: (x["under_pct"] if x["under_pct"] is not None
                                    else 101.0, -x["n"]))

        # ── 7. FINGERPRINTS ─────────────────────────────────────────────
        fprints = []
        for key in FINGERPRINT_KEYS:
            low = key.lower()
            sel = [r for r in cohort if r["fp"][f"fingerprint_{low}_triggered"]]
            blk = _rate_block(sel)
            nn = blk["n"]
            blk.update({
                "key": key,
                "label": FINGERPRINT_LABELS.get(key, key),
                "coverage_pct": round(100.0 * nn / n, 2) if n else None,
                "delta_pp": (round(blk["under_pct"] - overall["under_pct"], 2)
                             if nn and blk["under_pct"] is not None
                             and overall["under_pct"] is not None else None),
                "unavailable": sum(
                    1 for r in cohort
                    if r["fp"][f"fingerprint_{low}"] not in ("TRUE", "FALSE")),
            })
            fprints.append(blk)
        fprints.sort(key=lambda x: -(x["under_pct"] or 0))

        # ── 8. COUNT LADDER + the collapse identity ─────────────────────
        ladder = []
        for cnt in range(0, len(FINGERPRINT_KEYS) + 1):
            sel = [r for r in cohort if r["fp"]["fingerprint_count"] == cnt]
            if not sel:
                continue
            blk = _rate_block(sel)
            blk["count"] = cnt
            ladder.append(blk)

        # The seven keys SHARE legs (C4=C1&C2, C3/C5/C6 pair a required-pace
        # leg with the Q3 leg), so a full house is NOT seven independent
        # confirmations.  ASSERT the identity rather than describing it.
        all_seven = {r["gid"] for r in cohort
                     if r["fp"]["fingerprint_count"] == len(FINGERPRINT_KEYS)}
        c1 = {r["gid"] for r in cohort if r["fp"]["fingerprint_c1_triggered"]}
        c2 = {r["gid"] for r in cohort if r["fp"]["fingerprint_c2_triggered"]}
        r2 = {r["gid"] for r in cohort if r["fp"]["fingerprint_r2_triggered"]}
        inter = c1 & c2 & r2
        collapse = {
            "seven_n": len(all_seven),
            "intersection_n": len(inter),
            "identical": bool(all_seven) and all_seven == inter,
            "definition": "count == 7   ==   C1 AND C2 AND R2",
            "explanation": (
                "C1 fixes req_ratio in [1.10,1.20) and R2 fixes q3_ratio<0.90, "
                "so req>1.04 & q3<1.00 (C3), req>1.10 & q3<1.00 (C5) and "
                "C2 & q3<1.00 (C6) all follow arithmetically. A full house is "
                "ONE conjunction of three conditions, not seven votes."),
        }

        # ── 9. req/bar bands — the edge is NOT monotone ─────────────────
        bands = []
        for lo_b, hi_b in ((1.00, 1.05), (1.05, 1.10), (1.10, 1.20),
                           (1.20, 1.50), (1.50, 99.0)):
            sel = [r for r in cohort if r["ratio"] is not None
                   and lo_b <= r["ratio"] < hi_b]
            if not sel:
                continue
            blk = _rate_block(sel)
            blk["label"] = (f"[{lo_b:.2f}, {hi_b:.2f})" if hi_b < 90
                            else f">= {lo_b:.2f}")
            bands.append(blk)

        # ── 10. observed fired-set combinations ─────────────────────────
        combos: dict[tuple, list] = defaultdict(list)
        for r in cohort:
            combos[tuple(r["fp"]["fingerprints_fired"])].append(r)
        combo_rows = []
        for s, recs in sorted(combos.items(), key=lambda kv: -len(kv[1]))[:16]:
            blk = _rate_block(recs)
            blk["set"] = list(s)
            blk["label"] = "+".join(s) if s else "(none)"
            combo_rows.append(blk)

        # ── 11. THE ALERT-TIMING WINDOW (reported, never enforced) ──────
        # Measured from each game's FIRST firing moment (step 5b), NOT from
        # the cohort's closest-to-75 observation — that one is pinned at ~75%
        # by construction and could never show lateness.  `late` means: the
        # moment the alert became TRUE sat after the 4th minute of Q4.
        f_in = [f for f in firings if f["timing"] == "in_window"]
        f_late = [f for f in firings if f["timing"] == "late"]
        f_early = [f for f in firings if f["timing"] == "early"]

        def _firing_block(recs: list) -> dict:
            scored = [r for r in recs if r["outcome"]]
            n_s = len(scored)
            k = sum(1 for r in scored if r["outcome"] == "UNDER")
            lo_w, hi_w = _wilson(k, n_s)
            return {
                "n": len(recs), "scored": n_s, "under": k,
                "under_pct": round(100.0 * k / n_s, 2) if n_s else None,
                "wilson_lo": round(lo_w, 2) if lo_w is not None else None,
                "wilson_hi": round(hi_w, 2) if hi_w is not None else None,
                "actionable": sum(1 for r in recs if r["fresh"]),
            }

        cost = {
            "dropped_n": len(f_late),
            "dropped_under_pct": _firing_block(f_late)["under_pct"],
            "kept_n": len(f_in),
            "kept_under_pct": _firing_block(f_in)["under_pct"],
            "verdict": (
                "Lateness is an EXECUTABILITY constraint, not an accuracy "
                "one: compare the two rates above before enforcing the "
                "ceiling."),
        }
        sample = None
        window = {
            "bounds": {
                "early_q3_minute": EARLY_BOUND_Q3_MINUTE,
                "late_q4_minute": LATE_BOUND_Q4_MINUTE,
                "firing_scan_lo_pct": FIRING_SCAN_LO,
            },
            "per_classification": [
                {"classification": cls,
                 "quarter_minutes": duration_for(cls)[0],
                 "full_minutes": duration_for(cls)[1],
                 "floor_minutes": window_minutes(cls)[0],
                 "floor_pct": (round(100.0 * window_minutes(cls)[0]
                                     / window_minutes(cls)[2], 2)
                               if window_minutes(cls)[2] else None),
                 "ceiling_minutes": window_minutes(cls)[1],
                 "ceiling_pct": (round(100.0 * window_minutes(cls)[1]
                                       / window_minutes(cls)[2], 2)
                                 if window_minutes(cls)[2] else None),
                 "trigger_floor_pct": ALERT_PROGRESS_PCT}
                for cls in ("BETUAL_NBA", "CYBER_2K26")],
            "firings_total": len(firings),
            "scored": len([f for f in firings if f["outcome"]]),
            "in_window": _firing_block(f_in),
            "late": _firing_block(f_late),
            "early": _firing_block(f_early),
            "cost_of_enforcing_ceiling": cost,
            "note": ("The current trigger floor is progress >= 75%, which is "
                     "Q4 minute 0 — already LATER than the 6th minute of Q3, "
                     "so no alert fires early today. The floor and the "
                     "ceiling are therefore not symmetric: the ceiling is "
                     "binding, the floor is not."),
        }

        return {
            "model": _COHORT_MODEL,
            "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "computed_in_s": round(time.monotonic() - t0, 2),
            "window_rows_scanned": n_window_rows,
            "cohort": {**overall,
                       "universe_settled": len(universe),
                       "settled_games_total": len(settled)},
            "actionable": {
                **_rate_block(actionable),
                "definition": ("the cohort restricted to a LIVE market at the "
                               "trigger moment (the eligibility gate the live "
                               "alert applies) — the bets that could actually "
                               "be placed"),
                "excluded_no_live_market": n - len(actionable),
            },
            "window": window,
            "leagues": leagues,
            "fingerprints": fprints,
            "ladder": ladder,
            "collapse": collapse,
            "bands": bands,
            "combos": combo_rows,
            "definitions": {
                "fingerprints": [
                    {"key": "C1", "rule": "1.10 <= req_ratio < 1.20",
                     "label": FINGERPRINT_LABELS.get("C1")},
                    {"key": "C2", "rule": "recent_pace_3m - actual_pts_per_min <= -0.5",
                     "label": FINGERPRINT_LABELS.get("C2")},
                    {"key": "C3", "rule": "req_ratio > 1.04 AND q3_ratio < 1.00",
                     "label": FINGERPRINT_LABELS.get("C3")},
                    {"key": "C4", "rule": "C1 AND C2",
                     "label": FINGERPRINT_LABELS.get("C4")},
                    {"key": "C5", "rule": "req_ratio > 1.10 AND q3_ratio < 1.00",
                     "label": FINGERPRINT_LABELS.get("C5")},
                    {"key": "C6", "rule": "C2 AND q3_ratio < 1.00",
                     "label": FINGERPRINT_LABELS.get("C6")},
                    {"key": "R2", "rule": "q3_ratio < 0.90",
                     "label": FINGERPRINT_LABELS.get("R2")},
                    {"key": "R1", "rule": "EXCLUDED by directive",
                     "label": "not implemented, enforced absent by test"},
                ],
                "operands": {
                    "req_ratio": "required_pts_per_min / league_average_pace",
                    "q3_ratio": ("game's Q3 ppm / that competition's settled "
                                 "Q3 average"),
                    "three_state": ("TRUE / FALSE / UNAVAILABLE — a missing "
                                    "operand is never TRUE"),
                },
                "alert": ("progress_pct >= 75 AND required_pts_per_min > "
                          "league_average_pace * 1.04 (STRICT)"),
            },
            "notes": [
                "Cohort: progress >= 75% AND required_pts_per_min > "
                "league_avg * 1.04 (strict), one observation per game (the one "
                "closest to 75%), authoritative settled finals only.",
                "Bar (league_average_pace) is the whole-archive mean realised "
                "pace of that SAME competition's settled OK finals; the live "
                "alert uses a point-in-time reference, so borderline members "
                "can differ.",
                "Segmented by competition_slug, never by classification: "
                "BETUAL_NBA bundles five competitions (NBA, TBSL, EuroLeague, "
                "CBA, KBL) and grouping by it hides members (audited in "
                "blm_v4/live_analytics/league.py).",
                "Wilson 95% intervals: a league whose lower bound is not above "
                "50% is NOT distinguishable from a coin flip at this sample "
                "size.",
                "Fingerprints are RECORDED context. They gate nothing; the "
                "alert fires on the required-pace rule alone.",
                "This cohort is the fingerprints' DISCOVERY dataset, so their "
                "rates are in-sample. Out-of-sample durability is not tested "
                "here.",
            ],
        }
    finally:
        conn_p.close()
        conn_c.close()


# ── process-level TTL cache ────────────────────────────────────────────────
_STATS_CACHE: dict[str, tuple] = {}
_STATS_TTL_S = 120.0
_STATS_LOCK = threading.Lock()


def get_stats(prod_path: Path, clean_path: Path, ttl_s: float = _STATS_TTL_S,
              force: bool = False) -> dict:
    """``compute_stats`` behind a TTL cache (SYNCHRONOUS).

    For tests and CLI use.  HTTP callers must use :class:`StatsWorker` instead:
    the computation is a full-history scan (~30 s cold on this box) and a
    request must never block on it.

    Concurrent first-callers are serialised by the lock so a burst of polls
    triggers ONE scan, not N.  A failed scan is NOT cached — the next call
    retries rather than serving a stale error forever.
    """
    key = f"{prod_path}|{clean_path}"
    now = time.monotonic()
    hit = _STATS_CACHE.get(key)
    if hit and not force and (now - hit[0]) < ttl_s:
        return hit[1]
    with _STATS_LOCK:
        hit = _STATS_CACHE.get(key)
        if hit and not force and (time.monotonic() - hit[0]) < ttl_s:
            return hit[1]                # another thread just computed it
        stats = compute_stats(prod_path, clean_path)
        _STATS_CACHE[key] = (time.monotonic(), stats)
        return stats


class StatsWorker:
    """Recompute the stats payload on a timer, off the request path.

    WHY A WORKER AND NOT A TTL CACHE IN THE ROUTE.  ``compute_stats`` is a
    full-history scan: measured ~30 s cold on this box, dominated by
    ``fingerprint_c5.q3_pace_reference`` grouping the ~1.76 M-row ``snapshots``
    table.  Even at a 120 s TTL that would stall a dashboard request for half a
    minute every two minutes, and the tab is polled every 5 s.  So the
    computation runs on its own daemon thread and the route only ever READS the
    last completed payload.

    The API therefore stays responsive no matter how slow the scan is, and the
    payload updates continuously as new games settle — which is the point of the
    tab.  Failure-isolated like the repo's other workers (SettleWorker,
    ResultReconcilerWorker): a failed scan logs and retries next tick, leaving
    the previous payload in place; it never blanks the tab.

    ``interval_s`` is the recompute cadence.  It is deliberately much longer
    than the TTL of the module-level reference caches (300 s) would suggest is
    needed — those are per-database and cheap once warm; the cost here is the
    scan, so the cadence is what bounds CPU.
    """

    def __init__(self, prod_path: Path, clean_path: Path,
                 interval_s: float = 60.0, log: Any = None,
                 max_age_s: float = 900.0) -> None:
        self._prod = prod_path
        self._clean = clean_path
        self._interval_s = max(5.0, float(interval_s))
        #: Backstop: rescan anyway once the payload is this old, even if the
        #: dirty key has not moved.  Guarantees the tab can never sit on a
        #: payload older than this, whatever the change-detector does.
        self._max_age_s = max(60.0, float(max_age_s))
        self._log = log
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._latest: Optional[dict] = None
        self._latest_at: float = 0.0
        self._last_error: Optional[str] = None
        self._last_key: Optional[tuple] = None
        self._runs = 0
        self._skips = 0
        self._failures = 0

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> "StatsWorker":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._loop,
                                        name="blm-stats-worker", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._run_once()
            self._stop.wait(self._interval_s)

    def should_scan(self) -> tuple[bool, str]:
        """(scan?, why).  Cheap: one indexed aggregate (~6 ms measured).

        Skips the expensive full-history scan when the settled set is
        unchanged AND the payload is still within ``max_age_s`` — the common
        case, since the cohort only moves when a game settles.
        """
        with self._lock:
            have = self._latest is not None
            age = time.monotonic() - self._latest_at if have else None
            prev = self._last_key
        if not have:
            return True, "first_run"
        if age is not None and age >= self._max_age_s:
            return True, "max_age"
        try:
            key = _dirty_key(self._prod)
        except sqlite3.Error as exc:
            return True, f"key_unavailable:{type(exc).__name__}"
        if key != prev:
            return True, "settled_changed"
        return False, "unchanged"

    def _run_once(self, force: bool = False) -> None:
        if not force:
            scan, why = self.should_scan()
            if not scan:
                with self._lock:
                    self._skips += 1
                return
        else:
            why = "forced"
        t0 = time.monotonic()
        try:
            key = _dirty_key(self._prod)
        except sqlite3.Error:
            key = None
        try:
            stats = compute_stats(self._prod, self._clean)
        except Exception as exc:                      # noqa: BLE001
            self._failures += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            if self._log is not None:
                self._log.warning("stats_run_failed", error=self._last_error)
            return
        with self._lock:
            self._latest = stats
            self._latest_at = time.monotonic()
            self._last_error = None
            self._last_key = key
        self._runs += 1
        if self._log is not None:
            self._log.info("stats_run",
                           elapsed_s=round(time.monotonic() - t0, 1),
                           reason=why,
                           cohort_n=(stats.get("cohort") or {}).get("n"))

    # ── read side (never blocks, never computes) ────────────────────────
    def latest(self) -> dict:
        """The last completed payload, or a warming stub.

        ``stale`` is informational: the payload carries its own ``as_of`` and
        ``age_s``, so the frontend can show how current the numbers are without
        the backend ever withholding a usable payload.
        """
        with self._lock:
            payload = self._latest
            at = self._latest_at
            err = self._last_error
            runs, fails, skips = self._runs, self._failures, self._skips
        if payload is None:
            return {
                "status": "warming",
                "message": ("first stats scan in progress — the full-history "
                            "scan takes about half a minute"),
                "runs": runs, "failures": fails, "skips": skips, "last_error": err,
                "interval_s": self._interval_s,
                "max_age_s": self._max_age_s,
            }
        out = dict(payload)
        out["status"] = "ok"
        out["age_s"] = round(time.monotonic() - at, 1)
        out["stale"] = bool(err)
        out["runs"] = runs
        out["failures"] = fails
        #: Scans avoided because no new game had settled.  Surfaced so the
        #: saving is auditable from the dashboard rather than a claim.
        out["skips"] = skips
        if err:
            out["last_error"] = err
        out["interval_s"] = self._interval_s
        out["max_age_s"] = self._max_age_s
        return out
