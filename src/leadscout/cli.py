import logging
import os
import re
from pathlib import Path

import click

from leadscout.audit import audit_websites
from leadscout.config import DEFAULT_RADIUS
from leadscout.discovery import discover_urls
from leadscout.exceptions import APIError, LeadScoutError
from leadscout.models import UrlClassification, UrlSource
from leadscout.search import search_places
from leadscout.storage import load_data, merge_business, save_data


# @click.group() makes this function the parent command that hosts subcommands.
# invoke_without_command=True means running just "leadscout" (no subcommand)
# will execute this function's body instead of showing an error.
@click.group(invoke_without_command=True)
@click.option("--verbose", is_flag=True, help="Enable debug logging")
@click.option("--data-dir", default="./data", help="Directory for data files")
@click.pass_context
def cli(ctx, verbose: bool, data_dir: str) -> None:
    """LeadScout: Find restaurants that need websites."""
    # Set log level based on --verbose flag. DEBUG shows everything,
    # INFO shows operational messages without the noisy details.
    level = logging.DEBUG if verbose else logging.INFO
    # basicConfig configures the root logger once. All modules using
    # logging.getLogger(__name__) inherit this config.
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # ctx.obj is Click's way of sharing state between the group and its
    # subcommands. ensure_object(dict) creates it if it doesn't exist.
    ctx.ensure_object(dict)
    ctx.obj["data_dir"] = data_dir

    # If no subcommand was given, print help text so the user sees
    # what commands are available
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# --- Stub subcommands ---
# Each subcommand is registered with @cli.command(). They'll be filled in
# by later features. @click.pass_context gives them access to ctx.obj
# (where data_dir lives).


@cli.command()
@click.option(
    "--location",
    required=True,
    help='City/state to search around, e.g. "Santa Rosa Beach, FL".',
)
@click.option(
    "--radius",
    default=DEFAULT_RADIUS,
    type=int,
    show_default=True,
    help="Search radius in meters.",
)
@click.pass_context
def search(ctx, location: str, radius: int) -> None:
    """Search for local businesses via Google Places API."""
    # API key comes from the environment; we use os.environ.get instead of
    # python-dotenv (project decision: no implicit .env loading dependency).
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        # Print to stderr (err=True) and exit non-zero so shell scripts can
        # detect the failure. ctx.exit() is Click's clean way to abort.
        click.echo(
            "Error: GOOGLE_PLACES_API_KEY is not set in the environment.",
            err=True,
        )
        ctx.exit(1)

    # Run the search. Any APIError surfaced from search_places (auth,
    # quota, geocoding failures) is a clean user-facing message.
    try:
        businesses = search_places(location, radius, api_key)
    except APIError as e:
        click.echo(f"Error: {e}", err=True)
        ctx.exit(1)
    except LeadScoutError as e:
        # Catch-all for any other LeadScout-defined error so we never
        # leak a raw traceback to the user. Unknown exceptions still
        # propagate so genuine bugs don't hide.
        click.echo(f"Unexpected LeadScout error: {e}", err=True)
        ctx.exit(1)

    # Derive the per-location data file. Slug the location string so it's
    # filesystem-safe: lowercase, non-word characters collapsed to "_".
    # re.sub(r"[^\w]+", "_", ...) replaces runs of non-[a-zA-Z0-9_]
    # characters with a single underscore; .strip("_") trims any leading/
    # trailing underscores (e.g., from a trailing comma).
    data_dir = Path(ctx.obj["data_dir"])
    slug = re.sub(r"[^\w]+", "_", location.lower()).strip("_")
    path = data_dir / f"{slug}.json"

    # Merge into existing data: load any prior results for this location,
    # update existing records by place_id, and append new ones.
    existing = load_data(path)
    by_id = {b.place_id: b for b in existing}
    new_count = 0
    for b in businesses:
        if b.place_id in by_id:
            # merge_business mutates `existing[id]` in place and returns
            # it; the side-effect is what we want here.
            merge_business(by_id[b.place_id], b)
        else:
            by_id[b.place_id] = b
            new_count += 1

    # by_id.values() preserves insertion order (Python 3.7+ guarantee),
    # so existing records keep their position and new ones append.
    save_data(path, list(by_id.values()))
    click.echo(
        f"Found {len(businesses)} businesses ({new_count} new), saved to {path}"
    )


@cli.command()
@click.option(
    "--data-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a JSON file produced by `search`.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Re-search even for businesses that already have a URL.",
)
@click.pass_context
def discover(ctx, data_file: str, force: bool) -> None:
    """Discover and classify website URLs for businesses."""
    # Both env vars are required: the API key authenticates the request,
    # the CX (custom search engine ID) selects which programmable search
    # engine to query against. They're separate per spec convention.
    api_key = os.environ.get("GOOGLE_CUSTOM_SEARCH_API_KEY")
    cx = os.environ.get("GOOGLE_CUSTOM_SEARCH_CX")
    if not api_key or not cx:
        click.echo(
            "Error: GOOGLE_CUSTOM_SEARCH_API_KEY and GOOGLE_CUSTOM_SEARCH_CX "
            "must both be set in the environment.",
            err=True,
        )
        ctx.exit(1)

    # Load whatever the search step (or a previous discover run) wrote.
    path = Path(data_file)
    businesses = load_data(path)
    if not businesses:
        # Empty file is not an error; just nothing to do. Echo a hint
        # so the user knows to run `search` first if they expected data.
        click.echo(f"No businesses in {path}; run `search` first.")
        return

    # Quota tracker lives in --data-dir (default ./data), not next to
    # --data-file. This way a single quota counter is shared across all
    # location files for the day.
    data_dir = Path(ctx.obj["data_dir"])

    try:
        updated = discover_urls(
            businesses, api_key, cx, data_dir=data_dir, force=force
        )
    except APIError as e:
        click.echo(f"Error: {e}", err=True)
        ctx.exit(1)
    except LeadScoutError as e:
        click.echo(f"Unexpected LeadScout error: {e}", err=True)
        ctx.exit(1)

    # Persist updated classifications/URLs back to the same file.
    save_data(path, updated)

    # Summary line: counts by source and classification so the user has
    # a quick view of what changed without reading the JSON.
    total = len(updated)
    discovered = sum(
        1 for b in updated if b.url_source == UrlSource.SEARCH_DISCOVERED
    )
    no_url = sum(1 for b in updated if b.url_source == UrlSource.NONE)
    official = sum(
        1
        for b in updated
        if b.url_classification == UrlClassification.OFFICIAL_SITE
    )
    social = sum(
        1
        for b in updated
        if b.url_classification == UrlClassification.SOCIAL_MEDIA
    )
    directory = sum(
        1
        for b in updated
        if b.url_classification == UrlClassification.DIRECTORY_LISTING
    )
    click.echo(
        f"{total} businesses processed: {discovered} URLs discovered, "
        f"{no_url} still without URL. "
        f"Classifications: {official} official, {social} social, "
        f"{directory} directory."
    )


@cli.command()
@click.option(
    "--data-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a JSON file produced by `search` / `discover`.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Re-audit even businesses with a fresh audit (< 7 days old).",
)
@click.pass_context
def audit(ctx, data_file: str, force: bool) -> None:
    """Audit business websites for performance and features."""
    # Three-level PSI key chain matching the spec's "use Places key if
    # set, fall back to unauthenticated" while adding an explicit primary
    # for clarity. Unauthenticated still works (lower quota).
    api_key = (
        os.environ.get("GOOGLE_PAGESPEED_API_KEY")
        or os.environ.get("GOOGLE_PLACES_API_KEY")
        or None
    )
    if not api_key:
        click.echo(
            "Note: no PageSpeed API key in env; using unauthenticated "
            "PSI requests (lower quota).",
            err=True,
        )

    path = Path(data_file)
    businesses = load_data(path)
    if not businesses:
        click.echo(f"No businesses in {path}; run `search` first.")
        return

    try:
        updated = audit_websites(businesses, api_key, force=force)
    except APIError as e:
        click.echo(f"Error: {e}", err=True)
        ctx.exit(1)
    except LeadScoutError as e:
        # AuditError (e.g., "playwright not installed", browser launch
        # failure) is a subclass of LeadScoutError; same handling.
        click.echo(f"Error: {e}", err=True)
        ctx.exit(1)

    save_data(path, updated)

    # Summary: count how many got audited this run vs total, plus the
    # aggregate deficiency count for a quick read on lead density.
    audited = sum(
        1 for b in updated if b.audit and b.audit.audited_at
    )
    total_deficiencies = sum(
        len(b.audit.deficiencies) for b in updated if b.audit
    )
    click.echo(
        f"{audited} businesses audited, {total_deficiencies} total "
        f"deficiencies recorded. Saved to {path}"
    )


@cli.command()
@click.pass_context
def score(ctx) -> None:
    """Score and rank businesses as leads."""
    click.echo("Not yet implemented.")


@cli.command()
@click.pass_context
def run(ctx) -> None:
    """Run the full pipeline: search -> discover -> audit -> score."""
    click.echo("Not yet implemented.")
