"""LIVE MARKETS ONLY — a stale or missing market line can never alert.

Directive (COMPLETE DIRECTIVE — LIVE MARKETS ONLY, 2026-09-12):

  1. the authoritative API gate requires BOTH
         game genuinely live  AND  market_status == "LIVE"
     before an Under Alert may be active;
  2. the exclusion is EXPLICIT — a sibling `under_alert_eligibility`
     block names why (market_live / market_stale / market_missing, or the
     existing genuine-live exclusion reason), so a stale market never
     silently looks like an active live opportunity.

The quantitative condition itself is NOT changed:

    actual_pace < required_pace  AND  actual_pace < league_average_pace

    (corrected 2026-09-13: both comparisons are against actual_pace —
    the game must be behind BOTH the market-required pace and the
    league-specific average)

The seven-field `under_alert` contract is NOT changed.

Self-contained: these tests exercise the market gate and read only the
committed contract, so the commit stands alone.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from blm_v4 import api as v4api
from blm_v4.live_analytics.under_alert import (
    under_alert_eligibility,
    under_alert_state,
)

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"

# the directive's reason vocabulary
MARKET_REASONS = {"market_live", "market_stale", "market_missing"}


def _payload() -> dict:
    return v4api.v4_live(classification=None)


def _quant(ua: dict):
    """The quantitative condition (2026-09-13: both comparisons against
    actual_pace — behind BOTH the market-required pace and the league
    average), recomputed from the SERVED numbers only — independent of the
    server's own `active` boolean.  None when an operand is absent (i.e.
    nothing is claimed)."""
    a, r, lg = ua["actual_pace"], ua["required_pace"], ua["league_average_pace"]
    if a is None or r is None or lg is None:
        return None
    return a < r and a < lg


# ══════════════════════════════════════════════════════════════════════
# 1. the gate itself — every combination (deterministic)
# ══════════════════════════════════════════════════════════════════════

def test_eligibility_requires_a_live_game_and_a_live_market():
    # a genuinely live game with a LIVE market is the ONLY eligible case
    assert under_alert_eligibility("LIVE", True, None) == {
        "eligible": True, "reason": "market_live"}

    # STALE and MISSING markets are excluded, each with its own reason
    assert under_alert_eligibility("STALE", True, None) == {
        "eligible": False, "reason": "market_stale"}
    assert under_alert_eligibility("MISSING", True, None) == {
        "eligible": False, "reason": "market_missing"}

    # an absent / unrecognised classification fails CLOSED
    for unknown in (None, "", "UNKNOWN", "stale", "Live"):
        got = under_alert_eligibility(unknown, True, None)
        assert got["eligible"] is False, unknown
        assert got["reason"] == "market_missing", (unknown, got)


def test_eligibility_preserves_the_existing_genuine_live_reason():
    """A game that is not live keeps the EXISTING exclusion vocabulary —
    no new reason is invented for it."""
    for reason in ("game_finished", "unsupported_status",
                   "no_live_observation", "stale_observation",
                   "terminal_clock"):
        got = under_alert_eligibility("LIVE", False, reason)
        assert got == {"eligible": False, "reason": reason}, reason
    # a non-live game with no supplied reason still fails closed
    assert under_alert_eligibility("LIVE", False, None) == {
        "eligible": False, "reason": "not_live"}


def test_ineligible_market_suppresses_an_otherwise_true_condition():
    """The gate must actually suppress: same numbers, same condition TRUE,
    only eligibility differs."""
    args = (3.84, 5.02, 4.155, 80, 1852)   # actual < required, actual < avg
    assert under_alert_state(*args)["active"] is True            # un-gated
    assert under_alert_state(*args, eligible=True)["active"] is True
    assert under_alert_state(*args, eligible=False)["active"] is False

    # the quantitative numbers are STILL served — suppression is explicit
    # (the reason lives in the sibling block), never a blank verdict
    suppressed = under_alert_state(*args, eligible=False)
    assert suppressed["actual_pace"] == 3.84
    assert suppressed["required_pace"] == 5.02
    assert suppressed["pace_gap"] == 3.84 - 5.02
    assert suppressed["checkpoint"] == 75
    # and the seven-field contract is intact
    assert set(suppressed) == {"active", "checkpoint", "actual_pace",
                               "required_pace", "league_average_pace",
                               "league_reference_games", "pace_gap"}


# ══════════════════════════════════════════════════════════════════════
# 2. the API actually wires the gate
# ══════════════════════════════════════════════════════════════════════

def test_api_passes_the_gate_verdict_into_every_verdict(monkeypatch):
    """Anti-regression: the API must PASS the gate's verdict per game, not
    rely on the function's default.  Removing `eligible=` from the call
    site fails this test."""
    calls = []
    real = v4api.under_alert_state

    def spy(*a, **kw):
        calls.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(v4api, "under_alert_state", spy)
    payload = _payload()
    games = payload["games"]

    assert games and len(calls) == len(games), (len(calls), len(games))
    assert all("eligible" in kw for kw in calls), \
        "a verdict was composed WITHOUT the market gate"
    # one call per game, in order — each flag equals the gate's verdict
    for g, kw in zip(games, calls):
        proj = g.get("projector") or {}
        gate = under_alert_eligibility(
            proj.get("market_status"), g.get("live"), g.get("live_reason"))
        assert g["under_alert_eligibility"] == gate, g["game_id"]
        assert kw["eligible"] == gate["eligible"], g["game_id"]


def test_api_active_flag_follows_the_gate(monkeypatch):
    """Prove the flag — not a default — drives `active`: force the gate
    each way and watch the served verdicts follow."""
    # force INELIGIBLE: nothing anywhere can be active
    monkeypatch.setattr(v4api, "under_alert_eligibility",
                        lambda *a, **k: {"eligible": False,
                                         "reason": "market_stale"})
    p = _payload()
    assert p["games"]
    assert not any(g["under_alert"]["active"] for g in p["games"])

    # force ELIGIBLE: every quantitatively-qualifying game becomes active,
    # and nothing else does
    monkeypatch.setattr(v4api, "under_alert_eligibility",
                        lambda *a, **k: {"eligible": True,
                                         "reason": "market_live"})
    p = _payload()
    qual = [g for g in p["games"] if _quant(g["under_alert"]) is True]
    assert qual, "no qualifying game to prove the flag drives active"
    assert all(g["under_alert"]["active"] is True for g in qual)
    assert not [g["game_id"] for g in p["games"]
                if _quant(g["under_alert"]) is not True
                and g["under_alert"]["active"]]


# ══════════════════════════════════════════════════════════════════════
# 3. the served payload
# ══════════════════════════════════════════════════════════════════════

def test_every_served_game_carries_an_explicit_eligibility_reason():
    payload = _payload()
    assert payload["games"]
    for g in payload["games"]:
        elig = g["under_alert_eligibility"]
        assert set(elig) == {"eligible", "reason"}, elig
        assert isinstance(elig["eligible"], bool), elig
        assert isinstance(elig["reason"], str) and elig["reason"], elig

        ms = (g.get("projector") or {}).get("market_status")
        ua = g["under_alert"]
        # the seven-field contract is unchanged
        assert set(ua) == {"active", "checkpoint", "actual_pace",
                           "required_pace", "league_average_pace",
                           "league_reference_games", "pace_gap"}, ua
        if elig["eligible"]:
            assert elig["reason"] == "market_live", g["game_id"]
            assert ms == "LIVE", (g["game_id"], ms)
            assert ua["active"] is (_quant(ua) is True), g["game_id"]
        elif g.get("live"):
            # a genuinely live game excluded by the MARKET — name which
            assert elig["reason"] == ("market_stale" if ms == "STALE"
                                      else "market_missing"), (g["game_id"], ms)
            assert ua["active"] is False, g["game_id"]


def test_no_active_alert_without_a_live_market():
    """The bare invariant, on the real payload."""
    payload = _payload()
    assert payload["games"]
    for g in payload["games"]:
        if g["under_alert"]["active"]:
            assert g["under_alert_eligibility"]["eligible"] is True, g["game_id"]
            assert (g.get("projector") or {}).get("market_status") == "LIVE", \
                g["game_id"]


def test_opening_line_is_never_substituted_for_a_stale_live_line():
    """The gate excludes a non-live market — it never falls back to the
    opening line, and never fabricates numbers from one."""
    src = (HERE.parent / "blm_v4" / "api.py").read_text(encoding="utf-8")
    i = src.index("# AUTHORITATIVE MARKET GATE")
    gate = src[i:src.index("return {", i)]
    assert "opening_line" not in gate
    assert "opening_snapshot" not in gate
    assert "total_line" not in gate          # the line is never re-selected
    assert "under_alert_eligibility" in gate
    # the eligibility decision itself never consults a line value
    mod = (HERE.parent / "blm_v4" / "live_analytics"
           / "under_alert.py").read_text(encoding="utf-8")
    j = mod.index("def under_alert_eligibility")
    fn = mod[j:mod.index("def under_alert_state", j)]
    assert "opening" not in fn
    assert "live_total_line" not in fn


# ══════════════════════════════════════════════════════════════════════
# 4. the served frontend — consumes the server's gate, never re-derives
# ══════════════════════════════════════════════════════════════════════

def _js() -> str:
    return (DASH_STATIC / "dashboard.js").read_text(encoding="utf-8")


def test_served_frontend_consumes_the_eligibility_reason():
    js = _js()
    assert "under_alert_eligibility" in js


def test_served_frontend_gates_on_the_server_market_field():
    """Defence-in-depth: the served asset must consume the server's market
    eligibility, and only ever as a REMOVAL — never re-deriving the verdict
    from pace numbers in the browser."""
    js = _js()
    assert "g.under_alert_eligibility.eligible === true" in js
    assert "isActuallyLive(g) && alertEligible(g) && mktEligible" in js
    # the browser still does not reconstruct the quantitative condition
    assert "actual < required" not in js
    assert "required > " not in js


# ══════════════════════════════════════════════════════════════════════
# 5. stale-market lifecycle on the SHIPPED state machine
# ══════════════════════════════════════════════════════════════════════

node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")

PURE_BEGIN = "/* __PURE_ALERT_BEGIN__ */"
PURE_END = "/* __PURE_ALERT_END__ */"
STORE_BEGIN = "/* __ALERT_STORE_BEGIN__ */"
STORE_END = "/* __ALERT_STORE_END__ */"

# stub environment: only what the shipped code reads from outside both
# extracted blocks (mirrors tests/test_under_alert_lifecycle.py).
STUBS = """
let __T = Date.parse("2026-09-12T15:42:08Z");
Date.now = () => __T;
const __LS = {};
const localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(__LS, k) ? __LS[k] : null),
  setItem: (k, v) => { __LS[k] = String(v); },
};
const __EL = {};
function $(id) { return __EL[id] || (__EL[id] = { innerHTML: "", textContent: "" }); }
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtTime = (iso) => !iso ? "--"
  : new Date(iso).toLocaleTimeString("en-GB", { hour12: false });
function isActuallyLive(g) { return !!(g && g.live === true); }
"""

EXPORTS = """
module.exports = { UNDER_ALERTS, renderUnderAlerts, activeAlertsHTML };
"""

# a qualifying live game, driven through LIVE -> STALE -> LIVE.  The server
# verdict (g.under_alert / g.under_alert_eligibility) is supplied explicitly
# — the store is a pure consumer of it.
LIFECYCLE = """
const LABELS = { "betual-tbsl": "TBSL" };
const base = (o) => Object.assign({
  game_id: "G-STALE", competition_slug: "betual-tbsl", live: true,
  live_reason: null, alert: { eligible: true },
  projector: { progress_pct: 80, market_status: "LIVE",
               live_total_line: 160.5, market_age_seconds: 100 },
  under_alert: { active: true, checkpoint: 75, actual_pace: 3.84,
                 required_pace: 5.02, league_average_pace: 4.155,
                 league_reference_games: 1852, pace_gap: 3.84 - 5.02 },
  under_alert_eligibility: { eligible: true, reason: "market_live" },
}, o || {});
const stale = (reason) => base({
  projector: { progress_pct: 80, market_status: reason === "market_stale"
               ? "STALE" : "MISSING", live_total_line: 160.5,
               market_age_seconds: 900 },
  under_alert: Object.assign(base().under_alert, { active: false }),
  under_alert_eligibility: { eligible: false, reason },
});
const out = {};
m.renderUnderAlerts([base()], LABELS);
out.live_active = m.UNDER_ALERTS.active.size;
out.live_shown = m.activeAlertsHTML().indexOf("UNDER ALERT") !== -1;
out.live_hist = m.UNDER_ALERTS.history.length;

m.renderUnderAlerts([stale("market_stale")], LABELS);
out.stale_active = m.UNDER_ALERTS.active.size;
out.stale_shown = m.activeAlertsHTML().indexOf("UNDER ALERT") !== -1;
out.stale_hist = m.UNDER_ALERTS.history.length;
out.stale_resolved = m.UNDER_ALERTS.history[0].resolved_at !== null;
out.stale_reason = m.UNDER_ALERTS.history[0].resolved_reason;

m.renderUnderAlerts([base()], LABELS);
out.back_active = m.UNDER_ALERTS.active.size;
out.back_shown = m.activeAlertsHTML().indexOf("UNDER ALERT") !== -1;
out.back_hist = m.UNDER_ALERTS.history.length;
// the earlier stale episode stays RESOLVED (never re-promoted) and the
// re-trigger opens its own record
out.back_hist0_resolved = m.UNDER_ALERTS.history[0].resolved_at !== null;
out.back_hist1_open = m.UNDER_ALERTS.history[1]
  && m.UNDER_ALERTS.history[1].resolved_at === null;

m.renderUnderAlerts([stale("market_missing")], LABELS);
out.missing_active = m.UNDER_ALERTS.active.size;

// an ineligible market can never resurrect a record even when the numbers
// would qualify: eligibility is the gate, the numbers are not
m.renderUnderAlerts([Object.assign(base(), {
  under_alert_eligibility: { eligible: false, reason: "market_stale" },
})], LABELS);
out.qual_but_ineligible_active = m.UNDER_ALERTS.active.size;
console.log(JSON.stringify(out));
"""


@node
def test_stale_market_lifecycle(tmp_path):
    js = _js()
    i = js.index(PURE_BEGIN) + len(PURE_BEGIN)
    pure = js[i:js.index(PURE_END, i)]
    i = js.index(STORE_BEGIN) + len(STORE_BEGIN)
    store = js[i:js.index(STORE_END, i)]
    mod = tmp_path / "alert_store.js"
    mod.write_text(STUBS + pure + store + EXPORTS, encoding="utf-8")
    script = "const m = require(%s);\n%s" % (json.dumps(str(mod)), LIFECYCLE)
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    got = json.loads(out.stdout)

    # LIVE + qualifying  ->  ACTIVE UNDER ALERT
    assert got["live_active"] == 1, got
    assert got["live_shown"] is True, got
    assert got["live_hist"] == 1, got

    # LIVE -> STALE  ->  active removed, history retained + resolved
    assert got["stale_active"] == 0, got
    assert got["stale_shown"] is False, got
    assert got["stale_hist"] == 1, got          # retained, never erased
    assert got["stale_resolved"] is True, got
    assert got["stale_reason"] == "market_stale", got

    # STALE -> LIVE  ->  active again (the authoritative condition is TRUE)
    assert got["back_active"] == 1, got
    assert got["back_shown"] is True, got
    # the stale episode REOPENS the same single record (one record per
    # identity, ever — 2026-09-13); the episode is retained in the record's
    # history, never duplicated as a second one
    assert got["back_hist"] == 1, got
    assert got["back_hist0_resolved"] is False, got

    # MISSING behaves the same as STALE — no active record
    assert got["missing_active"] == 0, got

    # qualifying numbers are NOT sufficient on their own
    assert got["qual_but_ineligible_active"] == 0, got
