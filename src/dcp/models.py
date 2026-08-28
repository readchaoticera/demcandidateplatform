"""Core schema for the candidate roster and platform analysis.

Design notes that matter for correctness:

*   A district does not always resolve to exactly one Democrat. Under the
    top-two rules used by California and Washington, and Alaska's top-four
    ranked-choice rule, a district's general-election ballot may carry two or
    more Democrats, or none at all. Louisiana in 2026 holds its all-party
    primary *on* general election day, so it has no party nominees whatsoever.
    The roster is therefore a list of (district, candidate) pairs, never a
    dict keyed by district.

*   ``UNKNOWN`` is not ``NO_COVERAGE_POSITION``. The first means we failed to
    read the candidate's material; the second means we read it and there was
    no healthcare position in it. Collapsing the two inflates the denominator
    of any "share who support X" statistic, so they stay distinct all the way
    through to the report.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from enum import Enum
from typing import Any, Iterable, Optional


class BallotRule(str, Enum):
    """How a state decides who appears on the November general-election ballot."""

    PARTY_NOMINEE = "party_nominee"
    """Standard closed/open/semi-closed primary: each party nominates one candidate."""

    TOP_TWO = "top_two"
    """CA, WA. Top two primary finishers advance regardless of party.
    A district may send two Democrats, or zero, to the general."""

    TOP_FOUR_RCV = "top_four_rcv"
    """AK. Top four advance from an all-party primary; general is ranked-choice."""

    JUNGLE_NOV = "jungle_nov"
    """LA 2026. The all-party primary *is* the November election, with a
    December runoff if nobody clears 50%. There is no such thing as a
    Democratic nominee; there are only Democratic candidates."""


class NominationStatus(str, Enum):
    """Whether this candidate is actually on the November ballot."""

    ON_BALLOT = "on_ballot"
    """Won the primary / advanced, and certified for the general."""

    PRESUMPTIVE = "presumptive"
    """Primary won or uncontested, but the state has not certified the ballot."""

    PENDING_PRIMARY = "pending_primary"
    """Primary has not been held yet. Not eligible for the roster."""

    ADVANCED_ALL_PARTY = "advanced_all_party"
    """Advanced from a top-two / top-four all-party primary."""

    ALL_PARTY_NOVEMBER = "all_party_november"
    """LA: filed for the November all-party ballot. No nomination occurred."""

    LOST_PRIMARY = "lost_primary"
    WITHDREW = "withdrew"
    DISQUALIFIED = "disqualified"

    @property
    def on_general_ballot(self) -> bool:
        """True for statuses that put a name in front of a November voter."""
        return self in {
            NominationStatus.ON_BALLOT,
            NominationStatus.PRESUMPTIVE,
            NominationStatus.ADVANCED_ALL_PARTY,
            NominationStatus.ALL_PARTY_NOVEMBER,
        }


class M4ATier(str, Enum):
    """Tiered healthcare position scale, most to least transformative.

    The tiers are ordered but not evenly spaced; treat them as ordinal
    categories, not a numeric score to be averaged.
    """

    EXPLICIT_M4A = "explicit_m4a"
    """Affirmatively names Medicare for All / single-payer as their position."""

    SINGLE_PAYER_SUBSTANCE = "single_payer_substance"
    """Describes universal single-payer coverage without using the brand name
    (e.g. "one public plan covering every American, replacing private insurance")."""

    PUBLIC_OPTION = "public_option"
    """Public option, Medicare buy-in, lowering the Medicare age. Universal
    coverage as an aspiration, but keeps the private market."""

    ACA_STRENGTHEN = "aca_strengthen"
    """Protect/expand the ACA, cap drug costs, extend subsidies. No structural change."""

    NO_COVERAGE_POSITION = "no_coverage_position"
    """Material was read and states no position on how coverage should work.

    This is NOT "says nothing about health". Candidates in this tier often
    write at length about cancer research, mental health, reproductive rights
    or veterans' care while taking no position on the insurance question this
    project measures. Naming the tier for healthcare generally would misreport
    them."""

    OPPOSED = "opposed"
    """Explicitly opposes Medicare for All / single-payer."""

    UNKNOWN = "unknown"
    """Could not retrieve or read the candidate's material. NOT a finding."""

    @property
    def is_finding(self) -> bool:
        """False for UNKNOWN, which represents missing data rather than a position."""
        return self is not M4ATier.UNKNOWN

    @property
    def supports_universal_single_payer(self) -> bool:
        """The narrow reading of "supports Medicare for All"."""
        return self in {M4ATier.EXPLICIT_M4A, M4ATier.SINGLE_PAYER_SUBSTANCE}


class Bucket(str, Enum):
    """The three-way public grouping of the seven tiers.

    The tiers stay the unit of classification - they carry the evidence and the
    rules that fired - but they are finer than most readers need. These buckets
    are the reporting view.

    Note that ``NONE_OR_NOT_FOUND`` deliberately merges "we read the material
    and it states no coverage position" with "we could not read the material".
    Those are different facts, and the tier is still recorded per candidate, so
    the distinction remains available in the data even though the headline
    grouping sets it aside.
    """

    M4A_OR_SINGLE_PAYER = "m4a_or_single_payer"
    ACA_OR_PUBLIC_OPTION = "aca_or_public_option"
    NONE_OR_NOT_FOUND = "none_or_not_found"


#: Tier -> bucket. ``OPPOSED`` groups with the incremental bucket: in this field
#: it means opposing Medicare for All while backing the existing system, which
#: is a stated position rather than an absence of one.
TIER_BUCKET: dict["M4ATier", Bucket] = {}

BUCKET_LABELS: dict[Bucket, str] = {
    Bucket.M4A_OR_SINGLE_PAYER: "Medicare for All or single-payer",
    Bucket.ACA_OR_PUBLIC_OPTION: "Strengthen ACA or add public option",
    Bucket.NONE_OR_NOT_FOUND: "No coverage position / not found",
}

#: Display order, most to least transformative.
BUCKET_ORDER: tuple[Bucket, ...] = (
    Bucket.M4A_OR_SINGLE_PAYER,
    Bucket.ACA_OR_PUBLIC_OPTION,
    Bucket.NONE_OR_NOT_FOUND,
)


#: Display order for reports, most to least transformative.
TIER_ORDER: tuple[M4ATier, ...] = (
    M4ATier.EXPLICIT_M4A,
    M4ATier.SINGLE_PAYER_SUBSTANCE,
    M4ATier.PUBLIC_OPTION,
    M4ATier.ACA_STRENGTHEN,
    M4ATier.NO_COVERAGE_POSITION,
    M4ATier.OPPOSED,
    M4ATier.UNKNOWN,
)


TIER_BUCKET.update({
    M4ATier.EXPLICIT_M4A: Bucket.M4A_OR_SINGLE_PAYER,
    M4ATier.SINGLE_PAYER_SUBSTANCE: Bucket.M4A_OR_SINGLE_PAYER,
    M4ATier.PUBLIC_OPTION: Bucket.ACA_OR_PUBLIC_OPTION,
    M4ATier.ACA_STRENGTHEN: Bucket.ACA_OR_PUBLIC_OPTION,
    M4ATier.OPPOSED: Bucket.ACA_OR_PUBLIC_OPTION,
    M4ATier.NO_COVERAGE_POSITION: Bucket.NONE_OR_NOT_FOUND,
    M4ATier.UNKNOWN: Bucket.NONE_OR_NOT_FOUND,
})


def bucket_for(tier: "M4ATier") -> Bucket:
    return TIER_BUCKET[tier]


@dataclass(frozen=True)
class Provenance:
    """Where a fact came from. Every non-derived field should carry one."""

    source: str
    """Short source id, e.g. "wikipedia", "ballotpedia", "fec", "campaign_site"."""

    url: str
    retrieved_at: datetime
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["retrieved_at"] = self.retrieved_at.isoformat()
        return d


@dataclass
class Evidence:
    """A verbatim quote supporting a classification decision.

    Every tier assignment above NO_COVERAGE_POSITION must be backed by at
    least one Evidence, so a human can audit any row in the final table.
    """

    quote: str
    url: str
    matched_rule: str
    """Which taxonomy rule fired, for auditing and for tuning the patterns."""

    tier: M4ATier
    negated: bool = False
    context: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tier"] = self.tier.value
        return d


@dataclass
class District:
    state: str
    """Two-letter USPS code."""

    number: int
    """1-indexed district number. At-large districts are 1 (not 0), matching
    FEC and Ballotpedia convention; ``at_large`` records the distinction."""

    ballot_rule: BallotRule = BallotRule.PARTY_NOMINEE
    at_large: bool = False

    @property
    def code(self) -> str:
        """Canonical district id, e.g. "CA-12", "DE-AL"."""
        return f"{self.state}-{'AL' if self.at_large else f'{self.number:02d}'}"

    def __str__(self) -> str:
        return self.code


@dataclass
class Candidate:
    """One Democratic candidate on (or headed for) a November general ballot."""

    full_name: str
    district: District
    status: NominationStatus

    # --- identity / linkage -------------------------------------------------
    fec_candidate_id: Optional[str] = None
    incumbent: bool = False
    wikipedia_url: Optional[str] = None
    ballotpedia_url: Optional[str] = None

    # --- campaign web presence ----------------------------------------------
    campaign_url: Optional[str] = None
    campaign_url_confidence: float = 0.0
    """0.0-1.0. See websites.score_candidate_url for how this is derived."""

    issues_urls: list[str] = field(default_factory=list)

    # --- analysis -----------------------------------------------------------
    m4a_tier: M4ATier = M4ATier.UNKNOWN
    m4a_evidence: list[Evidence] = field(default_factory=list)
    m4a_notes: str = ""

    # --- corroborating signals (optional, populated by later passes) ---------
    cosponsored_m4a_bill: Optional[bool] = None
    """Incumbents only: co-sponsor of the Medicare for All Act in the current
    Congress. ``None`` means not checked or not an incumbent."""

    endorsements: list[str] = field(default_factory=list)

    # --- human review -------------------------------------------------------
    override_tier: "M4ATier" = None  # set in __post_init__
    """A reviewed correction. Outranks every automated source, because a person
    who opened the page is better evidence than any automated read of it."""

    override_note: str = ""
    override_source: str = ""
    override_reviewer: str = ""

    # --- secondary (news-sourced) assessment --------------------------------
    secondary_tier: "M4ATier" = None  # set in __post_init__
    """Position derived from news coverage and third-party profiles, for
    candidates whose own site could not be read. Weaker evidence than either
    the campaign site or the cosponsor roll, so it is kept separate and never
    overwrites them."""

    secondary_confidence: float = 0.0
    secondary_note: str = ""
    secondary_sources: list[str] = field(default_factory=list)

    # --- bookkeeping --------------------------------------------------------
    provenance: list[Provenance] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    """Human-readable notes where sources disagreed. Never silently resolved."""

    def __post_init__(self) -> None:
        if self.secondary_tier is None:
            self.secondary_tier = M4ATier.UNKNOWN
        if self.override_tier is None:
            self.override_tier = M4ATier.UNKNOWN

    @property
    def resolved_tier(self) -> "M4ATier":
        """Best available position, combining all three evidence types.

        Precedence reflects evidential strength, not convenience:

        0. **A reviewed correction**, where a person has read the page. This
           outranks the rest: it exists for material automation cannot reach
           at all, such as Javascript-rendered sites.
        1. **Cosponsorship** of the Medicare for All Act - a recorded
           legislative act, and the least deniable evidence there is.
        2. **The candidate's own campaign site** - what they choose to tell
           voters, which is the measure the primary analysis reports.
        3. **News coverage** - third-party reporting, used only where the site
           could not be read at all.

        ``m4a_tier`` remains the site-only measure so the two can be compared;
        this property is what the combined figures use.
        """
        if self.override_tier.is_finding:
            return self.override_tier
        if self.cosponsored_m4a_bill:
            return M4ATier.EXPLICIT_M4A
        if self.m4a_tier.is_finding:
            return self.m4a_tier
        if self.secondary_tier.is_finding:
            return self.secondary_tier
        return M4ATier.UNKNOWN

    @property
    def bucket(self) -> Bucket:
        """The three-way grouping of ``resolved_tier``."""
        return bucket_for(self.resolved_tier)

    @property
    def evidence_basis(self) -> str:
        """Which evidence type set ``resolved_tier``."""
        if self.override_tier.is_finding:
            return "human_review"
        if self.cosponsored_m4a_bill:
            return "cosponsorship"
        if self.m4a_tier.is_finding:
            return "campaign_site"
        if self.secondary_tier.is_finding:
            return "news"
        return "none"

    @property
    def candidate_id(self) -> str:
        """Stable synthetic id, used to join across runs when FEC id is absent."""
        if self.fec_candidate_id:
            return self.fec_candidate_id
        basis = f"{self.district.code}|{normalize_name(self.full_name)}"
        return "X" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:9].upper()

    @property
    def on_general_ballot(self) -> bool:
        return self.status.on_general_ballot

    def add_provenance(self, source: str, url: str, note: str = "") -> None:
        self.provenance.append(
            Provenance(source=source, url=url, retrieved_at=datetime.utcnow(), note=note)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "full_name": self.full_name,
            "state": self.district.state,
            "district": self.district.code,
            "ballot_rule": self.district.ballot_rule.value,
            "status": self.status.value,
            "on_general_ballot": self.on_general_ballot,
            "incumbent": self.incumbent,
            "fec_candidate_id": self.fec_candidate_id,
            "campaign_url": self.campaign_url,
            "campaign_url_confidence": round(self.campaign_url_confidence, 3),
            "issues_urls": list(self.issues_urls),
            "m4a_tier": self.m4a_tier.value,
            "m4a_evidence": [e.to_dict() for e in self.m4a_evidence],
            "m4a_notes": self.m4a_notes,
            "resolved_tier": self.resolved_tier.value,
            "bucket": self.bucket.value,
            "evidence_basis": self.evidence_basis,
            "override_tier": self.override_tier.value,
            "override_note": self.override_note,
            "override_source": self.override_source,
            "override_reviewer": self.override_reviewer,
            "secondary_tier": self.secondary_tier.value,
            "secondary_confidence": round(self.secondary_confidence, 3),
            "secondary_note": self.secondary_note,
            "secondary_sources": list(self.secondary_sources),
            "cosponsored_m4a_bill": self.cosponsored_m4a_bill,
            "endorsements": list(self.endorsements),
            "wikipedia_url": self.wikipedia_url,
            "ballotpedia_url": self.ballotpedia_url,
            "provenance": [p.to_dict() for p in self.provenance],
            "conflicts": list(self.conflicts),
        }


@dataclass
class Roster:
    """The full set of Democratic candidates, plus what we know is missing."""

    candidates: list[Candidate] = field(default_factory=list)
    generated_at: datetime = field(default_factory=datetime.utcnow)
    coverage_gaps: list[str] = field(default_factory=list)
    """Explicit record of districts/states we could not resolve, so the report
    can state its own incompleteness rather than implying full coverage."""

    def on_ballot(self) -> list[Candidate]:
        return [c for c in self.candidates if c.on_general_ballot]

    def by_state(self, state: str) -> list[Candidate]:
        return [c for c in self.candidates if c.district.state == state.upper()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "candidate_count": len(self.candidates),
            "on_ballot_count": len(self.on_ballot()),
            "coverage_gaps": list(self.coverage_gaps),
            "candidates": [c.to_dict() for c in self.candidates],
        }


def normalize_name(name: str) -> str:
    """Fold a display name for matching across sources.

    Handles the common cross-source variations: nicknames in quotes, middle
    initials, suffixes, and punctuation. Deliberately conservative - it is
    better to leave two records unmerged (and flag a conflict) than to merge
    two different people.
    """
    import re

    s = name.strip().lower()
    s = re.sub(r'"[^"]*"', " ", s)          # drop "Nickname"
    s = re.sub(r"\([^)]*\)", " ", s)         # drop (Nickname)
    s = s.replace(".", " ").replace(",", " ")
    s = re.sub(r"\b(jr|sr|ii|iii|iv|md|phd|esq)\b", " ", s)
    s = re.sub(r"[^a-z\s'-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    parts = [p for p in s.split(" ") if len(p) > 1]  # drop middle initials
    return " ".join(parts)
