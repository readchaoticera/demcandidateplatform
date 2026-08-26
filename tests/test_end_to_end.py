"""End-to-end wiring test: crawl -> classify -> resolve -> report.

Uses a stub fetcher so the whole chain is exercised without network access.
This is what catches wiring mistakes that unit tests pass straight over, e.g.
a classifier that works but is never actually reached from the crawler.
"""

import csv
import io
from datetime import date, datetime

from dcp.classify.classifier import classify_pages
from dcp.crawl import collect_position_pages
from dcp.models import Candidate, District, M4ATier, NominationStatus
from dcp.net import Response
from dcp.report import analyze, to_csv, to_markdown
from dcp.resolve import build_roster
from dcp.statefacts import ballot_rule

AS_OF = date(2026, 8, 26)

FILLER = (
    " Our district deserves a representative who shows up and does the work every"
    " single day, listens to neighbors, and answers to the people who sent them."
)

SITES = {
    "https://aaa.example/": f"""<html><head><title>Ann Alvarez for Congress</title></head>
        <body><nav><a href="/issues">Issues</a><a href="/issues/health">Health Care</a></nav>
        <p>Donate today. Paid for by Ann Alvarez for Congress.{FILLER}</p></body></html>""",
    "https://aaa.example/issues": f"<html><body><p>My priorities are jobs and schools.{FILLER}</p></body></html>",
    "https://aaa.example/issues/health": (
        f"<html><body><p>I support Medicare for All and will co-sponsor it in Congress.{FILLER}</p></body></html>"
    ),
    "https://bbb.example/": f"""<html><head><title>Ben Boyd for Congress</title></head>
        <body><nav><a href="/issues">Issues</a></nav>
        <p>Volunteer with us.{FILLER}</p></body></html>""",
    "https://bbb.example/issues": (
        f"<html><body><p>I support a public option. I do not support Medicare for All.{FILLER}</p></body></html>"
    ),
    "https://ccc.example/": f"""<html><head><title>Cara Cole for Congress</title></head>
        <body><p>I will protect the Affordable Care Act and coverage for pre-existing conditions.{FILLER}</p>
        </body></html>""",
}


class StubFetcher:
    """Serves canned pages; 404s anything else, like a real site would."""

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url, *, force=False):
        self.requested.append(url)
        body = self.pages.get(url) or self.pages.get(url.rstrip("/") + "/")
        status = 200 if body else 404
        return Response(url=url, status=status, text=body or "",
                        from_cache=False, fetched_at=datetime.utcnow())


def _candidate(name, state, num, url):
    c = Candidate(
        name, District(state, num, ballot_rule=ballot_rule(state)),
        NominationStatus.ON_BALLOT,
    )
    c.campaign_url = url
    c.campaign_url_confidence = 0.9
    return c


def test_full_chain_produces_a_consistent_report():
    fetcher = StubFetcher(SITES)
    candidates = [
        _candidate("Ann Alvarez", "OH", 1, "https://aaa.example/"),
        _candidate("Ben Boyd", "OH", 2, "https://bbb.example/"),
        _candidate("Cara Cole", "OH", 3, "https://ccc.example/"),
        _candidate("Dana Doe", "OH", 4, None),  # no site -> must land as UNKNOWN
    ]

    for cand in candidates:
        if not cand.campaign_url:
            cand.m4a_tier = M4ATier.UNKNOWN
            cand.m4a_notes = "no campaign website resolved"
            continue
        pages = collect_position_pages(fetcher, cand.campaign_url)
        result = classify_pages(pages)
        cand.m4a_tier = result.tier
        cand.m4a_evidence = result.evidence
        cand.issues_urls = sorted(pages)
        if result.explicitly_rejects_m4a:
            cand.m4a_notes = "explicitly rejects Medicare for All"

    tiers = {c.full_name: c.m4a_tier for c in candidates}
    assert tiers["Ann Alvarez"] is M4ATier.EXPLICIT_M4A
    assert tiers["Ben Boyd"] is M4ATier.PUBLIC_OPTION
    assert tiers["Cara Cole"] is M4ATier.ACA_STRENGTHEN
    assert tiers["Dana Doe"] is M4ATier.UNKNOWN

    # The crawler must have followed the health link, not just the homepage.
    assert "https://aaa.example/issues/health" in fetcher.requested

    roster = build_roster(candidates, AS_OF)
    analysis = analyze(roster, AS_OF)

    assert analysis.total_on_ballot == 4
    assert analysis.classified == 3          # Dana excluded
    assert analysis.explicit_m4a == 1
    assert abs(analysis.share(analysis.explicit_m4a) - 1 / 3) < 1e-9
    assert analysis.share(analysis.explicit_m4a, of_classified=False) == 0.25

    markdown = to_markdown(analysis, roster)
    assert "Medicare for All" in markdown
    assert "Known gaps" in markdown

    # Parse rather than count newlines: evidence quotes can contain newlines,
    # which the csv module quotes correctly but a naive count would miscount.
    rows = list(csv.DictReader(io.StringIO(to_csv(roster))))
    assert len(rows) == 4
    assert {r["full_name"] for r in rows} == {
        "Ann Alvarez", "Ben Boyd", "Cara Cole", "Dana Doe"
    }
    ann = next(r for r in rows if r["full_name"] == "Ann Alvarez")
    assert ann["m4a_tier"] == "explicit_m4a"
    assert "Medicare for All" in ann["m4a_evidence_quote"]


def test_evidence_quote_survives_to_the_csv():
    fetcher = StubFetcher(SITES)
    cand = _candidate("Ann Alvarez", "OH", 1, "https://aaa.example/")
    result = classify_pages(collect_position_pages(fetcher, cand.campaign_url))
    cand.m4a_tier, cand.m4a_evidence = result.tier, result.evidence

    roster = build_roster([cand], AS_OF)
    row = next(csv.DictReader(io.StringIO(to_csv(roster))))
    assert row["m4a_evidence_rule"] == "m4a.phrase"
    assert cand.m4a_evidence[0].url == "https://aaa.example/issues/health"


# --- crawl depth ------------------------------------------------------------

INDEX_SITE = {
    "https://x.example/": (
        "<html><head><title>Pat Doe for Congress</title></head><body>"
        "<nav><a href='/issues'>Issues</a></nav>"
        "<p>Donate today. Paid for by Pat Doe for Congress.</p></body></html>"
    ),
    # A near-empty index whose links go one level deeper. This is the shape
    # that silently produced "no coverage position" before crawling recursed.
    "https://x.example/issues": (
        "<html><body><nav>"
        "<a href='/issues/housing'>Housing</a>"
        "<a href='/issues/healthcare'>Healthcare</a>"
        "<a href='/issues/environment'>Environment</a>"
        "</nav></body></html>"
    ),
    "https://x.example/issues/housing":
        f"<html><body><p>We need more homes.{FILLER}</p></body></html>",
    "https://x.example/issues/healthcare": (
        "<html><body><p>I believe healthcare is a human right, and am a strong "
        f"supporter of Medicare for All.{FILLER}</p></body></html>"
    ),
    "https://x.example/issues/environment":
        f"<html><body><p>Clean air and water.{FILLER}</p></body></html>",
}


def test_crawler_follows_an_index_page_to_its_children():
    fetcher = StubFetcher(INDEX_SITE)
    pages = collect_position_pages(fetcher, "https://x.example/")
    assert "https://x.example/issues/healthcare" in pages
    assert classify_pages(pages).tier is M4ATier.EXPLICIT_M4A


def test_health_pages_are_fetched_before_other_issue_pages():
    # The page budget is finite, so the health page must not be crowded out.
    fetcher = StubFetcher(INDEX_SITE)
    collect_position_pages(fetcher, "https://x.example/", max_pages=3)
    health = fetcher.requested.index("https://x.example/issues/healthcare")
    others = [fetcher.requested.index(u) for u in
              ("https://x.example/issues/housing", "https://x.example/issues/environment")
              if u in fetcher.requested]
    assert all(health < o for o in others)


def test_crawl_respects_the_page_budget():
    fetcher = StubFetcher(INDEX_SITE)
    assert len(collect_position_pages(fetcher, "https://x.example/", max_pages=3)) <= 3
