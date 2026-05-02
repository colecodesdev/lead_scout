import logging

import click


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
@click.pass_context
def search(ctx) -> None:
    """Search for local businesses via Google Places API."""
    click.echo("Not yet implemented.")


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
