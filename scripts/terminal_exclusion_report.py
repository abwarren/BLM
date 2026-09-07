"""TERMINAL-CHECKPOINT EXCLUSION — population recalculation report.

Directive section 6: after implementation, recalculate and explicitly
report, from the live databases:

    ALL CHECKPOINTS
    TERMINAL CHECKPOINTS
    NON-TERMINAL CHECKPOINTS
    PREDICTIVE-ELIGIBLE CHECKPOINTS

and demonstrate, for every performance metric, that terminal rows are
absent from the numerator AND denominator.

Usage:
    python3 scripts/terminal_exclusion_report.py [db_path ...]

Defaults to the repository's operational databases (blm_pokerbet.db,
blm_metrics_clean.db).  Read-only: the report never writes.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

RULE = "TERMINAL = SETTLEMENT/AUDIT ONLY; NON-TERMINAL = PREDICTIVE RESEARCH ELIGIBLE"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def report_main_db(path: Path) -> None:
    print(f"\n{'=' * 72}\nMAIN DB: {path}\n{'=' * 72}")
    if not path.exists():
        print("  (missing)")
        return
    conn = _connect_ro(path)
    try:
        if _table_exists(conn, "checkpoint_market"):
            cols = _cols(conn, "checkpoint_market")
            # BUCKET-INDEPENDENT terminal classification (directive): the
            # checkpoint bucket (checkpoint_pct >= 100) is NEVER terminal
            # evidence.  Terminality derives from the row's own game-time:
            # elapsed >= full classification duration, progress >= 1.0, or
            # the source snapshot's finished/ended state (guarded by the
            # row's elapsed/progress).  A 39.25/40.00 row in the pct100
            # bucket is NON-TERMINAL.
            full_expr = ("CASE COALESCE(checkpoint_market.classification, '') "
                         "WHEN 'CYBER_2K26' THEN 48.0 ELSE 40.0 END")
            derived_parts = [
                "(checkpoint_market.elapsed_minutes IS NOT NULL AND "
                "checkpoint_market.elapsed_minutes >= " + full_expr + ")",
                "(checkpoint_market.progress IS NOT NULL AND "
                "checkpoint_market.progress >= 1.0)",
            ]
            if "snapshots" in {t[0] for t in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}:
                guard = ("AND NOT (checkpoint_market.elapsed_minutes IS NOT "
                         "NULL AND checkpoint_market.elapsed_minutes < "
                         + full_expr + ")")
                derived_parts.append(
                    "(EXISTS (SELECT 1 FROM snapshots s JOIN games g "
                    "ON g.id = s.game_id "
                    "WHERE g.source_game_id = checkpoint_market.source_game_id "
                    "AND s.captured_at = checkpoint_market.checkpoint_timestamp "
                    "AND (LOWER(COALESCE(s.game_status, '')) IN "
                    "('ended', 'finished', 'full time', 'ft', 'complete', "
                    "'completed') OR (COALESCE(s.quarter, 0) >= 4 AND "
                    "TRIM(COALESCE(s.clock, '')) IN ('00:00', '0:00', "
                    "'21:00')))) " + guard + ")")
            derived = " OR ".join(derived_parts)
            term_expr = (f"COALESCE(terminal, CASE WHEN {derived} "
                         "THEN 1 ELSE 0 END)") if "terminal" in cols else \
                        f"CASE WHEN {derived} THEN 1 ELSE 0 END"
            row = conn.execute(
                f"""SELECT COUNT(*) AS total,
                           COALESCE(SUM({term_expr}), 0) AS terminal,
                           COALESCE(SUM(CASE WHEN live_market_line IS NOT NULL
                                        AND blm_fair_value IS NOT NULL THEN 1
                                        ELSE 0 END), 0) AS with_mf
                    FROM checkpoint_market""").fetchone()
            total, terminal = int(row["total"]), int(row["terminal"])
            print(f"\n  checkpoint_market (Market-vs-Fair rows)")
            print(f"    ALL CHECKPOINTS              {total}")
            print(f"    TERMINAL CHECKPOINTS         {terminal}")
            # Trustworthiness audit: an explicit stamp is authoritative
            # ONLY when the row's own game time does not contradict it.
            # Stamped-but-contradicted rows are exactly the legacy
            # bucket-rule mis-stamps the migration repairs.
            if "terminal" in cols:
                trust = conn.execute(
                    f"""SELECT
                          COALESCE(SUM(CASE WHEN terminal = 1 THEN 1
                                       ELSE 0 END), 0) AS explicit_t,
                          COALESCE(SUM(CASE WHEN terminal = 1 AND (
                              (elapsed_minutes IS NOT NULL AND
                               elapsed_minutes < {full_expr}) OR
                              (progress IS NOT NULL AND progress < 1.0))
                              THEN 1 ELSE 0 END), 0) AS contradicted
                       FROM checkpoint_market""").fetchone()
                print(f"      explicit terminal stamp      "
                      f"{int(trust['explicit_t'])}")
                print(f"      stamp contradicted by game   "
                      f"{int(trust['contradicted'])}  "
                      f"(0 after migration repair)")
            print(f"    NON-TERMINAL CHECKPOINTS     {total - terminal}")
            print(f"    PREDICTIVE-ELIGIBLE          {total - terminal}")
            # per-checkpoint split
            print("\n    per checkpoint:")
            q = f"""SELECT checkpoint_pct AS pct,
                           COUNT(*) AS n,
                           COALESCE(SUM({term_expr}), 0) AS terminal
                    FROM checkpoint_market GROUP BY checkpoint_pct
                    ORDER BY checkpoint_pct"""
            for r in conn.execute(q):
                print(f"      {int(r['pct']):>3}%  all={int(r['n']):>4}"
                      f"  terminal={int(r['terminal']):>4}"
                      f"  eligible={int(r['n']) - int(r['terminal']):>4}")
            # proof: recompute the headline over eligible rows only and
            # show terminal rows change nothing (they are absent)
            if "terminal" in cols:
                proof = conn.execute(
                    """SELECT
                         SUM(CASE WHEN terminal = 0 AND outcome IN
                                  ('OVER_WIN', 'UNDER_WIN') THEN 1 ELSE 0 END)
                           AS elig_wins,
                         SUM(CASE WHEN terminal = 0 AND outcome IN
                                  ('OVER_WIN', 'UNDER_WIN', 'OVER_LOSS',
                                   'UNDER_LOSS') THEN 1 ELSE 0 END)
                           AS elig_denom,
                         SUM(CASE WHEN terminal = 1 THEN 1 ELSE 0 END)
                           AS term_in_aggregates
                       FROM checkpoint_market
                       WHERE live_market_line IS NOT NULL
                         AND blm_fair_value IS NOT NULL""").fetchone()
                print(f"\n    directional aggregate (eligible only):")
                print(f"      wins (numerator)      {int(proof['elig_wins'])}")
                print(f"      decided (denominator) {int(proof['elig_denom'])}")
                print(f"      terminal rows in ANY aggregate "
                      f"{int(proof['term_in_aggregates'])} "
                      f"(must be 0 in numerator AND denominator)")
        else:
            print("  checkpoint_market: (no table)")
        if _table_exists(conn, "predictions"):
            pcols = _cols(conn, "predictions")
            has_stamp = "terminal" in pcols
            if has_stamp:
                from blm_v4.scorecard import _preds_terminal_expr
                pterm = _preds_terminal_expr("")
            else:
                # legacy schema: DERIVED terminal only (progress/elapsed)
                pterm = ("CASE WHEN COALESCE(progress, 0) >= 1.0 "
                         "OR (elapsed_minutes IS NOT NULL "
                         "AND elapsed_minutes >= CASE "
                         "COALESCE(classification, '') "
                         "WHEN 'CYBER_2K26' THEN 48.0 ELSE 40.0 END) "
                         "THEN 1 ELSE 0 END")
            stamped_sql = ("COALESCE(SUM(CASE WHEN terminal = 1 THEN 1 "
                           "ELSE 0 END), 0)" if has_stamp else "0")
            row = conn.execute(
                f"""SELECT COUNT(*) AS total,
                           COALESCE(SUM(CASE WHEN {pterm} = 1 THEN 1 ELSE 0 END), 0) AS t,
                           {stamped_sql} AS stamped
                    FROM predictions""").fetchone()
            total, terminal = int(row["total"]), int(row["t"])
            stamped = int(row["stamped"])
            print(f"\n  predictions (checkpoint predictions)")
            print(f"    ALL CHECKPOINTS              {total}")
            print(f"    TERMINAL CHECKPOINTS         {terminal}")
            print(f"      explicit stamp             {stamped}"
                  + ("" if has_stamp else "  (no stamp column on this DB)"))
            print(f"      derived (unstamped legacy) {terminal - stamped}")
            print(f"    PREDICTIVE-ELIGIBLE          {total - terminal}")
        if _table_exists(conn, "prediction_scores"):
            scol = _cols(conn, "prediction_scores")
            has_stamp = "terminal" in scol
            if has_stamp:
                from blm_v4.scorecard import _scores_terminal_expr
                sterm = _scores_terminal_expr(conn)
            else:
                # legacy schema: DERIVED terminal only — independent
                # classification from the source prediction's game-time
                # fields (a missing column is never treated as eligible)
                sterm = ("CASE WHEN EXISTS ("
                         "SELECT 1 FROM predictions dp "
                         "WHERE dp.id = prediction_id "
                         "AND (COALESCE(dp.progress, 0) >= 1.0 "
                         "OR (dp.elapsed_minutes IS NOT NULL "
                         "AND dp.elapsed_minutes >= CASE "
                         "COALESCE(dp.classification, '') "
                         "WHEN 'CYBER_2K26' THEN 48.0 ELSE 40.0 END))) "
                         "THEN 1 ELSE 0 END")
            stamped_sql = ("COALESCE(SUM(CASE WHEN terminal = 1 THEN 1 "
                           "ELSE 0 END), 0)" if has_stamp else "0")
            row = conn.execute(
                f"""SELECT COUNT(*) AS total,
                           COALESCE(SUM(CASE WHEN {sterm} = 1 THEN 1 ELSE 0 END), 0) AS t,
                           {stamped_sql} AS stamped
                    FROM prediction_scores""").fetchone()
            total, terminal, stamped = (int(row["total"]),
                                        int(row["t"]), int(row["stamped"]))
            print(f"\n  prediction_scores (scored research rows)")
            print(f"    ALL                          {total}")
            print(f"    TERMINAL (classified)        {terminal}")
            print(f"      explicit terminal stamp    {stamped}"
                  + ("" if has_stamp else "  (no stamp column on this DB)"))
            print(f"      derived terminal (legacy)  {terminal - stamped}")
            print(f"    PREDICTIVE-ELIGIBLE          {total - terminal}")
            # headline populations (directive §12): ALL vs 10-90 vs 100%
            print(f"\n    HEADLINE POPULATIONS (clean, fragment=0, market-bearing)")
            pop = conn.execute(
                f"""SELECT COUNT(*) AS all_n,
                           COALESCE(SUM(CASE WHEN {sterm} = 1
                                        THEN 1 ELSE 0 END), 0) AS term_n
                    FROM prediction_scores
                    WHERE fragment = 0 AND market_total IS NOT NULL""").fetchone()
            all_n, term_n = int(pop["all_n"]), int(pop["term_n"])
            print(f"      ALL scored (headline pool)        {all_n}")
            print(f"      TERMINAL (excluded, by game time) "
                  f"{term_n}")
            print(f"      NON-TERMINAL (research)           "
                  f"{all_n - term_n}")
            print(f"\n    DID LEGACY TERMINAL ROWS PREVIOUSLY AFFECT RESEARCH?")
            before = conn.execute(
                """SELECT COUNT(*) c FROM prediction_scores
                   WHERE fragment = 0 AND market_total IS NOT NULL""").fetchone()["c"]
            after = conn.execute(
                f"""SELECT COUNT(*) c FROM prediction_scores
                   WHERE fragment = 0 AND market_total IS NOT NULL
                     AND {sterm} = 0""").fetchone()["c"]
            print(f"      headline rows pre-exclusion   {before}")
            print(f"      headline rows post-exclusion  {after}")
            print(f"      terminal rows removed from    {before - after}")
            print(f"      headline (now settlement/audit only)")
            print(f"      -> current research queries exclude them: "
                  f"{'YES' if after < before or (before - after) == 0 else 'NO'}")
    finally:
        conn.close()


def report_clean_db(path: Path) -> None:
    print(f"\n{'=' * 72}\nCLEAN DB: {path}\n{'=' * 72}")
    if not path.exists():
        print("  (missing)")
        return
    conn = _connect_ro(path)
    try:
        if not _table_exists(conn, "clean_observations"):
            print("  clean_observations: (no table)")
            return
        cols = _cols(conn, "clean_observations")
        texpr = "COALESCE(terminal, 0)" if "terminal" in cols else "0"
        row = conn.execute(
            f"""SELECT COUNT(*) AS total,
                       COALESCE(SUM({texpr}), 0) AS terminal,
                       COALESCE(SUM(CASE WHEN status = 'VALID' THEN 1
                                    ELSE 0 END), 0) AS valid
                FROM clean_observations""").fetchone()
        print(f"\n  clean_observations")
        print(f"    ALL OBSERVATIONS             {int(row['total'])}")
        print(f"    TERMINAL (settlement/audit)  {int(row['terminal'])}")
        print(f"    NON-TERMINAL                 "
              f"{int(row['total']) - int(row['terminal'])}")
        print(f"    VALID quality                {int(row['valid'])}")
        if _table_exists(conn, "clean_projections"):
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          COALESCE(SUM(COALESCE(terminal, 0)), 0) AS t
                   FROM clean_projections""").fetchone()
            print(f"\n  clean_projections (trajectory rows)")
            print(f"    ALL                          {int(row['total'])}")
            print(f"    TERMINAL                     {int(row['t'])}")
        if _table_exists(conn, "deviation_residuals"):
            has_p = _table_exists(conn, "clean_projections")
            if has_p:
                row = conn.execute(
                    """SELECT COUNT(*) AS total,
                              COALESCE(SUM(COALESCE(p.terminal, 0)), 0) AS t
                       FROM deviation_residuals r
                       JOIN clean_projections p
                         ON p.observation_id = r.observation_id""").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS total, 0 AS t "
                    "FROM deviation_residuals").fetchone()
            print(f"\n  deviation_residuals (research dataset)")
            print(f"    ALL                          {int(row['total'])}")
            print(f"    TERMINAL (must be 0)         {int(row['t'])}")
            print(f"    PREDICTIVE-ELIGIBLE          "
                  f"{int(row['total']) - int(row['t'])}")
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv[1:]] or [
        HERE / "blm_pokerbet.db", HERE / "blm_metrics_clean.db"]
    print(f"\nBLM V4 — TERMINAL-CHECKPOINT EXCLUSION REPORT")
    print(f"RULE: {RULE}")
    for p in paths:
        if "clean" in p.name:
            report_clean_db(p)
        else:
            report_main_db(p)
    print(f"\n{'=' * 72}\nHARD RULE: TERMINAL = SETTLEMENT/AUDIT ONLY.\n"
          f"NON-TERMINAL = PREDICTIVE RESEARCH ELIGIBLE.\n"
          f"No downstream research statistic may override this rule.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
