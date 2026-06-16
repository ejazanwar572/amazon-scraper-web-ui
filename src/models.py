"""Data models for the Amazon product scraper."""

from __future__ import annotations

from dataclasses import dataclass, fields, asdict
from datetime import datetime
from typing import Any

__all__ = ["Product", "ScrapeResult"]


@dataclass
class Product:
    """Represents a scraped Amazon product listing."""

    asin: str
    title: str | None
    price: float | None
    currency: str | None
    rating: float | None
    review_count: int | None
    bsr: int | None  # Best Seller Rank
    availability: str | None
    seller: str | None
    brand: str | None
    category: str | None
    image_url: str | None
    url: str
    marketplace: str  # 'in', 'com', etc.
    scraped_at: datetime
    raw_html_hash: str | None  # to detect changes
    specification: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict with ISO-formatted datetime."""
        data = asdict(self)
        data["scraped_at"] = self.scraped_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Product:
        """Deserialize from a plain dict."""
        data = dict(data)  # shallow copy to avoid mutating caller's dict
        raw_ts = data.get("scraped_at")
        if isinstance(raw_ts, str):
            data["scraped_at"] = datetime.fromisoformat(raw_ts)
        # Filter to only known fields so extra keys don't blow up the constructor
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


@dataclass
class ScrapeResult:
    """Outcome of a single scrape attempt."""

    url: str
    success: bool
    status_code: int | None
    html: str | None
    html_hash: str | None
    error: str | None
    response_time_ms: float
    proxy_used: str | None
    timestamp: datetime

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        data: dict[str, Any] = {
            "url": self.url,
            "success": self.success,
            "status_code": self.status_code,
            "html": self.html,
            "html_hash": self.html_hash,
            "error": self.error,
            "response_time_ms": self.response_time_ms,
            "proxy_used": self.proxy_used,
            "timestamp": self.timestamp.isoformat(),
        }
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScrapeResult:
        """Deserialize from a plain dict."""
        data = dict(data)
        raw_ts = data.get("timestamp")
        if isinstance(raw_ts, str):
            data["timestamp"] = datetime.fromisoformat(raw_ts)
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)
