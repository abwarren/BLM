"""BLM V4 — PokerBet eu-swarm WebSocket market parser.

The BetConstruct SPA pushes the ENTIRE live sportsbook over
``wss://eu-swarm-newm.pokerbet.co.za/``: game state (score, quarter,
clock), stats, and the full market tree — including the O/U total
(``MatchTotal`` / "Total Points") with its line (``base``) and Over/Under
prices.  This feed is independent of the event-view DOM: no clicks, no
navigation, no hydration dependency.

This module parses a captured frame into normalized market observations.
Pure functions of the frame payload — no network, no storage.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

# Market types we persist as O/U observations (game total + team totals).
_TOTAL_TYPES = {
    "MatchTotal": "Total Points",
    "MatchHomeTeamTotal2": "Team 1 Total Points",
    "MatchAwayTeamTotal2": "Team 2 Total Points",
}

_PERIODS = {1: "1st Quarter", 2: "2nd Quarter", 3: "3rd Quarter", 4: "4th Quarter"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _f(v: Any) -> Optional[float]:
    return None if v is None else float(v)


def _period_label(state: Optional[str], quarter: Any) -> Optional[str]:
    """Map the feed's game state to a period label.

    ``current_game_state`` is ``set1``..``set4`` for virtual basketball
    (occasionally ``fulltime``); ``additional_data.quarter`` carries the
    numeric quarter.  Fall back to the quarter number when available.
    """
    if quarter is not None:
        try:
            q = int(quarter)
            if q in _PERIODS:
                return _PERIODS[q]
        except (TypeError, ValueError):
            pass
    s = (state or "").strip().lower()
    if s.startswith("set"):
        try:
            q = int(s[3:])
            if q in _PERIODS:
                return _PERIODS[q]
        except (TypeError, ValueError):
            pass
    if s in ("fulltime", "ended", "finished"):
        return "4th Quarter"  # virtual replays settle as full-time
    return None


def parse_market_frame(frame: str) -> list[dict]:
    """Parse a WS frame into raw game-market payloads.

    Returns one dict per game that carries a ``market`` tree:
      {game_id, home_name, away_name, home_score, away_score,
       period_label, clock, markets: [{market_id, type, name, base,
       events: [{type_1, price, base, name}]}]}

    Non-market frames return [].
    """
    if not frame:
        return []
    try:
        obj = json.loads(frame)
    except (ValueError, TypeError):
        return []

    out: list[dict] = []
    _walk(obj, out)
    return out


def _walk(node: Any, out: list[dict]) -> None:
    """Depth-first walk: any dict containing a 'game' key whose value is a
    dict of {game_id: {..., 'market': {...}}} yields one payload per game."""
    if isinstance(node, dict):
        for key, val in node.items():
            if key == "game" and isinstance(val, dict):
                for gid, gbody in val.items():
                    if isinstance(gbody, dict) and isinstance(gbody.get("market"), dict):
                        out.append(_extract_game(str(gid), gbody))
            else:
                _walk(val, out)
    elif isinstance(node, list):
        for item in node:
            _walk(item, out)


def _extract_game(gid: str, gbody: dict) -> dict:
    info = gbody.get("info") or {}
    addl = info.get("additional_data") or {}
    score1 = info.get("score1")
    score2 = info.get("score2")
    markets = []
    for mid, mkt in (gbody.get("market") or {}).items():
        if not isinstance(mkt, dict):
            continue
        events = []
        for eid, ev in (mkt.get("event") or {}).items():
            if isinstance(ev, dict):
                events.append({
                    "event_id": str(eid),
                    "type_1": ev.get("type_1"),
                    "price": _f(ev.get("price")),
                    "base": _f(ev.get("base")),
                    "name": ev.get("name"),
                })
        markets.append({
            "market_id": str(mid),
            "type": mkt.get("type"),
            "name": mkt.get("name"),
            "base": _f(mkt.get("base")),
            "name_template": mkt.get("name_template"),
            "events": events,
        })
    return {
        "game_id": gid,
        "home_name": gbody.get("team1_name"),
        "away_name": gbody.get("team2_name"),
        "home_score": _int(score1),
        "away_score": _int(score2),
        "period_label": _period_label(info.get("current_game_state"),
                                      addl.get("quarter")),
        "clock": info.get("current_game_time"),
        "markets": markets,
    }


def _int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def normalize_observations(payloads: list[dict], captured_at: Optional[str] = None,
                           ) -> list[dict]:
    """Flatten raw game payloads into persistable market observations.

    Only O/U total markets (``_TOTAL_TYPES``) are kept — the game total
    (MatchTotal) plus team totals.  Each becomes one observation row:
      {source_game_id, captured_at, market_type, market_name, line_value,
       over_price, under_price, home_score, away_score, period_label,
       clock, raw}

    ``line_value`` comes from the market's own ``base`` (the bookmaker
    line); Over/Under prices from the matching events.
    """
    ts = captured_at or _utcnow()
    obs: list[dict] = []
    for p in payloads:
        for m in p["markets"]:
            name = _TOTAL_TYPES.get(m["type"])
            if not name:
                continue
            over = under = None
            for ev in m["events"]:
                if ev["type_1"] == "Over":
                    over = ev["price"]
                elif ev["type_1"] == "Under":
                    under = ev["price"]
            obs.append({
                "source_game_id": p["game_id"],
                "captured_at": ts,
                "market_type": m["type"],
                "market_name": m["name"] or name,
                "line_value": m["base"],
                "over_price": over,
                "under_price": under,
                "home_score": p["home_score"],
                "away_score": p["away_score"],
                "period_label": p["period_label"],
                "clock": p["clock"],
                "raw": {
                    "home": p["home_name"],
                    "away": p["away_name"],
                    "markets_count": len(p["markets"]),
                    "market_id": m["market_id"],
                    "events": [
                        {"type_1": e["type_1"], "price": e["price"],
                         "base": e["base"], "name": e["name"]}
                        for e in m["events"]
                    ],
                },
            })
    return obs
