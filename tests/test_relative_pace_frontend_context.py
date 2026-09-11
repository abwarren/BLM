"""RELATIVE-PACE frontend contract — the two conditions independently exposed.

The operator must be TOLD, explicitly, for every eligible game with a mature
historical benchmark:

    RELATIVE PACE — HISTORICAL STATE

    Provider / Competition / Period / State    (benchmark identity)
    Historical N                               (relative-pace benchmark N)
    Historical μ                               (state mean, pts/min)
    ACTUAL PACE    <value>   vs mean  ABOVE / BELOW
    REQUIRED PACE  <value>   vs mean  AT/ABOVE / BELOW

    ACTUAL < MEAN — TRUE/FALSE
    REQUIRED >= MEAN — TRUE/FALSE
    BOTH CONDITIONS TRUE — TRUE/FALSE

plus — when BOTH conditions are TRUE — the frozen whole-archive qualifying
historical context (69.75% observation UNDER / 73.02% equal-game UNDER /
1,515 qualifying games / 12,444 qualifying observations / 49.79% archive
baseline), labelled historical/descriptive.

Invariants pinned here:
  * the browser NEVER recomputes either relationship NOR either direction —
    it renders the authoritative booleans and the server-evaluated labels
    from the /historical-context payload verbatim
  * each relationship is rendered as its OWN literal TRUE/FALSE line, so no
    quadrant of the 2x2 can be hidden (checked by executing the real
    renderers in Node)
  * the RELATIVE-PACE HISTORICAL N is its OWN population (state_mean_n) and
    is NEVER the Z-score benchmark N; the Z population is rendered only in
    the PACE Z-SCORE panel, labelled Z-SCORE HISTORICAL N
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
        "RELATIVE PACE — HISTORICAL STATE",
        "ACTUAL PACE",
        "REQUIRED PACE",
        "Historical N",
        "Historical μ",
        "vs mean",
        "Provider",
        "Competition",
        "Period",
        "State",
        "ACTUAL &lt; MEAN",
        "REQUIRED &ge; MEAN",
        "BOTH CONDITIONS TRUE",
        "— TRUE/FALSE →",
        # the Z-score population is labelled distinctly, elsewhere
        "Z-SCORE HISTORICAL N",
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
    """The renderers read the payload booleans and the server-evaluated
    directions verbatim; they never compare the pace values themselves and
    never derive the combined flag."""
    js = _js(client)
    for banned in ("ctx.actual_pace <", "ctx.actual_pace >",
                   "ctx.required_pace >", "ctx.required_pace <",
                   "ctx.historical_avg_pace <", "ctx.historical_avg_pace >",
                   "ctx.state_mean_pace <", "ctx.state_mean_pace >"):
        assert banned not in js, banned
    # every rendered verdict traces to an authoritative payload field
    assert "ctx.actual_below_state_mean" in js
    assert "ctx.required_ge_state_mean" in js
    assert "ctx.both_conditions_true" in js
    # ...and every rendered direction to a server-evaluated label
    assert "ctx.actual_vs_state_mean" in js
    assert "ctx.required_vs_state_mean" in js


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

def _payload(a_below, req_ge, state_n=1234, z_n=None):
    """A matched historical-context payload.  ``state_n`` is the
    RELATIVE-PACE benchmark N (state_mean_n); ``z_n`` (when given) rides
    along as a DIFFERENT population's N to prove the block ignores it."""
    p = {
        "status": "matched", "eligible": True,
        "provider": "BETUAL", "competition": "betual-nba",
        "period": "4th Quarter", "progress_pct": 93.75, "state": "P090",
        "benchmark_n": state_n, "benchmark_key": "BETUAL|betual-nba|Q4|P090",
        # explicit relative-pace / historical state-mean benchmark identity
        "state_mean_provider": "BETUAL",
        "state_mean_competition": "betual-nba",
        "state_mean_period": "Q4",
        "state_mean_state": "P090",
        "state_mean_key": "BETUAL|betual-nba|Q4|P090",
        "state_mean_n": state_n,
        "state_mean_pace": 4.4,
        "actual_pace": 3.9, "historical_avg_pace": 4.4,
        "required_pace": 5.1,
        "actual_vs_state_mean": "BELOW" if a_below else "ABOVE",
        "required_vs_state_mean": "AT/ABOVE" if req_ge else "BELOW",
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
    if z_n is not None:
        p["z_n"] = z_n
        p["mean_pace"] = 6.769
        p["std_pace"] = 0.897
    return p


TRUE_L1 = "ACTUAL &lt; MEAN — TRUE/FALSE → <b>TRUE</b>"
FALSE_L1 = "ACTUAL &lt; MEAN — TRUE/FALSE → <b>FALSE</b>"
TRUE_L2 = "REQUIRED &ge; MEAN — TRUE/FALSE → <b>TRUE</b>"
FALSE_L2 = "REQUIRED &ge; MEAN — TRUE/FALSE → <b>FALSE</b>"
BOTH_T = "BOTH CONDITIONS TRUE — TRUE/FALSE → <b>TRUE</b>"
BOTH_F = "BOTH CONDITIONS TRUE — TRUE/FALSE → <b>FALSE</b>"


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
    assert TRUE_L1 in tt and TRUE_L2 in tt and BOTH_T in tt
    assert TRUE_L1 in tf and FALSE_L2 in tf and BOTH_F in tf
    assert FALSE_L1 in ft and TRUE_L2 in ft and BOTH_F in ft
    assert FALSE_L1 in ff and FALSE_L2 in ff and BOTH_F in ff


@node
def test_every_quadrant_is_visible_in_the_full_block(client, tmp_path):
    """The combined RELATIVE PACE — HISTORICAL STATE block must carry all
    three literal TRUE/FALSE lines for EVERY quadrant — the operator never
    has to open a second panel or infer a relationship."""
    for a_below in (True, False):
        for req_ge in (True, False):
            js = _js(client)
            out = _run_node(js, tmp_path, "m.relPaceHTML(%s)"
                            % json.dumps(_payload(a_below, req_ge)))
            assert ("ACTUAL &lt; MEAN — TRUE/FALSE → <b>%s</b>"
                    % ("TRUE" if a_below else "FALSE")) in out
            assert ("REQUIRED &ge; MEAN — TRUE/FALSE → <b>%s</b>"
                    % ("TRUE" if req_ge else "FALSE")) in out
            assert ("BOTH CONDITIONS TRUE — TRUE/FALSE → <b>%s</b>"
                    % ("TRUE" if (a_below and req_ge) else "FALSE")) in out


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
    for fragment in ("RELATIVE PACE — HISTORICAL STATE",
                     "ACTUAL PACE", "3.900",
                     "REQUIRED PACE", "5.100",
                     "Historical N", "1,234",
                     "Historical μ", "4.400",
                     "Provider", "BETUAL",
                     "Competition", "betual-nba",
                     "Period", "Q4",
                     "State", "P090",
                     "vs mean", "BELOW", "AT/ABOVE"):
        assert fragment in out, fragment


# ── the relative-pace N is its OWN population, never the Z-score N ──────

def test_relative_block_never_reads_the_z_payload(client):
    """relPaceHTML must read ONLY the state_mean_* contract.  The Z-score
    benchmark N lives in a different payload (zm / pz) and must never be
    rendered in the relative-pace block."""
    js = _js(client)
    body = _extract_fn(js, "relPaceHTML")
    for banned in ("zm.", "pz.", "modalZMeta", "std_pace", "z_n",
                   "benchmark_n", "historical_avg_pace"):
        assert banned not in body, banned
    assert "ctx.state_mean_n" in body
    assert "ctx.state_mean_pace" in body
    assert "ctx.state_mean_provider" in body
    assert "ctx.state_mean_competition" in body


@node
def test_relative_block_renders_state_mean_n_not_a_z_n(client, tmp_path):
    """Given a payload carrying BOTH a relative-pace N (12,845) and a
    DIFFERENT Z-score population N (7) with its own μ/σ, the block renders
    the relative-pace N and neither the Z N nor the Z μ/σ."""
    js = _js(client)
    out = _run_node(js, tmp_path, "m.relPaceHTML(%s)"
                    % json.dumps(_payload(True, True, state_n=12845,
                                          z_n=7)))
    assert "12,845" in out                 # relative-pace benchmark N
    assert "6.769" not in out              # Z historical μ
    assert "0.897" not in out              # Z historical σ


def test_z_score_panel_keeps_its_own_n_separately(client):
    """The PACE Z-SCORE panel keeps its OWN N — labelled Z-SCORE HISTORICAL
    N and sourced from the Z payload (zm.n), never from the ctx contract."""
    js = _js(client)
    assert "Z-SCORE HISTORICAL N" in js
    i = js.index("Z-SCORE HISTORICAL N")
    seg = js[i:i + 200]
    assert "zm.n" in seg, seg
    assert "state_mean_n" not in seg, seg
    # and the Z readout line labels its N as the Z-score population's
    assert "Z-SCORE N=" in js


def test_both_populations_are_labelled_distinctly(client):
    """The two N's must be unmistakably separate populations in the UI."""
    js = _js(client)
    assert "Z-SCORE HISTORICAL N" in js
    assert "Historical N" in js
    # the block title names the relative-pace population explicitly
    assert "RELATIVE PACE — HISTORICAL STATE" in js
    # an explicit note links the two as separate
    assert "SEPARATE calculation from the RELATIVE PACE" in js


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
        # explicit relative-pace / state-mean benchmark contract
        assert ctx["state_mean_provider"] == "BETUAL"
        assert ctx["state_mean_competition"] == "betual-nba"
        assert ctx["state_mean_period"] == "Q4"
        assert ctx["state_mean_state"] == "P090"
        assert ctx["state_mean_n"] == ctx["benchmark_n"]      # documented alias
        assert ctx["state_mean_pace"] == ctx["historical_avg_pace"]
        assert ctx["state_mean_key"] == ctx["benchmark_key"]
    assert tt["actual_vs_state_mean"] == "BELOW"
    assert tt["required_vs_state_mean"] == "AT/ABOVE"
    assert tf["required_vs_state_mean"] == "BELOW"
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
