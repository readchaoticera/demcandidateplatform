"""Campaign website discovery and verification.

Finding the right site is the step most likely to inject silent errors into
the final table. The failure mode is not "no site found" - that is visible and
harmless - but "wrong site found", where a news profile, a Ballotpedia page, a
namesake's business, or the *opponent's* site gets attached to a candidate and
then classified as if it were theirs.

So no discovered URL is trusted on the strength of the search ranking. Each
candidate URL is fetched and scored on positive evidence (the candidate's name
in the title, campaign apparatus like donate/volunteer links, the district or
state named) and against a denylist of things that are definitionally not a
campaign site. Anything below ``ACCEPT_THRESHOLD`` is recorded with its score
and flagged rather than used.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional, Protocol
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .models import Candidate, normalize_name
from .net import Fetcher, FetchError
from .statefacts import SEAT_COUNTS

log = logging.getLogger(__name__)

#: Minimum score for a URL to be accepted as a candidate's campaign site.
ACCEPT_THRESHOLD = 0.55

#: Hosts that are never a campaign website, however well they rank.
DENY_HOSTS = re.compile(
    r"(^|\.)(ballotpedia\.org|wikipedia\.org|wikiwand\.com|facebook\.com|x\.com|"
    r"twitter\.com|instagram\.com|linkedin\.com|youtube\.com|tiktok\.com|"
    r"actblue\.com|secure\.actblue\.com|winred\.com|fec\.gov|opensecrets\.org|"
    r"vote411\.org|votesmart\.org|congress\.gov|house\.gov|senate\.gov|"
    r"nytimes\.com|washingtonpost\.com|politico\.com|cnn\.com|foxnews\.com|"
    r"apnews\.com|reuters\.com|axios\.com|thehill\.com|rollcall\.com|"
    r"linktr\.ee|medium\.com|substack\.com|eventbrite\.com|mobilize\.us)$",
    re.IGNORECASE,
)

#: .gov sites are official offices, not campaigns. Legally distinct and they
#: must not carry campaign material, so they cannot be used for this analysis.
OFFICIAL_SITE = re.compile(r"\.(gov|mil)$", re.IGNORECASE)

CAMPAIGN_APPARATUS = re.compile(
    r"\b(donate|contribute|chip\s+in|volunteer|yard\s+sign|get\s+involved|"
    r"join\s+(the\s+)?(team|campaign)|paid\s+for\s+by|authorized\s+by)\b",
    re.IGNORECASE,
)

FOR_CONGRESS = re.compile(
    r"\bfor\s+congress\b|\bfor\s+(the\s+)?u\.?s\.?\s+house\b|\bcongressional\s+district\b",
    re.IGNORECASE,
)


class SearchProvider(Protocol):
    """Pluggable web-search backend used to find candidate sites.

    Deliberately an interface rather than a hardcoded provider: which search
    API is available depends on the deployment, and some environments have
    none. ``resolve_campaign_url`` works without one whenever a source already
    supplied a candidate URL (Ballotpedia usually does).
    """

    def search(self, query: str, limit: int = 5) -> list[str]:
        """Return candidate URLs, best first."""


@dataclass
class UrlScore:
    url: str
    score: float
    reasons: list[str]

    @property
    def accepted(self) -> bool:
        return self.score >= ACCEPT_THRESHOLD


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def is_disqualified(url: str) -> Optional[str]:
    """Return a reason string if this URL can never be a campaign site."""
    host = _host(url)
    if not host:
        return "unparseable URL"
    if DENY_HOSTS.search(host):
        return f"denylisted host ({host})"
    if OFFICIAL_SITE.search(host):
        return f"official government site, not a campaign ({host})"
    return None


def score_candidate_url(html: str, url: str, candidate: Candidate) -> UrlScore:
    """Score how strongly a fetched page looks like this candidate's campaign site.

    Scores are additive evidence, capped at 1.0. Name match is necessary but
    not sufficient - a news article about the candidate also matches the name -
    so campaign apparatus carries comparable weight.
    """
    reasons: list[str] = []
    score = 0.0

    disq = is_disqualified(url)
    if disq:
        return UrlScore(url, 0.0, [disq])

    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")
    text = soup.get_text(" ", strip=True)
    lowered = text.lower()

    surname = normalize_name(candidate.full_name).split(" ")[-1] if candidate.full_name else ""
    full_norm = normalize_name(candidate.full_name)

    if full_norm and full_norm in normalize_name(title):
        score += 0.35
        reasons.append("full name in <title>")
    elif surname and surname in title.lower():
        score += 0.20
        reasons.append("surname in <title>")

    if surname and surname in _host(url):
        score += 0.25
        reasons.append("surname in domain")

    if CAMPAIGN_APPARATUS.search(text[:20000]):
        score += 0.25
        reasons.append("campaign apparatus (donate/volunteer/disclaimer)")

    if FOR_CONGRESS.search(text[:20000]) or FOR_CONGRESS.search(title):
        score += 0.15
        reasons.append("'for Congress' framing")

    state = candidate.district.state
    if re.search(rf"\b{state}[\s\-]?{candidate.district.number}\b", text[:20000], re.IGNORECASE):
        score += 0.10
        reasons.append("district referenced")

    # Negative evidence: a page that names many other candidates is a roundup.
    if lowered.count("for congress") > 6:
        score -= 0.20
        reasons.append("looks like a multi-candidate roundup")

    return UrlScore(url, max(0.0, min(1.0, score)), reasons)


def resolve_campaign_url(
    fetcher: Fetcher,
    candidate: Candidate,
    hints: list[str],
    search: Optional[SearchProvider] = None,
) -> UrlScore:
    """Pick the best campaign URL for a candidate.

    ``hints`` are URLs already supplied by upstream sources (Ballotpedia's
    "Campaign website" field, an FEC committee URL). They are tried first,
    then a search provider if one is configured.
    """
    tried: list[UrlScore] = []
    seen: set[str] = set()

    # A curated link may point at a sub-page ("/endorsements", "/splash").
    # Crawling from there loses the homepage's navigation, so try the site root
    # first and keep the sub-page as a fallback.
    queue: list[str] = []
    for hint in hints:
        if not hint:
            continue
        parts = urlparse(hint)
        if parts.path.strip("/") not in ("", "home", "index.html"):
            root = f"{parts.scheme}://{parts.netloc}/"
            if root not in queue:
                queue.append(root)
        if hint not in queue:
            queue.append(hint)
    if search is not None:
        state_name = candidate.district.state
        query = f'"{candidate.full_name}" for Congress {state_name} campaign'
        try:
            queue.extend(search.search(query, limit=5))
        except Exception as exc:  # a search backend failure must not kill the run
            log.warning("search failed for %s: %s", candidate.full_name, exc)

    for url in queue:
        norm = url.rstrip("/")
        if norm in seen:
            continue
        seen.add(norm)
        if is_disqualified(url):
            tried.append(UrlScore(url, 0.0, [is_disqualified(url) or ""]))
            continue
        try:
            resp = fetcher.get(url)
        except FetchError as exc:
            tried.append(UrlScore(url, 0.0, [f"fetch failed: {exc}"]))
            continue
        if not resp.ok:
            tried.append(UrlScore(url, 0.0, [f"HTTP {resp.status}"]))
            continue
        scored = score_candidate_url(resp.text, resp.url, candidate)
        tried.append(scored)
        if scored.accepted:
            return scored

    if not tried:
        return UrlScore("", 0.0, ["no candidate URLs to try"])
    return max(tried, key=lambda s: s.score)
