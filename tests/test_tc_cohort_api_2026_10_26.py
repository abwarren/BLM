"""Read-only cohort API contract tests (doc §6, §8.3).

The derived read model is built by the real rescore over synthetic fixtures, so
these tests exercise the same code path the dashboard hits — no mocks, no
production data.

Run:  python -m pytest tests/test_tc_cohort_api_2026_10_26.py -q
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))
from test_tc_rescore_firewall_2026_10_26 import build  # synthetic fixture builder

from blm_v4.cohorts import rescore, spec


@pytest.fixture()
def client(tmp_path, monkeypatch):
    src = build(str(tmp_path / "src.db"))
    out = str(tmp_path / "derived.db")
    rescore.run(src, out, verbose=False, run_at="2026-10-26T00:00:00Z")
    monkeypatch.setenv("BLM_COHORT_DB", out)
    # cohorts.api reads the env at call time (not import time) — no reload needed
    import blm_v4.cohorts.api as api_mod
    app = FastAPI()
    app.include_router(api_mod.router)
    return TestClient(app)


def test_spec_endpoint_serves_the_frozen_manifest(client):
    r = client.get("/api/v4/cohorts/spec")
    assert r.status_code == 200
    body = r.json()
    assert body["rule_version"] == spec.RULE_VERSION
    assert [c["code"] for c in body["cohorts"]] == list(spec.QUALIFYING_ORDER)


def test_summary_reports_denominators_rate_and_wilson(client):
    r = client.get("/api/v4/cohorts/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["rule_version"] == spec.RULE_VERSION
    x = next(c for c in body["cohorts"] if c["code"] == "TC-X")
    assert x["unique_games"] == 1 and x["under"] == 1 and x["settled"] == 1
    assert x["under_rate"] == 1.0
    lo, hi = x["wilson95"]
    assert 0.0 <= lo <= hi <= 1.0
    assert x["market_coverage"] == 1.0
    assert body["read_model"]["run"] == "2026-10-26T00:00:00Z"


def test_summary_never_counts_push_as_a_win(tmp_path, monkeypatch):
    src = build(str(tmp_path / "p.db"), g1_result=200)      # final == line -> PUSH
    out = str(tmp_path / "pd.db")
    rescore.run(src, out, verbose=False)
    monkeypatch.setenv("BLM_COHORT_DB", out)
    import blm_v4.cohorts.api as api_mod
    app = FastAPI(); app.include_router(api_mod.router)
    c = TestClient(app)
    x = next(c2 for c2 in c.get("/api/v4/cohorts/summary").json()["cohorts"]
             if c2["code"] == "TC-X")
    assert x["push"] == 1 and x["settled"] == 0 and x["under_rate"] is None


def test_ledger_paginates_and_filters(client):
    r = client.get("/api/v4/cohorts/ledger", params={"limit": 1, "offset": 0})
    body = r.json()
    assert body["total"] >= 2 and len(body["rows"]) == 1
    first = body["rows"][0]
    assert first["source_type"] == "reconstructed_firing"
    assert first["trigger_line"] == 200.0
    fired = client.get("/api/v4/cohorts/ledger",
                       params={"source_type": "reconstructed_firing"}).json()
    assert all(x["source_type"] == "reconstructed_firing" for x in fired["rows"])
    tc_x = client.get("/api/v4/cohorts/ledger", params={"cohort": "TC-X"}).json()
    assert all("TC-X" in x["qualifying_cohorts"] for x in tc_x["rows"])


def test_ledger_rejects_unknown_cohort_and_bad_status(client):
    assert client.get("/api/v4/cohorts/ledger", params={"cohort": "TC-Z"}).status_code == 422
    assert client.get("/api/v4/cohorts/ledger", params={"status": "NOPE"}).status_code == 422
    assert client.get("/api/v4/cohorts/ledger", params={"limit": 0}).status_code == 422


def test_comparisons_group_by_competition_and_price(client):
    body = client.get("/api/v4/cohorts/comparisons", params={"by": "competition"}).json()
    assert body["by"] == "competition"
    assert body["buckets"] and body["buckets"][0]["bucket"] == "betual-euroleague"
    price = client.get("/api/v4/cohorts/comparisons", params={"by": "price"}).json()
    # bands are [lo, hi): the fixture price is 1.80 -> "1.8-2"
    assert price["buckets"][0]["bucket"] == "1.8-2"
    assert client.get("/api/v4/cohorts/comparisons",
                      params={"by": "nope"}).status_code == 422


def test_quality_endpoint_exposes_unknown_reasons_and_caveats(client):
    body = client.get("/api/v4/cohorts/quality").json()
    assert body["ledger_rows"] >= 2
    assert "caveats" in body and any("RECONSTRUCTED" in c for c in body["caveats"])
    assert body["sources"].get("reconstructed_firing") == 1


def test_api_is_read_only_on_the_derived_db(client, tmp_path):
    db = os.environ["BLM_COHORT_DB"]
    before = (os.path.getsize(db), os.stat(db).st_mtime_ns)
    for path in ("/api/v4/cohorts/summary", "/api/v4/cohorts/ledger",
                 "/api/v4/cohorts/quality", "/api/v4/cohorts/comparisons"):
        assert client.get(path).status_code == 200
    assert (os.path.getsize(db), os.stat(db).st_mtime_ns) == before


def test_missing_read_model_fails_soft(tmp_path, monkeypatch):
    monkeypatch.setenv("BLM_COHORT_DB", str(tmp_path / "nope.db"))
    import blm_v4.cohorts.api as api_mod
    app = FastAPI(); app.include_router(api_mod.router)
    r = TestClient(app).get("/api/v4/cohorts/summary")
    assert r.status_code == 503
