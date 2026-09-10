"""UNDER-only historical-condition alert layer — frontend presentation tests.

The alert layer is READ-ONLY: it compares CURRENT authoritative API state
(projector fields + the /pace-z payload) against FIXED UNDER research
findings from settled games and renders badges/panels.  It never predicts,
never computes its own values, and never touches the charts.

Level hierarchy (pace_gap > 1.5 is LOAD-BEARING — z is only ever a qualifier):
  base    pace_gap > 1.5
  strong  + Q4 + progress >= 90
  high    + pace_z < -1

Static contract tests (served assets, parallel-safe) prove the layer exists,
consumes authoritative fields only, carries the exact fixed UNDER statistics,
flags freshness/CYBER-sample limits, uses UNDER-only vocabulary (no OVER),
stays stable across refreshes, and reintroduces no old analytical vocabulary.

Behavior tests extract the pure level-selection block between the
__PURE_ALERT_BEGIN__ / __PURE_ALERT_END__ markers and execute it in Node.js
(skipped when node is unavailable).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from blm_v4.api import router as v4_router

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"

PURE_BEGIN = "/* __PURE_ALERT_BEGIN__ */"
PURE_END = "/* __PURE_ALERT_END__ */"

# fixed UNDER research statistics that MUST be presented verbatim
FIXED = {
    "base": ["72.65%", "87.31%", "1,489"],
    "strong": ["78.01%", "87.71%", "1,446"],
    "high": ["85.74%", "89.27%", "363"],
    "cyber": ["59.3%", "56.5%", "84"],
}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(v4_router)
    app.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
              name="test_dashboard_static")
    return TestClient(app)


def _js(client) -> str:
    resp = client.get("/static/dashboard.js")
    assert resp.status_code == 200
    return resp.text


def _css(client) -> str:
    resp = client.get("/static/styles.css")
    assert resp.status_code == 200
    return resp.text


def _pure(js: str) -> str:
    i = js.index(PURE_BEGIN) + len(PURE_BEGIN)
    j = js.index(PURE_END, i)
    return js[i:j]


node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")


def _run_node(js: str, tmp_path: Path, expr: str):
    """Evaluate `expr` (JS, may reference the pure fns) in node."""
    mod = tmp_path / "alert_pure.js"
    mod.write_text(_pure(js)
                   + "\nmodule.exports = { histAlertLevel, alertEscalation };",
                   encoding="utf-8")
    script = ("const m = require(%s); console.log(JSON.stringify(%s));"
              % (json.dumps(str(mod)), expr))
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ── fixed UNDER research numbers + vocabulary ─────────────────────

def test_fixed_archive_statistics_present(client):
    js = _js(client)
    for lvl, values in FIXED.items():
        for v in values:
            assert v in js, (lvl, v)
    for label in ("UNDER CONDITION",
                  "UNDER ALERT — STRONG",
                  "UNDER ALERT — VERY STRONG"):
        assert label in js, label


def test_no_stale_or_retired_numbers(client):
    """The superseded constant sets must be fully replaced: the earlier
    1dp HEAD forms, and the withdrawn 4-level revision (with its L4 tier)."""
    js = _js(client)
    retired = ("72.7%", "87.3%", "78.0%", "87.7%", "85.7%", "89.3%",   # 1dp
               "74.48", "88.65", "1,762", "80.00", "89.04", "1,711",
               "86.75", "89.99", "87.56", "93.23",                      # 4-level
               "THINNEST SAMPLE", "extreme", "EXTREME")
    for stale in retired:
        assert stale not in js, stale


def test_alert_terminology_descriptive_only(client):
    js = _js(client)
    for word in ("HISTORICAL UNDER CONDITION", "observation UNDER rate",
                 "mean per-game UNDER rate", "settled games",
                 "LIMITED SAMPLE", "SETTLED GAMES"):
        assert word in js, word
    # never presented as guidance / no old or betting vocabulary
    low = js.lower()
    for banned in ("edge", "signal", "momentum", "win rate", "probab",
                   "calibrat", "forecast", "predict", "fair", "z_score",
                   "betting", "staking"):
        assert banned not in low, banned


def test_no_over_anywhere(client):
    """UNDER ONLY: the word OVER must not appear; no mirrored condition."""
    js = _js(client)
    assert "OVER" not in js
    # the removed opposite-direction context classifier is gone
    assert "zContextBin" not in js
    assert "over-concentrated" not in js.lower()


def test_no_extra_alert_tier(client):
    """Exactly three UNDER levels — no reconstructed L4/thin tier."""
    js = _js(client)
    for gone in ("\"extreme\"", "extreme:", "al-extreme", "--extreme",
                 "al-thin", "meta.thin", "THINNEST SAMPLE"):
        assert gone not in js, gone


def test_cyber_limited_sample_not_overstated(client):
    js = _js(client)
    # CYBER is context with its thin sample explicitly flagged
    assert "CYBER HISTORICAL CONTEXT" in js
    assert "LIMITED SAMPLE — " in js and "${CYBER_HIST.n} SETTLED GAMES" in js
    assert "${CYBER_HIST.obs}" in js and "${CYBER_HIST.game}" in js
    # CYBER is NOT an alert level: HIST_ALERTS carries no cyber tier
    assert '\\"cyber\\"' not in js
    assert "al-cyber" in js
    # the CYBER note exists in both compact (card) and full (panel) forms
    assert "cyberNoteHTML(true)" in js and "cyberNoteHTML(false)" in js


# ── authoritative-only consumption ─────────────────────────────────

def test_alert_reads_authoritative_fields_only(client):
    js = _js(client)
    # inputs are the stored projector / pace-z values, never recomputed
    assert "paceGap: p.pace_gap" in js
    assert "progressPct: p.progress_pct" in js
    assert "paceGap: p.pace_gap, z: zm.z" in js or "z: zm.z" in js
    # no z arithmetic anywhere
    for banned in ("/ pz.std_pace", "mean_pace) /", "(z - "):
        assert banned not in js
    # level selection is pure: it never touches DOM/state
    pure = _pure(js)
    assert "document." not in pure and "state." not in pure


def test_missing_line_and_freshness_gating(client):
    js = _js(client)
    # missing line → no alert; stale line → condition may show but is
    # explicitly marked MARKET STALE
    assert "missing line → no alert" in js
    assert "MARKET STALE" in js
    assert "MARKET: LIVE" in js
    assert "hist-alert-box:empty" in _css(client)


def test_current_indicators_present_in_panel(client):
    """The alert must show the actual current values, incl. the
    canonical competition identity."""
    js = _js(client)
    for label in ("Live line", "Score − line", "Actual pace", "Required pace",
                  "Pace gap", "Pace Z", "Period", "Progress", "Competition"):
        assert label in js, label
    # the panel's competition row is the canonical slug, never a display alias
    assert 'class="v">${esc(g.competition_slug || g.competition || "–")}' in js


def test_z_displayed_verbatim_from_authoritative_payload(client):
    js = _js(client)
    assert 'zDisplay(zm.z)' in js
    assert "zDisplay(z)" in js
    # display-only formatting of the API value (2dp sign-explicit)
    assert 'return z == null ? "n/a" : (z > 0 ? "+" : "") + num(z, 2);' in js


# ── three-tier visual treatment ────────────────────────────────────

def test_three_level_visual_treatment(client):
    js, css = _js(client), _css(client)
    # three escalating tiers, each with its own panel border/tint
    for lvl in ("base", "strong", "high"):
        assert f"al-{lvl}" in css
        assert f".hist-panel.hist-alert.al-{lvl}" in css
    # badge/panel classes are built from the level name (no hard-coded tier)
    assert '" al-" + lvl' in js and 'hist-panel hist-alert' in js
    # ranked escalation across exactly the three tiers
    assert "base: 1, strong: 2, high: 3" in js


# ── stability / no-chart-touch ─────────────────────────────────────

def test_no_duplicate_alert_and_stable_refresh(client):
    js = _js(client)
    # exactly one modal container; idempotent render guard
    assert 'id="histAlertBox"' in js
    assert js.count('id="histAlertBox"') == 1
    assert "box.innerHTML === html" in js          # stable during refresh
    assert "box.innerHTML = html" in js
    # card badge level transitions tracked (pulse only on entry/escalation)
    assert "prevAlert" in js
    assert "al-in" in js


def test_per_game_alert_state_isolated(client):
    """Each game keeps its OWN alert state (keyed map), so one game's
    escalation can never flash another game's card."""
    js = _js(client)
    # cards are stored in a Map keyed by game_id; prevAlert lives on the
    # per-game card object, not in shared/global state
    assert "state.cards = new Map()" in js or "new Map()" in js
    assert "card.prevAlert" in js
    assert "g.game_id" in js


def test_primary_chart_unchanged_by_alert_layer(client):
    js = _js(client)
    # the two observed series remain the only primary-chart datasets
    assert 'label: "Actual score (combined)"' in js
    assert 'label: "Live O/U line"' in js
    assert "data.datasets[2]" not in js
    # alert code paths never touch the chart instances
    assert "modalCharts" not in _pure(js)


# ── real behavior of the pure classifier (node) ────────────────────

@node
def test_behavior_three_level_gating(client, tmp_path):
    def lvl(**kw):
        base = dict(hasLine=True, periodQ="Q2", progressPct=50,
                    paceGap=2.0, z=None)
        base.update(kw)
        return base
    res = _run_node(_js(client), tmp_path, "["
        "m.histAlertLevel(" + json.dumps(lvl(paceGap=1.5)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(paceGap=1.51)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(paceGap=2.0, z=-2.0)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(periodQ="Q4", progressPct=90)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(periodQ="Q4", progressPct=93, z=-1.2)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(periodQ="Q4", progressPct=93, z=-0.9)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(periodQ="Q4", progressPct=89, z=-2)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(hasLine=False)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(paceGap=None)) + "),"
        "m.histAlertLevel(null)"
        "]")
    # gap<=1.5 none · gap>1.5 non-late base (z irrelevant) · L2 strong ·
    # L3 high · z>-1 strong · progress<90 base · missing line none ·
    # null gap none · null state none
    assert res == [None, "base", "base", "strong", "high", "strong",
                   "base", None, None, None], res


@node
def test_behavior_z_alone_never_alerts(client, tmp_path):
    """z is only a qualifier on top of pace_gap > 1.5 — it can never
    raise an alert by itself, at any intensity."""
    def lvl(**kw):
        base = dict(hasLine=True, periodQ="Q4", progressPct=95,
                    paceGap=1.0, z=None)
        base.update(kw)
        return base
    res = _run_node(_js(client), tmp_path, "["
        "m.histAlertLevel(" + json.dumps(lvl(z=-1.0)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(z=-1.5)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(z=-3)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(z=-99)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(paceGap=1.5, z=-3)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(paceGap=None, z=-3)) + ")"
        "]")
    assert res == [None, None, None, None, None, None], res


@node
def test_behavior_boundary_semantics(client, tmp_path):
    def lvl(**kw):
        base = dict(hasLine=True, periodQ="Q4", progressPct=95,
                    paceGap=2.0, z=None)
        base.update(kw)
        return base
    res = _run_node(_js(client), tmp_path, "["
        "m.histAlertLevel(" + json.dumps(lvl(z=-1.0)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(z=-1.01)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(z=-1.5)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(z=-1.51)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(periodQ="Q4", progressPct=90)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(periodQ="Q4", progressPct=89.9)) + ")"
        "]")
    # z=-1 exactly is NOT < -1 → strong; deeper z stays high (no L4 tier)
    assert res == ["strong", "high", "high", "high", "strong", "base"], res


@node
def test_behavior_card_path_never_high_and_downgrades(client, tmp_path):
    js = _js(client)
    # card path passes z:null → the same late state yields strong, never
    # high, until the authoritative z arrives in the modal
    strong_no_z = dict(hasLine=True, periodQ="Q4", progressPct=95,
                       paceGap=2.2, z=None)
    deep_z = dict(strong_no_z, z=-1.7)
    # downgrade chain: high → z relaxes → strong → not late → base → gap gone
    res = _run_node(js, tmp_path, "["
        "m.histAlertLevel(" + json.dumps(strong_no_z) + "),"
        "m.histAlertLevel(" + json.dumps(deep_z) + "),"
        "m.histAlertLevel(" + json.dumps(dict(deep_z, z=-1.2)) + "),"
        "m.histAlertLevel(" + json.dumps(dict(deep_z, z=0)) + "),"
        "m.histAlertLevel(" + json.dumps(dict(strong_no_z, progressPct=50)) + "),"
        "m.histAlertLevel(" + json.dumps(dict(strong_no_z, paceGap=1.4)) + ")"
        "]")
    assert res == ["strong", "high", "high", "strong", "base", None], res


@node
def test_behavior_pulse_only_on_entry_or_escalation(client, tmp_path):
    js = _js(client)
    # alertEscalation(prev, next): the attention pulse fires ONLY when the
    # level RISES.  Entry (null→any) pulses; upgrades pulse; steady state
    # and DOWNGRADES return null (silent re-render) — repeated polling of
    # the same state can never re-flash.
    res = _run_node(js, tmp_path, "["
        "m.alertEscalation(null, 'base'),"
        "m.alertEscalation(null, 'high'),"
        "m.alertEscalation(undefined, 'strong'),"
        "m.alertEscalation('base', 'base'),"
        "m.alertEscalation('base', 'strong'),"
        "m.alertEscalation('strong', 'high'),"
        "m.alertEscalation('high', 'high'),"
        "m.alertEscalation('high', 'strong'),"
        "m.alertEscalation('strong', 'base'),"
        "m.alertEscalation('base', null),"
        "m.alertEscalation(null, null)"
        "]")
    # entry + upgrades return the new level (pulse); steady / downgrades
    # / clears return null (no attention treatment)
    assert res == ["base", "high", "strong", None, "strong", "high",
                   None, None, None, None, None], res
