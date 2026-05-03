"""Verify connectivity for every external API LeadScout uses.

Loads .env, then makes one minimal call against each service:

- Geocoding API (uses GOOGLE_PLACES_API_KEY)
- Places Nearby Search (New) (uses GOOGLE_PLACES_API_KEY)
- Custom Search JSON API (uses GOOGLE_CUSTOM_SEARCH_API_KEY + CX)
- PageSpeed Insights (GOOGLE_PAGESPEED_API_KEY -> GOOGLE_PLACES_API_KEY -> unauthenticated)
- Playwright Chromium (local browser binary)

Run with:
    uv run python scripts/verify_apis.py

Note: the Custom Search check consumes 1 of the 100 free daily queries.
"""

import os
import sys
from pathlib import Path

import httpx


def load_env(path: Path) -> None:
    """Lightweight .env loader. The project deliberately avoids
    python-dotenv as a dep, so this script parses .env itself.
    Strips matching surrounding quotes; ignores blanks and #-comments.
    """
    if not path.exists():
        print(f"[!] {path} not found", file=sys.stderr)
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        # Strip a single layer of matching quotes (single or double).
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        # Don't clobber a value already set in the real environment.
        os.environ.setdefault(key, value)


def report(name: str, ok: bool, detail: str) -> bool:
    icon = "[PASS]" if ok else "[FAIL]"
    print(f"{icon} {name}: {detail}")
    return ok


def test_geocoding(client: httpx.Client, places_key: str | None) -> bool:
    name = "Geocoding API"
    if not places_key:
        return report(name, False, "GOOGLE_PLACES_API_KEY not set")
    try:
        r = client.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": "Santa Rosa Beach, FL", "key": places_key},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        if status == "OK" and data.get("results"):
            loc = data["results"][0]["geometry"]["location"]
            return report(name, True, f"OK (lat={loc['lat']}, lng={loc['lng']})")
        return report(
            name,
            False,
            f"status={status} {data.get('error_message', '')}".strip(),
        )
    except httpx.HTTPStatusError as e:
        return report(
            name, False, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        )
    except Exception as e:
        return report(name, False, f"{type(e).__name__}: {e}")


def test_places_nearby(client: httpx.Client, places_key: str | None) -> bool:
    name = "Places Nearby Search (New)"
    if not places_key:
        return report(name, False, "GOOGLE_PLACES_API_KEY not set")
    try:
        r = client.post(
            "https://places.googleapis.com/v1/places:searchNearby",
            headers={
                "X-Goog-Api-Key": places_key,
                "X-Goog-FieldMask": "places.id,places.displayName",
                "Content-Type": "application/json",
            },
            json={
                "includedTypes": ["restaurant"],
                "locationRestriction": {
                    "circle": {
                        "center": {"latitude": 30.3766, "longitude": -86.2354},
                        "radius": 500.0,
                    }
                },
                "maxResultCount": 1,
            },
            timeout=15,
        )
        r.raise_for_status()
        n = len(r.json().get("places", []))
        return report(name, True, f"OK (returned {n} place{'s' if n != 1 else ''})")
    except httpx.HTTPStatusError as e:
        return report(
            name, False, f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        )
    except Exception as e:
        return report(name, False, f"{type(e).__name__}: {e}")


def test_custom_search(
    client: httpx.Client, cs_key: str | None, cs_cx: str | None
) -> bool:
    name = "Custom Search JSON API"
    if not cs_key:
        return report(name, False, "GOOGLE_CUSTOM_SEARCH_API_KEY not set")
    if not cs_cx:
        return report(name, False, "GOOGLE_CUSTOM_SEARCH_CX not set")
    try:
        r = client.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": cs_key, "cx": cs_cx, "q": "test", "num": 1},
            timeout=15,
        )
        r.raise_for_status()
        n = len(r.json().get("items", []))
        return report(
            name,
            True,
            f"OK (returned {n} item; used 1 of 100 daily quota)",
        )
    except httpx.HTTPStatusError as e:
        return report(
            name, False, f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        )
    except Exception as e:
        return report(name, False, f"{type(e).__name__}: {e}")


def test_pagespeed(
    client: httpx.Client,
    psi_key: str | None,
    places_key: str | None,
) -> bool:
    """PSI works unauthenticated; we still test with whichever key the
    audit step would actually use, so a key misconfiguration shows up."""
    key = psi_key or places_key
    if psi_key:
        label = "PageSpeed Insights (PSI key)"
    elif places_key:
        label = "PageSpeed Insights (Places fallback)"
    else:
        label = "PageSpeed Insights (unauthenticated)"
    try:
        params: dict[str, str] = {
            "url": "https://www.example.com",
            "strategy": "mobile",
        }
        if key:
            params["key"] = key
        # PSI is slow (10-30s typical). Generous timeout.
        r = client.get(
            "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
            params=params,
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
        if "lighthouseResult" not in data:
            return report(
                label, False, f"unexpected shape: keys={list(data)[:5]}"
            )
        perf = (
            data["lighthouseResult"]["categories"]
            .get("performance", {})
            .get("score")
        )
        perf_str = "n/a" if perf is None else f"{round(perf * 100)}/100"
        return report(label, True, f"OK (mobile perf={perf_str})")
    except httpx.HTTPStatusError as e:
        return report(
            label, False, f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        )
    except Exception as e:
        return report(label, False, f"{type(e).__name__}: {e}")


def test_playwright() -> bool:
    """Confirms the chromium binary is installed and a page can render.
    Uses a data: URL so no network request is needed."""
    name = "Playwright (Chromium)"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return report(name, False, "playwright Python package not installed")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                page = context.new_page()
                page.goto(
                    "data:text/html,<h1>Hello</h1>",
                    wait_until="domcontentloaded",
                    timeout=10_000,
                )
                text = page.inner_text("h1")
                if text != "Hello":
                    return report(name, False, f"unexpected page text: {text!r}")
                return report(name, True, "OK (browser launched, page rendered)")
            finally:
                browser.close()
    except Exception as e:
        return report(name, False, f"{type(e).__name__}: {e}")


def main() -> int:
    load_env(Path(".env"))

    places = os.environ.get("GOOGLE_PLACES_API_KEY")
    cs_key = os.environ.get("GOOGLE_CUSTOM_SEARCH_API_KEY")
    cs_cx = os.environ.get("GOOGLE_CUSTOM_SEARCH_CX")
    psi_key = os.environ.get("GOOGLE_PAGESPEED_API_KEY")

    print("--- LeadScout API connectivity check ---\n")

    results: list[bool] = []
    with httpx.Client(follow_redirects=True) as client:
        results.append(test_geocoding(client, places))
        results.append(test_places_nearby(client, places))
        results.append(test_custom_search(client, cs_key, cs_cx))
        results.append(test_pagespeed(client, psi_key, places))
    results.append(test_playwright())

    n_pass = sum(1 for r in results if r)
    print(f"\n--- {n_pass}/{len(results)} checks passed ---")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
