"""Wikipedia adapter: who actually won each primary.

Wikipedia's per-state articles ("2026 United States House of Representatives
elections in Ohio") are the most current free source for primary *results*,
which is the fact the FEC cannot supply.

Table layouts on these articles are not standardised - they drift between
states and get rewritten mid-cycle - so the parser targets the invariant
rather than a fixed column order: a row that names a district, containing
cells that name candidates tagged with a party. Anything it cannot parse is
reported as a coverage gap rather than silently dropped, because a silent drop
here looks identical to "this district has no Democrat running".
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Iterator, Optional

from bs4 import BeautifulSoup, Tag

from ..models import Candidate, District, NominationStatus
from ..net import Fetcher
from ..statefacts import AT_LARGE_STATES, SEAT_COUNTS, ballot_rule

log = logging.getLogger(__name__)

API = "https://en.wikipedia.org/w/api.php"

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

DEM_MARKERS = re.compile(r"\bdemocratic\b|\bdemocrat\b|\(D\)|\bDFL\b|\bD-", re.IGNORECASE)
OTHER_PARTY = re.compile(
    r"\brepublican\b|\blibertarian\b|\bgreen\s+party\b|\bindependent\b|"
    r"\bconstitution\b|\(R\)|\(L\)|\(G\)|\(I\)",
    re.IGNORECASE,
)

#: Row annotations that mean this person is NOT the general-election candidate.
LOST_MARKERS = re.compile(r"\b(lost|defeated|withdrew|eliminated|did\s+not\s+advance)\b", re.IGNORECASE)
WON_MARKERS = re.compile(r"\b(won|nominee|advanced|nominated|unopposed)\b", re.IGNORECASE)


def article_title(state: str) -> str:
    return f"2026 United States House of Representatives elections in {STATE_NAMES[state.upper()]}"


def fetch_state_html(fetcher: Fetcher, state: str) -> Optional[str]:
    """Fetch the rendered HTML of a state's 2026 House elections article."""
    title = article_title(state)
    url = (
        f"{API}?action=parse&format=json&prop=text&redirects=1"
        f"&page={title.replace(' ', '%20')}"
    )
    resp = fetcher.get(url)
    if not resp.ok:
        log.warning("wikipedia: %s -> HTTP %s", title, resp.status)
        return None
    try:
        blob = json.loads(resp.text)
    except json.JSONDecodeError:
        return None
    if "error" in blob:
        log.warning("wikipedia: %s -> %s", title, blob["error"].get("info"))
        return None
    return blob.get("parse", {}).get("text", {}).get("*")


@dataclass
class ParsedRow:
    district_number: Optional[int]
    at_large: bool
    democrats: list[str]
    raw: str


def _cell_text(cell: Tag) -> str:
    return re.sub(r"\s+", " ", cell.get_text(" ", strip=True))


def _split_lines(cell: Tag) -> list[Tag]:
    """Split a table cell into per-candidate fragments on <br> boundaries.

    Wikipedia packs every candidate for a district into one cell, one per
    line. Treating the cell as a unit mixes parties together and lets a single
    "lost primary" annotation suppress the whole district, so party and
    elimination have to be decided per line.
    """
    from bs4 import BeautifulSoup as _BS

    groups: list[list] = [[]]
    for child in cell.children:
        if getattr(child, "name", None) == "br":
            groups.append([])
        else:
            groups[-1].append(child)

    lines: list[Tag] = []
    for group in groups:
        if not group:
            continue
        frag = _BS("<div></div>", "lxml").div
        for node in group:
            frag.append(node.__copy__() if hasattr(node, "__copy__") else str(node))
        lines.append(frag)
    return lines


def _name_from_line(line: Tag) -> Optional[str]:
    """Pull the person's name out of a single candidate line."""
    for a in line.find_all("a"):
        text = a.get_text(" ", strip=True)
        title = a.get("title", "")
        if not text or len(text) < 4:
            continue
        if re.search(r"\b(election|primary|district|party|congress)\b", title, re.IGNORECASE):
            continue
        return text

    text = _cell_text(line)
    text = re.sub(r"\([^)]*\)", " ", text)          # (Democratic)
    text = re.sub(r"\b\d[\d,.%]*\b", " ", text)      # vote counts
    text = re.sub(r"[✔✓†*—–-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,·")
    if 4 <= len(text) <= 60 and " " in text:
        return text
    return None


@dataclass
class CandidateLine:
    name: str
    is_democrat: bool
    eliminated: bool


def _candidate_lines(cell: Tag) -> list[CandidateLine]:
    """Every candidate named in a cell, with party and elimination flags."""
    out: list[CandidateLine] = []
    dem_coded_cell = _cell_is_dem_coded(cell)
    for line in _split_lines(cell):
        text = _cell_text(line)
        if not text:
            continue
        has_dem = bool(DEM_MARKERS.search(text))
        has_other = bool(OTHER_PARTY.search(text))
        # An explicit other-party tag wins over an inherited cell colour.
        if has_other and not has_dem:
            is_dem = False
        elif has_dem:
            is_dem = True
        else:
            is_dem = dem_coded_cell
        name = _name_from_line(line)
        if not name:
            continue
        out.append(
            CandidateLine(
                name=name,
                is_democrat=is_dem,
                eliminated=bool(LOST_MARKERS.search(text)),
            )
        )
    return out


def parse_district_number(text: str, state: str) -> tuple[Optional[int], bool]:
    if re.search(r"at[\s\-]?large", text, re.IGNORECASE):
        return 1, True
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", text)
    if not m:
        return None, False
    num = int(m.group(1))
    if not 1 <= num <= SEAT_COUNTS.get(state.upper(), 53):
        return None, False
    return num, state.upper() in AT_LARGE_STATES


def parse_rows(html: str, state: str) -> list[ParsedRow]:
    """Extract (district, Democratic candidates) pairs from every table."""
    soup = BeautifulSoup(html, "lxml")
    rows: list[ParsedRow] = []

    for table in soup.find_all("table"):
        headers = [_cell_text(th).lower() for th in table.find_all("th")[:12]]
        if not any("district" in h for h in headers):
            continue
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            row_text = _cell_text(tr)
            num, at_large = parse_district_number(_cell_text(cells[0]), state)
            if num is None:
                continue

            dems: list[str] = []
            for cell in cells[1:]:
                for entry in _candidate_lines(cell):
                    if entry.is_democrat and not entry.eliminated:
                        dems.append(entry.name)

            rows.append(ParsedRow(num, at_large, _dedupe(dems), row_text[:300]))
    return rows


def _cell_is_dem_coded(cell: Tag) -> bool:
    """Wikipedia colours party cells; the class or style often encodes it."""
    style = (cell.get("style") or "").lower()
    classes = " ".join(cell.get("class") or []).lower()
    return "democratic" in classes or "3333ff" in style or "0044c9" in style


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        k = n.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(n.strip())
    return out


def candidates_for_state(
    fetcher: Fetcher, state: str, status: NominationStatus
) -> tuple[list[Candidate], list[str]]:
    """Return (candidates, coverage_gap_messages) for one state."""
    st = state.upper()
    html = fetch_state_html(fetcher, st)
    if html is None:
        return [], [f"{st}: could not retrieve Wikipedia article"]

    rows = parse_rows(html, st)
    found: dict[int, list[str]] = {}
    for row in rows:
        if row.district_number is None:
            continue
        found.setdefault(row.district_number, [])
        for name in row.democrats:
            if name not in found[row.district_number]:
                found[row.district_number].append(name)

    out: list[Candidate] = []
    rule = ballot_rule(st)
    at_large = st in AT_LARGE_STATES
    for num, names in sorted(found.items()):
        district = District(st, num, ballot_rule=rule, at_large=at_large)
        for name in names:
            cand = Candidate(full_name=name, district=district, status=status)
            cand.add_provenance(
                "wikipedia",
                f"https://en.wikipedia.org/wiki/{article_title(st).replace(' ', '_')}",
            )
            out.append(cand)

    gaps: list[str] = []
    missing = sorted(set(range(1, SEAT_COUNTS[st] + 1)) - set(found))
    if missing:
        gaps.append(
            f"{st}: no Democratic candidate parsed for district(s) "
            + ", ".join(str(m) for m in missing)
        )
    return out, gaps
