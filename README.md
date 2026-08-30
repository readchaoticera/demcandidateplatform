# Democratic House Candidate Platform Analysis

A pipeline that builds a roster of Democratic candidates on the November 2026
U.S. House general-election ballot, finds their campaign websites, and
classifies each one's healthcare position on a tiered Medicare for All scale.

**Status: the pipeline is built and tested; no data has been collected yet.**
See [Why there is no data](#why-there-is-no-data-yet).

---

## The two things that make this harder than it looks

### 1. "Every Democrat who won their primary" is not a well-defined set

Two separate problems, both encoded in `src/dcp/statefacts.py`:

**Not all primaries have happened.** As of late August 2026, four states have
yet to vote:

| State | Seats | Primary |
|---|---|---|
| Massachusetts | 9 | Sept 1 |
| New Hampshire | 2 | Sept 8 |
| Rhode Island | 2 | Sept 9 |
| Delaware | 1 | Sept 15 |

**Some states never produce a party nominee at all.** Four states do not run
the standard one-nominee-per-party primary, and a schema with a single
"Democratic nominee" column per district is simply wrong for them:

- **Louisiana** (6 seats) — in 2026 the state reverted to an all-party
  "jungle" primary held *on* general election day, Nov 3, with a Dec 12
  runoff. There is no Democratic nominee, only Democratic candidates.
- **California, Washington** (62 seats) — top-two. A district's general
  election ballot can carry two Democrats, or none.
- **Alaska** (1 seat) — top-four, ranked choice.

So the roster is a list of `(district, candidate)` pairs, never a dict keyed by
district, and `BallotRule` is a first-class field. `dcp doctor` reports how
many seats are genuinely unsettled on any given date.

### 2. Keyword-matching "Medicare for All" overcounts support

Every naive failure mode pushes the number in the same direction:

- *"Protect and strengthen Medicare"* is near-universal Democratic boilerplate
  and has nothing to do with Medicare for All. Bare "Medicare" never matches.
- *"I don't support Medicare for All"* contains the phrase verbatim.
- *"My opponent supports Medicare for All"* is somebody else's position.
- Endorsement pages quote third parties at length.

`src/dcp/classify/` therefore scores each match **in sentence context** and
resolves a stance — affirmed, negated, or attributed to a third party — before
letting it set a tier. See `tests/test_classifier.py`, which is written around
exactly these traps.

---

## The scale

Ordered, most to least transformative. Ordinal categories, not a numeric score.

| Tier | Meaning |
|---|---|
| `explicit_m4a` | Affirmatively names Medicare for All / single-payer as their position |
| `single_payer_substance` | Describes universal single-payer without the brand name |
| `public_option` | Public option, Medicare buy-in, lowering the eligibility age |
| `aca_strengthen` | Protect/expand the ACA, cap drug costs — no structural change |
| `no_healthcare_position` | Material was read; it contains no healthcare position |
| `opposed` | Explicitly opposes Medicare for All |
| `unknown` | **Could not read the material. Not a finding.** |

Two distinctions carry most of the weight:

- **`unknown` ≠ `no_healthcare_position`.** The first is missing data, the
  second is a finding. Collapsing them inflates the denominator of any "share
  who support X" statistic. They stay distinct through to the report, which
  always states coverage before it states a share.
- **A candidate can hold a position and reject M4A.** "I back a public option,
  not Medicare for All" is a `public_option` tier *and* an M4A rejection;
  `explicitly_rejects_m4a` is tracked separately from `tier`.

The report gives the headline under both readings of "supports Medicare for
All" — explicit endorsement only, and explicit plus single-payer-in-substance —
and never blends them.

---

## Pipeline

```
dcp doctor      # can this environment reach the sources at all?
dcp calendar    # which primary dates are verified, which are missing
dcp roster      # FEC filing universe x Wikipedia primary results -> roster.json
dcp websites    # attach campaign URLs (Ballotpedia hints, then verify by fetching)
dcp classify    # crawl each site's issues pages, assign a tier with evidence
dcp fec         # cross-check the roster against the FEC bulk filing universe
dcp ratings     # attach Cook Political Report race ratings, via Wikipedia
dcp louisiana   # replace LA's roster with the Secretary of State's certified list
dcp overrides   # apply human-reviewed position corrections
dcp adjust      # apply reviewed roster membership changes
dcp report      # report.md + candidates.csv + analysis.json + needs_review.csv
dcp run         # all of the above
```

Stages are separate commands because each is slow and network-bound and worth
inspecting before paying for the next one.

| Module | Role |
|---|---|
| `models.py` | Schema. `BallotRule`, `NominationStatus`, `M4ATier`, provenance, evidence |
| `statefacts.py` | Apportionment, ballot rules, primary calendar, what is unsettled |
| `net.py` | Cached, rate-limited, robots-aware fetching; egress diagnostics |
| `sources/fec.py` | Candidate universe via the OpenFEC API (**filers, not winners**) |
| `sources/fec_bulk.py` | The same universe from the bulk candidate master file, no API key |
| `sources/wikipedia.py` | Primary *results* — who actually won |
| `sources/ratings.py` | Cook Political Report race ratings, read from Wikipedia |
| `sources/louisiana.py` | LA's certified qualifying list from the Secretary of State |
| `sources/ballotpedia.py` | Campaign website URLs |
| `resolve.py` | Merge sources; record conflicts rather than resolving them |
| `websites.py` | Verify a URL really is that candidate's campaign site |
| `crawl.py` | Follow a site's own nav to its issues pages |
| `classify/` | Taxonomy, stance detection, tier assignment with evidence |
| `adjudicate.py` | Review queue for cases the rules cannot settle |
| `config/roster_adjustments.yaml` | Reviewed additions/exclusions: who is on the roster at all |
| `report.py` | Aggregation with honest denominators |
| `scripts/build_chart.py` | Newsletter chart PNG, rendered from the published data |

### Design commitments

- **Provenance on every fact.** Each field carries a source URL and retrieval
  timestamp; each tier above `no_healthcare_position` carries a verbatim quote
  and the rule ID that fired. Any row in the final table can be audited.
- **Conflicts are recorded, not resolved.** When sources disagree, both land in
  `Candidate.conflicts`. Silently picking a winner would make the dataset look
  cleaner than the evidence is.
- **Ambiguity is flagged, not guessed.** Cases the rules cannot settle go to
  `needs_review.csv`.
- **The optional LLM adjudicator must ground its answer.** It has to return a
  quote that appears verbatim in the source text; if it does not, the answer is
  rejected and logged. It cannot invent evidence.
- **Failures fail safe.** A missing calendar file means "date unknown", which
  makes the pipeline *refuse* to assert on-ballot status — never "primary
  already held".

---

## The dashboard

`docs/` is a static GitHub Pages site built from a completed run:

- **`index.html`** — searchable, filterable table of all 432 candidates. Filter by
  position, state or evidence source; open any row for the verbatim quote, the rule
  that fired, the campaign URL and any recorded conflicts. Filters are encoded in the
  URL, so a filtered view can be shared.
- **`analysis.html`** — the written analysis.
- **`data.json`** — the payload, regenerated by `scripts/build_site.py`.

```bash
make site     # rebuild docs/data.json from data/out/
make serve    # http://localhost:8000
```

To publish: **Settings → Pages → Deploy from a branch**, branch
`claude/house-candidates-medicare-analysis-qyihx3` (the default branch), folder
`/docs`. See `docs/README.md`.

---

## Usage

```bash
make install
make test          # 89 tests, no network required
dcp doctor         # check reachability before spending a run
dcp run --as-of 2026-09-16
```

Set `FEC_API_KEY` for the OpenFEC API. `DEMO_KEY` works but is rate-limited too
hard to finish a 435-district run: ten requests an hour against the thirteen
pages the 2026 Democratic House field alone needs.

`dcp fec` sidesteps that entirely. The [bulk candidate master
file](https://www.fec.gov/campaign-finance-data/candidate-master-file-description/)
carries the same records, needs no key, and arrives in one request. The stage
runs the cross-check in both directions:

* every on-ballot name is looked for among that seat's filers, so a name with
  no filing is flagged as a probable transcription error;
* every seat with active Democratic filers is looked for in the roster, so a
  seat with filers and no candidate is flagged as a probable missed nominee.

It also attaches what only the FEC has: canonical candidate IDs and incumbency.
What it cannot supply is who *won* a primary — the file lists every filer — so
nomination status still comes from Wikipedia.

### Race ratings

`dcp ratings` attaches The Cook Political Report's competitiveness rating for
each candidate's district. It does not read cookpolitical.com: that site's
robots.txt names the ratings dataset as proprietary, and `/ratings/` sits
behind a Cloudflare challenge returning 403 to any automated client. Getting
past that challenge would be evasion, so the pipeline does not try.

The ratings come instead from Wikipedia, which republishes them under CC BY-SA
with a citation to Cook against each one. Two articles cover the whole House
between them: the national ratings article carries the ~155 seats some rater
calls competitive, refreshed within days, and each state's own article carries
a per-district table where the safe seats are. The national table wins where
both have a district, being the fresher of the two.

The rating describes the **seat**, not the candidate, and it is Cook's
editorial judgement rather than this project's. It is carried for context,
displayed with attribution, and never feeds a classification. A district in
neither source is left unrated rather than assumed safe.

### Louisiana

Louisiana is the one state a primary-results source cannot answer. It holds no
nominating primary in 2026: after *Louisiana v. Callais* forced a mid-cycle
redraw, the governor postponed the party primaries and the state reverted to an
all-party ballot held **on** election day. Whoever qualified is on the November
ballot and nobody else is, so the roster there is the qualifying list.

`dcp louisiana` takes it from the Secretary of State. The two sources it
replaced were wrong in opposite directions:

* Wikipedia's per-district "Declared" sections over-count and mislabel party.
  Of the four Democrats listed for LA-01, one qualified; the candidate given as
  Democratic in LA-02 qualified No Party.
* The campaign-finance fallback under-counts, catching only candidates who
  raised enough to appear in a finance table — and it kept Cleo Fields, the
  LA-06 incumbent, who has money and withdrew to run for state senate.

Access differs sharply between the state's two hosts. `www.sos.la.gov` serves
`Disallow: /` to every agent but a handful of named search engines, so nothing
there is fetchable. The Voter Portal disallows `/CandidateInquiry/Parish/` and
`/CandidateInquiry/Statewide/`, legacy paths its own robots.txt comment says
generate error reports, while the endpoints the page actually calls
(`/CandidateInquiry/StatewideCandidate/*`) are covered by neither prefix. A run
costs two requests.

The filing is a public record carrying each candidate's home address, phone
number and email. Only name, party, district and filing date are read into the
roster. The contact *domain* is used as a lead when looking for a campaign
site — it is often the campaign's own — but the address itself is never stored,
and the test fixture is scrubbed.

### Roster adjustments

`config/overrides.yaml` corrects what a candidate's position is.
`config/roster_adjustments.yaml` corrects who is on the roster at all, and
`dcp adjust` applies it. Both exist so a judgement made by a person looking at
the race survives the next `dcp roster` run instead of being silently reverted.

It is for decisions the sources cannot make. A candidate missing or wrongly
present because a parser misread a page is a bug to fix in the parser.

There is one entry as of this writing. Alaska's at-large seat has no Democrat
contesting it in earnest: the only Democrat on the November ballot is a
perennial filer serving a federal sentence who did not campaign, took 3.8% of
the primary vote, and reached the general only because Matt Schultz withdrew —
endorsing the independent, Bill Hill, who placed second with 32.1%. Hill is
included in his place.

That makes `party` a field on `Candidate` rather than an assumption. It
defaults to `Democratic`; anything else is carried through to the CSV, marked
with an asterisk in the tracker, and skipped by the FEC cross-check, whose
filing universe is Democratic. An excluded candidate is marked `excluded` and
keeps the reason rather than being deleted — they are genuinely on the ballot,
so recording them as withdrawn or defeated would state something untrue.

---

## Why there is no data yet

This pipeline was built in an environment whose egress policy allowlists GitHub
only. Every data source is unreachable:

```
$ dcp doctor
  [FAIL] en.wikipedia.org         egress-blocked
  [FAIL] ballotpedia.org          egress-blocked
  [FAIL] api.open.fec.gov         egress-blocked
  [FAIL] www.fec.gov              egress-blocked
  [FAIL] api.congress.gov         egress-blocked
```

`dcp roster` exits 2 rather than running, deliberately: a completed run under
these conditions would emit a roster of zero candidates and 435 coverage gaps,
which reads like a finding about the election rather than a finding about the
network.

To collect real data, run it where those hosts are reachable.

## Before trusting any output

1. **Verify the HTML selectors.** The Wikipedia and Ballotpedia parsers are
   tested against fixtures, not live pages. Both sites drift. Run one state
   first and read the output by hand.
2. **Fill in `config/primary_calendar.yaml`** from the FEC's official dates.
   Only four states are verified; the rest are deliberately absent.
3. **Check Ballotpedia's terms** before a full-scale crawl. `net.Fetcher`
   honors robots.txt and rate-limits, but several hundred district pages is
   bulk use — prefer their API.
4. **Read `needs_review.csv`.** It is where the classifier admits it is unsure.
5. **Read the coverage line before quoting a share.** If coverage is below 80%,
   the report says so, and the headline figure should not travel on its own.
6. **Re-verify redistricting.** Mid-decade map changes (Texas, Missouri, Ohio,
   California Prop 50, the Louisiana litigation) change district lines, not
   counts — but they do change who is running where.
