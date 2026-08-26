from dcp.models import (
    BallotRule, Candidate, District, M4ATier, NominationStatus, normalize_name,
)


def test_normalize_name_folds_cross_source_variation():
    assert normalize_name('Sarah "Sam" O\'Brien Jr.') == "sarah o'brien"
    assert normalize_name("ALEXANDRIA OCASIO-CORTEZ") == "alexandria ocasio-cortez"
    assert normalize_name("Jane Q. Smith") == normalize_name("Jane Smith")
    assert normalize_name("Robert Garcia (Bob)") == "robert garcia"


def test_normalize_name_keeps_different_people_distinct():
    assert normalize_name("Jane Smith") != normalize_name("John Smith")


def test_candidate_id_prefers_fec_id_and_is_stable():
    d = District("OH", 5)
    with_fec = Candidate("Jane Smith", d, NominationStatus.ON_BALLOT, fec_candidate_id="H0OH05001")
    assert with_fec.candidate_id == "H0OH05001"

    a = Candidate("Jane Smith", d, NominationStatus.ON_BALLOT)
    b = Candidate("Jane  SMITH", d, NominationStatus.ON_BALLOT)
    assert a.candidate_id == b.candidate_id           # same person, same id
    assert a.candidate_id.startswith("X")


def test_on_general_ballot_covers_all_party_statuses():
    on = {
        NominationStatus.ON_BALLOT, NominationStatus.PRESUMPTIVE,
        NominationStatus.ADVANCED_ALL_PARTY, NominationStatus.ALL_PARTY_NOVEMBER,
    }
    off = {
        NominationStatus.PENDING_PRIMARY, NominationStatus.LOST_PRIMARY,
        NominationStatus.WITHDREW, NominationStatus.DISQUALIFIED,
    }
    assert all(s.on_general_ballot for s in on)
    assert not any(s.on_general_ballot for s in off)


def test_unknown_tier_is_not_a_finding():
    # The distinction the whole denominator depends on.
    assert not M4ATier.UNKNOWN.is_finding
    assert M4ATier.NO_HEALTHCARE_POSITION.is_finding


def test_supports_universal_single_payer_excludes_public_option():
    assert M4ATier.EXPLICIT_M4A.supports_universal_single_payer
    assert M4ATier.SINGLE_PAYER_SUBSTANCE.supports_universal_single_payer
    assert not M4ATier.PUBLIC_OPTION.supports_universal_single_payer
    assert not M4ATier.ACA_STRENGTHEN.supports_universal_single_payer
