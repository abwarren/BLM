"""The in-server legacy V1 collector is opt-out via BLM_V1_COLLECTOR.

REQUIREMENT (2026-10-29, operator directive "1"): gate the duplicate headless
scraping loop off with an env flag, DEFAULT ON so behaviour is unchanged unless
the deployment opts out.

Why it matters: the server ran a SECOND headless PokerBet scraper beside the
standalone blm-collector — 18 chromium processes and roughly a third of the
box's CPU — which starved the v4 collector's page captures (fast ticks 50-178 s
against a 5 s design, heartbeat past the dashboard's 90 s OFFLINE threshold).
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path("/home/ubuntu/blm-fix-price-mapping")
sys.path.insert(0, str(ROOT))


def _server_module():
    """Import server.py without running it (it only defines app in main())."""
    spec = importlib.util.spec_from_file_location("blm_server_entry", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_v1_collector_gate_is_off_only_on_an_explicit_falsey_value():
    srv = _server_module()

    # DEFAULT ON — a deployment that says nothing keeps the old behaviour
    assert srv._v1_collector_enabled({}) is True
    assert srv._v1_collector_enabled({"BLM_V1_COLLECTOR": "1"}) is True
    assert srv._v1_collector_enabled({"BLM_V1_COLLECTOR": "true"}) is True
    assert srv._v1_collector_enabled({"BLM_V1_COLLECTOR": "yes"}) is True
    # an unrecognised value is ON, never silently off
    assert srv._v1_collector_enabled({"BLM_V1_COLLECTOR": "banana"}) is True

    # explicit off
    for off in ("0", "false", "FALSE", "no", "off", "", " 0 "):
        assert srv._v1_collector_enabled({"BLM_V1_COLLECTOR": off}) is False, off
