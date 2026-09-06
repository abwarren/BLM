"""BLM — STRICT WALK-FORWARD DIRECTIONAL VALIDATION (authorized slice).

READ-ONLY research instrument over ``blm_pokerbet.db``.  Implements the
authorized validation-slice protocol:

  1.  Terminal tautology (pct100) EXCLUDED from the primary view
      (retained as a separately-labelled diagnostic).
  2.  Game-level walk-forward: benchmark state for each test game is
      built from PREVIOUSLY COMPLETED games only; the current game's
      checkpoints never update the state used to evaluate it.
  3.  Daily chronological blocks (no pooled-only reporting).
  4.  LIVE vs STALE line split (MARKET_STALE_SECONDS = 300, the
      dashboard's existing freshness definition).  Never mixed.
  5.  Per-checkpoint 10..90% + aggregate.
  6.  MODEL OVER vs MODEL UNDER.
  7.  Baselines on the EXACT same test rows (Always OVER, Always UNDER,
      Market direction, M-F sign).
  8.  GAME is the primary statistical unit (checkpoint majority rule,
      documented below).
  9.  Stale-line hypothesis: does the signal survive LIVE-only?
  10. Retrospective deviation/reversion segmentation by the ALREADY
      RECORDED market_vs_fair magnitude (no new rules, no thresholds
      optimized — fixed analytical bands).
  11. No future information: rows are frozen at-or-before checkpoint by
      construction (scorecard.record_checkpoint_market); final total is
      used ONLY to score; closing line used ONLY for CLV notes.
  12. Primary success criteria -> final classification.
  13. NO MODEL CHANGES: production prediction logic untouched; this
      script is read-only (SQLite mode=ro).  No betting signal, no
      probability, no edge field, no threshold optimization.
  14. Required 15-item output (see print_report).

Position semantics (identical to blm_v4.scorecard._checkpoint_outcome):
  fair > market -> MODEL OVER        fair < market -> MODEL UNDER
  fair == market -> NO_EDGE (no-bet, excluded from denominators)
  actual == market -> PUSH (excluded from denominators)
  Win = position matches actual-vs-market outcome.

Game-level rule (section 8, stated explicitly): a game's position is the
PLURALITY direction (OVER/UNDER) among its scored ex-tautology (10-90)
market-bearing checkpoints; the settlement line is the MEDIAN of those
same checkpoints' live lines; the game WINS if the plurality position
matches actual-vs-settlement-line.  Ties, NO_EDGE majorities and pushes
are excluded from numerator and denominator.  Walk-forward ordering:
games sorted by started_date then source_game_id; the running benchmark
state is updated only AFTER a game is scored.

DEVIATION / Z (section 10): checkpoint_market has NO z_score or
market_trajectory_residual column (those live in other eras/layers).
The already-recorded signed ``market_vs_fair`` IS the deviation variable;
it is bucketed into fixed analytical bands (<=2, 2-5, 5-10, >10 pts,
absolute magnitude, direction preserved).

Usage:
    python3 scripts/walk_forward_validation.py [path/to/blm_pokerbet.db]
DB path resolution: argv[1] > $BLM_DB > ./blm_pokerbet.db.
Classification filter: CYBER_2K26 only by default (populations are never
mixed); pass --all-classifications to include BETUAL_NBA as its own
section.
"""

from __future__ import annotations

import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

MARKET_STALE_SECONDS = 300  # existing dashboard freshness definition (M009-M3)
TERMINAL_PCT = 100
PRIMARY_PCTS = (10, 20, 30, 40, 50, 60, 70, 80, 90)
DEVIATION_BANDS = ("<=", "2-5", "5-10", ">10")  # |market_vs_fair| pts

# Fixed success criteria (protocol section 12) — the classification does
# NOT require one pooled number > 60%.
CRITERIA = [
    "positive aggregate 10-90 accuracy",
    "positive in every chronological block with n >= 10 decisions",
    "LIVE-only result positive (n >= 30)",
    "not caused by terminal checkpoints (10-90 ≈ all-checkpoints)",
    "not caused by stale observations alone (LIVE not negative)",
    "not one league/period only (per-classification agreement)",
    "not solely an UNDER baseline effect (beats Always-UNDER baseline)",
    "survives game-level walk-forward evaluation (game-level positive)",
]


# ── freshness helpers (mirror blm_v4.scorecard semantics) ──────────────
def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _market_status(market_ts: Optional[str], checkpoint_ts: Optional[str]) -> str:
    if market_ts is None:
        return "MISSING"
    mt, ct = _parse_ts(market_ts), _parse_ts(checkpoint_ts)
    if mt is None or ct is None:
        return "MISSING"
    age = max(0.0, (ct - mt).total_seconds())  # clock skew clamps to 0
    return "LIVE" if age <= MARKET_STALE_SECONDS else "STALE"


def _position(fair: Optional[float], market: Optional[float]) -> Optional[str]:
    if fair is None or market is None:
        return None
    if fair > market:
        return "OVER"
    if fair < market:
        return "UNDER"
    return "NO_EDGE"


def _outcome_dir(actual: Optional[float], market: Optional[float]) -> Optional[str]:
    if actual is None or market is None:
        return None
    if actual > market:
        return "OVER"
    if actual < market:
        return "UNDER"
    return "PUSH"


def _binom_p_two_sided(wins: int, n: int, p0: float = 0.5) -> Optional[float]:
    """Exact two-sided binomial p-value (no scipy dependency)."""
    if n <= 0:
        return None
    def _pmf(k: int) -> float:
        return math.comb(n, k) * (p0 ** k) * ((1 - p0) ** (n - k))
    obs = _pmf(wins)
    return min(1.0, sum(_pmf(k) for k in range(n + 1) if _pmf(k) <= obs + 1e-12))


def _pct(part: int, n: int) -> Optional[float]:
    return round(100.0 * part / n, 1) if n else None


def _acc(wins: int, n: int) -> dict[str, Any]:
    return {"n": n, "wins": wins, "losses": n - wins,
            "accuracy_pct": _pct(wins, n)}


# ── data loading ───────────────────────────────────────────────────────
LOAD_SQL = """
SELECT cm.source_game_id, cm.classification, cm.checkpoint_pct,
       cm.checkpoint_timestamp, cm.blm_fair_value, cm.live_market_line,
       cm.market_timestamp, cm.market_vs_fair, cm.actual_final_total,
       cm.closing_line, g.home_team, g.away_team
FROM checkpoint_market cm
JOIN game_results r ON r.source_game_id = cm.source_game_id
JOIN games g ON g.source_game_id = cm.source_game_id
WHERE r.final_result_status = 'OK'
  AND NOT EXISTS (SELECT 1 FROM game_quality q
                  WHERE q.source_game_id = cm.source_game_id
                    AND q.status = 'INVALID')
"""


def resolve_db_path(argv: list[str]) -> str:
    if len(argv) > 1:
        return argv[1]
    env = os.environ.get("BLM_DB")
    if env:
        return env
    return "blm_pokerbet.db"


def load_rows(db_path: str, classification: Optional[str]) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql, params = LOAD_SQL, []
        if classification:
            sql += " AND cm.classification = ?"
            params.append(classification)
        rows = [dict(r) for r in conn.execute(sql, params)]
        # game_start = first snapshot captured_at (MIN over snapshots) —
        # the tautology guard needs the TRUE first observation, and
        # started_date needs the same instant.
        starts = dict(conn.execute(
            """SELECT g.source_game_id, MIN(s.captured_at)
               FROM games g JOIN snapshots s ON s.game_id = g.id
               GROUP BY g.source_game_id""").fetchall())
        for r in rows:
            start = starts.get(r["source_game_id"])
            r["game_start"] = start
            r["started_date"] = (start or "")[:10]
            # terminal-tautology flag: row frozen at the game's first
            # observed instant (projection sees an empty history ->
            # fair ≈ market by construction, ~100% by tautology).
            r["is_tautology"] = bool(
                start and r["checkpoint_timestamp"]
                and str(r["checkpoint_timestamp"])[:16] <= str(start)[:16])
        return rows
    finally:
        conn.close()


def scored(rows: list[dict]) -> list[dict]:
    """Market-bearing, decided rows (excludes NO_EDGE positions and PUSH
    outcomes from denominators; they are counted separately)."""
    out = []
    for r in rows:
        pos = _position(r["blm_fair_value"], r["live_market_line"])
        outcome = _outcome_dir(r["actual_final_total"], r["live_market_line"])
        if pos in (None, "NO_EDGE") or outcome in (None, "PUSH"):
            r = {**r, "position": pos, "outcome_dir": outcome,
                 "win": None}
        else:
            r = {**r, "position": pos, "outcome_dir": outcome,
                 "win": 1 if pos == outcome else 0}
        out.append(r)
    return out


def primary(rows: list[dict]) -> list[dict]:
    """Primary validation view: ex-tautology, checkpoints 10-90, decided."""
    return [r for r in scored(rows)
            if r["checkpoint_pct"] in PRIMARY_PCTS
            and not r["is_tautology"] and r["win"] is not None]


# ── walk-forward benchmark state (protocol section 2) ──────────────────
def walk_forward_state(rows: list[dict]) -> dict[str, dict]:
    """For each game (chronological), the benchmark state built from
    STRICTLY PRIOR completed games' ex-tautology 10-90 rows.

    The running state is the mean signed market-vs-fair bias of prior
    games (a control): v4-pace-1 has NO cross-game learning — the
    projection is a pure function of the current game's snapshots — so
    this state CANNOT alter predictions.  It is reported to prove the
    walk-forward construction and to show any population drift.
    """
    games: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["checkpoint_pct"] in PRIMARY_PCTS and not r["is_tautology"] \
                and r["live_market_line"] is not None \
                and r["blm_fair_value"] is not None:
            games[r["source_game_id"]].append(r)
    ordered = sorted(games.items(),
                     key=lambda kv: (kv[1][0]["started_date"], kv[0]))
    state: dict[str, dict] = {}
    mf_sum, mf_n = 0.0, 0
    for gid, grows in ordered:
        state[gid] = {
            "prior_games": len(state),
            "prior_rows": mf_n,
            "prior_mean_mvf": round(mf_sum / mf_n, 3) if mf_n else None,
        }
        for r in grows:  # update ONLY after the game's state was captured
            mf_sum += r["market_vs_fair"] or 0.0
            mf_n += 1
    return state


# ── reporting blocks ───────────────────────────────────────────────────
def block_report(rows: list[dict]) -> list[dict]:
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_date[r["started_date"]].append(r)
    out = []
    for date in sorted(by_date):
        d = by_date[date]
        gids = {r["source_game_id"] for r in d}
        out.append({
            "date": date,
            "games": len(gids),
            "decisions": len(d),
            "o_win": sum(1 for r in d if r["position"] == "OVER" and r["win"]),
            "o_loss": sum(1 for r in d if r["position"] == "OVER" and not r["win"]),
            "u_win": sum(1 for r in d if r["position"] == "UNDER" and r["win"]),
            "u_loss": sum(1 for r in d if r["position"] == "UNDER" and not r["win"]),
            **_acc(sum(r["win"] for r in d), len(d)),
        })
    return out


def checkpoint_report(rows: list[dict]) -> list[dict]:
    out = []
    for pct in PRIMARY_PCTS:
        d = [r for r in rows if r["checkpoint_pct"] == pct]
        out.append({
            "checkpoint_pct": pct,
            "n": len(d),
            "o_win": sum(1 for r in d if r["position"] == "OVER" and r["win"]),
            "o_loss": sum(1 for r in d if r["position"] == "OVER" and not r["win"]),
            "u_win": sum(1 for r in d if r["position"] == "UNDER" and r["win"]),
            "u_loss": sum(1 for r in d if r["position"] == "UNDER" and not r["win"]),
            **_acc(sum(r["win"] for r in d), len(d)),
        })
    return out


def direction_report(rows: list[dict]) -> dict[str, dict]:
    out = {}
    for pos in ("OVER", "UNDER"):
        d = [r for r in rows if r["position"] == pos]
        out[f"model_{pos.lower()}"] = _acc(sum(r["win"] for r in d), len(d))
    return out


def baseline_report(rows: list[dict]) -> dict[str, dict]:
    """Baselines on the EXACT same rows (protocol section 7)."""
    n = len(rows)
    over = sum(1 for r in rows if r["outcome_dir"] == "OVER")
    under = sum(1 for r in rows if r["outcome_dir"] == "UNDER")
    market_dir = sum(1 for r in rows
                     if (r["live_market_line"] or 0) != (r["blm_fair_value"] or 0)
                     and _outcome_dir(r["actual_final_total"],
                                      r["live_market_line"])
                     == _outcome_dir(r["live_market_line"],
                                     r["blm_fair_value"]))
    mf_sign = sum(1 for r in rows
                  if r["market_vs_fair"] is not None
                  and (r["market_vs_fair"] < 0) == (r["win"] == 1))
    return {
        "always_over": _acc(over, n),
        "always_under": _acc(under, n),
        "market_direction": _acc(market_dir, n),
        "mf_sign_direction": _acc(mf_sign, n),
        "blm_model": _acc(sum(r["win"] for r in rows), n),
    }


def freshness_split(rows: list[dict]) -> dict[str, dict]:
    live = [r for r in rows
            if _market_status(r["market_timestamp"], r["checkpoint_timestamp"]) == "LIVE"]
    stale = [r for r in rows
             if _market_status(r["market_timestamp"], r["checkpoint_timestamp"]) == "STALE"]
    return {
        "live": _acc(sum(r["win"] for r in live), len(live)),
        "stale": _acc(sum(r["win"] for r in stale), len(stale)),
        "missing": sum(1 for r in rows
                       if _market_status(r["market_timestamp"],
                                         r["checkpoint_timestamp"]) == "MISSING"),
    }


def game_level(rows: list[dict]) -> dict[str, Any]:
    """Primary statistical unit = GAME (protocol section 8).

    Rule: plurality position of the game's decided 10-90 checkpoints;
    settlement line = MEDIAN of those checkpoints' live lines; win =
    plurality matches actual-vs-median-line.  Ties / NO_EDGE majority /
    push excluded from numerator and denominator.
    """
    games: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        games[r["source_game_id"]].append(r)
    wins = losses = excluded = 0
    details = []
    for gid, grows in sorted(games.items(),
                             key=lambda kv: (kv[1][0]["started_date"], kv[0])):
        over = sum(1 for r in grows if r["position"] == "OVER")
        under = sum(1 for r in grows if r["position"] == "UNDER")
        lines = sorted(r["live_market_line"] for r in grows)
        median_line = lines[len(lines) // 2] if lines else None
        actual = grows[0]["actual_final_total"]
        outcome = _outcome_dir(actual, median_line)
        if over == under or outcome in (None, "PUSH"):
            excluded += 1
            continue
        pos = "OVER" if over > under else "UNDER"
        win = 1 if pos == outcome else 0
        wins += win
        losses += 1 - win
        details.append({"game": gid, "date": grows[0]["started_date"],
                        "position": pos, "outcome": outcome, "win": win,
                        "n_checkpoints": len(grows)})
    return {**_acc(wins, wins + losses), "games_evaluated": len(games),
            "games_excluded": excluded,
            "p_value_binomial_two_sided": _binom_p_two_sided(wins, wins + losses)}


def deviation_report(rows: list[dict]) -> list[dict]:
    """Retrospective segmentation by the ALREADY-RECORDED market_vs_fair
    magnitude (protocol section 10) — fixed analytical bands, no new
    rules, no threshold optimization."""
    def band(mvf: Optional[float]) -> Optional[str]:
        if mvf is None:
            return None
        a = abs(mvf)
        if a <= 2:
            return "<="
        if a <= 5:
            return "2-5"
        if a <= 10:
            return "5-10"
        return ">10"
    out = []
    for b in DEVIATION_BANDS:
        d = [r for r in rows if band(r["market_vs_fair"]) == b]
        if not d:
            out.append({"band": b, "n": 0})
            continue
        out.append({
            "band": b,
            "n": len(d),
            "model_over_n": sum(1 for r in d if r["position"] == "OVER"),
            "model_under_n": sum(1 for r in d if r["position"] == "UNDER"),
            **_acc(sum(r["win"] for r in d), len(d)),
            "avg_signed_mvf": round(sum(r["market_vs_fair"] for r in d) / len(d), 2),
        })
    return out


def classification(blocks: list[dict], agg: dict, fresh: dict,
                   game_res: dict, baselines: dict,
                   diag_terminal: dict) -> dict[str, Any]:
    """Apply the fixed primary success criteria (protocol section 12)."""
    results = {}
    agg_acc = agg.get("accuracy_pct")
    results["aggregate_positive"] = agg_acc is not None and agg_acc > 50.0
    big_blocks = [b for b in blocks if b["decisions"] >= 10]
    results["blocks_positive_n10"] = all(
        b["accuracy_pct"] > 50.0 for b in big_blocks) if big_blocks else None
    live_acc = fresh["live"].get("accuracy_pct")
    results["live_only_positive_n30"] = (
        live_acc is not None and live_acc > 50.0) if fresh["live"]["n"] >= 30 else None
    results["terminal_not_primary_cause"] = (
        diag_terminal["n"] > 0 and agg_acc is not None)
    results["live_not_negative"] = live_acc is None or live_acc >= 50.0
    under_base = baselines["always_under"].get("accuracy_pct") or 0.0
    results["beats_under_baseline"] = agg_acc is not None and agg_acc > under_base
    game_acc = game_res.get("accuracy_pct")
    results["game_level_positive"] = game_acc is not None and game_acc > 50.0
    passed = [k for k, v in results.items() if v is True]
    failed = [k for k, v in results.items() if v is False]
    if len(failed) == 0 and len(passed) == len(results) \
            and results.get("live_only_positive_n30") is not None:
        verdict = "VALIDATED OUT-OF-SAMPLE SIGNAL"
    elif failed:
        verdict = "UNVALIDATED / INSUFFICIENT EVIDENCE"
    else:
        verdict = "UNVALIDATED / INSUFFICIENT EVIDENCE"
    return {"criteria_results": results, "passed": passed,
            "failed": failed, "classification": verdict}


# ── report ─────────────────────────────────────────────────────────────
def print_report(data: dict[str, Any]) -> None:
    print("=" * 64)
    print("BLM STRICT WALK-FORWARD DIRECTIONAL VALIDATION — REPORT")
    print("=" * 64)
    print(f"db: {data['db']}")
    print(f"classification filter: {data['classification_filter']}")
    print(f"model versions in population: {data['model_versions']}")
    print()

    print("1. WALK-FORWARD PROTOCOL")
    print("   - test unit: GAME (chronological by started_date, game_id)")
    print("   - benchmark state: PRIOR completed games only (control:")
    print("     v4-pace-1 has no cross-game learning; state cannot alter")
    print("     predictions — reported to prove the construction)")
    print("   - primary rows: checkpoints 10-90, LIVE+STALE reported")
    print("     separately, terminal pct100 excluded (diagnostic only)")
    print()

    print("2. NUMBER OF TEST GAMES:", data["n_games"])
    print("3. NUMBER OF TEST CHECKPOINTS (primary decided rows):",
          data["aggregate"]["n"])
    excluded = data["excluded_rows"]
    print(f"   excluded from denominators: {excluded['no_edge']} NO_EDGE"
          f" (fair==market), {excluded['push']} PUSH"
          f" (actual==line), {excluded['tautology']} terminal-tautology"
          f" 10-90 rows, {excluded['no_market']} no-market rows")
    print()

    print("4. LIVE-ONLY RESULT:", data["freshness"]["live"])
    print("5. STALE-ONLY RESULT:", data["freshness"]["stale"])
    print()

    print("6. 10-90 AGGREGATE:", data["aggregate"])
    print(f"   binomial p (two-sided, vs 50%): "
          f"{data['aggregate_p_value']}")
    print()

    print("7. ACCURACY BY CHECKPOINT (10-90)")
    print(f"   {'pct':>4} {'n':>5} {'O win':>6} {'O loss':>7}"
          f" {'U win':>6} {'U loss':>7} {'acc%':>6}")
    for c in data["by_checkpoint"]:
        print(f"   {c['checkpoint_pct']:>4} {c['n']:>5} {c['o_win']:>6}"
              f" {c['o_loss']:>7} {c['u_win']:>6} {c['u_loss']:>7}"
              f" {str(c['accuracy_pct']):>6}")
    print()

    print("8. ACCURACY BY DIRECTION:", data["by_direction"])
    print()

    print("9. ACCURACY BY CHRONOLOGICAL BLOCK (daily)")
    print(f"   {'DATE':>10} {'GAMES':>6} {'DEC':>5} {'O win':>6}"
          f" {'O loss':>7} {'U win':>6} {'U loss':>7} {'ACC%':>6}")
    for b in data["by_block"]:
        print(f"   {b['date']:>10} {b['games']:>6} {b['decisions']:>5}"
              f" {b['o_win']:>6} {b['o_loss']:>7} {b['u_win']:>6}"
              f" {b['u_loss']:>7} {str(b['accuracy_pct']):>6}")
    print()

    print("10. GAME-LEVEL RESULT (primary unit):", data["game_level"])
    print()

    print("11. BASELINES (exact same rows):")
    for k, v in data["baselines"].items():
        print(f"    {k:<20} {v}")
    print()

    print("12. DEVIATION / Z RELATIONSHIP (|market_vs_fair| bands)")
    for b in data["deviation_bands"]:
        print(f"    {b}")
    print("    NOTE: checkpoint_market carries no z_score /")
    print("    market_trajectory_residual columns; the recorded signed")
    print("    market_vs_fair IS the deviation variable. No z report")
    print("    possible without new instrumentation (out of scope).")
    print()

    print("13. UNDER-SELECTION EFFECT:", data["under_effect"])
    print("14. STALE-LINE EXPLANATION:", data["stale_effect"])
    print()

    print("15. FINAL CLASSIFICATION:", data["verdict"]["classification"])
    for k, v in data["verdict"]["criteria_results"].items():
        print(f"    [{'PASS' if v is True else 'FAIL' if v is False else 'N/A'}] {k}")
    print()

    print("DIAGNOSTIC (separately labelled, NOT primary):")
    print("   terminal pct100 rows:", data["terminal_diagnostic"])
    print("   all-checkpoints incl. terminal:", data["all_checkpoints_diag"])
    print("   CLV note: closing line present on",
          data["clv_rows"], "primary-row games' rows; CLV is used for")
    print("   separate CLV analysis only and never enters scoring.")
    if data.get("classification_breakdown"):
        print()
        print("PER-CLASSIFICATION (populations never mixed):")
        for c, v in data["classification_breakdown"].items():
            print(f"   {c}: {v}")


def run_classification(rows_all: list[dict], name: str) -> dict:
    pr = primary(rows_all)
    agg = _acc(sum(r["win"] for r in pr), len(pr))
    return {"aggregate_10_90": agg,
            "live": freshness_split(pr)["live"],
            "games": len({r["source_game_id"] for r in pr})}


def main() -> int:
    db_path = resolve_db_path(sys.argv)
    include_all = "--all-classifications" in sys.argv
    if not os.path.exists(db_path):
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        print("The validation requires the production blm_pokerbet.db",
              file=sys.stderr)
        print("(checkpoint_market + games + snapshots + game_results).",
              file=sys.stderr)
        return 2

    rows_all = load_rows(db_path, None)
    if not rows_all:
        print("ERROR: no eligible checkpoint_market rows (empty population).",
              file=sys.stderr)
        return 2

    model_versions = sorted({r.get("model_version", "?") for r in rows_all}
                            | {"v4-pace-1 (frozen rows, see schema)"})
    cyber = [r for r in rows_all if r["classification"] == "CYBER_2K26"]
    pop = cyber if cyber else rows_all

    pr = primary(pop)
    state = walk_forward_state(pop)
    blocks = block_report(pr)
    agg = _acc(sum(r["win"] for r in pr), len(pr))
    fresh = freshness_split(pr)
    game_res = game_level(pr)
    baselines = baseline_report(pr)

    # terminal-tautology handling (protocol section 1)
    term = [r for r in scored(pop) if r["checkpoint_pct"] == TERMINAL_PCT
            and r["win"] is not None]
    all_diag = [r for r in scored(pop)
                if r["checkpoint_pct"] in PRIMARY_PCTS and r["win"] is not None]
    taut_1090 = [r for r in pr if r["is_tautology"]]

    # protocol section 13 answers
    under_dec = [r for r in pr if r["position"] == "UNDER"]
    over_dec = [r for r in pr if r["position"] == "OVER"]
    under_base = baselines["always_under"]["accuracy_pct"]
    under_effect = {
        "model_under_n": len(under_dec), "model_over_n": len(over_dec),
        "always_under_accuracy_pct": under_base,
        "model_accuracy_pct": agg["accuracy_pct"],
        "two_sided": (len(under_dec) > 0 and len(over_dec) > 0
                      and agg["accuracy_pct"] is not None),
    }
    stale_effect = {
        "live_accuracy_pct": fresh["live"]["accuracy_pct"],
        "stale_accuracy_pct": fresh["stale"]["accuracy_pct"],
        "survives_live_only": (fresh["live"]["accuracy_pct"] is not None
                               and fresh["live"]["accuracy_pct"] > 50.0),
        "stale_only_carries_it": (
            (fresh["live"]["accuracy_pct"] is None
             or fresh["live"]["accuracy_pct"] <= 50.0)
            and (fresh["stale"]["accuracy_pct"] or 0) > 50.0),
    }
    clv_rows = sum(1 for r in pr if r["closing_line"] is not None)

    cls = classification(blocks, agg, fresh, game_res, baselines,
                         _acc(sum(r["win"] for r in term), len(term)))

    report = {
        "db": db_path,
        "verdict": cls,
        "classification_filter": "CYBER_2K26" if cyber else "ALL",
        "model_versions": model_versions,
        "n_games": len({r["source_game_id"] for r in pr}),
        "aggregate": agg,
        "aggregate_p_value": _binom_p_two_sided(agg["wins"], agg["n"]),
        "excluded_rows": {
            "no_edge": sum(1 for r in scored(pop)
                           if r["checkpoint_pct"] in PRIMARY_PCTS
                           and not r["is_tautology"] and r["position"] == "NO_EDGE"),
            "push": sum(1 for r in scored(pop)
                        if r["checkpoint_pct"] in PRIMARY_PCTS
                        and not r["is_tautology"] and r["outcome_dir"] == "PUSH"),
            "tautology": len(taut_1090),
            "no_market": sum(1 for r in pop
                             if r["checkpoint_pct"] in PRIMARY_PCTS
                             and r["live_market_line"] is None),
        },
        "freshness": fresh,
        "by_checkpoint": checkpoint_report(pr),
        "by_direction": direction_report(pr),
        "by_block": blocks,
        "game_level": game_res,
        "baselines": baselines,
        "deviation_bands": deviation_report(pr),
        "under_effect": under_effect,
        "stale_effect": stale_effect,
        "terminal_diagnostic": _acc(sum(r["win"] for r in term), len(term)),
        "all_checkpoints_diag": _acc(sum(r["win"] for r in all_diag),
                                     len(all_diag)),
        "clv_rows": clv_rows,
        "walk_forward_state_sample": {
            gid: state[gid] for gid in list(state)[:3]},
        "classification_breakdown": (
            {c: run_classification([r for r in rows_all
                                    if r["classification"] == c], c)
             for c in sorted({r["classification"] for r in rows_all})}
            if include_all else {}),
    }
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
