#!/usr/bin/env python3
"""Generate the GitHub Pages dashboard payload from a completed run.

Usage:
    python scripts/build_site.py [--roster data/out/roster.json]
                                 [--analysis data/out/analysis.json]
                                 [--out docs/data.json]

The dashboard is a static page, so everything it needs ships as one JSON file.
Only on-ballot candidates and only the fields the UI actually renders are
included: the full roster carries provenance chains and per-page URL lists that
would triple the payload for no visible benefit. `results/` keeps the complete
record for anyone who wants it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

#: Rendered directly by the dashboard; keep in sync with index.html.
TIER_LABELS = {
    "explicit_m4a": "Medicare for All",
    "single_payer_substance": "Single-payer (unbranded)",
    "public_option": "Public option",
    "aca_strengthen": "Strengthen the ACA",
    "no_coverage_position": "No coverage position",
    "opposed": "Opposed",
    "unknown": "Not found",
}

#: The three-way public grouping. Tiers stay in the payload for the row detail;
#: these are what the table and filters show.
BUCKET_LABELS = {
    "supports_m4a": "Supports Medicare for All or single-payer",
    "does_not_support_m4a": "Does not support Medicare for All",
}

#: Cook's scale, safe Democratic to safe Republican. Drives both the filter
#: order and the colour ramp in the dashboard.
RATING_ORDER = (
    "Solid D", "Likely D", "Lean D", "Tilt D",
    "Toss Up",
    "Tilt R", "Lean R", "Likely R", "Solid R",
)

BASIS_LABELS = {
    "cosponsorship": "Cosponsors the bill",
    "campaign_site": "Campaign site",
    "news": "News coverage",
    "human_review": "Reviewed by hand",
    "none": "No source",
}


def _exception_note(candidate: dict) -> str:
    """Why this row is an exception, or "" for the rows that are not.

    Two kinds so far and no reason to expect a third shape: someone who is not
    a Democrat, and someone in a seat that is not one of the 435. Both are
    reviewed decisions from config/roster_adjustments.yaml, and both get the
    same asterisk, so the page builds its footnote from this one field rather
    than knowing about either case.
    """
    notes = []
    party = candidate.get("party", "Democratic")
    if party != "Democratic":
        notes.append(f"runs as {party}, not as a Democrat")
    if candidate.get("delegate"):
        notes.append("is running for a non-voting delegate seat, "
                     "not one of the 435 voting districts")
    return "; ".join(notes)


def trim(candidate: dict) -> dict:
    """One dashboard row. Evidence is reduced to the single strongest quote."""
    evidence = candidate.get("m4a_evidence") or []
    top = evidence[0] if evidence else None
    return {
        "n": candidate["full_name"],
        "d": candidate["district"],
        "s": candidate["state"],
        "t": candidate["resolved_tier"],
        "bk": candidate["bucket"],
        "st": candidate["m4a_tier"],
        "b": candidate["evidence_basis"],
        "co": bool(candidate.get("cosponsored_m4a_bill")),
        "u": candidate.get("campaign_url") or "",
        "uc": candidate.get("campaign_url_confidence") or 0,
        "q": (top or {}).get("quote", "")[:400],
        "qu": (top or {}).get("url", ""),
        "qr": (top or {}).get("matched_rule", ""),
        "sn": candidate.get("secondary_note", ""),
        "ss": candidate.get("secondary_sources") or [],
        "sc": candidate.get("secondary_confidence") or 0,
        "nt": candidate.get("m4a_notes", ""),
        "cf": candidate.get("conflicts") or [],
        "rule": candidate.get("ballot_rule", "party_nominee"),
        "cr": candidate.get("cook_rating") or "",
        # Empty for the ordinary rows; set only for the deliberate exceptions,
        # which the table marks with an asterisk rather than counting silently.
        "p": "" if candidate.get("party", "Democratic") == "Democratic"
             else candidate["party"],
        "x": _exception_note(candidate),
    }


def build(roster_path: Path, analysis_path: Path) -> dict:
    roster = json.loads(roster_path.read_text(encoding="utf-8"))
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))["analysis"]

    rows = [trim(c) for c in roster["candidates"] if c["on_general_ballot"]]
    rows.sort(key=lambda r: (r["s"], r["d"], r["n"]))

    return {
        "generated_at": analysis["generated_at"],
        "as_of": analysis["as_of"],
        "summary": {
            "total": analysis["total_on_ballot"],
            "resolved": analysis["resolved_classified"],
            "explicit": analysis["resolved_explicit"],
            "site_explicit": analysis["explicit_m4a"],
            "cosponsors": analysis["cosponsors"],
            "cosponsor_silent": analysis["cosponsor_silent"],
            "by_evidence": analysis["by_evidence"],
            "buckets": analysis["bucket_counts"],
            "resolved_counts": analysis["resolved_counts"],
            "tier_counts": analysis["tier_counts"],
            "unresolved_seats": analysis["unresolved_seats"],
        },
        "tier_labels": TIER_LABELS,
        "bucket_labels": BUCKET_LABELS,
        "basis_labels": BASIS_LABELS,
        "rating_order": list(RATING_ORDER),
        "rating_as_of": next(
            (c["cook_rating_as_of"] for c in roster["candidates"]
             if c.get("cook_rating_as_of")), ""
        ),
        "coverage_gaps": analysis["coverage_gaps"],
        "candidates": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--roster", type=Path, default=Path("data/out/roster.json"))
    ap.add_argument("--analysis", type=Path, default=Path("data/out/analysis.json"))
    ap.add_argument("--out", type=Path, default=Path("docs/data.json"))
    args = ap.parse_args()

    payload = build(args.roster, args.analysis)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    size = args.out.stat().st_size
    print(f"wrote {args.out}: {len(payload['candidates'])} candidates, {size/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
