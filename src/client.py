"""HTTP client with TLS fingerprinting and anti-detection for Amazon scraping.

Uses curl_cffi to impersonate real browser TLS fingerprints, rotates headers
and user agents, handles retries with exponential backoff, and detects
CAPTCHA/WAF blocks.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import logging
import random
import time
import urllib.parse
from typing import TYPE_CHECKING

from curl_cffi.requests import AsyncSession
from playwright.async_api import async_playwright

from src.config import ScraperConfig
from src.models import ScrapeResult
from src.monitor import ScrapeMonitor
from src.proxy import ProxyManager

if TYPE_CHECKING:
    from curl_cffi.requests import Response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# User-Agent pool — 24 real, current user agents
# ---------------------------------------------------------------------------

_USER_AGENTS: list[str] = [
    # Chrome 125 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome 125 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome 125 – Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome 126 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Chrome 126 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Chrome 126 – Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Chrome 127 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    # Chrome 127 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    # Chrome 128 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    # Chrome 128 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    # Firefox 126 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Firefox 126 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Firefox 126 – Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Firefox 127 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    # Firefox 127 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0",
    # Firefox 127 – Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
    # Firefox 128 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    # Firefox 128 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0",
    # Firefox 128 – Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    # Safari 17.4 – macOS Sonoma
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    # Safari 17.5 – macOS Sonoma
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    # Safari 17.3 – macOS Ventura
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    # Safari 17.2 – macOS Sonoma
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    # Chrome 127 – Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
]

# Impersonate targets supported by curl_cffi that match our UA pool
_IMPERSONATE_TARGETS: list[str] = [
    "chrome110",
    "chrome120",
    "chrome124",
]

# Referer pool — realistic referrers for Amazon product pages
_REFERER_POOL: list[str] = [
    "https://www.amazon.{tld}/s?k={query}",
    "https://www.amazon.{tld}/",
    "https://www.google.com/",
    "https://www.google.co.in/",
    "",  # direct navigation — no referer
]

_MARKETPLACE_TLD: dict[str, str] = {
    "in": "in",
    "us": "com",
    "uk": "co.uk",
    "de": "de",
    "fr": "fr",
    "ca": "ca",
    "au": "com.au",
    "jp": "co.jp",
}

# ---------------------------------------------------------------------------
# Block-detection signals
# ---------------------------------------------------------------------------

_CAPTCHA_SIGNALS: list[str] = [
    "captcha",
    "Type the characters you see in this image",
    "Enter the characters you see below",
    "api-services-support@amazon",
    "Sorry, we just need to make sure you're not a robot",
    "To discuss automated access to Amazon data",
]

_WAF_SIGNALS: list[str] = [
    "Request blocked",
    "Access Denied",
    "The request could not be satisfied",
    "bm-verify",
    "akamai",
]


def _detect_block(status_code: int, body: str) -> str | None:
    """Return a block reason string if the response looks like a block, else ``None``."""
    if status_code == 503:
        return "rate_limited_503"
    if status_code == 429:
        return "rate_limited_429"
    if status_code == 403:
        return "forbidden_403"

    body_lower = body[:8000].lower()  # only inspect head of page
    for signal in _CAPTCHA_SIGNALS:
        if signal.lower() in body_lower:
            return "captcha"
    for signal in _WAF_SIGNALS:
        if signal.lower() in body_lower:
            return "waf_challenge"

    return None


# ---------------------------------------------------------------------------
# AmazonClient
# ---------------------------------------------------------------------------


class AmazonClient:
    """HTTP client with TLS fingerprinting and anti-detection for Amazon."""

    def __init__(
        self,
        config: ScraperConfig,
        proxy_manager: ProxyManager,
        monitor: ScrapeMonitor,
    ) -> None:
        self._config = config
        self._proxy_manager = proxy_manager
        self._monitor = monitor

        self._timeout: int = config.http.timeout
        self._max_retries: int = config.http.max_retries
        self._retry_backoff: float = config.http.retry_backoff
        self._concurrency: int = config.http.concurrent_requests
        self._delay_min: float = config.scraping.request_delay_min
        self._delay_max: float = config.scraping.request_delay_max
        self._marketplace: str = config.scraping.marketplace

        self._tld: str = _MARKETPLACE_TLD.get(self._marketplace, "com")
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(self._concurrency)
        self._playwright_semaphore: asyncio.Semaphore = asyncio.Semaphore(2)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_product_page(
        self,
        url: str,
        session_id: str | None = None,
    ) -> ScrapeResult:
        """Fetch a single product page with full anti-detection."""
        return await self._fetch_with_retry(url, session_id=session_id)

    async def fetch_batch(
        self,
        urls: list[str],
        concurrency: int = 50,
    ) -> list[ScrapeResult]:
        """Fetch multiple product pages with controlled concurrency.

        ``concurrency`` overrides the config value for this batch only.
        """
        sem = asyncio.Semaphore(min(concurrency, self._concurrency))

        async def _limited(url: str) -> ScrapeResult:
            async with sem:
                result = await self._fetch_with_retry(url)
                await asyncio.sleep(self._get_random_delay())
                return result

        tasks = [asyncio.create_task(_limited(u)) for u in urls]
        return list(await asyncio.gather(*tasks, return_exceptions=False))

    # ------------------------------------------------------------------
    # Header construction
    # ------------------------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        """Generate realistic browser headers with rotated values."""
        ua = random.choice(_USER_AGENTS)
        is_chrome = "Chrome/" in ua and "Safari/" in ua and "Firefox" not in ua
        is_firefox = "Firefox/" in ua
        # is_safari: everything else

        headers: dict[str, str] = {
            "User-Agent": ua,
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

        if is_chrome:
            chrome_ver = ua.split("Chrome/")[1].split(".")[0]
            headers.update(
                {
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,"
                        "application/signed-exchange;v=b3;q=0.7"
                    ),
                    "Accept-Language": random.choice(
                        [
                            "en-US,en;q=0.9",
                            "en-GB,en;q=0.9",
                            "en-IN,en;q=0.9,hi;q=0.8",
                            "en-US,en;q=0.9,de;q=0.8",
                        ]
                    ),
                    "Sec-Ch-Ua": (
                        f'"Chromium";v="{chrome_ver}", '
                        f'"Google Chrome";v="{chrome_ver}", '
                        '"Not-A.Brand";v="99"'
                    ),
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": random.choice(
                        ['"Windows"', '"macOS"', '"Linux"']
                    ),
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": random.choice(
                        ["none", "same-origin", "cross-site"]
                    ),
                    "Sec-Fetch-User": "?1",
                }
            )
        elif is_firefox:
            headers.update(
                {
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,image/avif,image/webp,*/*;q=0.8"
                    ),
                    "Accept-Language": random.choice(
                        [
                            "en-US,en;q=0.5",
                            "en-GB,en;q=0.5",
                            "en-IN,en;q=0.5",
                        ]
                    ),
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": random.choice(["none", "same-origin"]),
                    "Sec-Fetch-User": "?1",
                }
            )
        else:
            # Safari
            headers.update(
                {
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,*/*;q=0.8"
                    ),
                    "Accept-Language": random.choice(
                        [
                            "en-US,en;q=0.9",
                            "en-GB,en;q=0.9",
                        ]
                    ),
                }
            )

        # Referer
        referer_template = random.choice(_REFERER_POOL)
        if referer_template:
            headers["Referer"] = referer_template.format(
                tld=self._tld,
                query=random.choice(
                    ["laptop", "headphones", "phone case", "usb cable", "book"]
                ),
            )

        return headers

    # ------------------------------------------------------------------
    # Delay / jitter
    # ------------------------------------------------------------------

    def _get_random_delay(self) -> float:
        """Random delay between configured min and max, with ±15 % jitter."""
        base = random.uniform(self._delay_min, self._delay_max)
        jitter = base * random.uniform(-0.15, 0.15)
        return max(0.1, base + jitter)

    # ------------------------------------------------------------------
    # Core fetch with retry
    # ------------------------------------------------------------------

    async def _fetch_with_retry(
        self,
        url: str,
        session_id: str | None = None,
    ) -> ScrapeResult:
        """Fetch with exponential backoff retry and block detection."""
        last_error: str = "unknown"

        for attempt in range(1, self._max_retries + 1):
            async with self._semaphore:
                result = await self._do_fetch(url, session_id, attempt)

            self._monitor.record_request(result)

            if result.success:
                return result

            last_error = result.error or "unknown"

            # Don't retry on captcha — proxy is likely burned. Try Playwright fallback immediately.
            if last_error in ("captcha", "waf_challenge"):
                logger.warning("CAPTCHA/Block detected for %s. Triggering Playwright fallback...", url)
                try:
                    playwright_result = await self._fetch_with_playwright(url, session_id)
                    self._monitor.record_request(playwright_result)
                    return playwright_result
                except Exception as exc:
                    logger.exception("Playwright fallback crashed on block")
                    return result

            if attempt < self._max_retries:
                backoff = self._retry_backoff * (2 ** (attempt - 1))
                backoff += random.uniform(0, backoff * 0.25)  # jitter
                logger.info(
                    "Retry %d/%d for %s in %.1fs (error: %s)",
                    attempt,
                    self._max_retries,
                    url,
                    backoff,
                    last_error,
                )
                await asyncio.sleep(backoff)

        # All retries exhausted — try Playwright browser automation fallback
        logger.warning("HTTP fetching failed for %s. Triggering Playwright fallback...", url)
        try:
            playwright_result = await self._fetch_with_playwright(url, session_id)
            self._monitor.record_request(playwright_result)
            if playwright_result.success:
                logger.info("Playwright fallback succeeded for %s", url)
                return playwright_result
            last_error = playwright_result.error or "playwright_failed"
        except Exception as exc:
            last_error = f"PlaywrightError: {exc}"
            logger.exception("Playwright fallback crashed")

        return ScrapeResult(
            url=url,
            success=False,
            status_code=0,
            html=None,
            html_hash=None,
            error=f"max_retries_exceeded: {last_error}",
            response_time_ms=0.0,
            proxy_used=None,
            timestamp=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Single-attempt fetch
    # ------------------------------------------------------------------

    async def _do_fetch(
        self,
        url: str,
        session_id: str | None,
        attempt: int,
    ) -> ScrapeResult:
        """Execute a single HTTP request with anti-detection measures."""
        proxy: str | None = self._proxy_manager.get_proxy(session_id)
        headers = self._build_headers()
        impersonate = random.choice(_IMPERSONATE_TARGETS)

        start = time.perf_counter()
        status_code = 0
        html: str | None = None
        error: str | None = None

        try:
            async with AsyncSession(
                impersonate=impersonate,
                timeout=self._timeout,
                verify=False,
            ) as session:
                response: Response = await session.get(
                    url,
                    headers=headers,
                    proxies={"https": proxy, "http": proxy} if proxy else None,
                    allow_redirects=True,
                )

            elapsed_ms = (time.perf_counter() - start) * 1000
            status_code = response.status_code
            html = response.text

            # Block detection
            block_reason = _detect_block(status_code, html or "")
            if block_reason:
                if proxy:
                    self._proxy_manager.mark_failed(proxy)
                logger.warning(
                    "Block detected: %s (status=%d, url=%s, attempt=%d)",
                    block_reason,
                    status_code,
                    url,
                    attempt,
                )
                return ScrapeResult(
                    url=url,
                    success=False,
                    status_code=status_code,
                    html=html,
                    html_hash=None,
                    error=block_reason,
                    response_time_ms=elapsed_ms,
                    proxy_used=proxy,
                    timestamp=datetime.now(timezone.utc),
                )

            # Non-200 but not a detected block — still an error
            if status_code != 200:
                if proxy:
                    self._proxy_manager.mark_failed(proxy)
                return ScrapeResult(
                    url=url,
                    success=False,
                    status_code=status_code,
                    html=html,
                    html_hash=None,
                    error=f"http_{status_code}",
                    response_time_ms=elapsed_ms,
                    proxy_used=proxy,
                    timestamp=datetime.now(timezone.utc),
                )

            # Success
            if proxy:
                self._proxy_manager.mark_success(proxy)

            html_hash = hashlib.md5(html.encode("utf-8")).hexdigest()

            return ScrapeResult(
                url=url,
                success=True,
                status_code=status_code,
                html=html,
                html_hash=html_hash,
                error=None,
                response_time_ms=elapsed_ms,
                proxy_used=proxy,
                timestamp=datetime.now(timezone.utc),
            )

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            error = f"{type(exc).__name__}: {exc}"
            if proxy:
                self._proxy_manager.mark_failed(proxy)
            logger.error(
                "Request failed: %s (url=%s, attempt=%d, proxy=%s)",
                error,
                url,
                attempt,
                proxy,
            )
            return ScrapeResult(
                url=url,
                success=False,
                status_code=status_code,
                html=None,
                html_hash=None,
                error=error,
                response_time_ms=elapsed_ms,
                proxy_used=proxy,
                timestamp=datetime.now(timezone.utc),
            )

    async def _fetch_with_playwright(
        self,
        url: str,
        session_id: str | None = None,
    ) -> ScrapeResult:
        """Fetch page with headless browser as a fallback to bypass WAF."""
        async with self._playwright_semaphore:
            start = time.perf_counter()
            proxy: str | None = self._proxy_manager.get_proxy(session_id)

            pw_proxy = None
            if proxy:
                try:
                    parsed = urllib.parse.urlparse(proxy)
                    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
                    pw_proxy = {"server": server}
                    if parsed.username and parsed.password:
                        pw_proxy["username"] = parsed.username
                        pw_proxy["password"] = parsed.password
                except Exception:
                    logger.exception("Failed to parse proxy for Playwright: %s", proxy)
                    pw_proxy = {"server": proxy}

            ua = random.choice(_USER_AGENTS)
            status_code = 0
            html = None

            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(
                        headless=True,
                        proxy=pw_proxy,
                    )
                    context = await browser.new_context(
                        user_agent=ua,
                        viewport={"width": 1280, "height": 800},
                    )
                    page = await context.new_page()

                    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    if response:
                        status_code = response.status
                        html = await page.content()

                    await browser.close()

                elapsed_ms = (time.perf_counter() - start) * 1000

                if not html:
                    raise RuntimeError("No content returned from browser")

                # Block detection
                block_reason = _detect_block(status_code, html)
                if block_reason:
                    if proxy:
                        self._proxy_manager.mark_failed(proxy)
                    return ScrapeResult(
                        url=url,
                        success=False,
                        status_code=status_code,
                        html=html,
                        html_hash=None,
                        error=f"playwright_blocked: {block_reason}",
                        response_time_ms=elapsed_ms,
                        proxy_used=proxy,
                        timestamp=datetime.now(timezone.utc),
                    )

                if status_code != 200:
                    if proxy:
                        self._proxy_manager.mark_failed(proxy)
                    return ScrapeResult(
                        url=url,
                        success=False,
                        status_code=status_code,
                        html=html,
                        html_hash=None,
                        error=f"playwright_http_{status_code}",
                        response_time_ms=elapsed_ms,
                        proxy_used=proxy,
                        timestamp=datetime.now(timezone.utc),
                    )

                if proxy:
                    self._proxy_manager.mark_success(proxy)

                html_hash = hashlib.md5(html.encode("utf-8")).hexdigest()
                return ScrapeResult(
                    url=url,
                    success=True,
                    status_code=status_code,
                    html=html,
                    html_hash=html_hash,
                    error=None,
                    response_time_ms=elapsed_ms,
                    proxy_used=proxy,
                    timestamp=datetime.now(timezone.utc),
                )

            except Exception as exc:
                elapsed_ms = (time.perf_counter() - start) * 1000
                error = f"PlaywrightError: {exc}"
                if proxy:
                    self._proxy_manager.mark_failed(proxy)
                logger.error("Playwright fetch failed: %s (url=%s)", error, url)
                return ScrapeResult(
                    url=url,
                    success=False,
                    status_code=status_code,
                    html=None,
                    html_hash=None,
                    error=error,
                    response_time_ms=elapsed_ms,
                    proxy_used=proxy,
                    timestamp=datetime.now(timezone.utc),
                )
