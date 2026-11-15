"""TEST-only contention checks for the shared pipeline SQLite database."""

import concurrent.futures
import multiprocessing
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from blm_v4.sqlite_coordination import (
    SQLITE_BUSY_TIMEOUT_MS,
    connect_pipeline_db,
    run_schema_stage,
)


def _writer(path: str, worker: int, rows: int) -> None:
    conn = connect_pipeline_db(path, row_factory=False)
    try:
        for n in range(rows):
            conn.execute("INSERT INTO writes(worker, n) VALUES (?, ?)",
                         (worker, n))
            conn.commit()
    finally:
        conn.close()


def _schema_worker(path: str, stage: str) -> None:
    def migrate(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS schema_hits (name TEXT)")
        conn.execute("INSERT INTO schema_hits(name) VALUES (?)", (stage,))
        time.sleep(0.03)

    run_schema_stage(path, stage, migrate)


def _crash_writer(path: str, ready) -> None:
    """Commit two batches, then hold an uncommitted batch for forced kill."""
    conn = connect_pipeline_db(path, row_factory=False)
    try:
        for batch in range(2):
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO writes(worker, n) VALUES (77, ?)",
                [(batch * 10 + n,) for n in range(10)])
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT INTO writes(worker, n) VALUES (77, ?)",
            [(100 + n,) for n in range(50)])
        ready.set()
        while True:
            time.sleep(1)
    finally:
        conn.close()


class SQLiteCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="blm-sqlite-test-")
        self.db = Path(self.temp.name) / "pipeline.db"
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA journal_mode=WAL")  # one-time TEST bootstrap
        conn.execute("CREATE TABLE writes(worker INTEGER, n INTEGER)")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_pipeline_connection_uses_common_timeout_without_wal_pragma(self):
        conn = connect_pipeline_db(self.db)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0],
                         SQLITE_BUSY_TIMEOUT_MS)
        conn.close()

    def test_schema_bootstrap_enables_wal_once_for_a_fresh_test_database(self):
        fresh = Path(self.temp.name) / "fresh.db"
        self.assertTrue(run_schema_stage(
            fresh, "bootstrap-test-v1", lambda conn: None))
        conn = sqlite3.connect(f"file:{fresh}?mode=ro", uri=True)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0],
                         "wal")
        conn.close()

    def test_simultaneous_server_collector_schema_stages_serialize(self):
        ctx = multiprocessing.get_context("spawn")
        jobs = [ctx.Process(target=_schema_worker,
                            args=(str(self.db), name))
                for name in ("server", "collector")]
        for job in jobs:
            job.start()
        for job in jobs:
            job.join(10)
            self.assertEqual(job.exitcode, 0)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM schema_hits").fetchone()[0], 2)
        conn.close()

    def test_multiple_concurrent_writers_preserve_all_records(self):
        ctx = multiprocessing.get_context("spawn")
        jobs = [ctx.Process(target=_writer, args=(str(self.db), i, 50))
                for i in range(4)]
        for job in jobs:
            job.start()
        for job in jobs:
            job.join(15)
            self.assertEqual(job.exitcode, 0)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT count(*) FROM writes").fetchone()[0],
                         200)
        self.assertEqual(conn.execute(
            "SELECT count(DISTINCT worker || ':' || n) FROM writes").fetchone()[0],
            200)
        conn.close()

    def test_repeated_connections_close_and_database_remains_healthy(self):
        for _ in range(200):
            conn = connect_pipeline_db(self.db)
            self.assertEqual(conn.execute("SELECT count(*) FROM writes").fetchone()[0], 0)
            conn.close()
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        conn.close()

    def test_passive_checkpoint_with_active_reader_and_writer(self):
        from blm_v4.wal_hygiene import passive_checkpoint_once

        reader = connect_pipeline_db(self.db)
        reader.execute("BEGIN")
        before = reader.execute("SELECT count(*) FROM writes").fetchone()[0]
        writer = connect_pipeline_db(self.db, row_factory=False)
        writer.execute("INSERT INTO writes VALUES (31, 1)")
        writer.commit()
        result = passive_checkpoint_once(self.db, threshold_mb=0)
        self.assertTrue(result["checked"])
        self.assertEqual(reader.execute("SELECT count(*) FROM writes").fetchone()[0], before)
        reader.commit()
        writer.close()
        reader.close()
        check = connect_pipeline_db(self.db)
        self.assertEqual(check.execute("SELECT count(*) FROM writes").fetchone()[0], before + 1)
        self.assertEqual(check.execute("PRAGMA quick_check").fetchone()[0], "ok")
        check.close()

    def test_forced_termination_recovers_committed_rows_and_discards_open_tx(self):
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        child = ctx.Process(target=_crash_writer, args=(str(self.db), ready))
        child.start()
        try:
            self.assertTrue(ready.wait(10), "writer did not reach open transaction")
            import os
            import signal
            os.kill(child.pid, signal.SIGKILL)
            child.join(10)
            self.assertFalse(child.is_alive())
            self.assertNotEqual(child.exitcode, 0)
        finally:
            if child.is_alive():
                child.kill()
                child.join(5)
        conn = connect_pipeline_db(self.db)
        self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("SELECT count(*) FROM writes WHERE worker=77").fetchone()[0], 20)
        self.assertEqual(conn.execute("SELECT count(*) FROM writes WHERE worker=77 AND n>=100").fetchone()[0], 0)
        conn.execute("INSERT INTO writes VALUES (78, 1)")
        conn.commit()
        conn.close()

    def test_busy_writer_recovers_without_lost_record(self):
        holder = sqlite3.connect(self.db, timeout=1, check_same_thread=False)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO writes VALUES (9, 1)")
        result = []

        def delayed_write():
            conn = connect_pipeline_db(self.db, row_factory=False)
            try:
                conn.execute("INSERT INTO writes VALUES (9, 2)")
                conn.commit()
                result.append("committed")
            except Exception as exc:  # surfaced to the assertion
                result.append(exc)
            finally:
                conn.close()

        worker = threading.Thread(target=delayed_write)
        worker.start()
        time.sleep(0.15)
        holder.commit()
        holder.close()
        worker.join(3)
        self.assertEqual(result, ["committed"])
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM writes WHERE worker=9").fetchone()[0], 2)
        conn.close()

    def test_passive_checkpoint_coexists_with_active_writer(self):
        from blm_v4.wal_hygiene import passive_checkpoint_once

        holder = sqlite3.connect(self.db, timeout=1, check_same_thread=False)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO writes VALUES (7, 1)")
        result = passive_checkpoint_once(self.db, threshold_mb=0)
        self.assertTrue(result["checked"])
        holder.commit()
        holder.close()

    def test_settle_schema_initialization_waits_out_active_writer(self):
        from blm_v4.settle_worker import settle_once
        from blm_v4.storage import PokerBetStore

        PokerBetStore(self.db)
        holder = sqlite3.connect(self.db, timeout=1, check_same_thread=False)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO writes VALUES (6, 1)")
        result = []

        def initialize_worker():
            try:
                result.append(settle_once(self.db, batch_limit=1))
            except Exception as exc:
                result.append(exc)

        worker = threading.Thread(target=initialize_worker)
        worker.start()
        time.sleep(0.15)
        holder.commit()
        holder.close()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], dict)
        self.assertEqual(result[0]["checked"], 0)

    def test_worker_stage_retries_after_restart_without_duplicate_stage(self):
        def migration(conn):
            conn.execute("CREATE TABLE IF NOT EXISTS restart_test (id INTEGER)")
            conn.execute("INSERT INTO restart_test VALUES (1)")

        self.assertTrue(run_schema_stage(self.db, "restart-test-v1", migration))
        self.assertFalse(run_schema_stage(self.db, "restart-test-v1", migration))
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM restart_test").fetchone()[0], 1)
        conn.close()

    def test_concurrent_execution_ledger_writes_keep_unique_jobs(self):
        from blm_v4.execution.store import ExecutionStore

        path = Path(self.temp.name) / "execution-ledger.db"
        stores = [ExecutionStore(str(path)) for _ in range(4)]
        failures = []

        def write_job(i):
            try:
                stores[i % len(stores)].upsert_job(
                    f"exec-{i}", {"parlay_id": f"parlay-{i}",
                                  "combination_id": f"combo-{i}",
                                  "fold_size": 1,
                                  "status": "pending",
                                  "legs": [{"event": "test"}]}, "test")
            except Exception as exc:
                failures.append(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(write_job, range(80)))
        self.assertEqual(failures, [])
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM execution_jobs").fetchone()[0], 80)
        conn.close()

    def test_result_reconciler_and_settler_schema_during_scorecard_writer(self):
        from blm_v4.result_reconciler import ResultReconciler
        from blm_v4.scorecard import Scorecard
        from blm_v4.settle_worker import settle_once
        from blm_v4.storage import PokerBetStore

        # This is a fresh, isolated TEST DB; establish collector schema first.
        PokerBetStore(self.db)
        barrier = threading.Barrier(3)
        failures = []

        def initialize(fn):
            try:
                barrier.wait(timeout=3)
                fn()
            except Exception as exc:
                failures.append(exc)

        # A scorecard transaction is active while result and settle startup
        # migrations compete. The coordinator must serialize the DDL stages.
        def scorecard_activity():
            conn = connect_pipeline_db(self.db)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO writes VALUES (8, 1)")
                barrier.wait(timeout=3)
                time.sleep(0.2)
                conn.commit()
            finally:
                conn.close()

        tasks = [
            threading.Thread(target=initialize, args=(lambda: Scorecard(self.db),)),
            threading.Thread(target=initialize, args=(lambda: ResultReconciler(self.db),)),
            threading.Thread(target=scorecard_activity),
        ]
        for task in tasks:
            task.start()
        for task in tasks:
            task.join(10)
            self.assertFalse(task.is_alive())
        self.assertEqual(failures, [])

        # SettleWorker's standalone pass also uses the same once-only schema
        # stage and exits cleanly with no ended fixtures.
        stats = settle_once(self.db, batch_limit=1)
        self.assertEqual(stats["checked"], 0)


if __name__ == "__main__":
    unittest.main()
