"""LIVE ALERT ELIGIBILITY — the operational alert gate.

The dashboard may raise an UNDER alert ONLY when the game's latest
authoritative observation is concurrently:

  1. non-terminal        (authoritative game-time evidence)
  2. not a finished game (status)
  3. CURRENT             (observation age <= api.ALERT_MAX_OBS_AGE_S)
  4. remaining_game_minutes >= 2.5   (inclusive; api.ALERT_MIN_REMAINING_MINUTES)

The gate is evaluated by the BACKEND (api._alert_gate) and consumed
verbatim by every frontend alert path (card badge, modal panel, pulse,
audio).  A finished game or a stale stored observation must never alert
merely because its old state still satisfies the condition.

Nothing here changes the historical analysis, the frozen audit constants,
the benchmark semantics, the Z-score or the relative-pace definitions.
"""
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.api import (ALERT_MAX_OBS_AGE_S, ALERT_MIN_REMAINING_MINUTES,
                        _alert_gate)
from blm_v4.terminal_eligibility import is_terminal_checkpoint

HERE = Path(__file__).resolve().parent
DASH_JS = HERE.parent / "blm_v4" / "dashboard" / "static" / "dashboard.js"

PURE_BEGIN = "/* __PURE_ALERT_BEGIN__ */"
PURE_END = "/* __PURE_ALERT_END__ */"

NOW = datetime(2026, 9, 11, 16, 0, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _iso(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _game(status="live", classification="BETUAL_NBA"):
    return {"status": status, "classification": classification}


def _proj(remaining, *, captured=FRESH, elapsed=None, pct=None,
          label="4th Quarter", clock="07:00", cls="BETUAL_NBA", full=40.0):
    """A clean-projection row.  elapsed/pct default from remaining so the
    fixture is internally consistent unless a case deliberately breaks it."""
    if elapsed is None:
        elapsed = full - remaining
    if pct is None:
        pct = elapsed / full * 100.0
    return {"captured_at": captured, "classification": cls,
            "elapsed_game_minutes": elapsed, "progress_pct": pct,
            "remaining_game_minutes": remaining, "period_label": label,
            "clock": clock}


def _elig(game, proj, quality=None, age=None):
    return _alert_gate(game, proj, NOW, age, quality)


# ── 1. the 2.5-minute HARD CUTOFF (inclusive at 2.50) ───────────────────

@pytest.mark.parametrize("remaining,eligible", [
    (6.0, True),      # 6:00
    (5.0, True),      # 5:00
    (4.0, True),      # 4:00
    (3.0, True),      # 3:00
    (2.5, True),      # 2:30 — the boundary itself IS eligible
    (2.48333333, False),  # 2:29
    (2.0, False),     # 2:00
    (1.0, False),     # 1:00
    (0.5, False),     # 0:30
])
def test_min_remaining_cutoff_is_inclusive_at_2_5(remaining, eligible):
    got = _elig(_game(), _proj(remaining))
    assert got["eligible"] is eligible
    if not eligible:
        assert got["reason"] == "below_min_remaining"


def test_cutoff_constant_is_the_analytical_floor():
    """The alert floor must BE the analytical 2.5-minute cutoff — a second,
    drifting notion of the cutoff is exactly what this gate prevents."""
    assert ALERT_MIN_REMAINING_MINUTES == 2.5


# ── 2. terminal / finished games can never alert ────────────────────────

@pytest.mark.parametrize("label,game,proj", [
    ("finished, remaining 0", _game("ended"), _proj(0.0)),
    # a finished game whose LAST stored observation is a qualifying
    # mid-game state (7:00 left) must still be ineligible
    ("finished but mid-game obs", _game("ended"), _proj(7.0)),
])
def test_finished_game_never_alerts(label, game, proj):
    got = _elig(game, proj)
    assert got["eligible"] is False, label
    assert got["reason"] == "game_finished", label


@pytest.mark.parametrize("label,proj", [
    ("elapsed reached full duration",
     _proj(0.0, elapsed=40.0, pct=100.0)),
    ("Q4 clock sentinel 00:00",
     _proj(0.0, elapsed=39.0, pct=97.5, clock="00:00")),
    ("explicit finished label",
     _proj(0.0, elapsed=39.0, pct=97.5, label="Full Time")),
])
def test_terminal_observation_never_alerts(label, proj):
    """Terminal is decided from authoritative game time — a game still
    flagged 'live' but whose own observation proves the end is terminal."""
    got = _elig(_game("live"), proj)
    assert got["eligible"] is False, label
    assert got["reason"] == "terminal_observation", label


def test_qualifying_old_observation_on_terminal_game_cannot_alert():
    """The exact stale-alert scenario: a qualifying (>=2.5 min) stored state
    on a game that has since finished must NOT be eligible."""
    game = _game("ended")
    proj = _proj(6.0)                       # would qualify on its own
    assert proj["remaining_game_minutes"] >= ALERT_MIN_REMAINING_MINUTES
    assert _elig(game, proj)["eligible"] is False


# ── 3. freshness — a stale stored observation can never alert ───────────

def test_stale_observation_cannot_alert():
    for age_s in (ALERT_MAX_OBS_AGE_S + 1, 900.0, 3600.0):
        got = _elig(_game(), _proj(6.0, captured=_iso(age_s)))
        assert got["eligible"] is False, age_s
        assert got["reason"] == "stale_observation", age_s


def test_observation_within_freshness_bound_can_alert():
    got = _elig(_game(), _proj(6.0, captured=_iso(ALERT_MAX_OBS_AGE_S - 1)))
    assert got["eligible"] is True


def test_no_observation_row_cannot_alert():
    got = _elig(_game(), None)
    assert got["eligible"] is False
    assert got["reason"] == "no_live_observation"


def test_invalid_quality_cannot_alert():
    got = _elig(_game(), _proj(6.0), quality={"status": "INVALID"})
    assert got["eligible"] is False
    assert got["reason"] == "invalid_quality"


# ── 4. terminal semantics are authoritative over the status flag ────────

def test_terminal_semantics_not_overridden_by_ended_status():
    """Authoritative game time wins over the game-level status flag: a
    mid-game observation of a since-'ended' game is NOT terminal.  The
    elapsed rule needs the classification to resolve the full duration
    (and progress==1.0 is the duration-independent backstop)."""
    assert is_terminal_checkpoint(
        classification="BETUAL_NBA", game_status="ended",
        elapsed_minutes=20.0) is False
    assert is_terminal_checkpoint(
        classification="BETUAL_NBA", game_status="ended",
        elapsed_minutes=40.0) is True
    assert is_terminal_checkpoint(
        game_status="ended", progress=1.0) is True
    # the gate forwards the classification so the elapsed rule can resolve
    assert _elig(_game("ended"), _proj(0.0, elapsed=40.0, pct=100.0)
                 )["reason"] == "game_finished"
    # a live, non-terminal observation is eligible
    assert _elig(_game("live"), _proj(6.0))["eligible"] is True


@pytest.mark.parametrize("elapsed,terminal", [
    (40.0, True),    # 40/40 BETUAL_NBA -> terminal
    (39.25, False),  # 39.25/40 -> non-terminal
    (39.99, False),
])
def test_terminal_boundary_table(elapsed, terminal):
    assert is_terminal_checkpoint(
        classification="BETUAL_NBA", elapsed_minutes=elapsed) is terminal


def test_cyber_duration_is_authoritative():
    assert is_terminal_checkpoint(
        classification="CYBER_2K26", elapsed_minutes=48.0) is True
    assert is_terminal_checkpoint(
        classification="CYBER_2K26", elapsed_minutes=40.0) is False


def test_cyber_live_game_can_alert_cyber_end_cannot():
    live = _elig(_game("live", "CYBER_2K26"),
                 _proj(6.0, elapsed=42.0, pct=87.5, cls="CYBER_2K26"))
    assert live["eligible"] is True
    end = _elig(_game("live", "CYBER_2K26"),
                _proj(0.0, elapsed=48.0, pct=100.0, cls="CYBER_2K26"))
    assert end["eligible"] is False
    assert end["reason"] == "terminal_observation"


# ── 5. the transition RESET — no alert survives ineligibility ───────────

def test_crossing_the_cutoff_clears_the_prior_alert_state():
    """A game that qualified at 2:30 must not remain eligible at 2:29: the
    gate flips to ineligible, so the level collapses to null and the pulse
    state resets — it cannot re-fire from the stale qualified state."""
    at_230 = _elig(_game(), _proj(2.5))
    at_229 = _elig(_game(), _proj(2.48333333))
    assert at_230["eligible"] is True
    assert at_229["eligible"] is False
    # the frontend consumes the gate directly: an ineligible game yields no
    # level, so prevAlert is reset to null and escalation cannot fire
    assert _escalation("pace-state", None) is None
    assert _escalation(None, None) is None


# ── 6. the FRONTEND routes every alert path through the backend gate ────

def _js() -> str:
    return DASH_JS.read_text(encoding="utf-8")


def _js_norm() -> str:
    """dashboard.js with runs of whitespace collapsed, so multi-line
    statements can be asserted against as one string."""
    return " ".join(_js().split())


def test_every_alert_level_site_is_gated():
    """Every place that decides an alert level must consult
    alertEligible(g) — badge, panel, card pulse, modal pulse and the
    UNDER alerts surface."""
    js = _js()
    # the gate exists and reads the backend payload only (no re-derivation)
    assert "function alertEligible(g)" in js
    assert "g.alert && g.alert.eligible === true" in js
    # alertEligible is inside the pure (node-testable) block
    assert "function alertEligible(g)" in js[js.index(PURE_BEGIN):js.index(PURE_END)]
    # 1 definition + exactly 5 consuming sites (the 5th is the
    # active/history reconciliation, which gates every record it keeps)
    assert _js_norm().count("alertEligible(g)") == 6
    assert "isActuallyLive(g) && alertEligible(g)" in _js_norm()


def test_no_alert_path_bypasses_the_gate():
    """The legacy pace-gap tiers historically had NO remaining-time gate;
    every level assignment must now be routed through the gate."""
    import re
    norm = _js_norm()
    sites = re.findall(r"const (?:alLvl|lvl) = .*?;", norm)
    assert len(sites) == 4, sites
    for s in sites:
        assert "alertEligible(g)" in s, s


def node_missing():
    return shutil.which("node") is None


node = pytest.mark.skipif(node_missing(), reason="node not available")


@node
def test_alert_eligible_is_fail_closed(tmp_path):
    """In node: only an explicit backend `eligible === true` opens the gate."""
    js = _js()
    mod = tmp_path / "pure.js"
    mod.write_text(
        js[js.index(PURE_BEGIN):js.index(PURE_END)]
        + "\nmodule.exports = { alertEligible, alertEscalation };",
        encoding="utf-8")
    expr = """JSON.stringify([
      m.alertEligible({alert:{eligible:true}}),
      m.alertEligible({alert:{eligible:false}}),
      m.alertEligible({alert:{eligible:'yes'}}),
      m.alertEligible({alert:{}}),
      m.alertEligible({alert:null}),
      m.alertEligible({}),
      m.alertEligible(null),
      m.alertEscalation('pace-state', null),
      m.alertEscalation(null, 'pace-state')
    ])"""
    out = subprocess.run(
        ["node", "-e",
         f"const m = require({json.dumps(str(mod))}); "
         f"console.log({expr});"],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got == [True, False, False, False, False, False, False, None,
                   "pace-state"], got


# ── 7. the served payload carries the gate for every game ───────────────

def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from blm_v4.api import router as v4_router
    app = FastAPI()
    app.include_router(v4_router)
    return TestClient(app)


def test_live_payload_exposes_alert_gate_for_every_game():
    """/live must carry an authoritative `alert` block per game so the
    frontend never has to infer eligibility."""
    resp = _client().get("/api/v4/live")
    assert resp.status_code == 200
    games = resp.json()["games"]
    assert games, "expected live payload"
    for g in games:
        alert = g.get("alert")
        assert isinstance(alert, dict), g.get("game_id")
        assert isinstance(alert.get("eligible"), bool), g.get("game_id")
        if not alert["eligible"]:
            assert alert.get("reason"), g.get("game_id")


def _escalation(prev, nxt):
    """alertEscalation via node (the pure implementation, not a copy)."""
    if node_missing():
        pytest.skip("node not available")
    js = _js()
    mod = DASH_JS.parent / "_pure_escalation_test.js"
    mod.write_text(
        js[js.index(PURE_BEGIN):js.index(PURE_END)]
        + "\nmodule.exports = { alertEscalation };", encoding="utf-8")
    try:
        out = subprocess.run(
            ["node", "-e",
             f"const m=require({json.dumps(str(mod))});"
             f"console.log(JSON.stringify(m.alertEscalation("
             f"{json.dumps(prev)},{json.dumps(nxt)})));"],
            capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    finally:
        mod.unlink(missing_ok=True)
