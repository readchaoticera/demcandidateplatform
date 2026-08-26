from pathlib import Path

import pytest

from dcp.models import District
from dcp.sources.ballotpedia import (
    _looks_like_person, _ordinal, district_page_url, parse_campaign_links,
)
from dcp.sources.fec import _district_from_fec, _tidy_name
from dcp.sources.wikipedia import (
    article_title, clean_name, is_democratic_party, parse_rows,
)

FIXTURES = Path(__file__).parent / "fixtures"


# --- FEC -------------------------------------------------------------------

def test_fec_name_is_reordered_from_last_first():
    assert _tidy_name("SMITH, JANE Q") == "Jane Q Smith"
    assert _tidy_name("OCASIO-CORTEZ, ALEXANDRIA") == "Alexandria Ocasio-Cortez"
    assert _tidy_name("Jane Smith") == "Jane Smith"


def test_fec_at_large_district_00_maps_to_AL():
    assert _district_from_fec("DE", "00").code == "DE-AL"
    assert _district_from_fec("AK", "00").code == "AK-AL"
    assert _district_from_fec("TX", "38").code == "TX-38"


def test_fec_district_00_in_multi_district_state_is_rejected():
    # A data error, not an at-large seat. Must not silently become CA-01.
    assert _district_from_fec("CA", "00") is None


# --- Wikipedia -------------------------------------------------------------

def test_democratic_party_labels_across_states():
    for label in ("Democratic", "Democratic (DFL)", "Democratic\u2013Farmer\u2013Labor",
                  "Democratic-NPL", "DFL", "Democratic / Working Families"):
        assert is_democratic_party(label), label
    for label in ("Republican", "Libertarian", "Independent", "Green", ""):
        assert not is_democratic_party(label), label


def test_clean_name_strips_annotations_and_rejects_placeholders():
    assert clean_name("Jim McGovern (presumptive)") == "Jim McGovern"
    assert clean_name("Zach Nunn (incumbent)") == "Zach Nunn"
    assert clean_name("Alice Nguyen [1]") == "Alice Nguyen"
    for junk in ("TBD", "TBA", "To be determined", "Undecided", "Vacant", ""):
        assert clean_name(junk) is None


def test_article_title_has_singular_form_for_single_seat_states():
    assert article_title("DE", plural=False).endswith("election in Delaware")
    assert article_title("OH", plural=True).endswith("elections in Ohio")


def test_infobox_nominee_is_extracted():
    html = (FIXTURES / "wiki_state_sample.html").read_text()
    rows = {r.district_number: r.democrats for r in parse_rows(html, "OH")}
    assert rows[1] == ["Alice Nguyen"]


def test_dfl_label_is_recognised_as_democratic():
    html = (FIXTURES / "wiki_state_sample.html").read_text()
    rows = {r.district_number: r.democrats for r in parse_rows(html, "OH")}
    assert rows[2] == ["Devon Park"]


def test_tbd_nominee_falls_back_to_the_primary_winner():
    # District 3's infobox says TBD; the primary table's top vote-getter wins.
    html = (FIXTURES / "wiki_state_sample.html").read_text()
    rows = {r.district_number: r.democrats for r in parse_rows(html, "OH")}
    assert rows[3] == ["Elena Moss"]


def test_top_two_district_can_return_two_democrats():
    # District 4 uses the "Candidate" label with two Democrats, as CA does.
    html = (FIXTURES / "wiki_state_sample.html").read_text()
    rows = {r.district_number: r.democrats for r in parse_rows(html, "OH")}
    assert rows[4] == ["Ivy Chen", "Jack Ross"]


def test_primary_losers_are_not_included():
    html = (FIXTURES / "wiki_state_sample.html").read_text()
    everyone = [n for r in parse_rows(html, "OH") for n in r.democrats]
    assert "Frank Toll" not in everyone     # lost D1 primary
    assert "Harold Kim" not in everyone     # lost D3 primary


def test_finance_fallback_only_applies_to_jungle_states():
    html = """<div class="mw-parser-output">
      <div class="mw-heading mw-heading2"><h2>District 1</h2></div>
      <table class="wikitable sortable">
        <tr><th>Campaign finance reports as of June 30</th></tr>
        <tr><th>Candidate</th><th>Raised</th></tr>
        <tr><th>Lauren Jewett (D)</th><td>$1</td></tr>
        <tr><th>Steve Scalise (R)</th><td>$2</td></tr>
      </table></div>"""
    assert parse_rows(html, "LA")[0].democrats == ["Lauren Jewett"]
    # Ohio runs a nominating primary, so filers are not automatically on the ballot.
    assert parse_rows(html, "OH")[0].democrats == []


# --- Ballotpedia -----------------------------------------------------------

@pytest.mark.parametrize("n,expected", [
    (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
    (11, "11th"), (12, "12th"), (13, "13th"),
    (21, "21st"), (22, "22nd"), (23, "23rd"), (52, "52nd"),
])
def test_ordinals(n, expected):
    assert _ordinal(n) == expected


def test_district_page_urls():
    assert district_page_url(District("OH", 5)).endswith("Ohio%27s_5th_Congressional_District_election,_2026")
    assert "At-Large" in district_page_url(District("DE", 1, at_large=True))


def test_looks_like_person_rejects_page_furniture():
    assert _looks_like_person("Jane Smith")
    assert _looks_like_person("Alexandria Ocasio-Cortez")
    for junk in ("See also", "Campaign website", "the election", "General election", "X"):
        assert not _looks_like_person(junk)


def test_parse_campaign_links_pairs_names_with_urls():
    html = """
    <div class="candidate">
      <h3><a href="/Jane_Smith">Jane Smith</a></h3>
      <p><a href="https://janesmithforcongress.com">Campaign website</a></p>
    </div>
    <div class="candidate">
      <h3><a href="/Bob_Ryder">Bob Ryder</a></h3>
      <p><a href="https://bobryder.com">Campaign website</a></p>
    </div>
    """
    links = {l.candidate_name: l.url for l in parse_campaign_links(html)}
    assert links["Jane Smith"] == "https://janesmithforcongress.com"
    assert links["Bob Ryder"] == "https://bobryder.com"


def test_parse_campaign_links_ignores_internal_links():
    html = '<div><h3><a href="/Jane_Smith">Jane Smith</a></h3>' \
           '<a href="https://ballotpedia.org/x">Campaign website</a></div>'
    assert parse_campaign_links(html) == []


# --- Congress (Medicare for All Act cosponsors) ------------------------------

def test_cosponsor_name_keys_include_quoted_nicknames():
    from dcp.sources.congress import name_keys
    # Rolls list members formally; rosters use the familiar name.
    assert "bobby" in name_keys('Robert "Bobby" Scott')
    assert "hank" in name_keys('Henry C. "Hank" Johnson')
    assert name_keys('Robert "Bobby" Scott') & name_keys("Bobby Scott")


def test_annotate_requires_district_and_name_to_match():
    from dcp.models import Candidate, District, NominationStatus
    from dcp.sources.congress import Cosponsor, annotate
    roll = [Cosponsor(name="Ted Lieu", district="CA-36", role="Cosponsor")]

    incumbent = Candidate("Ted Lieu", District("CA", 36), NominationStatus.ON_BALLOT)
    assert annotate([incumbent], roll) == 1
    assert incumbent.cosponsored_m4a_bill is True
    assert incumbent.incumbent

    # A challenger in the same seat must NOT inherit the incumbent's record.
    challenger = Candidate("Jane Doe", District("CA", 36), NominationStatus.ON_BALLOT)
    assert annotate([challenger], roll) == 0
    assert challenger.cosponsored_m4a_bill is None


def test_annotate_leaves_non_cosponsor_districts_unset():
    from dcp.models import Candidate, District, NominationStatus
    from dcp.sources.congress import Cosponsor, annotate
    roll = [Cosponsor(name="Ted Lieu", district="CA-36", role="Cosponsor")]
    other = Candidate("Someone Else", District("TX", 33), NominationStatus.ON_BALLOT)
    assert annotate([other], roll) == 0
    assert other.cosponsored_m4a_bill is None


def test_redistricted_member_still_matches_within_their_state():
    # Mid-decade maps moved several members: the roll lists the district they
    # were elected in, the roster the one they now run in.
    from dcp.models import Candidate, District, NominationStatus
    from dcp.sources.congress import Cosponsor, annotate
    roll = [Cosponsor(name="Lois Frankel", district="FL-22", role="Cosponsor")]
    cand = Candidate("Lois Frankel", District("FL", 23), NominationStatus.ON_BALLOT)
    assert annotate([cand], roll) == 1
    assert cand.cosponsored_m4a_bill is True
    assert any("redistricting" in p.note for p in cand.provenance)


def test_state_fallback_does_not_match_a_different_person():
    from dcp.models import Candidate, District, NominationStatus
    from dcp.sources.congress import Cosponsor, annotate
    roll = [Cosponsor(name="Lois Frankel", district="FL-22", role="Cosponsor")]
    # Shares a surname only - one token is not a strong match.
    other = Candidate("Marcus Frankel", District("FL", 25), NominationStatus.ON_BALLOT)
    assert annotate([other], roll) == 0
    assert other.cosponsored_m4a_bill is None
