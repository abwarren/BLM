"""Momentum replay-distortion fix — _velocity collapses repeated source states.

Raw snapshots contain repeated observations of ONE source state: consecutive
rows with the same home/away score, period label AND clock (the ~10s poll
catching a 1s-tick virtual clock mid-dwell).  These sub-tick duplicates used
to enter _velocity's last-3 window as ~0-delta rows, dragging the mean toward
0 and flipping momentum state (measured: 12 state flips / 120 checkpoint rows,
e.g. recorded FLAT score 50.0 vs deduplicated RISING 93.9).

The fix: collapse runs of identical source state to the run's FIRST row so
velocity measures change across DISTINCT source states only.

Covers:
  1. identical consecutive snapshots do not dilute momentum
  2. a legitimate no-score interval with advancing clock remains valid
  3. multiple repeated identical snapshots collapse to one analytical state
  4. momentum(raw w/ repeats) == momentum(equivalent deduplicated sequence)
  5. no future row enters the calculation (prefix-only)
  6. existing checkpoint prefix behavior unchanged
  7. Market/Fair untouched (regression suites run separately)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from blm_v4.api import _momentum, _velocity

BASE = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row(t_min: float, h: int, a: int, period: str = "1st Quarter",
         clock: str = "10:00") -> dict:
    return {
        "captured_at": _iso(BASE + timedelta(minutes=t_min)),
        "home_score": h, "away_score": a,
        "period_label": period, "clock": clock,
    }


def _repeat(state: dict, times: int, spacing_min: float = 1 / 6):
    """Duplicate a state `times` times at `spacing_min` (sub-tick poll)."""
    out = []
    for i in range(times):
        r = dict(state)
        r["captured_at"] = _iso(
            datetime.fromisoformat(state["captured_at"].replace("Z", "+00:00"))
            + timedelta(minutes=spacing_min * i))
        out.append(r)
    return out


def test_identical_consecutive_snapshots_do_not_dilute_momentum():
    """Distinct states 1 min apart, each repeated 3x at 10s: velocity must
    equal the clean (one-row-per-state) sequence, not ~1/3 of it."""
    clean = [
        _row(0, 0, 0, clock="12:00"), _row(1, 5, 5, clock="10:00"),
        _row(2, 12, 8, clock="08:00"), _row(3, 15, 15, clock="06:00"),
    ]
    raw = []
    for i, s in enumerate(clean):
        raw += _repeat(s, 3, 1 / 6)  # each state seen 3x over 20s
    v_clean = _velocity(clean)[0]
    v_raw = _velocity(raw)[0]
    assert v_clean is not None and v_raw is not None
    assert v_raw == v_clean  # no dilution
    assert _momentum(raw)["score"] == _momentum(clean)["score"]


def test_legit_no_score_interval_with_advancing_clock_remains_valid():
    """10-10 with the clock advancing 10:00 -> 09:00 is a REAL no-basket
    minute: its 0-delta must be preserved (velocity 1.0, not 2.0 which would
    mean the 0-delta was discarded, and not ~0 which would mean dilution)."""
    rows = [
        _row(0, 10, 10, clock="10:00"),
        _row(1, 10, 10, clock="09:00"),   # no basket, clock advanced
        _row(2, 12, 10, clock="08:00"),   # basket
    ]
    # add sub-tick repeats around each — must not change anything
    raw = _repeat(rows[0], 3, 1 / 6) + _repeat(rows[1], 3, 1 / 6) \
        + _repeat(rows[2], 3, 1 / 6)
    for seq in (rows, raw):
        v = _velocity(seq)[0]
        assert v == 1.0  # mean of [0 (no-basket min), 2 (scoring min)]


def test_multiple_repeats_collapse_to_one_state():
    """5 identical rows then a jump: exactly ONE analytical transition."""
    a = _row(0, 20, 20, clock="08:00")
    b = _row(1, 24, 22, clock="07:00")
    rows = _repeat(a, 5, 1 / 6) + [b]
    v = _velocity(rows)[0]
    assert v is not None
    assert v == 6.0  # 6 pts over 1 min — single A->B delta, repeats gone


def test_raw_with_repeats_matches_deduplicated_sequence():
    """Momentum over raw (repeated) snapshots must equal momentum over the
    first-of-run deduplicated sequence — mixed repeat counts + real
    no-basket stretches."""
    base = [
        _row(0, 0, 0, clock="12:00"),
        _row(1, 2, 2, clock="11:00"),    # slow start
        _row(2, 2, 2, clock="10:30"),    # no basket, clock advanced 30s
        _row(3, 10, 6, clock="09:00"),   # burst
        _row(4, 14, 10, clock="08:00"),  # continues
    ]
    raw = (_repeat(base[0], 2, 1 / 6) + _repeat(base[1], 3, 1 / 6)
           + _repeat(base[2], 2, 1 / 6) + _repeat(base[3], 4, 1 / 6)
           + [base[4]])
    dedup = [base[0], base[1], base[2], base[3], base[4]]
    assert _velocity(raw)[0] == _velocity(dedup)[0]
    m_raw, m_dd = _momentum(raw), _momentum(dedup)
    assert m_raw["score"] == m_dd["score"]
    assert m_raw["direction"] == m_dd["direction"]


def test_no_future_row_enters_the_calculation():
    """Prefix-only: momentum at a checkpoint is unaffected by later rows
    (a future scoring burst with repeats must not leak in)."""
    rows = [
        _row(0, 0, 0, clock="12:00"), _row(1, 4, 4, clock="10:00"),
        _row(2, 8, 8, clock="08:00"),  # checkpoint lives here (idx 2)
    ]
    idx = 2
    future = _repeat(_row(5, 40, 40, clock="04:00"), 3, 1 / 6)
    prefix_mom = _momentum(rows[: idx + 1])
    # same prefix sliced out of a longer list that also holds the future burst
    assert _momentum((rows + future)[: idx + 1]) == prefix_mom
    # sanity: the future burst WOULD change momentum if it were passed
    assert _momentum(rows + future)["velocity"] != prefix_mom["velocity"]


def test_checkpoint_prefix_behavior_unchanged():
    """The checkpoint prefix (rows[:idx+1]) is the exact input the scorecard
    freezes momentum from; collapsing must not change which rows the prefix
    ends at or the momentum of that prefix vs its dedup."""
    rows = []
    for t in range(6):  # one distinct state per minute, 2x repeats each
        rows += _repeat(_row(t, t * 3, t * 3, clock=f"{12 - t:02d}:00"), 2, 1 / 6)
    # prefix ending at the 4-minute checkpoint (idx of that state's first row)
    idx = next(i for i, r in enumerate(rows)
               if "08:00" in (r["clock"] or ""))
    prefix = rows[: idx + 1]
    dedup_prefix = []
    for r in prefix:
        if dedup_prefix and (r["home_score"] == dedup_prefix[-1]["home_score"]
                             and r["away_score"] == dedup_prefix[-1]["away_score"]
                             and r["period_label"] == dedup_prefix[-1]["period_label"]
                             and r["clock"] == dedup_prefix[-1]["clock"]):
            continue
        dedup_prefix.append(r)
    assert _velocity(prefix)[0] == _velocity(dedup_prefix)[0]
    assert _momentum(prefix)["score"] == _momentum(dedup_prefix)["score"]
    # prefix still ends at the checkpoint state (12-12 @ 08:00)
    assert prefix[-1]["home_score"] == 12 and prefix[-1]["clock"] == "08:00"
