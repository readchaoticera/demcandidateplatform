"""Command-line entry point.

Pipeline stages are separate commands rather than one monolith, because each
stage is slow, network-bound, and worth inspecting before spending the next
one. ``run`` chains them for convenience.

    dcp doctor                  # can this environment reach the sources at all?
    dcp calendar                # which primary dates are verified, which are missing
    dcp roster                  # who is on the ballot -> data/out/roster.json
    dcp websites                # attach campaign URLs
    dcp classify                # crawl sites, assign M4A tiers
    dcp report                  # markdown + CSV + JSON output
    dcp run                     # all of the above
"""

from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from . import statefacts
from .adjudicate import to_review_csv
from .classify.classifier import classify_pages
from .crawl import collect_position_pages
from .models import Candidate, District, M4ATier, NominationStatus, Roster
from .net import EgressBlocked, Fetcher, REQUIRED_HOSTS, doctor
from .report import analyze, to_csv, to_json, to_markdown
from .resolve import build_roster, merge
from .sources import ballotpedia, fec, wikipedia
from .websites import resolve_campaign_url

log = logging.getLogger("dcp")

OUT = Path("data/out")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )


def _as_of(arg: Optional[str]) -> date:
    return datetime.strptime(arg, "%Y-%m-%d").date() if arg else date.today()


def _require_egress() -> None:
    """Abort early if the sources are unreachable.

    Without this the pipeline would run to completion and produce a roster of
    zero candidates with 435 coverage gaps, which reads like a finding about
    the election rather than a finding about the network.
    """
    results = doctor()
    blocked = [h for h, s in results.items() if s != "ok" and "ok (" not in s]
    if len(blocked) == len(results):
        print("Cannot reach any required data source:\n", file=sys.stderr)
        for host, status in results.items():
            print(f"  {host:24} {status}", file=sys.stderr)
        print(
            "\nEvery source is blocked, so no run can produce real data.\n"
            "Allowlist these hosts and retry:\n  " + "\n  ".join(REQUIRED_HOSTS),
            file=sys.stderr,
        )
        raise SystemExit(2)
    if blocked:
        log.warning("some sources unreachable: %s", ", ".join(blocked))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    print("Source reachability:\n")
    results = doctor()
    for host, status in results.items():
        mark = "OK  " if status.startswith("ok") else "FAIL"
        print(f"  [{mark}] {host:24} {status}")

    as_of = _as_of(args.as_of)
    print(f"\nField status as of {as_of:%B %d, %Y}:")
    print(f"  seats whose Democratic field is not yet settled: "
          f"{statefacts.unsettled_field_seats(as_of)}")
    for state, u in statefacts.unresolved_states(as_of).items():
        if u.reason is not statefacts.UnresolvedReason.CALENDAR_UNSYNCED:
            print(f"    {state} ({u.seats} seats): {u.detail}")

    unsynced = [
        u for u in statefacts.unresolved_states(as_of).values()
        if u.reason is statefacts.UnresolvedReason.CALENDAR_UNSYNCED
    ]
    if unsynced:
        print(f"  {len(unsynced)} state(s) have no synced primary date "
              f"({sum(u.seats for u in unsynced)} seats)")
    return 0


def cmd_calendar(args: argparse.Namespace) -> int:
    """Show the loaded primary calendar and what is still missing from it."""
    as_of = _as_of(args.as_of)
    known = statefacts.VERIFIED_PRIMARY_DATES
    print(f"Primary calendar: {statefacts.CALENDAR_PATH}")
    print(f"  {len(known)} of 50 states have a verified date.\n")

    if known:
        print("Known dates:")
        for state, when in sorted(known.items(), key=lambda kv: (kv[1], kv[0])):
            marker = "held" if when <= as_of else "upcoming"
            print(f"  {state}  {when:%Y-%m-%d}  ({marker}, {statefacts.SEAT_COUNTS[state]} seats)")

    missing = [
        s for s in sorted(statefacts.SEAT_COUNTS)
        if s not in known and statefacts.ballot_rule(s) is not statefacts.BallotRule.JUNGLE_NOV
    ]
    if missing:
        seats = sum(statefacts.SEAT_COUNTS[s] for s in missing)
        print(f"\nMissing ({len(missing)} states, {seats} seats):")
        print("  " + " ".join(missing))
        print(
            "\nAdd verified dates to config/primary_calendar.yaml. Until then these\n"
            "states' on-ballot status cannot be confirmed and rows are flagged."
        )
    return 0


def cmd_roster(args: argparse.Namespace) -> int:
    _require_egress()
    as_of = _as_of(args.as_of)
    fetcher = Fetcher(ttl=_ttl(args))

    # FEC supplies canonical candidate IDs and the filing universe, but its
    # rate limits make DEMO_KEY unusable for a 435-district run. Without a real
    # key we skip it rather than half-populate IDs, and record that we did.
    universe: list[Candidate] = []
    fec_gap: list[str] = []
    if args.no_fec or not os.environ.get("FEC_API_KEY"):
        reason = "--no-fec" if args.no_fec else "FEC_API_KEY not set"
        log.warning("skipping FEC source (%s): no canonical candidate IDs", reason)
        fec_gap.append(
            f"FEC source skipped ({reason}); candidate IDs are synthetic and the "
            "roster is not cross-checked against the filing universe"
        )
    else:
        log.info("fetching FEC filing universe")
        universe = fec.fetch_democratic_house_candidates(fetcher, args.year)

    results: list[Candidate] = []
    gaps: list[str] = []
    states = args.states or sorted(statefacts.SEAT_COUNTS)
    for state in states:
        held = statefacts.primary_held(state, as_of)
        status = (
            NominationStatus.ON_BALLOT if held else NominationStatus.PENDING_PRIMARY
        )
        log.info("wikipedia: %s", state)
        cands, state_gaps = wikipedia.candidates_for_state(fetcher, state, status)
        results.extend(cands)
        gaps.extend(state_gaps)

    roster = build_roster(merge(results, universe), as_of, extra_gaps=gaps + fec_gap)
    _write(OUT / "roster.json", json.dumps(roster.to_dict(), indent=2))
    log.info("roster: %d candidates, %d on ballot",
             len(roster.candidates), len(roster.on_ballot()))
    return 0


def _resolve_one(fetcher: Fetcher, cand: Candidate, hints: list[str]) -> bool:
    """Attach a verified campaign URL to one candidate. Never raises."""
    if not hints:
        cand.conflicts.append("no campaign URL listed on the state's Wikipedia article")
        return False
    try:
        score = resolve_campaign_url(fetcher, cand, hints)
    except Exception as exc:
        cand.conflicts.append(f"campaign site lookup failed: {type(exc).__name__}")
        return False

    if score.accepted:
        cand.campaign_url = score.url
        cand.campaign_url_confidence = score.score
        cand.add_provenance("campaign_site", score.url, "; ".join(score.reasons))
        return True

    # Keep the URL even when scoring is weak: it came from a curated,
    # party-tagged list, so a low score usually means an unusual homepage
    # rather than the wrong candidate. The score travels with it so the
    # caveat stays visible in the output.
    cand.campaign_url = score.url or hints[0]
    cand.campaign_url_confidence = score.score
    cand.conflicts.append(
        f"campaign site verification scored {score.score:.2f}: "
        f"{'; '.join(score.reasons) or 'no signal'}"
    )
    return bool(cand.campaign_url)


def cmd_websites(args: argparse.Namespace) -> int:
    """Attach campaign URLs, sourced from Wikipedia and verified by fetching.

    Ballotpedia was the intended source but serves an empty HTTP 202 to
    automated clients, so it yields nothing. Wikipedia's per-state "External
    links" section lists official campaign sites tagged by party, which turns
    out to be both reachable and better structured.
    """
    _require_egress()
    roster = _load_roster()
    fetcher = Fetcher(ttl=_ttl(args))
    targets = [c for c in roster.on_ballot() if not c.campaign_url]

    # Gather per-state hints serially: one article fetch per state, all cached.
    hints_by_state: dict[str, dict[str, str]] = {}
    for state in sorted({c.district.state for c in targets}):
        html = wikipedia.fetch_state_html(fetcher, state)
        hints_by_state[state] = wikipedia.democratic_campaign_urls(html) if html else {}

    def hints_for(cand: Candidate) -> list[str]:
        return [
            url for name, url in hints_by_state[cand.district.state].items()
            if _name_matches(name, cand.full_name)
        ]

    resolved = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(_resolve_one, fetcher, cand, hints_for(cand))
            for cand in targets
        ]
        for i, future in enumerate(as_completed(futures), 1):
            if future.result():
                resolved += 1
            if i % 50 == 0:
                log.info("campaign sites: %d/%d processed", i, len(targets))

    _write(OUT / "roster.json", json.dumps(roster.to_dict(), indent=2))
    log.info(
        "campaign sites: %d resolved of %d on-ballot candidates",
        sum(1 for c in roster.on_ballot() if c.campaign_url), len(roster.on_ballot()),
    )
    return 0


def _classify_one(fetcher: Fetcher, cand: Candidate, max_pages: int) -> None:
    """Crawl one candidate's site and set their tier. Never raises."""
    if not cand.campaign_url:
        cand.m4a_tier = M4ATier.UNKNOWN
        cand.m4a_notes = "no campaign website resolved"
        return
    try:
        pages = collect_position_pages(fetcher, cand.campaign_url, max_pages)
    except Exception as exc:  # one bad site must not end the run
        cand.m4a_tier = M4ATier.UNKNOWN
        cand.m4a_notes = f"crawl failed: {type(exc).__name__}"
        return
    if not pages:
        cand.m4a_tier = M4ATier.UNKNOWN
        cand.m4a_notes = "campaign site unreachable"
        return

    result = classify_pages(pages)
    cand.m4a_tier = result.tier
    cand.m4a_evidence = result.evidence
    cand.issues_urls = sorted(pages)
    notes = [result.notes] if result.notes else []
    if result.needs_review:
        notes.append(f"REVIEW: {result.review_reason}")
    if result.explicitly_rejects_m4a:
        notes.append("explicitly rejects Medicare for All")
    cand.m4a_notes = " | ".join(n for n in notes if n)


def cmd_classify(args: argparse.Namespace) -> int:
    """Crawl each campaign site and assign a tier.

    Runs candidates concurrently. Politeness is a per-host obligation and each
    campaign is its own host, so parallelism across candidates costs no site
    anything; Fetcher still serialises and rate-limits per host.
    """
    _require_egress()
    roster = _load_roster()
    fetcher = Fetcher(ttl=_ttl(args))
    targets = roster.on_ballot()

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_classify_one, fetcher, cand, args.max_pages): cand
            for cand in targets
        }
        for future in as_completed(futures):
            future.result()  # _classify_one swallows its own errors
            done += 1
            if done % 25 == 0:
                log.info("classified %d/%d", done, len(targets))

    _write(OUT / "roster.json", json.dumps(roster.to_dict(), indent=2))
    classified = sum(1 for c in targets if c.m4a_tier is not M4ATier.UNKNOWN)
    log.info("classified %d of %d on-ballot candidates", classified, len(targets))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    roster = _load_roster()
    as_of = _as_of(args.as_of)
    analysis = analyze(roster, as_of)

    _write(OUT / "report.md", to_markdown(analysis, roster))
    _write(OUT / "candidates.csv", to_csv(roster))
    _write(OUT / "analysis.json", to_json(analysis, roster))
    review = to_review_csv(roster.candidates)
    if review.count("\n") > 1:
        _write(OUT / "needs_review.csv", review)

    print(to_markdown(analysis, roster))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    for step in (cmd_roster, cmd_websites, cmd_classify, cmd_report):
        rc = step(args)
        if rc != 0:
            return rc
    return 0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _ttl(args: argparse.Namespace):
    from datetime import timedelta
    return timedelta(days=args.cache_days)


def _name_matches(a: str, b: str) -> bool:
    from .models import normalize_name
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # Surname plus first initial is enough given we are already inside one district.
    pa, pb = na.split(), nb.split()
    return pa[-1] == pb[-1] and pa[0][:1] == pb[0][:1]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("wrote %s (%d bytes)", path, len(content))


#: Tier values written by earlier versions, mapped to their current names.
#: Roster files outlive a rename, and crashing on one strands the artifact.
_LEGACY_TIERS = {"no_healthcare_position": M4ATier.NO_COVERAGE_POSITION}


def _parse_tier(value: str) -> M4ATier:
    """Deserialise a tier, tolerating legacy and unrecognised values."""
    try:
        return M4ATier(value)
    except ValueError:
        tier = _LEGACY_TIERS.get(value)
        if tier is not None:
            return tier
        log.warning("unrecognised tier %r in roster; treating as unknown", value)
        return M4ATier.UNKNOWN


def _load_roster() -> Roster:
    path = OUT / "roster.json"
    if not path.exists():
        print(f"{path} not found - run `dcp roster` first.", file=sys.stderr)
        raise SystemExit(1)
    blob = json.loads(path.read_text(encoding="utf-8"))

    roster = Roster(coverage_gaps=blob.get("coverage_gaps", []))
    for row in blob.get("candidates", []):
        state = row["state"]
        code = row["district"]
        at_large = code.endswith("-AL")
        number = 1 if at_large else int(code.split("-")[1])
        district = District(
            state, number, ballot_rule=statefacts.ballot_rule(state), at_large=at_large
        )
        cand = Candidate(
            full_name=row["full_name"],
            district=district,
            status=NominationStatus(row["status"]),
            fec_candidate_id=row.get("fec_candidate_id"),
            incumbent=row.get("incumbent", False),
            campaign_url=row.get("campaign_url"),
            campaign_url_confidence=row.get("campaign_url_confidence", 0.0),
            issues_urls=row.get("issues_urls", []),
            m4a_tier=_parse_tier(row.get("m4a_tier", "unknown")),
            m4a_notes=row.get("m4a_notes", ""),
            conflicts=row.get("conflicts", []),
        )
        roster.candidates.append(cand)
    return roster


def main(argv: Optional[list[str]] = None) -> int:
    # Shared options live on a parent parser so they are accepted either
    # before or after the subcommand; argparse otherwise only allows the
    # former, which is a constant source of confusion.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument("--as-of", help="YYYY-MM-DD; defaults to today")
    common.add_argument("--cache-days", type=int, default=7)
    common.add_argument("--year", type=int, default=2026)
    common.add_argument("--states", nargs="*", help="limit to these state codes")
    common.add_argument("--max-pages", type=int, default=8)
    common.add_argument("--workers", type=int, default=8,
                        help="concurrent candidates during crawl/classify")
    common.add_argument("--no-fec", action="store_true",
                        help="skip the FEC source (implied when FEC_API_KEY is unset)")

    parser = argparse.ArgumentParser(prog="dcp", description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn, help_text in (
        ("doctor", cmd_doctor, "check source reachability and field status"),
        ("calendar", cmd_calendar, "show the primary calendar and its gaps"),
        ("roster", cmd_roster, "build the candidate roster"),
        ("websites", cmd_websites, "resolve campaign websites"),
        ("classify", cmd_classify, "crawl sites and classify positions"),
        ("report", cmd_report, "write the analysis"),
        ("run", cmd_run, "run every stage"),
    ):
        p = sub.add_parser(name, help=help_text, parents=[common])
        p.set_defaults(func=fn)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except EgressBlocked as exc:
        print(f"\nNetwork egress blocked: {exc}", file=sys.stderr)
        print("Run `dcp doctor` for details.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
