"""An exception logged through the real pipeline must render its traceback.

INCIDENT (2026-09-20): the 17:46 scorecard pass died after 4m08s and the
journal recorded only

    {"exc_info": true, "event": "scorecard_run_failed", ...}

— no exception type, no message, no frame.  ``server.py`` does
``except Exception: logger.exception("scorecard_run_failed")``, which is
correct; what failed is the PIPELINE.  ``structlog.dev.set_exc_info`` sets
``exc_info`` to a BOOLEAN flag expecting a later processor to replace it with
the real traceback, and no such processor was configured — so the flag reached
the JSON renderer verbatim and every exception in BLM was undiagnosable.  The
crash destroyed its own evidence by construction.

These tests drive the PUBLIC interface (``setup_logging`` + ``get_logger``),
never the processor list, and assert on the PARSED log line rather than a
substring of serialized JSON (which would have to match escaped quotes), so
they hold for any pipeline restructuring that preserves the behaviour.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import sys

import pytest
import structlog

from blm_v2.telemetry.logging import get_logger, setup_logging

EVENT = "scorecard_run_failed"


@contextlib.contextmanager
def _pipeline_into_a_buffer(environment: str):
    """Run the REAL pipeline for *environment*, capturing what it emits.

    ``logging.StreamHandler`` binds its stream at CONSTRUCTION time, so the
    buffer is installed as ``sys.stdout`` *before* ``setup_logging()`` runs —
    the handler then writes somewhere we can read regardless of how pytest's
    capture swaps streams between phases.  (Reading via ``capsys`` instead
    returns an empty string: pytest replaces ``sys.stdout`` per phase, so the
    stream the handler captured is not the one ``capsys`` reports.)

    ``setup_logging`` mutates the root logger and structlog's global config;
    both are restored so this cannot leak into the rest of the suite.
    """
    buffer = io.StringIO()
    saved_stdout = sys.stdout
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    sys.stdout = buffer
    try:
        setup_logging(environment=environment, log_level="INFO")
        yield buffer
    finally:
        sys.stdout = saved_stdout
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        structlog.reset_defaults()


@pytest.fixture
def logged():
    """The production pipeline, as the systemd unit configures it."""
    with _pipeline_into_a_buffer("production") as buffer:
        yield buffer


def _emit_one_exception(exc: Exception) -> None:
    """Log one exception exactly as server.py's scorecard loop does."""
    try:
        raise exc
    except type(exc):
        get_logger("server").exception(EVENT)


def _emitted_event(buffer: io.StringIO) -> dict:
    """The parsed JSON line for the logged exception."""
    for line in buffer.getvalue().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        payload = json.loads(line)
        if payload.get("event") == EVENT:
            return payload
    raise AssertionError(f"no {EVENT!r} line was emitted: {buffer.getvalue()!r}")


def test_exception_renders_its_traceback(logged):
    """The traceback itself must reach the log stream."""
    _emit_one_exception(ValueError("SYNTHETIC-BOOM"))

    exception = _emitted_event(logged)["exception"]
    assert "Traceback (most recent call last)" in exception, exception
    assert "SYNTHETIC-BOOM" in exception, exception


def test_exception_renders_the_exception_type(logged):
    """The exception class must be named — the first thing a diagnosis needs."""
    _emit_one_exception(KeyError("missing-market-row"))

    exception = _emitted_event(logged)["exception"]
    assert "KeyError" in exception, exception
    assert "missing-market-row" in exception, exception


def test_exception_renders_a_frame_reference(logged):
    """A frame reference is what makes the failure locatable at all."""
    _emit_one_exception(RuntimeError("frame-marker"))

    exception = _emitted_event(logged)["exception"]
    assert 'File "' in exception, exception
    assert "test_logging_traceback.py" in exception, exception


def test_unrendered_exc_info_flag_is_the_regression_marker(logged):
    """A naked ``exc_info`` flag reaching the renderer IS the defect signature.

    ``format_exc_info`` consumes the flag and emits the traceback in its
    place, so the flag must not survive into the emitted payload.  Asserting
    its absence is what turns this file into a permanent regression test.
    """
    _emit_one_exception(RuntimeError("boom"))

    payload = _emitted_event(logged)
    assert "exc_info" not in payload, (
        f"exc_info reached the renderer unrendered — traceback dropped: {payload}"
    )


def test_the_development_pipeline_renders_tracebacks_too():
    """The dev pipeline shares the processor list, so it must not regress.

    ``_shared_processors`` feeds the structlog config, ``ProcessorFormatter``
    and ``foreign_pre_chain`` for BOTH environments.  Fixing only the
    production branch would leave local runs — and every test that configures
    dev logging — still dropping tracebacks, so both are pinned here.
    """
    with _pipeline_into_a_buffer("development") as buffer:
        _emit_one_exception(RuntimeError("DEV-BOOM"))
        out = buffer.getvalue()

    assert "Traceback (most recent call last)" in out, out
    assert "DEV-BOOM" in out, out
    assert "exc_info" not in out, out
