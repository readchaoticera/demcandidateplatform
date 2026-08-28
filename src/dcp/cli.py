"""Command-line entry point.

Pipeline stages are separate commands rather than one monolith, because each
stage is slow, network-bound, and worth inspecting before spending the next
one. ``run`` chains them for convenience.

    dcp doctor                  # can this environment reach the sources at all?
    dcp calendar                # which primary dates are verified, which are missing
    dcp roster                  # who is on the ballot -> data/out/roster.json
    dcp websites                # attach campaign URLs
    dcp classify                # crawl sites, assign M4A tiers
    dcp cosponsors              # mark Medicare for All Act cosponsors
    dcp fec                     # cross-check the roster against FEC filings
    dcp ratings                 # attach Cook Political Report race ratings
    dcp louisiana               # apply Louisiana's certified qualifying list
    dcp secondary               # apply news-sourced assessments for unreadable sites
    dcp overrides               # apply human-reviewed corrections
    dcp adjust                  # apply reviewed roster membership changes
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
from .models import (BallotRule, Candidate, District, Evidence, M4ATier,
                     NominationStatus, Provenance, Roster)
from .net import EgressBlocked, Fetcher, REQUIRED_HOSTS, doctor
from .report import analyze, to_csv, to_json, to_markdown
from .resolve import build_roster, merge
from .sources import (ballotpedia, congress, fec, fec_bulk, louisiana, ratings,
                      wikipedia)
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
            f"FEC API skipped ({reason}); candidate IDs are synthetic until "
            "`dcp fec` cross-checks the roster against the bulk filing universe"
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


def cmd_cosponsors(args: argparse.Namespace) -> int:
    """Mark candidates who cosponsor the current Medicare for All Act.

    Kept in its own field rather than folded into the tier, because a
    legislative act and a campaign-website statement are different kinds of
    evidence. Where they disagree - a member who cosponsors but never mentions
    it to voters - that gap is a finding.
    """
    _require_egress()
    roster = _load_roster()
    cosponsors = congress.fetch_cosponsors(Fetcher(ttl=_ttl(args)))
    if not cosponsors:
        print("Could not retrieve the cosponsor roll; nothing changed.", file=sys.stderr)
        return 1

    congress.annotate(roster.candidates, cosponsors)
    _write(OUT / "roster.json", json.dumps(roster.to_dict(), indent=2))
    # Count over on-ballot candidates only: annotate() also marks candidates
    # whose primary has not happened, and mixing them into this ratio would
    # not match its denominator.
    on_ballot = [c for c in roster.on_ballot() if c.cosponsored_m4a_bill]
    log.info("%d of %d on-ballot candidates cosponsor %s",
             len(on_ballot), len(roster.on_ballot()), congress.BILL.upper())
    return 0


#: Sentinel used in the secondary file for a candidate found to have left the
#: race. Not a position - it corrects the roster instead.
_WITHDREW = "withdrew"


def cmd_secondary(args: argparse.Namespace) -> int:
    """Apply news-sourced assessments for candidates whose site was unreadable.

    Reads ``data/out/secondary.json``: {name: {tier, confidence, note, sources}}.
    These are recorded in their own fields and never overwrite the campaign-site
    tier, so the site-only measure stays comparable. Research that turns up a
    candidate who has left the race corrects their status instead.
    """
    path = OUT / "secondary.json"
    if not path.exists():
        print(f"{path} not found; nothing to apply.", file=sys.stderr)
        return 1
    blob = json.loads(path.read_text(encoding="utf-8"))
    roster = _load_roster()

    by_name = {c.full_name: c for c in roster.candidates}
    applied = withdrawn = missed = 0
    for name, rec in blob.items():
        cand = by_name.get(name)
        if cand is None:
            log.warning("secondary: no roster match for %r", name)
            missed += 1
            continue
        if rec.get("tier") == _WITHDREW:
            cand.status = NominationStatus.WITHDREW
            cand.conflicts.append(f"withdrew from the race: {rec.get('note','')}")
            cand.add_provenance("news", (rec.get("sources") or [""])[0], rec.get("note", ""))
            withdrawn += 1
            continue
        cand.secondary_tier = _parse_tier(rec.get("tier", "unknown"))
        cand.secondary_confidence = float(rec.get("confidence", 0.0))
        cand.secondary_note = rec.get("note", "")
        cand.secondary_sources = list(rec.get("sources") or [])
        for url in cand.secondary_sources[:1]:
            cand.add_provenance("news", url, rec.get("note", "")[:120])
        applied += 1

    _write(OUT / "roster.json", json.dumps(roster.to_dict(), indent=2))
    log.info("secondary: %d assessments applied, %d withdrawals, %d unmatched",
             applied, withdrawn, missed)
    return 0


OVERRIDES_PATH = Path("config/overrides.yaml")


def cmd_overrides(args: argparse.Namespace) -> int:
    """Apply human-reviewed corrections from config/overrides.yaml.

    These outrank every automated source. They exist for material automation
    cannot reach - Javascript-rendered sites, positions stated in images - and
    for calls that are simply wrong on inspection. Applying them as a pipeline
    stage rather than by editing the data means a correction survives the next
    re-run instead of being silently overwritten by it.
    """
    import yaml

    if not OVERRIDES_PATH.exists():
        log.info("no %s; nothing to apply", OVERRIDES_PATH)
        return 0
    blob = yaml.safe_load(OVERRIDES_PATH.read_text(encoding="utf-8")) or {}
    entries = blob.get("overrides") or []

    roster = _load_roster()
    by_key = {(c.full_name, c.district.code): c for c in roster.candidates}
    by_name: dict[str, list[Candidate]] = {}
    for c in roster.candidates:
        by_name.setdefault(c.full_name, []).append(c)

    applied = 0
    for entry in entries:
        name, district = entry.get("name", ""), entry.get("district", "")
        cand = by_key.get((name, district))
        if cand is None:
            matches = by_name.get(name, [])
            if len(matches) == 1:
                cand = matches[0]
                log.warning("override for %r: district %s did not match, matched on name "
                            "alone (%s)", name, district, cand.district.code)
        if cand is None:
            log.error("override for %r (%s) matched no candidate; skipped", name, district)
            continue

        tier = _parse_tier(entry.get("tier", "unknown"))
        if not tier.is_finding:
            log.error("override for %r has unusable tier %r; skipped", name, entry.get("tier"))
            continue
        cand.override_tier = tier
        cand.override_note = " ".join((entry.get("note") or "").split())
        cand.override_source = entry.get("source", "")
        cand.override_reviewer = entry.get("reviewed_by", "")
        cand.add_provenance(
            "human_review", cand.override_source,
            f"reviewed by {cand.override_reviewer} on {entry.get('reviewed_on','?')}",
        )
        applied += 1

    _write(OUT / "roster.json", json.dumps(roster.to_dict(), indent=2))
    log.info("overrides: %d of %d applied", applied, len(entries))
    return 0


ADJUSTMENTS_PATH = Path("config/roster_adjustments.yaml")


def cmd_adjust(args: argparse.Namespace) -> int:
    """Apply reviewed roster membership changes from config/roster_adjustments.yaml.

    ``overrides`` corrects what a candidate's position is; this corrects who is
    on the roster at all. Both exist so that a judgement made by a person
    looking at the race survives the next `dcp roster` run rather than being
    silently reverted by it.

    An excluded candidate is marked, not deleted, and keeps the reason. An
    included one carries their real party, which is how a non-Democrat can be
    on this roster without being counted as a Democrat.
    """
    import yaml

    if not ADJUSTMENTS_PATH.exists():
        log.info("no %s; nothing to apply", ADJUSTMENTS_PATH)
        return 0
    blob = yaml.safe_load(ADJUSTMENTS_PATH.read_text(encoding="utf-8")) or {}

    roster = _load_roster()
    by_key = {(c.full_name, c.district.code): c for c in roster.candidates}
    excluded = included = 0

    for entry in blob.get("exclude") or []:
        cand = by_key.get((entry.get("name", ""), entry.get("district", "")))
        if cand is None:
            log.warning("adjust: no roster entry for %s (%s)",
                        entry.get("name"), entry.get("district"))
            continue
        reason = " ".join((entry.get("reason") or "").split())
        cand.status = NominationStatus.EXCLUDED
        cand.conflicts = [c for c in cand.conflicts if not c.startswith("excluded by review")]
        cand.conflicts.append(f"excluded by review: {reason}")
        cand.add_provenance("human_review", entry.get("reviewed_by", ""), reason)
        excluded += 1

    for entry in blob.get("include") or []:
        name, code = entry.get("name", ""), entry.get("district", "")
        cand = by_key.get((name, code))
        if cand is None:
            state, seat = code.split("-")
            at_large = seat == "AL"
            cand = Candidate(
                full_name=name,
                district=District(
                    state,
                    1 if at_large else int(seat),
                    ballot_rule=statefacts.ballot_rule(state),
                    at_large=at_large,
                ),
                status=NominationStatus.ON_BALLOT,
            )
            roster.candidates.append(cand)
        cand.party = entry.get("party", "Democratic")
        cand.status = NominationStatus.ON_BALLOT
        if entry.get("campaign_url") and not cand.campaign_url:
            cand.campaign_url = entry["campaign_url"]
        reason = " ".join((entry.get("reason") or "").split())
        cand.add_provenance("human_review", entry.get("reviewed_by", ""), reason)
        included += 1

    roster.candidates.sort(key=lambda c: (c.district.state, c.district.number, c.full_name))
    _write(OUT / "roster.json", json.dumps(roster.to_dict(), indent=2))
    log.info("adjust: %d excluded, %d included", excluded, included)
    non_dem = [c for c in roster.on_ballot() if c.party != "Democratic"]
    if non_dem:
        log.info("adjust: %d non-Democratic candidate(s) on the roster: %s",
                 len(non_dem),
                 ", ".join(f"{c.full_name} ({c.district.code}, {c.party})" for c in non_dem))
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


def cmd_ratings(args: argparse.Namespace) -> int:
    """Attach The Cook Political Report's rating for each candidate's district.

    Cook's own site cannot be read: its robots.txt names the ratings dataset as
    proprietary and the pages sit behind a Cloudflare challenge that 403s any
    automated client. Wikipedia republishes the ratings under CC BY-SA with
    citations back to Cook, which is where these come from.

    The rating describes the seat, not the candidate, and it is Cook's
    judgement rather than this project's. It is carried for context only and
    never feeds a classification.
    """
    _require_egress()
    roster = _load_roster()
    table = ratings.fetch_all(Fetcher(ttl=_ttl(args)), args.states)
    if not table:
        print("Could not retrieve any race ratings; nothing changed.", file=sys.stderr)
        return 1

    hit = 0
    for cand in roster.candidates:
        found = table.get(cand.district.code)
        if not found:
            continue
        cand.cook_rating, cand.cook_rating_as_of = found
        cand.add_provenance("cook_via_wikipedia", ratings.NATIONAL_URL,
                            f"{found[0]} as of {found[1]}")
        hit += 1

    _write(OUT / "roster.json", json.dumps(roster.to_dict(), indent=2))
    on_ballot = [c for c in roster.on_ballot() if c.cook_rating]
    log.info("ratings: %d districts rated, covering %d of %d on-ballot candidates",
             len(table), len(on_ballot), len(roster.on_ballot()))
    unrated = [c.district.code for c in roster.on_ballot() if not c.cook_rating]
    if unrated:
        log.warning("ratings: no rating found for %s", ", ".join(sorted(set(unrated))))
    return 0


def cmd_louisiana(args: argparse.Namespace) -> int:
    """Replace Louisiana's roster with the Secretary of State's certified list.

    Louisiana has no nominating primary in 2026: the all-party ballot is the
    November election, so whoever qualified is on it and nobody else is. That
    makes the qualifying list the roster, and it is the only state where a
    primary-results source cannot answer the question.

    Both sources the pipeline had were wrong in opposite directions. Wikipedia
    lists everyone who *declared*, which over-counts and mislabels party.
    The campaign-finance table under-counts, catching only candidates who
    raised enough to appear in it - and it kept Cleo Fields, who has money and
    withdrew.
    """
    roster = _load_roster()
    qualified = louisiana.fetch()
    if not qualified:
        print("Could not retrieve the Louisiana certified list; nothing changed.",
              file=sys.stderr)
        return 1

    certified = louisiana.democrats_by_district(qualified)
    existing = [c for c in roster.candidates if c.district.state == "LA"]
    kept, dropped, added = [], [], []

    for cand in existing:
        names = certified.get(cand.district.code, [])
        if any(_name_matches(n, cand.full_name) for n in names):
            kept.append(cand)
        else:
            # Two different facts, and saying the wrong one is its own error: a
            # candidate who qualified under another party is on the November
            # ballot, just not as a Democrat.
            other = next(
                (q for q in qualified
                 if q.district == cand.district.number
                 and _name_matches(louisiana.display_name(q.name), cand.full_name)),
                None,
            )
            if other is not None:
                reason = (f"qualified with the Louisiana Secretary of State as "
                          f"{other.party}, not as a Democrat")
            else:
                reason = ("did not appear on the Louisiana Secretary of State's "
                          "certified list of candidates who qualified for the "
                          "November 3, 2026 ballot")
            # Not deleted: an on-ballot candidate who turns out not to belong
            # there is a correction worth keeping the reason for.
            cand.status = NominationStatus.LOST_PRIMARY
            # Re-running the stage must not stack another copy of the reason.
            cand.conflicts = [c for c in cand.conflicts
                              if "Louisiana Secretary of State" not in c]
            cand.conflicts.append(reason)
            dropped.append((cand, reason))

    for code, names in certified.items():
        for name in names:
            if any(_name_matches(name, c.full_name) for c in existing
                   if c.district.code == code):
                continue
            number = int(code.split("-")[1])
            cand = Candidate(
                full_name=name,
                district=District("LA", number, ballot_rule=statefacts.ballot_rule("LA")),
                status=NominationStatus.ALL_PARTY_NOVEMBER,
            )
            cand.add_provenance("louisiana_sos", louisiana.INQUIRY_URL,
                                "qualified for the November 3, 2026 all-party ballot")
            roster.candidates.append(cand)
            added.append(cand)

    # Wikipedia links campaign sites for the candidates it covers, which for
    # Louisiana is a minority of the field. The qualifying record's contact
    # domain is the only lead for the rest, so verify it the same way any
    # other hint is verified rather than trusting it.
    fetcher = Fetcher(ttl=_ttl(args))
    hinted = 0
    for cand in [c for c in roster.on_ballot()
                 if c.district.state == "LA" and not c.campaign_url]:
        hint = next(
            (q.campaign_domain for q in qualified
             if q.district == cand.district.number
             and _name_matches(louisiana.display_name(q.name), cand.full_name)
             and q.campaign_domain),
            "",
        )
        if hint and _resolve_one(fetcher, cand, [f"https://{hint}/"]):
            hinted += 1

    roster.candidates.sort(key=lambda c: (c.district.state, c.district.number, c.full_name))
    _write(OUT / "roster.json", json.dumps(roster.to_dict(), indent=2))

    log.info("louisiana: %d qualified candidates of all parties, %d Democrats",
             len(qualified), sum(len(v) for v in certified.values()))
    log.info("louisiana: %d kept, %d added, %d marked not-qualified; "
             "%d campaign site(s) found from the qualifying record",
             len(kept), len(added), len(dropped), hinted)
    for cand, reason in dropped:
        log.warning("louisiana: %s (%s) removed - %s",
                    cand.full_name, cand.district.code, reason)
    for cand in added:
        log.info("louisiana: added %s (%s)", cand.full_name, cand.district.code)
    return 0


def cmd_fec(args: argparse.Namespace) -> int:
    """Cross-check the roster against the FEC candidate master file.

    Two independent things come out of one file. The roster gains what only the
    FEC has - canonical candidate IDs and incumbency - and, more usefully, it
    gets audited: every on-ballot name is looked for among the filers for that
    seat, and every seat with active Democratic filers is looked for in the
    roster. A name with no filing is probably misspelled; a seat with filers
    and no nominee is either a missed nominee or a primary that has not
    happened yet, and the coverage gaps say which.

    No API key is needed. The bulk file is the same data the OpenFEC API
    serves, without the ten-requests-an-hour ceiling that makes DEMO_KEY
    useless for a 435-seat sweep.
    """
    roster = _load_roster()
    raw = fec_bulk.load(args.year, max_age_days=args.cache_days)
    filers = fec_bulk.parse(raw, args.year)
    if not filers:
        print(f"No Democratic House filers found for {args.year}.", file=sys.stderr)
        return 1

    # The filing universe loaded here is Democratic, so a non-Democrat on the
    # roster would be reported as a missing filing when the truth is that this
    # check does not cover them.
    on_ballot = [c for c in roster.on_ballot() if c.party == "Democratic"]
    skipped = len(roster.on_ballot()) - len(on_ballot)
    stats = fec_bulk.annotate(on_ballot, filers)
    missing = fec_bulk.districts_with_filers_but_no_candidate(roster.on_ballot(), filers)
    # Coverage gaps are recorded as prose keyed by state ("MA (9 seats): ..."),
    # so a seat is explained when its state is already known to be unsettled.
    explained_states = {g[:2] for g in roster.coverage_gaps if g[2:3] == " "}

    # The roster stage records that no API key was available and the IDs are
    # therefore synthetic. That note is now out of date - this stage has just
    # done the cross-check the note says is missing - so replace it rather than
    # leaving the roster claiming it was never checked.
    roster.coverage_gaps = [
        g for g in roster.coverage_gaps
        if not g.startswith(("FEC API skipped", "FEC source skipped"))
        and "no matching FEC filing" not in g
    ]
    _write(OUT / "roster.json", json.dumps(roster.to_dict(), indent=2))

    total = len(on_ballot)
    rate = 100.0 * stats["matched"] / total if total else 0.0
    log.info("FEC: %d Democratic House filers for %d (%d active)",
             len(filers), args.year, sum(1 for f in filers if f.active))
    log.info("FEC: matched %d/%d on-ballot Democrats (%.1f%%), %d incumbents",
             stats["matched"], total, rate, stats["incumbents"])
    if skipped:
        log.info("FEC: %d non-Democratic candidate(s) not covered by this check", skipped)
    if stats["statewide"]:
        log.info("FEC: %d matched statewide rather than in-district (%d of them "
                 "across a redistricting boundary)",
                 stats["statewide"], stats["redistricted"])
    for cand in on_ballot:
        if not cand.fec_candidate_id:
            # Not necessarily an error: a candidate only has to register with
            # the FEC once they raise or spend $5,000, so a low-budget entrant
            # in an all-party race legitimately has no filing to match.
            log.warning("FEC: no filing for %s (%s) - a misspelling, or under "
                        "the $5,000 registration threshold",
                        cand.full_name, cand.district.code)
    if stats["unmatched"]:
        roster.coverage_gaps.append(
            f"{stats['unmatched']} of {total} on-ballot candidates have no matching "
            "FEC filing; their names may be transcribed incorrectly"
        )
        _write(OUT / "roster.json", json.dumps(roster.to_dict(), indent=2))

    # A district whose all-party primary has been held can legitimately send no
    # Democrat: in CA-40 two Republicans took both slots. Its losing Democrats
    # are still active FEC filers, so that seat is not a roster gap.
    settled_all_party = {
        d for d in missing
        if statefacts.ballot_rule(d[:2]) in (BallotRule.TOP_TWO, BallotRule.TOP_FOUR_RCV)
    }
    unexplained = [
        d for d in missing
        if d[:2] not in explained_states and d not in settled_all_party
    ]
    if settled_all_party:
        log.info("FEC: %s had Democratic filers but advanced none out of an "
                 "all-party primary, which is a result rather than a gap",
                 ", ".join(sorted(settled_all_party)))
    if unexplained:
        log.warning("FEC: %d seats have active Democratic filers but no nominee "
                    "in the roster, and are not known coverage gaps: %s",
                    len(unexplained), ", ".join(unexplained))
    elif missing:
        log.info("FEC: %d seats with filers but no nominee, all of them known "
                 "coverage gaps", len(missing))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    for step in (cmd_roster, cmd_louisiana, cmd_websites, cmd_classify, cmd_cosponsors,
                 cmd_fec, cmd_ratings, cmd_secondary, cmd_overrides,
                 cmd_adjust, cmd_report):
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


def _parse_ts(raw: Optional[str]) -> datetime:
    """Parse a stored provenance timestamp, tolerating a missing or bad one.

    A malformed timestamp should cost the record its date, not the whole run.
    """
    try:
        return datetime.fromisoformat(raw) if raw else datetime.utcnow()
    except ValueError:
        return datetime.utcnow()


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
            party=row.get("party", "Democratic"),
            district=district,
            status=NominationStatus(row["status"]),
            fec_candidate_id=row.get("fec_candidate_id"),
            incumbent=row.get("incumbent", False),
            cook_rating=row.get("cook_rating"),
            cook_rating_as_of=row.get("cook_rating_as_of", ""),
            campaign_url=row.get("campaign_url"),
            campaign_url_confidence=row.get("campaign_url_confidence", 0.0),
            issues_urls=row.get("issues_urls", []),
            m4a_tier=_parse_tier(row.get("m4a_tier", "unknown")),
            m4a_notes=row.get("m4a_notes", ""),
            cosponsored_m4a_bill=row.get("cosponsored_m4a_bill"),
            secondary_tier=_parse_tier(row.get("secondary_tier", "unknown")),
            secondary_confidence=row.get("secondary_confidence", 0.0),
            secondary_note=row.get("secondary_note", ""),
            secondary_sources=row.get("secondary_sources", []),
            override_tier=_parse_tier(row.get("override_tier", "unknown")),
            override_note=row.get("override_note", ""),
            override_source=row.get("override_source", ""),
            override_reviewer=row.get("override_reviewer", ""),
            # Evidence must survive the round trip. Without this, any stage
            # that loads, mutates and saves the roster silently strips every
            # verbatim quote - which is the whole basis for auditing a row.
            m4a_evidence=[
                Evidence(
                    quote=e.get("quote", ""),
                    url=e.get("url", ""),
                    matched_rule=e.get("matched_rule", ""),
                    tier=_parse_tier(e.get("tier", "unknown")),
                    negated=e.get("negated", False),
                    context=e.get("context", ""),
                )
                for e in row.get("m4a_evidence", [])
            ],
            # Provenance is the audit trail for every field above it. Like the
            # evidence quotes, it is written on save but was not read back, so
            # each stage that loaded and re-saved the roster erased the record
            # of where the previous stage's facts came from.
            provenance=[
                Provenance(
                    source=p.get("source", ""),
                    url=p.get("url", ""),
                    retrieved_at=_parse_ts(p.get("retrieved_at")),
                    note=p.get("note", ""),
                )
                for p in row.get("provenance", [])
            ],
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
        ("cosponsors", cmd_cosponsors, "mark Medicare for All Act cosponsors"),
        ("fec", cmd_fec, "cross-check the roster against FEC filings"),
        ("ratings", cmd_ratings, "attach Cook Political Report race ratings"),
        ("louisiana", cmd_louisiana, "apply Louisiana's certified qualifying list"),
        ("secondary", cmd_secondary, "apply news-sourced assessments"),
        ("overrides", cmd_overrides, "apply human-reviewed corrections"),
        ("adjust", cmd_adjust, "apply reviewed roster membership changes"),
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
