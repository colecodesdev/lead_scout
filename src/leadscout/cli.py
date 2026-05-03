import logging
import os
import re
from pathlib import Path

import click

from leadscout.config import DEFAULT_RADIUS
from leadscout.exceptions import APIError, LeadScoutError
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
@click.pass_context
def discover(ctx) -> None:
    """Discover and classify website URLs for businesses."""
    click.echo("Not yet implemented.")


@cli.command()
@click.pass_context
def audit(ctx) -> None:
    """Audit business websites for performance and features."""
    click.echo("Not yet implemented.")


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
