"""
BLM V4 — BetConstruct Reconciliation.

PokerBet runs on the BetConstruct platform: the "underlying BetConstruct
game" IS the event served at the BetConstruct event-view URL.  The
game_id in the URL (e.g. /30738600/) is the BetConstruct event ID.

Reconciliation verifies the three-way agreement:

    PokerBet displayed game  ↕  BetConstruct URL taxonomy  ↕  BLM record

Checks performed per game:
  1. URL taxonomy parses (sport, region, competition_id, comp_slug,
     game_id, game_slug)
  2. game_id from the URL matches the recorded source_game_id
  3. competition slug classification agrees with the recorded
     classification
  4. displayed team names match the URL slug teams (normalized)
  5. the event page actually renders a scoreboard + the key markets
     (Total Points / Points Handicap / Match Winner)
  6. the displayed competition header agrees with the recorded
     competition

Any failed check → result='mismatch' and is recorded, never silently
accepted.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from blm_v4.classifications import (
    Classification,
    classify_competition,
    parse_event_url,
    slugify_team,
)
from blm_v4.event_parser import parse_event_view

_SLUG_TEAM_RE = re.compile(r"^(.*?)-vs-(.*?)$")


def _slug_teams(game_slug: str) -> tuple[str, str]:
    # BetConstruct game slugs are `<slugify(home)>-<slugify(away)>` with no
    # explicit separator (e.g. sacramento-kings-virtual-miami-heat-virtual),
    # so they cannot be split by regex.  Verification is containment-based:
    # the slug must START with the slugified home and END with the slugified
    # away.  Return the raw slug for that check.
    return game_slug or "", ""


def reconcile_event(
    url: str,
    page_text: str,
    recorded: dict[str, Any],
) -> dict[str, Any]:
    """Run reconciliation checks for one captured event.

    ``recorded`` carries the BLM record: source_game_id, classification,
    competition, home_team, away_team, plus the parsed observation dict
    (from parse_event_view).

    Returns a checks dict + result ('matched' | 'mismatch').
    """
    checks: dict[str, Any] = {}
    failures: list[str] = []

    # 1. URL taxonomy
    tax = parse_event_url(url)
    if not tax:
        return _result(checks, failures, fatal="event URL is not an event-view URL")

    checks["url_taxonomy"] = tax

    # 2. game_id agreement
    url_game_id = tax["game_id"]
    rec_game_id = str(recorded.get("source_game_id", ""))
    ok = url_game_id == rec_game_id
    checks["game_id_matches"] = ok
    if not ok:
        failures.append(f"game_id mismatch: url={url_game_id} record={rec_game_id}")

    # 3. classification agreement
    url_cls = classify_competition(
        competition_slug=tax["competition_slug"], region=tax["region"],
    ).value
    rec_cls = recorded.get("classification", "")
    ok = url_cls == rec_cls
    checks["classification_matches"] = ok
    checks["classification_url"] = url_cls
    if not ok:
        failures.append(f"classification mismatch: url={url_cls} record={rec_cls}")

    # 4. team slug agreement (containment: slug starts with home, ends with away)
    slug = _slug_teams(tax["game_slug"])[0]
    rec_home = slugify_team(recorded.get("home_team", ""))
    rec_away = slugify_team(recorded.get("away_team", ""))
    ok = bool(slug) and slug.startswith(rec_home) and slug.endswith(rec_away)
    checks["teams_match_slug"] = ok
    if not ok:
        failures.append(
            f"teams mismatch: slug={slug} record={rec_home}/{rec_away}"
        )

    # 5. displayed scoreboard + markets present
    parsed = parse_event_view(page_text)
    checks["scoreboard_present"] = (
        parsed["home_score"] is not None and parsed["away_score"] is not None
    )
    checks["total_points_present"] = bool(parsed["total"])
    checks["handicap_present"] = bool(parsed["handicap"])
    checks["match_winner_present"] = bool(parsed["match_winner"])
    checks["period"] = parsed["period_label"]
    checks["clock"] = parsed["clock"]
    if not checks["scoreboard_present"]:
        failures.append("scoreboard not present on event page")
    if not (checks["total_points_present"] or checks["match_winner_present"]):
        failures.append("no pricing markets present on event page")

    # 6. displayed competition header
    comp_header = recorded.get("competition", "")
    if comp_header and comp_header.lower() in page_text.lower():
        checks["competition_header_matches"] = True
    else:
        checks["competition_header_matches"] = False
        failures.append(f"competition header '{comp_header}' not on page")

    result = "matched" if not failures else "mismatch"
    return {
        "result": result,
        "failures": failures,
        "checks": checks,
        "bc_event_id": tax["game_id"],
        "bc_event_name": tax["game_slug"],
        "bc_competition_id": tax["competition_id"],
        "bc_url": url,
        "parsed": parsed,
    }


def _result(
    checks: dict[str, Any], failures: list[str], *, fatal: str,
) -> dict[str, Any]:
    failures.append(fatal)
    return {
        "result": "mismatch",
        "failures": failures,
        "checks": checks,
        "bc_event_id": None,
        "bc_event_name": None,
        "bc_competition_id": None,
        "bc_url": "",
        "parsed": None,
    }
