# Medicare for All support among Democratic U.S. House candidates

Generated 2026-08-26 14:15 UTC, reflecting the field as of August 26, 2026.

## Coverage first

- **433** Democratic candidates recorded as on the November ballot.
- **328** (76%) had a position we could actually read and classify.
- **105** could not be classified (no site, unreachable, or too little readable text).
- **20 seats** are in states whose field is not yet settled: primaries still to come, or no party nomination at all. No candidate list compiled today can cover them.
- **49** of the classified rows are flagged for human review; see `needs_review.csv`.

> **Coverage is below 80%.** The shares below should be read as describing the
> candidates we could read, not the full field.

## Headline

| Reading of "supports Medicare for All" | Count | Share of classified | Share of all on-ballot |
|---|---|---|---|
| Explicit endorsement only | 62 | 18.9% | 14.3% |
| Explicit + single-payer in substance | 62 | 18.9% | 14.3% |

## Full distribution

| Position tier | Count | Share of classified |
|---|---|---|
| `explicit_m4a` | 62 | 18.9% |
| `single_payer_substance` | 0 | 0.0% |
| `public_option` | 10 | 3.0% |
| `aca_strengthen` | 110 | 33.5% |
| `no_coverage_position` | 146 | 44.5% |
| `opposed` | 0 | 0.0% |
| `unknown` | 105 | n/a |

## By incumbency

Not available: no source in this run marked which candidates are incumbents. The FEC provides that field, and it was skipped because no API key was configured.

## Known gaps

These are the limits of the dataset, stated so the numbers above are not
mistaken for a complete census.

- FEC source skipped (FEC_API_KEY not set); candidate IDs are synthetic and the roster is not cross-checked against the filing universe
- DE (1 seats): nominees not settled until Sep 15, 2026
- LA (6 seats): all-party primary held on election day Nov 03, 2026; no party nominees exist (runoff Dec 12)
- MA (9 seats): nominees not settled until Sep 01, 2026
- NH (2 seats): nominees not settled until Sep 08, 2026
- RI (2 seats): nominees not settled until Sep 09, 2026
- 14 district(s) with no Democrat recorded as on-ballot (uncontested seats and/or collection gaps): DE-AL, MA-01, MA-02, MA-03, MA-04, MA-05, MA-06, MA-07, MA-08, MA-09, NH-01, NH-02 ...

## Method

Positions are classified from candidates' own campaign websites into ordered
tiers (`explicit_m4a` > `single_payer_substance` > `public_option` >
`aca_strengthen`). Matches are scored in sentence context, so negated
statements ("I don't support Medicare for All") and positions attributed to
third parties ("my opponent supports...") do not count as support. Every
non-trivial classification carries a verbatim quote and the rule that fired;
see the per-candidate CSV/JSON output.
