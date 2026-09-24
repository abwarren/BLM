"""
BLM V4 — PokerBet Results-Page Fetcher + Parser.

The authoritative result fallback: when a game disappears from the live
panel, its completed-game result is served at

    https://www.pokerbet.co.za/en/sports/results?game={GAME_ID}

Directive 2026-09-23 (result reconciliation): a disappearing live market
is NOT evidence that a game has no result.  This module FETCHES that
page and PARSES the completed-game scoreboard from it; verification
(game identity proof) lives in result_reconciler.py — this module only
reports what the page rendered.

FETCHER PROTOCOL (pluggable for tests — Playwright is never imported at
module load):
    fetch_results_page(gid) -> str | None
        the visible body text of the rendered page (inner_text), or
        None when the fetch itself failed (navigation/timeout).  A
        fetched page that renders NO scoreboard returns "" — a real
        observation of a scoreless page, never conflated with a fetch
        error.

The shipped adapter drives Playwright in an isolated browser.  Isolation
is a CORRECTNESS requirement, not hygiene:

  * the SPA is opaque — it silently KEEPS the previously rendered
    game's scoreboard when the URL's game id does not resolve (verified
    live 2026-09-23: navigating game=30840226 then game=99999999 in the
    SAME context kept rendering 112:93).  Every fetch therefore runs in
    a fresh context so no prior render can leak into an observation.
  * each fetch closes its context — a daemon reusing one browser page
    would accumulate SPA state across games.

TEMPLATE GUARD — the page has TWO renderings:
  1. a completed game: team names + scores + the compact scoreboard
     line  "112:93 (22:25, 24:17, 40:34, 26:17)"  — same shape the
     event-view scoreboard uses, so the proven event_parser regexes
     parse it;
  2. the FAILED template: the filters form ("Start Date *", "End
     Date *", "Sport", "Competition", "RESET", "SHOW") with NO
     scoreboard.  parse() detects that shape and returns a
     TEMPLATE_FAILED result — the game's result genuinely is not
     retrievable from the results page (usually too old / purged);
     the caller records an ATTEMPT row, never a fabricated result.

The parser extracts: game_id (echoed from the request for provenance),
teams, final score, quarter scores, status label, competition, start
time — plus parse_quality: 'full' | 'partial' (quarter scores absent) |
'failed' (no scoreboard).
"""

from __future__ import annotations

import re
from typing import Any, Optional, Protocol

# ── Parsing (text-based — identical policy to event_parser.py) ────────

# The compact scoreboard line: "112:93 (22:25, 24:17, 40:34, 26:17)".
# The results page renders the four quarters COMMA-SEPARATED INSIDE ONE
# paren pair (the event view renders separate parens per quarter — the
# compact regex there is different by design).  The paren contents are
# captured for the quarter split.
_RE_COMPACT_SCORE = re.compile(r"(\d{1,3})\s*:\s*(\d{1,3})\s*\(([^)]*)\)")
_RE_QUARTER_PAIR = re.compile(r"^(\d{1,3}):(\d{1,3})$")
_RE_SCORE_TOKEN = re.compile(r"^\d{1,3}$")

# The results-page FAILED template's distinguishing markers (the filter
# form renders without any game block).  Detection needs a QUORUM: the
# page's global nav ("LIVE CALENDAR" / "RESULTS") appears on every
# rendering, so it can never signal failure on its own.
_FAILED_MARKERS_STRONG = ("Start Date *", "End Date *")
_FAILED_MARKERS_SOFT = ("RESET", "SHOW")
_FAILED_QUORUM = 2

_RE_COMPETITION = re.compile(r"^[A-Za-z].*\([A-Za-z ]+\)$")
_RE_DATEY = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}\b|\d{2}:\d{2}\b|\d{1,2}\s+\w+\s+\d{4})")


def _lines(text: str) -> list[str]:
    return [l.strip() for l in (text or "").split("\n") if l.strip()]


# ── Row-based DOM observation (2026-09-24) ───────────────────────────
#
# The results page does NOT echo the requested game id in its DOM (the only
# occurrences are analytics trackers echoing the URL) and it renders a
# LIST of games — ``div.results-block-bc`` per game, each with
# ``p.results-teams-name-bc`` / ``span.results-teams-score-bc`` and a
# ``p.results-details-bc`` compact line, plus ``time.results-footer-date-bc``
# date/time nodes inside ``div.competition-wrapper-bc``.
#
# Consequence, measured live 2026-09-24: reading the page's inner text and
# taking the first scoreboard reaches a DIFFERENT game's result whenever the
# requested game is not on the page — which is the usual case (the page
# renders an unrelated default list: 260 football rows for a basketball
# virtual id).  A result may therefore only be taken from a ROW THAT MATCHES
# the record's fixture, and a page with no matching row is an observation of
# ABSENCE, never a result.

ROW_EXTRACT_JS = r"""
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

_RE_DM = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")


def _row_start_iso(row: dict) -> Optional[str]:
    """The row's fixture start as 'YYYY-MM-DD HH:MM' (None when absent).

    The page renders the footer as two ``time`` nodes — DD.MM.YYYY then
    HH:MM — and the virtual fixtures carry a full start time.
    """
    d, t = (row or {}).get("date"), (row or {}).get("time")
    if not d or not t:
        return None
    m = _RE_DM.match(str(d).strip())
    if not m:
        return None
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)} {str(t).strip()}"


def parse_row(row: dict) -> dict[str, Any]:
    """One DOM row -> the same dict shape ``parse_results_page`` returns.

    The compact ``details`` line ("101:111 (29:23, 25:27, 26:31, 21:30)")
    is authoritative for the score and the quarter split; the per-team
    score spans are the fallback when it is absent.
    """
    res: dict[str, Any] = {
        "home_team": None, "away_team": None, "home_score": None,
        "away_score": None, "rendered_pairs": [], "final_total": None,
        "quarter_scores": [], "status_label": None, "competition": None,
        "start_time": None, "parse_quality": "failed",
        "failed_template": False,
    }
    teams = [t for t in ((row or {}).get("teams") or [])
             if t.get("name")]
    if len(teams) >= 2:
        res["home_team"] = teams[0]["name"]
        res["away_team"] = teams[1]["name"]
    details = (row or {}).get("details") or ""
    m = _RE_COMPACT_SCORE.search(details)
    if m:
        res["home_score"] = int(m.group(1))
        res["away_score"] = int(m.group(2))
        quarters = []
        for pair in (m.group(3) or "").split(","):
            pm = _RE_QUARTER_PAIR.match(pair.strip())
            if pm:
                quarters.append((int(pm.group(1)), int(pm.group(2))))
        res["quarter_scores"] = quarters
        res["parse_quality"] = ("full" if len(quarters) >= 4 else "partial")
    elif len(teams) >= 2:
        def _i(v):
            try:
                return int(str(v).strip())
            except (TypeError, ValueError):
                return None
        res["home_score"] = _i(teams[0].get("score"))
        res["away_score"] = _i(teams[1].get("score"))
        if res["home_score"] is not None and res["away_score"] is not None:
            res["parse_quality"] = "partial"
    if res["home_score"] is not None and res["away_score"] is not None:
        res["final_total"] = res["home_score"] + res["away_score"]
        res["rendered_pairs"] = [
            (res["home_team"], res["home_score"]),
            (res["away_team"], res["away_score"])]
    res["competition"] = (row or {}).get("competition")
    res["start_time"] = _row_start_iso(row)
    return res


def looks_like_failed_template(text: str) -> bool:
    """True when the rendered page is the filters-only FAILED template.

    Quorum rule (_FAILED_QUORUM): BOTH strong markers ("Start Date *" /
    "End Date *") or one strong + both soft markers.  The nav strings
    ("RESULTS", "LIVE CALENDAR") render on every page and never count.
    """
    lines = set(_lines(text))
    strong = sum(1 for m in _FAILED_MARKERS_STRONG if m in lines)
    soft = sum(1 for m in _FAILED_MARKERS_SOFT if m in lines)
    return strong >= 2 or (strong >= 1 and soft >= _FAILED_QUORUM)


def parse_results_page(text: str, game_id: str = "") -> dict[str, Any]:
    """Parse one results-page body text into a game-result dict.

    Returns the fields the reconciler verifies (game_id, teams, final
    score, quarter scores, status, competition, start time) plus
    parse_quality.  Never raises; a scoreless page yields
    parse_quality='failed' with all game fields None.
    """
    res: dict[str, Any] = {
        "game_id": str(game_id or ""),
        "home_team": None,
        "away_team": None,
        "home_score": None,
        "away_score": None,
        "rendered_pairs": [],
        "final_total": None,
        "quarter_scores": [],
        "status_label": None,
        "competition": None,
        "start_time": None,
        "parse_quality": "failed",
        "failed_template": False,
    }
    if not (text or "").strip():
        return res

    lines = _lines(text)

    # ── compact scoreboard: the authoritative score + quarter split ──
    m = _RE_COMPACT_SCORE.search(text)
    if not m:
        res["failed_template"] = looks_like_failed_template(text)
        return res

    res["home_score"] = int(m.group(1))
    res["away_score"] = int(m.group(2))
    res["final_total"] = res["home_score"] + res["away_score"]
    quarters: list[tuple[int, int]] = []
    for pair in (m.group(3) or "").split(","):
        pm = _RE_QUARTER_PAIR.match(pair.strip())
        if pm:
            quarters.append((int(pm.group(1)), int(pm.group(2))))
    res["quarter_scores"] = quarters
    res["parse_quality"] = ("full"
                            if len(quarters) >= 4 else "partial")

    # Status: the "Live | Finished" toggle — "Finished" renders above
    # the form for a completed game.
    for lab in ("Finished", "Live"):
        if lab in lines:
            res["status_label"] = lab
            break

    # ── team block: the 4 lines immediately before the compact line ──
    #     observed live (2026-09-23): home-name, home-score, away-name,
    #     away-score, compact (the first-listed team is HOME; its score
    #     is the compact line's first number).
    comp_idx = None
    for i, l in enumerate(lines):
        if _RE_COMPACT_SCORE.search(l):
            comp_idx = i
            break
    if comp_idx is not None and comp_idx >= 4:
        pre = lines[comp_idx - 4: comp_idx]
        scores = [t for t in pre if _RE_SCORE_TOKEN.match(t)]
        names = [t for t in pre if not _RE_SCORE_TOKEN.match(t)
                 and len(t) >= 4 and re.search(r"[A-Za-z]{3,}", t)]
        if len(scores) >= 2 and len(names) >= 2:
            res["home_team"] = names[0]
            res["away_team"] = names[-1]
            # Positional pairing: the rendered-FIRST name owns the
            # compact line's FIRST number (verified layout).  Some SPA
            # renderings swap the block order — the pairing travels with
            # it, so verification can orient scores by RECORD teams.
            res["rendered_pairs"] = [
                (names[0], res["home_score"]),
                (names[1], res["away_score"]),
            ]

    # ── competition / start time: the block above the team block ─────
    if comp_idx is not None:
        above = lines[: comp_idx - 4] if comp_idx >= 4 else []
        for l in reversed(above):
            if res["competition"] is None and _RE_COMPETITION.match(l) \
                    and "(" in l:
                res["competition"] = l
            elif res["start_time"] is None and _RE_DATEY.match(l):
                res["start_time"] = l
            if res["competition"] and res["start_time"]:
                break

    return res


# ── Fetcher protocol + shipped Playwright adapter ─────────────────────

class ResultsFetcher(Protocol):
    """Fetch the results page body text for one game id (or None)."""

    def fetch_results_page(self, game_id: str) -> Optional[str]: ...


class RowResultsFetcher(Protocol):
    """Fetch the results page as STRUCTURED ROWS (or None on fetch error).

    Preferred over text: the page is a list of games and carries no game
    id, so only the row structure can prove which game a score belongs to.
    """

    def fetch_results_rows(self, game_id: str) -> Optional[list[dict]]: ...


RESULTS_URL = "https://www.pokerbet.co.za/en/sports/results?game={gid}"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class PlaywrightResultsFetcher:
    """Shipped adapter: Playwright, one fresh context per fetch.

    Fresh-context isolation is load-bearing (see the module docstring):
    the SPA keeps the previous game's scoreboard when the requested id
    does not resolve, so any shared state can poison an observation
    with the WRONG game's result.  The browser is lazily launched and
    recycled by age to bound memory in the daemon.
    """

    def __init__(self, *, headless: bool = True,
                 load_wait_s: float = 9.0,
                 nav_timeout_ms: int = 35_000,
                 max_browser_uses: int = 200,
                 log: Any = None):
        self._headless = bool(headless)
        self._load_wait_s = float(load_wait_s)
        self._nav_timeout_ms = int(nav_timeout_ms)
        self._max_browser_uses = max(1, int(max_browser_uses))
        self._log = log
        self._pw = None
        self._browser = None
        self._uses = 0

    # -- lifecycle ------------------------------------------------------
    def _ensure_browser(self):
        if self._browser is not None and self._browser.is_connected() \
                and self._uses < self._max_browser_uses:
            return
        self._close_browser()
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self._headless,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        self._uses = 0

    def _close_browser(self):
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw = None

    def close(self):
        self._close_browser()

    # -- protocol -------------------------------------------------------
    def fetch_results_page(self, game_id: str) -> Optional[str]:
        """Rendered body text for ``game_id``; None on fetch failure.

        Returns "" when the page rendered without a scoreboard (a real
        observation — see the module docstring's fetch contract)."""
        gid = str(game_id or "").strip()
        if not gid.isdigit():
            return None
        self._ensure_browser()
        self._uses += 1
        try:
            context = self._browser.new_context(
                viewport={"width": 1600, "height": 900},
                user_agent=USER_AGENT, locale="en-ZA",
            )
        except Exception:
            self._close_browser()      # dead browser — rebuilt next call
            return None
        try:
            page = context.new_page()
            page.goto(RESULTS_URL.format(gid=gid),
                      timeout=self._nav_timeout_ms,
                      wait_until="domcontentloaded")
            page.wait_for_timeout(int(self._load_wait_s * 1000))
            return page.inner_text("body", timeout=10_000)
        except Exception:
            if self._log is not None:
                try:
                    self._log.warning("results_page_fetch_failed gid=%s", gid)
                except Exception:
                    pass
            return None
        finally:
            try:
                context.close()
            except Exception:
                pass

    def fetch_results_rows(self, game_id: str) -> Optional[list[dict]]:
        """The page's result ROWS for ``game_id``; None on fetch failure.

        THE authoritative observation (2026-09-24): the page is a list of
        games and echoes no game id, so identity is decided by matching a
        row — never by reading the first scoreboard in the text.  An EMPTY
        list is a real observation ("the page rendered no result rows"),
        distinct from None ("the fetch itself failed").

        Each row: {competition, teams:[{name,score},..], details, date,
        time}.  A fresh context isolates every fetch (the SPA keeps the
        previous render otherwise); the rows are extracted with one
        evaluate() so no element handle can go stale mid-read.
        """
        gid = str(game_id or "").strip()
        if not gid.isdigit():
            return None
        self._ensure_browser()
        self._uses += 1
        try:
            context = self._browser.new_context(
                viewport={"width": 1600, "height": 1000},
                user_agent=USER_AGENT, locale="en-ZA",
            )
        except Exception:
            self._close_browser()
            return None
        try:
            page = context.new_page()
            page.goto(RESULTS_URL.format(gid=gid),
                      timeout=self._nav_timeout_ms,
                      wait_until="domcontentloaded")
            page.wait_for_timeout(int(self._load_wait_s * 1000))
            # the list renders lazily in waves; give the rows a second
            # chance before declaring the page empty
            rows = page.evaluate(ROW_EXTRACT_JS) or []
            if not rows:
                page.wait_for_timeout(2500)
                rows = page.evaluate(ROW_EXTRACT_JS) or []
            return rows
        except Exception:
            if self._log is not None:
                try:
                    self._log.warning("results_page_rows_failed gid=%s", gid)
                except Exception:
                    pass
            return None
        finally:
            try:
                context.close()
            except Exception:
                pass
