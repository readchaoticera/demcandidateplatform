"""OpenFEC adapter: the candidate *universe* and stable identifiers.

Important limitation, and the reason this is not the only source: the FEC
knows who **filed** to run, not who **won a primary**. A district with nine
Democratic filers still shows nine here in September. FEC data therefore
supplies the universe and the canonical ``candidate_id``, while nomination
status has to come from an election-results source (see ``wikipedia.py`` and
``ballotpedia.py``).

FEC records also carry a principal-committee URL rather than a campaign
website, so they seed website discovery but rarely settle it.

API key: set ``FEC_API_KEY``. ``DEMO_KEY`` works but is rate-limited hard
enough that a full 435-district run will not finish under it.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterator, Optional

from ..models import Candidate, District, NominationStatus
from ..net import Fetcher
from ..statefacts import ballot_rule, AT_LARGE_STATES

log = logging.getLogger(__name__)

API_ROOT = "https://api.open.fec.gov/v1"


def api_key() -> str:
    return os.environ.get("FEC_API_KEY", "DEMO_KEY")


def _paged(fetcher: Fetcher, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Iterate every result page of an OpenFEC endpoint."""
    page = 1
    while True:
        q = dict(params, api_key=api_key(), page=page, per_page=100)
        query = "&".join(f"{k}={v}" for k, v in q.items() if v is not None)
        url = f"{API_ROOT}{path}?{query}"
        resp = fetcher.get(url)
        if not resp.ok:
            log.warning("FEC %s returned %s", path, resp.status)
            return
        try:
            blob = json.loads(resp.text)
        except json.JSONDecodeError:
            log.warning("FEC %s returned non-JSON", path)
            return
        results = blob.get("results", [])
        if not results:
            return
        yield from results
        pagination = blob.get("pagination", {})
        if page >= pagination.get("pages", page):
            return
        page += 1


def _district_from_fec(state: str, district_str: Optional[str]) -> Optional[District]:
    """FEC encodes at-large districts as "00"; the roster uses 1/at_large."""
    if not state:
        return None
    st = state.upper()
    raw = (district_str or "").strip() or "00"
    try:
        num = int(raw)
    except ValueError:
        return None
    at_large = st in AT_LARGE_STATES
    if num == 0:
        if not at_large:
            return None  # "00" in a multi-district state is a data error
        num = 1
    return District(st, num, ballot_rule=ballot_rule(st), at_large=at_large)


def fetch_democratic_house_candidates(
    fetcher: Fetcher, election_year: int = 2026
) -> list[Candidate]:
    """Every Democrat who has filed for a U.S. House seat in ``election_year``.

    Returned with ``PENDING_PRIMARY`` status: the FEC cannot tell us who won,
    so nothing here is on the ballot until another source says so.
    """
    out: list[Candidate] = []
    seen: set[str] = set()

    params = {
        "office": "H",
        "party": "DEM",
        "election_year": election_year,
        "candidate_status": "C",  # statutory candidate
        "sort": "name",
    }
    for row in _paged(fetcher, "/candidates/search/", params):
        cid = row.get("candidate_id")
        if not cid or cid in seen:
            continue
        district = _district_from_fec(row.get("state", ""), row.get("district"))
        if district is None:
            log.debug("skipping %s: unparseable district %r", cid, row.get("district"))
            continue

        cand = Candidate(
            full_name=_tidy_name(row.get("name", "")),
            district=district,
            status=NominationStatus.PENDING_PRIMARY,
            fec_candidate_id=cid,
            incumbent=row.get("incumbent_challenge") == "I",
        )
        cand.add_provenance(
            "fec", f"{API_ROOT}/candidate/{cid}/", note="filed candidate record"
        )
        out.append(cand)
        seen.add(cid)

    log.info("FEC: %d Democratic House filers for %d", len(out), election_year)
    return out


def _tidy_name(fec_name: str) -> str:
    """FEC stores "LAST, FIRST MIDDLE"; convert to display order."""
    name = fec_name.strip()
    if "," not in name:
        return name.title() if name.isupper() else name
    last, _, rest = name.partition(",")
    display = f"{rest.strip()} {last.strip()}".strip()
    return display.title() if display.isupper() else display
