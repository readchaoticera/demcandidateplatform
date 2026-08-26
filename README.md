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
| `sources/fec.py` | Candidate universe and canonical IDs (**filers, not winners**) |
| `sources/wikipedia.py` | Primary *results* — who actually won |
| `sources/ballotpedia.py` | Campaign website URLs |
| `resolve.py` | Merge sources; record conflicts rather than resolving them |
| `websites.py` | Verify a URL really is that candidate's campaign site |
| `crawl.py` | Follow a site's own nav to its issues pages |
| `classify/` | Taxonomy, stance detection, tier assignment with evidence |
| `adjudicate.py` | Review queue for cases the rules cannot settle |
| `report.py` | Aggregation with honest denominators |

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
hard to finish a 435-district run.

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
