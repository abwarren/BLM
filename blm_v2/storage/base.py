"""Abstract storage interface for BLM V2 CRUD operations.

Handles all non-time-series persistent data: game metadata, configuration,
alerts, and other structured entities.  Concrete backends (SQLite, InfluxDB)
provide the actual persistence mechanics.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional


class StorageDB(ABC):
    """CRUD abstraction for BLM V2 structured (non-time-series) entities.

    Methods cover game metadata and alerts.  Config persistence is handled
    separately by ``pydantic-settings``.
    """

    # ── Games ─────────────────────────────────────────────────────

    @abstractmethod
    async def save_game(self, game: dict[str, Any]) -> None:
        """Create or update a game record.

        The dict must contain at minimum a ``game_id`` key.  Typical keys:
        ``game_id``, ``league``, ``season``, ``home_team``, ``away_team``,
        ``status`` (pre/live/halftime/ended).
        """

    @abstractmethod
    async def get_game(self, game_id: str) -> Optional[dict[str, Any]]:
        """Return a single game record by ID, or *None*.

        Returns the full game metadata dict as stored.
        """

    @abstractmethod
    async def list_games(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent game records, newest first.

        Each dict contains the full game metadata plus a ``snapshot_count``
        and ``last_snapshot_ts`` when the backend supports joins.
        """

    # ── Alerts ────────────────────────────────────────────────────

    @abstractmethod
    async def save_alert(self, alert: dict[str, Any]) -> None:
        """Persist an alert/notification record.

        Must contain at minimum a ``game_id``, ``type``, ``message``, and
        ``timestamp`` key.
        """

    @abstractmethod
    async def get_alerts(
        self,
        game_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return alerts for a game, newest first."""
