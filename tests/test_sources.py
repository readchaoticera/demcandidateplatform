from pathlib import Path

import pytest

from dcp.models import District
from dcp.sources.ballotpedia import (
    _looks_like_person, _ordinal, district_page_url, parse_campaign_links,
)
from dcp.sources.fec import _district_from_fec, _tidy_name
from dcp.sources.wikipedia import parse_district_number, parse_rows

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

def test_parse_ordinal_district_numbers():
    assert parse_district_number("1st", "OH") == (1, False)
    assert parse_district_number("12th", "CA") == (12, False)
    assert parse_district_number("At-large", "DE") == (1, True)
    assert parse_district_number("Statewide summary", "OH") == (None, False)


def test_parse_district_number_rejects_out_of_range():
    assert parse_district_number("40th", "OH") == (None, False)  # OH has 15 seats


def test_wikipedia_rows_select_only_democrats():
    html = (FIXTURES / "wiki_state_sample.html").read_text()
    rows = {r.district_number: r.democrats for r in parse_rows(html, "OH")}
    assert rows[1] == ["Alice Nguyen"]      # Republican in the same cell excluded
    assert rows[2] == ["Devon Park"]        # Democrat listed after the Republican
    assert rows[3] == ["Elena Moss"]        # primary loser excluded


def test_wikipedia_primary_loser_does_not_suppress_the_winner():
    # A per-cell "lost primary" annotation must not blank the whole district.
    html = (FIXTURES / "wiki_state_sample.html").read_text()
    rows = {r.district_number: r.democrats for r in parse_rows(html, "OH")}
    assert "Frank Toll" not in rows[3]
    assert rows[3]


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
