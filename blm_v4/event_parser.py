"""
BLM V4 — Event-View Market Parser.

Parses the PokerBet (BetConstruct) event-view page for a single game
into a full market observation.  BetConstruct obfuscates class names
per deploy, so parsing is text-based on visible page text — the
approach proven in the betconstruct-sportsbook-scraping skill.

Real captured layout (Cyber Basketball 2K26, 2026-08-30):

    Cyber Basketball. 2K26 Matches
    4th Quarter
    09:46'
    Oklahoma City Thunder Cyber
    San Antonio Spurs Cyber
    1 32 22  2 28 23  3 33 22  4 7 6  Quarter 100 73
    100 : 73, (32:22), (28:23), (33:22), (7:6) 09:46
    All Match Totals Handicaps Markets
    Points Handicap
    Oklahoma City Thunder Cyber
    San Antonio Spurs Cyber
    -26.5 1.95  +26.5 1.75  -25.5 1.75  +25.5 1.95
    Total Points
    Over Under
    216.5 1.70 2.02   217.5 1.80 1.90   218.5 1.90 1.80
    Oklahoma City Thunder Cyber Total Points
    Over Under
    121.5 1.75 1.95
    San Antonio Spurs Cyber Total Points
    Over Under
    95.5 1.75 1.95

Betual NBA adds the descriptor line "4 Quarters of 12 min. Simulated Game"
and Halves/Quarters market tabs.

Market sections are parsed from a token stream: each line may hold one
value per line OR several space-separated values — both renderings occur
depending on BetConstruct's CSS.  Non-numeric labels (Over/Under, team
names) are skipped/handled explicitly.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_RE_COMPACT_SCORE = re.compile(
    r"(\d{1,3})\s*:\s*(\d{1,3})\s*,\s*(\((?:\d{1,2}:\d{1,2})\)"
    r"(?:\s*,\s*\(\d{1,2}:\d{1,2}\))*)\s*(\d{1,2}:\d{2}|\d{1,2}`)?"
)
_RE_PERIOD = re.compile(r"^(1st|2nd|3rd|4th) Quarter|Half End|Half Time|Halftime|Quarter End")
_RE_CLOCK = re.compile(r"^(\d{1,2}:\d{2})'?$")
_RE_LINE = re.compile(r"^\d{2,3}\.\d$")
_RE_ODD = re.compile(r"^\d+(\.\d+)?$")
_RE_HANDICAP = re.compile(r"^[+-]\d{1,2}\.\d$")
_RE_QUARTER_SCORES = re.compile(r"\((\d{1,2}):(\d{1,2})\)")

SECTION_HEADERS = ("Points Handicap", "Total Points", "Match Winner")
TEAM_TOTAL_SUFFIX = "Total Points"


def _lines(text: str) -> list[str]:
    return [l.strip() for l in (text or "").split("\n") if l.strip()]


def _is_numeric(tok: str) -> bool:
    return bool(_RE_LINE.match(tok) or _RE_ODD.match(tok) or _RE_HANDICAP.match(tok))


def _tokenize(lines: list[str]) -> list[str]:
    """Flatten lines into tokens, dropping non-numeric labels (Over/Under)."""
    toks: list[str] = []
    for l in lines:
        for t in l.split():
            low = t.lower()
            if low in ("over", "under"):
                continue
            toks.append(t)
    return toks


def parse_period_label(text: str) -> tuple[str, Optional[int], Optional[str]]:
    """Return (period_label, quarter_number, clock) from page text."""
    period = ""
    quarter = None
    clock = None
    for l in _lines(text)[:60]:
        if not period and _RE_PERIOD.match(l):
            period = l
            m = re.match(r"(\d)", l)
            quarter = int(m.group(1)) if m else None
        m = _RE_CLOCK.match(l)
        if m and clock is None:
            clock = m.group(1)
        if period and clock:
            break
    if not clock:
        m = _RE_COMPACT_SCORE.search(text)
        if m and m.group(4):
            clock = m.group(4)
    return period, quarter, clock


def _extract_teams(lines: list[str]) -> tuple[Optional[str], Optional[str]]:
    """First two non-numeric, non-label lines = home/away team names."""
    names: list[str] = []
    for l in lines:
        if not l:
            continue
        if _is_numeric(l) or l.lower() in ("over", "under", "all", "match"):
            continue
        if len(l) < 3 or not re.search(r"[A-Za-z]{3,}", l):
            continue
        names.append(l)
        if len(names) == 2:
            break
    return (names[0] if names else None, names[1] if len(names) > 1 else None)


def parse_event_view(text: str) -> dict[str, Any]:
    """Parse event-view page text into a market observation dict.

    Returns fields consumable by MarketObservation plus ``markets_json``
    (full parsed market state) and ``raw_json`` (the raw page text).
    """
    lines = _lines(text)
    period, quarter, clock = parse_period_label(text)

    # ── Scoreboard: compact line "100 : 73, (32:22), ... 09:46" ──
    home_score = away_score = None
    quarter_scores: list[tuple[int, int]] = []
    m = _RE_COMPACT_SCORE.search(text)
    if m:
        home_score = int(m.group(1))
        away_score = int(m.group(2))
        quarter_scores = [
            (int(a), int(b)) for a, b in _RE_QUARTER_SCORES.findall(m.group(3))
        ]
        if not clock:
            clock = m.group(4)

    # ── Team names from scoreboard area ─────────────────────────
    home_team = away_team = ""
    for k, l in enumerate(lines[:30]):
        if _RE_CLOCK.match(l):
            if k + 1 < len(lines) and not _RE_PERIOD.match(lines[k + 1]):
                home_team = lines[k + 1]
            if k + 2 < len(lines):
                away_team = lines[k + 2]
            break

    # ── Market sections (token-stream) ──────────────────────────
    totals: dict[str, Any] = {}
    handicaps: dict[str, Any] = {}
    team_totals: dict[str, Any] = {}
    match_winner: dict[str, Any] = {}
    simulated_note = False

    n = len(lines)
    i = 0
    while i < n:
        l = lines[i]
        if "Simulated Game" in l:
            simulated_note = True
            i += 1
            continue

        # section header?
        header = None
        if l in SECTION_HEADERS:
            header = l
        elif l.endswith(TEAM_TOTAL_SUFFIX) and l != TEAM_TOTAL_SUFFIX:
            header = "team_total"

        if header is None:
            i += 1
            continue

        # collect following lines until the next section header
        j = i + 1
        body: list[str] = []
        while j < n:
            nl = lines[j]
            if nl in SECTION_HEADERS or (nl.endswith(TEAM_TOTAL_SUFFIX) and nl != TEAM_TOTAL_SUFFIX):
                break
            body.append(nl)
            j += 1

        if header == "Total Points":
            toks = _tokenize(body)
            ladder: list[dict] = []
            k = next((i for i, t in enumerate(toks) if _RE_LINE.match(t)), len(toks))
            while k + 2 < len(toks):
                a, b, c = toks[k], toks[k + 1], toks[k + 2]
                if _RE_LINE.match(a) and _RE_ODD.match(b) and _RE_ODD.match(c):
                    ladder.append({"line": float(a), "over": float(b), "under": float(c)})
                    k += 3
                else:
                    break
            if ladder:
                totals = {
                    "first_line": ladder[0]["line"],
                    "over_odds": ladder[0]["over"],
                    "under_odds": ladder[0]["under"],
                    "ladder": ladder,
                }
        elif header == "Points Handicap":
            home, away = _extract_teams(body)
            toks = _tokenize(body)
            ladder = []
            k = next((i for i, t in enumerate(toks) if _RE_HANDICAP.match(t)), len(toks))
            while k + 3 < len(toks):
                a, b, c, d = toks[k], toks[k + 1], toks[k + 2], toks[k + 3]
                if (_RE_HANDICAP.match(a) and _RE_ODD.match(b)
                        and _RE_HANDICAP.match(c) and _RE_ODD.match(d)):
                    ladder.append({
                        "home_line": float(a), "home_odds": float(b),
                        "away_line": float(c), "away_odds": float(d),
                    })
                    k += 4
                else:
                    break
            if ladder:
                handicaps = {
                    "home_team": home or "", "away_team": away or "",
                    "first_home_line": ladder[0]["home_line"],
                    "first_home_odds": ladder[0]["home_odds"],
                    "first_away_line": ladder[0]["away_line"],
                    "first_away_odds": ladder[0]["away_odds"],
                    "ladder": ladder,
                }
        elif header == "Match Winner":
            toks = _tokenize(body)
            odds = [float(t) for t in toks if _RE_ODD.match(t)][:2]
            if odds:
                match_winner = {
                    "home_odds": odds[0] if len(odds) > 0 else None,
                    "away_odds": odds[1] if len(odds) > 1 else None,
                }
        else:  # team_total
            team = l[: -len(TEAM_TOTAL_SUFFIX)].strip()
            toks = _tokenize(body)
            line = over = under = None
            k = next((i for i, t in enumerate(toks) if _RE_LINE.match(t)), len(toks))
            while k + 2 < len(toks):
                a, b, c = toks[k], toks[k + 1], toks[k + 2]
                if _RE_LINE.match(a) and _RE_ODD.match(b) and _RE_ODD.match(c):
                    line, over, under = float(a), float(b), float(c)
                    break
                k += 1
            team_totals[team] = {"line": line, "over": over, "under": under}

        i = j

    return {
        "home_team": home_team,
        "away_team": away_team,
        "home_score": home_score,
        "away_score": away_score,
        "period_label": period,
        "quarter": quarter,
        "clock": clock,
        "quarter_scores": quarter_scores,
        "simulated_note": simulated_note,
        "total": totals,
        "handicap": handicaps,
        "team_totals": team_totals,
        "match_winner": match_winner,
        "raw_json": json.dumps(text),
        "markets_json": json.dumps({
            "total": totals,
            "handicap": handicaps,
            "team_totals": team_totals,
            "match_winner": match_winner,
            "quarter_scores": quarter_scores,
            "simulated_note": simulated_note,
        }, default=str),
    }
