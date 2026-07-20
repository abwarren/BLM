"""Abstract collector interface for BLM V2 data ingestion.

Defines the contract that any data-source collector (PokerBet scraper,
simulator, WebSocket feed, etc.) must satisfy for use with the BLM V2
enrichment pipeline.
"""

from abc import ABC, abstractmethod
from typing import Optional

from blm_v2.collector.snapshot import RawSnapshot


class Collector(ABC):
    """Interface for a live data source that produces snapshots.

    Implementations wrap a specific source (e.g. Playwright scraping a
    betting site, consuming a WebSocket feed, replaying historical data)
    and expose the latest observed state through a uniform interface.
    """

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """*True* while the collector loop is active."""

    @property
    @abstractmethod
    def latest_snapshot(self) -> Optional[RawSnapshot]:
        """The most recently scraped snapshot, or *None* if none yet."""

    @abstractmethod
    def start(self) -> None:
        """Begin collecting data (may block or spawn a background thread)."""

    @abstractmethod
    def stop(self) -> None:
        """Gracefully stop the collector and release resources."""
