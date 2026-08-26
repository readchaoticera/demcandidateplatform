"""Cosponsorship of the Medicare for All Act — the strongest available signal.

For sitting members, cosponsoring the bill is a recorded legislative act. It is
better evidence of a position than anything a campaign website says, and it is
available for exactly the candidates whose sites most often defeat a scraper
(incumbents tend to run modern, Javascript-heavy sites).

It is nonetheless a *different kind* of evidence from campaign copy, so it is
kept in its own field rather than folded into ``m4a_tier``. A member can
cosponsor the bill and never mention it to voters; that difference is a finding
in itself, not noise to be averaged away.

Source is GovTrack rather than congress.gov, which answers 403 to non-browser
clients; the official API needs a key. GovTrack publishes the same roll.

The bill is reintroduced each Congress under a new number, so ``BILL`` must be
updated at the start of each one. H.R.3069 is the 119th Congress version, lead
sponsor Rep. Pramila Jayapal.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ..models import Candidate
from ..net import Fetcher, FetchError

log = logging.getLogger(__name__)

CONGRESS = 119
BILL = "hr3069"
BILL_TITLE = "Medicare for All Act"
SOURCE_URL = f"https://www.govtrack.us/congress/bills/{CONGRESS}/{BILL}/cosponsors"

#: "D Jayapal, Pramila [D-WA7, 2017-2026]"
_ROW = re.compile(r"^([DRI])\s+(.+?)\s*\[([DRI])-([A-Z]{2})(\d+|AL)?,")


@dataclass(frozen=True)
class Cosponsor:
    name: str
    district: str
    role: str

    @property
    def is_sponsor(self) -> bool:
        return "primary sponsor" in self.role.lower()


def name_keys(name: str) -> set[str]:
    """Name tokens for matching, including quoted nicknames.

    Rolls list members formally — 'Henry C. "Hank" Johnson', 'Robert "Bobby"
    Scott' — while rosters use the familiar form. Dropping the nickname (as
    general name normalisation does) loses exactly the token that matches.
    """
    nicknames = re.findall(r'[“"]([^”"]+)[”"]', name)
    base = re.sub(r'[“"][^”"]*[”"]', " ", name)
    base = re.sub(r"[^A-Za-zÀ-ſ\s'-]", " ", base)
    tokens = {t.lower() for t in base.split() if len(t) > 1}
    return tokens | {n.lower() for n in nicknames}


def fetch_cosponsors(fetcher: Fetcher) -> list[Cosponsor]:
    """The sponsor and every cosponsor of the current Medicare for All Act."""
    try:
        resp = fetcher.get(SOURCE_URL)
    except FetchError as exc:
        log.warning("congress: %s -> %s", SOURCE_URL, exc)
        return []
    if not resp.ok:
        log.warning("congress: %s -> HTTP %s", SOURCE_URL, resp.status)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    tables = soup.find_all("table")
    if not tables:
        log.warning("congress: no tables at %s; layout changed", SOURCE_URL)
        return []

    # The first table is the cosponsor roll. A later table lists *non*-cosponsors
    # ("Cosponsorship of Other Relevant Legislators") and must not be read as one.
    out: list[Cosponsor] = []
    for tr in tables[0].find_all("tr")[1:]:
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        match = _ROW.match(cells[0].get_text(" ", strip=True))
        if not match:
            continue
        surname_first = match.group(2)
        state, seat = match.group(4), match.group(5) or "AL"
        parts = [p.strip() for p in surname_first.split(",")]
        display = f"{parts[1]} {parts[0]}" if len(parts) == 2 else surname_first
        out.append(
            Cosponsor(
                name=display,
                district=f"{state}-{seat.zfill(2) if seat != 'AL' else 'AL'}",
                role=cells[1].get_text(" ", strip=True),
            )
        )
    log.info("congress: %d cosponsors of %s (%s)", len(out), BILL.upper(), BILL_TITLE)
    return out


#: Shared name tokens required to accept a match outside the member's own
#: district. Two is enough to mean forename and surname both agree.
_STRONG_NAME_MATCH = 2


def annotate(candidates: Iterable[Candidate], cosponsors: list[Cosponsor]) -> int:
    """Set ``cosponsored_m4a_bill`` on candidates who are on the roll.

    Two passes, because neither key is reliable alone:

    1. **District plus a shared name token.** District alone would credit a
       challenger with the incumbent's cosponsorship - the one error that would
       badly overstate support - so a name token is always required.

    2. **State plus a strong name match**, for members whose district number
       changed. Mid-decade redistricting moved several: the roll lists Linda
       Sánchez in CA-38 while she runs in CA-41, Lois Frankel in FL-22 running
       in FL-23, Jared Moskowitz in FL-23 running in FL-25. Requiring the
       district would silently drop all three. Two shared tokens means forename
       and surname both agree, which no challenger-versus-incumbent pair in one
       state realistically satisfies.
    """
    by_district: dict[str, Cosponsor] = {c.district: c for c in cosponsors}
    matched = 0

    for cand in candidates:
        cand.cosponsored_m4a_bill = None
        cand_keys = name_keys(cand.full_name)

        sponsor = by_district.get(cand.district.code)
        if sponsor is not None and (cand_keys & name_keys(sponsor.name)):
            note = f"{sponsor.role} of {BILL.upper()}, {BILL_TITLE} ({CONGRESS}th Congress)"
        else:
            sponsor, note = None, ""
            for other in cosponsors:
                if other.district[:2] != cand.district.state:
                    continue
                if len(cand_keys & name_keys(other.name)) >= _STRONG_NAME_MATCH:
                    sponsor = other
                    note = (
                        f"{other.role} of {BILL.upper()}, {BILL_TITLE} "
                        f"({CONGRESS}th Congress); roll lists {other.district}, "
                        f"candidate runs in {cand.district.code} after redistricting"
                    )
                    break
        if sponsor is None:
            continue

        cand.cosponsored_m4a_bill = True
        cand.incumbent = True
        cand.add_provenance("congress", SOURCE_URL, note)
        matched += 1
    return matched
