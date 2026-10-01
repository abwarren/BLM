"""DASHBOARD — COLLECTOR-STATUS SEMANTICS (directive 2026-10-01).

The 2026-10-01 incident had TWO independent conflation bugs in the
dashboard, both of which rendered as "COLLECTOR OFFLINE" while collection
was perfectly healthy:

  * every /api/v4/status request timed out behind a saturated worker pool
    → paintCollectorStatus(null) → "collector: OFFLINE"; and
  * every request from a tab whose session had expired returned 401
    → the same pill.

The directive makes collector health and API availability SEPARATE
questions.  These tests pin the resulting state machine in the served JS.
They are source-contract tests (the same style as tests/test_auth_ui.py):
the dashboard has no JS test runner in this repo, so the contract is
asserted against the shipped source, and the syntax is verified with
``node --check``.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "blm_v4" / "dashboard" / "static"
DASHBOARD_JS = STATIC / "dashboard.js"
INDEX_HTML = STATIC / "index.html"


@pytest.fixture(scope="module")
def js() -> str:
    return DASHBOARD_JS.read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """Extract a top-level ``function name(...) { ... }`` body by brace
    matching (source-level inspection, no JS runtime required)."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
                  src)
    assert m, f"function {name}() not found in dashboard.js"
    start = src.index("{", m.start())
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unterminated body for {name}()")


# ══════════════════════════════════════════════════════════════════════
# Syntax
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dashboard_js_is_syntactically_valid():
    proc = subprocess.run(["node", "--check", str(DASHBOARD_JS)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node --check failed:\n{proc.stderr}"


# ══════════════════════════════════════════════════════════════════════
# FIX 2 — collector health vs API availability
# ══════════════════════════════════════════════════════════════════════

def test_an_explicit_staleness_threshold_governs_offline(js):
    assert "COLLECTOR_STALE_AFTER_S" in js
    assert re.search(r"const\s+COLLECTOR_STALE_AFTER_S\s*=\s*\d+", js)


def test_paint_collector_status_takes_a_verified_flag(js):
    body = _function_body(js, "paintCollectorStatus")
    assert re.search(r"function\s+paintCollectorStatus\s*\(\s*st\s*,\s*verified\s*\)",
                     js), "paintCollectorStatus must be told whether the read succeeded"


def test_only_a_verified_heartbeat_read_can_render_offline(js):
    """"COLLECTOR OFFLINE" is emitted exactly ONCE, inside the
    `tickAge > COLLECTOR_STALE_AFTER_S` branch — i.e. only when a real
    heartbeat was read and found stale."""
    body = _function_body(js, "paintCollectorStatus")
    assert body.count('"collector: OFFLINE"') == 1, (
        "OFFLINE must be emitted from exactly one place")
    stale_guard = body.index("tickAge > COLLECTOR_STALE_AFTER_S")
    offline = body.index('"collector: OFFLINE"')
    assert stale_guard < offline, (
        "OFFLINE must sit inside the verified-staleness branch")
    # and that branch returns, so it cannot fall through to another state
    assert "return;" in body[offline:offline + 400]


def test_unverified_state_never_says_offline(js):
    """When /status cannot be reached the collector is UNVERIFIED."""
    body = _function_body(js, "paintCollectorStatus")
    unverified = body.index('"collector: UNVERIFIED"')
    tail = body[unverified:]
    assert "OFFLINE" not in tail, (
        "the unverified path must never render COLLECTOR OFFLINE")
    assert "cannot be verified" in body


def test_unverified_state_surfaces_the_last_verified_tick(js):
    body = _function_body(js, "paintCollectorStatus")
    assert "lastCollectorState" in js
    assert "last verified tick" in body


def test_api_unavailable_banner_text_is_distinct_from_collector_offline(js):
    assert "API/DATA UNAVAILABLE" in js
    assert "data collection is DOWN" in js          # the genuine-offline banner
    body = _function_body(js, "paintCollectorStatus")
    assert "API/DATA UNAVAILABLE" in body


def test_a_status_request_failure_paints_unverified_not_offline(js):
    body = _function_body(js, "pollCollectorStatus")
    # the catch path passes verified=false
    assert re.search(r"paintCollectorStatus\(\s*null\s*,\s*false\s*\)", body)
    assert re.search(r"paintCollectorStatus\(\s*await\s+resp\.json\(\)\s*,\s*true\s*\)",
                     body)


# ══════════════════════════════════════════════════════════════════════
# FIX 3 — 401 is AUTH, not collector state
# ══════════════════════════════════════════════════════════════════════

def test_a_401_on_the_status_channel_is_not_painted_as_a_collector_state(js):
    body = _function_body(js, "pollCollectorStatus")
    assert re.search(r"resp\.status\s*===\s*401", body)
    assert "return;" in body[body.index("401"):body.index("401") + 120]


def test_session_expiry_halts_polling_and_routes_to_login(js):
    body = _function_body(js, "onSessionExpired")
    assert "stopAllPolling()" in body, "401 must stop hammering protected endpoints"
    assert "paintSessionExpired()" in body
    assert re.search(r"/login\?error=expired", body)


def test_the_auth_guard_calls_the_session_expired_handler(js):
    start = js.index("if (res.status === 401 && !isLoginUrl(input)) {")
    branch = js[start:js.index("});", start)]
    assert "onSessionExpired()" in branch
    assert "window.location.replace" not in branch, (
        "the 401 branch must explain the expiry, not silently redirect")


def test_session_expired_state_is_its_own_label(js):
    body = _function_body(js, "paintSessionExpired")
    assert "SESSION EXPIRED" in body
    assert "OFFLINE" not in body


def test_stop_all_polling_clears_every_timer(js):
    body = _function_body(js, "stopAllPolling")
    for handle in ("liveTimer", "statusTimer", "bettingTimer", "traceTimer"):
        assert handle in body, f"{handle} is not cleared on session expiry"


# ══════════════════════════════════════════════════════════════════════
# FIX 2 (cont) — ONE writer for collector health
# ══════════════════════════════════════════════════════════════════════

def test_render_status_no_longer_writes_the_collector_pill(js):
    """The /live payload used to paint the collector pill too, so the two
    channels raced and a failed/slow/cached /live could overwrite a healthy
    verdict.  The dedicated /status channel is now the only writer."""
    body = _function_body(js, "renderStatus")
    assert "collectorPill" not in body, (
        "renderStatus must not write the collector pill")
    assert "OFFLINE" not in body


def test_the_failed_live_poll_does_not_touch_the_collector_pill(js):
    body = _function_body(js, "refresh")
    catch = body[body.index("catch (err)"):]
    assert "collectorPill" not in catch, (
        "a failed /live poll must not be reported as a collector state")


# ══════════════════════════════════════════════════════════════════════
# FIX 1 (frontend) — no overlapping /live polls
# ══════════════════════════════════════════════════════════════════════

def test_live_polls_never_overlap(js):
    body = _function_body(js, "refresh")
    assert "livePollInFlight" in body
    assert re.search(r"if\s*\(\s*livePollInFlight\s*\)\s*return", body), (
        "refresh() must bail out while a poll is still in flight")
    # the flag must be cleared on every exit path
    assert "finally" in body
    assert re.search(r"livePollInFlight\s*=\s*false", body)


def test_live_has_its_own_slower_cadence(js):
    assert re.search(r"const\s+LIVE_POLL_MS\s*=\s*(\d+)", js)
    interval = int(re.search(r"const\s+LIVE_POLL_MS\s*=\s*(\d+)", js).group(1))
    assert interval >= 10000, (
        "the /live cadence must be longer than the server cache TTL")
    assert re.search(r"liveTimer\s*=\s*setInterval\(\s*refresh\s*,\s*LIVE_POLL_MS",
                     js), "refresh must be driven by LIVE_POLL_MS"


def test_abort_is_documented_as_not_cancelling_server_work(js):
    """The directive requires that frontend cancellation is not treated as
    server-side cancellation."""
    idx = js.index("const LIVE_POLL_MS")
    block_start = js.rindex("/*", 0, idx)
    # normalise the wrap so the assertion is about the sentence, not the
    # line breaks in the comment
    block = " ".join(js[block_start:idx].split())
    assert "does NOT cancel the server-side Python work" in block
