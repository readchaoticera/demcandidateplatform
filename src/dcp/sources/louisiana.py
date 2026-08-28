"""Louisiana's certified qualifying list, from the Secretary of State.

Louisiana is the one state where the roster cannot come from primary results,
because there is no nominating primary: in 2026 the all-party ballot *is* the
November election. Everyone who qualified appears on it, so the roster is the
qualifying list and nothing else.

Wikipedia is the wrong source for that. Its per-district "Declared" sections
list people who announced a run, which is a superset of those who qualified and
also mislabels party in places: of the four Democrats it lists for LA-01, only
one filed, and the candidate it gives as Democratic in LA-02 qualified No
Party. The campaign-finance fallback the pipeline used before is the opposite
error, listing only candidates who raised enough money to appear in a finance
table.

The Secretary of State publishes the certified list through the Voter Portal's
candidate inquiry, with each candidate's filing date and the party they
qualified under. That is the ballot.

Access notes, since two of this project's sources are blocked and this one is
not: ``www.sos.la.gov`` serves ``Disallow: /`` to every agent but a handful of
named search engines, so nothing there is fetchable. The Voter Portal is
different. Its robots.txt disallows ``/CandidateInquiry/Parish/`` and
``/CandidateInquiry/Statewide/`` - legacy paths its own comment says generate
error reports - while the endpoints the page actually calls,
``/CandidateInquiry/StatewideCandidate/*``, are not covered by either prefix.
A run costs two requests.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterator, Optional

import requests
from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)

BASE = "https://voterportal.sos.la.gov"
INQUIRY_URL = f"{BASE}/candidateinquiry"
OFFICE_LIST_URL = f"{BASE}/CandidateInquiry/StatewideCandidate/OfficeList"
CANDIDATE_LIST_URL = f"{BASE}/CandidateInquiry/StatewideCandidate/CandidateList"

#: Louisiana labels the party "Democrat" rather than "Democratic".
DEM_PARTY = re.compile(r"^\s*democrat(ic)?\s*$", re.IGNORECASE)

#: "U. S. Representative 3rd Congressional District" -> 3. The comma is
#: optional because the two pages disagree: the office picker writes
#: "Representative, 1st Congressional District" and the candidate listing
#: writes "Representative 1st Congressional District". The spacing in "U. S."
#: is the site's own and varies, so it is not matched on.
OFFICE_DISTRICT = re.compile(
    r"Representative,?\s+(\d{1,2})(?:st|nd|rd|th)\s+Congressional\s+District",
    re.IGNORECASE,
)


#: Mailbox providers that say nothing about a campaign. A candidate filing
#: from one of these has no campaign domain to find, which is itself common in
#: an all-party race with many low-budget entrants.
FREE_MAIL = {
    "gmail.com", "yahoo.com", "aol.com", "outlook.com", "hotmail.com",
    "comcast.net", "icloud.com", "me.com", "att.net", "bellsouth.net",
    "live.com", "msn.com", "protonmail.com", "proton.me", "cox.net",
}


@dataclass(frozen=True)
class Qualified:
    """One candidate who qualified for the November ballot."""

    name: str
    party: str
    district: int
    filed: str

    contact_domain: str = ""
    """Domain of the contact address on the qualifying form, never the address.

    The filing is a public record carrying each candidate's home address,
    phone number and email. None of that belongs in this project's outputs,
    but the domain alone often names the campaign site - patforbes.com - and
    is a better lead than anything else available for a Louisiana candidate
    whom Wikipedia does not link.
    """

    @property
    def campaign_domain(self) -> str:
        """The contact domain when it plausibly belongs to a campaign."""
        d = self.contact_domain.lower()
        return "" if d in FREE_MAIL or "." not in d else d

    @property
    def is_democrat(self) -> bool:
        return bool(DEM_PARTY.match(self.party))


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "dcp-research/1.0 (+https://github.com/readchaoticera/demcandidateplatform)",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": INQUIRY_URL,
    })
    return s


def election_id(html: str, date: str = "11/03/2026") -> Optional[str]:
    """The portal's internal id for an election date.

    Read from the page rather than hard-coded: the ids are not derivable from
    the date, and the value for a given election changes between cycles.
    """
    for match in re.finditer(r'<option[^>]*value="(\d+)"[^>]*>([^<]+)</option>', html):
        if match.group(2).strip() == date:
            return match.group(1)
    return None


def house_office_ids(html: str) -> list[str]:
    """Checkbox ids for the six U.S. House races, in district order."""
    found: list[tuple[int, str]] = []
    for match in re.finditer(
        r'<label for="cb_(\d+)">\s*<input[^>]*>\s*([^<]+)</label>', html
    ):
        district = OFFICE_DISTRICT.search(" ".join(match.group(2).split()))
        if district:
            found.append((int(district.group(1)), match.group(1)))
    return [oid for _, oid in sorted(found)]


def _sections(soup: BeautifulSoup) -> Iterator[tuple[int, Tag]]:
    """Walk the document, tracking which office heading each block falls under.

    The list is flat - office titles and candidate blocks are siblings, not
    nested - so the current district has to be carried along the walk.
    """
    district: Optional[int] = None
    for node in soup.find_all(["span", "div"]):
        classes = node.get("class") or []
        if "office-title" in classes:
            match = OFFICE_DISTRICT.search(" ".join(node.get_text(" ", strip=True).split()))
            district = int(match.group(1)) if match else None
        elif "candidate-section" in classes and district is not None:
            yield district, node


def parse(html: str) -> list[Qualified]:
    """Every qualified U.S. House candidate, of any party.

    Only name, party, district and filing date are read. The page also carries
    each candidate's home address, phone number and email; those are public
    record but have no bearing on this analysis, so they are left where they
    are rather than copied into this project's outputs.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[Qualified] = []
    for district, section in _sections(soup):
        top = section.find("div", class_="candidate-top-row")
        if top is None:
            continue
        cols = top.find_all("div", recursive=False)
        fields = [c.get_text(" ", strip=True) for c in cols]
        # The first column carries a "Name / Address / Phone" label that is
        # hidden on desktop; strip it or it becomes part of the name.
        named = [f for f in fields if f]
        if len(named) < 3:
            continue
        name = re.sub(r"^Name\s*/\s*Address\s*/\s*Phone\s*", "", named[-3]).strip()
        filed, party = named[-2].strip(), named[-1].strip()
        if not name or not re.match(r"^\d{2}/\d{2}/\d{4}$", filed):
            continue
        link = section.find("a", href=re.compile(r"^\s*mailto:", re.I))
        domain = ""
        if link is not None:
            address = link.get_text(strip=True)
            if "@" in address:
                domain = address.rsplit("@", 1)[-1].strip().lower()
        out.append(Qualified(name=name, party=party, district=district,
                             filed=filed, contact_domain=domain))
    return out


def fetch(date: str = "11/03/2026") -> list[Qualified]:
    """Fetch and parse the certified list for one election date."""
    session = _session()
    page = session.get(INQUIRY_URL, timeout=60)
    page.raise_for_status()

    eid = election_id(page.text, date)
    if eid is None:
        log.warning("louisiana: no election on %s in the portal", date)
        return []

    offices = session.post(OFFICE_LIST_URL, data={"electionId": eid}, timeout=60)
    offices.raise_for_status()
    ids = house_office_ids(offices.text)
    if not ids:
        log.warning("louisiana: no U.S. House offices listed for %s", date)
        return []

    # traditional array serialisation: officeIds repeats, per the portal's own
    # jQuery call. A comma-joined value is rejected with a redirect to /Error.
    body = [("electionId", eid)] + [("officeIds", i) for i in ids]
    listing = session.post(CANDIDATE_LIST_URL, data=body, timeout=90)
    listing.raise_for_status()
    return parse(listing.text)


#: The SoS records the name a candidate qualified under, which carries their
#: ballot nickname in quotes: 'Caleb "With a C" Walker', '"Matt" Gromlich'.
_QUOTED = re.compile(r'"([^"]*)"')


def display_name(qualified_name: str) -> str:
    """Turn a qualifying-form name into the one a reader would recognise.

    A quoted first token is the name the candidate goes by and replaces
    nothing - '"Matt" Gromlich' is Matt Gromlich. A quoted token in the middle
    is a nickname sitting between given and family name, and is dropped:
    'Caleb "With a C" Walker' is Caleb Walker, not Caleb With A C Walker.
    """
    name = " ".join(qualified_name.split())
    if name.startswith('"'):
        name = _QUOTED.sub(r"\1", name, count=1)
    else:
        name = _QUOTED.sub("", name)
    return " ".join(name.split())


def democrats_by_district(qualified: list[Qualified]) -> dict[str, list[str]]:
    """{district code: [names]} for Democrats only."""
    out: dict[str, list[str]] = {}
    for q in qualified:
        if q.is_democrat:
            out.setdefault(f"LA-{q.district:02d}", []).append(display_name(q.name))
    return out
