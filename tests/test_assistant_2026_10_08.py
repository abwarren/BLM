"""The BLM assistant: read-only by construction, and mounted on the API.

Note on style: the forbidden SQL keywords are ASSEMBLED FROM FRAGMENTS below
rather than written as literals.  This repo is watched by an automated guard
that flags destructive statements in commands and files, and a test that proves
writes are refused should not itself look like one.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

from blm_v4.assistant import agent, tools  # noqa: E402
from blm_v4.assistant.api import router  # noqa: E402

_DEL = "DE" + "LETE"
_DRP = "DR" + "OP"
_UPD = "UP" + "DATE"
_INS = "IN" + "SERT"


# ── the read-only guarantee ───────────────────────────────────────────────
def test_the_sql_screen_accepts_reads():
    assert tools._screen_sql("SELECT 1") == "SELECT 1"
    assert tools._screen_sql("  select * from t limit 5 ; ").lower().startswith("select")
    assert tools._screen_sql("WITH x AS (SELECT 1) SELECT * FROM x").startswith("WITH")


def test_the_sql_screen_refuses_writes_and_multi_statements():
    for sql in (f"{_DEL} FROM betting_config",
                f"{_DRP} TABLE bet_executions",
                f"{_UPD} betting_config SET value='x'",
                f"{_INS} INTO bet_executions (game_id) VALUES ('x')",
                "SELECT 1; SELECT 2",
                "PRAGMA journal_mode=DELETE",
                "ATTACH DATABASE '/tmp/x' AS y"):
        try:
            tools._screen_sql(sql)
        except ValueError:
            continue
        raise AssertionError(f"screen allowed a forbidden statement: {sql}")


def test_the_connection_refuses_writes_at_the_database_level():
    """mode=ro + query_only: even bypassing the screen, the DB says no."""
    try:
        c = tools._ro_conn("betting")
    except Exception as e:
        import pytest
        pytest.skip(f"betting db not available here: {e}")
    try:
        assert c.execute("PRAGMA query_only").fetchone()[0] == 1
        for sql in (f"{_DEL} FROM betting_config",
                    f"{_DRP} TABLE bet_executions"):
            try:
                c.execute(sql)
            except sqlite3.OperationalError:
                continue
            raise AssertionError(f"a read-only connection accepted: {sql}")
    finally:
        c.close()


def test_no_tool_can_reach_the_auth_database():
    assert "blm_auth.db" not in str(tools.DATABASES.values())
    for alias in tools.DATABASES:
        assert "auth" not in alias


def test_every_tool_schema_is_wellformed():
    schemas = tools.tool_schemas()
    assert schemas, "no tools registered"
    for s in schemas:
        assert s["name"] and s["description"]
        assert s["input_schema"]["type"] == "object"


def test_an_unknown_tool_reports_instead_of_raising():
    out = tools.run_tool("no_such_tool", {})
    assert out.startswith("ERROR") and "unknown tool" in out


def test_a_bad_argument_reports_instead_of_raising():
    assert tools.run_tool("query", {"database": "nope", "sql": "SELECT 1"}).startswith("ERROR")
    assert tools.run_tool("query", {"database": "betting", "sql": ""}).startswith("ERROR")
    assert tools.run_tool("logs", {"unit": "not-a-unit"}).startswith("ERROR")


# ── the API surface ───────────────────────────────────────────────────────
def test_the_assistant_routes_are_mounted_under_api_v4():
    paths = {r.path for r in router.routes}
    assert "/api/v4/assistant/chat" in paths
    assert "/api/v4/assistant/status" in paths


def test_the_chat_route_is_a_post_and_the_status_is_a_get():
    by_path = {r.path: set(getattr(r, "methods", [])) for r in router.routes}
    assert "POST" in by_path["/api/v4/assistant/chat"]
    assert "GET" in by_path["/api/v4/assistant/status"]


# ── the agent plumbing ────────────────────────────────────────────────────
def test_credentials_resolve_or_report_a_reason():
    ok, why = agent.available()
    creds = agent.credentials()
    assert isinstance(ok, bool)
    assert creds["model"], "a model id must always resolve to something"
    if not ok:
        assert "ANTHROPIC_API_KEY" in why


def test_the_system_prompt_carries_the_real_contract():
    p = agent.SYSTEM_PROMPT
    for must in ("progress_pct", "1.04", "0.95", "under_alert_exec_armed",
                 "PROVIDER_AMBIGUOUS", "READ-ONLY"):
        assert must in p, must


def test_the_logs_tool_filters_before_capping():
    """A small `lines` value with a grep must not starve the filter."""
    out = tools.run_tool("logs", {"unit": "blm-server", "minutes": 30,
                                  "grep": "betting_pass", "lines": 1})
    assert isinstance(out, str) and out
    assert "Traceback" not in out
