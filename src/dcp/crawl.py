"""Locate and fetch the pages of a campaign site that state policy positions.

Campaign sites bury positions inconsistently: sometimes one /issues page,
sometimes a page per issue, sometimes only a paragraph on the homepage. This
module follows the homepage's own navigation rather than guessing URLs, then
falls back to a small set of conventional paths.
"""

from __future__ import annotations

import heapq
import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .net import Fetcher, FetchError

log = logging.getLogger(__name__)

#: Link text / hrefs that indicate a positions page.
#:
#: Campaigns label this page many different ways, and a label not on this list
#: is not a near miss - the page is never fetched at all, and the candidate
#: reads as having no position. "Values" alone cost one candidate their
#: correct classification, so the list errs wide; a wrongly-followed page
#: costs one fetch, a missed one costs a row.
ISSUE_LINK = re.compile(
    r"health|medicare|issues?|priorit|platform|policy|policies|"
    r"where[\s\-_]?i[\s\-_]?stand|on[\s\-_]the[\s\-_]issues|agenda|vision|plan|"
    r"values|beliefs|principles|commitments|solutions|"
    r"fight(ing)?[\s\-_]?for|stands?[\s\-_]?for|our[\s\-_]?work|"
    r"the[\s\-_]?record|my[\s\-_]?record|accomplishments",
    re.IGNORECASE,
)

#: Strong signal - fetch these first and always.
HEALTH_LINK = re.compile(r"health|medicare|medicaid|insur", re.IGNORECASE)

#: Pages that match ISSUE_LINK by accident. "Privacy policy" hits on "policy",
#: and every wasted fetch can crowd a real issues page out of the page budget.
NOT_AN_ISSUES_PAGE = re.compile(
    r"privacy|terms|cookie|refund|disclaimer|accessibility|sitemap|"
    r"unsubscribe|donate|contribut|shop|store|merch|press[\s\-_]?release|"
    r"careers?|jobs?|internship|volunteer|event|calendar|"
    r"login|sign[\s\-_]?in|account",
    re.IGNORECASE,
)

FALLBACK_PATHS = (
    "/issues", "/priorities", "/platform", "/on-the-issues",
    "/issues/health-care", "/issues/healthcare", "/meet", "/about",
)

MAX_PAGES = 12

#: How far to follow links from the homepage. Two is enough for the common
#: home -> /issues -> /issues/healthcare shape without wandering the whole site.
MAX_DEPTH = 2

#: Never follow links to these: they are documents and media, not pages.
#: Campaigns publish PDFs from paths that otherwise look like issue pages, and
#: decoding one as text produced a false Medicare for All match.
NON_HTML_SUFFIX = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|zip|gz|csv|"
    r"png|jpe?g|gif|webp|svg|ico|bmp|tiff?|"
    r"mp[34]|m4[av]|mov|avi|webm|wav|ogg|"
    r"woff2?|ttf|otf|eot|css|js|json|xml|rss)$",
    re.IGNORECASE,
)


def _same_site(base: str, url: str) -> bool:
    b, u = urlparse(base).netloc.lower(), urlparse(url).netloc.lower()
    return b.removeprefix("www.") == u.removeprefix("www.")


def score_issue_links(html: str, base_url: str) -> list[tuple[int, str]]:
    """Same-site links that may state positions, as (weight, url), best first."""
    soup = BeautifulSoup(html, "lxml")
    scored: dict[str, int] = {}

    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        if not href.startswith("http") or not _same_site(base_url, href):
            continue
        href = href.split("#")[0].rstrip("/")
        if not href or href.rstrip("/") == base_url.rstrip("/"):
            continue
        label = a.get_text(" ", strip=True)
        haystack = f"{label} {urlparse(href).path}"
        if NON_HTML_SUFFIX.search(urlparse(href).path):
            continue
        if not ISSUE_LINK.search(haystack) or NOT_AN_ISSUES_PAGE.search(haystack):
            continue
        weight = 2 if HEALTH_LINK.search(haystack) else 1
        scored[href] = max(scored.get(href, 0), weight)

    return [(w, u) for u, w in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))]


def find_issue_links(html: str, base_url: str) -> list[str]:
    """Ranked position-bearing links, best first."""
    return [u for _, u in score_issue_links(html, base_url)]


def collect_position_pages(
    fetcher: Fetcher, campaign_url: str, max_pages: int = MAX_PAGES
) -> dict[str, str]:
    """Fetch the homepage plus its most position-bearing pages.

    Crawls to a bounded depth rather than only reading links off the homepage.
    Very common campaign structure: the homepage nav links to ``/issues``,
    which is a near-empty index whose links go to ``/issues/healthcare`` and
    friends. Reading only the homepage's links stops at the index and finds
    nothing, which is indistinguishable from a candidate with no healthcare
    position - and silently under-counts support.

    Links are visited best-first (health-related pages before generic issue
    pages) so the page budget is spent where the answer usually is.

    Returns ``{url: html}``. The homepage is always included - some campaigns
    state their entire platform there and have no issues page at all.
    """
    pages: dict[str, str] = {}
    try:
        home = fetcher.get(campaign_url)
    except FetchError as exc:
        log.warning("homepage fetch failed for %s: %s", campaign_url, exc)
        return pages
    if not home.ok:
        log.warning("homepage %s -> HTTP %s", campaign_url, home.status)
        return pages

    pages[home.url] = home.text

    # (-weight, depth, url): highest weight first, shallower first as tiebreak.
    frontier: list[tuple[int, int, str]] = [
        (-w, 1, u) for w, u in score_issue_links(home.text, home.url)
    ]
    heapq.heapify(frontier)
    seen = {home.url.rstrip("/")} | {u.rstrip("/") for _, _, u in frontier}

    if not frontier:
        for path in FALLBACK_PATHS:
            url = urljoin(home.url, path)
            if url.rstrip("/") not in seen:
                seen.add(url.rstrip("/"))
                heapq.heappush(frontier, (0, 1, url))

    while frontier and len(pages) < max_pages:
        neg_weight, depth, url = heapq.heappop(frontier)
        try:
            resp = fetcher.get(url)
        except FetchError:
            continue
        if not resp.ok or resp.url in pages:
            continue
        pages[resp.url] = resp.text

        if depth >= MAX_DEPTH:
            continue
        for weight, link in score_issue_links(resp.text, resp.url):
            key = link.rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            heapq.heappush(frontier, (-weight, depth + 1, link))

    return pages
