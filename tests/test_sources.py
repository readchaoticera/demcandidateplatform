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


# --- FEC bulk candidate master file ----------------------------------------

#: Shape of cn.txt: pipe-delimited, headerless, "LAST, FIRST" names.
_CN = "\n".join([
    "H6CA40123|KIM-VARET, ESTHER|DEM|2026|CA|H|40|C|C|C001",
    "H6CA40999|NGUYEN, LONG|DEM|2026|CA|H|40|C|C|C002",
    "H2TX18456|GREEN, ALEXANDER N|DEM|2026|TX|H|18|I|C|C003",
    "H8NM03111|LEGER FERNANDEZ, TERESA|DEM|2026|NM|H|03|I|C|C004",
    "H0VA03222|SCOTT, ROBERT C|DEM|2026|VA|H|03|I|C|C005",
    "H4AK00164|HAFNER, ERIC|DEM|2026|AK|H|00|C|N|C006",
    "H6TX18777|SMITH, JOHN|REP|2026|TX|H|18|C|C|C007",
    "H6TX18888|OLD, CANDIDATE|DEM|2024|TX|H|18|C|P|C008",
    "S6TX00999|SENATE, PERSON|DEM|2026|TX|S|00|C|C|C009",
])


def _bulk():
    from dcp.sources import fec_bulk
    return fec_bulk, fec_bulk.parse(_CN, 2026)


def test_fec_bulk_keeps_only_democratic_house_filers_for_the_year():
    _, filers = _bulk()
    names = {f.name for f in filers}
    assert "John Smith" not in names          # Republican
    assert "Candidate Old" not in names       # 2024 cycle
    assert "Person Senate" not in names       # Senate, not House
    assert len(filers) == 6


def test_fec_bulk_at_large_district_00_becomes_AL():
    _, filers = _bulk()
    hafner = next(f for f in filers if f.name == "Eric Hafner")
    assert hafner.district_code == "AK-AL"
    assert hafner.active is False             # status N, not yet a candidate


def test_fec_bulk_marks_incumbents_and_active_status():
    _, filers = _bulk()
    green = next(f for f in filers if f.district_code == "TX-18")
    assert green.incumbent and green.active


def test_fec_bulk_matches_on_surname_plus_first_initial():
    from dcp.models import Candidate, District, NominationStatus
    fec_bulk, filers = _bulk()
    by_district = fec_bulk.index_by_district(filers)
    cand = Candidate("Al Green", District("TX", 18), NominationStatus.ON_BALLOT)
    assert fec_bulk.match(cand, by_district["TX-18"]).candidate_id == "H2TX18456"


def test_fec_bulk_matches_a_nickname_when_the_surname_is_unique_in_district():
    # The FEC files legal names, so "Bobby Scott" never matches "Robert C Scott"
    # on first initial. Inside one district a lone surname is unambiguous.
    from dcp.models import Candidate, District, NominationStatus
    fec_bulk, filers = _bulk()
    by_district = fec_bulk.index_by_district(filers)
    cand = Candidate("Bobby Scott", District("VA", 3), NominationStatus.ON_BALLOT)
    assert fec_bulk.match(cand, by_district["VA-03"]).candidate_id == "H0VA03222"


def test_fec_bulk_matches_a_compound_surname_split_differently():
    from dcp.models import Candidate, District, NominationStatus
    fec_bulk, filers = _bulk()
    by_district = fec_bulk.index_by_district(filers)
    cand = Candidate("Teresa Leger Fernandez", District("NM", 3), NominationStatus.ON_BALLOT)
    assert fec_bulk.match(cand, by_district["NM-03"]).candidate_id == "H8NM03111"


def test_fec_bulk_returns_none_rather_than_guessing_between_two_filers():
    from dcp.models import Candidate, District, NominationStatus
    fec_bulk, filers = _bulk()
    by_district = fec_bulk.index_by_district(filers)
    cand = Candidate("Maria Torres", District("CA", 40), NominationStatus.ON_BALLOT)
    assert fec_bulk.match(cand, by_district["CA-40"]) is None


def test_fec_bulk_statewide_fallback_needs_two_shared_name_tokens():
    # A member whose district was redrawn: the FEC still files the old seat.
    from dcp.models import Candidate, District, NominationStatus
    fec_bulk, filers = _bulk()
    by_district = fec_bulk.index_by_district(filers)
    moved = Candidate("Teresa Leger Fernandez", District("NM", 1), NominationStatus.ON_BALLOT)
    assert fec_bulk.match_statewide(moved, by_district).candidate_id == "H8NM03111"
    # One shared token is not enough to claim a match: "Al Green" and the FEC's
    # "Alexander N Green" overlap only on the surname.
    other = Candidate("Al Green", District("TX", 9), NominationStatus.ON_BALLOT)
    assert fec_bulk.match_statewide(other, by_district) is None


def test_fec_bulk_annotate_attaches_ids_and_flags_the_unmatched():
    from dcp.models import Candidate, District, NominationStatus
    fec_bulk, filers = _bulk()
    matched = Candidate("Al Green", District("TX", 18), NominationStatus.ON_BALLOT)
    missing = Candidate("Nobody Here", District("TX", 18), NominationStatus.ON_BALLOT)
    stats = fec_bulk.annotate([matched, missing], filers)
    assert stats == {"matched": 1, "unmatched": 1, "incumbents": 1,
                     "statewide": 0, "redistricted": 0}
    assert matched.fec_candidate_id == "H2TX18456" and matched.incumbent
    assert missing.fec_candidate_id is None and missing.conflicts


def test_fec_bulk_reports_seats_with_filers_but_no_candidate():
    from dcp.models import Candidate, District, NominationStatus
    fec_bulk, filers = _bulk()
    roster = [Candidate("Al Green", District("TX", 18), NominationStatus.ON_BALLOT)]
    gaps = fec_bulk.districts_with_filers_but_no_candidate(roster, filers)
    assert "CA-40" in gaps and "TX-18" not in gaps
    # Hafner's filing is status N, so AK-AL raises no flag from an inactive filer.
    assert "AK-AL" not in gaps


# --- Wikipedia: blanket-primary states -------------------------------------

#: Alaska's article shape. The top-four primary sends several Democrats
#: forward, one of whom has withdrawn, so the primary table's leading Democrat
#: is not on the November ballot.
_AK_HTML = """
<div class="mw-parser-output">
<table class="wikitable">
  <caption>Blanket primary results</caption>
  <tr><th>Party</th><th>Candidate</th><th>Votes</th><th>%</th></tr>
  <tr><td>Republican</td><td>Nick Begich III</td><td>69,201</td><td>45.2</td></tr>
  <tr><td>Democratic</td><td>Matt Schultz (withdrawn)</td><td>12,268</td><td>8.0</td></tr>
  <tr><td>Democratic</td><td>Eric Hafner</td><td>5,774</td><td>3.8</td></tr>
  <tr><td>Democratic</td><td>John B. Williams</td><td>4,084</td><td>2.7</td></tr>
</table>
<table class="wikitable">
  <caption>2026 Alaska's at-large congressional district election</caption>
  <tr><th>Party</th><th>Candidate</th><th>First choice</th></tr>
  <tr><td>Republican</td><td>Nick Begich III</td><td>TBD</td></tr>
  <tr><td>Independent</td><td>Bill Hill</td><td>TBD</td></tr>
  <tr><td>Democratic</td><td>Eric Hafner</td><td>TBD</td></tr>
</table>
</div>
"""


def test_top_four_state_reads_the_general_ballot_not_the_primary_leader():
    # Under a blanket primary there is no nominee, so "most Democratic votes"
    # is the wrong question. Schultz led the Democrats and then withdrew.
    rows = parse_rows(_AK_HTML, "AK")
    assert [r.democrats for r in rows] == [["Eric Hafner"]]


def test_withdrawn_candidates_are_not_read_as_nominees():
    from dcp.sources.wikipedia import democratic_primary_candidates
    from bs4 import BeautifulSoup
    nodes = [BeautifulSoup(_AK_HTML, "lxml")]
    names = [n for n, _ in democratic_primary_candidates(nodes)]
    assert "Matt Schultz" not in names
    assert "Eric Hafner" in names


# --- Cook Political Report ratings, via Wikipedia ---------------------------

_RATINGS_HTML = """
<table class="wikitable">
  <tr><th>Constituency</th><th>Incumbent</th><th>Ratings</th></tr>
  <tr><th>District</th><th>CPVI</th><th>Incumbent</th><th>Last result</th>
      <th>Sabato Aug. 26, 2026</th><th>Cook Aug. 25, 2026 [ 3 ]</th><th>IE Aug. 20, 2026</th></tr>
  <tr><td>Alabama 2</td><td>R+7</td><td>Shomari Figures</td><td>54.6% D</td>
      <td>Solid R</td><td>Likely R (flip)</td><td>Lean R</td></tr>
  <tr><td>Alaska at-large</td><td>R+6</td><td>Nick Begich III</td><td>51.3% R</td>
      <td>Safe R</td><td>Tossup</td><td>Lean R</td></tr>
  <tr><td>Puerto Rico at-large</td><td></td><td>—</td><td>—</td>
      <td>—</td><td>Solid D</td><td>—</td></tr>
</table>
"""


def test_cook_column_is_found_by_header_not_by_position():
    # The table carries a dozen raters and gains more through the cycle, so a
    # fixed index would quietly start reporting Sabato's call as Cook's.
    from dcp.sources.ratings import parse_national
    got = parse_national(_RATINGS_HTML)
    assert got["AL-02"] == ("Likely R", "Aug. 25, 2026")
    assert got["AK-AL"][0] == "Toss Up"


def test_ratings_drop_the_flip_annotation_but_keep_the_level():
    from dcp.sources.ratings import normalise_rating
    assert normalise_rating("Likely R (flip)") == "Likely R"
    assert normalise_rating("Solid D (hold)") == "Solid D"
    # Wikipedia uses "Safe" and "Tossup" where Cook says "Solid" and "Toss Up".
    assert normalise_rating("Safe R") == "Solid R"
    assert normalise_rating("Tossup") == "Toss Up"
    assert normalise_rating("Toss-up") == "Toss Up"
    assert normalise_rating("—") is None


def test_district_labels_map_to_codes_and_reject_non_states():
    from dcp.sources.ratings import district_code
    assert district_code("Alabama 2") == "AL-02"
    assert district_code("Alaska at-large") == "AK-AL"
    assert district_code("New Hampshire 1") == "NH-01"
    # Territories send delegates, not representatives, and are not in the roster.
    assert district_code("Puerto Rico at-large") is None
    # A seat number the state does not have is a parse error, not a district.
    assert district_code("Delaware 4") is None
    assert district_code("Wyoming 2") is None


def test_ratings_skip_districts_the_source_does_not_carry():
    # An unrated district must stay absent rather than defaulting to Solid:
    # "nobody published a rating" and "every rater says safe" are different.
    from dcp.sources.ratings import parse_national
    got = parse_national(_RATINGS_HTML)
    assert "PR-AL" not in got and "CA-12" not in got


_STATE_HTML = """
<div class="mw-parser-output">
<div class="mw-heading mw-heading2"><h2>District 1</h2></div>
<table class="wikitable">
  <tr><th>Source</th><th>Ranking</th><th>As of</th></tr>
  <tr><td>The Cook Political Report [ 26 ]</td><td>Lean D</td><td>April 7, 2026</td></tr>
  <tr><td>Sabato's Crystal Ball [ 28 ]</td><td>Likely D</td><td>March 26, 2026</td></tr>
</table>
<div class="mw-heading mw-heading2"><h2>District 2</h2></div>
<table class="wikitable">
  <tr><th>Source</th><th>Ranking</th><th>As of</th></tr>
  <tr><td>Sabato's Crystal Ball [ 28 ]</td><td>Safe R</td><td>April 10, 2025</td></tr>
  <tr><td>The Cook Political Report [ 26 ]</td><td>Solid R</td><td>November 2, 2025</td></tr>
</table>
</div>
"""


def test_per_state_articles_supply_the_safe_seats():
    from dcp.sources.ratings import parse_state
    got = parse_state(_STATE_HTML, "OH")
    assert got["OH-01"] == ("Lean D", "April 7, 2026")
    # Cook's row is second here: the source column decides, not the row order.
    assert got["OH-02"] == ("Solid R", "November 2, 2025")


#: A top-two district where both slots went to the same party. Two Republicans
#: advanced and the leading Democrat finished third, so the district sends no
#: Democrat to November at all.
_TOP_TWO_HTML = """
<div class="mw-parser-output">
<div class="mw-heading mw-heading2"><h2>District 12</h2></div>
<table class="infobox">
  <tr><th>Candidate</th><td>Ken Calvert</td><td>Young Kim</td></tr>
  <tr><th>Party</th><td>Republican</td><td>Republican</td></tr>
</table>
<table class="wikitable">
  <caption>Primary results</caption>
  <tr><th>Party</th><th>Candidate</th><th>Votes</th><th>%</th></tr>
  <tr><td>Republican</td><td>Ken Calvert (incumbent)</td><td>75,811</td><td>34.89</td></tr>
  <tr><td>Republican</td><td>Young Kim (incumbent)</td><td>44,818</td><td>20.63</td></tr>
  <tr><td>Democratic</td><td>Esther Kim-Varet</td><td>36,072</td><td>16.60</td></tr>
  <tr><td>Democratic</td><td>Lisa Ramirez</td><td>30,495</td><td>14.03</td></tr>
</table>
</div>
"""


def test_top_two_district_that_advances_no_democrat_yields_none():
    # The leading Democrat in an all-party primary is not a nominee, and here
    # did not advance at all. Reading her as one put an eliminated candidate on
    # the roster for a seat contested by two Republicans.
    rows = parse_rows(_TOP_TWO_HTML, "CA")
    assert [r.democrats for r in rows] == [[]]


def test_party_nominee_state_still_uses_the_primary_leader():
    # The same table in a state that nominates one candidate per party: there
    # the leading Democrat really is the nominee, and must not be lost.
    rows = parse_rows(_TOP_TWO_HTML.replace("Ken Calvert", "Some Republican"), "OH")
    assert [r.democrats for r in rows] == [["Esther Kim-Varet"]]
