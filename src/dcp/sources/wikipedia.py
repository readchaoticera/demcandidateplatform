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

import logging
import re
from dataclasses import dataclass
from typing import Iterator, Optional

from bs4 import BeautifulSoup, Tag

from ..models import BallotRule, Candidate, District, NominationStatus
from ..net import Fetcher, FetchError
from ..statefacts import AT_LARGE_STATES, SEAT_COUNTS, ballot_rule

log = logging.getLogger(__name__)

BASE = "https://en.wikipedia.org/wiki"

# Wikipedia's robots.txt disallows /w/ and /api/ for generic user agents, which
# rules out the MediaWiki parse API. The ordinary /wiki/<Article> path is
# explicitly allowed, serves the same rendered content, and at ~50 fetches puts
# negligible load on them. So we read articles the way a reader would.

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
LOST_MARKERS = re.compile(
    r"\b(lost|defeated|withdrew|withdrawn|eliminated|disqualified|"
    r"did\s+not\s+(?:advance|file|qualify))\b",
    re.IGNORECASE,
)
WON_MARKERS = re.compile(r"\b(won|nominee|advanced|nominated|unopposed)\b", re.IGNORECASE)


def article_title(state: str, plural: bool = True) -> str:
    noun = "elections" if plural else "election"
    return (
        f"2026 United States House of Representatives {noun} in "
        f"{STATE_NAMES[state.upper()]}"
    )


def article_url(state: str, plural: bool = True) -> str:
    return f"{BASE}/{article_title(state, plural).replace(' ', '_')}"


def fetch_state_html(fetcher: Fetcher, state: str) -> Optional[str]:
    """Fetch a state's 2026 House elections article.

    Single-seat states title the article "...election in Delaware" (singular),
    so both forms are tried before giving up.
    """
    for plural in (True, False):
        url = article_url(state, plural)
        try:
            resp = fetcher.get(url)
        except FetchError as exc:
            log.warning("wikipedia: %s -> %s", url, exc)
            continue
        if resp.ok:
            return resp.text
        log.debug("wikipedia: %s -> HTTP %s", url, resp.status)
    log.warning("wikipedia: no article found for %s", state)
    return None


# --- section splitting -----------------------------------------------------

DISTRICT_HEADING = re.compile(r"^District\s+(\d{1,2})\b|^At[\s-]?large", re.IGNORECASE)

#: Placeholder names that are not people. Wikipedia uses these before a primary.
PLACEHOLDER = re.compile(
    r"^(TBD|TBA|To be determined|To be decided|Undecided|Vacant|None|N/?A)$",
    re.IGNORECASE,
)

#: Party labels vary far more than expected across states and templates:
#: "Democratic", "Democratic (DFL)", "Democratic-Farmer-Labor", "Democratic-NPL",
#: fusion tickets like "Democratic / Working Families". Rather than enumerate
#: them, the label is normalised and matched on a token.
_PAREN = re.compile(r"[()\[\]]")
_DASHES = re.compile(r"[\u2010-\u2015]")
_DEM_TOKEN = re.compile(r"\bDemocrat(ic)?\b|\bDFL\b", re.IGNORECASE)
_REP_TOKEN = re.compile(r"\bRepublican\b", re.IGNORECASE)


def is_democratic_party(label: str) -> bool:
    """Whether a Wikipedia party label denotes the Democratic Party.

    Unwraps parentheticals rather than discarding them, so "Democratic (DFL)"
    still reads as Democratic. Guards against "Republican" so a fusion or
    combined label cannot be misread.
    """
    text = _DASHES.sub("-", _PAREN.sub(" ", label or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return False
    if _REP_TOKEN.search(text) and not _DEM_TOKEN.search(text):
        return False
    return bool(_DEM_TOKEN.search(text))


#: Infobox row labels that introduce the list of general-election candidates.
#: Top-two and jungle states say "Candidate" because no nomination occurs.
NAME_ROW_LABELS = {"nominee", "candidate"}


@dataclass
class ParsedRow:
    """One Democrat found in a district section."""

    district_number: Optional[int]
    at_large: bool
    democrats: list[str]
    raw: str = ""


def _section_nodes(heading: Tag) -> list[Tag]:
    """Every element belonging to a heading's section, up to the next h2."""
    parent = heading.parent
    anchor = heading
    if parent is not None and "mw-heading" in (parent.get("class") or []):
        anchor = parent
    nodes: list[Tag] = []
    for sib in anchor.next_siblings:
        if not isinstance(sib, Tag):
            continue
        if sib.name == "h2" or "mw-heading2" in (sib.get("class") or []):
            break
        nodes.append(sib)
    return nodes


def _tables(nodes: list[Tag]):
    for node in nodes:
        if node.name == "table":
            yield node
        else:
            yield from node.find_all("table")


def clean_name(raw: str) -> Optional[str]:
    """Strip annotations from an infobox name, or reject it as a placeholder."""
    name = re.sub(r"\([^)]*\)", " ", raw)          # "(presumptive)", "(incumbent)"
    name = re.sub(r"\[[^\]]*\]", " ", name)         # footnote markers
    name = re.sub(r"\s+", " ", name).strip(" ,\u2013-")
    if not name or PLACEHOLDER.match(name):
        return None
    if not 3 <= len(name) <= 60 or " " not in name:
        return None
    return name


def infobox_candidates(nodes: list[Tag]) -> list[tuple[str, str]]:
    """Zip a district infobox's name row with its party row.

    Returns [(name, party)]. Wikipedia renders these as two parallel rows,
    which is why they are read positionally rather than per-candidate.
    """
    for tbl in _tables(nodes):
        if "infobox" not in " ".join(tbl.get("class") or []):
            continue
        names: Optional[list[str]] = None
        parties: Optional[list[str]] = None
        for tr in tbl.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(" ", strip=True).lower()
            values = [c.get_text(" ", strip=True) for c in cells[1:]]
            if not any(values):
                continue
            if label in NAME_ROW_LABELS and names is None:
                names = values
            elif label == "party" and parties is None:
                parties = values
        if names and parties:
            return list(zip(names, parties))
    return []


def democratic_primary_candidates(nodes: list[Tag]) -> list[tuple[str, int]]:
    """(name, votes) from `Party | Candidate | Votes | %` results tables.

    Used where a district has no general-election infobox yet. Votes allow the
    winner to be identified; -1 means the cell was not a number.
    """
    out: list[tuple[str, int]] = []
    for tbl in _tables(nodes):
        headers = [th.get_text(" ", strip=True) for th in tbl.find_all("th")[:4]]
        if headers[:2] != ["Party", "Candidate"]:
            continue
        for tr in tbl.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            cells = [c for c in cells if c]
            if len(cells) < 3 or not is_democratic_party(cells[0]):
                continue
            # clean_name() strips parentheticals, which is where Wikipedia
            # puts "(withdrawn)". Test the raw cell first, or a candidate who
            # topped the poll and then quit reads as the nominee.
            if LOST_MARKERS.search(cells[1]):
                continue
            name = clean_name(cells[1])
            if not name:
                continue
            digits = cells[2].replace(",", "")
            out.append((name, int(digits) if digits.isdigit() else -1))
    return out


#: Caption text that identifies a general-election results table rather than a
#: primary one. Both use the same `Party | Candidate | ...` shape.
_GENERAL_CAPTION = re.compile(
    r"congressional\s+district\s+election|general\s+election", re.IGNORECASE
)


def general_election_candidates(nodes: list[Tag]) -> list[str]:
    """Democrats listed in a district's *general election* results table.

    Needed for Alaska, whose top-four primary sends several candidates of the
    same party forward. There the top Democratic primary vote-getter is not
    "the nominee" - there is no nominee - so the primary table cannot answer
    who is on the November ballot. The general-election table lists exactly
    the people who are, with vote columns still empty before the election.
    """
    out: list[str] = []
    for tbl in _tables(nodes):
        caption = tbl.find("caption")
        headers = [th.get_text(" ", strip=True) for th in tbl.find_all("th")[:4]]
        label = caption.get_text(" ", strip=True) if caption else " ".join(headers)
        if not _GENERAL_CAPTION.search(label):
            continue
        for tr in tbl.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            cells = [c for c in cells if c]
            if len(cells) < 2 or not is_democratic_party(cells[0]):
                continue
            if LOST_MARKERS.search(cells[1]):
                continue
            name = clean_name(cells[1])
            if name and name not in out:
                out.append(name)
    return out


#: "Lauren Jewett (D)" in a campaign-finance table.
FINANCE_NAME = re.compile(r"^(?P<name>.+?)\s*\((?P<party>[A-Z]{1,3})\)\s*$")


def finance_table_candidates(nodes: list[Tag]) -> list[str]:
    """Democrats named in a district's campaign-finance table.

    Only safe where every filer appears on the general-election ballot, i.e.
    Louisiana's all-party November election. Everywhere else these tables also
    list primary losers, so using them would inflate the roster.
    """
    out: list[str] = []
    for tbl in _tables(nodes):
        headers = [th.get_text(" ", strip=True) for th in tbl.find_all("th")[:6]]
        if not any(h.startswith("Campaign finance") for h in headers):
            continue
        for th in tbl.find_all(["th", "td"]):
            match = FINANCE_NAME.match(th.get_text(" ", strip=True))
            if not match or match.group("party") != "D":
                continue
            name = clean_name(match.group("name"))
            if name and name not in out:
                out.append(name)
    return out


def parse_rows(html: str, state: str) -> list[ParsedRow]:
    """Extract each district's Democratic general-election candidates."""
    soup = BeautifulSoup(html, "lxml")
    st = state.upper()
    seats = SEAT_COUNTS[st]
    rows: list[ParsedRow] = []

    headings = [
        h for h in soup.find_all("h2")
        if DISTRICT_HEADING.match(h.get_text(" ", strip=True))
    ]

    # Single-seat states have no per-district sections; the article is the district.
    if not headings and seats == 1:
        body = soup.find("div", class_="mw-parser-output") or soup
        rows.append(_row_from_section(1, st, [body]))
        return rows

    for heading in headings:
        text = heading.get_text(" ", strip=True)
        match = DISTRICT_HEADING.match(text)
        if not match:
            continue
        if match.group(1):
            number = int(match.group(1))
            if not 1 <= number <= seats:
                continue
        else:
            number = 1  # at-large
        rows.append(_row_from_section(number, st, _section_nodes(heading), text))
    return rows


def _row_from_section(
    number: int, state: str, nodes: list[Tag], raw: str = ""
) -> ParsedRow:
    at_large = state in AT_LARGE_STATES
    dems: list[str] = []

    for raw_name, party in infobox_candidates(nodes):
        if not is_democratic_party(party):
            continue
        name = clean_name(raw_name)
        if name and name not in dems:
            dems.append(name)

    # Top-four states run a blanket primary: several Democrats can advance and
    # none of them is a "nominee", so the primary table's leader is the wrong
    # answer. Read who actually appears on the November ballot instead.
    if not dems and ballot_rule(state) is BallotRule.TOP_FOUR_RCV:
        dems.extend(general_election_candidates(nodes))

    # No infobox (or no Democrat in it): fall back to the primary results table
    # and take the top vote-getter, which is the nominee.
    if not dems:
        primary = democratic_primary_candidates(nodes)
        if primary:
            winner = max(primary, key=lambda kv: kv[1])
            if winner[1] >= 0:
                dems.append(winner[0])

    # Louisiana holds no nominating primary, so no district has a nominee
    # infobox. Every filer goes on the November ballot, which makes the
    # campaign-finance table a valid roster there and only there.
    if not dems and ballot_rule(state) is BallotRule.JUNGLE_NOV:
        dems.extend(finance_table_candidates(nodes))

    return ParsedRow(number, at_large, dems, raw[:200])


# ---------------------------------------------------------------------------
# Campaign websites
# ---------------------------------------------------------------------------

#: Wikipedia's per-state "External links" section lists official campaign sites
#: as "Yolanda Prince (D)" or "Christina Bohannan (D) for Congress". This is a
#: far better source of campaign URLs than Ballotpedia, which serves an empty
#: HTTP 202 to automated clients.
CAMPAIGN_LINK = re.compile(
    r"^(?P<name>.+?)\s*\((?P<party>[A-Z]{1,3})\)"
    r"(?:\s+for\s+(?:Congress|U\.?S\.?\s+House|the\s+U\.?S\.?\s+House))?$"
)

_NON_CAMPAIGN_HOST = re.compile(
    r"(wikipedia\.org|wikimedia\.org|wikidata\.org|fec\.gov|ballotpedia\.org)",
    re.IGNORECASE,
)


def campaign_links(html: str) -> list[tuple[str, str, str]]:
    """Extract (name, party_letter, url) from the External links section.

    The section is flat for the whole state rather than per district, so the
    caller matches on name within the state.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    for heading in soup.find_all("h2"):
        if not heading.get_text(" ", strip=True).startswith("External links"):
            continue
        for node in _section_nodes(heading):
            for a in node.find_all("a", href=True):
                href = a["href"].strip()
                if not href.startswith("http") or _NON_CAMPAIGN_HOST.search(href):
                    continue
                match = CAMPAIGN_LINK.match(a.get_text(" ", strip=True))
                if not match:
                    continue
                name = clean_name(match.group("name"))
                if not name:
                    continue
                key = f"{name.lower()}|{href}"
                if key in seen:
                    continue
                seen.add(key)
                out.append((name, match.group("party"), href))
        break
    return out


def democratic_campaign_urls(html: str) -> dict[str, str]:
    """{candidate name: campaign URL} for Democrats only."""
    return {
        name: url for name, party, url in campaign_links(html) if party == "D"
    }


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
            cand.add_provenance("wikipedia", article_url(st))
            out.append(cand)

    gaps: list[str] = []
    missing = sorted(set(range(1, SEAT_COUNTS[st] + 1)) - set(found))
    if missing:
        gaps.append(
            f"{st}: no Democratic candidate parsed for district(s) "
            + ", ".join(str(m) for m in missing)
        )
    return out, gaps
