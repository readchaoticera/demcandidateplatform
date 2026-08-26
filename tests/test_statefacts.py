from datetime import date

import pytest

from dcp import statefacts
from dcp.models import BallotRule
from dcp.statefacts import (
    SEAT_COUNTS, UnresolvedReason, all_districts, ballot_rule, districts,
    primary_held, unresolved_states, unsettled_field_seats, yields_party_nominee,
)

AS_OF = date(2026, 8, 26)


def test_apportionment_totals_435():
    assert sum(SEAT_COUNTS.values()) == 435
    assert len(all_districts()) == 435


def test_at_large_states_render_as_AL():
    assert districts("DE")[0].code == "DE-AL"
    assert districts("WY")[0].code == "WY-AL"
    assert districts("MT")[0].code == "MT-01"  # 2 seats, not at-large


def test_non_standard_ballot_rules():
    assert ballot_rule("CA") is BallotRule.TOP_TWO
    assert ballot_rule("WA") is BallotRule.TOP_TWO
    assert ballot_rule("AK") is BallotRule.TOP_FOUR_RCV
    assert ballot_rule("LA") is BallotRule.JUNGLE_NOV
    assert ballot_rule("OH") is BallotRule.PARTY_NOMINEE


def test_only_party_nominee_states_yield_a_nominee():
    assert yields_party_nominee("OH")
    for state in ("CA", "WA", "AK", "LA"):
        assert not yields_party_nominee(state)


def test_louisiana_primary_never_counts_as_held_before_election_day():
    # Its all-party primary IS the November election.
    assert primary_held("LA", AS_OF) is False
    assert primary_held("LA", date(2026, 10, 1)) is False


def test_unknown_primary_date_is_none_not_false(monkeypatch):
    # None means "we don't know"; False would wrongly assert it is upcoming,
    # and True would assert nominees that may not exist.
    monkeypatch.setitem(statefacts.VERIFIED_PRIMARY_DATES, "OH", None)
    monkeypatch.delitem(statefacts.VERIFIED_PRIMARY_DATES, "OH")
    assert primary_held("OH", AS_OF) is None


def test_calendar_is_populated_for_every_state_except_louisiana():
    # Louisiana must stay absent: it has a ballot rule, not a primary date.
    missing = set(SEAT_COUNTS) - set(statefacts.VERIFIED_PRIMARY_DATES)
    assert missing == {"LA"}


def test_settled_date_waits_for_a_scheduled_runoff(monkeypatch):
    monkeypatch.setitem(statefacts.VERIFIED_PRIMARY_DATES, "ZZ", date(2026, 5, 19))
    monkeypatch.setitem(statefacts.SEAT_COUNTS, "ZZ", 1)
    assert statefacts.settled_date("ZZ") == date(2026, 5, 19)
    monkeypatch.setitem(statefacts.RUNOFF_DATES, "ZZ", date(2026, 6, 16))
    assert statefacts.settled_date("ZZ") == date(2026, 6, 16)
    # Between primary and runoff the field is not yet settled.
    assert primary_held("ZZ", date(2026, 6, 1)) is False
    assert primary_held("ZZ", date(2026, 6, 20)) is True


def test_september_primaries_are_upcoming_on_aug_26():
    unresolved = unresolved_states(AS_OF)
    for state in ("MA", "NH", "RI", "DE"):
        assert unresolved[state].reason is UnresolvedReason.PRIMARY_UPCOMING


def test_unsettled_seats_excludes_our_own_calendar_gap():
    # 14 seats behind September primaries + 6 Louisiana seats.
    assert unsettled_field_seats(AS_OF) == 20
    assert unresolved_states(AS_OF)["LA"].reason is UnresolvedReason.NO_NOMINATION


def test_after_all_primaries_only_louisiana_remains_unsettled():
    assert unsettled_field_seats(date(2026, 9, 20)) == SEAT_COUNTS["LA"]
