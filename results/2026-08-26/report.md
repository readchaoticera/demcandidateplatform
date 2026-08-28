# Medicare for All support among Democratic U.S. House candidates

Generated 2026-08-28 15:18 UTC, reflecting the field as of August 28, 2026.

## Coverage first

- **433** Democratic candidates recorded as on the November ballot.
- **340** (79%) had a position we could actually read and classify.
- **93** could not be classified (no site, unreachable, or too little readable text).
- **20 seats** are in states whose field is not yet settled: primaries still to come, or no party nomination at all. No candidate list compiled today can cover them.
- **64** of the classified rows are flagged for human review; see `needs_review.csv`.

> **Coverage is below 80%.** The shares below should be read as describing the
> candidates we could read, not the full field.

## Headline

| Reading of "supports Medicare for All" | Count | Share of classified | Share of all on-ballot |
|---|---|---|---|
| Explicit endorsement only | 75 | 22.1% | 17.3% |
| Explicit + single-payer in substance | 75 | 22.1% | 17.3% |

## Adding the legislative record and news coverage

The campaign-site measure above is what candidates choose to tell voters.
Two other evidence types fill in candidates whose sites could not be read,
and one of them is stronger than a website: cosponsoring the bill is a
recorded legislative act.

| Evidence | Candidates |
|---|---|
| Cosponsors H.R.3069, the Medicare for All Act | 89 |
| Position read from their own campaign site | 255 |
| Position from news coverage (site unreadable) | 69 |
| No position from any source | 4 |

Combined, **429 of 433** candidates (99%) now have a position from some source, leaving **4** with none.

On the combined measure, **161** candidates (37.5% of those with a known position) support Medicare for All - against 75 on campaign sites alone.

> **64 of the 89 cosponsors never mention it on their own campaign site.** Cosponsorship and campaign messaging are
> close to disjoint, which is a finding in itself rather than a gap to be
> averaged away.

## Full distribution

| Position tier | Campaign site | Share | Combined | Share |
|---|---|---|---|---|
| `explicit_m4a` | 75 | 22.1% | 161 | 37.5% |
| `single_payer_substance` | 0 | 0.0% | 1 | 0.2% |
| `public_option` | 15 | 4.4% | 24 | 5.6% |
| `aca_strengthen` | 169 | 49.7% | 202 | 47.1% |
| `no_coverage_position` | 81 | 23.8% | 41 | 9.6% |
| `opposed` | 0 | 0.0% | 0 | 0.0% |
| `unknown` | 93 | n/a | 4 | n/a |

## By incumbency

| Group | Explicit M4A | Classified | Share |
|---|---|---|---|
| incumbent | 25 | 131 | 19.1% |
| non-incumbent | 50 | 209 | 23.9% |

## Known gaps

These are the limits of the dataset, stated so the numbers above are not
mistaken for a complete census.

- DE (1 seats): nominees not settled until Sep 15, 2026
- LA (6 seats): all-party primary held on election day Nov 03, 2026; no party nominees exist (runoff Dec 12)
- MA (9 seats): nominees not settled until Sep 01, 2026
- NH (2 seats): nominees not settled until Sep 08, 2026
- RI (2 seats): nominees not settled until Sep 09, 2026
- 14 district(s) with no Democrat recorded as on-ballot (uncontested seats and/or collection gaps): DE-AL, MA-01, MA-02, MA-03, MA-04, MA-05, MA-06, MA-07, MA-08, MA-09, NH-01, NH-02 ...
- 7 of 433 on-ballot candidates have no matching FEC filing; their names may be transcribed incorrectly

## Method

Positions are classified from candidates' own campaign websites into ordered
tiers (`explicit_m4a` > `single_payer_substance` > `public_option` >
`aca_strengthen`). Matches are scored in sentence context, so negated
statements ("I don't support Medicare for All") and positions attributed to
third parties ("my opponent supports...") do not count as support. Every
non-trivial classification carries a verbatim quote and the rule that fired;
see the per-candidate CSV/JSON output.
