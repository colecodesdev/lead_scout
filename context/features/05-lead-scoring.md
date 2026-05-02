# Lead Scoring Spec

## Overview

Score each business as a lead based on accumulated data from search, discovery, and audit stages. Assign a numeric score and tier, build a human-readable deficiency summary, and produce the final ranked JSON output. Lives in `src/leadscout/scoring.py` and wired to the `score` CLI subcommand.

## Requirements

- `score_leads(businesses: list[Business], audits: dict[str, Audit]) -> list[Lead]` as the main function. `audits` keyed by `place_id`.
- **Tier assignment (mutually exclusive, evaluated in order):**
  - `"no_website"`: `url_classification` is `"none"`, `"social_media"`, or `"directory_listing"`. These businesses have no official site.
  - `"failing_audit"`: has an official site but Lighthouse mobile performance < 50 OR accessibility < 50 OR 3+ DOM check failures
  - `"missing_features"`: has an official site, passes basic Lighthouse thresholds, but has 1-2 DOM check failures
  - `"skip"`: has an official site that passes audit. Not a lead.
- **Numeric score (0-100 scale, higher = better lead):**
  - Base score by tier: `no_website` = 80, `failing_audit` = 60, `missing_features` = 40, `skip` = 0
  - Modifiers (additive, applied to base):
    - No website at all (not even social/directory): +10
    - Google rating >= 4.0 (higher-value business, worth pursuing): +5
    - Google rating >= 4.5: +5 more (total +10 for rating)
    - Each missing DOM element (menu, hours, contact, SSL, ordering, reservation): +2 each
    - Lighthouse mobile performance < 30: +5
    - Load time > 5 seconds: +3
    - Broken assets found: +2
  - Cap at 100
- **Deficiency summary**: list of plain-English strings pulled from audit data. Examples: "No website found", "No online menu", "Missing SSL certificate", "Mobile performance: 28/100", "Load time: 8.2s", "No hours listed", "3 broken images". This is what Cole reads when deciding who to contact.
- Wire to CLI: `leadscout score --data-file data/santa_rosa_beach_fl.json`
- Output: update the location JSON with Lead records attached. Also print a ranked summary table to stdout: rank, name, score, tier, top 3 deficiencies.
- `leadscout score --export csv` option: dump ranked leads to `data/leads_{location}_{date}.csv` for easy scanning or import
- Skip businesses with tier `"skip"` in the output summary (still stored in JSON for completeness)
- Log: total leads by tier, score distribution, top 10 leads

## Files to Create

1. `src/leadscout/scoring.py`: Scoring logic
2. `tests/test_scoring.py`: Unit tests
3. `tests/fixtures/scored_business_no_site.json`: Business fixture with no website for scoring
4. `tests/fixtures/scored_business_bad_audit.json`: Business fixture with poor audit results

## Files to Modify

1. `src/leadscout/cli.py`: Wire up `score` subcommand with `--data-file` and `--export` options

## Key Gotchas

- The score modifiers are intentionally simple and additive. Resist making this a weighted ML model. The output goes to a human (Cole) who makes the final call. The score just sorts the list so the best leads are at the top.
- Rating-based modifiers reward targeting businesses that are popular but underserved online. A 4.8-star restaurant with no website is a better pitch than a 2.3-star one because the owner clearly runs a good business and has budget to invest.
- The tier thresholds (Lighthouse < 50, 3+ DOM failures) are starting values. These should live in `config.py` and be easy to tune after the first real scan.
- CSV export should use stdlib `csv` module. No pandas.

## Environment Variables

```
# No new env vars for this feature.
```

## Notes

- The `run` subcommand (full pipeline) chains: search -> discover -> audit -> score. Now that all four features exist, `run` should be implemented as a single command that executes all stages in sequence, passing the data file between them.
- The scoring weights and tier thresholds should all be constants in `config.py`, not hardcoded in `scoring.py`. This makes it easy to tune after real-world data comes in.
- Future iteration: add a `--min-score` flag to filter output. Not in v1, but the structure supports it trivially.

## Testing

1. Business with no website, no social, rating 4.6. Assert tier = `"no_website"`, score = 80 (base) + 10 (no presence at all) + 10 (rating 4.5+) = 100.
2. Business with official site, Lighthouse mobile performance 25, no menu, no hours, no SSL. Assert tier = `"failing_audit"`, score = 60 + 2 + 2 + 2 + 5 = 71. Assert deficiency summary contains "Mobile performance: 25/100", "No online menu", "No hours listed", "Missing SSL certificate".
3. Business with official site, all audits passing. Assert tier = `"skip"`, score = 0.
4. Three businesses with different scores. Assert output list is sorted descending by score.

## References

- @context/features/01-project-scaffold.md
- @context/features/04-website-audit.md
- @context/leadscout_design_doc.md (Scoring and Storage section)
- @coding-standards.md
