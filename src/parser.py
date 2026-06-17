"""Amazon product page parser using selectolax.

Extracts structured product data from raw HTML, with multiple CSS-selector
fallbacks per field so the parser survives Amazon's frequent layout changes.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
import re

from selectolax.parser import HTMLParser

from src.models import Product

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Currency symbols → currency code mapping
# ---------------------------------------------------------------------------

_CURRENCY_MAP: dict[str, str] = {
    "₹": "INR",
    "$": "USD",
    "£": "GBP",
    "€": "EUR",
    "¥": "JPY",
    "CA$": "CAD",
    "A$": "AUD",
}

# Pre-compiled patterns
_PRICE_PATTERN: re.Pattern[str] = re.compile(
    r"[₹$£€¥][\s]?[\d,]+(?:\.\d{1,2})?|[\d,]+(?:\.\d{1,2})?\s?[₹$£€¥]"
)
_NUMERIC_PATTERN: re.Pattern[str] = re.compile(r"[\d,]+\.?\d*")
_RATING_PATTERN: re.Pattern[str] = re.compile(r"(\d+(?:\.\d+)?)\s+out\s+of\s+5")
_REVIEW_COUNT_PATTERN: re.Pattern[str] = re.compile(r"([\d,]+)\s+(?:ratings?|reviews?|global ratings?)")
_BSR_PATTERN: re.Pattern[str] = re.compile(
    r"#?([\d,]+)\s+in\s+(.+?)(?:\s*\(|$)", re.IGNORECASE
)
_ASIN_URL_PATTERN: re.Pattern[str] = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
_BRAND_VISIT_PATTERN: re.Pattern[str] = re.compile(
    r"Visit the\s+(.+?)\s+Store", re.IGNORECASE
)
_BRAND_LABEL_PATTERN: re.Pattern[str] = re.compile(
    r"Brand:\s*(.+)", re.IGNORECASE
)


class AmazonParser:
    """Parses Amazon product pages into structured ``Product`` data."""

    def __init__(self, marketplace: str = "in") -> None:
        self._marketplace = marketplace

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self, html: str, url: str) -> Product | None:
        """Parse a product page HTML into a ``Product`` object.

        Returns ``None`` if the ASIN cannot be determined (indicating the
        HTML is not a valid product page).
        """
        tree = HTMLParser(html)

        asin = self._extract_asin(url, tree)
        if asin is None:
            logger.warning("Could not extract ASIN from %s — skipping", url)
            return None

        price, currency = self._extract_price(tree)
        title = self._extract_title(tree)
        specification = self._extract_specification(title)

        product = Product(
            asin=asin,
            title=title,
            price=price,
            currency=currency,
            rating=self._extract_rating(tree),
            review_count=self._extract_review_count(tree),
            bsr=self._extract_bsr(tree),
            availability=self._extract_availability(tree),
            seller=self._extract_seller(tree),
            brand=self._extract_brand(tree),
            category=self._extract_category(tree),
            image_url=self._extract_image_url(tree),
            url=url,
            marketplace=self._marketplace,
            scraped_at=datetime.now(timezone.utc),
            raw_html_hash=hashlib.md5(html.encode("utf-8")).hexdigest(),
            specification=specification,
        )
        return product

    # ------------------------------------------------------------------
    # ASIN
    # ------------------------------------------------------------------

    def _extract_asin(self, url: str, tree: HTMLParser) -> str | None:
        """Extract ASIN from the page data attributes or canonical link, falling back to the URL."""
        try:
            # 1. From canonical link (most reliable indicator of the active variation displayed)
            canonical = tree.css_first("link[rel='canonical']")
            if canonical:
                href = canonical.attributes.get("href", "")
                match = _ASIN_URL_PATTERN.search(href)
                if match:
                    return match.group(1)

            # 2. From meta/body data attribute
            for selector in (
                "input#ASIN",
                "input[name='ASIN']",
                "[data-asin]",
            ):
                node = tree.css_first(selector)
                if node is not None:
                    asin = node.attributes.get("value") or node.attributes.get(
                        "data-asin"
                    )
                    if asin and len(asin) == 10:
                        return asin

            # 3. Fallback: From requested URL path: /dp/B0XXXXXXXXX or /gp/product/B0XXXXXXXXX
            match = _ASIN_URL_PATTERN.search(url)
            if match:
                return match.group(1)

        except Exception:
            logger.exception("Error extracting ASIN from %s", url)
        return None

    # ------------------------------------------------------------------
    # Title
    # ------------------------------------------------------------------

    def _extract_title(self, tree: HTMLParser) -> str | None:
        """Extract product title with fallback selectors."""
        selectors = [
            "#productTitle",
            "span.product-title-word-break",
            "h1.a-size-large span",
            "[data-feature-name='title'] span",
            "#title span",
        ]
        try:
            for sel in selectors:
                node = tree.css_first(sel)
                if node is not None:
                    text = node.text(strip=True)
                    if text:
                        return text
        except Exception:
            logger.exception("Error extracting title")
        return None

    # ------------------------------------------------------------------
    # Price
    # ------------------------------------------------------------------

    def _extract_price(
        self, tree: HTMLParser
    ) -> tuple[float | None, str | None]:
        """Extract the current/deal price and currency code.

        Returns ``(price_float, currency_code)`` or ``(None, None)`` if
        unavailable.
        """
        selectors = [
            # Deal / current price (visually-hidden accessible text)
            "span.a-price .a-offscreen",
            "#corePrice_feature_div span.a-offscreen",
            # Legacy selectors
            "#priceblock_dealprice",
            "#priceblock_ourprice",
            "#priceblock_saleprice",
            # Whole + fraction composite
            ".a-price .a-price-whole",
            # Kindle / other formats
            "#price",
            "#newBuyBoxPrice",
            "#kindle-price",
        ]

        try:
            for sel in selectors:
                node = tree.css_first(sel)
                if node is None:
                    continue

                raw = node.text(strip=True)
                if not raw:
                    continue

                # Handle composite whole+fraction (e.g. selector hit .a-price-whole)
                if sel == ".a-price .a-price-whole":
                    fraction_node = tree.css_first(".a-price .a-price-fraction")
                    fraction = (
                        fraction_node.text(strip=True) if fraction_node else "00"
                    )
                    raw = raw.rstrip(".") + "." + fraction

                return self._parse_price_string(raw)

        except Exception:
            logger.exception("Error extracting price")

        return None, None

    @staticmethod
    def _parse_price_string(raw: str) -> tuple[float | None, str | None]:
        """Parse a raw price string like '₹1,299.00' into (1299.0, 'INR')."""
        currency: str | None = None
        for symbol, code in _CURRENCY_MAP.items():
            if symbol in raw:
                currency = code
                break

        # Extract numeric portion
        numeric_match = _NUMERIC_PATTERN.search(raw.replace(",", ""))
        if numeric_match:
            try:
                price = float(numeric_match.group())
                return price, currency
            except ValueError:
                pass
        return None, currency

    # ------------------------------------------------------------------
    # Rating
    # ------------------------------------------------------------------

    def _extract_rating(self, tree: HTMLParser) -> float | None:
        """Extract average star rating (e.g. 4.2)."""
        selectors = [
            "span.a-icon-alt",
            "#acrPopover",
            '[data-hook="rating-out-of-text"]',
            "#averageCustomerReviews span.a-icon-alt",
            "i.a-icon-star span.a-icon-alt",
        ]
        try:
            for sel in selectors:
                node = tree.css_first(sel)
                if node is None:
                    continue

                # Try title attribute first (for #acrPopover)
                text = node.attributes.get("title", "") or node.text(strip=True)
                match = _RATING_PATTERN.search(text)
                if match:
                    rating = float(match.group(1))
                    if 0 <= rating <= 5:
                        return rating

        except Exception:
            logger.exception("Error extracting rating")
        return None

    # ------------------------------------------------------------------
    # Review count
    # ------------------------------------------------------------------

    def _extract_review_count(self, tree: HTMLParser) -> int | None:
        """Extract total number of ratings/reviews."""
        selectors = [
            "#acrCustomerReviewText",
            '[data-hook="total-review-count"]',
            "#acrCustomerReviewLink span",
            "#reviewsMedley .a-size-base",
        ]
        try:
            for sel in selectors:
                node = tree.css_first(sel)
                if node is None:
                    continue
                text = node.text(strip=True)
                match = _REVIEW_COUNT_PATTERN.search(text)
                if match:
                    return int(match.group(1).replace(",", ""))

                # Fallback: just grab any number
                num_match = _NUMERIC_PATTERN.search(text.replace(",", ""))
                if num_match:
                    val = int(float(num_match.group()))
                    if val > 0:
                        return val

        except Exception:
            logger.exception("Error extracting review count")
        return None

    # ------------------------------------------------------------------
    # Best Sellers Rank
    # ------------------------------------------------------------------

    def _extract_bsr(self, tree: HTMLParser) -> int | None:
        """Extract the primary Best Sellers Rank number."""
        selectors = [
            "#SalesRank",
            "#detailBulletsWrapper_feature_div",
            "#productDetails_detailBullets_sections1",
            ".prodDetTable",
        ]
        try:
            # Strategy 1: look for the BSR table rows
            for sel in selectors:
                node = tree.css_first(sel)
                if node is None:
                    continue
                text = node.text(strip=True)
                match = _BSR_PATTERN.search(text)
                if match:
                    return int(match.group(1).replace(",", ""))

            # Strategy 2: search inside detail container elements
            detail_containers = [
                "#detailBulletsWrapper_feature_div",
                "#detailBullets_feature_div",
                "#productDetails_feature_div",
                "#SalesRank",
                ".prodDetTable",
                "#productDetails_db_sections",
            ]
            for container_sel in detail_containers:
                container = tree.css_first(container_sel)
                if container is None:
                    continue
                for tag in ("li", "tr", "span"):
                    for node in container.css(tag):
                        text = node.text(strip=True)
                        if "best sellers rank" in text.lower() or "bestsellers rank" in text.lower():
                            match = _BSR_PATTERN.search(text)
                            if match:
                                return int(match.group(1).replace(",", ""))

            # Strategy 3: scan raw text of the body to find BSR line
            body_node = tree.css_first("body")
            if body_node:
                body_text = body_node.text()
                for line in body_text.splitlines():
                    line_lower = line.lower()
                    if "best sellers rank" in line_lower or "bestsellers rank" in line_lower:
                        match = _BSR_PATTERN.search(line)
                        if match:
                            return int(match.group(1).replace(",", ""))

        except Exception:
            logger.exception("Error extracting BSR")
        return None

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def _extract_availability(self, tree: HTMLParser) -> str | None:
        """Extract availability status text."""
        selectors = [
            "#availability span",
            "#availability",
            "#outOfStock span",
            "#deliveryBlockMessage",
            ".a-color-success",
        ]
        try:
            for sel in selectors:
                node = tree.css_first(sel)
                if node is None:
                    continue
                text = node.text(strip=True)
                if text:
                    # Normalise multi-line whitespace
                    return " ".join(text.split())

        except Exception:
            logger.exception("Error extracting availability")
        return None

    # ------------------------------------------------------------------
    # Seller
    # ------------------------------------------------------------------

    def _extract_seller(self, tree: HTMLParser) -> str | None:
        """Extract the seller / merchant name."""
        selectors = [
            "#sellerProfileTriggerId",
            "#merchant-info a",
            "#merchant-info",
            "#tabular-buybox .tabular-buybox-text a",
            "#buybox-tabular .tabular-buybox-text[tabular-attribute-name='Sold by'] span",
        ]
        try:
            for sel in selectors:
                node = tree.css_first(sel)
                if node is None:
                    continue
                text = node.text(strip=True)
                if text:
                    return text

        except Exception:
            logger.exception("Error extracting seller")
        return None

    # ------------------------------------------------------------------
    # Brand
    # ------------------------------------------------------------------

    def _extract_brand(self, tree: HTMLParser) -> str | None:
        """Extract the brand name."""
        selectors = [
            "#bylineInfo",
            "a#bylineInfo",
            ".po-brand .po-break-word",
            '[data-feature-name="bylineInfo"]',
            "#brand",
        ]
        try:
            for sel in selectors:
                node = tree.css_first(sel)
                if node is None:
                    continue
                text = node.text(strip=True)
                if not text:
                    continue

                # "Visit the Sony Store"
                visit_match = _BRAND_VISIT_PATTERN.search(text)
                if visit_match:
                    return visit_match.group(1).strip()

                # "Brand: Sony"
                brand_match = _BRAND_LABEL_PATTERN.search(text)
                if brand_match:
                    return brand_match.group(1).strip()

                # Bare brand name
                return text

        except Exception:
            logger.exception("Error extracting brand")
        return None

    # ------------------------------------------------------------------
    # Category
    # ------------------------------------------------------------------

    def _extract_category(self, tree: HTMLParser) -> str | None:
        """Extract the primary product category from breadcrumbs or BSR."""
        selectors = [
            "#wayfinding-breadcrumbs_feature_div ul li:last-child a",
            "#wayfinding-breadcrumbs_container ul li:last-child a",
            ".a-breadcrumb li:last-child a",
            "#nav-subnav .nav-a:first-child",
        ]
        try:
            for sel in selectors:
                node = tree.css_first(sel)
                if node is not None:
                    text = node.text(strip=True)
                    if text:
                        return text

            # Fallback 1: search inside detail container elements
            detail_containers = [
                "#detailBulletsWrapper_feature_div",
                "#detailBullets_feature_div",
                "#productDetails_feature_div",
                "#SalesRank",
                ".prodDetTable",
                "#productDetails_db_sections",
            ]
            for container_sel in detail_containers:
                container = tree.css_first(container_sel)
                if container is None:
                    continue
                for tag in ("li", "tr", "span"):
                    for node in container.css(tag):
                        text = node.text(strip=True)
                        if "best sellers rank" in text.lower():
                            match = _BSR_PATTERN.search(text)
                            if match:
                                return match.group(2).strip()

            # Fallback 2: scan raw text of the body to find BSR line
            body_node = tree.css_first("body")
            if body_node:
                body_text = body_node.text()
                for line in body_text.splitlines():
                    line_lower = line.lower()
                    if "best sellers rank" in line_lower:
                        match = _BSR_PATTERN.search(line)
                        if match:
                            return match.group(2).strip()

        except Exception:
            logger.exception("Error extracting category")
        return None

    # ------------------------------------------------------------------
    # Image URL
    # ------------------------------------------------------------------

    def _extract_image_url(self, tree: HTMLParser) -> str | None:
        """Extract the main product image URL."""
        selectors = [
            "#landingImage",
            "#imgBlkFront",
            "#main-image",
            "#ebooksImgBlkFront",
            ".a-dynamic-image",
        ]
        try:
            for sel in selectors:
                node = tree.css_first(sel)
                if node is None:
                    continue

                # Prefer data-old-hires (high-res) over src (low-res thumb)
                url: str | None = node.attributes.get("data-old-hires") or None

                # Fallback: parse the first URL from data-a-dynamic-image JSON
                if not url:
                    dynamic = node.attributes.get("data-a-dynamic-image", "")
                    if '"' in dynamic:
                        try:
                            url = dynamic.split('"')[1]
                        except IndexError:
                            url = None

                # Fallback: plain src attribute
                if not url:
                    url = node.attributes.get("src")

                if url and url.startswith("http"):
                    return url

        except Exception:
            logger.exception("Error extracting image URL")
        return None

    def _extract_specification(self, title: str | None) -> str | None:
        """Extract product volume/weight/specification from the title."""
        if not title:
            return None
        # Look for weight patterns like 500g, 1kg, 250 grams
        # Look for volume patterns like 650ml, 1L, 10 fl oz
        # Look for count patterns like Pack of 4, 120 count, 2 pcs
        patterns = [
            # Volume and Weight patterns
            r"\b\d+(?:\.\d+)?\s*(?:ml|l|g|kg|grams?|kilograms?|milliliters?|liters?|fl\.?\s*oz\.?)\b",
            # Pieces / count / pack patterns
            r"\b(?:pack\s+of\s+)?\d+\s*(?:pcs?|pieces?|packs?|count|sheets?|rolls?)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                return match.group(0).strip()
        return None


class AmazonSearchParser:
    """Parses Amazon search results pages to extract product ASINs."""

    def __init__(self, marketplace: str = "in") -> None:
        self._marketplace = marketplace

    def extract_asins(self, html: str) -> list[str]:
        """Extract a list of ASINs from search page HTML."""
        tree = HTMLParser(html)
        asins: list[str] = []
        # Amazon search results have data-component-type="s-search-result"
        for node in tree.css("div[data-component-type='s-search-result']"):
            asin = node.attributes.get("data-asin")
            if asin and len(asin) == 10:
                asins.append(asin)
        return asins
