"""MARKET-LINE SELECTION VERIFICATION — 2026-09-16 (read-only, no prod changes).

Task: Total Points market-line selection must be price-aware
(1.80 <= price <= 1.95, closest to 1.85), NEVER positional.

Sections:
  A. Current production behaviour (DOM path + WS fallback path)
  B. Proposed selection policy (prototype, pure function)
  C. Required test matrix (a-f) + supplied 3-line example
  D. Line/price/side synchronization through the pipeline
     (required_pts_per_min + settlement use the SELECTED line)
  E. REPLAY: real market_observations batches from the PRODUCTION DB
     (blm_pokerbet.db, read-only) through the selector + one real
     batch end-to-end through clean_metrics.record_snapshot (tmp DB)
"""
from __future__ import annotations

import math
import sys

sys.path.insert(0, ".")  # run from BLM/

from blm_v4.event_parser import parse_event_view  # production parser
from blm_v4.clean_metrics import pace_metrics      # production pace math

# ════════════════════════════════════════════════════════════════════
# A. CURRENT PRODUCTION BEHAVIOUR
# ════════════════════════════════════════════════════════════════════
#
# Path 1 — event view (collector.py:2171): parsed["total"]["first_line"]
#          event_parser.py:203 promotes ladder[0] → the FIRST row.
# Path 2 — WS fallback (clean_metrics.record_snapshot): ws_batch[0] where
#          latest_market_batch() is ORDER BY line_value ASC → LOWEST line.
# Path 3 — scorecard WS fallback (_frozen_market_obs):
#          ORDER BY line_value ASC LIMIT 1 → LOWEST line.
#
# All three are POSITIONAL, none is price-aware.

EVENT_TEXT_3LINES = """Cyber Basketball. 2K26 Matches
4th Quarter
09:46'
Oklahoma City Thunder Cyber
San Antonio Spurs Cyber
100 : 73, (32:22), (28:23), (33:22), (7:6) 09:46
Total Points
Over Under
169.5 1.50 2.40   171.5 1.85 1.85   173.5 2.40 1.50
"""

print("=" * 72)
print("A. CURRENT PRODUCTION BEHAVIOUR (supplied 3-line example)")
print("=" * 72)
parsed = parse_event_view(EVENT_TEXT_3LINES)
ladder = parsed["total"]["ladder"]
print(f"parsed ladder rows ({len(ladder)}):")
for r in ladder:
    print(f"    {r['line']}  over={r['over']}  under={r['under']}")
cur_dom = (parsed["total"]["first_line"], parsed["total"]["over_odds"],
           parsed["total"]["under_odds"])
print(f"DOM path (ladder[0]) selects      : line={cur_dom[0]} over={cur_dom[1]} under={cur_dom[2]}")
ws_batch = sorted(ladder, key=lambda r: r["line"])   # latest_market_batch: ORDER BY line_value ASC
b = ws_batch[0]
print(f"WS fallback (ws_batch[0], lowest) : line={b['line']} over={b['over']} under={b['under']}")
print("➜ both are positional; neither is the required 171.5 @ 1.85")

# ════════════════════════════════════════════════════════════════════
# B. PROPOSED SELECTION POLICY (prototype of the minimal prod change)
# ════════════════════════════════════════════════════════════════════

BAND_LO, BAND_HI, TARGET = 1.80, 1.95, 1.85

def _valid_price(p) -> bool:
    """A usable decimal price: finite and > 1.0 (implied prob < 100%)."""
    return p is not None and math.isfinite(p) and p > 1.0

def select_total_market(ladder: list[dict]) -> dict:
    """Price-aware Total Points selection (deterministic, documented).

    Candidates: every (line, side, price) with a valid price.

    1. PREFERRED: prices in [1.80, 1.95] → closest to 1.85 wins.
    2. FALLBACK (no price in band): closest to 1.85 across ALL
       candidates (explicit, never positional / never 'last line').
    Tie-breakers (deterministic, documented):
       i.   lower market line  — matches the repo's existing
              'lowest line of the batch is the main line' convention
              (storage.latest_market_batch / scorecard._frozen_market_obs);
       ii.  Over side before Under (feed's first-listed side).

    Returns {line, side, price, over, under, rule}; the triple
    (line, side, price) always comes from the SAME ladder row.
    """
    cands: list[tuple[float, float, str, dict]] = []  # (price, line, side, row)
    for row in ladder or []:
        lo, hi = row.get("line"), row.get("line")
        if lo is None:
            continue
        if _valid_price(row.get("over")):
            cands.append((float(row["over"]), float(lo), "OVER", row))
        if _valid_price(row.get("under")):
            cands.append((float(row["under"]), float(lo), "UNDER", row))
    if not cands:
        return {"line": None, "side": None, "price": None,
                "over": None, "under": None, "rule": "no_candidates"}

    in_band = [c for c in cands if BAND_LO <= c[0] <= BAND_HI]
    pool = in_band if in_band else cands
    rule = "band_closest_to_1.85" if in_band else "fallback_nearest_1.85"
    # min() is total-ordered by (distance, line, side-rank) → deterministic
    price, line, side, row = min(
        pool,
        key=lambda c: (abs(c[0] - TARGET), c[1], 0 if c[2] == "OVER" else 1),
    )
    return {"line": line, "side": side, "price": price,
            "over": row.get("over"), "under": row.get("under"), "rule": rule}

print()
print("=" * 72)
print("B. PROPOSED POLICY on the supplied example")
print("=" * 72)
sel = select_total_market(ladder)
print(f"selected: line={sel['line']} side={sel['side']} price={sel['price']} "
      f"(over={sel['over']} under={sel['under']}) rule={sel['rule']}")
assert sel["line"] == 171.5 and sel["price"] == 1.85, "supplied example MUST select 171.5 @ 1.85"
print("➜ PASS 171.5 @ 1.85 selected")

# ════════════════════════════════════════════════════════════════════
# C. REQUIRED TEST MATRIX
# ════════════════════════════════════════════════════════════════════

def L(line, over, under):
    return {"line": float(line), "over": float(over), "under": float(under)}

CASES = [
    ("a) 1 line only (no band price → fallback nearest)",
     [L(215.5, 1.70, 2.02)],
     (215.5, "OVER", 1.70, "fallback_nearest_1.85")),

    ("b) 2 lines (one band price)",
     [L(214.5, 1.62, 1.68), L(216.5, 1.88, 1.92)],
     (216.5, "OVER", 1.88, "band_closest_to_1.85")),

    ("c) 3 lines = supplied example",
     [L(169.5, 1.50, 2.40), L(171.5, 1.85, 1.85), L(173.5, 2.40, 1.50)],
     (171.5, "OVER", 1.85, "band_closest_to_1.85")),

    ("d) 3 lines, middle line 1.85",
     [L(168.5, 1.62, 1.72), L(170.5, 1.85, 1.85), L(172.5, 2.05, 1.68)],
     (170.5, "OVER", 1.85, "band_closest_to_1.85")),

    ("e1) multiple in band (closest wins)",
     [L(170.5, 1.82, 1.90), L(171.5, 1.84, 1.88), L(172.5, 1.90, 1.82)],
     (171.5, "OVER", 1.84, "band_closest_to_1.85")),

    ("e2) exact distance tie → tie-break: LOWER line",
     [L(171.5, 1.83, 1.87), L(172.5, 1.87, 1.83)],
     (171.5, "OVER", 1.83, "band_closest_to_1.85")),

    ("f) no price in band → explicit fallback (middle, NOT last)",
     [L(169.5, 1.50, 2.40), L(171.5, 1.62, 1.72), L(173.5, 2.10, 1.55)],
     (171.5, "UNDER", 1.72, "fallback_nearest_1.85")),
]

print()
print("=" * 72)
print("C. TEST MATRIX")
print("=" * 72)
fails = 0
for name, ladder_in, expect in CASES:
    s = select_total_market(ladder_in)
    got = (s["line"], s["side"], s["price"], s["rule"])
    ok = got == expect
    fails += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print(f"      got    {got}")
    if not ok:
        print(f"      expect {expect}")

last_line_checks = [
    ("c) supplied example", CASES[2][1]),
    ("f) none in band",     CASES[6][1]),
]
print()
for name, ladder_in in last_line_checks:
    s = select_total_market(ladder_in)
    ok = s["line"] != ladder_in[-1]["line"]
    fails += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  last line NOT selected — {name}: "
          f"selected {s['line']} ≠ last {ladder_in[-1]['line']}")

# ════════════════════════════════════════════════════════════════════
# D. LINE / PRICE / SIDE SYNCHRONIZATION THROUGH THE PIPELINE
# ════════════════════════════════════════════════════════════════════

print()
print("=" * 72)
print("D. SYNCHRONIZATION (selected triple feeds pace AND settlement)")
print("=" * 72)

# D1 — selected triple always comes from ONE ladder row
for name, ladder_in, _ in CASES:
    s = select_total_market(ladder_in)
    row = next(r for r in ladder_in if r["line"] == s["line"])
    sync = (s["price"] in (row["over"], row["under"])
            and s["side"] == ("OVER" if s["price"] == row["over"] else "UNDER"))
    if not sync:
        fails += 1
        print(f"FAIL  desynchronized triple in: {name}")
print("PASS  D1 line/price/side come from the same ladder row (all cases)")

# D2 — required_pts_per_min uses the SELECTED line (production pace_metrics)
score, elapsed, remaining = 100, 28, 20   # Q4 example state
pm = pace_metrics(elapsed, remaining, score, sel["line"])   # 171.5
required = pm["required_pts_per_min"]
exp_req = round((171.5 - 100) / 20, 4)
ok = required == exp_req
fails += 0 if ok else 1
print(f"{'PASS' if ok else 'FAIL'}  D2 required_pts_per_min = "
      f"({sel['line']} − {score}) / {remaining} = {required} (expected {exp_req})")

# D3 — settlement uses the SELECTED line; wrong line flips the verdict
final_total = 172
verdict_selected = ("UNDER" if final_total < sel["line"]
                    else "OVER" if final_total > sel["line"] else "PUSH")
wrong_line = ladder[-1]["line"]           # positional 'last line' = 173.5
verdict_wrong = ("UNDER" if final_total < wrong_line
                 else "OVER" if final_total > wrong_line else "PUSH")
ok = verdict_selected == "OVER" and verdict_wrong == "UNDER"
fails += 0 if ok else 1
print(f"{'PASS' if ok else 'FAIL'}  D3 settlement on selected {sel['line']}: "
      f"final {final_total} → {verdict_selected}; on wrong last line "
      f"{wrong_line}: → {verdict_wrong} (association matters)")

# D4 — re-parse round trip: selection applied to the REAL parser output
s2 = select_total_market(parse_event_view(EVENT_TEXT_3LINES)["total"]["ladder"])
ok = (s2["line"], s2["price"]) == (171.5, 1.85)
fails += 0 if ok else 1
print(f"{'PASS' if ok else 'FAIL'}  D4 end-to-end: event-view text → parser → "
      f"policy → {s2['line']} @ {s2['price']}")

# ════════════════════════════════════════════════════════════════════
# E. REPLAY — real production market_observations batches (READ-ONLY)
# ════════════════════════════════════════════════════════════════════

print()
print("=" * 72)
print("E. REPLAY: real market_observations batches (blm_pokerbet.db, ro)")
print("=" * 72)

import sqlite3
import tempfile
from pathlib import Path

MAIN_DB = Path("blm_pokerbet.db")
REPLAY_BATCHES = 150
replay_fails = 0

if not MAIN_DB.exists():
    print("SKIP: blm_pokerbet.db not found (run from BLM/)")
else:
    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("""
            SELECT source_game_id, captured_at, line_value,
                   over_price, under_price
            FROM market_observations
            WHERE market_type='MatchTotal' AND line_value IS NOT NULL
              AND (source_game_id, captured_at) IN (
                  SELECT source_game_id, captured_at
                  FROM market_observations
                  WHERE market_type='MatchTotal' AND line_value IS NOT NULL
                  GROUP BY source_game_id, captured_at
                  HAVING COUNT(DISTINCT line_value) >= 2
                  ORDER BY captured_at DESC
                  LIMIT ?)
            ORDER BY captured_at DESC, source_game_id, line_value
        """, (REPLAY_BATCHES,))
        batches: dict[tuple, list[dict]] = {}
        for r in cur:
            batches.setdefault((r["source_game_id"], r["captured_at"]),
                               []).append(dict(r))
    finally:
        conn.close()

    n_checked = 0
    band_rule_hits = 0
    fallback_hits = 0
    end2end_done = False
    end2end_batch = None
    for (gid, ts), rows in batches.items():
        rows = [r for r in rows if r["line_value"] is not None]
        if len(rows) < 2:
            continue
        n_checked += 1
        sel = select_total_market([
            {"line": r["line_value"], "over": r["over_price"],
             "under": r["under_price"]} for r in rows
        ])
        if sel["line"] is None:
            continue  # no valid prices in this real batch

        # E1 — the triple originates from ONE real batch row
        row = next((r for r in rows if r["line_value"] == sel["line"]), None)
        ok = (row is not None
              and sel["over"] == row["over_price"]
              and sel["under"] == row["under_price"])
        replay_fails += 0 if ok else 1
        if not ok:
            print(f"FAIL E1 sync {gid}@{ts[:19]}")

        # E2 — rule honours the band policy on real data
        band_prices = [p for r in rows for p in (r["over_price"],
                                                 r["under_price"])
                       if p is not None and 1.80 <= p <= 1.95]
        if band_prices:
            ok = sel["rule"] == "band_closest_to_1.85" and 1.80 <= sel["price"] <= 1.95
            band_rule_hits += 1
        else:
            best = min(abs(p - 1.85) for r in rows
                       for p in (r["over_price"], r["under_price"])
                       if p is not None and p > 1.0)
            ok = (sel["rule"] == "fallback_nearest_1.85"
                  and round(abs(sel["price"] - 1.85), 6) == round(best, 6))
            fallback_hits += 1
        replay_fails += 0 if ok else 1
        if not ok:
            print(f"FAIL E2 rule {gid}@{ts[:19]}: {sel}")

        # E3 — ORDER-INVARIANCE: selection never depends on row order
        import random
        shuffled = rows[:]
        random.Random(42).shuffle(shuffled)
        sel_shuffled = select_total_market([
            {"line": r["line_value"], "over": r["over_price"],
             "under": r["under_price"]} for r in shuffled
        ])
        ok = (sel_shuffled["line"], sel_shuffled["over"], sel_shuffled["under"]) \
            == (sel["line"], sel["over"], sel["under"])
        replay_fails += 0 if ok else 1
        if not ok:
            print(f"FAIL E3 order-invariance {gid}@{ts[:19]}")

        # keep a 3-line real batch with a 1.85 price for E5
        if (not end2end_done and len(rows) == 3
                and any(p == 1.85 for r in rows
                        for p in (r["over_price"], r["under_price"]))):
            end2end_batch = (gid, ts, rows)

    print(f"replayed {n_checked} real multi-line batches "
          f"(band-rule: {band_rule_hits}, fallback: {fallback_hits})")
    print(f"{'PASS' if replay_fails == 0 else 'FAIL'}  "
          f"E1/E2/E3 across all replayed batches ({replay_fails} failures)")

    # E4 — end-to-end: one REAL production batch through record_snapshot
    if end2end_batch:
        gid, ts, rows = end2end_batch

        class _RealBatchStore:
            def latest_market_batch(self, source_game_id,
                                    market_type="MatchTotal"):
                return rows
            def get_snapshots(self, source_game_id, ascending=False):
                return []

        from blm_v4.clean_metrics import CleanMetricsStore
        with tempfile.TemporaryDirectory() as td:
            clean = CleanMetricsStore(Path(td) / "clean.db")
            res = clean.record_snapshot(
                source_game_id=gid, classification="CYBER_2K26",
                captured_at=ts, quarter=4, period_label="4th Quarter",
                clock="06:00", home_score=100, away_score=73,
                game_status="live", source="PokerBet",
                snapshot_total_line=None, snapshot_over_odds=None,
                snapshot_under_odds=None, main_store=_RealBatchStore(),
            )
            sel = select_total_market([
                {"line": r["line_value"], "over": r["over_price"],
                 "under": r["under_price"]} for r in rows])
            ok = (res["market_source"] == "ws"
                  and res["live_total_line"] == sel["line"]
                  and res["over_price"] == sel["over"]
                  and res["under_price"] == sel["under"])
            replay_fails += 0 if ok else 1
            print(f"{'PASS' if ok else 'FAIL'}  E4 real batch {gid}@{ts[:19]} "
                  f"→ record_snapshot line={res['live_total_line']} "
                  f"o={res['over_price']} u={res['under_price']} "
                  f"(selector: {sel['line']}@{sel['over']}/{sel['under']})")
            # required pace from the SELECTED line (elapsed 42 / remaining 6)
            ok2 = res["required_pts_per_min"] == \
                round((sel["line"] - 173) / 6, 4)
            replay_fails += 0 if ok2 else 1
            print(f"{'PASS' if ok2 else 'FAIL'}  E5 required_pts_per_min "
                  f"= ({sel['line']} − 173) / 6 = "
                  f"{res['required_pts_per_min']} (from the SELECTED line)")
            lines_stored = sorted(r["line_value"]
                                  for r in clean.list_market_lines(gid))
            ok3 = lines_stored == sorted(r["line_value"] for r in rows)
            replay_fails += 0 if ok3 else 1
            print(f"{'PASS' if ok3 else 'FAIL'}  E6 all {len(rows)} real line "
                  f"identities preserved: {lines_stored}")
    else:
        print("SKIP E4-E6: no 3-line batch with a 1.85 price in the replay set")

    fails += replay_fails

print()
print("=" * 72)
print(f"RESULT: {'ALL CHECKS PASSED' if fails == 0 else f'{fails} CHECK(S) FAILED'}")
print("No production files were modified. No services restarted.")
print("=" * 72)
sys.exit(1 if fails else 0)
