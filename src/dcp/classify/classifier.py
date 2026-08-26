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

#: A single page below this is not worth classifying.
MIN_PAGE_CHARS = 120

#: Below this much text across ALL of a candidate's pages, we have not really
#: read their platform. Javascript-rendered sites return a near-empty shell to
#: a plain HTTP fetch, which looks identical to a candidate who states no
#: position - except the first is missing data and the second is a finding.
#: Measured against real sites: campaigns that genuinely state no coverage
#: position still publish several thousand characters of platform text.
MIN_CORPUS_CHARS = 2000


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
        if self.stance in (Stance.NEGATE, Stance.ATTRIBUTED, Stance.HEDGED):
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


#: Above this share of undecodable or control bytes, the "page" is not text.
#: Campaign sites serve PDFs, images and font files from paths that look like
#: ordinary pages; decoded as text these are noise that regexes still match.
#: A real one produced a false Medicare for All endorsement by matching "M4A"
#: inside binary data.
_BINARY_RATIO = 0.05


#: Leading bytes of formats campaigns publish from page-like URLs. Checked
#: because a ratio test alone is not enough: a PDF's header and object
#: dictionaries are ASCII, which held one real file just under the threshold
#: while "M4A" matched deeper in its compressed streams.
_MAGIC = (
    "%PDF",           # pdf
    "PK\x03\x04",     # zip, docx, xlsx, pptx
    "\x89PNG",        # png
    "GIF8",           # gif
    "\xff\xd8\xff",   # jpeg
    "RIFF",           # webp, wav
    "OggS",
    "ID3",
    "\x00\x00\x00",   # mp4 and friends
    "wOFF", "wOF2", "\x00\x01\x00\x00",  # fonts
)


def looks_binary(text: str, sample: int = 4000) -> bool:
    """Whether a decoded response body is binary rather than prose."""
    if not text:
        return False
    if text.lstrip()[:8].startswith(_MAGIC):
        return True
    head = text[:sample]
    bad = sum(
        1 for ch in head
        if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\t\n\r")
    )
    return bad / len(head) > _BINARY_RATIO


def extract_text(html: str) -> str:
    """Strip a page down to readable prose.

    Returns "" for binary payloads, so they contribute no text and no matches
    rather than yielding spurious ones.
    """
    if looks_binary(html):
        return ""
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


#: Characters of context kept either side of a match in the evidence quote.
_QUOTE_BEFORE, _QUOTE_AFTER = 150, 220

#: Sentence punctuation. Its absence over a long span means the text is a list
#: of labels rather than prose.
_PROSE = re.compile(r"[.!?;:]")

#: How much unpunctuated lead-in to keep. Enough for the words immediately
#: before the match, not enough for a navigation menu.
_NON_PROSE_LEAD = 45

def _quote_for(match: Match) -> str:
    """A readable quote centred on the match, with navigation trimmed off.

    Two problems, both from sites whose menus are not marked up as <nav>. The
    menu survives text extraction, and because it carries no sentence-ending
    punctuation it merges with the following prose into one very long
    "sentence" - so quoting from the sentence start shows a list of menu
    labels instead of the claim.

    So the quote is centred on the match, and leading context that is not prose
    is capped rather than kept in full. Prose has sentence punctuation; a run of
    menu labels does not, which distinguishes them without needing to recognise
    menus. The cap keeps the words immediately before the match - often the
    subject of the claim, as in "I am a strong supporter of ..." - while
    dropping the menu behind them.
    """
    text = match.sentence.strip()
    offset = len(match.sentence) - len(match.sentence.lstrip())
    m_start = max(0, match.start - offset)
    m_end = max(m_start, match.end - offset)

    start = max(0, m_start - _QUOTE_BEFORE)
    end = min(len(text), m_end + _QUOTE_AFTER)

    lead = text[start:m_start]
    if len(lead) > _NON_PROSE_LEAD and not _PROSE.search(lead):
        start = max(0, m_start - _NON_PROSE_LEAD)

    quote = text[start:end].strip()
    return ("\u2026" if start > 0 else "") + quote + ("\u2026" if end < len(text) else "")


def _evidence_from(match: Match) -> Evidence:
    return Evidence(
        quote=_quote_for(match),
        url="",  # filled in by the caller, which knows the source page
        matched_rule=match.rule.rule_id,
        tier=match.rule.tier,
        negated=match.stance is Stance.NEGATE,
        context=match.stance.value,
    )


def classify_text(text: str, source_url: str = "") -> ClassificationResult:
    """Classify one page's prose into a tier with supporting evidence."""
    result = ClassificationResult()

    if not text or len(text) < MIN_PAGE_CHARS:
        result.needs_review = True
        result.review_reason = "page had too little text to classify"
        return result

    matches = find_matches(text)
    if not matches:
        result.tier = M4ATier.NO_COVERAGE_POSITION
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
            result.tier = M4ATier.NO_COVERAGE_POSITION
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
    corpus_chars = 0

    for url, html in pages.items():
        text = extract_text(html)
        corpus_chars += len(text)
        res = classify_text(text, source_url=url)
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

    # Too little text overall means we did not read the platform. Only demote
    # a null result: a real quote found in a short page is still real evidence.
    if corpus_chars < MIN_CORPUS_CHARS and best.tier in (
        M4ATier.NO_COVERAGE_POSITION, M4ATier.UNKNOWN
    ):
        out = ClassificationResult(tier=M4ATier.UNKNOWN, needs_review=True)
        out.review_reason = (
            f"only {corpus_chars} characters retrieved across {len(pages)} page(s); "
            "site is likely Javascript-rendered"
        )
        out.matched_rules = sorted(all_rules)
        return out

    best.matched_rules = sorted(all_rules)
    best.explicitly_rejects_m4a = rejects
    if reviewed_reasons and not best.needs_review:
        best.notes = (best.notes + " | " if best.notes else "") + "; ".join(reviewed_reasons[:2])
    return best


def _rank(tier: M4ATier) -> int:
    """Position in TIER_ORDER; lower is more transformative."""
    return TIER_ORDER.index(tier)
