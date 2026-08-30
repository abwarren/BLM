"""
BLM V4 — PokerBet Data Pipeline: Data Models.

Game identity is source + source_game_id (the BetConstruct event ID).
Classification is stored independently on every record.
Snapshots are immutable timestamped observations.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from blm_v4.classifications import Classification

SOURCE_POKERBET = "PokerBet"
SOURCE_BETCONSTRUCT = "BetConstruct"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class GameIdentity(BaseModel):
    """Durable game identity — never derived from team names alone."""

    source: str = SOURCE_POKERBET
    source_game_id: str = Field(..., description="BetConstruct event ID")
    competition_id: Optional[str] = None
    competition_slug: str = ""
    competition: str = ""
    region: str = ""
    game_family: str = ""
    classification: str = Classification.UNKNOWN.value
    sport: str = "basketball"

    def identity_key(self) -> str:
        return f"{self.source}:{self.source_game_id}"


class PokerBetGame(GameIdentity):
    """A game as discovered/recorded from PokerBet."""

    home_team: str = ""
    away_team: str = ""
    game_slug: str = ""
    source_url: str = ""
    status: str = "live"          # pre | live | halftime | ended
    first_seen_at: str = Field(default_factory=utcnow_iso)
    last_seen_at: str = Field(default_factory=utcnow_iso)

    @property
    def display_id(self) -> str:
        """Human-friendly game id: home-vs-away-classification."""
        return f"{self.home_team}-vs-{self.away_team}-{self.classification}"


class MarketObservation(BaseModel):
    """One timestamped observation of one game's market state."""

    source: str = SOURCE_POKERBET
    source_game_id: str
    classification: str
    captured_at: str = Field(default_factory=utcnow_iso)

    # scoreboard
    home_team: str = ""
    away_team: str = ""
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    period_label: str = ""            # "3rd Quarter", "Half End", "1st Quarter"...
    quarter: Optional[int] = None
    clock: Optional[str] = None       # "MM:SS" or "M`"
    game_status: str = "live"

    # markets (list-level: W1/W2 + spread indicator)
    w1_odds: Optional[float] = None
    w2_odds: Optional[float] = None
    spread_indicator: Optional[str] = None   # "+37" from the row

    # markets (event-view level)
    total_line: Optional[float] = None
    total_over_odds: Optional[float] = None
    total_under_odds: Optional[float] = None
    spread: Optional[float] = None           # home spread
    spread_home_odds: Optional[float] = None
    spread_away_odds: Optional[float] = None
    home_total_line: Optional[float] = None
    away_total_line: Optional[float] = None

    # raw reproducibility payloads
    markets_json: str = "{}"
    raw_json: str = "{}"
    source_url: str = ""

    def fingerprint(self) -> str:
        """Stable fingerprint for exact-duplicate suppression."""
        return json.dumps({
            "s": self.home_score, "a": self.away_score,
            "p": self.period_label, "c": self.clock,
            "tl": self.total_line, "sp": self.spread,
            "w1": self.w1_odds, "w2": self.w2_odds,
        }, sort_keys=True)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()
