"""FEC candidate master file — the filing universe, without an API key.

``sources/fec.py`` uses the OpenFEC REST API, which needs a key: DEMO_KEY is
capped at ten requests an hour, and the 2026 Democratic House field alone runs
to thirteen pages. The bulk candidate master file carries the same records,
needs no key at all, and arrives in one request.

What it is good for is the *roster*: which Democrats filed for which seat, who
is an incumbent, and the canonical FEC candidate ID. What it cannot tell you is
who won a primary — the file lists every filer, so a district with nine
Democratic filers still shows nine. Nomination status has to keep coming from
an election-results source.

Layout: https://www.fec.gov/campaign-finance-data/candidate-master-file-description/
"""

from __future__ import annotations

import io
import logging
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import requests

from ..models import Candidate, normalize_name
from ..statefacts import AT_LARGE_STATES, SEAT_COUNTS

log = logging.getLogger(__name__)

BULK_URL = "https://www.fec.gov/files/bulk-downloads/{year}/cn{yy}.zip"

#: Column order of cn.txt, pipe-delimited and headerless.
COLUMNS = (
    "candidate_id", "name", "party", "election_year", "office_state", "office",
    "district", "incumbent_challenge", "status", "committee_id",
)

#: Party codes that mean "Democrat" in FEC filings.
DEM_PARTIES = {"DEM", "DFL", "D"}


@dataclass(frozen=True)
class Filer:
    candidate_id: str
    name: str
    district_code: str
    incumbent: bool
    status: str
    """C = statutory candidate, F/N = future/not yet, P = prior candidate."""

    @property
    def active(self) -> bool:
        return self.status == "C"


def download(year: int = 2026, dest: Optional[Path] = None) -> str:
    """Fetch and unzip the candidate master file, returning its text."""
    url = BULK_URL.format(year=year, yy=str(year)[-2:])
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        raw = zf.read("cn.txt").decode("utf-8", errors="replace")
    if dest:
        dest.write_text(raw, encoding="utf-8")
    return raw


def load(year: int = 2026, cache: Optional[Path] = None, max_age_days: int = 7) -> str:
    """Return the candidate master file, downloading only when the cache is stale.

    The file is ~800 KB and changes as filings come in, so a weekly refresh is
    the right granularity; re-fetching it on every stage run is wasteful and
    re-parsing a stale copy silently is worse.
    """
    if cache is None:
        cache = Path("data/cache/fec") / f"cn{str(year)[-2:]}.txt"
    if cache.exists():
        age = time.time() - cache.stat().st_mtime
        if age < max_age_days * 86400:
            return cache.read_text(encoding="utf-8")
    cache.parent.mkdir(parents=True, exist_ok=True)
    return download(year, dest=cache)


def parse(raw: str, year: int = 2026) -> list[Filer]:
    """Democratic U.S. House filers for ``year``."""
    out: list[Filer] = []
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) < 10:
            continue
        row = dict(zip(COLUMNS, parts))
        if row["office"] != "H" or row["party"].upper() not in DEM_PARTIES:
            continue
        if row["election_year"] != str(year):
            continue
        code = _district_code(row["office_state"], row["district"])
        if code is None:
            continue
        out.append(
            Filer(
                candidate_id=row["candidate_id"],
                name=_display_name(row["name"]),
                district_code=code,
                incumbent=row["incumbent_challenge"] == "I",
                status=row["status"],
            )
        )
    return out


def _district_code(state: str, district: str) -> Optional[str]:
    st = (state or "").upper()
    if st not in SEAT_COUNTS:
        return None
    try:
        num = int(district or "0")
    except ValueError:
        return None
    if st in AT_LARGE_STATES:
        return f"{st}-AL"
    if not 1 <= num <= SEAT_COUNTS[st]:
        return None
    return f"{st}-{num:02d}"


def _display_name(fec_name: str) -> str:
    """FEC stores "LAST, FIRST MIDDLE"; convert to display order."""
    name = fec_name.strip()
    if "," not in name:
        return name.title() if name.isupper() else name
    last, _, rest = name.partition(",")
    display = f"{rest.strip()} {last.strip()}".strip()
    return display.title() if display.isupper() else display


def index_by_district(filers: Iterable[Filer]) -> dict[str, list[Filer]]:
    out: dict[str, list[Filer]] = {}
    for f in filers:
        out.setdefault(f.district_code, []).append(f)
    return out


def match(candidate: Candidate, filers: list[Filer]) -> Optional[Filer]:
    """Find a candidate's FEC record within one district.

    Three passes, loosening only as far as stays safe inside a single seat:

    1. Surname plus first initial — tolerates the middle names and suffixes the
       FEC records but rosters omit.
    2. Surname alone, but only when exactly one filer in the district has it.
       The FEC files legal names, so "Robert C Scott" never matches a roster's
       "Bobby Scott" on initial; within one district a lone surname is
       unambiguous.
    3. Compound surnames: the FEC and the roster disagree about where a
       multi-part surname starts ("Leger Fernandez"), so any shared surname
       token counts, again only when it picks out exactly one filer.
    """
    want = normalize_name(candidate.full_name).split()
    if not want:
        return None

    parsed = [(f, normalize_name(f.name).split()) for f in filers]
    parsed = [(f, toks) for f, toks in parsed if toks]

    for f, got in parsed:
        if got[-1] == want[-1] and got[0][:1] == want[0][:1]:
            return f

    same_surname = [f for f, got in parsed if got[-1] == want[-1]]
    if len(same_surname) == 1:
        return same_surname[0]

    shared = [f for f, got in parsed if set(got[1:]) & set(want[1:])]
    if len(shared) == 1:
        return shared[0]
    return None


def match_statewide(candidate: Candidate, by_district: dict[str, list[Filer]]) -> Optional[Filer]:
    """Fall back to the whole state, for members whose district was redrawn.

    Mid-decade redistricting moved several: the FEC files the seat they were
    elected in, the roster the one they now run in. Requires two shared name
    tokens, which no two different candidates in one state realistically share.
    """
    want = set(normalize_name(candidate.full_name).split())
    if len(want) < 2:
        return None
    hits = [
        f
        for code, filers in by_district.items()
        if code[:2] == candidate.district.state
        for f in filers
        if len(want & set(normalize_name(f.name).split())) >= 2
    ]
    return hits[0] if len(hits) == 1 else None


def annotate(candidates: Iterable[Candidate], filers: list[Filer]) -> dict[str, int]:
    """Attach FEC IDs and incumbency. Returns a summary of what matched."""
    by_district = index_by_district(filers)
    stats = {"matched": 0, "unmatched": 0, "incumbents": 0,
             "statewide": 0, "redistricted": 0}
    for cand in candidates:
        hit = match(cand, by_district.get(cand.district.code, []))
        note = ""
        if hit is None:
            hit = match_statewide(cand, by_district)
            if hit is not None:
                stats["statewide"] += 1
                if hit.district_code != cand.district.code:
                    # Mid-decade redistricting: the FEC keeps the seat the
                    # candidate registered for, the roster the one they run in.
                    stats["redistricted"] += 1
                    note = (f"; FEC files {hit.district_code}, candidate runs in "
                            f"{cand.district.code} after redistricting")
                else:
                    # Same seat, so the in-district passes failed on the name
                    # itself - usually the FEC's "LAST, FIRST" field filled in
                    # backwards. Worth recording rather than smoothing over.
                    note = f"; matched on name tokens only, FEC records \"{hit.name}\""
        if hit is None:
            stats["unmatched"] += 1
            cand.conflicts.append("no FEC filing record found for this name and district")
            continue
        stats["matched"] += 1
        cand.fec_candidate_id = hit.candidate_id
        if hit.incumbent:
            cand.incumbent = True
            stats["incumbents"] += 1
        cand.add_provenance(
            "fec_bulk", "https://www.fec.gov/files/bulk-downloads/2026/cn26.zip",
            f"{hit.candidate_id}, status {hit.status}"
            + (", incumbent" if hit.incumbent else "") + note,
        )
    return stats


def districts_with_filers_but_no_candidate(
    candidates: Iterable[Candidate], filers: list[Filer]
) -> list[str]:
    """Seats where Democrats have filed with the FEC but the roster has nobody.

    This is the cross-check that matters most: an unexplained entry here means
    the roster missed a nominee. Entries that line up with the known coverage
    gaps — states whose primary has not been held — mean the opposite, that the
    roster is right and simply waiting on a result.
    """
    have = {c.district.code for c in candidates}
    filed = {f.district_code for f in filers if f.active}
    return sorted(filed - have)
