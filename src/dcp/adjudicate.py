"""Review queue for classifications the rules could not settle.

Ambiguous cases are routed here rather than guessed at. Two sinks are
supported: a CSV queue for human review, and an optional LLM adjudicator.

The LLM path is deliberately narrow. It sees only the flagged candidate's
extracted quotes and is asked for a tier plus a justification quote that must
appear verbatim in the supplied text; a response whose quote is not found in
the source is rejected. That keeps the model from inventing evidence, which is
the specific failure this dataset cannot tolerate.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Callable, Iterable, Optional

from .models import Candidate, M4ATier

log = logging.getLogger(__name__)

#: Signature of an adjudicator: (candidate, extracted_text) -> (tier, quote) or None
Adjudicator = Callable[[Candidate, str], Optional[tuple[M4ATier, str]]]

REVIEW_COLUMNS = (
    "candidate_id", "full_name", "district", "campaign_url",
    "proposed_tier", "confidence", "review_reason", "evidence_quote",
)


def needs_review(candidate: Candidate) -> bool:
    return "review" in (candidate.m4a_notes or "").lower()


def to_review_csv(candidates: Iterable[Candidate]) -> str:
    """Emit the flagged rows as a CSV for a human to work through."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=REVIEW_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for c in candidates:
        if not needs_review(c):
            continue
        ev = c.m4a_evidence[0] if c.m4a_evidence else None
        writer.writerow({
            "candidate_id": c.candidate_id,
            "full_name": c.full_name,
            "district": c.district.code,
            "campaign_url": c.campaign_url or "",
            "proposed_tier": c.m4a_tier.value,
            "confidence": round(c.campaign_url_confidence, 3),
            "review_reason": c.m4a_notes,
            "evidence_quote": ev.quote if ev else "",
        })
    return buf.getvalue()


def apply_adjudication(
    candidate: Candidate, source_text: str, adjudicator: Adjudicator
) -> bool:
    """Run an adjudicator on one candidate, rejecting unfounded answers.

    Returns True if the candidate's tier was changed. A proposed quote that
    does not appear in ``source_text`` is discarded and logged: an adjudicator
    that cannot ground its answer does not get to move the number.
    """
    try:
        verdict = adjudicator(candidate, source_text)
    except Exception as exc:
        log.warning("adjudicator failed for %s: %s", candidate.full_name, exc)
        return False
    if verdict is None:
        return False

    tier, quote = verdict
    normalized = " ".join(source_text.split()).lower()
    if quote and " ".join(quote.split()).lower() not in normalized:
        log.warning(
            "rejected adjudication for %s: quote not found in source text",
            candidate.full_name,
        )
        candidate.m4a_notes += " | adjudication rejected (ungrounded quote)"
        return False

    old = candidate.m4a_tier
    candidate.m4a_tier = tier
    candidate.m4a_notes = (
        f"adjudicated {old.value} -> {tier.value}"
        + (f" | {candidate.m4a_notes}" if candidate.m4a_notes else "")
    )
    return True
