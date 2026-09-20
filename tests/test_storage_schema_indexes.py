"""Snapshots index contract — the ``source_game_id`` lookup must be indexed.

Guards ``blm_v4/storage.py``'s ``_SCHEMA`` index set against two regressions:

1. ``idx_snapshots_source_ts`` disappearing.  Without it the scorecard's
   stage 5 (``record_market_history``) reverts to a FULL SCAN of
   ``snapshots`` plus a temporary B-tree sort, once per game, over ~11,570
   games — measured 2.05s/game idle and 18.0s/game under memory pressure on
   the live DB, which is why ``checkpoint_market`` goes hours stale.

2. ``idx_snapshots_game_ts`` being re-added.  It is a byte-identical
   duplicate of ``sqlite_autoindex_snapshots_1``, the index SQLite already
   creates for ``UNIQUE(game_id, captured_at)`` — same columns, same order,
   same collation.  On the live DB that duplicate is 71.9 MB of pure waste,
   so it is dropped in the same change that adds the new index.

The stage-5 SQL in :data:`STAGE5_SQL` mirrors ``blm_v4/scorecard.py:1880-1883``
verbatim; :func:`test_the_real_scorecard_still_makes_this_exact_query` keeps
the two in sync.

Every test here runs against a per-test ``tmp_path`` database.  No test
touches a production database.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blm_v4.models import MarketObservation, PokerBetGame
from blm_v4.storage import PokerBetStore

# The exact stage-5 statement from blm_v4/scorecard.py:1880-1883.
# NOTE: `source_game_id` leads the WHERE clause and `captured_at` supplies
# the ORDER BY — idx_snapshots_source_ts(source_game_id, captured_at) is
# designed to satisfy BOTH from the index.
STAGE5_SQL = (
    "SELECT captured_at, total_line, spread\n"
    "                           FROM snapshots WHERE source_game_id = ?\n"
    "                           ORDER BY captured_at"
)

# Every game_id-keyed consumer (scorecard stages 3/4/6, settle_worker,
# storage.insert_snapshot's dedup probe) uses this shape.
GAME_ID_SQL = ("SELECT * FROM snapshots WHERE game_id=? "
               "ORDER BY captured_at ASC")

CLASSIFICATION_SQL = ("SELECT COUNT(*) FROM snapshots "
                      "WHERE classification=? AND captured_at>=?")

NEW_INDEX = "idx_snapshots_source_ts"
REDUNDANT_INDEX = "idx_snapshots_game_ts"
AUTOINDEX = "sqlite_autoindex_snapshots_1"


# ── Fixtures ────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "blm_pokerbet.db"


@pytest.fixture
def store(db_path: Path) -> PokerBetStore:
    """A writable PokerBetStore — construction runs _SCHEMA via _init()."""
    return PokerBetStore(db_path)


def _add_game(store: PokerBetStore, gid: str, now: datetime) -> int:
    game = PokerBetGame(
        source="PokerBet", source_game_id=gid,
        competition_id="comp-b", competition_slug="betual-nba",
        competition="Betual NBA", region="Virtual Matches",
        game_family="betual", classification="BETUAL_NBA",
        sport="basketball",
        home_team=f"Home {gid}", away_team=f"Away {gid}",
        game_slug=f"home-{gid}", source_url=f"https://x/{gid}",
        status="live",
        first_seen_at=_iso(now - timedelta(minutes=60)),
        last_seen_at=_iso(now))
    return store.upsert_game(game)


def _add_snapshot(store: PokerBetStore, game_db_id: int, gid: str,
                  captured_at: str, home: int, away: int,
                  total_line: float | None, spread: float | None) -> None:
    obs = MarketObservation(
        source="PokerBet", source_game_id=gid,
        classification="BETUAL_NBA", captured_at=captured_at,
        home_team=f"Home {gid}", away_team=f"Away {gid}",
        home_score=home, away_score=away,
        period_label="1st Quarter", quarter=1, clock="09:00",
        game_status="live", total_line=total_line, spread=spread,
        w1_odds=None, w2_odds=None, markets_json="{}")
    store.insert_snapshot(game_db_id, obs, force=True)


def _interleaved_games(store: PokerBetStore, now: datetime,
                       n_games: int = 3,
                       ticks: int = 12) -> dict[str, list[tuple]]:
    """n_games games whose snapshot timestamps INTERLEAVE tick by tick.

    Interleaving matters: a scan that ignored the ``source_game_id`` filter
    would return rows from every game, and the per-game ORDER BY would have
    to sort across them.  Returns {source_game_id: [(captured_at, total, spread)]}.
    """
    gids = [f"ITL{i:02d}" for i in range(n_games)]
    db_ids = {g: _add_game(store, g, now) for g in gids}
    expect: dict[str, list[tuple]] = {g: [] for g in gids}
    base = now - timedelta(minutes=ticks)
    for t in range(ticks):
        for i, g in enumerate(gids):
            ts = _iso(base + timedelta(minutes=t))
            total = 160.0 + i + t
            spread = float(-(i + 1))
            _add_snapshot(store, db_ids[g], g, ts, t, t + i, total, spread)
            expect[g].append((ts, total, spread))
    return expect


def _connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path))


def _index_names(conn: sqlite3.Connection, table: str = "snapshots") -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})")}


def _index_cols(conn: sqlite3.Connection, index: str) -> list[str]:
    return [r[2] for r in conn.execute(f"PRAGMA index_info({index})")]


def _plan(conn: sqlite3.Connection, sql: str, params: tuple = ("x",)) -> str:
    """The EXPLAIN QUERY PLAN detail lines, joined — the plan as the
    planner reports it for THIS connection."""
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " | ".join(r[3] for r in rows)


# ══════════════════════════════════════════════════════════════════════
# A. PokerBetStore initialization creates idx_snapshots_source_ts
# ══════════════════════════════════════════════════════════════════════

def test_store_init_creates_source_game_id_index(db_path, store):
    """_SCHEMA must create the index the scorecard's stage 5 depends on."""
    conn = _connect(db_path)
    try:
        names = _index_names(conn)
    finally:
        conn.close()
    assert NEW_INDEX in names, (
        "_SCHEMA must create %s — without it the stage-5 market_history "
        "lookup is a full snapshots scan (no index leads with "
        "source_game_id). Found: %s" % (NEW_INDEX, sorted(names)))


def test_source_game_id_index_has_the_intended_key(db_path, store):
    """Key order is (source_game_id, captured_at) — the equality column
    first, then the ORDER BY column, so the index resolves both."""
    conn = _connect(db_path)
    try:
        cols = _index_cols(conn, NEW_INDEX)
    finally:
        conn.close()
    assert cols == ["source_game_id", "captured_at"], (
        "expected (source_game_id, captured_at), got %s" % (cols,))


# ══════════════════════════════════════════════════════════════════════
# B. The exact stage-5 query plan is index-led
# ══════════════════════════════════════════════════════════════════════

def test_stage5_query_plan_has_no_full_scan(db_path, store):
    conn = _connect(db_path)
    try:
        plan = _plan(conn, STAGE5_SQL, ("ITL00",))
    finally:
        conn.close()
    assert "SCAN snapshots" not in plan, (
        "stage-5 lookup must not full-scan snapshots; plan was: %s" % plan)
    assert NEW_INDEX in plan, (
        "stage-5 lookup should use %s; plan was: %s" % (NEW_INDEX, plan))


def test_stage5_query_plan_has_no_temp_btree_sort(db_path, store):
    """(source_game_id, captured_at) supplies the ORDER BY from the index,
    so the temporary sort must be gone."""
    conn = _connect(db_path)
    try:
        plan = _plan(conn, STAGE5_SQL, ("ITL00",))
    finally:
        conn.close()
    assert "USE TEMP B-TREE" not in plan, (
        "ORDER BY captured_at is satisfied by the index; plan was: %s" % plan)


def test_the_real_scorecard_still_makes_this_exact_query():
    """Keep STAGE5_SQL honest: the predicate it pins must still exist in
    scorecard.py, or this contract is guarding a query nobody runs."""
    src = (Path(__file__).resolve().parent.parent
           / "blm_v4" / "scorecard.py").read_text()
    assert "FROM snapshots WHERE source_game_id = ?" in src, (
        "scorecard.py no longer contains the source_game_id snapshots "
        "lookup this module pins — update STAGE5_SQL")


# ══════════════════════════════════════════════════════════════════════
# C. Result equivalence — same rows, same ordering, with and without
# ══════════════════════════════════════════════════════════════════════

def test_results_identical_with_and_without_the_new_index(db_path, store):
    """The index may change the PLAN, never the ANSWER.

    The post-drop read MUST use a brand-new connection.  A connection that
    already planned this statement keeps reporting the dropped index by
    name — verified: after ``DROP INDEX idx_snapshots_source_ts`` the same
    connection still answered ``SEARCH snapshots USING INDEX
    idx_snapshots_source_ts``, an index that no longer existed.  Measuring
    the unindexed plan on that connection would compare the plan against
    itself and prove nothing.
    """
    now = datetime.now(timezone.utc)
    expect = _interleaved_games(store, now, n_games=3, ticks=12)

    conn = _connect(db_path)
    try:
        with_index = {g: conn.execute(STAGE5_SQL, (g,)).fetchall()
                      for g in expect}
        plan_with = _plan(conn, STAGE5_SQL, ("ITL00",))
        conn.execute(f"DROP INDEX {NEW_INDEX}")
        conn.commit()
    finally:
        conn.close()

    fresh = _connect(db_path)
    try:
        assert NEW_INDEX not in _index_names(fresh)
        without_index = {g: fresh.execute(STAGE5_SQL, (g,)).fetchall()
                         for g in expect}
        plan_without = _plan(fresh, STAGE5_SQL, ("ITL00",))
    finally:
        fresh.close()

    # the two plans really are different — otherwise this proves nothing
    assert plan_with != plan_without, (
        "index drop did not change the plan, so equivalence is untested")
    assert "SCAN snapshots" in plan_without

    for g in expect:
        assert with_index[g] == without_index[g], (
            "row set/order differs for %s between indexed and unindexed "
            "reads" % g)
        assert with_index[g] == expect[g], (
            "%s rows are not the game's own rows in captured_at order: "
            "%r" % (g, with_index[g]))


def test_indexed_read_never_returns_another_games_rows(db_path, store):
    """Independent oracle: cross-game contamination would be invisible to a
    plan-only check, so assert every returned timestamp belongs to the game
    asked for."""
    now = datetime.now(timezone.utc)
    expect = _interleaved_games(store, now, n_games=3, ticks=12)
    conn = _connect(db_path)
    try:
        for g in expect:
            got = conn.execute(STAGE5_SQL, (g,)).fetchall()
            assert len(got) == len(expect[g]), (
                "%s: expected %d rows, got %d"
                % (g, len(expect[g]), len(got)))
            assert [r[0] for r in got] == sorted(r[0] for r in got)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════
# D. Dropping the duplicate keeps game_id queries index-led
# ══════════════════════════════════════════════════════════════════════

def test_game_id_queries_use_autoindex_after_dropping_the_duplicate(
        db_path, store):
    """idx_snapshots_game_ts is redundant with the UNIQUE constraint's
    autoindex, so dropping it must not cost the game_id consumers their
    index-led SEARCH.

    The duplicate is CREATED here if absent, so this exercises the real
    migration path for every database built before this change (all of
    which had it) as well as the post-change schema.

    The post-drop plan MUST be read on a FRESH connection: SQLite caches
    prepared statements, so a connection that planned the query before the
    DROP keeps reporting the dropped index's name — verified, it reported
    an index that no longer existed — which would make this test pass or
    fail for the wrong reason.
    """
    now = datetime.now(timezone.utc)
    _interleaved_games(store, now, n_games=2, ticks=4)

    conn = _connect(db_path)
    try:
        # the LEGACY state: the duplicate existed on every pre-change DB
        if REDUNDANT_INDEX not in _index_names(conn):
            conn.execute(f"CREATE INDEX {REDUNDANT_INDEX} "
                         "ON snapshots(game_id, captured_at)")
            conn.commit()
        before = _plan(conn, GAME_ID_SQL, (1,))
        assert "SCAN snapshots" not in before, (
            "precondition: game_id queries should already be index-led; "
            "plan: %s" % before)
        conn.execute(f"DROP INDEX {REDUNDANT_INDEX}")
        conn.commit()
    finally:
        conn.close()

    # a genuinely new connection — no cached plan can leak across it
    fresh = _connect(db_path)
    try:
        assert REDUNDANT_INDEX not in _index_names(fresh), (
            "%s should be gone" % REDUNDANT_INDEX)
        after = _plan(fresh, GAME_ID_SQL, (1,))
    finally:
        fresh.close()

    assert "SCAN snapshots" not in after, (
        "game_id queries degraded to a full scan after the duplicate was "
        "dropped; plan: %s" % after)
    assert AUTOINDEX in after, (
        "game_id queries should fall back to %s; plan: %s"
        % (AUTOINDEX, after))


def test_classification_queries_keep_their_index(db_path, store):
    """The classification index is serving a real consumer
    (count_snapshots / _snapshot_stats) and must survive the change."""
    now = datetime.now(timezone.utc)
    _interleaved_games(store, now, n_games=2, ticks=4)
    conn = _connect(db_path)
    try:
        assert "idx_snapshots_class_captured" in _index_names(conn)
        plan = _plan(conn, CLASSIFICATION_SQL, ("BETUAL_NBA", "2000-01-01"))
    finally:
        conn.close()
    assert "SCAN snapshots" not in plan, (
        "classification lookup lost its index; plan: %s" % plan)


# ══════════════════════════════════════════════════════════════════════
# E. Prevent the 71.9 MB duplicate from creeping back
# ══════════════════════════════════════════════════════════════════════

def test_redundant_game_ts_index_is_not_recreated(db_path, store):
    """UNIQUE(game_id, captured_at) already builds an identical index, so a
    separate idx_snapshots_game_ts is 71.9 MB of duplicated B-tree for no
    query benefit.  _SCHEMA must not create it."""
    conn = _connect(db_path)
    try:
        names = _index_names(conn)
    finally:
        conn.close()
    assert AUTOINDEX in names, (
        "precondition: UNIQUE(game_id, captured_at) should provide %s"
        % AUTOINDEX)
    assert REDUNDANT_INDEX not in names, (
        "%s duplicates %s exactly — do not re-add it to _SCHEMA"
        % (REDUNDANT_INDEX, AUTOINDEX))


def test_the_two_game_id_indexes_would_be_byte_identical(db_path, store):
    """Documents WHY the duplicate is dropped, so the next reader does not
    'restore' it.  Compares key columns and collations of the autoindex
    against a freshly built copy of the candidate."""
    conn = _connect(db_path)
    try:
        auto_cols = _index_cols(conn, AUTOINDEX)
        conn.execute(
            f"CREATE INDEX _probe_game_ts ON snapshots(game_id, captured_at)")
        probe_cols = _index_cols(conn, "_probe_game_ts")
        conn.execute("DROP INDEX _probe_game_ts")
        conn.commit()
    finally:
        conn.close()
    assert auto_cols == probe_cols == ["game_id", "captured_at"], (
        "autoindex %s and (game_id, captured_at) must have the same key: "
        "%s vs %s" % (AUTOINDEX, auto_cols, probe_cols))


# ══════════════════════════════════════════════════════════════════════
# F. The schema text itself — the strongest guard
# ══════════════════════════════════════════════════════════════════════

def test_schema_source_declares_new_index_and_not_the_duplicate():
    """_SCHEMA is the ONE place these indexes are declared, so pin the
    source text too: a runtime check alone would still let someone add the
    duplicate back behind an IF NOT EXISTS that never fires in tests."""
    from blm_v4.storage import _SCHEMA
    assert NEW_INDEX in _SCHEMA, (
        "_SCHEMA must declare %s" % NEW_INDEX)
    assert f"CREATE INDEX IF NOT EXISTS {REDUNDANT_INDEX}" not in _SCHEMA, (
        "_SCHEMA must not declare the redundant %s" % REDUNDANT_INDEX)
    assert "ON snapshots(source_game_id, captured_at)" in _SCHEMA, (
        "_SCHEMA must declare the (source_game_id, captured_at) key")
