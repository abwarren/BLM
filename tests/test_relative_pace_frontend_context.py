"""RELATIVE-PACE frontend contract — the two conditions independently exposed.

The operator must be TOLD, explicitly, for every eligible game with a mature
historical benchmark:

    ACTUAL PACE            (value)
    REQUIRED PACE          (value)
    HISTORICAL STATE MEAN  (value)

    ACTUAL < HISTORICAL MEAN — TRUE/FALSE
    REQUIRED >= HISTORICAL MEAN — TRUE/FALSE
    BOTH CONDITIONS — TRUE/FALSE

plus the benchmark identity (PROVIDER / COMPETITION / PERIOD / PROGRESS /
STATE / HISTORICAL N) and — when BOTH conditions are TRUE — the frozen
whole-archive qualifying historical context (69.75% observation UNDER /
73.02% equal-game UNDER / 1,515 qualifying games / 12,444 qualifying
observations / 49.79% archive baseline), labelled historical/descriptive.

Invariants pinned here:
  * the browser NEVER recomputes either relationship — it renders the
    authoritative booleans from the /historical-context payload verbatim
  * each relationship is rendered as its OWN literal TRUE/FALSE line, so no
    quadrant of the 2x2 can be hidden (checked by executing the real
    renderers in Node)
  * the frozen context block appears ONLY when both conditions are TRUE
  * the frozen QUALIFYING rates are never conflated with the matched-key
    hindsight (key_hindsight_*) figures
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from blm_v4.api import router as v4_router

HERE = Path(__file__).resolve().parent
DASH_STATIC = HERE.parent / "blm_v4" / "dashboard" / "static"

node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not available")


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(v4_router)
    app.mount("/static", StaticFiles(directory=str(DASH_STATIC)),
              name="rp_test_static")
    return TestClient(app)


def _js(client) -> str:
    r = client.get("/static/dashboard.js")
    assert r.status_code == 200
    return r.text


def _extract_fn(js: str, name: str) -> str:
    """Return the full source of a top-level ``function name(...) {...}``."""
    i = js.index("function " + name + "(")
    j = js.index("{", i)
    depth = 0
    for k in range(j, len(js)):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                return js[i:k + 1]
    raise AssertionError("unterminated function " + name)


def _run_node(js: str, tmp_path: Path, expr: str):
    src = (
        "const num = (v, d = 1) => (v == null ? '-' : Number(v).toFixed(d));\n"
        "const esc = (s) => (s == null ? '' : String(s));\n"
        + _extract_fn(js, "relPaceHTML") + "\n"
        + _extract_fn(js, "paceStateHTML") + "\n"
        + _extract_fn(js, "histContextHTML") + "\n"
        "module.exports = { relPaceHTML, paceStateHTML, histContextHTML };\n"
    )
    mod = tmp_path / "rp_render.js"
    mod.write_text(src, encoding="utf-8")
    script = ("const m = require(%s); console.log(JSON.stringify(%s));"
              % (json.dumps(str(mod)), expr))
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ── static contract: exact labels + no browser recomputation ────────────

def test_required_labels_are_present_in_served_js(client):
    js = _js(client)
    for label in (
        "RELATIVE-PACE CONTEXT",
        "ACTUAL PACE",
        "REQUIRED PACE",
        "HISTORICAL STATE MEAN",
        "RELATIVE-PACE STATE",
        "ACTUAL &lt; HISTORICAL MEAN — TRUE/FALSE",
        "REQUIRED &ge; HISTORICAL MEAN — TRUE/FALSE",
        "BOTH CONDITIONS — TRUE/FALSE",
        "PROVIDER: ",
        "COMPETITION: ",
        "PERIOD: ",
        "PROGRESS: ",
        "STATE: ",
        "HISTORICAL N: ",
    ):
        assert label in js, label


def test_frozen_historical_context_block_labels_present(client):
    js = _js(client)
    for label in ("HISTORICAL CONTEXT",
                  "UNDER</b> — observation rate",
                  "UNDER</b> — equal-game rate",
                  "qualifying games</span>",
                  "qualifying observations</span>",
                  "archive baseline</span>",
                  "historical / ",
                  "not a forward claim"):
        assert label in js, label


def test_browser_never_recomputes_the_relationships(client):
    """The renderers read the payload booleans; they never compare the pace
    values themselves and never derive the combined flag."""
    js = _js(client)
    for banned in ("ctx.actual_pace <", "ctx.actual_pace >",
                   "ctx.required_pace >", "ctx.required_pace <",
                   "ctx.historical_avg_pace <", "ctx.historical_avg_pace >"):
        assert banned not in js, banned
    # every rendered verdict traces to an authoritative payload field
    assert "ctx.actual_below_state_mean" in js
    assert "ctx.required_ge_state_mean" in js
    assert "ctx.both_conditions_true" in js


def test_context_block_is_gated_on_both_conditions_true(client):
    js = _js(client)
    i = js.index("function histContextHTML(")
    head = js[i:i + 220]
    assert "!ctx.both_conditions_true" in head, head


def test_key_hindsight_never_confused_with_frozen_qualifying_rates(client):
    """The matched-key hindsight figures must carry their own explicit
    label and the frozen qualifying rates their own constants."""
    js = _js(client)
    assert "MATCHED-KEY HINDSIGHT" in js
    assert "key_hindsight_under_pct" in js
    assert "qualifying_under_pct" in js
    assert "qualifying_equal_game_under_pct" in js
    # the two are rendered in separate blocks — the hindsight note is not
    # inside the HISTORICAL CONTEXT block
    i = js.index("function histContextHTML(")
    j = js.index("function histBadgeHTML(")
    assert "key_hindsight" not in js[i:j]


# ── behaviour: each quadrant renders its own literal TRUE/FALSE ─────────

def _payload(a_below, req_ge):
    return {
        "status": "matched", "eligible": True,
        "provider": "BETUAL", "competition": "betual-nba",
        "period": "4th Quarter", "progress_pct": 93.75, "state": "P090",
        "benchmark_n": 1234, "benchmark_key": "BETUAL|betual-nba|Q4|P090",
        "actual_pace": 3.9, "historical_avg_pace": 4.4,
        "required_pace": 5.1,
        "actual_below_state_mean": a_below,
        "required_ge_state_mean": req_ge,
        "both_conditions_true": bool(a_below and req_ge),
        "qualifying_under_pct": 69.75,
        "qualifying_equal_game_under_pct": 73.02,
        "qualifying_games": 1515,
        "qualifying_observations": 12444,
        "archive_baseline_under_pct": 49.79,
        "frozen_audit_utc": "2026-09-11T13:05:48Z",
        "key_hindsight_under_pct": 48.2,
    }


TRUE_L1 = "ACTUAL &lt; HISTORICAL MEAN — TRUE/FALSE → <b>TRUE</b>"
FALSE_L1 = "ACTUAL &lt; HISTORICAL MEAN — TRUE/FALSE → <b>FALSE</b>"
TRUE_L2 = "REQUIRED &ge; HISTORICAL MEAN — TRUE/FALSE → <b>TRUE</b>"
FALSE_L2 = "REQUIRED &ge; HISTORICAL MEAN — TRUE/FALSE → <b>FALSE</b>"


@node
def test_each_quadrant_is_rendered_separately(client, tmp_path):
    js = _js(client)
    expr = ("[m.paceStateHTML(%s), m.paceStateHTML(%s), "
            "m.paceStateHTML(%s), m.paceStateHTML(%s)]"
            % (json.dumps(_payload(True, True)),
               json.dumps(_payload(True, False)),
               json.dumps(_payload(False, True)),
               json.dumps(_payload(False, False))))
    tt, tf, ft, ff = _run_node(js, tmp_path, expr)
    assert TRUE_L1 in tt and TRUE_L2 in tt
    assert "BOTH CONDITIONS — TRUE/FALSE → <b>TRUE</b>" in tt
    assert TRUE_L1 in tf and FALSE_L2 in tf
    assert "BOTH CONDITIONS — TRUE/FALSE → <b>FALSE</b>" in tf
    assert FALSE_L1 in ft and TRUE_L2 in ft
    assert "BOTH CONDITIONS — TRUE/FALSE → <b>FALSE</b>" in ft
    assert FALSE_L1 in ff and FALSE_L2 in ff
    assert "BOTH CONDITIONS — TRUE/FALSE → <b>FALSE</b>" in ff


@node
def test_context_block_only_when_both_true(client, tmp_path):
    js = _js(client)
    expr = ("[m.histContextHTML(%s), m.histContextHTML(%s), "
            "m.histContextHTML(%s), m.histContextHTML(%s)]"
            % (json.dumps(_payload(True, True)),
               json.dumps(_payload(True, False)),
               json.dumps(_payload(False, True)),
               json.dumps(_payload(False, False))))
    tt, tf, ft, ff = _run_node(js, tmp_path, expr)
    assert tf == "" and ft == "" and ff == ""
    for fragment in ("69.75% UNDER", "— observation rate",
                     "73.02% UNDER", "— equal-game rate",
                     "1,515", "qualifying games",
                     "12,444", "qualifying observations",
                     "49.79%", "archive baseline",
                     "historical / "):
        assert fragment in tt, fragment


@node
def test_identity_line_rendered_from_payload(client, tmp_path):
    js = _js(client)
    out = _run_node(js, tmp_path,
                    "m.relPaceHTML(%s)" % json.dumps(_payload(True, True)))
    for fragment in ("ACTUAL PACE", "3.90", "REQUIRED PACE", "5.10",
                     "HISTORICAL STATE MEAN", "4.40",
                     "PROVIDER: BETUAL", "COMPETITION: betual-nba",
                     "PERIOD: 4th Quarter", "PROGRESS: 94%",
                     "STATE: P090", "HISTORICAL N: 1,234"):
        assert fragment in out, fragment


# ── endpoint contract: the authoritative payload drives the above ───────

def test_historical_context_endpoint_serves_both_flags(tmp_path, monkeypatch):
    """The HTTP surface the browser consumes must carry the two booleans
    independently (this is the ONLY thing the renderers read)."""
    from tests.test_historical_context import (
        _make_dbs, _seed_archive, _insert_live,
    )
    clean, main, c, m = _make_dbs(tmp_path)
    _seed_archive(c, "betual-nba", "4th Quarter", 93.75, 40, actual=4.0,
                  side=sqlite3.connect(str(clean) + ".live_analytics.db"),
                  m=m)
    _insert_live(c, m, "EP_TT", 3.0, 5.0)
    _insert_live(c, m, "EP_TF", 3.0, 3.0)
    monkeypatch.setenv("BLM_POKERBET_DB", str(main))

    app = FastAPI()
    app.include_router(v4_router)
    cl = TestClient(app)
    tt = cl.get("/api/v4/game/EP_TT/historical-context").json()
    tf = cl.get("/api/v4/game/EP_TF/historical-context").json()
    for ctx in (tt, tf):
        assert ctx["status"] == "matched"
        assert ctx["provider"] == "BETUAL"
        assert ctx["competition"] == "betual-nba"
        assert ctx["historical_avg_pace"] == 4.0
        assert ctx["benchmark_n"] >= 30
    assert tt["actual_below_state_mean"] is True
    assert tt["required_ge_state_mean"] is True
    assert tt["both_conditions_true"] is True
    assert tf["actual_below_state_mean"] is True
    assert tf["required_ge_state_mean"] is False
    assert tf["both_conditions_true"] is False
    # frozen qualifying constants travel verbatim
    assert tt["qualifying_under_pct"] == 69.75
    assert tt["qualifying_equal_game_under_pct"] == 73.02
    assert tt["qualifying_games"] == 1515
    assert tt["qualifying_observations"] == 12444
    assert tt["archive_baseline_under_pct"] == 49.79
