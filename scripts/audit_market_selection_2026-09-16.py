"""MARKET-SELECTION HISTORICAL AUDIT — 2026-09-16 (READ-ONLY, streaming).

Question: how often did the legacy positional selection (first ladder
row / lowest WS line) differ from the 1.85-band policy that production
now uses (event_parser.select_total_market)?

Populations (production DBs, opened read-only):

  A. blm_metrics_clean.db — clean_observations rows: the recorded
     live_total_line / over_price / under_price triple, classified by
     whether its own prices sit inside the preferred band.

  B. blm_metrics_clean.db — clean_market_observations capture batches
     (>= 2 distinct lines): selector pick vs legacy lowest-line pick.

  C. blm_pokerbet.db — market_observations eu-swarm WS batches
     (>= 2 distinct lines): selector pick vs
       * lowest line  (legacy clean-metrics WS fallback, ws_batch[0])
       * first line   (legacy DOM ladder[0] when the feed lists
         ascending — same as lowest for these batches)

Streaming design: multi-line batches are selected SQL-side
(GROUP BY ... HAVING COUNT(DISTINCT line_value) >= 2), streamed ordered
by (game, captured_at, line) and evaluated once per batch — no
full-table materialization, no recomputation.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")  # run from BLM/

from blm_v4.event_parser import (PREFERRED_PRICE_BAND, PREFERRED_PRICE_TARGET,
                                 select_total_market)

LO, HI = PREFERRED_PRICE_BAND
TARGET = PREFERRED_PRICE_TARGET
CLEAN_DB = Path("blm_metrics_clean.db")
MAIN_DB = Path("blm_pokerbet.db")


def _ro(path: Path) -> sqlite3.Connection:
    assert path.exists(), f"missing {path}"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def in_band(p) -> bool:
    return p is not None and LO <= float(p) <= HI


def same_line(a_line, b_line) -> bool:
    return a_line == b_line


def evaluate_batch(rows: list[dict]) -> dict:
    """One batch → {selector, legacy} picks (line/over/under each)."""
    sel = select_total_market([
        {"line": r["line_value"], "over": r["over_price"],
         "under": r["under_price"]} for r in rows
    ])
    low = min((r for r in rows if r["line_value"] is not None),
              key=lambda r: r["line_value"], default=None)
    legacy = (None if low is None else
              {"line": low["line_value"], "over": low["over_price"],
               "under": low["under_price"]})
    picked = {"line": sel["line"], "over": sel["over"],
              "under": sel["under"]}
    return {"sel": picked, "legacy": legacy, "rule": sel["rule"]}


# ── A. clean_observations: recorded triple band-classification ──────
print("=" * 74)
print("A. clean_observations — recorded (line, over, under) triples")
print("=" * 74)
conn = _ro(CLEAN_DB)
try:
    arows = conn.execute("""
        SELECT source_game_id, captured_at, live_total_line,
               over_price, under_price, market_source, status
        FROM clean_observations
        WHERE live_total_line IS NOT NULL
        ORDER BY captured_at
    """).fetchall()
finally:
    conn.close()

band_ct: Counter = Counter()
band_examples: dict[str, dict] = {}
for r in arows:
    both = (r["over_price"], r["under_price"])
    key = ("both_in_band" if all(in_band(p) for p in both)
           else "one_in_band" if any(in_band(p) for p in both)
           else "none_in_band")
    band_ct[key] += 1
    band_examples.setdefault(key, dict(r))
total = len(arows)
print(f"rows with a recorded line: {total}")
for k in ("both_in_band", "one_in_band", "none_in_band"):
    n = band_ct.get(k, 0)
    pct = (100.0 * n / total) if total else 0.0
    ex = band_examples.get(k)
    exs = (f"e.g. {ex['source_game_id']} @{ex['captured_at'][:19]} "
           f"line={ex['live_total_line']} o={ex['over_price']} "
           f"u={ex['under_price']} [{ex['market_source']}]") if ex else ""
    print(f"  {k:13s}: {n:6d} ({pct:5.1f}%)  {exs}")
out_of_band = band_ct.get("none_in_band", 0)
print(f"➜ recorded OUT-OF-BAND line/price pair on {out_of_band}/{total} "
      f"rows ({100.0 * out_of_band / total if total else 0:.1f}%)")


# ── batch audit (B and C share the streaming implementation) ────────
BATCH_SQL = """
    SELECT source_game_id, captured_at, line_value, over_price, under_price
    FROM {table}
    WHERE ({where}) AND (source_game_id, captured_at) IN (
        SELECT source_game_id, captured_at FROM {table}
        WHERE ({where})
        GROUP BY source_game_id, captured_at
        HAVING COUNT(DISTINCT line_value) >= 2)
    ORDER BY source_game_id, captured_at, line_value
"""


def audit_batches(conn: sqlite3.Connection, table: str, where: str,
                  label: str) -> None:
    print()
    print("=" * 74)
    print(f"{label} — multi-line batches: selector vs legacy-lowest")
    print("=" * 74)
    t0 = time.monotonic()
    cur = conn.execute(BATCH_SQL.format(table=table, where=where))

    divergent_games: set[str] = set()
    n_batches = 0
    n_div = 0
    rule_ct: Counter = Counter()
    size_ct: Counter = Counter()

    cur_key: tuple | None = None
    batch: list[dict] = []
    shown = 0

    def flush(gid, ts, rows_):
        nonlocal n_batches, n_div, shown
        if not rows_:
            return
        n_batches += 1
        size_ct[len(rows_)] += 1
        res = evaluate_batch(rows_)
        rule_ct[res["rule"]] += 1
        if (res["sel"]["line"] != (res["legacy"] or {}).get("line")
                or res["sel"]["over"] != (res["legacy"] or {}).get("over")
                or res["sel"]["under"] != (res["legacy"] or {}).get("under")):
            n_div += 1
            divergent_games.add(gid)
            if shown < 5:
                lg = res["legacy"]
                s = res["sel"]
                print(f"  DIVERGE {gid} @{ts[:19]}: "
                      f"lowest={lg['line']}@{lg['over']}/{lg['under']} → "
                      f"selector={s['line']}@{s['over']}/{s['under']} "
                      f"({res['rule']})")
                shown += 1

    for r in cur:
        key = (r["source_game_id"], r["captured_at"])
        if key != cur_key:
            if cur_key is not None:
                flush(cur_key[0], cur_key[1], batch)
            cur_key, batch = key, []
        batch.append(dict(r))
    if cur_key is not None:
        flush(cur_key[0], cur_key[1], batch)

    dt = time.monotonic() - t0
    print(f"{label}: {n_batches} multi-line batches "
          f"(sizes {dict(sorted(size_ct.items()))}); "
          f"selector ≠ legacy-lowest on {n_div} batches "
          f"({len(divergent_games)} games); rules {dict(rule_ct)}; "
          f"[{dt:.1f}s]")


# ── B. clean_market_observations capture batches ────────────────────
conn = _ro(CLEAN_DB)
try:
    audit_batches(conn, "clean_market_observations", "line_value IS NOT NULL",
                  "B. clean_market_observations")
finally:
    conn.close()

# ── C. blm_pokerbet.db market_observations WS batches ───────────────
conn = _ro(MAIN_DB)
try:
    audit_batches(conn, "market_observations", "market_type='MatchTotal'",
                  "C. market_observations (main DB, eu-swarm WS)")
finally:
    conn.close()

print()
print("=" * 74)
print("READ-ONLY AUDIT COMPLETE — no databases were modified.")
print("=" * 74)
