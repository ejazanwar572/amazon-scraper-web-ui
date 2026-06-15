#!/usr/bin/env python3
"""Amazon product scraper — async CLI orchestrator.

Usage:
    python main.py scrape --input urls.txt [--config config.yaml] [--concurrency 50]
    python main.py scrape --asin-file asins.txt --marketplace in
    python main.py resume [--config config.yaml]
    python main.py stats [--config config.yaml]
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from src.client import AmazonClient
from src.config import ScraperConfig, load_config
from src.models import ScrapeResult
from src.monitor import ScrapeMonitor
from src.parser import AmazonParser, AmazonSearchParser
from src.proxy import ProxyManager
from src.queue import URLQueue
from src.storage import ProductStorage
import urllib.parse

console = Console()
logger = logging.getLogger("amzscraper")

scrape_progress = {
    "active": False,
    "keyword": "",
    "total": 0,
    "done": 0,
    "failed": 0,
    "saved": 0,
    "current_asin": "",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_logging(level: str) -> None:
    """Configure root logging with rich-compatible formatting."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
    )


def _build_asin_url(asin: str, marketplace: str) -> str:
    """Convert an ASIN to a full Amazon product URL."""
    domain = f"amazon.{marketplace}" if marketplace != "com" else "amazon.com"
    return f"https://www.{domain}/dp/{asin}"


def _load_urls_from_file(filepath: str) -> list[str]:
    """Read URLs from a text file, one per line."""
    path = Path(filepath)
    if not path.is_file():
        console.print(f"[red]File not found:[/red] {filepath}")
        sys.exit(1)
    urls: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def _load_asins_from_file(filepath: str, marketplace: str) -> list[str]:
    """Read ASINs from a file and convert to URLs."""
    path = Path(filepath)
    if not path.is_file():
        console.print(f"[red]File not found:[/red] {filepath}")
        sys.exit(1)
    urls: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            asin = raw.strip()
            if asin and not asin.startswith("#"):
                urls.append(_build_asin_url(asin, marketplace))
    return urls


def _load_keywords_from_file(filepath: str) -> list[str]:
    """Read keywords from a text file, one per line."""
    path = Path(filepath)
    if not path.is_file():
        console.print(f"[red]File not found:[/red] {filepath}")
        sys.exit(1)
    keywords: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line and not line.startswith("#"):
                keywords.append(line)
    return keywords


async def run_scrape_keywords(
    config: ScraperConfig,
    keywords: list[str],
    max_pages: int,
    concurrency: int | None = None,
) -> None:
    """Keyword-based search scraping workflow."""
    effective_concurrency = concurrency or config.http.concurrent_requests
    _setup_logging(config.monitoring.log_level)

    proxy_manager = ProxyManager(config.proxy)
    proxy_file = Path(config.proxy.proxy_list_file)
    if proxy_file.is_file():
        proxy_manager.load_proxies(str(proxy_file))

    monitor = ScrapeMonitor(config.monitoring)
    search_parser = AmazonSearchParser(marketplace=config.scraping.marketplace)
    client = AmazonClient(config=config, proxy_manager=proxy_manager, monitor=monitor)

    from src.client import _MARKETPLACE_TLD
    tld = _MARKETPLACE_TLD.get(config.scraping.marketplace, "com")

    search_urls: list[str] = []
    for keyword in keywords:
        quoted = urllib.parse.quote_plus(keyword)
        for page in range(1, max_pages + 1):
            url = f"https://www.amazon.{tld}/s?k={quoted}&page={page}"
            search_urls.append(url)

    console.print(f"[cyan]Keyword Search:[/cyan] Fetching {len(search_urls)} search result pages...")

    search_results = await client.fetch_batch(search_urls, concurrency=effective_concurrency)

    asins_found: set[str] = set()
    for result in search_results:
        if result.success and result.html:
            found = search_parser.extract_asins(result.html)
            asins_found.update(found)

    console.print(f"[green]Keyword Search:[/green] Extracted {len(asins_found)} unique ASINs from search results.")

    if not asins_found:
        console.print("[yellow]No ASINs found in search results. Exiting.[/yellow]")
        return

    detail_urls = [_build_asin_url(asin, config.scraping.marketplace) for asin in asins_found]

    await run_scrape(config, detail_urls, concurrency)


# ---------------------------------------------------------------------------
# Core scrape loop
# ---------------------------------------------------------------------------


async def run_scrape(
    config: ScraperConfig,
    urls: list[str],
    concurrency: int | None = None,
) -> None:
    """Main scrape orchestration loop."""

    effective_concurrency = concurrency or config.http.concurrent_requests
    _setup_logging(config.monitoring.log_level)

    # ---- Initialise components ----
    proxy_manager = ProxyManager(config.proxy)
    proxy_file = Path(config.proxy.proxy_list_file)
    if proxy_file.is_file():
        proxy_manager.load_proxies(str(proxy_file))
        console.print(f"[green]Proxies loaded:[/green] {proxy_manager.stats()['active']} active")
    else:
        console.print("[yellow]No proxy file found — scraping without proxies (not recommended at scale)[/yellow]")

    monitor = ScrapeMonitor(config.monitoring)
    parser = AmazonParser(marketplace=config.scraping.marketplace)
    client = AmazonClient(config=config, proxy_manager=proxy_manager, monitor=monitor)

    queue = URLQueue(config.queue)
    await queue.load_state()

    storage = ProductStorage(config.storage)
    await storage.initialize()

    # ---- Enqueue URLs ----
    added = await queue.enqueue(urls)
    total = queue.pending_count

    initial_done = queue.done_count
    initial_failed = queue.failed_count
    run_total = added if added > 0 else total
    
    scrape_progress["active"] = True
    scrape_progress["total"] = run_total
    scrape_progress["done"] = 0
    scrape_progress["failed"] = 0
    scrape_progress["saved"] = 0
    scrape_progress["current_asin"] = ""

    console.print(
        f"[cyan]Queue:[/cyan] {added} new URLs added, {total} total pending "
        f"(bloom filter deduped {len(urls) - added} duplicates)"
    )

    if total == 0:
        console.print("[yellow]Nothing to scrape — queue is empty.[/yellow]")
        await storage.close()
        return

    # ---- Graceful shutdown ----
    shutdown_event = asyncio.Event()

    def _signal_handler(sig: int, frame: object) -> None:
        console.print(f"\n[red]Received signal {sig} — shutting down gracefully...[/red]")
        shutdown_event.set()

    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except ValueError as e:
        logger.warning("Could not register signal handlers (must run in main thread): %s", e)

    # ---- Progress bar ----
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=40),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console,
    )

    scrape_task = progress.add_task("Scraping", total=total)
    start_time = time.monotonic()
    products_saved = 0
    batch_size = min(effective_concurrency, 100)

    try:
        with progress:
            while not shutdown_event.is_set():
                batch = await queue.dequeue(batch_size=batch_size)
                if not batch:
                    # Try requeuing failed URLs
                    requeued = await queue.requeue_failed(max_retries=config.http.max_retries)
                    if requeued == 0:
                        break
                    continue

                # Fetch batch
                results: list[ScrapeResult] = await client.fetch_batch(
                    urls=batch,
                    concurrency=effective_concurrency,
                )

                # Process results
                for result in results:
                    if result.success:
                        if result.html:
                            product = parser.parse(result.html, result.url)
                            if product:
                                await storage.save_product(product)
                                await queue.mark_done(result.url)
                                products_saved += 1
                                scrape_progress["saved"] = products_saved
                                scrape_progress["current_asin"] = product.asin
                            else:
                                await queue.mark_failed(result.url, "parse_failed")
                        else:
                            await queue.mark_failed(result.url, "empty_html")
                    else:
                        await queue.mark_failed(result.url, result.error or "unknown")

                    scrape_progress["done"] = queue.done_count - initial_done
                    scrape_progress["failed"] = queue.failed_count - initial_failed
                    progress.advance(scrape_task)

                # Periodic stats logging
                elapsed = time.monotonic() - start_time
                if elapsed > 0 and queue.done_count % 500 == 0 and queue.done_count > 0:
                    stats = monitor.get_stats()
                    logger.info(
                        "Progress: %d done, %d failed, %.1f req/min, %.1f%% success rate",
                        queue.done_count,
                        queue.failed_count,
                        stats.get("requests_per_minute", 0),
                        stats.get("success_rate", 0) * 100,
                    )

                # Check alert threshold
                if monitor.should_alert():
                    stats = monitor.get_stats()
                    block_rate = stats.get("block_rate", 0)
                    console.print(
                        f"[red]⚠ High block rate detected: {block_rate:.1%}[/red] — consider slowing down"
                    )
                    await monitor.send_alert(
                        f"Block rate {block_rate:.1%} exceeds threshold"
                    )

                # Periodic state save
                if queue.done_count % 1000 == 0 and queue.done_count > 0:
                    await queue.save_state()
    finally:
        scrape_progress["active"] = False

    # ---- Wrap up ----
    await queue.save_state()

    elapsed = time.monotonic() - start_time
    final_stats = monitor.get_stats()

    console.print()
    _print_summary(
        elapsed=elapsed,
        products_saved=products_saved,
        done=queue.done_count,
        failed=queue.failed_count,
        pending=queue.pending_count,
        stats=final_stats,
        proxy_stats=proxy_manager.stats(),
    )

    await storage.close()


def _print_summary(
    elapsed: float,
    products_saved: int,
    done: int,
    failed: int,
    pending: int,
    stats: dict,
    proxy_stats: dict,
) -> None:
    """Print a rich summary table at the end of the scrape."""
    table = Table(title="Scrape Summary", show_header=False, border_style="cyan")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    minutes = elapsed / 60
    table.add_row("Duration", f"{minutes:.1f} min")
    table.add_row("Products Saved", f"{products_saved:,}")
    table.add_row("URLs Processed", f"{done:,}")
    table.add_row("Failed", f"{failed:,}")
    table.add_row("Remaining", f"{pending:,}")
    table.add_row("─" * 20, "─" * 15)
    table.add_row("Success Rate", f"{stats.get('success_rate', 0):.1%}")
    table.add_row("Avg Response Time", f"{stats.get('avg_response_time_ms', 0):.0f} ms")
    table.add_row("Requests/min", f"{stats.get('requests_per_minute', 0):.1f}")
    table.add_row("Block Rate", f"{stats.get('block_rate', 0):.1%}")
    table.add_row("─" * 20, "─" * 15)
    table.add_row("Active Proxies", f"{proxy_stats.get('active', 0)}")
    table.add_row("Failed Proxies", f"{proxy_stats.get('failed', 0)}")

    console.print(Panel(table, border_style="green"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """Amazon Product Scraper — scrape 200K+ products daily."""
    pass


@cli.command()
@click.option("--input", "input_file", type=str, help="File with product URLs (one per line)")
@click.option("--asin-file", type=str, help="File with ASINs (one per line)")
@click.option("--keywords", type=str, default=None, help="Comma-separated keywords to search and scrape")
@click.option("--keyword-file", type=str, default=None, help="File with keywords (one per line)")
@click.option("--max-pages", type=int, default=3, help="Max search result pages to fetch per keyword")
@click.option("--marketplace", type=str, default=None, help="Amazon marketplace (in, com, co.uk, etc.)")
@click.option("--config", "config_path", type=str, default=None, help="Path to config.yaml")
@click.option("--concurrency", type=int, default=None, help="Override concurrent request count")
def scrape(
    input_file: str | None,
    asin_file: str | None,
    keywords: str | None,
    keyword_file: str | None,
    max_pages: int,
    marketplace: str | None,
    config_path: str | None,
    concurrency: int | None,
) -> None:
    """Scrape Amazon product pages."""
    if not input_file and not asin_file and not keywords and not keyword_file:
        console.print("[red]Provide --input (URL file), --asin-file (ASIN file), --keywords, or --keyword-file[/red]")
        sys.exit(1)

    config = load_config(config_path)
    if marketplace:
        config.scraping.marketplace = marketplace

    mk = marketplace or config.scraping.marketplace

    # Determine input type
    keywords_list: list[str] = []
    if keywords:
        keywords_list = [k.strip() for k in keywords.split(",") if k.strip()]
    elif keyword_file:
        keywords_list = _load_keywords_from_file(keyword_file)

    if keywords_list:
        console.print(f"[cyan]Loaded {len(keywords_list):,} keywords (marketplace: {mk})[/cyan]")
        asyncio.run(run_scrape_keywords(config, keywords_list, max_pages, concurrency))
    else:
        if input_file:
            urls = _load_urls_from_file(input_file)
            console.print(f"[cyan]Loaded {len(urls):,} URLs from {input_file}[/cyan]")
        else:
            urls = _load_asins_from_file(asin_file, mk)  # type: ignore[arg-type]
            console.print(f"[cyan]Loaded {len(urls):,} ASINs from {asin_file} (marketplace: {mk})[/cyan]")
        asyncio.run(run_scrape(config, urls, concurrency))


@cli.command()
@click.option("--config", "config_path", type=str, default=None, help="Path to config.yaml")
@click.option("--concurrency", type=int, default=None, help="Override concurrent request count")
def resume(config_path: str | None, concurrency: int | None) -> None:
    """Resume a previously interrupted scrape from saved queue state."""
    config = load_config(config_path)

    async def _resume() -> None:
        queue = URLQueue(config.queue)
        await queue.load_state()
        if queue.pending_count == 0 and queue.failed_count == 0:
            console.print("[yellow]No pending or failed URLs to resume.[/yellow]")
            return
        console.print(
            f"[cyan]Resuming: {queue.pending_count} pending, {queue.failed_count} failed[/cyan]"
        )
        # Re-enqueue with empty list (state already loaded)
        await run_scrape(config, [], concurrency)

    asyncio.run(_resume())


@cli.command()
@click.option("--config", "config_path", type=str, default=None, help="Path to config.yaml")
def stats(config_path: str | None) -> None:
    """Show database statistics for scraped products."""
    config = load_config(config_path)

    async def _stats() -> None:
        storage = ProductStorage(config.storage)
        await storage.initialize()
        daily = await storage.get_daily_stats()
        await storage.close()

        table = Table(title="Database Stats", show_header=False, border_style="cyan")
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")

        for key, value in daily.items():
            table.add_row(key.replace("_", " ").title(), str(value))

        console.print(table)

    asyncio.run(_stats())


if __name__ == "__main__":
    cli()
