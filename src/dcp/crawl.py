"""Locate and fetch the pages of a campaign site that state policy positions.

Campaign sites bury positions inconsistently: sometimes one /issues page,
sometimes a page per issue, sometimes only a paragraph on the homepage. This
module follows the homepage's own navigation rather than guessing URLs, then
falls back to a small set of conventional paths.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .net import Fetcher, FetchError

log = logging.getLogger(__name__)

#: Link text / hrefs that indicate a positions page, most specific first.
ISSUE_LINK = re.compile(
    r"health|medicare|issues?|priorit|platform|policy|policies|"
    r"where[\s\-_]?i[\s\-_]?stand|on[\s\-_]the[\s\-_]issues|agenda|vision|plan",
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

MAX_PAGES = 8

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


def find_issue_links(homepage_html: str, base_url: str) -> list[str]:
    """Rank same-site links by how likely they are to state positions."""
    soup = BeautifulSoup(homepage_html, "lxml")
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

    return [u for u, _ in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))]


def collect_position_pages(
    fetcher: Fetcher, campaign_url: str, max_pages: int = MAX_PAGES
) -> dict[str, str]:
    """Fetch the homepage plus its most position-bearing pages.

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
    targets = find_issue_links(home.text, home.url)

    if not targets:
        targets = [urljoin(home.url, p) for p in FALLBACK_PATHS]

    for url in targets:
        if len(pages) >= max_pages:
            break
        if url in pages:
            continue
        try:
            resp = fetcher.get(url)
        except FetchError:
            continue
        if resp.ok and resp.url not in pages:
            pages[resp.url] = resp.text

    return pages
