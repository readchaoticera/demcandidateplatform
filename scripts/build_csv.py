#!/usr/bin/env python3
"""Flat CSV of the roster: one row per candidate, six readable columns.

    python3 scripts/build_csv.py [--roster data/out/roster.json]
                                 [--out results/2026-08-26/medicare_for_all_candidates.csv]

``dcp report`` already writes candidates.csv with every field the pipeline
carries, including the seven-tier classification, the evidence quote and the
rule that fired. This is the short version for people who want the answer
rather than the audit trail.

Source URL points at whatever actually decided the row, which differs by
source: the cosponsor roll for a cosponsor, the reviewer's citation for a
hand-checked row, the specific page a quote came from for a campaign site.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dcp.models import BUCKET_LABELS, Bucket  # noqa: E402
from dcp.sources.congress import SOURCE_URL as COSPONSOR_URL  # noqa: E402

SOURCE_LABELS = {
    "cosponsorship": "Cosponsors H.R. 3069",
    "campaign_site": "Campaign site",
    "news": "News coverage",
    "human_review": "Reviewed by hand",
    "none": "No source",
}

COLUMNS = ["Candidate Name", "District", "Cook Rating", "Position", "Source", "Source URL"]


def source_url(cand: dict) -> str:
    """The URL behind this row's classification, or "" if there isn't one.

    Deliberately not just the campaign homepage: where a quote decided the
    row, the URL is the page carrying that quote, which is what a reader
    checking the claim actually needs.
    """
    basis = cand.get("evidence_basis", "")
    if basis == "cosponsorship":
        return COSPONSOR_URL
    if basis == "human_review":
        return cand.get("override_source") or ""
    if basis == "news":
        sources = cand.get("secondary_sources") or []
        return sources[0] if sources else ""
    if basis == "campaign_site":
        for evidence in cand.get("m4a_evidence") or []:
            if evidence.get("url"):
                return evidence["url"]
        return cand.get("campaign_url") or ""
    return cand.get("campaign_url") or ""


def rows(roster: dict) -> list[dict]:
    out = []
    for cand in roster["candidates"]:
        if not cand.get("on_general_ballot"):
            continue
        name = cand["full_name"]
        # The roster carries a couple of reviewed exceptions - someone who is
        # not a Democrat, and a non-voting delegate seat. Flag them in the
        # name rather than letting a spreadsheet imply they are neither.
        marks = []
        if cand.get("party", "Democratic") != "Democratic":
            marks.append(cand["party"])
        if cand.get("delegate"):
            marks.append("non-voting delegate")
        if marks:
            name = f"{name} ({'; '.join(marks)})"
        out.append({
            "Candidate Name": name,
            "District": cand["district"],
            "Cook Rating": cand.get("cook_rating") or "",
            "Position": BUCKET_LABELS[Bucket(cand["bucket"])],
            "Source": SOURCE_LABELS.get(cand.get("evidence_basis", ""), ""),
            "Source URL": source_url(cand),
        })
    out.sort(key=lambda r: (r["District"], r["Candidate Name"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--roster", type=Path, default=Path("data/out/roster.json"))
    ap.add_argument("--out", type=Path,
                    default=Path("results/2026-08-26/medicare_for_all_candidates.csv"))
    args = ap.parse_args()

    data = rows(json.loads(args.roster.read_text(encoding="utf-8")))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(data)

    with_url = sum(1 for r in data if r["Source URL"])
    print(f"wrote {args.out}: {len(data)} candidates, {with_url} with a source URL "
          f"({args.out.stat().st_size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
