#!/usr/bin/env python3
"""Regenerate config/primary_calendar.yaml from the FEC's official date list.

Usage:
    python scripts/build_calendar.py [--pdf PATH] [--out PATH]

Source: "2026 Congressional Primary Dates and Candidate Filing Deadlines",
https://www.fec.gov/resources/cms-content/documents/2026pdates.pdf

The PDF's chronological table is parsed rather than the state-ordered one: it
has a stable `State [S] PRIMARY [ (qualifier) ] [RUNOFF]` shape, where the
state-ordered table interleaves filing deadlines in the same columns.

Two judgement calls are encoded here, both deliberately conservative:

*   **A state is "settled" only after its runoff date**, not its primary date,
    where a runoff is scheduled. A runoff happens only if necessary, so this
    can call a state unsettled for a few weeks longer than reality. That
    direction is safe; the opposite would assert nominees that do not exist.
*   **A state with several primary dates takes the latest.** Alabama runs
    CDs 3/4/5 on one date and CDs 1/2/6/7 on another; the state's field is not
    complete until the later one.

Louisiana is written out deliberately. Its House "primary" is the November 3
general election itself (FEC note 2: House elections postponed following
Louisiana v. Callais), which statefacts.py models as a ballot rule, not a date.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

STATE_CODES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}

#: Territories and the federal district: no U.S. House districts.
NON_STATES = {"DC", "D.C.", "Guam", "Virgin Islands", "American Samoa",
              "Puerto Rico", "Northern Mariana Islands"}

#: Entries are found by state name anywhere in the flattened table rather than
#: by line position. PDF text extraction collapses two entries onto one line
#: ("Missouri 8/4 Virginia S 8/4") and wraps long names across lines
#: ("New" / "Hampshire" / "S 9/8"), so line-anchored parsing silently drops rows.
#: Longest names first so "West Virginia" wins over "Virginia".
_NAMES = "|".join(
    re.escape(n) for n in sorted(STATE_CODES, key=len, reverse=True)
)
ENTRY = re.compile(
    rf"(?P<state>{_NAMES})\s+(?:S\s+)?"
    r"(?P<primary>\d{1,2}/\d{1,2}(?:/\d{2,4})?)"
    r"(?P<qual>\s*\([^)]*\))?"
    r"(?:\s+(?P<runoff>\d{1,2}/\d{1,2}(?:/\d{2,4})?))?"
)


def parse_date(token: str, default_year: int = 2026) -> date:
    parts = token.split("/")
    month, day = int(parts[0]), int(parts[1])
    year = default_year
    if len(parts) == 3:
        year = int(parts[2])
        year += 2000 if year < 100 else 0
    return date(year, month, day)


def extract_text(pdf_path: Path) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages)


def chronological_section(text: str) -> str:
    """The chronological table as one whitespace-normalised string.

    Flattening is deliberate: the table's rows do not survive PDF extraction as
    lines, so the parser matches entries by state name instead of by position.
    The trailing notes are cut off first - they name states in prose
    ("In Virginia, political parties may choose...") and would otherwise be
    scanned for entries.
    """
    marker = "IN CHRONOLOGICAL ORDER"
    if marker not in text:
        raise SystemExit("chronological table not found; PDF layout changed")
    body = text[text.index(marker):]

    notes = re.search(r"\n\s*1\.\s+In\b", body)
    if notes:
        body = body[:notes.start()]
    return re.sub(r"\s+", " ", body).strip()


def build(pdf_path: Path) -> tuple[dict[str, date], dict[str, date], list[str]]:
    primaries: dict[str, date] = {}
    runoffs: dict[str, date] = {}
    notes: list[str] = []

    for match in ENTRY.finditer(chronological_section(extract_text(pdf_path))):
        name = match.group("state").strip()
        line = match.group(0)
        if name in NON_STATES:
            continue
        code = STATE_CODES.get(name)
        if not code:
            continue

        qualifier = (match.group("qual") or "").lower()
        if "senate" in qualifier:
            continue  # Senate-only date; this project is House-only
        if code == "LA":
            notes.append(f"skipped Louisiana entry ({line}): House uses the "
                         "jungle ballot rule, not a primary date")
            continue

        primary = parse_date(match.group("primary"))
        if code not in primaries or primary > primaries[code]:
            primaries[code] = primary
            if match.group("runoff"):
                runoffs[code] = parse_date(match.group("runoff"))
            else:
                runoffs.pop(code, None)
        elif match.group("runoff"):
            runoff = parse_date(match.group("runoff"))
            if runoff > runoffs.get(code, date.min):
                runoffs[code] = runoff

    return primaries, runoffs, notes


HEADER = '''# 2026 congressional primary dates, by USPS state code (ISO YYYY-MM-DD).
#
# GENERATED by scripts/build_calendar.py from the FEC's official document:
#   "2026 Congressional Primary Dates and Candidate Filing Deadlines"
#   https://www.fec.gov/resources/cms-content/documents/2026pdates.pdf
#   (FEC data as of 5/18/2026)
#
# Do not hand-edit; re-run the script instead. A state absent from `primaries`
# is treated as UNKNOWN, and the pipeline then refuses to assert that anyone is
# on that state's ballot. Guessing a date is the one edit that silently
# corrupts the roster.
#
# `runoffs` holds the congressional runoff date where a state schedules one.
# A state counts as settled only once BOTH dates have passed: a runoff happens
# only if necessary, so this can read as unsettled slightly longer than
# reality. That error direction is safe; the reverse asserts nominees that do
# not exist.
#
# The FEC warns these dates are subject to change, and several moved this cycle
# by litigation. Re-run before any collection run you intend to publish.
#
# Louisiana is deliberately absent and must stay absent. It holds no
# nominating primary for the House in 2026 - its all-party primary IS the
# November 3 election, with a December 12 runoff, after House elections were
# postponed following Louisiana v. Callais (FEC note 2). That is encoded as a
# ballot rule in statefacts.py, not as a date.
'''


def render(primaries: dict[str, date], runoffs: dict[str, date]) -> str:
    out = [HEADER, "\nprimaries:"]
    for code in sorted(primaries):
        out.append(f"  {code}: {primaries[code].isoformat()}")
    out.append("\nrunoffs:")
    for code in sorted(runoffs):
        out.append(f"  {code}: {runoffs[code].isoformat()}")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("config/primary_calendar.yaml"))
    args = ap.parse_args()

    primaries, runoffs, notes = build(args.pdf)
    for note in notes:
        print(f"note: {note}", file=sys.stderr)

    missing = sorted(set(STATE_CODES.values()) - set(primaries) - {"LA"})
    if missing:
        print(f"warning: no primary date parsed for {', '.join(missing)}", file=sys.stderr)

    args.out.write_text(render(primaries, runoffs), encoding="utf-8")
    print(f"wrote {args.out}: {len(primaries)} primaries, {len(runoffs)} runoffs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
