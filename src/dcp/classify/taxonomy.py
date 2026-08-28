"""Phrase taxonomy for healthcare positions, plus stance detection.

Naive keyword matching fails badly on this particular question, in ways that
all bias the result in the same direction (overcounting support):

*   "Protect and strengthen Medicare" is a near-universal Democratic line and
    has nothing to do with Medicare for All. Bare "Medicare" is never a match.
*   Candidates describe positions they *oppose*: "I don't support Medicare for
    All" contains the phrase verbatim.
*   Candidates describe *other people's* positions: "my opponent wants to end
    Medicare as we know it", "some in my party back Medicare for All, but...".
*   Endorsement and press-clip pages quote third parties at length.

So each match is scored in the context of its sentence, and a stance -
affirmed, negated, or attributed to someone else - is resolved before the
match is allowed to set a tier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from ..models import M4ATier


class Stance(str, Enum):
    AFFIRM = "affirm"
    NEGATE = "negate"
    ATTRIBUTED = "attributed"
    """The position belongs to a third party (opponent, party, critics)."""
    NEUTRAL = "neutral"
    """Mentioned with no stance marker.

    On a candidate's own campaign site this counts as support: naming a policy
    on your issues page is the endorsement. Not so for tiers that require an
    explicit affirmation."""

    HEDGED = "hedged"
    """Named, but explicitly held at arm's length - "while I admire the goal
    of X, we should start with Y". Never counts as support at any tier: the
    sentence exists precisely to decline the position it names."""


@dataclass(frozen=True)
class Rule:
    rule_id: str
    tier: M4ATier
    pattern: str
    weight: float = 1.0
    """Higher weight wins when two rules of the same tier fire."""

    requires_affirm: bool = True
    """If False, a NEUTRAL mention counts; NEGATE and ATTRIBUTED still do not.

    False for every tier, and that is deliberate. This taxonomy is applied to
    a candidate's OWN campaign website, where naming a policy on the issues
    page IS the endorsement - that is what an issues page is for. Real copy
    routinely states positions without a first-person verb: "Co-sponsored the
    Medicare For All Act", "Working families deserve good jobs, Medicare for
    All", or a bare "Healthcare for All" heading. Requiring "I support" missed
    every one of those, including the lead sponsor of the bill. Statements
    that are negated ("I don't support...") or belong to someone else ("my
    opponent supports...") are still excluded by stance detection."""

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Tier 1: explicit Medicare for All / single-payer by name
# ---------------------------------------------------------------------------
EXPLICIT_RULES: tuple[Rule, ...] = (
    Rule("m4a.phrase", M4ATier.EXPLICIT_M4A,
         r"\bmedicare[\s\-\u2010-\u2015]*for[\s\-\u2010-\u2015]*all\b",
         weight=3.0, requires_affirm=False),
    Rule("m4a.abbrev", M4ATier.EXPLICIT_M4A, r"\bM4A\b",
         weight=2.0, requires_affirm=False),
    Rule("m4a.improved_expanded", M4ATier.EXPLICIT_M4A,
         r"\bimproved\s+and\s+expanded\s+medicare\b",
         weight=3.0, requires_affirm=False),
    Rule("m4a.single_payer", M4ATier.EXPLICIT_M4A, r"\bsingle[\s\-]?payer\b",
         weight=2.5, requires_affirm=False),
    Rule("m4a.bill", M4ATier.EXPLICIT_M4A, r"\bmedicare\s+for\s+all\s+act\b",
         weight=3.0, requires_affirm=False),
)

# ---------------------------------------------------------------------------
# Tier 2: single-payer in substance, without the brand name
# ---------------------------------------------------------------------------
SUBSTANCE_RULES: tuple[Rule, ...] = (
    Rule("sp.replace_private", M4ATier.SINGLE_PAYER_SUBSTANCE,
         r"\b(replac\w+|eliminat\w+|abolish\w+|get\s+rid\s+of)\b[^.]{0,60}"
         r"\bprivate\s+(health\s+)?insur\w+", weight=3.0, requires_affirm=False),
    Rule("sp.national_program", M4ATier.SINGLE_PAYER_SUBSTANCE,
         r"\bnational\s+health\s+(insurance|care)\s+(program|system|plan)\b",
         weight=2.5, requires_affirm=False),
    Rule("sp.one_plan_everyone", M4ATier.SINGLE_PAYER_SUBSTANCE,
         r"\b(one|a\s+single)\s+(public\s+)?(plan|program|system)\b[^.]{0,50}"
         r"\b(cover\w*|for)\s+(every|all)\b", weight=2.5, requires_affirm=False),
    Rule("sp.universal_public", M4ATier.SINGLE_PAYER_SUBSTANCE,
         r"\buniversal\b[^.]{0,40}\b(publicly[\s\-]funded|government[\s\-]funded|"
         r"tax[\s\-]funded)\b", weight=2.0, requires_affirm=False),
    Rule("sp.free_at_point", M4ATier.SINGLE_PAYER_SUBSTANCE,
         r"\bno\s+(premiums|copays|deductibles)\b[^.]{0,40}"
         r"\b(everyone|all\s+americans)\b", weight=1.5, requires_affirm=False),
)

# ---------------------------------------------------------------------------
# Tier 3: public option / Medicare expansion short of single-payer
# ---------------------------------------------------------------------------
PUBLIC_OPTION_RULES: tuple[Rule, ...] = (
    Rule("po.public_option", M4ATier.PUBLIC_OPTION, r"\bpublic\s+option\b", weight=3.0),
    Rule("po.buy_in", M4ATier.PUBLIC_OPTION,
         r"\b(medicare|medicaid)\s+buy[\s\-]?in\b|\bbuy\s+into\s+medicare\b", weight=2.5),
    Rule("po.lower_age", M4ATier.PUBLIC_OPTION,
         # Either order: "lower the Medicare age", but also "lower the age that
         # seniors can start receiving Medicare".
         r"\b(lower\w*|reduc\w*|drop\w*)\b[^.]{0,70}\bage\b[^.]{0,60}\bmedicare\b|"
         r"\bmedicare\b[^.]{0,50}\b(eligibility\s+)?age\b[^.]{0,30}\b(to|down)\b|"
         r"\bmedicare\s+(eligibility\s+)?age\s+to\s+\d{2}\b", weight=2.5),
    Rule("po.medicare_x", M4ATier.PUBLIC_OPTION, r"\bmedicare\s+x\b", weight=2.0),
    Rule("po.choice_frame", M4ATier.PUBLIC_OPTION,
         r"\b(option|choice)\s+to\s+(buy|enroll)\b[^.]{0,40}\bmedicare\b", weight=1.5),
)

# ---------------------------------------------------------------------------
# Tier 4: ACA-strengthening and cost measures, no structural change
# ---------------------------------------------------------------------------
ACA_RULES: tuple[Rule, ...] = (
    Rule("aca.protect", M4ATier.ACA_STRENGTHEN,
         # "improve" and "fix" are as common as "protect" and were missing.
         r"\b(protect\w*|defend\w*|strengthen\w*|expand\w*|improv\w*|fix\w*|"
         r"bolster\w*|shore\s+up|save|build\s+on)\b[^.]{0,40}"
         r"\b(affordable\s+care\s+act|obamacare|\bACA\b)", weight=2.5,
         requires_affirm=False),
    Rule("aca.reimbursement", M4ATier.ACA_STRENGTHEN,
         r"\breimbursement\s+rates?\b", weight=1.0, requires_affirm=False),
    Rule("aca.exchange", M4ATier.ACA_STRENGTHEN,
         r"\b(health\s?care\s+)?(exchange|marketplace)\b[^.]{0,40}"
         r"\b(premium|tax\s+credit|coverage|option)s?\b|"
         r"\b(premium|tax\s+credit)s?\b[^.]{0,40}\b(exchange|marketplace)\b",
         weight=1.2, requires_affirm=False),
    Rule("aca.preexisting", M4ATier.ACA_STRENGTHEN,
         r"\bpre[\s\-]?existing\s+conditions?\b", weight=2.0, requires_affirm=False),
    Rule("aca.subsidies", M4ATier.ACA_STRENGTHEN,
         r"\b(premium\s+tax\s+credits?|enhanced\s+subsidies|marketplace\s+subsidies)\b",
         weight=2.0, requires_affirm=False),
    Rule("aca.drug_negotiation", M4ATier.ACA_STRENGTHEN,
         # Either order, and "costs" as often as "prices": campaigns write both
         # "negotiate drug prices" and "prices negotiated through Medicare".
         r"\bnegotiat\w+\b[^.]{0,60}\b(drug|prescription|medication|price|cost)s?\b|"
         r"\b(drug|prescription|medication|price|cost)s?\b[^.]{0,60}\bnegotiat\w+\b",
         weight=1.5, requires_affirm=False),
    Rule("aca.insulin", M4ATier.ACA_STRENGTHEN,
         r"\b(cap\w*|\$?35)\b[^.]{0,30}\binsulin\b", weight=1.5, requires_affirm=False),
    Rule("aca.medicaid_gap", M4ATier.ACA_STRENGTHEN,
         # Campaigns write "expand Medicaid" at least as often as the noun
         # phrase "Medicaid expansion"; both orders have to match.
         r"\bmedicaid\s+(expansion|coverage\s+gap)\b|"
         r"\bexpand\w*\s+medicaid\b|\bmedicaid\b[^.]{0,20}\bexpansion\b",
         weight=1.5, requires_affirm=False),
    Rule("aca.insurer_accountability", M4ATier.ACA_STRENGTHEN,
         r"\b(regulat\w+|rein\s+in|crack\s+down\s+on|hold\b[^.]{0,20}\baccountable)\b"
         r"[^.]{0,40}\b(insurance|insurer|health\s+plan)\w*\b",
         weight=1.2, requires_affirm=False),
    Rule("aca.rural_hospitals", M4ATier.ACA_STRENGTHEN,
         r"\b(rural|community)\s+(hospital|clinic|health\s+cent(er|re))s?\b",
         weight=1.0, requires_affirm=False),
    Rule("aca.coverage_access", M4ATier.ACA_STRENGTHEN,
         r"\b(expand\w*|increas\w*|improv\w*)\s+(access\s+to\s+)?"
         r"(affordable\s+)?(health\s?care|health\s+coverage|coverage)\b",
         weight=1.0, requires_affirm=False),
    Rule("aca.universal_aspiration", M4ATier.ACA_STRENGTHEN,
         # "Guarantee affordable health care for every American" with no
         # mechanism named. Universal coverage as a goal is a stated position,
         # but on its own it is not single-payer - the higher tiers catch a
         # candidate who names one, and the ladder prefers them.
         r"\b(guarantee\w*|ensur\w*|secur\w*|deliver\w*)\b[^.]{0,40}"
         r"\b(affordable\s+|quality\s+)*(health\s?care|health\s+coverage|coverage)\b"
         r"[^.]{0,50}\b(every|all|each)\b",
         weight=1.2, requires_affirm=False),
    Rule("aca.protect_medicare", M4ATier.ACA_STRENGTHEN,
         r"\b(protect\w*|defend\w*|save|strengthen\w*)\b[^.]{0,25}\bmedicare\b",
         weight=1.0, requires_affirm=False),
)

# ---------------------------------------------------------------------------
# Explicit opposition. Only fires on the candidate's own voice.
# ---------------------------------------------------------------------------
OPPOSED_RULES: tuple[Rule, ...] = (
    Rule("opp.govt_takeover", M4ATier.OPPOSED,
         r"\bgovernment\s+takeover\s+of\s+(our\s+)?health\s?care\b", weight=3.0,
         requires_affirm=False),
    Rule("opp.socialized", M4ATier.OPPOSED,
         r"\bsocialized\s+medicine\b", weight=2.0, requires_affirm=False),
    Rule("opp.keep_your_plan", M4ATier.OPPOSED,
         r"\b(keep|protect)\b[^.]{0,30}\b(your|their)\s+(current\s+)?"
         r"(private\s+)?(insurance|plan|coverage)\b[^.]{0,40}\bif\s+you\s+like\b",
         weight=1.5, requires_affirm=False),
)

ALL_RULES: tuple[Rule, ...] = (
    EXPLICIT_RULES + SUBSTANCE_RULES + PUBLIC_OPTION_RULES + ACA_RULES + OPPOSED_RULES
)

# ---------------------------------------------------------------------------
# Stance cues
# ---------------------------------------------------------------------------

#: First-person commitment. Strongest evidence the position is the candidate's.
AFFIRM_CUES = re.compile(
    r"\b(i\s+(strongly\s+)?(support|back|endorse|believe|champion|co[\s\-]?sponsor\w*)|"
    r"i(\s+a|')m\s+(a\s+)?(proud\s+)?(supporter|co[\s\-]?sponsor|advocate)|"
    # "I will <anything>" is a first-person commitment. Safe to take broadly:
    # detect_stance checks negation first, so "I will not support X" still
    # resolves to NEGATE rather than reaching this cue.
    r"i\s+will\b|"
    r"we\s+(must|need(\s+to)?|should|deserve|have\s+to|can|will)\b|"
    r"my\s+plan|that(\s+i|')s\s+why\s+i\s+support|"
    r"stands?\s+for|is\s+fighting\s+for|has\s+championed|fight(ing)?\s+for|"
    r"it(\s+i|')s\s+time\s+(for|to)\b|"
    r"believes?\s+(in|that)|supports?\b|advocat\w+\s+for|"
    r"guarantee\w*\s+(that\s+)?(every|all)\b|"
    r"co[\s\-]?sponsor\w*|cosponsor\w*|voted\s+for|introduc\w+|"
    r"has\s+(fought|pushed|worked)\s+for|champion\w*|"
    r"deserves?\b|will\s+deliver)\b",
    re.IGNORECASE,
)

#: Negation of the matched policy.
NEGATE_CUES = re.compile(
    r"\b(do(es)?\s?n[o']t\s+(support|back|believe)|will\s+not\s+support|won[''`]t\s+support|"
    r"oppos\w+|against\b|reject\w*|stop\b|block\b|prevent\b|"
    r"rather\s+than|instead\s+of|as\s+opposed\s+to|"
    r"not\s+(a\s+)?(supporter|the\s+answer|the\s+right)|"
    r"never\s+support|no\s+to\b|say\s+no\b)",
    re.IGNORECASE,
)

#: The position belongs to somebody else.
ATTRIBUTION_CUES = re.compile(
    r"\b(my\s+opponent|his\s+(plan|position|proposal)|her\s+(plan|position|proposal)|"
    r"their\s+(plan|position|proposal)|the\s+(republican|democratic)\s+(party|nominee)|"
    r"republicans?\s+(want|would|have|are)|democrats?\s+(want|would)|"
    r"critics\s+|opponents\s+|some\s+(say|argue|in\s+congress)|"
    r"the\s+far[\s\-](left|right)|extremists?\b|"
    r"according\s+to|said\s+in\s+a\s+statement|told\s+the\b)",
    re.IGNORECASE,
)

#: Conditional / aspirational hedging that weakens an affirmation to NEUTRAL.
HEDGE_CUES = re.compile(
    r"\b(open\s+to|willing\s+to\s+(consider|look)|would\s+consider|"
    r"eventually|long[\s\-]term\s+goal|someday|in\s+an\s+ideal\s+world|"
    r"while\s+i\s+(admire|respect))\b",
    re.IGNORECASE,
)


def _clause(text: str) -> str:
    """The final clause of ``text``, i.e. everything after the last break."""
    return re.split(r"[,;:]", text)[-1]


def detect_stance(sentence: str, match_start: int, match_end: int) -> Stance:
    """Classify the candidate's stance toward a matched phrase in its sentence.

    Order matters. Attribution is checked first: if the sentence is about
    somebody else's position, no stance of the candidate's is expressed,
    regardless of how many support verbs it contains.
    """
    before = sentence[:match_start]
    after = sentence[match_end:]

    if ATTRIBUTION_CUES.search(before) or ATTRIBUTION_CUES.search(after[:80]):
        return Stance.ATTRIBUTED

    # Negation is scoped: look at the clause leading up to the match, plus a
    # short window after it, so "support X rather than Y" negates Y not X.
    neg_window_before = before[-140:]
    neg_window_after = after[:60]
    if NEGATE_CUES.search(neg_window_before) or NEGATE_CUES.search(neg_window_after):
        return Stance.NEGATE

    # Hedges scope to their own clause, not the whole sentence. In "While I
    # admire the goal of Medicare for All, we should start with a public
    # option" the hedge governs Medicare for All only - letting it bleed past
    # the comma would suppress the public-option position the sentence exists
    # to state.
    if HEDGE_CUES.search(_clause(before)):
        return Stance.HEDGED

    if AFFIRM_CUES.search(before[-200:]) or AFFIRM_CUES.search(after[:60]):
        return Stance.AFFIRM

    return Stance.NEUTRAL
