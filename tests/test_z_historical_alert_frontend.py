"""HISTORICAL UNDER-CONDITION ALERT LAYER — frontend presentation tests.

The alert layer is READ-ONLY: it compares CURRENT authoritative API
state (projector fields + the /pace-z payload) against FIXED research
findings from settled games and renders badges/panels.  It never
predicts, never computes its own values, and never touches the charts.

Static contract tests (served assets, parallel-safe) prove the layer
exists, is wired to authoritative fields only, carries the exact fixed
archive statistics, flags freshness/CYBER-sample limits, stays stable
across refreshes, and reintroduces no old analytical vocabulary.

Behavior tests extract the pure level-selection block between the
__PURE_ALERT_BEGIN__ / __PURE_ALERT_END__ markers and execute it in
Node.js (skipped when node is unavailable) — proving real gating:
  pace_gap <= 1.5            → no alert
  pace_gap > 1.5             → base
  Q4 + progress>=90 + gap    → strong
  + stored z < -1            → high
  missing line               → no alert
  card path (no z)           → never "high"
  condition ceases/downgrades → level falls / null
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

# fixed research statistics that MUST be presented verbatim
FIXED = {
    "base": ["72.7%", "87.3%", "1,489"],
    "strong": ["78.0%", "87.7%", "1,446"],
    "high": ["85.7%", "89.3%", "363"],
    "cyber": ["60.5%", "56.6%", "50"],
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
                   + "\nmodule.exports = { histAlertLevel, zContextBin };",
                   encoding="utf-8")
    script = ("const m = require(%s); console.log(JSON.stringify(%s));"
              % (json.dumps(str(mod)), expr))
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ── fixed research numbers + vocabulary ────────────────────────────

def test_fixed_archive_statistics_present(client):
    js = _js(client)
    for lvl, values in FIXED.items():
        for v in values:
            assert v in js, (lvl, v)
    for label in ("HISTORICAL UNDER CONDITION",
                  "STRONG HISTORICAL UNDER CONCENTRATION",
                  "HIGH HISTORICAL UNDER CONCENTRATION"):
        assert label in js


def test_alert_terminology_descriptive_only(client):
    js = _js(client)
    # the layer vocabulary is the historical/empirical one required
    for word in ("HISTORICAL UNDER CONDITION", "HISTORICAL UNDER CONCENTRATION",
                 "HISTORICAL CONTEXT", "HISTORICAL OBSERVATIONS",
                 "Equal-game mean", "Historical game N",
                 "LIMITED SAMPLE", "SETTLED GAMES"):
        assert word in js
    # never presented as guidance / no old or betting vocabulary
    low = js.lower()
    for banned in ("edge", "signal", "momentum", "win rate", "probab",
                   "calibrat", "forecast", "predict", "fair", "z_score",
                   "betting", "staking"):
        assert banned not in low, banned


def test_cyber_limited_sample_not_overstated(client):
    js = _js(client)
    # CYBER is context with its thin sample explicitly flagged
    assert "CYBER HISTORICAL CONTEXT" in js
    assert "LIMITED SAMPLE — " in js and "${CYBER_HIST.n} SETTLED GAMES" in js
    assert "${CYBER_HIST.obs}" in js and "${CYBER_HIST.game}" in js
    # CYBER is not an alert level: HIST_ALERTS carries no cyber tier
    assert '"cyber"' not in js
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
    # no z arithmetic anywhere (still holds with the new layer)
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


def test_z_displayed_verbatim_from_authoritative_payload(client):
    js = _js(client)
    assert 'zDisplay(zm.z)' in js
    assert "zDisplay(z)" in js
    # display-only formatting of the API value (2dp sign-explicit)
    assert 'return z == null ? "n/a" : (z > 0 ? "+" : "") + num(z, 2);' in js


# ── stability / no-chart-touch ─────────────────────────────────────

def test_no_duplicate_alert_and_stable_refresh(client):
    js = _js(client)
    # exactly one modal container; idempotent render guard
    assert 'id="histAlertBox"' in js
    assert js.count('id="histAlertBox"') == 1
    assert "box.innerHTML === html" in js          # stable during refresh
    assert "box.innerHTML = html" in js
    # card badge level transitions tracked (pulse only on entry)
    assert "prevAlert" in js
    assert "al-in" in js


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
def test_behavior_level_gating(client, tmp_path):
    def lvl(**kw):
        base = dict(hasLine=True, periodQ="Q2", progressPct=50,
                    paceGap=2.0, z=None)
        base.update(kw)
        return base
    res = _run_node(_js(client), tmp_path, "["
        "m.histAlertLevel(" + json.dumps(lvl(paceGap=1.5)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(paceGap=1.51)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(paceGap=2.0, z=0.5)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(periodQ="Q4", progressPct=90)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(periodQ="Q4", progressPct=93, z=-1.2)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(periodQ="Q4", progressPct=93, z=-0.9)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(periodQ="Q4", progressPct=89)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(hasLine=False)) + "),"
        "m.histAlertLevel(" + json.dumps(lvl(paceGap=None)) + "),"
        "m.histAlertLevel(null)"
        "]")
    # gap <= 1.5 → none; gap > 1.5 non-late → base (z irrelevant)
    assert res == [None, "base", "base", "strong", "high",
                   "strong", "base", None, None, None], res


@node
def test_behavior_card_path_never_high_and_downgrades(client, tmp_path):
    js = _js(client)
    # card path passes z:null → the same late state yields strong, never
    # high, until the authoritative z arrives in the modal
    strong_no_z = dict(hasLine=True, periodQ="Q4", progressPct=95,
                       paceGap=2.2, z=None)
    high_z = dict(strong_no_z, z=-1.4)
    # downgrade chain: high → z lost → strong → not late → base → gap gone
    res = _run_node(js, tmp_path, "["
        "m.histAlertLevel(" + json.dumps(strong_no_z) + "),"
        "m.histAlertLevel(" + json.dumps(high_z) + "),"
        "m.histAlertLevel(" + json.dumps(dict(high_z, z=0)) + "),"
        "m.histAlertLevel(" + json.dumps(dict(strong_no_z, progressPct=50)) + "),"
        "m.histAlertLevel(" + json.dumps(dict(strong_no_z, paceGap=1.4)) + "),"
        "m.zContextBin(-2.5).lean,"
        "m.zContextBin(-0.5).lean,"
        "m.zContextBin(1.5).lean,"
        "m.zContextBin(null)"
        "]")
    assert res == ["strong", "high", "strong", "base", None,
                   "under", "under", "over", None], res


@node
def test_behavior_z_context_is_context_not_alert(client, tmp_path):
    js = _js(client)
    # z bins map to the documented gradient ranges; z alone never fires
    res = _run_node(js, tmp_path, "["
        "m.zContextBin(-2.5).range,"
        "m.zContextBin(-1.5).range,"
        "m.zContextBin(2.5).range,"
        "m.histAlertLevel(" + json.dumps(dict(hasLine=True, periodQ="Q4",
                                              progressPct=95, paceGap=1.0,
                                              z=-3)) + ")"
        "]")
    assert res == ["z < -2", "-2 <= z < -1", "z >= 2", None], res
