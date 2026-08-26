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

BASIS_LABELS = {
    "cosponsorship": "Cosponsors the bill",
    "campaign_site": "Campaign site",
    "news": "News coverage",
    "none": "No source",
}


def trim(candidate: dict) -> dict:
    """One dashboard row. Evidence is reduced to the single strongest quote."""
    evidence = candidate.get("m4a_evidence") or []
    top = evidence[0] if evidence else None
    return {
        "n": candidate["full_name"],
        "d": candidate["district"],
        "s": candidate["state"],
        "t": candidate["resolved_tier"],
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
            "resolved_counts": analysis["resolved_counts"],
            "tier_counts": analysis["tier_counts"],
            "unresolved_seats": analysis["unresolved_seats"],
        },
        "tier_labels": TIER_LABELS,
        "basis_labels": BASIS_LABELS,
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
