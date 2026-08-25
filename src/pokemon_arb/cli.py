"""Command line entry point: ``pokearb <command>``."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer
from sqlalchemy import func, select

from . import store
from .config import get_settings
from .db import get_sessionmaker, init_db
from .freshness import age_label, display_window, fresh_clause
from .models import Deal, Listing, Product, Target
from .money import fmt, pct
from .pipeline.scan import ScanService
from .sources.demo import DemoEbayClient, demo_products

app = typer.Typer(add_completion=False, help="Pokemon card arbitrage scanner.")
log = logging.getLogger("pokearb")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _service(demo: bool) -> ScanService:
    settings = get_settings()
    use_demo = demo or settings.demo_mode or not settings.has_ebay_credentials
    if use_demo and not demo:
        typer.secho(
            "No eBay credentials found -- running in demo mode with synthetic listings.",
            fg=typer.colors.YELLOW,
        )
    return ScanService(ebay_client=DemoEbayClient() if use_demo else None)


@app.command()
def init() -> None:
    """Create the database schema."""
    init_db()
    typer.secho(f"schema ready at {get_settings().sqlalchemy_url}", fg=typer.colors.GREEN)


@app.command("demo-seed")
def demo_seed(per_set: int = typer.Option(5, help="Top cards per set to target.")) -> None:
    """Load the built-in demo catalog and build targets from it."""
    init_db()
    service = ScanService(ebay_client=DemoEbayClient())
    products = service.sync_products(demo_products())
    targets = service.build_targets(per_set=per_set)
    typer.secho(f"seeded {products} cards, {targets} targets", fg=typer.colors.GREEN)


@app.command()
def sync(
    csv_path: Path | None = typer.Option(None, "--csv", help="A PriceCharting price-guide CSV."),
    price_guide: bool = typer.Option(
        False, "--price-guide", help="Download the full category guide."
    ),
    category: str = typer.Option("pokemon-cards", help="PriceCharting category slug."),
    query: list[str] = typer.Option([], "--query", "-q", help="Look up specific cards by name."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Pull market comps from PriceCharting into the local catalog."""
    _setup_logging(verbose)
    init_db()
    service = ScanService()
    if csv_path:
        count = service.sync_from_csv_text(csv_path.read_text())
    elif price_guide:
        count = service.sync_from_price_guide(category)
    elif query:
        count = service.sync_from_queries(list(query))
    else:
        typer.secho("Pass one of --csv, --price-guide or --query.", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    typer.secho(f"synced {count} cards", fg=typer.colors.GREEN)


@app.command()
def targets(
    per_set: int = typer.Option(25, help="How many top-value cards to track per set."),
    set_name: list[str] = typer.Option([], "--set", help="Limit to these sets."),
) -> None:
    """Rebuild the target list: the most valuable cards in each set."""
    init_db()
    count = ScanService().build_targets(per_set=per_set, sets=list(set_name) or None)
    typer.secho(f"{count} targets active", fg=typer.colors.GREEN)


@app.command()
def scan(
    max_targets: int | None = typer.Option(None, help="Stop after this many targets."),
    listings: int | None = typer.Option(None, help="Listings to pull per target."),
    demo: bool = typer.Option(False, "--demo", help="Use synthetic listings, no eBay calls."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Search eBay for every target and rank what comes back."""
    _setup_logging(verbose)
    init_db()
    run = _service(demo).run(max_targets=max_targets, listings_per_target=listings)
    if run.status == "no_targets":
        typer.secho(
            "No search targets configured -- nothing to scan.\n"
            "Load comps and build targets first:\n"
            '  pokearb sync --price-guide   (or: pokearb sync -q "charizard")\n'
            "  pokearb targets --per-set 25",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)
    color = typer.colors.GREEN if run.status == "ok" else typer.colors.RED
    typer.secho(
        f"scan {run.status}: {run.targets_scanned} targets, {run.listings_seen} listings, "
        f"{run.deals_found} deals, {run.api_calls} api calls",
        fg=color,
    )
    if run.error:
        typer.secho(run.error, fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command()
def top(
    limit: int = typer.Option(20, help="How many deals to print."),
    status: str = typer.Option("new"),
    min_confidence: float = typer.Option(0.0),
) -> None:
    """Print the current ranked deal board."""
    init_db()
    with get_sessionmaker()() as session:
        settings = get_settings()
        stmt = (
            select(Deal)
            .join(Listing)
            .where(
                Listing.is_active.is_(True),
                fresh_clause(settings),
                Deal.match_confidence >= min_confidence,
            )
        )
        if status != "all":
            stmt = stmt.where(Deal.status == status)
        deals = list(session.scalars(stmt.order_by(Deal.score.desc()).limit(limit)))
        if not deals:
            hours = display_window(settings).total_seconds() / 3600
            typer.secho(
                f"No deals with listing data under {hours:.0f}h old. Run 'pokearb scan'.",
                fg=typer.colors.YELLOW,
            )
            return
        header = (
            f"{'SCORE':>7}  {'PROFIT':>10} {'ROI':>6} {'OFF':>5} {'CONF':>5}  {'CARD':<32} LISTING"
        )
        typer.secho(header, bold=True)
        typer.echo("-" * len(header))
        for deal in deals:
            typer.echo(
                f"{deal.score:>7.0f}  {fmt(deal.profit_cents):>10} {pct(deal.roi):>6} "
                f"{pct(deal.discount_pct):>5} {deal.match_confidence:>5.2f}  "
                f"{deal.product.name[:32]:<32} {deal.listing.title[:46]}"
            )
            typer.secho(
                f"{'':>7}  seen {age_label(deal.listing)} ago", fg=typer.colors.BRIGHT_BLACK
            )
            if deal.risk_flags:
                typer.secho(f"{'':>7}  risk: {', '.join(deal.risk_flags)}", fg=typer.colors.YELLOW)


@app.command()
def stats() -> None:
    """Counts for the local catalog."""
    init_db()
    with get_sessionmaker()() as session:
        rows = {
            "cards": session.scalar(select(func.count()).select_from(Product)),
            "targets": session.scalar(select(func.count()).select_from(Target)),
            "listings": session.scalar(select(func.count()).select_from(Listing)),
            "deals": session.scalar(select(func.count()).select_from(Deal)),
        }
        for key, value in rows.items():
            typer.echo(f"{key:>10}: {value}")
        last = store.latest_scan(session)
        if last:
            typer.echo(f"{'last scan':>10}: {last.started_at:%Y-%m-%d %H:%M} ({last.status})")


@app.command()
def serve(
    host: str = typer.Option(None, help="Bind address; defaults to HOST or 0.0.0.0."),
    port: int = typer.Option(None, help="Bind port; defaults to PORT or 8000."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes."),
) -> None:
    """Run the web app."""
    import uvicorn

    settings = get_settings()
    init_db()
    uvicorn.run(
        "pokemon_arb.web.app:app",
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
    )


def main() -> None:  # pragma: no cover - console entry point
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
