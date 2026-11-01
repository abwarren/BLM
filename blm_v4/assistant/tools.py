"""Read-only data access for the BLM assistant.

Every tool here is read-only BY CONSTRUCTION, not by convention — the agent is
safe to expose to any signed-in operator because it can look but never touch:

* SQLite databases are opened with ``mode=ro`` in the URI, so the *database
  itself* refuses any write.  A confused or prompt-injected model cannot alter a
  row, drop a table, or checkpoint a WAL, no matter what SQL it produces.
* SQL is additionally screened to a single SELECT/WITH statement with the
  multi-statement separator forbidden, and every result set is row-capped.
* Journal access passes an argument LIST to ``journalctl`` (never a shell
  string) and validates the unit name against a fixed allowlist.
* No tool accepts a filesystem path or a command from the model.

The auth database (blm_auth.db) is deliberately NOT exposed: sessions and
password material are not assistant data.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

# ── the data root (same default the service uses) ─────────────────────────
DATA_ROOT = Path(os.environ.get("BLM_DATA_ROOT", "/home/ubuntu/BLM"))

#: database alias -> filename.  The auth DB is intentionally absent.
DATABASES = {
    "betting": "blm_betting.db",
    "pokerbet": "blm_pokerbet.db",
    "metrics": "blm_metrics_clean.db",
}

#: the units the assistant may read the journal for
UNITS = ("blm-server", "blm-collector")

#: env names that are NOT secrets and may be shown to the model.  Everything
#: else in the process environment is filtered out (keys, passwords, tokens).
SAFE_ENV_PREFIXES = ("BETTING_", "EXECUTION_", "BLM_", "STATS_", "COLLECTOR_")
SAFE_ENV_DENY = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CRED", re.I)

ROW_CAP = 200
SQL_TIMEOUT_S = 10.0
LOG_LINES_CAP = 400


# ── helpers ───────────────────────────────────────────────────────────────
def _db_path(alias: str) -> Path:
    name = DATABASES.get(str(alias or "").strip().lower())
    if not name:
        raise ValueError(f"unknown database '{alias}'. known: {sorted(DATABASES)}")
    p = DATA_ROOT / name
    if not p.exists():
        raise ValueError(f"{name} not found under {DATA_ROOT}")
    return p


def _ro_conn(alias: str) -> sqlite3.Connection:
    """A connection the database will not let us write through."""
    uri = f"file:{_db_path(alias)}?mode=ro"
    c = sqlite3.connect(uri, uri=True, timeout=SQL_TIMEOUT_S)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only = 1")          # belt and braces on top of mode=ro
    return c


def _screen_sql(sql: str) -> str:
    """One SELECT/WITH statement, no separators, no DDL/DML keywords."""
    q = (sql or "").strip().rstrip(";").strip()
    if not q:
        raise ValueError("empty SQL")
    if ";" in q:
        raise ValueError("only one statement per call")
    if not re.match(r"^(SELECT|WITH)\b", q, re.I):
        raise ValueError("only SELECT / WITH queries are allowed")
    if re.search(r"\b(ATTACH|DETACH|PRAGMA|VACUUM|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|REPLACE|REINDEX)\b", q, re.I):
        raise ValueError("write/maintenance statements are not allowed")
    return q


def _rows_to_text(rows: list[sqlite3.Row], truncated: bool) -> str:
    out = [dict(r) for r in rows]
    txt = json.dumps(out, default=str, indent=1)
    if truncated:
        txt += f"\n\n(row cap at {ROW_CAP} reached — narrow the query, or add your own LIMIT)"
    return txt or "[]"


# ── tools ─────────────────────────────────────────────────────────────────
def tool_list_databases() -> str:
    """List the readable databases with their table counts."""
    out = []
    for alias in sorted(DATABASES):
        try:
            with _ro_conn(alias) as c:
                n = c.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
            out.append({"database": alias, "tables": n})
        except Exception as e:                       # never raise into the loop
            out.append({"database": alias, "error": str(e)[:120]})
    return json.dumps(out, indent=1)


def tool_schema(database: str, table: str = "") -> str:
    """Tables in a database, or the columns of one table."""
    with _ro_conn(database) as c:
        if not table:
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()]
            return json.dumps({"database": database, "tables": names}, indent=1)
        cols = []
        for r in c.execute(f'PRAGMA table_info("{table}")').fetchall():
            cols.append({"name": r[1], "type": r[2]})
        if not cols:
            raise ValueError(f"no table '{table}' in {database}")
        pk = [r[1] for r in c.execute(f'PRAGMA table_info("{table}")').fetchall() if r[5]]
        return json.dumps({"database": database, "table": table,
                           "columns": cols, "primary_key": pk}, indent=1)


def tool_query(database: str, sql: str, limit: int = ROW_CAP) -> str:
    """Run ONE read-only SELECT against a BLM database."""
    q = _screen_sql(sql)
    cap = max(1, min(int(limit or ROW_CAP), ROW_CAP))
    has_limit = re.search(r"\bLIMIT\s+\d+\s*$", q, re.I) is not None
    wrapped = q if has_limit else f"SELECT * FROM (\n{q}\n) LIMIT {cap + 1}"
    with _ro_conn(database) as c:
        try:
            rows = c.execute(wrapped).fetchall()
        except sqlite3.Error as e:
            # retry unwrapped: a compound query with trailing ORDER BY can
            # upset the sub-select wrap, and mode=ro keeps this safe anyway
            rows = c.execute(q if has_limit else q + f" LIMIT {cap + 1}").fetchall()
    truncated = len(rows) > cap
    return _rows_to_text(rows[:cap], truncated)


def tool_logs(unit: str = "blm-server", minutes: int = 20,
              grep: str = "", lines: int = 120) -> str:
    """Recent journal lines for a BLM service (read-only)."""
    u = str(unit or "").strip()
    if u not in UNITS:
        raise ValueError(f"unit must be one of {list(UNITS)}")
    mins = max(1, min(int(minutes or 20), 1440))
    n = max(1, min(int(lines or 120), LOG_LINES_CAP))
    cmd = ["journalctl", "--user", "-u", u, "--since", f"-{mins} min",
           "--no-pager", "-o", "cat"]
    # fetch a wide window when filtering: -n before the grep would let a small
    # `lines` value starve the filter of anything to match
    cmd += ["-n", "2000" if grep else str(n)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return f"journalctl timed out reading {u}"
    txt = (p.stdout or "").strip() or "(no output)"
    if grep:
        needle = str(grep)
        kept = [ln for ln in txt.splitlines() if needle.lower() in ln.lower()]
        txt = "\n".join(kept[-n:]) if kept else f"(no line matched {needle!r} in the last {mins} min)"
    if len(txt) > 12000:
        txt = txt[-12000:]
        txt = "...(trimmed to the last 12000 chars)...\n" + txt
    return txt


def tool_config() -> str:
    """The live betting configuration and the non-secret service limits."""
    out: dict = {}
    try:
        with _ro_conn("betting") as c:
            out["betting_config"] = [dict(r) for r in c.execute(
                "SELECT key, value, updated_at_utc FROM betting_config ORDER BY key"
            ).fetchall()]
    except Exception as e:
        out["betting_config_error"] = str(e)[:160]
    limits = {}
    for k, v in os.environ.items():
        if k.startswith(SAFE_ENV_PREFIXES) and not SAFE_ENV_DENY.search(k):
            limits[k] = v
    out["service_limits"] = limits
    return json.dumps(out, indent=1, default=str)


def tool_health() -> str:
    """Whether the pipeline is alive: services, feed freshness, browser."""
    import urllib.request

    out: dict = {}
    # the server itself
    try:
        with urllib.request.urlopen("http://127.0.0.1:2262/healthz", timeout=5) as r:
            out["server_healthz"] = r.status
    except Exception as e:
        out["server_healthz"] = f"error: {type(e).__name__}"
    # systemd
    try:
        p = subprocess.run(
            ["systemctl", "--user", "is-active", "blm-server", "blm-collector"],
            capture_output=True, text=True, timeout=10)
        states = (p.stdout or "").split()
        out["services"] = dict(zip(("blm-server", "blm-collector"), states))
    except Exception as e:
        out["services"] = f"error: {type(e).__name__}"
    # feed freshness from the collector's own tables
    try:
        with _ro_conn("pokerbet") as c:
            snap = c.execute("SELECT MAX(captured_at) FROM snapshots").fetchone()[0]
        with _ro_conn("metrics") as c:
            proj = c.execute("SELECT MAX(captured_at) FROM clean_projections").fetchone()[0]
        out["newest_snapshot"] = snap
        out["newest_projection"] = proj
    except Exception as e:
        out["feed"] = f"error: {str(e)[:120]}"
    # the exec browser's debug port (the thing that silently kills submissions)
    try:
        with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=4) as r:
            out["exec_browser_cdp"] = "up" if r.status == 200 else r.status
    except Exception:
        out["exec_browser_cdp"] = "DOWN — bets cannot submit"
    return json.dumps(out, indent=1, default=str)


# ── the registry the agent loop consumes ──────────────────────────────────
TOOLS = {
    "list_databases": {
        "fn": tool_list_databases,
        "schema": {"name": "list_databases",
                   "description": "List the readable BLM databases with their table counts.",
                   "input_schema": {"type": "object", "properties": {}}},
    },
    "schema": {
        "fn": tool_schema,
        "schema": {"name": "schema",
                   "description": ("List the tables in a database, or the columns of one "
                                   "table. Call this before writing SQL."),
                   "input_schema": {"type": "object", "properties": {
                       "database": {"type": "string", "enum": sorted(DATABASES)},
                       "table": {"type": "string", "description": "optional"}},
                       "required": ["database"]}},
    },
    "query": {
        "fn": tool_query,
        "schema": {"name": "query",
                   "description": ("Run ONE read-only SELECT (or WITH ... SELECT) against a "
                                   "BLM database. Writes are impossible: the connection is "
                                   "opened read-only and the database refuses them."),
                   "input_schema": {"type": "object", "properties": {
                       "database": {"type": "string", "enum": sorted(DATABASES)},
                       "sql": {"type": "string", "description": "a single SELECT / WITH statement"},
                       "limit": {"type": "integer", "description": f"max rows (cap {ROW_CAP})"}},
                       "required": ["database", "sql"]}},
    },
    "logs": {
        "fn": tool_logs,
        "schema": {"name": "logs",
                   "description": "Recent journal lines for blm-server or blm-collector.",
                   "input_schema": {"type": "object", "properties": {
                       "unit": {"type": "string", "enum": list(UNITS)},
                       "minutes": {"type": "integer"},
                       "grep": {"type": "string", "description": "case-insensitive substring filter"},
                       "lines": {"type": "integer"}}}},
    },
    "config": {
        "fn": tool_config,
        "schema": {"name": "config",
                   "description": ("The live betting_config table (unit price, auto-betting "
                                   "switch) plus the non-secret BETTING_*/EXECUTION_* limits "
                                   "the service runs with."),
                   "input_schema": {"type": "object", "properties": {}}},
    },
    "health": {
        "fn": tool_health,
        "schema": {"name": "health",
                   "description": ("Pipeline health: server healthz, systemd states, feed "
                                   "freshness, and whether the exec browser's debug port is up."),
                   "input_schema": {"type": "object", "properties": {}}},
    },
}


def run_tool(name: str, args: dict | None = None) -> str:
    """Dispatch a tool call.  NEVER raises — errors go back to the model."""
    entry = TOOLS.get(str(name))
    if entry is None:
        return f"ERROR: unknown tool '{name}'. Available: {sorted(TOOLS)}"
    try:
        return str(entry["fn"](**(args or {})))
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


def tool_schemas() -> list[dict]:
    return [t["schema"] for t in TOOLS.values()]
