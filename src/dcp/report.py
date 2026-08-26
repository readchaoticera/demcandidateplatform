"""Aggregate the roster into the headline analysis.

The statistic this project exists to produce - "how many Democratic House
candidates support Medicare for All" - is easy to state and easy to get
subtly wrong. Two decisions drive the number more than the classifier does:

**The denominator.** Candidates whose material could not be retrieved are
``UNKNOWN``. They are not evidence of absence. Every share is therefore
reported twice: over all on-ballot candidates, and over only those with a
classified position. If those two numbers are far apart, coverage is the story
and the headline figure should not be quoted on its own.

**The threshold.** "Supports Medicare for All" can mean the explicit
endorsement only, or explicit plus single-payer-in-substance. Both are
reported, labelled, and never silently blended.
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Optional

from .models import TIER_ORDER, Candidate, M4ATier, Roster
from .statefacts import SEAT_COUNTS, unsettled_field_seats


@dataclass
class Analysis:
    generated_at: datetime
    as_of: date
    total_on_ballot: int
    tier_counts: dict[str, int]
    classified: int
    unknown: int
    explicit_m4a: int
    single_payer_any: int
    coverage_gaps: list[str] = field(default_factory=list)
    unresolved_seats: int = 0
    by_state: dict[str, dict[str, int]] = field(default_factory=dict)
    by_incumbency: dict[str, dict[str, int]] = field(default_factory=dict)
    needs_review: int = 0

    @property
    def coverage(self) -> float:
        return self.classified / self.total_on_ballot if self.total_on_ballot else 0.0

    def share(self, numerator: int, of_classified: bool = True) -> float:
        denom = self.classified if of_classified else self.total_on_ballot
        return numerator / denom if denom else 0.0


def analyze(roster: Roster, as_of: date) -> Analysis:
    on_ballot = roster.on_ballot()
    tier_counts = Counter(c.m4a_tier for c in on_ballot)

    classified = sum(n for t, n in tier_counts.items() if t.is_finding)
    unknown = tier_counts.get(M4ATier.UNKNOWN, 0)
    explicit = tier_counts.get(M4ATier.EXPLICIT_M4A, 0)
    substance = tier_counts.get(M4ATier.SINGLE_PAYER_SUBSTANCE, 0)

    by_state: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for c in on_ballot:
        by_state[c.district.state][c.m4a_tier.value] += 1

    by_inc: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for c in on_ballot:
        by_inc["incumbent" if c.incumbent else "non-incumbent"][c.m4a_tier.value] += 1

    return Analysis(
        generated_at=datetime.utcnow(),
        as_of=as_of,
        total_on_ballot=len(on_ballot),
        tier_counts={t.value: tier_counts.get(t, 0) for t in TIER_ORDER},
        classified=classified,
        unknown=unknown,
        explicit_m4a=explicit,
        single_payer_any=explicit + substance,
        coverage_gaps=list(roster.coverage_gaps),
        unresolved_seats=unsettled_field_seats(as_of),
        by_state={k: dict(v) for k, v in sorted(by_state.items())},
        by_incumbency={k: dict(v) for k, v in by_inc.items()},
        needs_review=sum(1 for c in on_ballot if "review" in c.m4a_notes.lower()),
    )


def to_markdown(analysis: Analysis, roster: Optional[Roster] = None) -> str:
    a = analysis
    out = io.StringIO()
    w = out.write

    w("# Medicare for All support among Democratic U.S. House candidates\n\n")
    w(f"Generated {a.generated_at:%Y-%m-%d %H:%M} UTC, reflecting the field as of {a.as_of:%B %d, %Y}.\n\n")

    w("## Coverage first\n\n")
    w(f"- **{a.total_on_ballot}** Democratic candidates recorded as on the November ballot.\n")
    w(f"- **{a.classified}** ({a.coverage:.0%}) had a position we could actually read and classify.\n")
    w(f"- **{a.unknown}** could not be classified (site unreachable, no site found, or no readable text).\n")
    if a.unresolved_seats:
        w(f"- **{a.unresolved_seats} seats** are in states whose field is not yet settled: "
          "primaries still to come, or no party nomination at all. No candidate list "
          "compiled today can cover them.\n")
    if a.needs_review:
        w(f"- **{a.needs_review}** classifications are flagged for human review.\n")
    w("\n")
    if a.coverage < 0.8:
        w("> **Coverage is below 80%.** The shares below should be read as describing the\n"
          "> candidates we could read, not the full field.\n\n")

    w("## Headline\n\n")
    w(f"| Reading of \"supports Medicare for All\" | Count | Share of classified | Share of all on-ballot |\n")
    w("|---|---|---|---|\n")
    w(f"| Explicit endorsement only | {a.explicit_m4a} | "
      f"{a.share(a.explicit_m4a):.1%} | {a.share(a.explicit_m4a, False):.1%} |\n")
    w(f"| Explicit + single-payer in substance | {a.single_payer_any} | "
      f"{a.share(a.single_payer_any):.1%} | {a.share(a.single_payer_any, False):.1%} |\n\n")

    w("## Full distribution\n\n")
    w("| Position tier | Count | Share of classified |\n|---|---|---|\n")
    for tier in TIER_ORDER:
        n = a.tier_counts.get(tier.value, 0)
        share = "n/a" if tier is M4ATier.UNKNOWN else f"{a.share(n):.1%}"
        w(f"| `{tier.value}` | {n} | {share} |\n")
    w("\n")

    if a.by_incumbency:
        w("## By incumbency\n\n| Group | Explicit M4A | Classified | Share |\n|---|---|---|---|\n")
        for group, counts in sorted(a.by_incumbency.items()):
            cl = sum(v for k, v in counts.items() if k != M4ATier.UNKNOWN.value)
            ex = counts.get(M4ATier.EXPLICIT_M4A.value, 0)
            w(f"| {group} | {ex} | {cl} | {(ex/cl if cl else 0):.1%} |\n")
        w("\n")

    if a.coverage_gaps:
        w("## Known gaps\n\n")
        w("These are the limits of the dataset, stated so the numbers above are not\n"
          "mistaken for a complete census.\n\n")
        for gap in a.coverage_gaps:
            w(f"- {gap}\n")
        w("\n")

    w("## Method\n\n")
    w("Positions are classified from candidates' own campaign websites into ordered\n"
      "tiers (`explicit_m4a` > `single_payer_substance` > `public_option` >\n"
      "`aca_strengthen`). Matches are scored in sentence context, so negated\n"
      "statements (\"I don't support Medicare for All\") and positions attributed to\n"
      "third parties (\"my opponent supports...\") do not count as support. Every\n"
      "non-trivial classification carries a verbatim quote and the rule that fired;\n"
      "see the per-candidate CSV/JSON output.\n")
    return out.getvalue()


CSV_COLUMNS = (
    "candidate_id", "full_name", "state", "district", "ballot_rule", "status",
    "incumbent", "campaign_url", "campaign_url_confidence", "m4a_tier",
    "m4a_evidence_quote", "m4a_evidence_rule", "m4a_notes", "conflicts",
)


def to_csv(roster: Roster) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for c in sorted(roster.candidates, key=lambda x: (x.district.state, x.district.number, x.full_name)):
        ev = c.m4a_evidence[0] if c.m4a_evidence else None
        writer.writerow({
            "candidate_id": c.candidate_id,
            "full_name": c.full_name,
            "state": c.district.state,
            "district": c.district.code,
            "ballot_rule": c.district.ballot_rule.value,
            "status": c.status.value,
            "incumbent": c.incumbent,
            "campaign_url": c.campaign_url or "",
            "campaign_url_confidence": round(c.campaign_url_confidence, 3),
            "m4a_tier": c.m4a_tier.value,
            "m4a_evidence_quote": ev.quote if ev else "",
            "m4a_evidence_rule": ev.matched_rule if ev else "",
            "m4a_notes": c.m4a_notes,
            "conflicts": " | ".join(c.conflicts),
        })
    return buf.getvalue()


def to_json(analysis: Analysis, roster: Roster) -> str:
    return json.dumps(
        {
            "analysis": {
                "generated_at": analysis.generated_at.isoformat(),
                "as_of": analysis.as_of.isoformat(),
                "total_on_ballot": analysis.total_on_ballot,
                "classified": analysis.classified,
                "unknown": analysis.unknown,
                "coverage": round(analysis.coverage, 4),
                "explicit_m4a": analysis.explicit_m4a,
                "single_payer_any": analysis.single_payer_any,
                "tier_counts": analysis.tier_counts,
                "by_state": analysis.by_state,
                "by_incumbency": analysis.by_incumbency,
                "unresolved_seats": analysis.unresolved_seats,
                "coverage_gaps": analysis.coverage_gaps,
            },
            "roster": roster.to_dict(),
        },
        indent=2,
    )
