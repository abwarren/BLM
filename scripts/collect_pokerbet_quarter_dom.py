#!/usr/bin/env python3
"""Collect historical quarter markets from hydrated PokerBet event pages.

For each completed BLM game this opens its PokerBet Results page, selects the
matching result row in the page, waits for the event view to hydrate, then
captures the visible event DOM. Every row written to the dedicated table is
tagged ``pokerbet_dom`` and keeps the verbatim period/market text. Unclear
lines and prices are NULL; this script never derives bookmaker lines.

Usage: python3 scripts/collect_pokerbet_quarter_dom.py [--db PATH] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from blm_v4.storage import PokerBetStore

PERIOD_RE = re.compile(r"\b(1st|first|2nd|second|3rd|third|4th|fourth)\s+quarter\b", re.I)
HALF_RE = re.compile(r"\b(first|1st|second|2nd)\s+half\b", re.I)
NUMBER_RE = re.compile(r"(?<!\w)([+-]?\d{1,3}(?:\.\d+)?)(?!\w)")
TOTAL_RE = re.compile(r"\b(?:total\s+(?:points|score)|total)\b", re.I)
TEAM_TOTAL_RE = re.compile(r"\b(?:home|away|team)\s+total\b|\btotal\s+(?:home|away|team)\b", re.I)
PRICE_RE = re.compile(r"\b(over|under)\b\s*[@:]?\s*(\d+(?:\.\d+)?)", re.I)

EVENT_DOM_JS = r"""() => {
  const clean = e => (e?.innerText || e?.textContent || '').trim();
  const bodyText = clean(document.body);
  const blocks = [...document.querySelectorAll('[data-testid*=market], [data-market-id],
    [class*=market-group], [class*=market-section], [class*=market-container], [class*=market-item]')];
  const marketNodes = blocks.filter(e => {
    const t = clean(e);
    return t && t.length < 1200 && /over|under|handicap|total|quarter|half/i.test(t);
  });
  const unique = new Set();
  const markets = [];
  for (const e of marketNodes) {
    const text = clean(e).replace(/\s+/g, ' ');
    if (!text || unique.has(text)) continue;
    unique.add(text);
    markets.push({text, tag:e.tagName, className:String(e.className || ''),
      marketId:e.getAttribute('data-market-id'), testId:e.getAttribute('data-testid')});
  }
  const score = [...document.querySelectorAll('p,span,div')]
    .map(e => clean(e)).find(t => /^\d{1,3}\s*:\s*\d{1,3}\s*,/.test(t)) || '';
  return {url: location.href, title: document.title, bodyText, markets, score,
    capturedAt: new Date().toISOString()};
}"""

PERIOD_CLICK_JS = r"""(period) => {
  const aliases = {
    Q1: [/^q1$/i, /^1st quarter$/i, /^first quarter$/i],
    Q2: [/^q2$/i, /^2nd quarter$/i, /^second quarter$/i],
    Q3: [/^q3$/i, /^3rd quarter$/i, /^third quarter$/i],
    Q4: [/^q4$/i, /^4th quarter$/i, /^fourth quarter$/i],
    '1H': [/^1st half$/i, /^first half$/i, /^1h$/i],
    '2H': [/^2nd half$/i, /^second half$/i, /^2h$/i]
  }[period] || [];
  const candidates = [...document.querySelectorAll('button,[role=tab],a,li,span,div')]
    .filter(e => e.children.length < 3 && aliases.some(r => r.test((e.innerText || '').trim())));
  const el = candidates.sort((a,b) => a.children.length - b.children.length)[0];
  if (!el) return false;
  el.click(); return true;
}"""


def _period(text: str) -> str | None:
    if re.search(r"\b(?:overtime|extra\s+time|OT)\b", text, re.I):
        return "OT"
    m = PERIOD_RE.search(text)
    if m:
        token = m.group(1).lower()
        q = {"1st": 1, "first": 1, "2nd": 2, "second": 2,
             "3rd": 3, "third": 3, "4th": 4, "fourth": 4}[token]
        return f"Q{q}"
    m = HALF_RE.search(text)
    if m:
        return "1H" if m.group(1).lower() in ("first", "1st") else "2H"
    return None


def _provider_event_id_from_url(url: str | None) -> str | None:
    """Read PokerBet's BetConstruct event ID from its canonical event-view URL.

    The route is /event-view/{sport}/{region}/{competition-id}/
    {competition-slug}/{event-id}/{event-slug}. Do not search for arbitrary
    digit runs: competition IDs and unrelated query values also contain digits.
    """
    if not url:
        return None
    parts = [unquote(p) for p in urlsplit(url).path.split("/") if p]
    try:
        i = parts.index("event-view")
    except ValueError:
        return None
    event_id = parts[i + 5] if len(parts) > i + 5 else None
    return event_id if event_id and re.fullmatch(r"\d{6,}", event_id) else None


def _verify_event_identity(game: dict, opened_url: str | None) -> str:
    """Require stored and opened canonical URLs to identify the same event."""
    stored_id = _provider_event_id_from_url(game.get("source_url"))
    if stored_id is None:
        raise ValueError("stored source_url has no canonical PokerBet event ID")
    if str(game.get("source_game_id")) != stored_id:
        raise ValueError("source_game_id is an alias, not the canonical provider event ID")
    opened_id = _provider_event_id_from_url(opened_url)
    if opened_id is None:
        raise ValueError("opened page has no canonical PokerBet event ID")
    if opened_id != stored_id:
        raise ValueError(f"event id mismatch: expected {stored_id}, opened {opened_id}")
    return stored_id


def _market_type(text: str) -> str | None:
    low = text.lower()
    if "handicap" in low or "spread" in low:
        return "handicap"
    if TEAM_TOTAL_RE.search(text):
        return None
    if TOTAL_RE.search(text):
        return "total"
    return None


def _market_values(text: str) -> tuple[float | None, float | None, float | None]:
    """Return only a line anchored to an explicitly named Total market.

    Prices are retained when the rendered text labels each side. No
    positional inference or score/model-derived fallback is permitted.
    """
    if _market_type(text) != "total":
        return None, None, None
    match = TOTAL_RE.search(text)
    tail = text[match.end():] if match else ""
    prices = {side.upper(): float(price) for side, price in PRICE_RE.findall(tail)}
    # The line must directly follow the Total label (allowing punctuation).
    line_match = re.match(r"\s*[:=-]?\s*(\d{1,3}(?:\.\d+)?)\b", tail)
    line = float(line_match.group(1)) if line_match else None
    return line, prices.get("OVER"), prices.get("UNDER")


def _rows_for_dom(game: dict, dom: dict) -> list[dict]:
    """Normalize only explicit text. Preserve each captured market verbatim."""
    expected = str(game.get("provider_event_id") or "")
    verified = str(dom.get("verified_event_id") or "")
    # The internal identity is source + source_game_id. A decorated BLM ID
    # (e.g. 12345678#i1) cannot be attributed to provider event 12345678.
    # Refuse missing, mismatched, and aliased IDs before constructing rows.
    if (not expected or not re.fullmatch(r"\d{6,}", expected)
            or str(game.get("source_game_id")) != expected
            or verified != expected):
        return []
    page_event_id = _provider_event_id_from_url(dom.get("url"))
    if page_event_id != expected:
        return []
    now = dom.get("capturedAt") or datetime.now(timezone.utc).isoformat()
    scoreboard = dom.get("score", "")
    final = re.search(r"(\d{1,3})\s*:\s*(\d{1,3})", scoreboard)
    scores = [int(final.group(1)), int(final.group(2))] if final else []
    q_scores = [(int(h), int(a)) for h, a in
                re.findall(r"\((\d{1,3})\s*:\s*(\d{1,3})\)", scoreboard)]
    out = []
    for item in dom.get("markets") or []:
        raw_text = item.get("text") or ""
        period = _period(raw_text) or dom.get("selected_period")
        if period not in {"Q1", "Q2", "Q3", "Q4", "OT", "1H", "2H"}:
            continue
        # The DOM's market card text is retained verbatim. BetConstruct
        # renders ladders and paired Over/Under prices differently across
        # sports; positional numbers cannot safely be called a line/price.
        # Leave normalized fields NULL until an explicit row structure is
        # available instead of guessing from text order.
        qscore = (q_scores[int(period[1:]) - 1]
                  if period.startswith("Q") and
                  len(q_scores) >= int(period[1:]) else None)
        market_type = _market_type(raw_text)
        line, over_price, under_price = _market_values(raw_text)
        out.append({
            "source_game_id": str(game["source_game_id"]),
            "game_id": game.get("id"), "event_url": dom.get("url"),
            "league": game.get("competition"),
            "home_team": game.get("home_team"),
            "away_team": game.get("away_team"),
            "game_started_at": game.get("result_started_at"),
            "observed_at": now, "source": "pokerbet_dom",
            "period": period, "market_type": market_type,
            "selection": None, "line_value": line, "odds": None,
            "over_price": over_price, "under_price": under_price,
            "quarter_home_score": qscore[0] if qscore else None,
            "quarter_away_score": qscore[1] if qscore else None,
            "cumulative_home_score": scores[0] if len(scores) == 2 else None,
            "cumulative_away_score": scores[1] if len(scores) == 2 else None,
            "market_timestamp": None, "raw_text": raw_text,
            "raw": {"tag": item.get("tag"), "class": item.get("className"),
                    "visible_score_text": dom.get("score"),
                    "event_url": dom.get("url"),
                    "provider_market_id": item.get("marketId"),
                    "provider_test_id": item.get("testId"),
                    "market_line": line, "over_price": over_price,
                    "under_price": under_price,
                    "score_validation": dom.get("score_validation")},
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "blm_pokerbet.db"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--wait", type=float, default=8.0)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    db_path = Path(args.db)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sql = """SELECT id,source_game_id,source_url,competition,home_team,away_team,
                     first_seen_at FROM games
              WHERE sport='basketball' AND status='ended' AND source_url IS NOT NULL
              ORDER BY first_seen_at"""
    raw_games = [dict(r) for r in con.execute(sql)]
    con.close()
    games = []
    identity_skipped = 0
    for game in raw_games:
        provider_id = _provider_event_id_from_url(game.get("source_url"))
        # Only the BLM row whose canonical source_game_id exactly equals the
        # provider event ID can receive this provider's DOM observation.
        # Suffixed collision aliases share a provider URL and are not distinct
        # bookmaker events, so they are deliberately excluded.
        if not provider_id or str(game.get("source_game_id")) != provider_id:
            identity_skipped += 1
            continue
        game["provider_event_id"] = provider_id
        games.append(game)
    if args.limit:
        games = games[:max(0, args.limit)]
    if not games:
        print("No completed basketball games with canonical PokerBet event IDs found.")
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Playwright is required for DOM collection") from exc

    store = PokerBetStore(db_path)
    captured = unavailable = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed,
                                     args=["--no-sandbox", "--disable-gpu"])
        for game in games:
            context = browser.new_context(viewport={"width": 1600, "height": 1000},
                                          locale="en-ZA")
            page = context.new_page()
            try:
                # Navigate directly to the canonical event URL already stored
                # for this BLM game. Never resolve identity by picking a
                # results row using display names.
                page.goto(game["source_url"],
                          timeout=45_000, wait_until="domcontentloaded")
                page.wait_for_timeout(int(args.wait * 1000))
                try:
                    verified_event_id = _verify_event_identity(
                        game, page.evaluate("() => location.href"))
                except ValueError as exc:
                    unavailable += 1
                    print(f"skip {game['source_game_id']}: {exc}")
                    continue
                # PokerBet fills markets asynchronously on the event page.
                page.wait_for_timeout(2500)
                captures = []
                for period in ("Q1", "Q2", "Q3", "Q4", "1H", "2H"):
                    if not page.evaluate(PERIOD_CLICK_JS, period):
                        continue
                    page.wait_for_timeout(900)
                    observed_dom = page.evaluate(EVENT_DOM_JS)
                    observed_dom["selected_period"] = period
                    captures.append(observed_dom)
                if not captures:
                    captures = [page.evaluate(EVENT_DOM_JS)]
                if not any(d.get("markets") for d in captures):
                    page.wait_for_timeout(5000)
                    captures = [page.evaluate(EVENT_DOM_JS)]
                dom = captures[0]
                dom["score_validation"] = {
                    "identity_source": "stored_and_opened_canonical_event_url",
                    "provider_event_id": verified_event_id,
                    "cross_checked_with_results_page": False,
                }
                rows = []
                for observed_dom in captures:
                    observed_dom["score_validation"] = dom["score_validation"]
                    observed_dom["verified_event_id"] = verified_event_id
                    rows.extend(_rows_for_dom(game, observed_dom))
                if not rows:
                    unavailable += 1
                    print(f"skip {game['source_game_id']}: no visible Q1-Q4/half markets")
                    continue
                for row in rows:
                    store.insert_pokerbet_dom_market_observation(row)
                captured += len(rows)
                print(f"{game['source_game_id']}: stored {len(rows)} DOM observations")
            except Exception as exc:
                unavailable += 1
                print(f"skip {game['source_game_id']}: {type(exc).__name__}: {exc}")
            finally:
                context.close()
                time.sleep(0.15)
        browser.close()
    print(f"complete: observations={captured} games_without_capture={unavailable} "
          f"identity_skipped={identity_skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
