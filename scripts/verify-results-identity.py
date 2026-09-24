#!/usr/bin/env python3
"""
Does the results page actually CONTAIN the requested game? (2026-09-24)

The page renders a list (div.results-block-bc) and carries NO game id in
the DOM (the only occurrences of the id are analytics trackers echoing the
URL).  So a page result is only trustworthy when a ROW matches the record.

This script measures that, live: for the N most recent games holding a
result_source='RESULTS_PAGE' OK row it fetches each game's results page,
extracts every row structurally, and reports whether any row matches the
record's teams (virtual-marker-insensitive) — with the matched row's
scores/side and the fixture date/time.

READ-ONLY: production is opened mode=ro + query_only (proven by a refused
write); the fetcher only reads a public page.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path("/home/ubuntu/BLM")
sys.path.insert(0, str(ROOT))

URL = "https://www.pokerbet.co.za/en/sports/results?game={gid}"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

ROW_JS = r"""
() => {
  const txt = (el) => el ? (el.textContent || '').trim() : null;
  const rows = [];
  document.querySelectorAll('div.results-block-bc').forEach(block => {
    const teams = [];
    block.querySelectorAll('div.results-teams-bc').forEach(t => {
      teams.push({name: txt(t.querySelector('p.results-teams-name-bc')),
                  score: txt(t.querySelector('span.results-teams-score-bc'))});
    });
    const times = [];
    block.querySelectorAll('time.results-footer-date-bc').forEach(
      t => times.push(txt(t)));
    const wrap = block.closest('div.competition-wrapper-bc');
    rows.push({competition: txt(wrap ? wrap.querySelector(
                 'span.competition-title-bc') : null),
               teams: teams,
               details: txt(block.querySelector('p.results-details-bc')),
               date: times[0] || null, time: times[1] || null});
  });
  return rows;
}
"""


def slug(name: str | None) -> str:
    if not name:
        return ""
    s = re.sub(r"\bvirtual\b", " ", name, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def matches(rendered: str | None, recorded: str | None) -> bool:
    a, b = slug(rendered), slug(recorded)
    if not a or not b:
        return False
    if a == b:
        return True
    sh, lo = (a, b) if len(a) <= len(b) else (b, a)
    return len(sh) >= 4 and sh in lo


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--db", default=str(ROOT / "blm_pokerbet.db"))
    ap.add_argument("--wait", type=float, default=10.0)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    try:
        conn.execute("CREATE TABLE __probe (x)")
        print("ABORT: not read-only")
        return 2
    except sqlite3.OperationalError:
        print("production: read-only PROVEN (write refused)\n")

    games = conn.execute(
        """SELECT g.source_game_id, g.classification, g.home_team,
                  g.away_team, g.first_seen_at, g.last_seen_at,
                  r.final_home, r.final_away, r.final_total, r.result_at
           FROM games g JOIN game_results r
                  ON r.source_game_id = g.source_game_id
           WHERE r.result_source = 'RESULTS_PAGE'
             AND r.final_result_status = 'OK'
           ORDER BY r.result_at DESC LIMIT ?""", (args.limit,)).fetchall()
    print(f"testing {len(games)} games holding a RESULTS_PAGE OK result "
          f"(most recent first)\n")

    from playwright.sync_api import sync_playwright

    out = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-gpu",
                                     "--disable-dev-shm-usage"])
            for g in games:
                gid = g["source_game_id"]
                ctx = browser.new_context(
                    viewport={"width": 1600, "height": 1000},
                    user_agent=UA, locale="en-ZA")
                page = ctx.new_page()
                try:
                    page.goto(URL.format(gid=gid), timeout=45_000,
                              wait_until="domcontentloaded")
                    page.wait_for_timeout(int(args.wait * 1000))
                    page.wait_for_timeout(2500)
                    rows = page.evaluate(ROW_JS) or []
                except Exception as e:
                    rows = []
                    print(f"  fetch error: {e}")
                finally:
                    ctx.close()

                hit = None
                for r in rows:
                    names = [t.get("name") for t in (r.get("teams") or [])]
                    if len(names) < 2 or not all(names):
                        continue
                    if ((matches(names[0], g["home_team"])
                         and matches(names[1], g["away_team"]))
                            or (matches(names[0], g["away_team"])
                                and matches(names[1], g["home_team"]))):
                        hit = r
                        break
                verdict = "ROW MATCHED" if hit else "NO MATCHING ROW"
                stored = (g["final_home"], g["final_away"])
                found = None
                if hit:
                    found = tuple(t.get("score") for t in hit["teams"])
                out.append({"game_id": gid, "classification": g["classification"],
                            "record": f"{g['home_team']} v {g['away_team']}",
                            "stored_final": stored, "rows": len(rows),
                            "verdict": verdict,
                            "matched_row": hit,
                            "row_scores": found,
                            "first_seen": g["first_seen_at"],
                            "last_seen": g["last_seen_at"]})
                print(f"{gid}  {g['classification']:11s} rows={len(rows):>3}  "
                      f"{verdict}\n    record: {g['home_team']!r} v "
                      f"{g['away_team']!r}  stored_final={stored} "
                      f"result_at={g['result_at']}")
                if hit:
                    print(f"    row: {hit['competition']!r} "
                          f"{[t.get('name') for t in hit['teams']]} "
                          f"scores={found} details={hit['details']!r} "
                          f"date={hit['date']} time={hit['time']}")
            browser.close()
    finally:
        conn.close()

    yes = sum(1 for r in out if r["verdict"] == "ROW MATCHED")
    print(f"\nVERDICT: {yes}/{len(out)} page-verified games are actually "
          f"present on their results page; {len(out)-yes} are NOT")
    no_rows = sum(1 for r in out if r["rows"] == 0)
    print(f"         pages that rendered ZERO rows: {no_rows}")
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1))
        print(f"json -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
