"""BLM Test Suite — Pytest configuration.

Enables auto mode for asyncio tests so fixtures and tests with ``async def``
are automatically handled without requiring an explicit ``@pytest.mark.asyncio``
on every test.

PREDICTION-GENERATION FREEZE: the scorecard gates classic prediction
writing behind BLM_PREDICTION_FREEZE (default 1 = frozen — the production
datum).  The suite as a whole exercises the prediction machinery as a
legacy / re-authorizable path, so the default UNDER TEST is unfrozen
(env "0") via this file; the dedicated freeze tests
(tests/test_prediction_freeze.py) explicitly re-enable the freeze
(BLM_PREDICTION_FREEZE=1) and prove the frozen behavior.
"""

import os
from pathlib import Path

import pytest

# Default for the whole suite (see module docstring).  The scorecard
# reads this env at call time, so setting it here (before tests run)
# reliably unfreezes prediction generation for machinery tests.
os.environ.setdefault("BLM_PREDICTION_FREEZE", "0")

# /api/v4/live single-flight cache (directive 2026-10-01).  The route now
# reuses one build for BLM_LIVE_CACHE_TTL_S seconds.  Tests assert on the
# result of a CALL, so the default under test is TTL 0 — every call builds
# afresh, exactly the pre-fix semantics the suite was written against.
# The dedicated cache/coalescing tests (tests/test_live_singleflight_cache.py)
# set an explicit TTL via monkeypatch to exercise the cached behaviour.
# The api module reads this env PER CALL, so monkeypatch takes effect
# without a reimport.
os.environ.setdefault("BLM_LIVE_CACHE_TTL_S", "0")

# ── AUTHENTICATION TEST HARNESS ────────────────────────────────────────
# Shared by the tests/test_auth_*.py suites.  Every fixture builds an
# isolated auth database in tmp_path with two throwaway accounts whose
# passwords are generated per run — no credential literal ever exists in
# source, and no test can reach the production auth database.

ADMIN_USERNAME = "t-admin"
USER_USERNAME = "t-user"


@pytest.fixture
def auth_root(tmp_path, monkeypatch):
    """Environment for an isolated auth stack (auth ON, cheap hashing)."""
    import secrets

    admin_pw = "A-" + secrets.token_urlsafe(16)
    user_pw = "U-" + secrets.token_urlsafe(16)
    monkeypatch.setenv("BLM_ENV", "test")
    monkeypatch.setenv("BLM_AUTH_ENABLED", "1")
    monkeypatch.setenv("BLM_AUTH_DB", str(tmp_path / "blm_auth.db"))
    monkeypatch.setenv("BLM_AUTH_COOKIE_SECURE", "0")
    monkeypatch.setenv("BLM_AUTH_TRUST_PROXY", "0")
    monkeypatch.setenv("BLM_AUTH_BCRYPT_ROUNDS", "4")
    monkeypatch.delenv("BLM_AUTH_PUBLIC_PATHS", raising=False)
    monkeypatch.delenv("BLM_SEED_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("BLM_SEED_USER_PASSWORD", raising=False)
    return {"root": tmp_path, "admin_pw": admin_pw, "user_pw": user_pw,
            "admin": ADMIN_USERNAME, "user": USER_USERNAME}


@pytest.fixture
def auth_stack(auth_root):
    """(app, state, TestClient) for a minimal app with the production guard.

    The routes mirror the real surfaces (page, /dashboard, an /api/v4 read,
    a mutating /api/v4 write and a WebSocket) so authorisation is exercised
    against the same shapes the BLM application serves.
    """
    from fastapi import FastAPI, WebSocket
    from fastapi.responses import HTMLResponse
    from fastapi.testclient import TestClient

    from blm_v4.auth import install as install_auth
    from blm_v4.auth.config import AuthConfig
    from blm_v4.auth.seed import seed
    from blm_v4.auth.store import AuthStore

    root = auth_root["root"]
    app = FastAPI()

    @app.get("/")
    def dashboard_page():
        return HTMLResponse("<h1>BLM DASHBOARD</h1>")

    @app.get("/dashboard")
    def dashboard_alias():
        return HTMLResponse("<h1>BLM DASHBOARD</h1>")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/api/v4/live")
    def v4_live():
        return {"games": [], "classification": None}

    @app.get("/api/v2/status")
    def v2_status():
        return {"status": "ok"}

    @app.post("/api/v4/betting/settings")
    def bet_settings(payload: dict):
        return {"ok": True, "received": payload}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    config = AuthConfig.from_env(root)
    seed(AuthStore(config.db_path), rounds=4,
         accounts=[
             {"username": ADMIN_USERNAME, "password": auth_root["admin_pw"],
              "role": "admin", "email": "admin@blm.test", "label": "admin"},
             {"username": USER_USERNAME, "password": auth_root["user_pw"],
              "role": "user", "email": None, "label": "user"},
         ],
         out=open(os.devnull, "w"))
    state = install_auth(app, root, trust_proxy=False)
    client = TestClient(app)
    yield {"app": app, "state": state, "client": client, **auth_root}


def login(client, username, password, **kwargs):
    """POST /api/auth/login and return the response."""
    body = {"username": username, "password": password}
    body.update(kwargs)
    return client.post("/api/auth/login", json=body)


def pytest_configure(config):
    """Register the asyncio mode marker."""
    config.addinivalue_line(
        "markers",
        "asyncio: mark test as async (auto-detected in auto mode)",
    )


# ── PRODUCTION COLLECTOR HOST GUARD (2026-09-30 directive) ────────────
# This box RUNS THE PRODUCTION COLLECTOR as a systemd user unit.  The
# 2026-09-29 restart-storm forensics measured the host at swap-full
# memory thrash (PSI-mem 40%, PSI-io 78%) while test/browser workloads
# ran beside it; every pytest pass adds browser trees and CPU load to a
# box that has none to spare.  Heavy pytest runs here are therefore
# DENIED by default while a production collector/server process is
# alive.  Run the targeted collector/liveness sets on a scratch tree,
# or pass BLM_ALLOW_HEAVY_TESTS=1 to accept the production impact
# explicitly (e.g. for the 2–3 file liveness set, NOT the full suite).

def _production_pipeline_pids() -> list[tuple[int, str]]:
    """(pid, cmdline) of live production pipeline processes."""
    found: list[tuple[int, str]] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmd = (proc / "cmdline").read_bytes().replace(
                b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "blm_v4.collector" in cmd or "BLM/server.py" in cmd:
            found.append((int(proc.name), cmd.strip()))
    return found


def _refuse_production_host_run() -> None:
    if os.environ.get("BLM_ALLOW_HEAVY_TESTS") == "1":
        return
    procs = _production_pipeline_pids()
    if not procs:
        return
    detail = "\n".join(
        f"  pid {pid}: {cmd[:90]}" for pid, cmd in sorted(procs))
    raise pytest.UsageError(
        "REFUSED: a production BLM pipeline is running on this host:\n"
        f"{detail}\n"
        "pytest workloads on this box compete with the collector for the\n"
        "CPU/RAM the 2026-09-29 restart-storm forensics showed to be the\n"
        "root kill driver.  Options:\n"
        "  * run the targeted set in a scratch checkout (see AGENTS.md),\n"
        "  * or re-run with BLM_ALLOW_HEAVY_TESTS=1 to accept the\n"
        "    production impact explicitly.")


_refuse_production_host_run()


@pytest.fixture
def signed_in(auth_stack):
    """An authenticated client (admin role) with its CSRF header attached."""
    client = auth_stack["client"]
    resp = login(client, auth_stack["admin"], auth_stack["admin_pw"])
    assert resp.status_code == 200, resp.text
    client.headers.update({"X-CSRF-Token": resp.json()["csrf_token"]})
    return auth_stack
