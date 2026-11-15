"""Read-only query layer for execution evidence shown in the DB Explorer."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from blm_v4.explorer_analytics import (
    _number, execution_state, performance, settlement,
)

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PUBLIC_EXECUTION_FIELDS = (
    "id", "execution_id", "game_id", "alert_id", "checkpoint", "market",
    "selection", "triggered_line", "price", "unit_price", "stake_units",
    "stake_amount", "accepted_amount", "requested_amount", "simulated_amount",
    "status", "execution_state", "provider_ref", "error_code", "error_message",
    "rejection_reason", "requested_at_utc", "submitted_at_utc", "resolved_at_utc",
    "elapsed_ms", "day_utc",
)


def _connect_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def _day(value: str, name: str) -> str:
    if not _DAY.fullmatch(value or ""):
        raise ValueError(f"{name} must use YYYY-MM-DD")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def _safe_json(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def load_execution_page(betting_db: Path, pipeline_db: Path, *,
                        from_day: str, to_day: str, league: str = "",
                        execution: str = "", settlement_filter: str = "",
                        eligibility: str = "", qualification: str = "",
                        cohort: str = "", strategy: str = "",
                        page: int = 1, page_size: int = 100) -> dict:
    """Read one bounded ledger page, joining only authoritative final data."""
    start, end = _day(from_day, "from_day"), _day(to_day, "to_day")
    if start > end:
        raise ValueError("from_day must not be after to_day")
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 200))
    offset = (page - 1) * page_size
    conn_b = _connect_ro(Path(betting_db))
    try:
        total = int(conn_b.execute(
            "SELECT COUNT(*) FROM bet_executions WHERE day_utc BETWEEN ? AND ?",
            (start, end)).fetchone()[0])
        raw_rows = [{key: r[key] for key in _PUBLIC_EXECUTION_FIELDS
                     if key in r.keys()} for r in conn_b.execute(
            "SELECT * FROM bet_executions WHERE day_utc BETWEEN ? AND ? "
            "ORDER BY requested_at_utc DESC, id DESC",
            (start, end)).fetchall()]
        summary_complete = True
        ids = [r["execution_id"] for r in raw_rows if r.get("execution_id")]
        audits: dict[str, list[dict]] = defaultdict(list)
        for base in range(0, len(ids), 400):
            group = ids[base:base + 400]
            marks = ",".join("?" for _ in group)
            for audit in conn_b.execute(
                    f"SELECT execution_id,event,reason,details,at_utc FROM bet_audit "
                    f"WHERE execution_id IN ({marks}) ORDER BY id", group):
                item = dict(audit)
                details = _safe_json(item.pop("details", None))
                item["details_result"] = details.get("result")
                audits[item["execution_id"]].append(item)
    finally:
        conn_b.close()

    wanted = sorted({str(r.get("game_id") or "").split("#", 1)[0]
                     for r in raw_rows if r.get("game_id")})
    games: dict[str, dict] = {}
    finals: dict[str, dict] = {}
    league_options: list[str] = []
    conn_p = _connect_ro(Path(pipeline_db))
    try:
        if wanted:
            for base in range(0, len(wanted), 400):
                group = wanted[base:base + 400]
                marks = ",".join("?" for _ in group)
                for row in conn_p.execute(
                        f"SELECT source_game_id,competition,competition_slug,"
                        f"home_team,away_team FROM games WHERE source_game_id IN ({marks})",
                        group):
                    games[row["source_game_id"]] = dict(row)
                for row in conn_p.execute(
                        f"SELECT source_game_id,final_home,final_away,final_total,"
                        f"final_result_status,result_at FROM game_results "
                        f"WHERE source_game_id IN ({marks})", group):
                    finals[row["source_game_id"]] = dict(row)
    finally:
        conn_p.close()

    # Keep the option query bounded to this already date-limited execution
    # population. Scanning DISTINCT leagues across the full operational games
    # table is unnecessary and can be expensive on the production database.
    league_options = sorted({
        str(game.get("competition_slug") or game.get("competition"))
        for game in games.values()
        if game.get("competition_slug") or game.get("competition")
    }, key=str.casefold)

    rows = []
    for raw in raw_rows:
        gid = str(raw.get("game_id") or "")
        base_id = gid.split("#", 1)[0]
        game = games.get(base_id, {})
        final = finals.get(gid) or finals.get(base_id) or {}
        state = execution_state(raw, audits.get(raw.get("execution_id"), ()))
        final_total = (final.get("final_total")
                       if final.get("final_result_status") == "OK" else None)
        result = settlement(raw.get("selection"), raw.get("triggered_line"),
                            final_total)
        actual_profit = None
        if state["execution"] == "Confirmed placed" and result["status"] in {
                "Won", "Lost", "Push"}:
            stake, odds = _number(raw.get("accepted_amount")), _number(raw.get("price"))
            if stake is None:
                stake = _number(raw.get("stake_amount"))
            if stake is not None and stake > 0:
                if result["status"] == "Lost":
                    actual_profit = -stake
                elif result["status"] == "Push":
                    actual_profit = 0.0
                elif odds is not None and odds > 1:
                    actual_profit = stake * (odds - 1)
        actual_units = None
        unit_price = _number(raw.get("unit_price"))
        if actual_profit is not None and unit_price is not None and unit_price > 0:
            actual_units = round(actual_profit / unit_price, 4)
        row = {
            **raw,
            "fixture": " vs ".join(v for v in
                (game.get("home_team"), game.get("away_team")) if v),
            "league": game.get("competition_slug") or game.get("competition"),
            "final_home": final.get("final_home") if final_total is not None else None,
            "final_away": final.get("final_away") if final_total is not None else None,
            "final_total": final_total,
            "result_at": final.get("result_at") if final_total is not None else None,
            "settlement": result["status"],
            "points_margin": result["margin"],
            **state,
            "eligibility_reason": state["eligibility_reason"],
            "execution_reason": (raw.get("rejection_reason")
                                 or raw.get("error_code")
                                 or raw.get("error_message")),
            "actual_profit": (round(actual_profit, 2)
                              if actual_profit is not None else None),
            "actual_units": actual_units,
            "actual_stake": (raw.get("accepted_amount")
                             if raw.get("accepted_amount") is not None
                             else raw.get("stake_amount")),
            "actual_stake_source": ("accepted_amount"
                                    if raw.get("accepted_amount") is not None
                                    else "recorded stake_amount; accepted amount unavailable"),
            "audit_reasons": [a.get("reason") for a in
                              audits.get(raw.get("execution_id"), ())
                              if a.get("reason")],
            # No immutable signal row means these classifications are not
            # reconstructable from this attempt record.
            "signal_qualification": "Unknown",
            "cohort": "UNCLASSIFIED",
            "cohort_reason": "no persisted historical cohort membership",
            "strategy": "UNCLASSIFIED",
        }
        rows.append(row)

    if league:
        rows = [r for r in rows if r.get("league") == league]
    if execution:
        rows = [r for r in rows if r.get("execution") == execution]
    if settlement_filter:
        rows = [r for r in rows if r.get("settlement") == settlement_filter]
    if eligibility:
        rows = [r for r in rows if r.get("eligibility") == eligibility]
    if qualification:
        rows = [r for r in rows
                if r.get("signal_qualification") == qualification]
    if cohort:
        rows = [r for r in rows if r.get("cohort") == cohort]
    if strategy:
        rows = [r for r in rows if r.get("strategy") == strategy]
    matched = len(rows)
    page_rows = rows[offset:offset + page_size]
    return {
        "rows": page_rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        "matched": matched,
        "league_options": league_options,
        "summary_complete": summary_complete,
        "from_day": start,
        "to_day": end,
        "population": "execution-ledger only",
        "signal_ledger_available": False,
        "historical_eligibility_available": False,
        "historical_configuration_available": False,
        "theoretical_signal_performance_available": False,
        "actual_performance": performance(rows),
        "league_performance": _group_performance(rows, "league"),
        "eligibility_performance": _group_performance(rows, "eligibility"),
        "execution_performance": _group_performance(rows, "execution"),
        "strategy_performance": _group_performance(rows, "strategy"),
        "data_gaps": [
            "No immutable record exists for every signal-qualified fixture.",
            "Eligibility decisions and their historical configuration are not persisted for non-attempted signals.",
            "Historical cohort membership and qualifying bookmaker prices cannot be reconstructed for all signals.",
            "The requested historical cohort display names have no matching runtime cohort definitions in this checkout.",
        ],
    }


def _group_performance(rows: list[dict], key: str) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "Unknown")].append(row)
    return [{"group": name, **performance(group)}
            for name, group in sorted(groups.items(), key=lambda item: item[0].casefold())]
