#!/usr/bin/env python3
"""
DOM row scanner for the PokerBet results page (directive 2026-09-24).

The page does NOT echo the requested game id anywhere (verified: the only
occurrences are analytics trackers echoing the URL).  It renders a LIST:

    div.results-block-bc                      <- one game
      div.results-info-bc
        div.results-teams-bc
          p.results-teams-name-bc              <- team name
          span.results-teams-score-bc          <- that team's score
        p.results-details-bc                   <- "0:2 (0:0)" compact line
      div.results-footer-bc
        time.results-footer-date-bc            <- date, then time (2 nodes)
    ...wrapped in div.competition-wrapper-bc with
       span.competition-title-bc               <- competition label

So identity must be established by MATCHING A ROW (teams + fixture time),
never by "the first scoreboard on the page".  This script dumps every row
so the match rule can be derived from real data.

Usage:
    python3 scripts/scan-results-rows.py 30840226 31013207 ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
      teams.push({
        name: txt(t.querySelector('p.results-teams-name-bc')),
        score: txt(t.querySelector('span.results-teams-score-bc')),
      });
    });
    const times = [];
    block.querySelectorAll('time.results-footer-date-bc').forEach(
      t => times.push(txt(t)));
    const wrap = block.closest('div.competition-wrapper-bc');
    rows.push({
      competition: txt(wrap ? wrap.querySelector(
        'span.competition-title-bc') : null),
      teams: teams,
      details: txt(block.querySelector('p.results-details-bc')),
      date: times[0] || null,
      time: times[1] || null,
      footer_date_nodes: times,
    });
  });
  // the filter form's current Sport / Competition / dates, so we know
  // whether the page is filtered to our game or showing the default list
  const form = {};
  document.querySelectorAll('div.form-control-bc').forEach(f => {
    const title = txt(f.querySelector('span.form-control-title-bc'));
    const input = f.querySelector('input');
    const sel = f.querySelector('div.form-control-select-bc');
    if (title) form[title] = input ? input.value : (sel ? txt(sel) : null);
  });
  const times = [];
  document.querySelectorAll('time.infoTime').forEach(t => times.push(txt(t)));
  return {rows: rows, filter_form: form, page_clock: times[0] || null};
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gids", nargs="+")
    ap.add_argument("--wait", type=float, default=9.0)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    out: dict = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        for gid in args.gids:
            ctx = browser.new_context(viewport={"width": 1600, "height": 1000},
                                      user_agent=UA, locale="en-ZA")
            page = ctx.new_page()
            try:
                page.goto(URL.format(gid=gid), timeout=45_000,
                          wait_until="domcontentloaded")
                page.wait_for_timeout(int(args.wait * 1000))
                page.wait_for_timeout(2500)
                data = page.evaluate(ROW_JS)
            except Exception as e:
                data = {"error": str(e)}
            finally:
                ctx.close()
            out[gid] = data
            rows = data.get("rows") or []
            print(f"\n=== game={gid}  rows={len(rows)}  "
                  f"page_clock={data.get('page_clock')} ===")
            print(f"    filter form: {data.get('filter_form')}")
            for i, r in enumerate(rows[:8]):
                names = " v ".join(f"{t['name']}({t['score']})"
                                   for t in r["teams"])
                print(f"  [{i}] {r['competition']!r:34s} {names}  "
                      f"details={r['details']!r} date={r['date']} "
                      f"time={r['time']}")
            if len(rows) > 8:
                print(f"    ... {len(rows)-8} more rows")
        browser.close()

    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1))
        print(f"\njson -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
