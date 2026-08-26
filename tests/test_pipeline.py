"""Tests for merging, calendar enforcement, reporting denominators, adjudication."""

from datetime import date

import pytest

from dcp.adjudicate import apply_adjudication, needs_review, to_review_csv
from dcp.models import (
    Candidate, District, Evidence, M4ATier, NominationStatus, Roster,
)
from dcp.report import analyze, to_csv, to_markdown
from dcp.resolve import apply_calendar, build_roster, merge
from dcp.statefacts import ballot_rule
from dcp.websites import is_disqualified, score_candidate_url

AS_OF = date(2026, 8, 26)


def mk(name, state, num, tier=M4ATier.UNKNOWN, status=NominationStatus.ON_BALLOT, inc=False):
    c = Candidate(
        name, District(state, num, ballot_rule=ballot_rule(state)), status, incumbent=inc
    )
    c.m4a_tier = tier
    if tier not in (M4ATier.UNKNOWN, M4ATier.NO_HEALTHCARE_POSITION):
        c.m4a_evidence = [Evidence("quote", "http://x", "rule", tier)]
    return c


# --- merging ---------------------------------------------------------------

def test_merge_joins_same_person_across_sources():
    a = mk("Jane Smith", "OH", 5, status=NominationStatus.PENDING_PRIMARY)
    a.fec_candidate_id = "H0OH05001"
    b = mk("Jane  SMITH", "OH", 5, status=NominationStatus.ON_BALLOT)
    merged = merge([a], [b])
    assert len(merged) == 1
    assert merged[0].fec_candidate_id == "H0OH05001"
    assert merged[0].status is NominationStatus.ON_BALLOT


def test_merge_keeps_different_people_apart():
    merged = merge([mk("Jane Smith", "OH", 5)], [mk("John Smith", "OH", 5)])
    assert len(merged) == 2


def test_conflicting_urls_are_recorded_not_silently_resolved():
    a = mk("Jane Smith", "OH", 5)
    a.campaign_url = "https://one.com"
    b = mk("Jane Smith", "OH", 5)
    b.campaign_url = "https://two.com"
    merged = merge([a], [b])
    assert merged[0].campaign_url == "https://one.com"
    assert any("campaign_url" in c for c in merged[0].conflicts)


# --- calendar enforcement --------------------------------------------------

def test_on_ballot_before_the_primary_is_downgraded():
    # Massachusetts votes Sept 1; nobody is on the ballot on Aug 26.
    c = apply_calendar([mk("Pat Q", "MA", 1)], AS_OF)[0]
    assert c.status is NominationStatus.PENDING_PRIMARY
    assert c.conflicts


def test_louisiana_filers_go_on_the_november_ballot_directly():
    c = apply_calendar([mk("Rene L", "LA", 2, status=NominationStatus.PENDING_PRIMARY)], AS_OF)[0]
    assert c.status is NominationStatus.ALL_PARTY_NOVEMBER
    assert c.on_general_ballot


def test_unknown_primary_date_flags_rather_than_downgrades():
    c = apply_calendar([mk("Ann O", "OH", 5)], AS_OF)[0]
    assert c.status is NominationStatus.ON_BALLOT
    assert any("unknown" in x for x in c.conflicts)


# --- coverage gaps ---------------------------------------------------------

def test_roster_reports_multiple_democrats_in_a_single_nominee_state():
    roster = build_roster([mk("A", "OH", 5), mk("B", "OH", 5)], AS_OF)
    assert any("2 Democrats" in g for g in roster.coverage_gaps)


def test_two_democrats_in_a_top_two_state_is_not_flagged():
    # California can legitimately send two Democrats to the general.
    roster = build_roster([mk("A", "CA", 12), mk("B", "CA", 12)], AS_OF)
    assert not any("Democrats marked on-ballot" in g for g in roster.coverage_gaps)


def test_roster_reports_districts_with_no_democrat():
    roster = build_roster([mk("A", "OH", 5)], AS_OF)
    assert any("no Democrat recorded" in g for g in roster.coverage_gaps)


# --- reporting denominators ------------------------------------------------

def test_unknown_is_excluded_from_the_classified_denominator():
    roster = Roster(candidates=[
        mk("A", "OH", 1, M4ATier.EXPLICIT_M4A),
        mk("B", "OH", 2, M4ATier.UNKNOWN),
    ])
    a = analyze(roster, AS_OF)
    assert a.total_on_ballot == 2
    assert a.classified == 1
    assert a.unknown == 1
    assert a.share(a.explicit_m4a) == 1.0                  # 1 of 1 classified
    assert a.share(a.explicit_m4a, of_classified=False) == 0.5  # 1 of 2 on ballot


def test_no_healthcare_position_counts_as_classified():
    # We read the site and found nothing; that is a finding, unlike UNKNOWN.
    roster = Roster(candidates=[mk("A", "OH", 1, M4ATier.NO_HEALTHCARE_POSITION)])
    assert analyze(roster, AS_OF).classified == 1


def test_two_readings_of_support_are_reported_separately():
    roster = Roster(candidates=[
        mk("A", "OH", 1, M4ATier.EXPLICIT_M4A),
        mk("B", "OH", 2, M4ATier.SINGLE_PAYER_SUBSTANCE),
        mk("C", "OH", 3, M4ATier.PUBLIC_OPTION),
    ])
    a = analyze(roster, AS_OF)
    assert a.explicit_m4a == 1
    assert a.single_payer_any == 2


def test_low_coverage_triggers_a_warning_in_the_report():
    roster = Roster(candidates=[mk(f"C{i}", "OH", i + 1, M4ATier.UNKNOWN) for i in range(9)])
    roster.candidates.append(mk("Known", "OH", 10, M4ATier.EXPLICIT_M4A))
    md = to_markdown(analyze(roster, AS_OF), roster)
    assert "Coverage is below 80%" in md


def test_empty_roster_does_not_divide_by_zero():
    a = analyze(Roster(), AS_OF)
    assert a.coverage == 0.0
    assert a.share(0) == 0.0
    to_markdown(a, Roster())


def test_csv_includes_evidence_and_conflicts():
    c = mk("Jane Smith", "OH", 5, M4ATier.EXPLICIT_M4A)
    c.conflicts.append("status mismatch")
    csv_text = to_csv(Roster(candidates=[c]))
    assert "Jane Smith" in csv_text
    assert "explicit_m4a" in csv_text
    assert "status mismatch" in csv_text


# --- website verification --------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://ballotpedia.org/Jane_Smith",
    "https://en.wikipedia.org/wiki/Jane_Smith",
    "https://smith.house.gov",
    "https://secure.actblue.com/donate/x",
    "https://www.facebook.com/janesmith",
    "https://www.politico.com/story/x",
])
def test_non_campaign_hosts_are_disqualified(url):
    assert is_disqualified(url) is not None


def test_real_campaign_domain_is_not_disqualified():
    assert is_disqualified("https://janesmithforcongress.com") is None


def test_scoring_accepts_a_genuine_campaign_site():
    c = mk("Jane Smith", "OH", 5)
    html = (
        "<html><head><title>Jane Smith for Congress</title></head><body>"
        "Donate today. Paid for by Jane Smith for Congress. OH-5 deserves better."
        "</body></html>"
    )
    score = score_candidate_url(html, "https://janesmithforcongress.com", c)
    assert score.accepted


def test_scoring_rejects_a_news_article_about_the_candidate():
    c = mk("Jane Smith", "OH", 5)
    html = "<html><head><title>Jane Smith wins primary - Local News</title></head>" \
           "<body>Jane Smith won her primary on Tuesday night.</body></html>"
    score = score_candidate_url(html, "https://localnews.example/article", c)
    assert not score.accepted


# --- adjudication ----------------------------------------------------------

def test_adjudication_requires_a_grounded_quote():
    c = mk("Jane Smith", "OH", 5)
    source = "I support Medicare for All and always have."
    assert apply_adjudication(c, source, lambda a, b: (M4ATier.EXPLICIT_M4A, "I support Medicare for All"))
    assert c.m4a_tier is M4ATier.EXPLICIT_M4A


def test_adjudication_with_an_invented_quote_is_rejected():
    c = mk("Bob R", "OH", 6)
    source = "I support a public option."
    assert not apply_adjudication(c, source, lambda a, b: (M4ATier.EXPLICIT_M4A, "I back Medicare for All"))
    assert c.m4a_tier is M4ATier.UNKNOWN
    assert "rejected" in c.m4a_notes


def test_adjudicator_exceptions_do_not_kill_the_run():
    c = mk("Bob R", "OH", 6)
    def boom(candidate, text):
        raise RuntimeError("provider down")
    assert not apply_adjudication(c, "text", boom)


def test_review_csv_contains_only_flagged_rows():
    ok = mk("Fine", "OH", 1, M4ATier.EXPLICIT_M4A)
    flagged = mk("Unclear", "OH", 2, M4ATier.EXPLICIT_M4A)
    flagged.m4a_notes = "REVIEW: weak evidence"
    assert needs_review(flagged) and not needs_review(ok)
    csv_text = to_review_csv([ok, flagged])
    assert "Unclear" in csv_text and "Fine" not in csv_text
