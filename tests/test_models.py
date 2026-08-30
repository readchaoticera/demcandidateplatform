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
    assert M4ATier.NO_COVERAGE_POSITION.is_finding


def test_supports_universal_single_payer_excludes_public_option():
    assert M4ATier.EXPLICIT_M4A.supports_universal_single_payer
    assert M4ATier.SINGLE_PAYER_SUBSTANCE.supports_universal_single_payer
    assert not M4ATier.PUBLIC_OPTION.supports_universal_single_payer
    assert not M4ATier.ACA_STRENGTHEN.supports_universal_single_payer


# --- combining evidence types ----------------------------------------------

def _cand():
    return Candidate("Jane Doe", District("OH", 5), NominationStatus.ON_BALLOT)


def test_resolved_tier_prefers_cosponsorship_over_everything():
    # A recorded legislative act outranks what a candidate puts on a website.
    c = _cand()
    c.m4a_tier = M4ATier.ACA_STRENGTHEN
    c.secondary_tier = M4ATier.PUBLIC_OPTION
    c.cosponsored_m4a_bill = True
    assert c.resolved_tier is M4ATier.EXPLICIT_M4A
    assert c.evidence_basis == "cosponsorship"


def test_campaign_site_outranks_news():
    c = _cand()
    c.m4a_tier = M4ATier.ACA_STRENGTHEN
    c.secondary_tier = M4ATier.EXPLICIT_M4A
    assert c.resolved_tier is M4ATier.ACA_STRENGTHEN
    assert c.evidence_basis == "campaign_site"


def test_news_fills_in_only_when_the_site_could_not_be_read():
    c = _cand()
    c.m4a_tier = M4ATier.UNKNOWN
    c.secondary_tier = M4ATier.EXPLICIT_M4A
    assert c.resolved_tier is M4ATier.EXPLICIT_M4A
    assert c.evidence_basis == "news"


def test_no_evidence_resolves_to_unknown():
    c = _cand()
    assert c.resolved_tier is M4ATier.UNKNOWN
    assert c.evidence_basis == "none"


def test_secondary_never_overwrites_the_site_only_measure():
    # The site-only figure must stay comparable across runs.
    c = _cand()
    c.m4a_tier = M4ATier.NO_COVERAGE_POSITION
    c.secondary_tier = M4ATier.EXPLICIT_M4A
    assert c.m4a_tier is M4ATier.NO_COVERAGE_POSITION


def test_human_review_outranks_every_automated_source():
    """A reviewed correction must survive re-runs, not be overwritten by them.

    It exists for material automation cannot reach at all, so it has to beat
    even the cosponsor roll.
    """
    c = _cand()
    c.m4a_tier = M4ATier.NO_COVERAGE_POSITION
    c.secondary_tier = M4ATier.PUBLIC_OPTION
    c.cosponsored_m4a_bill = True
    assert c.resolved_tier is M4ATier.EXPLICIT_M4A       # cosponsorship, so far

    c.override_tier = M4ATier.ACA_STRENGTHEN
    assert c.resolved_tier is M4ATier.ACA_STRENGTHEN
    assert c.evidence_basis == "human_review"


def test_unset_override_does_not_affect_resolution():
    c = _cand()
    c.m4a_tier = M4ATier.PUBLIC_OPTION
    assert c.override_tier is M4ATier.UNKNOWN
    assert c.resolved_tier is M4ATier.PUBLIC_OPTION
    assert c.evidence_basis == "campaign_site"


def test_add_provenance_refreshes_rather_than_duplicating():
    # Provenance now round-trips through roster.json, so a re-run of any stage
    # would otherwise append a second identical entry each time.
    from dcp.models import Candidate, District, NominationStatus
    cand = Candidate("Jane Doe", District("TX", 18), NominationStatus.ON_BALLOT)
    cand.add_provenance("fec_bulk", "https://fec.gov/x", "H2TX18456")
    first = cand.provenance[0].retrieved_at
    cand.add_provenance("fec_bulk", "https://fec.gov/x", "H2TX18456")
    assert len(cand.provenance) == 1
    assert cand.provenance[0].retrieved_at >= first
    # A different finding from the same source is a separate fact.
    cand.add_provenance("fec_bulk", "https://fec.gov/x", "incumbent")
    assert len(cand.provenance) == 2


def test_candidate_is_democratic_unless_told_otherwise():
    # The roster carries a small number of reviewed exceptions, and they must
    # be labelled rather than counted silently as Democrats.
    from dcp.models import Candidate, District, NominationStatus
    dem = Candidate("Jane Doe", District("TX", 18), NominationStatus.ON_BALLOT)
    assert dem.party == "Democratic"
    assert dem.to_dict()["party"] == "Democratic"
    ind = Candidate("Bill Hill", District("AK", 1, at_large=True),
                    NominationStatus.ON_BALLOT, party="Independent")
    assert ind.to_dict()["party"] == "Independent"


def test_excluded_status_is_not_on_the_general_ballot():
    # EXCLUDED means "really on the ballot, deliberately left out", so it must
    # not be counted, and must not be confused with a candidacy that ended.
    from dcp.models import NominationStatus
    assert not NominationStatus.EXCLUDED.on_general_ballot
    assert NominationStatus.EXCLUDED is not NominationStatus.WITHDREW
    assert NominationStatus.EXCLUDED is not NominationStatus.LOST_PRIMARY


def _member(**kw):
    from dcp.models import Candidate, District, NominationStatus
    c = Candidate("Jane Member", District("NJ", 9), NominationStatus.ON_BALLOT,
                  incumbent=True)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_incumbent_needs_cosponsorship_for_an_m4a_finding():
    # A sitting member can put their name on the bill. A press profile calling
    # them a supporter is not the same evidence, and crediting it puts them
    # alongside members who signed.
    from dcp.models import Bucket, M4ATier
    c = _member(secondary_tier=M4ATier.EXPLICIT_M4A)
    assert c.resolved_tier is M4ATier.UNKNOWN
    assert c.bucket is Bucket.NO_POSITION_FOUND
    assert c.evidence_basis == "none"


def test_incumbent_cosponsor_still_resolves_to_m4a():
    from dcp.models import Bucket, M4ATier
    c = _member(cosponsored_m4a_bill=True, secondary_tier=M4ATier.EXPLICIT_M4A)
    assert c.resolved_tier is M4ATier.EXPLICIT_M4A
    assert c.bucket is Bucket.SUPPORTS_M4A


def test_demotion_falls_back_to_what_their_own_site_says():
    # The claim is dropped, not replaced: whatever else their material says
    # stands rather than being overwritten with opposition.
    from dcp.models import Bucket, M4ATier
    c = _member(m4a_tier=M4ATier.ACA_STRENGTHEN, secondary_tier=M4ATier.EXPLICIT_M4A)
    assert c.resolved_tier is M4ATier.ACA_STRENGTHEN
    assert c.bucket is Bucket.DOES_NOT_SUPPORT_M4A


def test_demotion_does_not_apply_to_challengers():
    # Only a sitting member has the option of cosponsoring.
    from dcp.models import Candidate, District, NominationStatus, Bucket, M4ATier
    c = Candidate("Jane Challenger", District("NJ", 9), NominationStatus.ON_BALLOT)
    c.m4a_tier = M4ATier.EXPLICIT_M4A
    assert c.resolved_tier is M4ATier.EXPLICIT_M4A
    assert c.bucket is Bucket.SUPPORTS_M4A


def test_a_reviewed_override_outranks_the_incumbent_rule():
    # A person who looked at the record beats a general rule about records.
    from dcp.models import Bucket, M4ATier
    c = _member(override_tier=M4ATier.EXPLICIT_M4A)
    assert c.resolved_tier is M4ATier.EXPLICIT_M4A
    assert c.bucket is Bucket.SUPPORTS_M4A
    assert c.evidence_basis == "human_review"


def test_demotion_leaves_lower_tiers_untouched():
    from dcp.models import M4ATier
    for tier in (M4ATier.PUBLIC_OPTION, M4ATier.ACA_STRENGTHEN,
                 M4ATier.NO_COVERAGE_POSITION):
        assert _member(m4a_tier=tier).resolved_tier is tier
