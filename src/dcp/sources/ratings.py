"""Cook Political Report House race ratings, sourced via Wikipedia.

cookpolitical.com cannot be read directly. Its robots.txt names the ratings
dataset as proprietary, and the ``/ratings/`` pages sit behind a Cloudflare
challenge that returns 403 to any automated client; the file itself says hard
enforcement happens at the edge. Working around that challenge would be
evasion, so this module does not try.

Wikipedia republishes the ratings under CC BY-SA with per-rating citations
back to Cook, which is the ordinary way these numbers circulate. Two articles
between them cover the whole House:

*   "2026 United States House of Representatives election ratings" carries one
    table of the ~155 seats any rater calls competitive, refreshed within days.
*   Each state's own article carries a per-district ``Source | Ranking | As of``
    table, which is where the safe seats are.

The national table wins where both have a district, because it is the more
recently updated of the two. Districts in neither are left unrated rather than
assumed safe: "no rater has published a rating" and "every rater says Solid"
are different facts, and inferring the second from the first would invent data.

These ratings are Cook's editorial product. Attribute them to Cook and link
back; do not present them as this project's own assessment.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup, Tag

from ..net import Fetcher
from ..statefacts import AT_LARGE_STATES, SEAT_COUNTS
from .wikipedia import STATE_NAMES, _section_nodes, _tables, DISTRICT_HEADING, fetch_state_html

log = logging.getLogger(__name__)

NATIONAL_URL = (
    "https://en.wikipedia.org/wiki/"
    "2026_United_States_House_of_Representatives_election_ratings"
)

#: Cook's scale, safe Democratic to safe Republican. "Tilt" is Inside
#: Elections' term rather than Cook's, but it is accepted rather than rounded
#: to a neighbour: if it turns up in a Cook column, recording it faithfully
#: beats quietly restating someone else's call as one of Cook's.
RATING_ORDER: tuple[str, ...] = (
    "Solid D", "Likely D", "Lean D", "Tilt D",
    "Toss Up",
    "Tilt R", "Lean R", "Likely R", "Solid R",
)

#: Rating -> (background, foreground). The conventional blue-to-red ramp, with
#: contrast against black text checked at each step.
RATING_COLORS: dict[str, str] = {
    "Solid D": "#1d4ed8",
    "Likely D": "#4f83e8",
    "Lean D": "#9dbdf5",
    "Tilt D": "#cfdffb",
    "Toss Up": "#d8d8d8",
    "Tilt R": "#f9d2d2",
    "Lean R": "#f4a3a3",
    "Likely R": "#e56a6a",
    "Solid R": "#c02626",
}

#: Wikipedia is not consistent about these: "Tossup", "Toss-up", "Safe R" and
#: "Solid R" all appear, and a "(flip)" suffix marks a predicted party change.
_FLIP = re.compile(r"\s*\((?:flip|hold)\)\s*$", re.IGNORECASE)
_NORMALISE = {
    "tossup": "Toss Up", "toss up": "Toss Up", "toss-up": "Toss Up",
    "safe d": "Solid D", "solid d": "Solid D",
    "likely d": "Likely D", "lean d": "Lean D", "tilt d": "Tilt D",
    "safe r": "Solid R", "solid r": "Solid R",
    "likely r": "Likely R", "lean r": "Lean R", "tilt r": "Tilt R",
}

_COOK = re.compile(r"\bCook\b", re.IGNORECASE)
_DATE = re.compile(r"[A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4}")


def normalise_rating(raw: str) -> Optional[str]:
    """Canonicalise one rating cell, or None if it is not a rating.

    Drops the "(flip)" annotation: whether a rating implies a party change is
    a function of who holds the seat, not a separate rating, and keeping it
    would split each level into two.
    """
    text = _FLIP.sub("", re.sub(r"\[[^\]]*\]", " ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return _NORMALISE.get(text.lower())


def district_code(label: str) -> Optional[str]:
    """"Alabama 2" -> "AL-02"; "Alaska at-large" -> "AK-AL"."""
    text = re.sub(r"\[[^\]]*\]", " ", label or "")
    text = re.sub(r"\s+", " ", text).strip()
    match = re.match(r"^(?P<state>[A-Za-z .]+?)\s+(?P<seat>at[-\s]?large|\d{1,2})$", text)
    if not match:
        return None
    name = match.group("state").strip().lower()
    code = next((k for k, v in STATE_NAMES.items() if v.lower() == name), None)
    if code is None:
        return None
    seat = match.group("seat").lower()
    if seat.startswith("at"):
        return f"{code}-AL" if code in AT_LARGE_STATES else None
    number = int(seat)
    if code in AT_LARGE_STATES:
        return f"{code}-AL" if number == 1 else None
    return f"{code}-{number:02d}" if 1 <= number <= SEAT_COUNTS[code] else None


def _cook_column(header_cells: list[Tag]) -> Optional[int]:
    """Index of the Cook column, found by its header rather than its position.

    The table carries a dozen raters and gains more as the cycle goes on, so
    an index counted once would silently start reading Sabato's column.
    """
    for i, cell in enumerate(header_cells):
        if _COOK.search(cell.get_text(" ", strip=True)):
            return i
    return None


def _as_of(header_cells: list[Tag], index: int) -> str:
    match = _DATE.search(header_cells[index].get_text(" ", strip=True))
    return match.group(0) if match else ""


def parse_national(html: str) -> dict[str, tuple[str, str]]:
    """{district code: (rating, as-of)} from the competitive-seats table."""
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, tuple[str, str]] = {}
    for table in soup.select("table.wikitable"):
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue
        header = rows[1].find_all(["th", "td"])
        index = _cook_column(header)
        if index is None:
            continue
        as_of = _as_of(header, index)
        for tr in rows[2:]:
            cells = tr.find_all(["th", "td"])
            if len(cells) <= index:
                continue
            code = district_code(cells[0].get_text(" ", strip=True))
            rating = normalise_rating(cells[index].get_text(" ", strip=True))
            if code and rating:
                out[code] = (rating, as_of)
        if out:
            break
    return out


def parse_state(html: str, state: str) -> dict[str, tuple[str, str]]:
    """{district code: (rating, as-of)} from one state's per-district tables."""
    soup = BeautifulSoup(html, "lxml")
    st = state.upper()
    seats = SEAT_COUNTS[st]
    out: dict[str, tuple[str, str]] = {}

    headings = [
        h for h in soup.find_all("h2")
        if DISTRICT_HEADING.match(h.get_text(" ", strip=True))
    ]
    sections: list[tuple[int, list[Tag]]] = []
    if not headings and seats == 1:
        sections.append((1, [soup.find("div", class_="mw-parser-output") or soup]))
    for heading in headings:
        match = DISTRICT_HEADING.match(heading.get_text(" ", strip=True))
        number = int(match.group(1)) if match.group(1) else 1
        if 1 <= number <= seats:
            sections.append((number, _section_nodes(heading)))

    for number, nodes in sections:
        code = f"{st}-AL" if st in AT_LARGE_STATES else f"{st}-{number:02d}"
        for table in _tables(nodes):
            rows = [
                [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                for tr in table.find_all("tr")
            ]
            if not rows or rows[0][:2] != ["Source", "Ranking"]:
                continue
            for row in rows[1:]:
                if len(row) < 2 or not _COOK.search(row[0]):
                    continue
                rating = normalise_rating(row[1])
                if rating:
                    out[code] = (rating, row[2] if len(row) > 2 else "")
    return out


def fetch_all(fetcher: Fetcher, states: Optional[list[str]] = None) -> dict[str, tuple[str, str]]:
    """Every district's Cook rating, national table first then per-state.

    Merged in that order because the national table is refreshed within days
    while a safe seat's state-article row can be a year old; where both carry
    a district, the fresher call is the right one.
    """
    ratings: dict[str, tuple[str, str]] = {}

    resp = fetcher.get(NATIONAL_URL)
    if resp.ok:
        ratings.update(parse_national(resp.text))
        log.info("ratings: %d competitive seats from the national table", len(ratings))
    else:
        log.warning("ratings: national table unavailable (HTTP %s)", resp.status)

    filled = 0
    for state in states or sorted(SEAT_COUNTS):
        html = fetch_state_html(fetcher, state)
        if html is None:
            continue
        for code, value in parse_state(html, state).items():
            if code not in ratings:
                ratings[code] = value
                filled += 1
    log.info("ratings: %d more from per-state articles, %d districts rated",
             filled, len(ratings))
    return ratings
