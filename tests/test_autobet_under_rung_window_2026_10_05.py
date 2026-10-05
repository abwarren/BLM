"""UNDER EXECUTION WINDOW — the operator's lettered compliance suite
(2026-10-05 directive §10 A..P).

The rule ALREADY EXISTS and this suite does NOT re-implement it: every
assertion drives the canonical implementations —

    blm_v4/betting/rung.py       the ONE rung arithmetic + the ONE UNDER
                                 execution decision (rungs_moved >= -1)
    blm_v4/betting/precheck.py   the §4 pre-bet gate (line / odds / status /
                                 stake / duplicate) that owns every NON-rung
                                 refusal
    blm_v4/betting/position.py   the record that keeps trigger_line,
                                 executable_line and executable_odds separate

The BLM trigger model is untouched: nothing here calls, restates or adjusts
``under_alert_state`` or its condition.  The window is an EXECUTION rule about
an ALREADY-GENERATED signal.

Allowed UNDER window:  current_line >= trigger_line - 1 rung  (no upper bound)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blm_v4.betting import rung as R  # noqa: E402
from blm_v4.betting.position import position_from_command  # noqa: E402
from blm_v4.betting.precheck import (  # noqa: E402
    PrecheckLimits,
    run_precheck,
)

TRIGGER = 182.5
#: the LIVE market's rung, DERIVED from observed lines (never hard-coded):
#: the BETUAL virtual totals quote 1.0-point rungs (182.5, 181.5, ...) —
#: see test_autobet_rung_wiring_2026_10_03.py::test_pw06_*_1_0_tick.
LINES_1_0 = [178.5, 179.5, 180.5, 181.5, 182.5, 183.5, 184.5]


def game(current, *, trigger=TRIGGER, observed=LINES_1_0, market="TOTAL",
         selection="UNDER", alert=True):
    """The execution payload both producers build (manual + autonomous)."""
    return {
        "game_id": "31124212",
        "market": {"total_line": current, "observed_lines": observed},
        "under_alert": ({"trigger_line": trigger, "alert_id": "31124212|75",
                         "trigger_progress": 75.0,
                         "trigger_captured_at": "2026-10-05T10:13:27Z"}
                        if alert else None),
    }


def call(current, **kw):
    g = game(current, **kw)
    return R.validate_execution(g, market=kw.get("market", "TOTAL"),
                                selection=kw.get("selection", "UNDER"))


# ═══════════ A–F. the window itself (rung.py, canonical) ══════════════

def test_a_trigger_line_itself_allows():
    out = call(TRIGGER)
    assert out["decision"] == R.ALLOW and out["rungs_moved"] == 0.0


def test_b_one_rung_below_allows():
    out = call(181.5)
    assert out["decision"] == R.ALLOW and out["rungs_moved"] == -1.0


def test_c_two_rungs_below_rejects():
    out = call(180.5)
    assert out["decision"] == R.REJECT
    assert out["rungs_moved"] == -2.0
    assert out["reason"] == R.R_MOVED_TOO_FAR == "more_than_one_rung_down"


def test_d_one_rung_above_allows():
    out = call(183.5)
    assert out["decision"] == R.ALLOW and out["rungs_moved"] == 1.0


def test_e_multiple_rungs_above_allows():
    out = call(187.5)
    assert out["decision"] == R.ALLOW and out["rungs_moved"] == 5.0


def test_f_large_upward_movement_allows():
    """No upper bound: 182.5 -> 200.5 is +18 rungs and still ALLOW."""
    out = call(200.5)
    assert out["decision"] == R.ALLOW and out["rungs_moved"] == 18.0


def test_the_window_is_one_sided_only():
    """Down is bounded at exactly -1; up is unbounded."""
    below = [call(TRIGGER - s) for s in (1.0, 1.5)]
    assert [o["decision"] for o in below] == [R.ALLOW, R.REJECT]
    above = [call(TRIGGER + s * 1.0)["decision"] for s in range(1, 40)]
    assert set(above) == {R.ALLOW}


# ═══════════ G. trigger_line is immutable across the window ══════════

def _command(current, **kw):
    """The shared command shape both producers emit
    (``command.build_command``): the canonical rung verdict NESTED under
    ``"rung"``."""
    out = R.validate_execution(game(current, **kw))
    return {"decision": out["decision"], "rung": out, "game_id": "31124212",
            "checkpoint": 75, "market": "TOTAL", "selection": "UNDER",
            "stake": {"stake": 10.0}, "alert_id": out["alert_id"],
            "source": "test", "mode": "DRY_RUN"}


def test_g_trigger_line_never_becomes_the_executable_line():
    cmd = _command(187.5)
    pos = position_from_command(cmd, executable_line=187.5,
                                executable_odds=1.62)
    assert cmd["rung"]["trigger_line"] == TRIGGER  # frozen reference
    assert pos.trigger_line == TRIGGER             # never overwritten
    assert pos.executable_line == 187.5            # the live bookmaker line
    assert pos.executable_line != pos.trigger_line
    assert pos.executable_odds == 1.62             # DECIMAL ODDS, separate


def test_g_market_moving_never_moves_the_trigger_in_the_command():
    for current in (181.5, 182.5, 187.5, 200.5):
        assert _command(current)["rung"]["trigger_line"] == TRIGGER


# ═══════════ H. the rung delta is deterministic ══════════════════════

@pytest.mark.parametrize("current,expected", [
    (181.5, -1.0), (182.5, 0.0), (183.5, 1.0), (184.5, 2.0), (200.5, 18.0),
])
def test_h_execution_rung_delta(current, expected):
    """The EXISTING field (``rungs_moved``) is the delta — no duplicate
    ``execution_rung_delta`` concept was created."""
    out = call(current)
    assert out["rungs_moved"] == expected
    assert out["reason"] == "within_one_rung_down" if expected >= -1 \
        else out["reason"] == R.R_MOVED_TOO_FAR


def test_h_delta_is_reconstructable_from_stored_facts():
    out = call(184.5)
    cur, trig, size = out["current_line"], out["trigger_line"], out["rung_size"]
    assert (cur - trig) / size == out["rungs_moved"] == 2.0


# ═══════════ I/J/K/L/M. the non-rung refusals (precheck.py) ══════════

class _Quote:
    def __init__(self, line, price):
        self.line = line
        self.price = price


class _Adapter:
    def __init__(self, quote, market_status="OPEN"):
        self._q, self._m = quote, market_status

    def balance(self):
        return 1000.0

    def get_event(self, event_id):
        return {"status": "OPEN", "id": event_id}

    def get_market(self, event_id, market_id):
        return {"status": self._m, "id": market_id}

    def get_selection(self, event_id, market_id, position):
        return self._q


class _Session:
    account_id = "test-session"

    def assert_submittable(self):
        return True


def precheck(quote, *, line=182.5, price=1.85, stake=10.0, market="OPEN",
             already_claimed=False):
    signal = {"game_id": "g1", "market": "TOTAL", "selection": "UNDER",
              "line": line, "price": price, "stake_amount": stake,
              "idempotency_key": "k1"}
    limits = PrecheckLimits(max_stake_per_bet=100.0,
                            max_total_exposure=10_000.0,
                            current_exposure=0.0,
                            max_line_movement=0.0, max_odds_movement=0.05)
    return run_precheck(signal, adapter=_Adapter(quote, market),
                        session=_Session(), pre=limits,
                        already_claimed=already_claimed)


def test_i_non_rung_line_fails_closed_on_existing_line_validation():
    """A line off the market's rung grid is refused by the EXISTING line
    tolerance (requested 182.5 vs current 182.7)."""
    res = precheck(_Quote(182.7, 1.85))
    assert res.ok is False and res.reason == "LINE_TOLERANCE_EXCEEDED"


def test_i_unprovable_rung_size_fails_closed():
    """A non-multiple observation series cannot establish a tick → REJECT."""
    out = call(183.5, observed=[180.0, 181.3, 182.7, 174.0])
    assert out["decision"] == R.REJECT
    assert out["reason"] == R.R_RUNG_AMBIGUOUS


def test_j_missing_executable_line_rejects():
    assert call(None)["decision"] == R.REJECT
    assert call(None)["reason"] == R.R_MARKET_MISSING
    assert precheck(_Quote(None, 1.85)).reason == "LINE_UNAVAILABLE"


def test_k_missing_executable_odds_rejects():
    """The rung rule is about the LINE; odds are owned by the price gate."""
    res = precheck(_Quote(182.5, None))
    assert res.ok is False and res.reason == "ODDS_UNAVAILABLE"


def test_l_stale_market_rejects():
    assert precheck(_Quote(182.5, 1.85), market="STALE").reason == \
        "MARKET_STATUS_INVALID"


def test_m_wrong_market_or_direction_rejects():
    assert call(182.5, market="SPREAD")["reason"] == R.R_IDENTITY
    assert call(182.5, selection="OVER")["reason"] == R.R_IDENTITY
    assert call(182.5, alert=False)["reason"] == R.R_IDENTITY


# ═══════════ N/O/P. idempotency, zero stake, UNKNOWN ═════════════════

def test_n_duplicate_position_behaviour_unchanged():
    assert precheck(_Quote(182.5, 1.85)).ok is True
    dup = precheck(_Quote(182.5, 1.85), already_claimed=True)
    assert dup.ok is False and dup.reason == "DUPLICATE_BET"


def test_n_a_rejected_command_is_never_a_position():
    rejected = R.validate_execution(game(180.5))
    assert position_from_command(rejected, executable_odds=1.85) is None


def test_o_zero_stake_unchanged():
    assert precheck(_Quote(182.5, 1.85), stake=0.0).reason == "STAKE_INVALID"


def test_p_unknown_execution_is_never_a_position():
    """UNKNOWN is not an executed bet: only an ALLOWed command can become a
    position (the lifecycle of an UNKNOWN run stays UNKNOWN — see
    test_autobet_position_2026_10_05.py::test_lifecycle_matrix...)."""
    assert position_from_command({"decision": "UNKNOWN"}) is None
    assert position_from_command({"decision": "REJECT"}) is None
    ok = _command(182.5)
    assert ok["decision"] == R.ALLOW
    assert position_from_command(ok, executable_odds=1.85) is not None


# ═══════════ the model itself must be untouched by this rule ═════════

def test_rung_rule_does_not_touch_the_blm_trigger_condition():
    """The window can ALLOW (182.5-1 rung) while the model's own condition is
    irrelevant to it — and a REJECT is purely about the LINE, never the
    trigger's validity."""
    allowed = call(181.5)
    rejected = call(180.5)
    assert allowed["trigger_line"] == rejected["trigger_line"] == TRIGGER
    assert rejected["reason"] == R.R_MOVED_TOO_FAR     # line, not signal
    assert "under_alert_state" not in dir(R)           # no model coupling
