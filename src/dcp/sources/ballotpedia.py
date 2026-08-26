"""Ballotpedia adapter: primarily a source of campaign website URLs.

Ballotpedia is the best free source for the "Campaign website" field, which is
otherwise expensive to obtain reliably. Two constraints shape this module:

*   **Terms of use.** Ballotpedia's content is licensed and their robots.txt
    governs automated access. ``net.Fetcher`` enforces robots.txt and rate
    limits, and will raise ``RobotsDisallowed`` rather than proceed if the
    path is disallowed. For a full-scale run, use their API or request bulk
    access rather than crawling several hundred district pages.

*   **Layout drift.** Their district pages are template-rendered but the
    templates change. The parser targets external links labelled as campaign
    or personal sites near a candidate's name, and reports what it could not
    parse instead of guessing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..models import District, NominationStatus
from ..net import Fetcher, FetchError, RobotsDisallowed
from ..statefacts import AT_LARGE_STATES, ballot_rule
from .wikipedia import STATE_NAMES

log = logging.getLogger(__name__)

BASE = "https://ballotpedia.org"

#: Ordinal suffixes by final digit. 11/12/13 are irregular and handled separately.
ORDINAL_SUFFIX = {1: "st", 2: "nd", 3: "rd"}

WEBSITE_LABEL = re.compile(
    r"campaign\s+website|official\s+campaign|candidate\s+website|personal\s+website",
    re.IGNORECASE,
)

INTERNAL = re.compile(r"ballotpedia\.org|wikipedia\.org", re.IGNORECASE)


def _ordinal(n: int) -> str:
    """1 -> "1st", 12 -> "12th", 52 -> "52nd"."""
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ORDINAL_SUFFIX.get(n % 10, 'th')}"


def district_page_url(district: District) -> str:
    state_name = STATE_NAMES[district.state].replace(" ", "_")
    if district.at_large:
        slug = f"{state_name}%27s_At-Large_Congressional_District_election,_2026"
    else:
        slug = (
            f"{state_name}%27s_{_ordinal(district.number)}"
            f"_Congressional_District_election,_2026"
        )
    return f"{BASE}/{slug}"


@dataclass
class CampaignLink:
    candidate_name: str
    url: str


def parse_campaign_links(html: str) -> list[CampaignLink]:
    """Extract (candidate, campaign URL) pairs from a district page.

    Works from the link outward: find external anchors whose own text, or a
    nearby label, marks them as a campaign site, then walk up to find the
    candidate name they belong to.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[CampaignLink] = []
    seen: set[tuple[str, str]] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http") or INTERNAL.search(urlparse(href).netloc):
            continue

        label = a.get_text(" ", strip=True)
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        if not (WEBSITE_LABEL.search(label) or WEBSITE_LABEL.search(parent_text)):
            continue

        name = _nearest_candidate_name(a)
        if not name:
            continue
        key = (name.lower(), href.rstrip("/"))
        if key in seen:
            continue
        seen.add(key)
        out.append(CampaignLink(candidate_name=name, url=href))

    return out


def _nearest_candidate_name(anchor) -> Optional[str]:
    """Walk up from a campaign link to the candidate heading it belongs to."""
    node = anchor
    for _ in range(6):
        node = node.parent
        if node is None:
            return None
        # A candidate's name is usually a wiki-internal link in the same block.
        for link in node.find_all("a", href=True):
            href = link["href"]
            if href.startswith("/") and not href.startswith("//"):
                text = link.get_text(" ", strip=True)
                if _looks_like_person(text):
                    return text
        heading = node.find(["h2", "h3", "h4", "b", "strong"])
        if heading:
            text = heading.get_text(" ", strip=True)
            if _looks_like_person(text):
                return text
    return None


def _looks_like_person(text: str) -> bool:
    if not text or not 4 <= len(text) <= 60 or " " not in text:
        return False
    if re.search(
        r"\b(election|district|congress|ballot|primary|general|party|endorse|"
        r"campaign|website|see also|external links)\b",
        text,
        re.IGNORECASE,
    ):
        return False
    return bool(re.match(r"^[A-Z][\w.'\-]+(\s+[A-Z][\w.'\-]+)+$", text))


def campaign_urls_for_district(
    fetcher: Fetcher, district: District
) -> tuple[dict[str, str], list[str]]:
    """Return ({candidate_name: campaign_url}, warnings) for one district."""
    url = district_page_url(district)
    try:
        resp = fetcher.get(url)
    except RobotsDisallowed:
        return {}, [f"{district.code}: Ballotpedia robots.txt disallows {url}"]
    except FetchError as exc:
        return {}, [f"{district.code}: {exc}"]

    if not resp.ok:
        return {}, [f"{district.code}: Ballotpedia returned HTTP {resp.status}"]

    links = parse_campaign_links(resp.text)
    if not links:
        return {}, [f"{district.code}: no campaign links parsed from {url}"]
    return {link.candidate_name: link.url for link in links}, []
