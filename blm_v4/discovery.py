"""
BLM V4 — PokerBet Live-Panel Discovery.

Parses the PokerBet (BetConstruct) sports page DOM to discover live
basketball games grouped by competition.

The left navigation panel renders the whole live sports tree:

    div.sp-s-l-b-content-wrp.verticalNavigationContent   (sport level)
      div.sp-sub-list-bc                                  (sport: Basketball)
        div.sp-s-l-head-bc > p.sp-s-l-h-title-bc "Basketball"
        div.sp-s-l-b-content-wrp                          (region level)
          div.sp-sub-list-bc                              (region: World)
            div.sp-s-l-head-bc "World" + count
            div.sp-s-l-b-content-wrp                      (competition level)
              div.sp-sub-list-bc                          (competition)
                div.sp-s-l-head-bc "Cyber Basketball. 2K26 Matches" + count
                div.market-game-section                   (game row)
                  p.market-game-team > span.market-game-team-name + b.market-game-odd
                  div.market-game-part-container > span.market-game-part + b
                  div.market-game-additional-info-container > span + time
                  div.market-group-holder-bc > div.market-group-item-bc
                    span.market-name-bc ("W1") + span.market-odd-bc ("1.55")

This module is a PURE function of the HTML — it can run against frozen
fixture HTML in tests with no browser.  Uses stdlib html.parser only
(no external dependencies — keeps BLM independently deployable).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Optional

from blm_v4.classifications import Classification, classify_competition

SPORT_HEADER_CLS = "sp-s-l-h-title-bc"
SECTION_CLS = "sp-sub-list-bc"
GAME_ROW_CLS = "market-game-section"
TEAM_NAME_CLS = "market-game-team-name"
TEAM_ODD_CLS = "market-game-odd"
PERIOD_CLS = "market-game-part"
INFO_CLS = "market-game-additional-info"
TIME_CLS = "market-game-additional-info-time"
ODD_CLS = "market-odd-bc"
NAME_CLS = "market-name-bc"

_RE_NUM = re.compile(r"^\d+$")
_RE_ODD = re.compile(r"^\d+(\.\d+)?$")


# ── Minimal DOM (stdlib only) ──────────────────────────────────────

class _Node:
    __slots__ = ("tag", "attrs", "children", "text", "parent")

    def __init__(self, tag: str, attrs: list, parent: Optional["_Node"] = None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children: list[_Node] = []
        self.text = ""
        self.parent = parent

    def get(self, key: str, default: Any = None) -> Any:
        return self.attrs.get(key, default)

    def has_class(self, *names: str) -> bool:
        cls = self.attrs.get("class", "")
        parts = cls.split() if isinstance(cls, str) else []
        return all(n in parts for n in names)

    def find_all(self, tag: Optional[str] = None, class_: Optional[str] = None) -> list["_Node"]:
        out: list[_Node] = []
        for c in self.children:
            if (tag is None or c.tag == tag) and (class_ is None or c.has_class(class_)):
                out.append(c)
            out.extend(c.find_all(tag=tag, class_=class_))
        return out

    def find(self, tag: Optional[str] = None, class_: Optional[str] = None) -> Optional["_Node"]:
        for c in self.children:
            if (tag is None or c.tag == tag) and (class_ is None or c.has_class(class_)):
                return c
        for c in self.children:
            r = c.find(tag=tag, class_=class_)
            if r is not None:
                return r
        return None

    def find_parent(self, tag: Optional[str] = None, class_: Optional[str] = None) -> Optional["_Node"]:
        p = self.parent
        while p is not None:
            if (tag is None or p.tag == tag) and (class_ is None or p.has_class(class_)):
                return p
            p = p.parent
        return None

    def get_text(self, strip: bool = False) -> str:
        parts: list[str] = []

        def walk(n: _Node) -> None:
            if n.text:
                parts.append(n.text)
            for c in n.children:
                walk(c)

        walk(self)
        t = "".join(parts)
        return t.strip() if strip else t


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", [])

    def feed(self, data: str):  # type: ignore[override]
        self._stack: list[_Node] = [self.root]
        super().feed(data)
        return self.root

    def handle_starttag(self, tag: str, attrs: list) -> None:
        node = _Node(tag, attrs, parent=self._stack[-1])
        self._stack[-1].children.append(node)
        self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        node = _Node(tag, attrs, parent=self._stack[-1])
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        if len(self._stack) > 1:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._stack:
            self._stack[-1].text += data


# ── Parsed row model ───────────────────────────────────────────────

@dataclass
class RowGame:
    """A game row as rendered in the live panel (list-level state)."""

    home_team: str = ""
    away_team: str = ""
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    period_label: str = ""
    spread_indicator: Optional[str] = None
    score_detail: str = ""        # "78 : 76, (29:25), (28:25), (21:26) 04:15"
    clock: Optional[str] = None
    start_time: Optional[str] = None
    w1_odds: Optional[float] = None
    w2_odds: Optional[float] = None
    odds_raw: list[str] = field(default_factory=list)


@dataclass
class DiscoveredCompetition:
    """A competition section found in the live panel."""

    region: str = ""
    sport: str = ""
    display_name: str = ""
    game_count_text: str = ""
    classification: Classification = Classification.UNKNOWN
    games: list[RowGame] = field(default_factory=list)

    @property
    def classification_value(self) -> str:
        return self.classification.value


# ── Parsing helpers ────────────────────────────────────────────────

def _header_title(section: _Node) -> Optional[str]:
    head = section.find("div", class_="sp-s-l-head-bc")
    if not head:
        return None
    p = head.find("p", class_=SPORT_HEADER_CLS)
    return p.get_text(strip=True) if p else head.get_text(strip=True)


def _header_count(section: _Node) -> str:
    head = section.find("div", class_="sp-s-l-head-bc")
    if not head:
        return ""
    txt = head.get_text(strip=True)
    m = re.search(r"(\d+)\s*$", txt)
    return m.group(1) if m else ""


def _section_sport_region(section: _Node) -> tuple[str, str]:
    """Walk up to find the enclosing sport and region titles."""
    sport, region = "", ""
    parent_wrp = section.find_parent("div", class_="sp-s-l-b-content-wrp")
    if parent_wrp:
        region_section = parent_wrp.find_parent("div", class_=SECTION_CLS)
        if region_section:
            region = _header_title(region_section) or ""
            sport_wrp = region_section.find_parent("div", class_="sp-s-l-b-content-wrp")
            if sport_wrp:
                sport_section = sport_wrp.find_parent("div", class_=SECTION_CLS)
                if sport_section:
                    sport = _header_title(sport_section) or ""
    return sport, region


def _parse_game_row(row: _Node) -> Optional[RowGame]:
    teams = row.find_all("p", class_="market-game-team")
    if len(teams) < 2:
        return None

    def _team_name(p: _Node) -> str:
        span = p.find("span", class_=TEAM_NAME_CLS)
        return span.get_text(strip=True) if span else ""

    def _team_score(p: _Node) -> Optional[int]:
        b = p.find("b", class_=TEAM_ODD_CLS)
        if not b:
            return None
        txt = b.get_text(strip=True)
        return int(txt) if _RE_NUM.match(txt) else None

    home = _team_name(teams[0])
    away = _team_name(teams[1])
    if not home and not away:
        return None

    part = row.find("span", class_=PERIOD_CLS)
    period = part.get_text(strip=True) if part else ""

    spread = None
    part_cont = row.find("div", class_="market-game-part-container")
    if part_cont:
        b = part_cont.find("b")
        if b:
            spread = b.get_text(strip=True) or None

    info_span = row.find("span", class_=INFO_CLS)
    detail = info_span.get_text(strip=True) if info_span else ""
    clock = _extract_clock(detail)

    time_el = row.find("time", class_=TIME_CLS)
    start = time_el.get_text(strip=True) if time_el else None

    w1 = w2 = None
    names = row.find_all("span", class_=NAME_CLS)
    odds = row.find_all("span", class_=ODD_CLS)
    odds_raw: list[str] = []
    for o in odds:
        txt = o.get_text(strip=True)
        if _RE_ODD.match(txt or ""):
            odds_raw.append(txt)
    odds_iter = iter(odds_raw)
    for n in names:
        label = n.get_text(strip=True).upper()
        try:
            val = float(next(odds_iter))
        except (StopIteration, ValueError):
            break
        if label == "W1" and w1 is None:
            w1 = val
        elif label == "W2" and w2 is None:
            w2 = val

    return RowGame(
        home_team=home, away_team=away,
        home_score=_team_score(teams[0]), away_score=_team_score(teams[1]),
        period_label=period, spread_indicator=spread,
        score_detail=detail, clock=clock, start_time=start,
        w1_odds=w1, w2_odds=w2, odds_raw=odds_raw,
    )


def _extract_clock(detail: str) -> Optional[str]:
    """Extract game clock from the detail string.

    Formats seen: '09:46' (MM:SS), '71`' (minutes), trailing '04:15'.
    """
    m = re.search(r"(\d{1,2}:\d{2})\s*$", detail)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{1,2}:\d{2})\b", detail)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{1,2})`\s*$", detail)
    if m:
        return m.group(1) + ":00"
    return None


# ── Public API ─────────────────────────────────────────────────────

def discover_competitions(html: str) -> list[DiscoveredCompetition]:
    """Parse the live-panel HTML into discovered competitions + game rows.

    Pure function — no browser, no network.  Fixture-replayable.
    """
    root = _TreeBuilder().feed(html or "")
    out: list[DiscoveredCompetition] = []

    for section in root.find_all("div", class_=SECTION_CLS):
        # rows belonging DIRECTLY to this section (not nested sections)
        rows = [
            r for r in section.find_all("div", class_=GAME_ROW_CLS)
            if r.find_parent("div", class_=SECTION_CLS) is section
        ]
        if not rows:
            continue
        title = _header_title(section)
        if not title:
            continue
        sport, region = _section_sport_region(section)
        count = _header_count(section)
        classification = classify_competition(display_name=title, region=region)
        games = [g for g in (_parse_game_row(r) for r in rows) if g]
        out.append(DiscoveredCompetition(
            region=region, sport=sport, display_name=title,
            game_count_text=count, classification=classification, games=games,
        ))

    return out


def find_relevant_competitions(
    html: str,
    include: Optional[list[Classification]] = None,
) -> list[DiscoveredCompetition]:
    """Return competitions whose classification is in ``include``."""
    if include is None:
        include = [Classification.CYBER_2K26, Classification.BETUAL_NBA]
    comps = discover_competitions(html)
    return [c for c in comps if c.classification in include]


def to_dict(comp: DiscoveredCompetition) -> dict[str, Any]:
    return {
        "region": comp.region,
        "sport": comp.sport,
        "display_name": comp.display_name,
        "game_count": comp.game_count_text,
        "classification": comp.classification.value,
        "games": [g.__dict__ for g in comp.games],
    }
