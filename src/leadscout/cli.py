import logging
import os
from pathlib import Path

import click

from leadscout.audit import audit_websites
from leadscout.config import DEFAULT_RADIUS
from leadscout.discovery import discover_urls, reclassify_urls
from leadscout.exceptions import APIError, LeadScoutError
from leadscout.models import LeadTier, UrlClassification, UrlSource
from leadscout.scoring import (
    csv_path_for_data_file,
    export_to_csv,
    rank_leads,
    score_leads,
)
from leadscout.search import search_places
from leadscout.storage import (
    data_path_for_location,
    load_data,
    merge_business,
    save_data,
)


def _load_env_file(path: Path = Path(".env")) -> None:
    """Lightweight .env loader.

    Project deliberately avoids python-dotenv as a dep (per the scaffold
    spec), so this parses .env itself. Handles the common forms:
    ``KEY=value``, ``KEY="value"``, ``KEY='value'``, ``# comments``,
    blank lines.

    Uses ``os.environ.setdefault`` so values already exported in the
    user's shell take precedence over what's in .env. The loader is
    a no-op when the file is missing.

    Called once from the cli() group function below so every subcommand
    sees the keys without the user having to source .env in PowerShell.
    """
    if not path.exists():
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
        os.environ.setdefault(key, value)


# @click.group() makes this function the parent command that hosts subcommands.
# invoke_without_command=True means running just "leadscout" (no subcommand)
# will execute this function's body instead of showing an error.
@click.group(invoke_without_command=True)
@click.option("--verbose", is_flag=True, help="Enable debug logging")
@click.option("--data-dir", default="./data", help="Directory for data files")
@click.pass_context
def cli(ctx, verbose: bool, data_dir: str) -> None:
    """LeadScout: Find restaurants that need websites."""
    # Load .env from cwd before any subcommand reads env vars. setdefault()
    # in the loader means values already exported in the shell still win.
    _load_env_file()

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

    # Derive the per-location data file via the storage helper so the
    # `run` subcommand below uses the exact same slug logic without
    # duplicating it.
    data_dir = Path(ctx.obj["data_dir"])
    path = data_path_for_location(data_dir, location)

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
@click.option(
    "--data-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a JSON file produced by `search` / `discover` / `audit`.",
)
@click.option(
    "--export",
    type=click.Choice(["csv"], case_sensitive=False),
    default=None,
    help="Optional: also export ranked leads to CSV.",
)
@click.pass_context
def score(ctx, data_file: str, export: str | None) -> None:
    """Score and rank businesses as leads."""
    path = Path(data_file)
    businesses = load_data(path)
    if not businesses:
        click.echo(f"No businesses in {path}; run `search` first.")
        return

    # Score in place; persist before printing the ranked summary so
    # the JSON on disk is the source of truth even if stdout gets
    # piped/truncated.
    score_leads(businesses)
    save_data(path, businesses)

    ranked = rank_leads(businesses)
    _print_ranked_summary(ranked)

    if export == "csv":
        csv_path = csv_path_for_data_file(path)
        export_to_csv(ranked, csv_path)
        click.echo(f"Exported leads to {csv_path}")


def _print_ranked_summary(ranked: list) -> None:
    """Print a human-readable summary of ranked leads to stdout.

    Format: rank, name, score, tier, top 3 reasons. Skip-tier entries
    are excluded from the visible summary (still stored in the JSON
    for completeness, per spec).
    """
    visible = [
        b
        for b in ranked
        if b.lead is not None and b.lead.tier != LeadTier.SKIP
    ]
    if not visible:
        click.echo("No leads above skip tier.")
        return

    # Title line so the columns aren't a wall of context-free numbers.
    click.echo("\nRanked leads (top to bottom):")
    click.echo("=" * 60)
    for rank, biz in enumerate(visible, start=1):
        # biz.lead is non-None per the filter above.
        top_reasons = "; ".join(biz.lead.reasons[:3])
        click.echo(
            f"{rank}. {biz.name}  [{biz.lead.tier.value}]  "
            f"score={biz.lead.score}"
        )
        if top_reasons:
            click.echo(f"   {top_reasons}")
    click.echo("=" * 60)
    click.echo(
        f"Total leads above skip tier: {len(visible)} / "
        f"{len(ranked)} businesses."
    )


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
@click.option(
    "--force",
    is_flag=True,
    help="Force re-discovery + re-audit on existing entries.",
)
@click.option(
    "--export",
    type=click.Choice(["csv"], case_sensitive=False),
    default=None,
    help="Optional: also export ranked leads to CSV after scoring.",
)
@click.pass_context
def run(ctx, location: str, radius: int, force: bool, export: str | None) -> None:
    """Run the full pipeline: search -> discover -> audit -> score."""
    # Places is the only hard requirement (it's the entry point of the
    # pipeline). Custom Search is optional: if its env vars are absent
    # OR if it returns a project-level denial at runtime, we skip the
    # web-discovery step and fall back to local domain-only
    # reclassification so the rest of the pipeline still produces leads.
    # Background: as of Jan 2026 Google closed the Custom Search JSON
    # API to new GCP projects, so for many users discovery is simply
    # unavailable through no fault of their config.
    places_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    cs_key = os.environ.get("GOOGLE_CUSTOM_SEARCH_API_KEY")
    cs_cx = os.environ.get("GOOGLE_CUSTOM_SEARCH_CX")
    psi_key = (
        os.environ.get("GOOGLE_PAGESPEED_API_KEY") or places_key or None
    )
    if not places_key:
        click.echo(
            "Error: GOOGLE_PLACES_API_KEY is not set in the environment.",
            err=True,
        )
        ctx.exit(1)

    # Decide upfront whether Custom Search is available. None means yes;
    # a non-None reason string means we'll skip discovery + log why.
    skip_discover_reason: str | None = None
    if not cs_key or not cs_cx:
        skip_discover_reason = (
            "GOOGLE_CUSTOM_SEARCH_API_KEY or GOOGLE_CUSTOM_SEARCH_CX not set"
        )

    data_dir = Path(ctx.obj["data_dir"])
    path = data_path_for_location(data_dir, location)

    try:
        # --- Stage 1: search (Google Places) ---
        click.echo(f"[1/4] search   : {location} (radius={radius}m)")
        found = search_places(location, radius, places_key)
        # Merge into existing data if the location was scanned before.
        existing = load_data(path)
        by_id = {b.place_id: b for b in existing}
        for b in found:
            if b.place_id in by_id:
                merge_business(by_id[b.place_id], b)
            else:
                by_id[b.place_id] = b
        businesses = list(by_id.values())
        save_data(path, businesses)
        click.echo(f"        found  : {len(found)} ({len(businesses)} total)")

        # --- Stage 2: discover (Custom Search + classify) ---
        if skip_discover_reason:
            # Env-var-driven skip path: never even try the network call.
            click.echo(
                f"[2/4] discover : SKIPPED ({skip_discover_reason}); "
                "running classification only"
            )
            reclassify_urls(businesses)
        else:
            click.echo("[2/4] discover : Custom Search + URL classification")
            try:
                discover_urls(
                    businesses, cs_key, cs_cx, data_dir=data_dir, force=force
                )
            except APIError as e:
                # Runtime denial (e.g., the "project does not have access"
                # 403 that affects new GCP projects). Fall back to the
                # local-only classification so the pipeline continues
                # rather than aborting before audit and score.
                click.echo(
                    f"        Custom Search unavailable: {e}", err=True
                )
                click.echo(
                    "        Falling back to classification-only.",
                    err=True,
                )
                reclassify_urls(businesses)
        save_data(path, businesses)

        # --- Stage 3: audit (PSI + Playwright) ---
        click.echo("[3/4] audit    : PageSpeed Insights + Playwright DOM")
        audit_websites(businesses, psi_key, force=force)
        save_data(path, businesses)

        # --- Stage 4: score ---
        click.echo("[4/4] score    : tier + numeric ranking")
        score_leads(businesses)
        save_data(path, businesses)
    except APIError as e:
        # Search/audit/score errors still hard-fail; only Custom Search
        # was downgraded to soft-fail above (caught inside its try).
        click.echo(f"Error: {e}", err=True)
        ctx.exit(1)
    except LeadScoutError as e:
        click.echo(f"Error: {e}", err=True)
        ctx.exit(1)

    ranked = rank_leads(businesses)
    _print_ranked_summary(ranked)

    if export == "csv":
        csv_path = csv_path_for_data_file(path)
        export_to_csv(ranked, csv_path)
        click.echo(f"Exported leads to {csv_path}")
