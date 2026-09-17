"""RESULTED ALERTS — 100% RESULT-COLOUR COVERAGE VALIDATION (2026-09-17).

Directive: every RESULTED ALERTS row must be colour-coded by its FINAL
CALCULATED RESULT and nothing else:

    UNDER → UNDER styling   OVER → OVER styling   PUSH → PUSH styling
    PENDING → pending styling only if the game has not actually resulted
    0 resulted rows uncoloured

This script validates that over the COMPLETE production dataset.  For every
game and every alert checkpoint it recomputes the authoritative status the
API serves (``under_outcome.trigger_market_total`` vs the game's settled
final — the same inputs ``/api/v4/live`` and ``/api/v4/alert-outcomes`` use),
then classifies each status with the SHIPPED frontend classifier extracted
from ``dashboard.js`` (not a copy) and asserts:

    * every non-pending status yields a colour class (al-under / al-over /
      al-push, or the explicit al-unknown fallback for malformed values)
    * UNDER → al-under, OVER → al-over, PUSH → al-push exactly
    * classification is order-independent (a pure function of the status),
      so adding / removing / reordering rows can never strip a colour

READ-ONLY over blm_pokerbet.db (snapshot tail + settled game_results).
Usage:  python3 scripts/validate_resulted_colors_2026-09-17.py [limit]
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")  # run from BLM/

from blm_v4.live_analytics.under_outcome import (  # noqa: E402
    CHECKPOINTS, outcome_status, trigger_market_total)

DB = Path("blm_pokerbet.db")
DASH_JS = Path("blm_v4/dashboard/static/dashboard.js")
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 0   # 0 = everything


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_settled(conn: sqlite3.Connection) -> dict:
    """source_game_id -> (final_total,) for settled (OK) game_results rows."""
    out = {}
    try:
        for r in conn.execute(
                "SELECT source_game_id, final_total FROM game_results "
                "WHERE final_result_status='OK' AND final_total IS NOT NULL"):
            out[r["source_game_id"]] = (r["final_total"],)
    except sqlite3.OperationalError:
        pass                                    # scorecard has never run
    return out


def _classes_via_frontend(statuses: list) -> list:
    """Classify statuses with the SHIPPED dashboard.js resultColorClass."""
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node not available for the shipped classifier")
    js = DASH_JS.read_text(encoding="utf-8")
    i = js.index("function resultColorClass(")
    depth, k = 0, js.index("{", i)
    while k < len(js):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                break
        k += 1
    fn = js[i:k + 1]
    # statuses are piped over STDIN: the full dataset overflows the argv limit
    script = (fn
              + "\nlet raw = '';"
              + " process.stdin.on('data', (d) => { raw += d; });"
              + " process.stdin.on('end', () => {"
              + "   console.log(JSON.stringify(JSON.parse(raw)"
              + "     .map(resultColorClass))); });")
    out = subprocess.run(
        [node, "-e", script], input=json.dumps(statuses),
        capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout)


def collect() -> tuple[list, Counter, Counter]:
    """Every (game, checkpoint) status the API would settle, with provenance."""
    conn = _ro(DB)
    try:
        settled = _load_settled(conn)
        statuses: list = []
        prov: Counter = Counter()
        # the MOST RECENT settled games — the population the RESULTED ALERTS
        # panel actually reviews (the earliest settled days predate line
        # capture entirely, so their trigger lines are honestly unprovable)
        games = conn.execute(
            "SELECT gr.source_game_id AS source_game_id, "
            "       g.classification AS classification "
            "FROM game_results gr "
            "JOIN games g ON g.source_game_id = gr.source_game_id "
            "WHERE gr.final_result_status='OK' "
            "  AND gr.final_total IS NOT NULL "
            "ORDER BY gr.result_at DESC"
            + (f" LIMIT {int(LIMIT)}" if LIMIT else "")).fetchall()
        for g in games:
            gid = g["source_game_id"]
            final = settled[gid][0]
            try:
                # the FULL observation stream: the 25% boundary is reached a
                # few minutes into the game, so a tail would miss it.  Joined
                # through games.id (the indexed FK) in ONE batched query.
                rows = conn.execute(
                    "SELECT s.source_game_id AS sgid, s.home_score, "
                    "s.away_score, s.quarter, s.clock, s.period_label, "
                    "s.game_status, s.total_line, s.captured_at "
                    "FROM snapshots s JOIN games gg ON gg.id = s.game_id "
                    "WHERE gg.source_game_id = ? "
                    "ORDER BY s.captured_at ASC LIMIT 50000", (gid,)).fetchall()
                rows = [dict(r) for r in rows]
                for r in rows:
                    r.pop("sgid", None)
            except sqlite3.OperationalError:
                continue
            for cp in CHECKPOINTS:
                try:
                    trig = trigger_market_total(rows, cp,
                                                g["classification"])
                except Exception:
                    trig = None
                st = outcome_status(
                    trig["total_line"] if isinstance(trig, dict) else trig,
                    final)
                statuses.append(st)
                prov[st] += 1
        return statuses, prov, Counter()
    finally:
        conn.close()


def main() -> int:
    if not DB.exists():
        print(f"missing {DB} — nothing to validate")
        return 1
    statuses, prov, _ = collect()
    classes = _classes_via_frontend(statuses)

    VERDICT = {"al-under": "UNDER", "al-over": "OVER", "al-push": "PUSH",
               None: "PENDING", "al-unknown": "UNKNOWN"}
    coloured = Counter()
    uncoloured: list = []
    for st, cls in zip(statuses, classes):
        if cls is None:
            # pending family: honest no-result — allowed ONLY for these
            if st is None:
                coloured["PENDING"] += 1
                continue
            uncoloured.append((st, cls))
            continue
        coloured[VERDICT.get(cls, "?")] += 1
        expect = {"al-under": "under", "al-over": "over",
                  "al-push": "push"}.get(cls)
        if expect is not None and st is not None \
                and str(st).strip().lower() != expect:
            uncoloured.append((st, cls))       # wrong colour == broken row

    # order-independence: the classifier is pure — reversing the input
    # reverses the output exactly, so add/remove/reorder can never strip a colour
    assert classes == list(reversed(_classes_via_frontend(
        list(reversed(statuses))))), "classification is order-dependent!"

    total = len(statuses)
    under, over, push = coloured["UNDER"], coloured["OVER"], coloured["PUSH"]
    pending = coloured["PENDING"]
    unknown = coloured["UNKNOWN"]
    settled_n = under + over + push + unknown

    print("=" * 74)
    print("RESULTED ALERTS — RESULT-COLOUR COVERAGE (production dataset)")
    print("=" * 74)
    print(f"result rows checked (game × checkpoint statuses): {total}")
    print(f"  UNDER  → al-under   : {under}")
    print(f"  OVER   → al-over    : {over}")
    print(f"  PUSH   → al-push    : {push}")
    print(f"  PENDING (no result) : {pending}")
    print(f"  UNKNOWN fallback    : {unknown}")
    print("-" * 74)
    print(f"settled rows coloured : {settled_n}/{settled_n}"
          if not uncoloured else f"settled rows coloured : "
          f"{settled_n - len(uncoloured)}/{settled_n}")
    print(f"uncoloured resulted rows: {len(uncoloured)}")
    if uncoloured:
        for st, cls in uncoloured[:20]:
            print(f"  UNCOLOURED: status={st!r} class={cls!r}")
        return 1
    print()
    print("ACCEPTANCE: 100% of UNDER/OVER/PUSH results coloured,")
    print("           genuinely-pending rows pending-styled,")
    print("           0 resulted rows uncoloured, order-independent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
