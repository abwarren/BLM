#!/usr/bin/env python3
"""Query the swarm `get_result_games` API directly (the results-page source).

Discovered 2026-09-24 by hooking the socket the results page itself opens:

    wss://eu-swarm-newm.pokerbet.co.za/
      request_session   {language:'eng', site_id:18751019}
        -> {sid}
      get_result_games  {game_id:'31013207'}
        -> {"games":{"game":[{game_id, scores:"101:111(29:23, ...)",
             team1_name, team2_name, team1_id, team2_id, date,
             competition_name, sport_alias, ...}]}}
      get_result_games  {is_date_ts:1, from_date, to_date, live:0, sport_id}
        -> EVERY result in the range (one call, ~800 KB)

This is the authoritative result source behind https://.../sports/results:
exact game_id match, full 4-quarter score line, real team identities.

Usage:
    python3 probe_swarm_results.py --ids 31013207 30840226
    python3 probe_swarm_results.py --bulk --sport-id 3           # basketball today
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

LANDING = "https://www.pokerbet.co.za/en/sports/results"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
SITE_ID = 18751019

ASK_JS = r"""
async (arg) => {
  const {siteId, ids, bulk} = arg;
  const out = {sid: null, single: {}, bulk: null, errors: []};
  const ws = new WebSocket('wss://eu-swarm-newm.pokerbet.co.za/');
  const wait = {};
  let sessionResolve, sessionReject;
  const sessionP = new Promise((res, rej) => {
    sessionResolve = res; sessionReject = rej;
  });
  ws.onopen = () => ws.send(JSON.stringify({
    command: 'request_session',
    params: {language: 'eng', site_id: siteId},
    rid: 'sess'}));
  ws.onerror = () => sessionReject(new Error('ws error'));
  setTimeout(() => sessionReject(new Error('session timeout')), 20000);
  ws.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.rid === 'sess') {
      out.sid = (m.data && m.data.sid) || null;
      sessionResolve(); return;
    }
    if (m.rid && wait[m.rid] !== undefined) {
      if (m.rid === 'bulk') out.bulk = m;
      else out.single[wait[m.rid]] = m;
      wait[m.rid] = null;
    }
  };
  try { await sessionP; } catch (e) {
    out.errors.push(String(e)); return out;
  }
  if (!out.sid) { out.errors.push('no sid'); return out; }
  let i = 0;
  for (const gid of ids) {
    const rid = 'g' + (i++);
    wait[rid] = String(gid);
    ws.send(JSON.stringify({
      command: 'get_result_games',
      params: {game_id: String(gid)}, rid}));
  }
  if (bulk) {
    wait['bulk'] = true;
    ws.send(JSON.stringify({
      command: 'get_result_games',
      params: {is_date_ts: 1, from_date: bulk.from, to_date: bulk.to,
               live: 0, sport_id: bulk.sportId},
      rid: 'bulk'}));
  }
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (Object.values(wait).every(v => v === null)) break;
    await new Promise(r => setTimeout(r, 250));
  }
  try { ws.close(); } catch (e) {}
  return out;
}
"""


def _day_bounds(day: str) -> tuple[int, int]:
    d = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    start = int(d.timestamp())
    return start, start + 86399


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="*", default=[])
    ap.add_argument("--bulk", action="store_true")
    ap.add_argument("--sport-id", type=int, default=3)
    ap.add_argument("--day", default=None, help="YYYY-MM-DD (default today UTC)")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    day = args.day or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    frm, to = _day_bounds(day)
    bulk = {"from": frm, "to": to, "sportId": args.sport_id} if args.bulk else None

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        ctx = b.new_context(user_agent=UA, locale="en-ZA")
        page = ctx.new_page()
        page.goto(LANDING, timeout=45_000, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        res = page.evaluate(ASK_JS, {"siteId": SITE_ID, "ids": args.ids,
                                     "bulk": bulk})
        ctx.close()
        b.close()

    print("session sid :", (res.get("sid") or "")[:40])
    if res.get("errors"):
        print("errors      :", res["errors"])

    print(f"\n=== single lookups ({len(args.ids)} ids) ===")
    for gid, m in (res.get("single") or {}).items():
        games = (((m or {}).get("data") or {}).get("games") or {}).get("game")
        if not games:
            print(f"  {gid}: NO RESULT  (code={m.get('code')})")
            continue
        g = games[0]
        print(f"  {gid}: {g.get('team1_name')} vs {g.get('team2_name')}")
        print(f"      scores  : {g.get('scores')}")
        print(f"      comp    : {g.get('competition_name')} "
              f"({g.get('region_name')}) sport={g.get('sport_alias')}")
        d = g.get("date")
        if d:
            print(f"      start   : "
                  f"{dt.datetime.fromtimestamp(int(d), dt.timezone.utc)}")
        print(f"      team_ids: {g.get('team1_id')} / {g.get('team2_id')}"
              f"   game_id={g.get('game_id')}")

    if bulk:
        games = (((res.get("bulk") or {}).get("data") or {}).get("games")
                 or {}).get("game") or []
        print(f"\n=== BULK {day} sport_id={args.sport_id}: "
              f"{len(games)} results ===")
        comps: dict[str, int] = {}
        for g in games:
            c = g.get("competition_name") or "?"
            comps[c] = comps.get(c, 0) + 1
        for c, n in sorted(comps.items(), key=lambda kv: -kv[1]):
            print(f"    {n:>4}  {c}")
        print("  --- Betual/Cyber sample ---")
        n = 0
        for g in games:
            if "betual" in (g.get("competition_name") or "").lower() \
                    or "cyber" in (g.get("competition_name") or "").lower():
                print(f"    {g.get('game_id')} {g.get('competition_name')} | "
                      f"{g.get('team1_name')} vs {g.get('team2_name')} | "
                      f"{g.get('scores')}")
                n += 1
                if n >= 12:
                    break

    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=1, default=str))
        print(f"\njson -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
