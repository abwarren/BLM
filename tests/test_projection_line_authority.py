"""CANONICAL TRIGGER-LINE STORE (ruling 2026-09-20) — regression tests.

The 218-flip audit found the snapshots series and the pace projector's
``clean_projections`` store carrying DIFFERENT lines at the same boundary
instant (121 of 179 verified flips).  Ruling: ``clean_projections`` is the
canonical trigger-line store.

Contract pinned here:

  * ``trigger_observation(..., projection_rows=...)`` — the canonical
    store's last non-null ``live_total_line`` AT OR BEFORE the boundary IS
    the trigger line; the boundary itself (progress, captured_at) stays
    the SNAPSHOT observation's, one game-time geometry for everyone;
  * FALLBACK — a projector gap (no rows, no boundary coverage, no line by
    the boundary, malformed rows) never unproves a trigger the snapshots
    can prove: the snapshot series keeps its original authority;
  * no feed at all → behaviour byte-identical to the pre-ruling module;
  * the FINAL side of every verdict is untouched — ``game_results``
    (settled) and terminal observations remain the final authorities.
"""
from __future__ import annotations

from blm_v4.live_analytics.under_outcome import (
    trigger_market_total,
    trigger_observation,
    under_alert_outcome,
)


def _snap(minutes: float, *, line: float | None, hs: int, as_: int,
          status: str = "live") -> dict:
    """A snapshots-series row — the same shape every existing suite uses
    (BETUAL_NBA: 10-minute quarters, 40-minute regulation)."""
    q = min(4, int(minutes // 10) + 1)
    return {
        "classification": "BETUAL_NBA",
        "captured_at": f"2026-09-20T19:{int(minutes):02d}:00.000Z",
        "quarter": q, "clock": "00:00" if minutes >= 40 else "05:00",
        "period_label": f"{q}th Quarter" if status == "live" else "Finished",
        "game_status": status, "total_line": line,
        "home_score": hs, "away_score": as_,
    }


def _proj(minutes: float, *, line: float | None,
          progress_pct: float | None = None) -> dict:
    """A clean_projections row — only the fields the line authority reads.
    progress_pct defaults to the true percent scale (minutes of the 40-
    minute BETUAL_NBA regulation): 31 game-minutes is 77.5%, not 31%."""
    return {
        "captured_at": f"2026-09-20T19:{int(minutes):02d}:00.000Z",
        "live_total_line": line,
        "progress_pct": (progress_pct if progress_pct is not None
                         else minutes / 40.0 * 100.0),
        "elapsed_game_minutes": minutes,
        "classification": "BETUAL_NBA",
    }


# The running example of the audit: the two stores disagree at the SAME
# boundary instant (snapshots 180.5 vs clean_projections 178.5).
def _snapshot_rows():
    return [
        _snap(2.0, line=175.5, hs=4, as_=3),
        _snap(11.0, line=180.5, hs=24, as_=20),    # crosses 25% here
        _snap(21.0, line=182.5, hs=45, as_=40),
        _snap(31.0, line=181.5, hs=66, as_=58),    # crosses 75% here
        _snap(40.0, line=184.0, hs=93, as_=91, status="ended"),
    ]


def _projection_rows():
    return [
        _proj(2.0, line=174.5),
        _proj(11.0, line=178.5),                   # canonical says 178.5
        _proj(21.0, line=179.5),
        _proj(31.0, line=177.5),                   # canonical says 177.5
        _proj(40.0, line=183.0),
    ]


# ── precedence ──────────────────────────────────────────────────────────

def test_projection_rows_override_snapshot_line():
    oc = trigger_observation(_snapshot_rows(), 75, "BETUAL_NBA",
                             projection_rows=_projection_rows())
    # canonical line IN FORCE at the boundary: the last projector line
    # at-or-before 87.5% progress — the 31-min row's 177.5 — not the
    # snapshot series' 181.5 and not the store's later 40-min line
    assert oc["total_line"] == 177.5
    # the boundary itself stays the SNAPSHOT observation's
    assert oc["progress"] == 0.875            # 35 / 40 regulation minutes
    assert oc["captured_at"] == "2026-09-20T19:31:00.000Z"


def test_projection_line_at_earlier_checkpoint():
    oc = trigger_observation(_snapshot_rows(), 25, "BETUAL_NBA",
                             projection_rows=_projection_rows())
    assert oc["total_line"] == 178.5          # last store line <= 37.5%
    assert oc["progress"] == 0.375            # 15 / 40


def test_at_or_before_semantics_match_snapshots():
    """A line first observed AFTER the boundary never leaks in — the same
    at-or-before rule as the snapshot authority."""
    late = [_proj(2.0, line=None), _proj(11.0, line=None),
            _proj(21.0, line=None), _proj(36.0, line=190.0)]  # 90% > 87.5%
    oc = trigger_observation(_snapshot_rows(), 75, "BETUAL_NBA",
                             projection_rows=late)
    # no canonical line by the boundary → snapshot fallback
    assert oc["total_line"] == 181.5


# ── fallback: a projector gap never unproves a snapshot-provable trigger ─

def test_no_projection_rows_falls_back_to_snapshots():
    oc = trigger_observation(_snapshot_rows(), 75, "BETUAL_NBA",
                             projection_rows=[])
    assert oc["total_line"] == 181.5


def test_projection_series_ending_before_boundary_still_supplies_line():
    """A store whose rows END before the boundary still owns the line in
    force there — its last captured line IS the canonical read.  This is
    not a gap: the bookmaker line persists between captures."""
    early = _projection_rows()[:2]            # ends at 27.5%, line 178.5
    oc = trigger_observation(_snapshot_rows(), 75, "BETUAL_NBA",
                             projection_rows=early)
    assert oc["total_line"] == 178.5


def test_projection_rows_only_after_boundary_fall_back():
    """A store with NO row at-or-before the boundary is a gap: the
    snapshots decide."""
    late_only = [_proj(36.0, line=190.0)]     # 90% > 87.5% boundary
    oc = trigger_observation(_snapshot_rows(), 75, "BETUAL_NBA",
                             projection_rows=late_only)
    assert oc["total_line"] == 181.5


def test_projection_no_line_by_boundary_falls_back():
    all_null = [_proj(m, line=None) for m in (2.0, 11.0, 21.0, 31.0, 36.0)]
    oc = trigger_observation(_snapshot_rows(), 75, "BETUAL_NBA",
                             projection_rows=all_null)
    assert oc["total_line"] == 181.5


def test_malformed_projection_rows_fail_closed_to_snapshots():
    """Rows with neither progress_pct nor elapsed_game_minutes can never
    extend the line — the store is unusable, the snapshots decide."""
    malformed = [{"live_total_line": 150.0, "captured_at": "x"}]
    oc = trigger_observation(_snapshot_rows(), 75, "BETUAL_NBA",
                             projection_rows=malformed)
    assert oc["total_line"] == 181.5


def test_elapsed_game_minutes_fallback_progress():
    """progress_pct absent → progress recomputed from elapsed minutes and
    the classification's regulation length (the shared authority)."""
    rows = [_proj(31.0, line=177.5, progress_pct=None)]
    oc = trigger_observation(_snapshot_rows(), 75, "BETUAL_NBA",
                             projection_rows=rows)
    assert oc["total_line"] == 177.5


def test_no_line_anywhere_is_unprovable():
    bare = [_snap(31.0, line=None, hs=66, as_=58)]
    oc = trigger_observation(bare, 75, "BETUAL_NBA",
                             projection_rows=[_proj(31.0, line=None)])
    assert oc["total_line"] is None


# ── no feed = the pre-ruling contract, unchanged ─────────────────────────

def test_without_feed_behaviour_is_byte_identical():
    assert trigger_observation(_snapshot_rows(), 75, "BETUAL_NBA") \
        == trigger_observation(_snapshot_rows(), 75, "BETUAL_NBA",
                               projection_rows=None)
    assert trigger_market_total(_snapshot_rows(), 25, "BETUAL_NBA") == 180.5


# ── the verdict block: canonical line, untouched final side ──────────────

def test_outcome_block_settles_against_canonical_lines():
    oc = under_alert_outcome(_snapshot_rows(), "BETUAL_NBA",
                             settled=(181.0, "2026-09-20T21:00:00Z"),
                             projection_rows=_projection_rows())
    assert oc["final_source"] == "settled"          # final side unchanged
    assert oc["by_checkpoint"][75]["trigger_total"] == 177.5
    assert oc["by_checkpoint"][75]["status"] == "over"      # 181 > 177.5
    # same canonical line, same verdict — Q3_BREAK shares the 75% boundary
    assert oc["by_checkpoint"]["Q3_BREAK"]["trigger_total"] == 177.5
    assert oc["by_checkpoint"]["Q3_BREAK"]["status"] == "over"


def test_outcome_block_falls_back_to_snapshots_when_store_empty():
    oc = under_alert_outcome(_snapshot_rows(), "BETUAL_NBA",
                             settled=(180.0, "x"), projection_rows=[])
    assert oc["by_checkpoint"][75]["trigger_total"] == 181.5
    assert oc["by_checkpoint"][75]["status"] == "under"     # 180 < 181.5


def test_outcome_block_without_feed_unchanged():
    a = under_alert_outcome(_snapshot_rows(), "BETUAL_NBA",
                            settled=(180.0, "x"))
    b = under_alert_outcome(_snapshot_rows(), "BETUAL_NBA",
                            settled=(180.0, "x"), projection_rows=None)
    assert a == b
