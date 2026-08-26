"""Merge sources into a single roster.

Each source knows something the others do not: the FEC has canonical ids and
the full filing universe, Wikipedia has primary results, Ballotpedia has
campaign URLs. Merging them is where duplicates and contradictions surface.

The rule throughout is that disagreements are **recorded, not resolved**. If
Wikipedia says a district's nominee is one person and the FEC has a different
active filer, both facts land in ``Candidate.conflicts`` and the row is
reviewable. Silently picking a winner would make the dataset look cleaner than
the underlying evidence actually is.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Iterable, Optional

from .models import Candidate, NominationStatus, Roster, normalize_name
from .statefacts import (
    GENERAL_ELECTION,
    SEAT_COUNTS,
    UnresolvedReason,
    ballot_rule,
    primary_held,
    unresolved_states,
    yields_party_nominee,
)

log = logging.getLogger(__name__)


def _key(c: Candidate) -> tuple[str, str]:
    return (c.district.code, normalize_name(c.full_name))


def merge(
    primary_source: Iterable[Candidate],
    *others: Iterable[Candidate],
) -> list[Candidate]:
    """Merge candidate records, preferring earlier sources for scalar fields.

    Matching is on (district, normalized name). Conservative by design: a name
    that normalizes differently across sources yields two rows, which a human
    can spot, rather than a wrong merge, which they cannot.
    """
    merged: dict[tuple[str, str], Candidate] = {}

    for cand in primary_source:
        merged.setdefault(_key(cand), cand)

    for source in others:
        for cand in source:
            key = _key(cand)
            existing = merged.get(key)
            if existing is None:
                merged[key] = cand
                continue
            _fold_into(existing, cand)

    return list(merged.values())


def _fold_into(target: Candidate, extra: Candidate) -> None:
    """Copy facts from ``extra`` into ``target``, flagging contradictions."""
    target.provenance.extend(extra.provenance)

    for field in ("fec_candidate_id", "campaign_url", "wikipedia_url", "ballotpedia_url"):
        new = getattr(extra, field)
        old = getattr(target, field)
        if new and not old:
            setattr(target, field, new)
        elif new and old and new != old:
            target.conflicts.append(f"{field}: {old!r} vs {new!r}")

    if extra.status is not target.status:
        # A definite ballot status beats the FEC's default "pending".
        if target.status is NominationStatus.PENDING_PRIMARY and extra.status.on_general_ballot:
            target.status = extra.status
        elif extra.status is not NominationStatus.PENDING_PRIMARY:
            target.conflicts.append(f"status: {target.status.value} vs {extra.status.value}")

    if extra.incumbent and not target.incumbent:
        target.incumbent = True
    for url in extra.issues_urls:
        if url not in target.issues_urls:
            target.issues_urls.append(url)


def apply_calendar(candidates: Iterable[Candidate], as_of: date) -> list[Candidate]:
    """Force status to reflect what the calendar makes possible.

    A source may list a "presumptive nominee" in a state whose primary has not
    happened. That is a projection, not a fact, and it must not be counted as
    on-ballot.
    """
    out: list[Candidate] = []
    for cand in candidates:
        state = cand.district.state
        held = primary_held(state, as_of)
        rule = ballot_rule(state)

        if rule.value == "jungle_nov":
            # Louisiana: everyone who filed appears on the November ballot.
            if cand.status.on_general_ballot or cand.status is NominationStatus.PENDING_PRIMARY:
                cand.status = NominationStatus.ALL_PARTY_NOVEMBER
        elif held is False and cand.status.on_general_ballot:
            cand.conflicts.append(
                f"source reported {cand.status.value} before {state}'s primary; "
                "downgraded to pending"
            )
            cand.status = NominationStatus.PENDING_PRIMARY
        elif held is None and cand.status.on_general_ballot:
            cand.conflicts.append(
                f"{state} primary date unknown; on-ballot status unverified"
            )
        out.append(cand)
    return out


def build_roster(
    candidates: Iterable[Candidate], as_of: date, extra_gaps: Optional[list[str]] = None
) -> Roster:
    """Assemble the final roster and state its own coverage honestly."""
    cands = apply_calendar(candidates, as_of)
    roster = Roster(candidates=list(cands))

    gaps: list[str] = list(extra_gaps or [])

    unresolved = unresolved_states(as_of)
    for state, u in unresolved.items():
        if u.reason is UnresolvedReason.CALENDAR_UNSYNCED:
            continue  # summarised in one line below rather than 40+ times
        gaps.append(f"{state} ({u.seats} seats): {u.detail}")

    unsynced = [u for u in unresolved.values() if u.reason is UnresolvedReason.CALENDAR_UNSYNCED]
    if unsynced:
        gaps.append(
            f"{len(unsynced)} state(s) covering {sum(u.seats for u in unsynced)} seats have no "
            "synced primary date, so their on-ballot status is unverified "
            "(add it to config/primary_calendar.yaml)"
        )

    # Districts with no Democrat at all: real in safe-red seats, but also what
    # a scraping failure looks like, so they are surfaced either way.
    on_ballot_districts = {c.district.code for c in roster.on_ballot()}
    from .statefacts import all_districts

    empty = [d.code for d in all_districts() if d.code not in on_ballot_districts]
    if empty:
        gaps.append(
            f"{len(empty)} district(s) with no Democrat recorded as on-ballot "
            f"(uncontested seats and/or collection gaps): {', '.join(empty[:12])}"
            + (" ..." if len(empty) > 12 else "")
        )

    # Multi-Democrat districts are legitimate under top-two/top-four/jungle
    # rules, and a data error anywhere else.
    counts: dict[str, int] = {}
    for c in roster.on_ballot():
        counts[c.district.code] = counts.get(c.district.code, 0) + 1
    for code, n in sorted(counts.items()):
        if n > 1 and yields_party_nominee(code.split("-")[0]):
            gaps.append(f"{code}: {n} Democrats marked on-ballot in a single-nominee state")

    roster.coverage_gaps = gaps
    return roster
