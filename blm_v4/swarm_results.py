"""Swarm results client — the AUTHORITATIVE result source (2026-09-24).

The PokerBet Results page (https://www.pokerbet.co.za/en/sports/results) is
a React SPA.  Its rows are NOT scraped from HTML: the page opens

    wss://eu-swarm-newm.pokerbet.co.za/

and issues two swarm commands, observed live 2026-09-24 by hooking the
socket the page itself opens:

    {"command":"request_session","params":{"language":"eng",
                                           "site_id":18751019}}
      -> {"data":{"sid": "..."}}

    {"command":"get_result_games","params":{"game_id":"31013207"}}
      -> {"data":{"games":{"game":[{ "game_id":"31013207",
             "scores":"101:111(29:23, 25:27, 26:31, 21:30)",
             "team1_name":"Trabzonspor Virtual",
             "team2_name":"Anadolu Efes SK Virtual",
             "team1_id":1423916,"team2_id":1423878,
             "date":1790273100,           # fixture start, unix
             "competition_name":"Betual TBSL","region_name":"Virtual Matches",
             "sport_id":"3","sport_alias":"Basketball"}]}}}

    {"command":"get_result_games","params":{"is_date_ts":1,
             "from_date":<ts>,"to_date":<ts>,"live":0,"sport_id":3}}
      -> EVERY result in the range (measured: 1170 basketball results for
         2026-09-24 in one ~800 KB frame)

WHY THIS REPLACES DOM SCRAPING
------------------------------
1. The DOM carries NO game id — the only occurrences of the requested id in
   a 408 KB page are analytics trackers echoing the URL.  Identity on the
   DOM path can only be inferred from team names + footer time (measured:
   0/10 sampled page-verified games were even present in the page's default
   Sport=Football list, which is why inner-text parsing attached foreign
   fixtures and produced 739 arithmetically impossible finals).
2. The API matches on ``game_id`` EXACTLY and returns full team identities
   (names AND ids) plus the fixture start timestamp.
3. The score line arrives complete and structured — "H:A(q1h:q1a, ...)" —
   so the 4-quarter render and the quarter arithmetic can be checked
   directly instead of regexing a rendered list.

The browser still hosts the connection: Playwright cannot send on the page's
own WebSocket, so the client opens its OWN socket from inside the page
context (same origin, so the partner handshake is accepted).

READ-ONLY: this issues read commands against a public sportsbook feed.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Optional

__all__ = [
    "SwarmResultsClient",
    "parse_scores",
    "normalize_result",
    "SWARM_URL",
    "SITE_ID",
    "BASKETBALL_SPORT_ID",
]

SWARM_URL = "wss://eu-swarm-newm.pokerbet.co.za/"
SITE_ID = 18751019
LANDING_URL = "https://www.pokerbet.co.za/en/sports/results"
BASKETBALL_SPORT_ID = 3
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

#: "101:111(29:23, 25:27, 26:31, 21:30)" — and the football shape
#: "2:3 (2:3)" with a space and a single period pair.
_RE_SCORES = re.compile(
    r"^\s*(\d{1,3})\s*[:\-]\s*(\d{1,3})\s*(?:\(([^)]*)\))?\s*$")
_RE_PAIR = re.compile(r"^\s*(\d{1,3})\s*[:\-]\s*(\d{1,3})\s*$")


def parse_scores(scores: Optional[str]) -> Optional[dict[str, Any]]:
    """Parse a swarm ``scores`` string.

    Returns ``{"home": int, "away": int, "quarters": [(h, a), ...],
    "period_text": str|None}`` or None when the string is not a final score
    line.  ``quarters`` is empty when the source omits the split.
    """
    if not scores:
        return None
    m = _RE_SCORES.match(str(scores))
    if not m:
        return None
    quarters: list[tuple[int, int]] = []
    for part in (m.group(3) or "").split(","):
        pm = _RE_PAIR.match(part.strip())
        if pm:
            quarters.append((int(pm.group(1)), int(pm.group(2))))
    return {"home": int(m.group(1)), "away": int(m.group(2)),
            "quarters": quarters, "period_text": m.group(3)}


def _iso(ts: Any) -> Optional[str]:
    try:
        return dt.datetime.fromtimestamp(
            int(ts), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def normalize_result(game: dict) -> Optional[dict[str, Any]]:
    """One swarm ``game`` node -> the reconciler's result shape (or None).

    ``{"game_id", "home_team", "away_team", "home_score", "away_score",
    "quarter_scores", "final_total", "start_time", "start_ts",
    "competition", "region", "sport", "sport_id", "team1_id", "team2_id",
    "parse_quality", "raw_scores"}``
    """
    if not isinstance(game, dict):
        return None
    parsed = parse_scores(game.get("scores"))
    if parsed is None:
        return None
    quarters = parsed["quarters"]
    return {
        "game_id": str(game.get("game_id") or ""),
        "home_team": game.get("team1_name"),
        "away_team": game.get("team2_name"),
        "home_score": parsed["home"],
        "away_score": parsed["away"],
        "quarter_scores": quarters,
        "final_total": parsed["home"] + parsed["away"],
        "render_complete": len(quarters) >= 4,
        "parse_quality": ("full" if len(quarters) >= 4
                          else "partial" if quarters else "no_split"),
        "start_ts": game.get("date"),
        "start_time": _iso(game.get("date")),
        "competition": game.get("competition_name"),
        "region": game.get("region_name"),
        "sport": game.get("sport_alias") or game.get("sport_name"),
        "sport_id": str(game.get("sport_id") or ""),
        "team1_id": game.get("team1_id"),
        "team2_id": game.get("team2_id"),
        "interrupted": game.get("interrupted"),
        "raw_scores": game.get("scores"),
    }


#: The whole exchange, run inside the pokerbet.co.za page context.  Playwright
#: cannot send on the SPA's own socket, so this opens a second one (same
#: origin), performs request_session, then issues the lookups and resolves
#: with every reply keyed by its rid.
_ASK_JS = r"""
async (arg) => {
  const {siteId, ids, bulk, timeoutMs} = arg;
  const out = {sid: null, single: {}, bulk: null, errors: []};
  const ws = new WebSocket('wss://eu-swarm-newm.pokerbet.co.za/');
  const wait = {};
  let sessRes, sessRej;
  const sessP = new Promise((res, rej) => { sessRes = res; sessRej = rej; });
  const timer = setTimeout(() => sessRej(new Error('session timeout')),
                           Math.min(timeoutMs, 25000));
  ws.onopen = () => ws.send(JSON.stringify({
    command: 'request_session',
    params: {language: 'eng', site_id: siteId},
    rid: 'sess'}));
  ws.onerror = () => sessRej(new Error('ws error'));
  ws.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.rid === 'sess') {
      out.sid = (m.data && m.data.sid) || null;
      clearTimeout(timer);
      sessRes();
      return;
    }
    if (m.rid && Object.prototype.hasOwnProperty.call(wait, m.rid)) {
      if (m.rid === 'bulk') out.bulk = m;
      else out.single[wait[m.rid]] = m;
      wait[m.rid] = null;
    }
  };
  try { await sessP; } catch (e) {
    out.errors.push(String(e && e.message ? e.message : e));
    return out;
  }
  if (!out.sid) { out.errors.push('no session id'); return out; }
  let i = 0;
  for (const gid of ids) {
    const rid = 'g' + (i++);
    wait[rid] = String(gid);
    ws.send(JSON.stringify({
      command: 'get_result_games',
      params: {game_id: String(gid)},
      rid: rid}));
  }
  if (bulk) {
    wait['bulk'] = true;
    ws.send(JSON.stringify({
      command: 'get_result_games',
      params: {is_date_ts: 1, from_date: bulk.from, to_date: bulk.to,
               live: 0, sport_id: bulk.sportId},
      rid: 'bulk'}));
  }
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    let pending = false;
    for (const k in wait) { if (wait[k] !== null) { pending = true; break; } }
    if (!pending) break;
    await new Promise(r => setTimeout(r, 200));
  }
  for (const k in wait) { if (wait[k] !== null) out.errors.push('timeout ' + k); }
  try { ws.close(); } catch (e) {}
  return out;
}
"""


class SwarmResultsClient:
    """Read results from the swarm feed that backs the PokerBet results page.

    Usage:
        client = SwarmResultsClient()
        client.start()
        try:
            res = client.fetch_result("31013207")
            all_today = client.fetch_results_day("2026-09-24", sport_id=3)
        finally:
            client.close()
    """

    def __init__(self, *, site_id: int = SITE_ID, landing_url: str = LANDING_URL,
                 nav_timeout_ms: int = 45_000, load_wait_s: float = 6.0,
                 ask_timeout_ms: int = 40_000, log=None) -> None:
        self.site_id = site_id
        self.landing_url = landing_url
        self._nav_timeout_ms = nav_timeout_ms
        self._load_wait_s = load_wait_s
        self._ask_timeout_ms = ask_timeout_ms
        self._log = log
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # -- lifecycle ------------------------------------------------------
    def _ensure_page(self) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        self._context = self._browser.new_context(
            viewport={"width": 1600, "height": 1000},
            user_agent=USER_AGENT, locale="en-ZA")
        self._page = self._context.new_page()
        # Any pokerbet.co.za page is enough to host the same-origin socket;
        # the results page also proves the feed is being served.
        self._page.goto(self.landing_url, timeout=self._nav_timeout_ms,
                        wait_until="domcontentloaded")
        self._page.wait_for_timeout(int(self._load_wait_s * 1000))

    def start(self) -> None:
        self._ensure_page()

    def _teardown(self) -> None:
        for closer in (lambda: self._page and self._page.close(),
                       lambda: self._context and self._context.close(),
                       lambda: self._browser and self._browser.close(),
                       lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        self._page = self._context = self._browser = self._pw = None

    def close(self) -> None:
        self._teardown()

    def __enter__(self) -> "SwarmResultsClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- the wire ------------------------------------------------------
    def _ask(self, ids: list[str], bulk: Optional[dict] = None) -> dict:
        self._ensure_page()
        try:
            return self._page.evaluate(_ASK_JS, {
                "siteId": self.site_id, "ids": [str(i) for i in ids],
                "bulk": bulk, "timeoutMs": self._ask_timeout_ms}) or {}
        except Exception as exc:
            if self._log is not None:
                try:
                    self._log.warning("swarm_ask_failed: %s", exc)
                except Exception:
                    pass
            return {"errors": [str(exc)], "single": {}, "bulk": None}

    @staticmethod
    def _games_from(msg: Optional[dict]) -> list[dict]:
        node = ((msg or {}).get("data") or {}).get("games") or {}
        games = node.get("game")
        if isinstance(games, dict):
            return [games]
        return list(games or [])

    def fetch_results(self, game_ids: list[str]) -> dict[str, Optional[dict]]:
        """Look up MANY game ids in ONE session (None when the feed has no
        result for that id — a real absence, not a failure)."""
        ids = [str(g) for g in game_ids if g]
        if not ids:
            return {}
        reply = self._ask(ids)
        out: dict[str, Optional[dict]] = {g: None for g in ids}
        for gid, msg in (reply.get("single") or {}).items():
            games = self._games_from(msg)
            if not games:
                continue
            result = normalize_result(games[0])
            if result is None:
                continue
            # the feed keys the reply by rid, but trust the payload's own id
            key = result.get("game_id") or str(gid)
            out[str(gid)] = result
            if key != str(gid):
                out.setdefault(key, result)
        return out

    def fetch_result(self, game_id: str) -> Optional[dict]:
        """One game's authoritative result, or None when the feed has none."""
        return self.fetch_results([game_id]).get(str(game_id))

    def fetch_results_day(self, day: str,
                          sport_id: int = BASKETBALL_SPORT_ID
                          ) -> list[dict]:
        """EVERY result the feed holds for ``day`` in one sport.

        Measured 2026-09-24: 1170 basketball results in a single ~800 KB
        frame — the bulk path costs one round trip for a whole day, which is
        what makes whole-day reconciliation affordable.
        """
        d = dt.datetime.strptime(day, "%Y-%m-%d").replace(
            tzinfo=dt.timezone.utc)
        start = int(d.timestamp())
        reply = self._ask([], bulk={"from": start, "to": start + 86399,
                                    "sportId": sport_id})
        out = []
        for g in self._games_from(reply.get("bulk")):
            norm = normalize_result(g)
            if norm is not None:
                out.append(norm)
        return out
