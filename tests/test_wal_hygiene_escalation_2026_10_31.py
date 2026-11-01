"""WAL hygiene ESCALATION: above the hard cap, TRUNCATE — not PASSIVE.

Incident 2026-10-31: `blm_pokerbet.db`'s WAL reached 14.25 GB with the
hygiene worker running every 5 minutes.  Each PASSIVE pass copied 2.3-2.5M
frames back into the DB and held back only 19-285k — yet the file only grew,
because PASSIVE never truncates: with a writer appending continuously and
even a small reader-pinned tail, the WAL never resets.  The collector's
persistence stage then took 100-230 s, the self-watchdog killed the unit
twice, and the restarts fragmented live fixtures into instance ids that
never settled.

One named test per requirement.
"""
from __future__ import annotations

import sqlite3

from blm_v4.wal_hygiene import (
    DEFAULT_HARD_CAP_MB,
    env_hard_cap_mb,
    passive_checkpoint_once,
)


def _make_db(path, rows=2000):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)",
                     [(f"row-{i}-" + "x" * 200,) for i in range(rows)])
    conn.commit()
    return conn


def test_a_small_wal_reports_the_passive_mode(tmp_path):
    db = tmp_path / "small.db"
    conn = _make_db(db, rows=50)
    try:
        res = passive_checkpoint_once(db, threshold_mb=0.0)
    finally:
        conn.close()
    assert res["checked"] is True
    assert res["mode"] == "passive"


def test_a_wal_above_the_hard_cap_reports_the_truncate_mode(tmp_path):
    db = tmp_path / "big.db"
    conn = _make_db(db, rows=2000)
    try:
        # cap far below the WAL's real size -> escalation must fire
        res = passive_checkpoint_once(db, threshold_mb=0.0, hard_cap_mb=0.001)
    finally:
        conn.close()
    assert res["checked"] is True
    assert res["mode"] == "truncate"


def test_the_default_hard_cap_is_two_gigabytes():
    assert DEFAULT_HARD_CAP_MB == 2048.0


def test_the_hard_cap_is_env_overridable_and_falls_back():
    assert env_hard_cap_mb({"BLM_WAL_HYGIENE_HARD_CAP_MB": "512"}) == 512.0
    assert env_hard_cap_mb({"BLM_WAL_HYGIENE_HARD_CAP_MB": "nonsense"}) \
        == DEFAULT_HARD_CAP_MB
    assert env_hard_cap_mb({}) == DEFAULT_HARD_CAP_MB


def test_a_missing_wal_is_gated_not_escalated(tmp_path):
    db = tmp_path / "never-written.db"
    sqlite3.connect(str(db)).close()
    res = passive_checkpoint_once(db, threshold_mb=256.0, hard_cap_mb=0.001)
    assert res["checked"] is False
    assert res["mode"] == "gated"


def test_the_pass_never_raises_on_an_unreadable_db(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all" * 100)
    res = passive_checkpoint_once(junk, threshold_mb=0.0, hard_cap_mb=0.001)
    assert res["mode"] in ("truncate", "passive", "error")
    assert res["checkpointed"] in (True, False)
