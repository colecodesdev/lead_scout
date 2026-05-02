# Places Search Spec

## Overview

Implement the Google Places API integration that discovers restaurants in a target area. This is the entry point of the pipeline: takes a location and radius, returns a list of `Business` objects classified by whether they have a website. Lives in `src/leadscout/search.py` and wired to the `search` CLI subcommand.

## Requirements

- `search_places(location: str, radius: int, api_key: str) -> list[Business]` as the main function
- Accept location as a city/state string (e.g., "Santa Rosa Beach, FL"). Geocode it to lat/lng using the Places API Geocoding endpoint before searching.
- Hit the Google Places Nearby Search (New) endpoint filtered to restaurant types
- Paginate through all results using `next_page_token`. Google requires a short delay (~2s) before using the token; respect this.
- Parse each result into a `Business` dataclass: `place_id`, `name`, `address`, `phone` (from Place Details if not in Nearby response), `rating`, `website_url`, `url_source`, `url_classification`
- For businesses WITH a `website` field in the API response: set `url_source = "google_places"`, set `url_classification = "official_site"` (initial assumption, can be reclassified later)
- For businesses WITHOUT a `website` field: set `url_source = "none"`, `url_classification = "none"`
- Set `last_scanned` to current UTC timestamp on every parsed business
- Wire to CLI: `leadscout search --location "Santa Rosa Beach, FL" --radius 5000`
- On completion, merge results into the location's JSON file via `storage.py`
- Log: total results found, count with website, count without, any pagination issues
- Handle API errors: invalid key (401), over quota (429), bad location (ZERO_RESULTS). Log clearly and exit gracefully.
- Unit tests using fixture JSON that mirrors real Places API response shapes

## Files to Create

1. `src/leadscout/search.py`: Places API search logic
2. `tests/test_search.py`: Unit tests
3. `tests/fixtures/places_nearby_response.json`: Sample API response
4. `tests/fixtures/places_nearby_no_website.json`: Sample response for business without website

## Files to Modify

1. `src/leadscout/cli.py`: Wire up `search` subcommand with `--location` and `--radius` options

## Key Gotchas

- Google Places API (New) vs legacy: the "New" API uses different endpoints and field masks. The Nearby Search (New) endpoint is `https://places.googleapis.com/v1/places:searchNearby`. It requires a `fieldMask` header specifying which fields to return. Without the field mask, you get minimal data and still get billed for the full request.
- Field mask for our needs: `places.id,places.displayName,places.formattedAddress,places.nationalPhoneNumber,places.websiteUri,places.rating,places.types`
- The `next_page_token` from the Nearby Search (New) API is NOT immediately usable. Google's docs say to wait 2 seconds. In practice, 2-3 seconds is reliable. Calling before it's ready returns `INVALID_ARGUMENT`.
- Phone number is not always in the Nearby Search response. If we need it, a separate Place Details call per business is required, which costs more against the free credit. Decision: skip phone for now, fill it in only during the audit stage if we're already visiting the site.
- The free $200/month credit covers about 5,000 Nearby Search (New) requests. Each page of results is one request. A typical local scan is 1-3 pages (20 results per page, max 60). This is well within budget.

## Environment Variables

```
GOOGLE_PLACES_API_KEY=
```

## Notes

- The Geocoding step (city string to lat/lng) uses the Geocoding API, which also falls under the $200 free credit. One call per search run.
- Phone numbers are deferred to the audit stage. `Business.phone` will be `None` after this step for most results.
- The initial `url_classification` of "official_site" for businesses with a website field is a best-guess. Some Google Places listings link to Facebook pages or Yelp. The discovery/classification step (feature 03) will correct these.

## Testing

1. Mock a Places API response with 3 businesses (2 with websites, 1 without), run `search_places`, assert correct Business objects with right classifications
2. Mock a paginated response (first page returns `next_page_token`, second page returns final results), assert all businesses collected
3. Mock a 429 response, assert retry behavior and eventual graceful failure

## References

- @context/features/01-project-scaffold.md
- @context/leadscout_design_doc.md (Search and Discovery section)
- https://developers.google.com/maps/documentation/places/web-service/nearby-search
