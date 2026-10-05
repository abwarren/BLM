"""Cache policy for the operator dashboard assets (2026-10-05).

Why this exists: the server served a fixed dashboard.js but a dashboard tab
kept executing the pre-fix bundle, because the asset had ETag/Last-Modified
but NO Cache-Control and an unversioned URL.  These tests pin the mechanism
that makes a deploy deterministic.

Run: BLM_ALLOW_HEAVY_TESTS=1 python3 -m pytest tests/test_dashboard_asset_cache_2026_10_05.py -q
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient

from blm_v4.dashboard.asset_cache import NO_CACHE, NoCacheStaticFiles

SERVER_PY = Path(__file__).resolve().parents[1] / "server.py"


@pytest.fixture()
def client(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "dashboard.js").write_text("console.log('hi');")
    (static / "styles.css").write_text("body{color:#fff}")
    (static / "index.html").write_text("<html></html>")

    app = FastAPI()
    app.mount("/static", NoCacheStaticFiles(directory=str(static)), name="dash")

    @app.get("/", include_in_schema=False)
    async def root():
        return FileResponse(str(static / "index.html"),
                            headers={"Cache-Control": NO_CACHE})
    return TestClient(app)


def test_static_js_is_served_with_no_cache(client):
    r = client.get("/static/dashboard.js")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache", \
        "a browser must revalidate; without this a stale bundle survives a deploy"


def test_static_css_is_served_with_no_cache(client):
    r = client.get("/static/styles.css")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"


def test_etag_is_preserved_so_revalidation_is_cheap(client):
    """no-cache keeps the ETag: a changed bundle is picked up, an unchanged one is a 304."""
    r1 = client.get("/static/dashboard.js")
    etag = r1.headers.get("etag")
    assert etag, "StaticFiles must keep supplying an ETag"
    r2 = client.get("/static/dashboard.js", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.headers.get("cache-control") == "no-cache"


def test_dashboard_html_is_served_with_no_cache(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache", \
        "the HTML must revalidate too: a stale HTML keeps pointing at the old bundle URL"


def test_missing_asset_still_404s(client):
    assert client.get("/static/does-not-exist.js").status_code == 404


def test_server_wires_the_no_cache_policy():
    """The running server must actually use the policy (not just define it)."""
    src = SERVER_PY.read_text()
    assert "NoCacheStaticFiles(directory=" in src, \
        "server.py must mount the no-cache static class"
    assert 'headers={"Cache-Control": NO_CACHE}' in src, \
        "the '/' dashboard HTML route must send Cache-Control: no-cache"


def test_dashboard_html_references_versioned_assets():
    """Belt and braces for any intermediate cache/CDN: a changed bundle gets a new URL."""
    idx = (SERVER_PY.parent / "blm_v4" / "dashboard" / "static" / "index.html").read_text()
    for ref in ("/static/styles.css?v=", "/static/dashboard.js?v=",
                "/static/stats.js?v=", "/static/explorer.js?v="):
        assert ref in idx, f"{ref} must carry a version query"
    assert '<script src="/static/dashboard.js"></script>' not in idx
    assert '<link rel="stylesheet" href="/static/styles.css">' not in idx
