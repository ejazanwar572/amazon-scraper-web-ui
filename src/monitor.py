"""Scrape monitoring with sliding-window statistics and Rich output."""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Any

from rich.console import Console
from rich.table import Table

from src.config import MonitoringConfig
from src.models import ScrapeResult

__all__ = ["ScrapeMonitor"]

logger = logging.getLogger(__name__)

_BLOCK_STATUS_CODES = frozenset({403, 503})
_WINDOW_SIZE = 1000
_ALERT_THRESHOLD = 0.30  # 30 % block rate


class ScrapeMonitor:
    """Tracks scraping statistics and health in a sliding window."""

    def __init__(self, config: MonitoringConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._console = Console()

        # Sliding window of recent results
        self._window: deque[ScrapeResult] = deque(maxlen=_WINDOW_SIZE)

        # Lifetime counters
        self._total_requests: int = 0
        self._total_success: int = 0
        self._total_failed: int = 0
        self._total_blocked: int = 0
        self._error_counts: Counter[str] = Counter()

        self._start_time: float = time.monotonic()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_request(self, result: ScrapeResult) -> None:
        """Record the outcome of a single scrape request."""
        with self._lock:
            self._window.append(result)
            self._total_requests += 1

            if result.success:
                self._total_success += 1
            else:
                self._total_failed += 1
                if result.error:
                    self._error_counts[result.error] += 1

            if result.status_code in _BLOCK_STATUS_CODES:
                self._total_blocked += 1

    def get_stats(self) -> dict[str, Any]:
        """Return a snapshot of current statistics."""
        with self._lock:
            return self._compute_stats()

    def print_stats(self) -> None:
        """Pretty-print statistics using Rich."""
        stats = self.get_stats()

        table = Table(title="Scrape Monitor", show_header=True, header_style="bold cyan")
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")

        table.add_row("Total Requests", str(stats["total_requests"]))
        table.add_row("Success", str(stats["total_success"]))
        table.add_row("Failed", str(stats["total_failed"]))
        table.add_row("Blocked (403/503)", str(stats["total_blocked"]))
        table.add_row("Success Rate", f"{stats['success_rate']:.1%}")
        table.add_row("Block Rate (last 100)", f"{stats['block_rate']:.1%}")
        table.add_row("Avg Response Time", f"{stats['avg_response_time_ms']:.0f} ms")
        table.add_row("Requests / min", f"{stats['requests_per_minute']:.1f}")
        table.add_row("Uptime", f"{stats['uptime_seconds']:.0f} s")

        self._console.print(table)

        if stats["error_breakdown"]:
            err_table = Table(title="Error Breakdown", show_header=True, header_style="bold red")
            err_table.add_column("Error", style="dim")
            err_table.add_column("Count", justify="right")
            for err, cnt in stats["error_breakdown"].most_common(10):
                err_table.add_row(err, str(cnt))
            self._console.print(err_table)

    def should_alert(self) -> bool:
        """Return ``True`` if block rate in the last 100 requests exceeds threshold."""
        with self._lock:
            recent = list(self._window)[-100:]
            if not recent:
                return False
            blocked = sum(1 for r in recent if r.status_code in _BLOCK_STATUS_CODES)
            return (blocked / len(recent)) > _ALERT_THRESHOLD

    async def send_alert(self, message: str) -> None:
        """POST an alert to the configured webhook URL."""
        url = self._config.alert_webhook_url
        if not url:
            logger.warning("Alert triggered but no webhook URL configured")
            return

        try:
            import httpx  # Optional dependency

            payload = {
                "message": message,
                "stats": self.get_stats(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            # Serialise Counter → dict for JSON
            payload["stats"]["error_breakdown"] = dict(payload["stats"]["error_breakdown"])

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
            logger.info("Alert sent to webhook: %s", url)
        except ImportError:
            logger.error("httpx is required for webhook alerts — install it with: pip install httpx")
        except Exception:
            logger.exception("Failed to send alert to %s", url)

    def reset(self) -> None:
        """Reset all counters and the sliding window."""
        with self._lock:
            self._window.clear()
            self._total_requests = 0
            self._total_success = 0
            self._total_failed = 0
            self._total_blocked = 0
            self._error_counts.clear()
            self._start_time = time.monotonic()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _compute_stats(self) -> dict[str, Any]:
        """Build the stats dict.  Must be called while ``_lock`` is held."""
        now = time.monotonic()
        uptime = now - self._start_time

        # Success rate (lifetime)
        success_rate = (
            self._total_success / self._total_requests
            if self._total_requests > 0
            else 0.0
        )

        # Block rate over last 100 requests in the window
        recent = list(self._window)[-100:]
        if recent:
            blocked_recent = sum(1 for r in recent if r.status_code in _BLOCK_STATUS_CODES)
            block_rate = blocked_recent / len(recent)
        else:
            block_rate = 0.0

        # Average response time in the window
        if self._window:
            avg_rt = sum(r.response_time_ms for r in self._window) / len(self._window)
        else:
            avg_rt = 0.0

        # Requests per minute from the window timestamps
        if len(self._window) >= 2:
            oldest_ts = self._window[0].timestamp
            newest_ts = self._window[-1].timestamp
            span_seconds = (newest_ts - oldest_ts).total_seconds()
            rpm = (len(self._window) / span_seconds * 60) if span_seconds > 0 else 0.0
        else:
            rpm = 0.0

        return {
            "total_requests": self._total_requests,
            "total_success": self._total_success,
            "total_failed": self._total_failed,
            "total_blocked": self._total_blocked,
            "success_rate": success_rate,
            "block_rate": block_rate,
            "avg_response_time_ms": avg_rt,
            "requests_per_minute": rpm,
            "uptime_seconds": uptime,
            "error_breakdown": Counter(self._error_counts),
        }
