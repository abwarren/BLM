"""
BLM V4 — PokerBet Data Pipeline: Classifications & Taxonomy.

Classification is the heart of the data-separation requirement:
  - CYBER_2K26  (PokerBet: World / Cyber Basketball. 2K26 Matches)
  - BETUAL_NBA  (PokerBet: Virtual Matches / Betual NBA)

These are independent statistical populations.  Nothing in BLM may mix
their historical data, distributions, or derived statistics.

Classification is derived from the actual taxonomy exposed by
PokerBet/BetConstruct (URL slugs, region, competition display name) —
not purely from the display name.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional


class GameFamily(str, Enum):
    """Statistical family of a basketball game."""

    CYBER = "cyber"          # Cyber Basketball 2K26 (simulated, BetConstruct virtual)
    BETUAL = "betual"        # Betual NBA (simulated, BetConstruct virtual)
    CONVENTIONAL = "conventional"  # real-world basketball (NBA, FIBA, ...)
    UNKNOWN = "unknown"


class Classification(str, Enum):
    """BLM classification codes — the statistical population key."""

    CYBER_2K26 = "CYBER_2K26"
    BETUAL_NBA = "BETUAL_NBA"
    CONVENTIONAL = "CONVENTIONAL"
    UNKNOWN = "UNKNOWN"

    @property
    def game_family(self) -> GameFamily:
        return _CLASSIFICATION_FAMILY[self]


_CLASSIFICATION_FAMILY = {
    Classification.CYBER_2K26: GameFamily.CYBER,
    Classification.BETUAL_NBA: GameFamily.BETUAL,
    Classification.CONVENTIONAL: GameFamily.CONVENTIONAL,
    Classification.UNKNOWN: GameFamily.UNKNOWN,
}

# Canonical competition display names as exposed by PokerBet
CYBER_COMPETITION = "Cyber Basketball 2K26"
BETUAL_COMPETITION = "Betual NBA"

# ── Regex signals ───────────────────────────────────────────────────

_RE_CYBER = re.compile(r"cyber|2k26|2k25|2k24", re.I)
_RE_BETUAL = re.compile(r"betual", re.I)
_RE_VIRTUAL_MATCHES = re.compile(r"virtual", re.I)

# BetConstruct event-view URL taxonomy:
# /en/sports/live/event-view/{sport}/{region}/{comp_id}/{comp_slug}/{game_id}/{slug}
_EVENT_URL_RE = re.compile(
    r"/event-view/(?P<sport>[^/]+)/(?P<region>[^/]+)/(?P<competition_id>\d+)/"
    r"(?P<competition_slug>[^/]+)/(?P<game_id>\d+)/(?P<game_slug>[^/?#]+)"
)


class CompetitionInfo:
    """Identified competition from the PokerBet/BetConstruct taxonomy."""

    __slots__ = (
        "region", "competition_id", "competition_slug", "display_name",
        "classification", "game_family",
    )

    def __init__(
        self,
        region: str = "",
        competition_id: Optional[str] = None,
        competition_slug: str = "",
        display_name: str = "",
        classification: Classification = Classification.UNKNOWN,
    ):
        self.region = region
        self.competition_id = competition_id
        self.competition_slug = competition_slug
        self.display_name = display_name
        self.classification = classification
        self.game_family = classification.game_family

    def to_dict(self) -> dict:
        return {
            "region": self.region,
            "competition_id": self.competition_id,
            "competition_slug": self.competition_slug,
            "display_name": self.display_name,
            "classification": self.classification.value,
            "game_family": self.game_family.value,
        }


def classify_competition(
    display_name: str = "",
    competition_slug: str = "",
    region: str = "",
    sport: Optional[str] = None,
) -> Classification:
    """Classify a competition from the PokerBet/BetConstruct taxonomy.

    Priority order (strongest signal first):
      1. URL competition slug  (betual-nba, cyber-basketball-2k26-matches)
      2. Display name          (Cyber Basketball. 2K26 Matches, Betual NBA)
      3. Region                (Virtual Matches → betual family)

    Sport guard (2026-09-12): CYBER_2K26 / BETUAL_NBA are BASKETBALL
    statistical populations.  When the panel sport is known and is not
    (E-)Basketball, a name/slug that merely contains "betual"/"cyber"
    (Betual virtual FOOTBALL/VOLLEYBALL leagues, Cyber TENNIS) must not
    classify as a basketball population — observed live 2026-09-12:
    "Betual England Premier League" (Football) and "Betual Sultanlar
    Ligi" (Volleyball) classified BETUAL_NBA, "Cyber Tennis AO2 Matches
    - Women" classified CYBER_2K26.  ``sport=None`` keeps the legacy
    name/slug behaviour (callers without sport context, e.g. event-URL
    reconciliation, where the URL's own taxonomy is authoritative).
    """
    slug = (competition_slug or "").lower()
    name = (display_name or "").lower()
    region_l = (region or "").lower()

    is_basketball = (
        sport is None
        or (sport or "").strip().lower()
        in ("basketball", "e-basketball", "ebasketball")
    )
    if not is_basketball:
        return Classification.UNKNOWN

    # 1. URL slug is authoritative when present
    if slug and "betual" in slug:
        return Classification.BETUAL_NBA
    if slug and ("cyber-basketball" in slug or "cyber-basketball-2k26" in slug):
        return Classification.CYBER_2K26

    # 2. Display name
    if name and _RE_BETUAL.search(name):
        return Classification.BETUAL_NBA
    if name and _RE_CYBER.search(name):
        return Classification.CYBER_2K26

    # 3. Region fallback (Betual lives under "Virtual Matches")
    if region_l and "virtual" in region_l:
        # Only betual NBA is a basketball competition under Virtual Matches
        if "betual" in name or "nba" in name:
            return Classification.BETUAL_NBA

    return Classification.UNKNOWN


def classify_event_url(url: str) -> Classification:
    """Classify a game purely from its event-view URL taxonomy.

    The URL carries its own sport segment; a non-basketball URL sport
    vetoes a basketball classification even when the competition slug
    contains "betual" (virtual football/volleyball event URLs).
    """
    m = _EVENT_URL_RE.search(url or "")
    if not m:
        return Classification.UNKNOWN
    return classify_competition(
        competition_slug=m.group("competition_slug"),
        region=m.group("region"),
        sport=m.group("sport"),
    )


def parse_event_url(url: str) -> Optional[dict]:
    """Parse a BetConstruct event-view URL into its taxonomy components.

    Returns dict with keys: sport, region, competition_id, competition_slug,
    game_id, game_slug — or None if the URL is not an event-view URL.
    """
    m = _EVENT_URL_RE.search(url or "")
    if not m:
        return None
    return {
        "sport": m.group("sport"),
        "region": m.group("region"),
        "competition_id": m.group("competition_id"),
        "competition_slug": m.group("competition_slug"),
        "game_id": m.group("game_id"),
        "game_slug": m.group("game_slug"),
    }


def canonical_competition_name(classification: Classification) -> str:
    """Canonical competition name used for the games table."""
    if classification == Classification.CYBER_2K26:
        return CYBER_COMPETITION
    if classification == Classification.BETUAL_NBA:
        return BETUAL_COMPETITION
    return classification.value.title()


def slugify_team(name: str) -> str:
    """BetConstruct game-slug style: lowercase, spaces → '-', strip non-alnum."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower())
    return s.strip("-")


# Betual presents its virtual teams with a literal "Virtual" marker.
# Observed live 2026-09-12 (live panel + event view + eu-swarm feed):
# the marker is a SUFFIX in the current source ("Los Angeles Lakers
# Virtual", "Petkim Spor KB Virtual"); the brief also requires handling
# a PREFIX rendering ("Virtual Lakers") should the source switch.  The
# canonical BLM team name strips this presentation marker only — no
# other rewriting.  Case-aware (marker matched case-insensitively,
# exact source case of the team part preserved), deterministic,
# idempotent, and a no-op for non-Betual names (Cyber names like
# "Oklahoma City Thunder Cyber" never carry the marker).
_RE_VIRTUAL_PREFIX = re.compile(r"^virtual\s+", re.I)
_RE_VIRTUAL_SUFFIX = re.compile(r"\s+virtual$", re.I)


def normalize_betual_team(name: Optional[str]) -> str:
    """Strip Betual's "Virtual" presentation marker from a team name.

    "Los Angeles Lakers Virtual" → "Los Angeles Lakers"
    "Virtual Lakers"            → "Lakers"
    "  Lakers   Virtual "       → "Lakers"   (whitespace variations)
    "Lakers" / None / ""        → unchanged (no arbitrary rewriting)

    Applied ONLY to Betual-origin names by the callers (the pure
    function itself never sees non-Betual team names from them); a name
    without the marker is returned trimmed-and-unchanged, so the
    transform is idempotent: normalize(normalize(x)) == normalize(x).
    """
    s = (name or "").strip()
    if not s:
        return ""
    # strip marker (case-insensitive) — repeatedly, so an accidental
    # double marker still normalizes; then collapse outer whitespace.
    prev = None
    while prev != s:
        prev = s
        s = _RE_VIRTUAL_PREFIX.sub("", s)
        s = _RE_VIRTUAL_SUFFIX.sub("", s)
        s = s.strip()
    return s
