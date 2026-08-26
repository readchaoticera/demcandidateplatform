"""Structural facts about how each state puts House candidates on the ballot.

Two kinds of data live here and they have very different shelf lives:

*   **Ballot rules and district counts** are stable statute/apportionment facts
    for the 2022-2030 cycle. They are hardcoded and asserted.

*   **Primary dates** are volatile (they move by legislation and by
    litigation), so only dates verified for this run are seeded. Everything
    else is ``None`` until ``dcp calendar sync`` populates it from the FEC's
    official "2026 Congressional Primary Dates and Candidate Filing Deadlines"
    document. Code must treat ``None`` as "unknown", never as "already held".

Mid-decade redistricting (Texas, Missouri, Ohio, California Prop 50, the
Louisiana litigation) redraws district *lines* but does not change a state's
district *count*, so SEAT_COUNTS remains valid through the cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Optional

from .models import BallotRule, District

#: Seats per state under the 2020 apportionment (2022-2030 cycle).
SEAT_COUNTS: dict[str, int] = {
    "AL": 7, "AK": 1, "AZ": 9, "AR": 4, "CA": 52, "CO": 8, "CT": 5, "DE": 1,
    "FL": 28, "GA": 14, "HI": 2, "ID": 2, "IL": 17, "IN": 9, "IA": 4, "KS": 4,
    "KY": 6, "LA": 6, "ME": 2, "MD": 8, "MA": 9, "MI": 13, "MN": 8, "MS": 4,
    "MO": 8, "MT": 2, "NE": 3, "NV": 4, "NH": 2, "NJ": 12, "NM": 3, "NY": 26,
    "NC": 14, "ND": 1, "OH": 15, "OK": 5, "OR": 6, "PA": 17, "RI": 2, "SC": 7,
    "SD": 1, "TN": 9, "TX": 38, "UT": 4, "VT": 1, "VA": 11, "WA": 10, "WV": 2,
    "WI": 8, "WY": 1,
}

assert sum(SEAT_COUNTS.values()) == 435, "apportionment must total 435 seats"
assert len(SEAT_COUNTS) == 50

#: States whose single seat is elected at large.
AT_LARGE_STATES: frozenset[str] = frozenset({"AK", "DE", "ND", "SD", "VT", "WY"})

#: Non-standard ballot rules. Anything absent uses BallotRule.PARTY_NOMINEE.
BALLOT_RULES: dict[str, BallotRule] = {
    "CA": BallotRule.TOP_TWO,
    "WA": BallotRule.TOP_TWO,
    "AK": BallotRule.TOP_FOUR_RCV,
    "LA": BallotRule.JUNGLE_NOV,
}

#: Path to the operator-maintained primary calendar.
CALENDAR_PATH = Path(__file__).resolve().parents[2] / "config" / "primary_calendar.yaml"


def load_primary_calendar(path: Path | None = None) -> dict[str, date]:
    """Load verified primary dates from config.

    A missing or malformed file yields an empty calendar rather than an
    exception: every state then reads as "date unknown", which makes the
    pipeline refuse to assert on-ballot status. That is the safe failure
    direction - the unsafe one would be defaulting to "primary already held".
    """
    target = path or CALENDAR_PATH
    try:
        import yaml

        blob = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, ImportError):
        return {}
    except Exception:  # malformed YAML
        return {}

    return _read_section(blob, "primaries")


def load_runoff_calendar(path: Path | None = None) -> dict[str, date]:
    """Load scheduled congressional runoff dates from the same config."""
    target = path or CALENDAR_PATH
    try:
        import yaml

        blob = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, ImportError):
        return {}
    except Exception:
        return {}
    return _read_section(blob, "runoffs")


def _read_section(blob: dict, key: str) -> dict[str, date]:
    out: dict[str, date] = {}
    for state, value in (blob.get(key) or {}).items():
        code = str(state).upper()
        if code not in SEAT_COUNTS:
            continue
        if isinstance(value, date):
            out[code] = value
        else:
            try:
                out[code] = date.fromisoformat(str(value))
            except ValueError:
                continue
    return out


#: Congressional primary dates, loaded from config/primary_calendar.yaml.
VERIFIED_PRIMARY_DATES: dict[str, date] = load_primary_calendar()

#: Scheduled congressional runoff dates, where a state holds them.
RUNOFF_DATES: dict[str, date] = load_runoff_calendar()

#: 2026 general election day.
GENERAL_ELECTION = date(2026, 11, 3)

#: Louisiana runoff, for districts where nobody clears 50% on Nov 3.
LA_RUNOFF = date(2026, 12, 12)


def ballot_rule(state: str) -> BallotRule:
    return BALLOT_RULES.get(state.upper(), BallotRule.PARTY_NOMINEE)


def primary_date(state: str) -> Optional[date]:
    """Return the known primary date, or None if it has not been synced.

    ``None`` means *unknown*, not *not yet held*. Callers must not infer
    that a state's primary has occurred from the absence of a date.
    """
    return VERIFIED_PRIMARY_DATES.get(state.upper())


def settled_date(state: str) -> Optional[date]:
    """When a state's nominees are certainly known: its primary, or its runoff.

    A runoff only happens if necessary, so treating the runoff date as the
    settled date can call a state unsettled for a few weeks longer than
    reality. That error direction is safe. The reverse - declaring nominees
    final before a scheduled runoff - would put losing candidates on the
    roster.
    """
    primary = primary_date(state)
    if primary is None:
        return None
    runoff = RUNOFF_DATES.get(state.upper())
    return max(primary, runoff) if runoff else primary


def primary_held(state: str, as_of: date) -> Optional[bool]:
    """Tri-state: True (held), False (upcoming), None (date unknown)."""
    if ballot_rule(state) is BallotRule.JUNGLE_NOV:
        # Louisiana's all-party primary IS the November election.
        return False
    d = settled_date(state)
    if d is None:
        return None
    return d <= as_of


def yields_party_nominee(state: str) -> bool:
    """Whether "the Democratic nominee for this seat" is a well-formed phrase.

    False for top-two, top-four and jungle states, where the ballot may carry
    zero, one, or several Democrats and no nomination occurs.
    """
    return ballot_rule(state) is BallotRule.PARTY_NOMINEE


def districts(state: str) -> list[District]:
    """Every district in a state, with its ballot rule attached."""
    st = state.upper()
    n = SEAT_COUNTS[st]
    rule = ballot_rule(st)
    at_large = st in AT_LARGE_STATES
    return [District(st, i, ballot_rule=rule, at_large=at_large) for i in range(1, n + 1)]


def all_districts() -> list[District]:
    out: list[District] = []
    for st in sorted(SEAT_COUNTS):
        out.extend(districts(st))
    return out


class UnresolvedReason(str, Enum):
    """Why a state's Democratic general-election field is not yet fixed."""

    PRIMARY_UPCOMING = "primary_upcoming"
    """Primary date is known and still in the future."""

    NO_NOMINATION = "no_nomination"
    """The state never produces a party nominee (Louisiana 2026)."""

    CALENDAR_UNSYNCED = "calendar_unsynced"
    """We do not know this state's primary date, so we cannot assert anything.
    A data gap on our side, not a fact about the state."""


@dataclass(frozen=True)
class Unresolved:
    state: str
    reason: UnresolvedReason
    detail: str

    @property
    def seats(self) -> int:
        return SEAT_COUNTS[self.state]


def unresolved_states(as_of: date) -> dict[str, Unresolved]:
    """States where the Democratic general-election field is not yet fixed.

    Callers should distinguish PRIMARY_UPCOMING / NO_NOMINATION (facts about
    the election) from CALENDAR_UNSYNCED (a gap in our own data), because
    conflating them makes a missing config file look like an unsettled race.
    """
    out: dict[str, Unresolved] = {}
    for st in sorted(SEAT_COUNTS):
        rule = ballot_rule(st)
        if rule is BallotRule.JUNGLE_NOV:
            out[st] = Unresolved(
                st,
                UnresolvedReason.NO_NOMINATION,
                f"all-party primary held on election day {GENERAL_ELECTION:%b %d, %Y}; "
                f"no party nominees exist (runoff {LA_RUNOFF:%b %d})",
            )
            continue
        held = primary_held(st, as_of)
        if held is None:
            out[st] = Unresolved(
                st,
                UnresolvedReason.CALENDAR_UNSYNCED,
                "primary date not in config/primary_calendar.yaml",
            )
        elif not held:
            out[st] = Unresolved(
                st,
                UnresolvedReason.PRIMARY_UPCOMING,
                f"nominees not settled until {settled_date(st):%b %d, %Y}",
            )
    return out


def unresolved_seats_by_reason(as_of: date) -> dict[UnresolvedReason, int]:
    """Seat totals grouped by why the field is unsettled."""
    out: dict[UnresolvedReason, int] = {r: 0 for r in UnresolvedReason}
    for u in unresolved_states(as_of).values():
        out[u.reason] += u.seats
    return out


def unsettled_field_seats(as_of: date) -> int:
    """Seats genuinely not yet decided: upcoming primaries plus no-nomination states.

    Excludes CALENDAR_UNSYNCED, which is our gap rather than the election's.
    """
    by_reason = unresolved_seats_by_reason(as_of)
    return (
        by_reason[UnresolvedReason.PRIMARY_UPCOMING]
        + by_reason[UnresolvedReason.NO_NOMINATION]
    )


def unresolved_seat_count(as_of: date) -> int:
    return sum(u.seats for u in unresolved_states(as_of).values())
