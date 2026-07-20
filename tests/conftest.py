"""BLM Test Suite — Pytest configuration.

Enables auto mode for asyncio tests so fixtures and tests with ``async def``
are automatically handled without requiring an explicit ``@pytest.mark.asyncio``
on every test.
"""

import pytest


def pytest_configure(config):
    """Register the asyncio mode marker."""
    config.addinivalue_line(
        "markers",
        "asyncio: mark test as async (auto-detected in auto mode)",
    )
