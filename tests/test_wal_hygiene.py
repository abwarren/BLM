"""WAL-hygiene tests (incident 2026-09-29, open item 1).

EVERY test runs against a throwaway ``tmp_path`` database — the live
production ``blm_pokerbet.db`` is never opened, connected to or touched.
What is under test:

  * the stat gate (small/absent WAL → no pass, no connection),
  * a real PASSIVE checkpoint copying frames out of a grown WAL,
  * the readers-hold-frames path (PASSIVE skips, never blocks),
  * fail-closed behaviour on an unreadable "database",
  * the worker loop actually firing on cadence (wait-loop, not sleep),
  * the env-knob helpers (parse + garbage fallback).
"""
from __future__ import annotations

import logging
import sqlite3
import time

import pytest

from blm_v4 import wal_hygiene
from blm_v4.wal_hygiene import (
    WalHygieneWorker,
    env_interval_s,
    env_threshold_mb,
    passive_checkpoint_once,
    should_checkpoint,
    wal_bytes,
)

# WAL big enough to clear a 1 KB threshold after the seed writes below.
TINY_THRESHOLD_MB = 0.001
SEED_ROWS = 500


def _wal_db(tmp_path, rows: int = SEED_ROWS) -> "sqlite3.Connection":
    """A throwaway WAL-mode DB with ``rows`` rows committed."""
    db = tmp_path / "hygiene-test.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, blob TEXT)")
    conn.executemany(
        "INSERT INTO t (blob) VALUES (?)",
        (("x" * 400,) for _ in range(rows)))
    conn.commit()
    return conn


# ── stat gate ────────────────────────────────────────────────────────

def test_absent_wal_reads_zero_and_never_checks(tmp_path):
    db = tmp_path / "no-such.db"
    assert wal_bytes(db) == 0
    assert should_checkpoint(db) is False
    res = passive_checkpoint_once(db, threshold_mb=TINY_THRESHOLD_MB)
    assert res["checked"] is False
    assert res["checkpointed"] is False


def test_small_wal_is_gated_out(tmp_path):
    conn = _wal_db(tmp_path, rows=1)   # a WAL far below even 1 KB
    try:
        db = conn.execute("PRAGMA database_list").fetchone()[2]
        res = passive_checkpoint_once(db, threshold_mb=256.0)
        assert res["checked"] is False
        assert res["wal_bytes"] == wal_bytes(db)
    finally:
        conn.close()


# ── a real checkpoint ────────────────────────────────────────────────

def test_grown_wal_is_checkpointed(tmp_path):
    conn = _wal_db(tmp_path)           # 500 × 400 B ≈ 200 KB of frames
    try:
        db = conn.execute("PRAGMA database_list").fetchone()[2]
        assert wal_bytes(db) > 0
        assert should_checkpoint(db, TINY_THRESHOLD_MB) is True
        res = passive_checkpoint_once(db, threshold_mb=TINY_THRESHOLD_MB)
        assert res["checked"] is True
        assert res["checkpointed"] is True
        assert res["checkpointed_frames"] > 0
        # nothing held the WAL back: every frame copied out, none busy
        assert res["busy"] == 0
        assert res["log"] == res["checkpointed_frames"]
    finally:
        conn.close()


# ── readers hold frames back — PASSIVE skips, never blocks ──────────

def test_open_reader_holds_frames_and_pass_skips(tmp_path):
    writer = _wal_db(tmp_path)
    db = writer.execute("PRAGMA database_list").fetchone()[2]
    reader = sqlite3.connect(str(db), isolation_level=None)
    try:
        # a held-open read transaction pins the WAL snapshot: frames
        # written after it began cannot be copied by ANY checkpoint —
        # PASSIVE must skip them without waiting (a RESTART/FULL mode
        # would block here; that is exactly what the worker must never
        # do to the production pipeline).
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM t").fetchone()
        writer.executemany(
            "INSERT INTO t (blob) VALUES (?)",
            (("y" * 400,) for _ in range(50)))
        writer.commit()
        res = passive_checkpoint_once(db, threshold_mb=TINY_THRESHOLD_MB)
        assert res["checked"] is True
        assert res["busy"] == 0           # PASSIVE reported, never waited
        # the pass copies the SAFE PREFIX (every frame below the
        # reader's snapshot) and skips only the pinned tail — measured
        # empirically: (busy=0, log=N, checkpointed=log - pinned tail).
        assert res["checkpointed_frames"] >= 1
        assert res["held_back"] == res["log"] - res["checkpointed_frames"]
        assert res["held_back"] >= 1      # the reader pinned something
    finally:
        if reader.in_transaction:
            reader.execute("ROLLBACK")
        reader.close()
        writer.close()


# ── fail-closed ──────────────────────────────────────────────────────

def test_unreadable_db_fails_closed_without_raising(tmp_path):
    db = tmp_path / "corrupt.db"
    db.write_bytes(b"this is not a sqlite database" * 100)
    # companion WAL over the threshold so the pass is attempted at all
    (tmp_path / "corrupt.db-wal").write_bytes(b"z" * 4096)
    res = passive_checkpoint_once(db, threshold_mb=TINY_THRESHOLD_MB)
    assert res["checked"] is True
    assert res["checkpointed"] is False


# ── the worker loop ──────────────────────────────────────────────────

def test_worker_loop_fires_on_cadence(tmp_path):
    conn = _wal_db(tmp_path)
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    log = logging.getLogger("blm_v4.wal_hygiene")
    handler = _Capture()
    log.addHandler(handler)
    # the module logs the successful pass at INFO; pytest defaults the
    # effective level to WARNING, so the test must lower it itself
    log.setLevel(logging.DEBUG)
    worker = WalHygieneWorker(db, interval_s=0.05,
                              threshold_mb=TINY_THRESHOLD_MB)
    try:
        worker.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not records:
            time.sleep(0.02)           # wait-loop, never a bare sleep
        assert records, "worker never fired its checkpoint within 5 s"
        assert any("wal_hygiene_checkpoint" in r.getMessage()
                   for r in records)
    finally:
        worker.stop(timeout=2.0)
        log.removeHandler(handler)
        log.setLevel(logging.NOTSET)
        conn.close()
        assert not worker._thread.is_alive()


# ── env knobs ────────────────────────────────────────────────────────

def test_env_knobs_parse_and_fall_back():
    assert env_interval_s({}) == wal_hygiene.DEFAULT_INTERVAL_S
    assert env_interval_s({"BLM_WAL_HYGIENE_INTERVAL_S": "0"}) == 0.0
    assert env_interval_s({"BLM_WAL_HYGIENE_INTERVAL_S": "garbage"}) \
        == wal_hygiene.DEFAULT_INTERVAL_S
    assert env_threshold_mb({}) == wal_hygiene.DEFAULT_THRESHOLD_MB
    assert env_threshold_mb({"BLM_WAL_HYGIENE_THRESHOLD_MB": "16"}) == 16.0
    assert env_threshold_mb({"BLM_WAL_HYGIENE_THRESHOLD_MB": "garbage"}) \
        == wal_hygiene.DEFAULT_THRESHOLD_MB


def test_threshold_mb_zero_always_checks(tmp_path):
    """threshold 0 = checkpoint every tick (operator's explicit choice)."""
    conn = _wal_db(tmp_path, rows=2)
    try:
        db = conn.execute("PRAGMA database_list").fetchone()[2]
        res = passive_checkpoint_once(db, threshold_mb=0.0)
        assert res["checked"] is True
        assert res["checkpointed"] is True
    finally:
        conn.close()


@pytest.mark.parametrize("size,threshold,expected", [
    (0, 256.0, False),
    (256 * 1024 * 1024, 256.0, True),
    (256 * 1024 * 1024 - 1, 256.0, False),
    (4096, 0.001, True),
])
def test_should_checkpoint_boundaries(size, threshold, expected, tmp_path):
    wal = tmp_path / "bound.db-wal"
    wal.write_bytes(b"0" * size)
    assert should_checkpoint(tmp_path / "bound.db", threshold) is expected
