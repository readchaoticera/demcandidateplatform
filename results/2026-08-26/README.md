# Collection run: 2026-08-26

Democratic U.S. House general-election candidates and their Medicare for All
positions, as of August 26, 2026.

| File | Contents |
|---|---|
| `report.md` | The analysis: coverage, headline shares, full tier distribution, known gaps |
| `candidates.csv` | One row per candidate: district, status, campaign URL, tier, evidence quote, rule that fired, conflicts |
| `needs_review.csv` | The 49 classified rows the rules could not settle confidently |
| `roster.json` | Full records including per-page provenance and all evidence |

## Reproducing

```bash
make install
dcp roster --as-of 2026-08-26
dcp websites --as-of 2026-08-26 --workers 8
dcp classify --as-of 2026-08-26 --workers 12
dcp report  --as-of 2026-08-26
```

Requires network access to `en.wikipedia.org` and (for the incumbency field,
absent from this run) `api.open.fec.gov` with `FEC_API_KEY` set.

## Read the coverage line before quoting a share

76% of on-ballot candidates could be classified. The remaining 105 are not
evidence of absence - they are candidates whose sites are Javascript-rendered,
unreachable, or unpublished. Every share in `report.md` is given twice, over
classified candidates and over all on-ballot candidates, for that reason.

## Known limitations of this run

- **No incumbency data.** The FEC supplies it; no API key was configured.
- **Javascript-rendered sites are the largest error source.** 77 candidates
  returned near-empty shells to a plain HTTP fetch. Headless Chromium could not
  reach any host through the collection environment's proxy, so these are
  recorded as `unknown` rather than guessed at. Re-running where a browser can
  render would recover most of them.
- **20 seats cannot be covered by any list compiled today**: MA, NH, RI and DE
  had not yet held their primaries, and Louisiana holds no nominating primary
  at all in 2026.
