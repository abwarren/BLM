"""Abstract time series database interface for BLM V2 snapshots."""

from abc import ABC, abstractmethod
from typing import Any, Optional, Protocol, runtime_checkable


# ── Type aliases ──────────────────────────────────────────────────

SnapshotData = dict[str, Any]
"""A snapshot dict. Expected keys typically include:
game_id, timestamp, quarter, clock, home_score, away_score,
total_line, spread, home_projection, away_projection, pace, possessions,
and any BLM-enriched fields.
"""


# ── Interface ─────────────────────────────────────────────────────

class TimeSeriesDB(ABC):
    """Abstract time series database for BLM enriched snapshots.

    All snapshot data flowing through BLM V2 — raw scraped snapshots, enriched
    snapshots, projections — passes through this interface.  Concrete backends
    (InfluxDB, SQLite) implement the actual storage and retrieval mechanics.

    Implementations MUST be thread-safe and SHOULD support concurrent readers.
    """

    @abstractmethod
    async def write_snapshot(self, snapshot: SnapshotData) -> None:
        """Persist a single enriched snapshot.

        Args:
            snapshot: The snapshot dict.  Must contain at minimum a
                ``game_id`` and ``timestamp`` key so the backend can index it.
        """

    @abstractmethod
    async def query_snapshots(
        self,
        game_id: str,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: int = 500,
    ) -> list[SnapshotData]:
        """Return snapshots for a game, oldest-first, within an optional time range.

        Args:
            game_id: The game to retrieve snapshots for.
            from_ts: ISO-8601 timestamp (inclusive) lower bound.
            to_ts: ISO-8601 timestamp (inclusive) upper bound.
            limit: Maximum number of snapshots to return.

        Returns:
            Chronologically ordered list of snapshot dicts.
        """

    @abstractmethod
    async def query_latest(self, game_id: str) -> Optional[SnapshotData]:
        """Return the most recent snapshot for a game, or *None* if no data."""

    @abstractmethod
    async def list_games(self) -> list[str]:
        """Return the set of game IDs that have at least one snapshot stored."""

    @abstractmethod
    async def delete_game(self, game_id: str) -> None:
        """Remove **all** snapshots for a given game.  Idempotent."""

    # ── Line Analysis (OLV/CLV tracking) ──────────────────────────

    @abstractmethod
    async def write_line_analysis(self, analysis: SnapshotData) -> None:
        """Persist a line analysis record from the OLV/CLV tracker."""

    @abstractmethod
    async def query_line_analysis(
        self,
        game_id: str,
        limit: int = 500,
    ) -> list[SnapshotData]:
        """Return line analysis records for a game, oldest-first."""

    @abstractmethod
    async def get_live_line_analysis(self) -> Optional[SnapshotData]:
        """Return the most recent line analysis for any live game."""


# ── Protocol (structural typing) ──────────────────────────────────

@runtime_checkable
class TimeSeriesDBProtocol(Protocol):
    """Structural subtyping protocol for ``TimeSeriesDB``.

    Lets you type-check duck-typed implementations at runtime with
    ``isinstance(obj, TimeSeriesDBProtocol)`` without requiring them to
    inherit from ``TimeSeriesDB``.
    """

    async def write_snapshot(self, snapshot: SnapshotData) -> None: ...
    async def query_snapshots(
        self,
        game_id: str,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: int = 500,
    ) -> list[SnapshotData]: ...
    async def query_latest(self, game_id: str) -> Optional[SnapshotData]: ...
    async def list_games(self) -> list[str]: ...
    async def delete_game(self, game_id: str) -> None: ...

    async def write_line_analysis(self, analysis: SnapshotData) -> None: ...
    async def query_line_analysis(
        self,
        game_id: str,
        limit: int = 500,
    ) -> list[SnapshotData]: ...
    async def get_live_line_analysis(self) -> Optional[SnapshotData]: ...
