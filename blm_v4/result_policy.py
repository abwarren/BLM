"""
BLM V4 — RESULTS-PAGE AUTHORITY POLICY (directive 2026-09-24).

ONE definition of "a verified final result", shared by the three writers
that touch ``game_results``:

  * ``blm_v4/result_reconciler.py``  — WRITES a page-verified verdict
  * ``blm_v4/settle_worker.py``      — must never destroy one
  * ``blm_v4/scorecard.py``          — must never destroy one

Why this module exists (directive §"IMPORTANT — RESULTS PAGE IS
AUTHORITATIVE"):

  A disappearing live market is NOT evidence that a game has no result.
  The results page is the AUTHORITATIVE fallback.  Two independent
  defects were proven live on 2026-09-24 (read-only forensics, all numbers
  reproducible from blm_pokerbet.db):

  1. THE CLOBBER.  Both re-derivation paths (``settle_worker._settle_game``
     and ``Scorecard.capture_results``) treated a results-page verdict as
     merely a suggestion: when the game's own captured endpoint disagreed
     with the stored page final, they LIFTED the protection, re-derived
     from snapshots and wrote ``final_home/away/total = NULL`` with
     ``UNKNOWN`` — leaving ``result_source='RESULTS_PAGE'`` behind as a
     stale lie.  ``mark_needs_reconciliation`` then flipped the row to
     ``NEEDS_RECONCILIATION``, and because ``result_reconciliation_state``
     still said ``outcome='VERIFIED'``, ``candidate_games()`` excluded the
     game forever: the verified final was unreachable and the game never
     retried.  Measured: 2,592 of 2,907 page-verified games had NO OK row
     (479 of them collected that same day).

  2. THE IMPOSSIBLE FINAL.  Verification proved identity from TEAM NAMES
     only.  Betual/Cyber virtuals replay the same fixture back-to-back and
     the SPA silently keeps the previously rendered scoreboard when the
     requested game id does not resolve, so a *different* instance of the
     same fixture passed identity and was written as this game's final.
     Measured: 739 of 2,907 page-verified results (25.4%) have a stored
     final total LOWER than a total already observed live in that same
     game's snapshot history — arithmetically impossible for one fixture
     (scores are monotonic), 270 of them currently sitting in
     ``game_results`` as OK and therefore inside the performance
     statistics.

The guards implemented here:

  RESULT AUTHORITY — a ``result_source='RESULTS_PAGE'`` row with
  ``final_result_status='OK'`` is VERIFIED and IMMUTABLE against
  snapshot-derived re-derivation, always.  A disagreeing captured endpoint
  is FLAGGED (``result_conflicts``), never overwritten: a flag is a job for
  a human, a NULL is lost evidence.

  RESULT VALIDITY — a page render is accepted as a final ONLY when
    (a) the team block proves the same fixture (caller's identity policy),
    (b) the render is COMPLETE — all four quarters present
        (``parse_quality == 'full'``); a completed 4-quarter basketball
        game always renders four quarters, so a 2- or 3-quarter rendering
        is a mid-game or foreign-instance frame, never a final,
    (c) the quarter scores SUM to the compact final line, and
    (d) the final is CONSISTENT WITH THE CAPTURED HISTORY: neither side
        may be lower than a score already observed for this game
        (scores never decrease within a fixture).  This is the guard that
        rejects the 739 impossible finals.

Nothing in this module touches the database.  It is pure policy so all
three writers share exactly one definition, and it is unit-testable
without a fixture.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

#: provenance vocabulary (mirrors result_reconciler.SRC_*
SOURCE_RESULTS_PAGE = "RESULTS_PAGE"
STATUS_OK = "OK"

#: ``result_conflicts`` — FLAGGED disagreements between sources.  Defined
#: here because three modules write/read it; ``result_reconciler.SCHEMA``
#: embeds this same DDL (never a second copy).
CONFLICTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS result_conflicts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_game_id    TEXT NOT NULL,
    classification    TEXT,
    live_dom_total    INTEGER,                -- stored verdict's total
    results_total     INTEGER,                -- results-page rendered total
    detail            TEXT NOT NULL DEFAULT '',
    flagged_at        TEXT NOT NULL,
    UNIQUE(source_game_id)
);
"""

#: the parse quality that proves a COMPLETED render.
PARSE_QUALITY_COMPLETE = "full"


# ── the row-level authority predicate ────────────────────────────────

def _get(row: Any, key: str, default: Any = None) -> Any:
    """Column access that works for sqlite3.Row, dict and None rows."""
    if row is None:
        return default
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def is_results_page_verdict(row: Any) -> bool:
    """True when a ``game_results`` row IS a page-verified final.

    The single predicate every writer must agree on: provenance is
    ``RESULTS_PAGE`` AND the verdict status is ``OK``.  A row whose status
    is anything else is NOT a verdict, whatever provenance it carries
    (a failed attempt must not borrow authority from a stale column).
    """
    return (_get(row, "result_source") == SOURCE_RESULTS_PAGE
            and _get(row, "final_result_status") == STATUS_OK)


def is_authoritative_verdict(row: Any) -> bool:
    """Backwards-compatible alias — the verdict that may never be
    overwritten by a snapshot-derived re-derivation."""
    return is_results_page_verdict(row)


# ── the captured-history bound ───────────────────────────────────────

def history_pairs(rows: Iterable[Any]) -> list[tuple[int, int]]:
    """The scored ``(home, away)`` pairs of a snapshot history, in the
    order given (skipping rows with a missing side)."""
    out: list[tuple[int, int]] = []
    for r in rows or ():
        h, a = _get(r, "home_score"), _get(r, "away_score")
        if h is None or a is None:
            continue
        out.append((int(h), int(a)))
    return out


def history_bound(rows: Iterable[Any]) -> Optional[tuple[int, int]]:
    """``(max_home, max_away)`` ever observed for this game, or None when
    the history carries no scored row.

    Scores are monotonic inside a fixture: a basketball period total never
    decreases, so a final can never be BELOW a score already observed for
    the same fixture.  This is the comparator that catches a foreign
    instance's scoreboard.
    """
    pairs = history_pairs(rows)
    if not pairs:
        return None
    return (max(p[0] for p in pairs), max(p[1] for p in pairs))


def history_endpoint(rows: Iterable[Any]) -> Optional[tuple[int, int]]:
    """The LAST scored observation (the captured endpoint), or None."""
    pairs = history_pairs(rows)
    return pairs[-1] if pairs else None


def history_endpoint_total(rows: Iterable[Any]) -> Optional[int]:
    """The captured endpoint's total (home + away), or None.

    The "live DOM" side of a flagged conflict: what the game's own
    captured history last reported, against the page's verified final.
    """
    ep = history_endpoint(rows)
    return (ep[0] + ep[1]) if ep else None


def history_disagrees(rows: Iterable[Any],
                      home: Optional[int],
                      away: Optional[int]) -> bool:
    """True when any scored observation differs from ``(home, away)``.

    Used to decide whether a captured endpoint CONTRADICTS a stored
    verdict (the conflict-flag trigger).  A missing side on either input
    is not a disagreement (nothing to compare).
    """
    if home is None or away is None:
        return False
    hh, aa = int(home), int(away)
    return any(p != (hh, aa) for p in history_pairs(rows))


# ── page-result validation (the directive's "validate score") ────────

def validate_page_result(parsed: dict[str, Any],
                         history_rows: Optional[Sequence[Any]] = None
                         ) -> dict[str, Any]:
    """Validate ONE parsed results-page render; pure, never raises.

    Returns ``{"passed": bool, "checks": {...}, "failures": [str, ...]}``
    with named, reportable reasons.  The caller's identity policy (team
    names / start time) is a separate, prior gate — this function only
    decides whether the rendered SCORE may be believed.
    """
    checks: dict[str, Any] = {}
    failures: list[str] = []

    home, away = parsed.get("home_score"), parsed.get("away_score")
    checks["has_scoreboard"] = home is not None and away is not None
    if home is None or away is None:
        failures.append("no scoreboard rendered")
        return {"passed": False, "checks": checks, "failures": failures}
    hh, aa = int(home), int(away)          # narrowed by the guard above

    # (b) a COMPLETED render carries all four quarters.
    pq = parsed.get("parse_quality")
    checks["parse_quality"] = pq
    checks["render_complete"] = (pq == PARSE_QUALITY_COMPLETE)
    quarters = parsed.get("quarter_scores") or []
    checks["quarter_count"] = len(quarters)
    if not checks["render_complete"]:
        failures.append(
            f"incomplete render: {len(quarters)} quarter(s) "
            f"(parse_quality={pq!r}) — not a finished game's scoreboard")

    # (c) the quarters must SUM to the compact final line.
    if quarters:
        qh = sum(int(q[0]) for q in quarters)
        qa = sum(int(q[1]) for q in quarters)
        checks["quarter_sum_home"] = qh
        checks["quarter_sum_away"] = qa
        checks["page_sums_valid"] = (qh == hh and qa == aa)
        if not checks["page_sums_valid"]:
            failures.append(
                f"quarter scores {qh}:{qa} do not sum to the rendered "
                f"final {hh}:{aa}")
    else:
        checks["page_sums_valid"] = None

    # (d) consistency with what this game was already observed to score.
    bound = history_bound(history_rows) if history_rows is not None else None
    checks["observed_max_home"] = bound[0] if bound else None
    checks["observed_max_away"] = bound[1] if bound else None
    checks["observed_snapshots"] = len(history_pairs(history_rows or ()))
    if bound is None:
        # no captured history at all — the check is INAPPLICABLE (a game
        # the collector never scored; the render stands on its own)
        checks["history_consistent"] = None
    else:
        checks["history_consistent"] = (hh >= bound[0] and aa >= bound[1])
        if not checks["history_consistent"]:
            failures.append(
                f"impossible final {hh}:{aa} — the game was already "
                f"observed at {bound[0]}:{bound[1]} (a final can never be "
                f"lower; this render is another fixture or a mid-game frame)")

    return {"passed": not failures, "checks": checks, "failures": failures}


# ── conflict flagging (shared writer for result_conflicts) ───────────

def flag_conflict(conn, game: Any,
                  stored_total: Optional[int],
                  page_total: Optional[int],
                  detail: str,
                  *,
                  flagged_at: str) -> None:
    """Upsert ONE flagged disagreement — never overwrites a verdict.

    ``result_conflicts`` is keyed UNIQUE(source_game_id): a re-flagged
    disagreement updates the same row rather than duplicating it, and a
    resolved one is never auto-deleted (a human closes it).  ``game`` may
    be a dict or a sqlite3.Row (the three writers hold both shapes).
    """
    conn.execute(CONFLICTS_SCHEMA)
    conn.execute(
        """INSERT INTO result_conflicts (
               source_game_id, classification, live_dom_total,
               results_total, detail, flagged_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(source_game_id) DO UPDATE SET
               live_dom_total = excluded.live_dom_total,
               results_total = excluded.results_total,
               detail = excluded.detail,
               flagged_at = excluded.flagged_at""",
        (_get(game, "source_game_id"), _get(game, "classification") or "",
         stored_total, page_total, detail, flagged_at))
