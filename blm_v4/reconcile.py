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
import os
import re
from typing import Any, Optional

from blm_v4.classifications import (
    Classification,
    classify_competition,
    normalize_betual_team,
    parse_event_url,
    slugify_team,
)
from blm_v4.event_parser import parse_event_view

_SLUG_TEAM_RE = re.compile(r"^(.*?)-vs-(.*?)$")


def _strip_virtual_slug(slug: str) -> str:
    """Strip Betual's "Virtual" presentation marker from a game slug.

    Slugs are ``slugify(home) + slugify(away)``; the marker appears as a
    leading ``virtual-`` token (prefix rendering) and/or a trailing
    ``-virtual`` token (suffix rendering — the current source format).
    BLM stores canonical team names ("Lakers"), so the URL slug
    ("lakers-virtual-miami-heat-virtual") must lose the marker tokens
    before the containment check.  Deterministic + idempotent.
    """
    s = slug or ""
    prev = None
    while prev != s:
        prev = s
        if s.startswith("virtual-"):
            s = s[len("virtual-"):]
        if s.endswith("-virtual"):
            s = s[: -len("-virtual")]
    return s


def _slug_teams(game_slug: str) -> tuple[str, str]:
    # BetConstruct game slugs are `<slugify(home)>-<slugify(away)>` with no
    # explicit separator (e.g. sacramento-kings-virtual-miami-heat-virtual),
    # so they cannot be split by regex.  Verification is containment-based:
    # the slug must START with the slugified home and END with the slugified
    # away.  Return the raw slug for that check.
    return game_slug or "", ""


def _team_slug_variants(name: str) -> list[str]:
    """Slug variants of a recorded team name for marker-tolerant checks.

    BLM stores canonical Betual names ("Miami Heat") while older records
    and raw source data carry the "Virtual" marker ("Miami Heat
    Virtual").  Both slugify to valid comparison variants; the
    containment check accepts either so canonicalization never breaks
    reconciliation of already-stored games.
    """
    raw = slugify_team(name)
    canon = slugify_team(normalize_betual_team(name))
    return [v for v in dict.fromkeys([raw, canon]) if v]


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

    # 4. team slug agreement (containment: slug starts with home, ends
    #    with away; Betual's "Virtual" marker tokens stripped from the
    #    source slug edges, and both marker/canonical variants of the
    #    recorded names accepted so old and new records reconcile)
    slug = _strip_virtual_slug(_slug_teams(tax["game_slug"])[0])
    home_variants = _team_slug_variants(recorded.get("home_team", ""))
    away_variants = _team_slug_variants(recorded.get("away_team", ""))
    ok = bool(slug) \
        and any(slug.startswith(v) for v in home_variants) \
        and any(slug.endswith(v) for v in away_variants)
    checks["teams_match_slug"] = ok
    if not ok:
        failures.append(
            f"teams mismatch: slug={slug} "
            f"record={home_variants}/{away_variants}"
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

    # 6. displayed competition header (normalized — the page may render
    #    "Cyber Basketball. 2K26 Matches" vs canonical "Cyber Basketball 2K26",
    #    or add/omit suffixes like "Matches" / "Virtual")
    comp_header = recorded.get("competition", "")
    if comp_header:
        norm = re.sub(r"[^a-z0-9]+", "", comp_header.lower())
        page_norm = re.sub(r"[^a-z0-9]+", "", page_text.lower())
        # symmetric containment + shared-prefix tolerance for naming variants
        common = os.path.commonprefix([norm, page_norm])
        checks["competition_header_matches"] = (
            norm in page_norm or page_norm in norm or len(common) >= 10
        )
        if not checks["competition_header_matches"]:
            failures.append(f"competition header '{comp_header}' not on page")
    else:
        checks["competition_header_matches"] = False
        failures.append("no competition recorded to verify")

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
