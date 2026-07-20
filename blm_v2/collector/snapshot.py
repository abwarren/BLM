"""Raw snapshot model — the scraped game state BEFORE BLM enrichment.

This is the "source of truth" data straight from the scraping layer.
The BLM engine enriches a ``RawSnapshot`` into a ``BlmSnapshot``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field


class RawSnapshot(BaseModel):
    """A single raw data point scraped from the betting site.

    This model represents the unprocessed game state as observed at a
    single moment.  No BLM analysis, projections, or derived metrics are
    attached — those are added later by the enrichment engine.
    """

    game_id: str = Field(..., description="Unique game identifier (e.g. 'Sharks-vs-Eagles-2025-01-15')")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        description="ISO-8601 UTC timestamp of the scrape",
    )

    # ── Game identity ─────────────────────────────────────────────
    home_team: str = Field(default="", description="Home team name")
    away_team: str = Field(default="", description="Away team name")
    league: str = Field(default="Cyber 2K26", description="League / competition")
    season: Optional[str] = Field(default=None, description="Season identifier")
    status: str = Field(default="live", description="Game status: pre/live/halftime/ended")

    # ── Scoreboard ────────────────────────────────────────────────
    home_score: int = Field(default=0, ge=0, description="Home team score")
    away_score: int = Field(default=0, ge=0, description="Away team score")
    quarter: int = Field(default=1, ge=1, le=4, description="Current quarter")
    clock: Optional[str] = Field(default=None, description="Game clock string (e.g. '2:30')")

    # ── Market data ───────────────────────────────────────────────
    total_line: Optional[float] = Field(default=None, description="Over/under total line")
    spread: Optional[float] = Field(default=None, description="Point spread (home team perspective)")
    total_odds: Optional[str] = Field(default=None, description="Total odds string (e.g. '-110')")
    spread_odds: Optional[str] = Field(default=None, description="Spread odds string")
    moneyline_home: Optional[str] = Field(default=None, description="Home moneyline odds")
    moneyline_away: Optional[str] = Field(default=None, description="Away moneyline odds")

    # ── Raw DOM extras ────────────────────────────────────────────
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Any additional scraped fields not captured in the schema",
    )

    def to_ts_dict(self) -> dict[str, Any]:
        """Convert to a plain dict suitable for time-series storage.

        This is the dict shape that ``TimeSeriesDB.write_snapshot`` expects.
        """
        d = self.model_dump(exclude={"extra"})
        d.update(self.extra)
        return d

    @classmethod
    def from_v1_dict(cls, data: dict[str, Any]) -> "RawSnapshot":
        """Construct from a V1 collector snapshot dict.

        V1 dicts use snake_case keys that match the field names above,
        so this is primarily a validation + default-filling helper.
        """
        return cls(**{k: v for k, v in data.items() if k in cls.model_fields})
