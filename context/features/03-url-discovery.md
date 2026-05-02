# URL Discovery & Classification Spec

## Overview

For businesses with no website found in Google Places, run a secondary web search to check if a site exists but isn't linked to their listing. For all businesses with a URL (from Places or discovered here), classify the URL as official site, social media, or directory listing. Lives in `src/leadscout/discovery.py` and wired to the `discover` CLI subcommand.

## Requirements

- `discover_urls(businesses: list[Business], api_key: str, cx: str) -> list[Business]` as the main function
- For each business where `url_source == "none"`: build a search query `"{business name}" "{city, state}"` and hit the Google Custom Search JSON API
- Parse top 5 results from each search. Use `rapidfuzz.fuzz.token_sort_ratio` to compare each result's title/displayed URL against the business name. Accept matches above 70 threshold.
- If a match is found: set `url_source = "search_discovered"` and `website_url` to the matched URL
- If no match is found: leave as `url_source = "none"`, `url_classification = "none"`
- For ALL businesses with a URL (Places-sourced or just discovered), classify the URL:
  - `"social_media"` if domain contains: facebook.com, instagram.com, twitter.com, x.com, tiktok.com
  - `"directory_listing"` if domain contains: yelp.com, tripadvisor.com, grubhub.com, doordash.com, ubereats.com, opentable.com, yellowpages.com
  - `"official_site"` for everything else
- Reclassification updates `url_classification` even for businesses that came in with `"official_site"` from the Places step (catches Facebook pages linked in Google Places)
- Track and log Custom Search API usage count per run. Warn when approaching 80 queries (80% of daily free limit).
- Wire to CLI: `leadscout discover --data-file data/santa_rosa_beach_fl.json`
- On completion, update the location JSON via `storage.py`
- Log per-business: what was searched, what was found, what it was classified as
- Skip businesses that already have `url_source != "none"` unless `--force` flag is passed
- Unit tests for classification logic and fuzzy match threshold behavior

## Files to Create

1. `src/leadscout/discovery.py`: Custom Search API calls, fuzzy matching, URL classification
2. `tests/test_discovery.py`: Unit tests
3. `tests/fixtures/custom_search_response.json`: Sample Custom Search API response
4. `tests/fixtures/custom_search_no_match.json`: Sample response with no relevant results

## Files to Modify

1. `src/leadscout/cli.py`: Wire up `discover` subcommand with `--data-file` and `--force` options

## Key Gotchas

- Google Custom Search JSON API free tier: 100 queries per day, hard cap. No billing fallback; it just returns 429 after 100. The code must count queries and stop before hitting the wall, logging remaining businesses as "discovery skipped, daily limit reached."
- The Custom Search API requires both an API key AND a Custom Search Engine ID (`cx`). The CX is created at https://programmablesearchengine.google.com/ and must be configured to search the entire web (not restricted to specific sites).
- `rapidfuzz.fuzz.token_sort_ratio` is better than `ratio` for business names because word order varies ("The Red Bar" vs "Red Bar, The"). It normalizes word order before comparing.
- A 70 threshold is intentionally permissive because this produces a lead list for human review, not automated action. False positives are cheap. False negatives (missing a real website) make you pitch a business that already has a site, which is embarrassing.
- Some businesses have multiple presences. A restaurant might have both a Facebook page and a Wix site. The Custom Search results could surface either. Take the first result classified as `"official_site"` if one exists; only fall back to social/directory if that's all there is.

## Environment Variables

```
GOOGLE_CUSTOM_SEARCH_API_KEY=
GOOGLE_CUSTOM_SEARCH_CX=
```

## Notes

- The `GOOGLE_CUSTOM_SEARCH_API_KEY` can be the same key as `GOOGLE_PLACES_API_KEY` if both APIs are enabled on the same GCP project. But they're stored as separate env vars for clarity.
- The classification step runs on ALL businesses with URLs, not just newly discovered ones. This catches misclassified URLs from the Places step (e.g., a Google Places listing that links to a Facebook page).
- Businesses classified as `"directory_listing"` or `"social_media"` only are functionally treated the same as "no website" for scoring purposes. They have a web presence but not one they control.

## Testing

1. Feed a business with no URL into `discover_urls` with a mocked Custom Search response containing a fuzzy match. Assert `url_source` changes to `"search_discovered"` and URL is set.
2. Feed a business with a facebook.com URL. Assert classification is `"social_media"`.
3. Feed a business with a yelp.com URL. Assert classification is `"directory_listing"`.
4. Feed a business name "The Donut Hole" with search results including "Donut Hole Destin" (title). Assert fuzzy match succeeds above threshold.
5. Feed a business name "Bud & Alley's" with search results for a completely unrelated business. Assert no match below threshold.

## References

- @context/features/01-project-scaffold.md
- @context/features/02-places-search.md
- @context/leadscout_design_doc.md (Search and Discovery section, Key Decisions: fuzzy matching)
- https://developers.google.com/custom-search/v1/reference/rest/v1/cse/list
- https://rapidfuzz.github.io/RapidFuzz/
