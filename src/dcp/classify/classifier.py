"""Rule-based healthcare-position classifier.

The classifier is deliberately rule-based rather than a model call, for three
reasons: every decision is traceable to a named rule and a verbatim quote, it
is deterministic across re-runs, and it costs nothing to re-run while tuning.

Cases the rules cannot settle are not guessed at. They are flagged
``needs_review``, and ``adjudicate.py`` can route just those to a human or to
an LLM pass. A political dataset that quietly guesses on its hard cases is
worse than one that reports them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ..models import TIER_ORDER, Evidence, M4ATier
from .taxonomy import ALL_RULES, Rule, Stance, detect_stance

#: Tiers that represent affirmative support, ranked most to least transformative.
_SUPPORT_LADDER: tuple[M4ATier, ...] = (
    M4ATier.EXPLICIT_M4A,
    M4ATier.SINGLE_PAYER_SUBSTANCE,
    M4ATier.PUBLIC_OPTION,
    M4ATier.ACA_STRENGTHEN,
)

#: Page chrome that produces false positives (donation asks, footers, nav).
_STRIP_SELECTORS = (
    "script", "style", "nav", "footer", "noscript", "form", "svg",
    "[role=navigation]", "[aria-hidden=true]",
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])|\n{2,}|(?<=[.!?])(?=[A-Z])")


@dataclass
class Match:
    rule: Rule
    stance: Stance
    sentence: str
    start: int
    end: int

    @property
    def counts_as_support(self) -> bool:
        """Whether this match may set a support tier."""
        if self.stance in (Stance.NEGATE, Stance.ATTRIBUTED):
            return False
        if self.rule.requires_affirm:
            return self.stance is Stance.AFFIRM
        return self.stance in (Stance.AFFIRM, Stance.NEUTRAL)


@dataclass
class ClassificationResult:
    tier: M4ATier = M4ATier.UNKNOWN
    evidence: list[Evidence] = field(default_factory=list)
    confidence: float = 0.0
    needs_review: bool = False
    review_reason: str = ""
    explicitly_rejects_m4a: bool = False
    """True when the candidate, in their own voice, disclaims Medicare for All.
    Tracked separately from ``tier`` because "I back a public option, not
    Medicare for All" is a public-option position AND an M4A rejection."""

    matched_rules: list[str] = field(default_factory=list)
    notes: str = ""


def extract_text(html: str) -> str:
    """Strip a page down to readable prose."""
    soup = BeautifulSoup(html, "lxml")
    for sel in _STRIP_SELECTORS:
        for node in soup.select(sel):
            node.decompose()
    text = soup.get_text(separator=" ")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p and p.strip()]
    # Very long "sentences" are usually run-together nav text; cap for safety.
    return [p[:1200] for p in parts]


def find_matches(text: str, rules: Iterable[Rule] = ALL_RULES) -> list[Match]:
    matches: list[Match] = []
    for sentence in split_sentences(text):
        for rule in rules:
            for m in rule.compiled().finditer(sentence):
                matches.append(
                    Match(rule=rule, stance=detect_stance(sentence, m.start(), m.end()),
                          sentence=sentence, start=m.start(), end=m.end())
                )
    return matches


def _evidence_from(match: Match) -> Evidence:
    return Evidence(
        quote=match.sentence.strip()[:400],
        url="",  # filled in by the caller, which knows the source page
        matched_rule=match.rule.rule_id,
        tier=match.rule.tier,
        negated=match.stance is Stance.NEGATE,
        context=match.stance.value,
    )


def classify_text(text: str, source_url: str = "") -> ClassificationResult:
    """Classify one page's prose into a tier with supporting evidence."""
    result = ClassificationResult()

    if not text or len(text) < 120:
        result.needs_review = True
        result.review_reason = "page had too little text to classify"
        return result

    matches = find_matches(text)
    if not matches:
        result.tier = M4ATier.NO_HEALTHCARE_POSITION
        result.confidence = 0.6
        return result

    result.matched_rules = sorted({m.rule.rule_id for m in matches})

    # Did the candidate disclaim M4A in their own voice?
    result.explicitly_rejects_m4a = any(
        m.rule.tier is M4ATier.EXPLICIT_M4A and m.stance is Stance.NEGATE
        for m in matches
    )

    supporting = [m for m in matches if m.counts_as_support]
    by_tier: dict[M4ATier, list[Match]] = {}
    for m in supporting:
        by_tier.setdefault(m.rule.tier, []).append(m)

    chosen: Optional[M4ATier] = None
    for tier in _SUPPORT_LADDER:
        if by_tier.get(tier):
            chosen = tier
            break

    explicit_opposition = [
        m for m in matches
        if m.rule.tier is M4ATier.OPPOSED and m.stance is not Stance.ATTRIBUTED
    ]

    if chosen is None:
        if explicit_opposition or result.explicitly_rejects_m4a:
            result.tier = M4ATier.OPPOSED
            result.evidence = [_evidence_from(m) for m in (explicit_opposition or matches)[:3]]
            result.confidence = 0.7
        else:
            # Phrases appeared, but only attributed to others or negated.
            result.tier = M4ATier.NO_HEALTHCARE_POSITION
            result.confidence = 0.4
            result.needs_review = True
            result.review_reason = (
                "healthcare phrases present but none in the candidate's own voice"
            )
        return result

    result.tier = chosen
    tier_matches = sorted(by_tier[chosen], key=lambda m: -m.rule.weight)
    result.evidence = [_evidence_from(m) for m in tier_matches[:3]]
    for ev in result.evidence:
        ev.url = source_url

    # Confidence: strongest rule weight, bumped for corroboration, capped.
    top_weight = max(m.rule.weight for m in tier_matches)
    corroboration = min(len(tier_matches), 3) * 0.05
    affirmed = any(m.stance is Stance.AFFIRM for m in tier_matches)
    result.confidence = min(0.95, top_weight / 3.0 * 0.8 + corroboration + (0.1 if affirmed else 0))

    # Flag the genuinely ambiguous combinations for review.
    if chosen in (M4ATier.EXPLICIT_M4A, M4ATier.SINGLE_PAYER_SUBSTANCE) and result.explicitly_rejects_m4a:
        result.needs_review = True
        result.review_reason = "page both endorses and disclaims single-payer"
    elif chosen is M4ATier.EXPLICIT_M4A and not affirmed:
        result.needs_review = True
        result.review_reason = "M4A named without a first-person commitment cue"
    elif result.confidence < 0.5:
        result.needs_review = True
        result.review_reason = "weak evidence"

    if result.explicitly_rejects_m4a and chosen is M4ATier.PUBLIC_OPTION:
        result.notes = "supports a public option while explicitly rejecting Medicare for All"

    return result


def classify_pages(pages: dict[str, str]) -> ClassificationResult:
    """Classify a candidate across several pages, keeping the strongest finding.

    ``pages`` maps URL -> HTML. Campaign sites scatter healthcare language
    across an issues index, a healthcare subpage, and sometimes an "about"
    page; the candidate's position is the most transformative one they claim
    anywhere in their own voice.
    """
    best: Optional[ClassificationResult] = None
    all_rules: set[str] = set()
    rejects = False
    reviewed_reasons: list[str] = []

    for url, html in pages.items():
        res = classify_text(extract_text(html), source_url=url)
        all_rules.update(res.matched_rules)
        rejects = rejects or res.explicitly_rejects_m4a
        if res.needs_review and res.review_reason:
            reviewed_reasons.append(f"{url}: {res.review_reason}")
        if best is None or _rank(res.tier) < _rank(best.tier):
            best = res
        elif _rank(res.tier) == _rank(best.tier) and res.confidence > best.confidence:
            best = res

    if best is None:
        out = ClassificationResult()
        out.needs_review = True
        out.review_reason = "no pages retrieved"
        return out

    best.matched_rules = sorted(all_rules)
    best.explicitly_rejects_m4a = rejects
    if reviewed_reasons and not best.needs_review:
        best.notes = (best.notes + " | " if best.notes else "") + "; ".join(reviewed_reasons[:2])
    return best


def _rank(tier: M4ATier) -> int:
    """Position in TIER_ORDER; lower is more transformative."""
    return TIER_ORDER.index(tier)
