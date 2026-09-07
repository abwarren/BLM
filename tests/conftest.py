"""BLM Test Suite — Pytest configuration.

Enables auto mode for asyncio tests so fixtures and tests with ``async def``
are automatically handled without requiring an explicit ``@pytest.mark.asyncio``
on every test.

PREDICTION-GENERATION FREEZE: the scorecard gates classic prediction
writing behind BLM_PREDICTION_FREEZE (default 1 = frozen — the production
datum).  The suite as a whole exercises the prediction machinery as a
legacy / re-authorizable path, so the default UNDER TEST is unfrozen
(env "0") via this file; the dedicated freeze tests
(tests/test_prediction_freeze.py) explicitly re-enable the freeze
(BLM_PREDICTION_FREEZE=1) and prove the frozen behavior.
"""

import os

import pytest

# Default for the whole suite (see module docstring).  The scorecard
# reads this env at call time, so setting it here (before tests run)
# reliably unfreezes prediction generation for machinery tests.
os.environ.setdefault("BLM_PREDICTION_FREEZE", "0")


def pytest_configure(config):
    """Register the asyncio mode marker."""
    config.addinivalue_line(
        "markers",
        "asyncio: mark test as async (auto-detected in auto mode)",
    )
