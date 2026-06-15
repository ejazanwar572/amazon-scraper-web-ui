"""Proxy rotation manager with session stickiness."""

from __future__ import annotations

import logging
import random
import threading
import time
from pathlib import Path

from src.config import ProxyConfig

__all__ = ["ProxyManager"]

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_FAILURES = 5


class ProxyManager:
    """Manages proxy rotation with session stickiness.

    Proxies are loaded from a text file (one per line) in the format
    ``protocol://user:pass@host:port``.  Sessions can request a *sticky*
    proxy that remains bound for ``session_duration_sec`` seconds.
    """

    def __init__(self, config: ProxyConfig) -> None:
        self._config = config
        self._lock = threading.Lock()

        # Ordered list of active proxies
        self._proxies: list[str] = []
        # Round-robin index
        self._rr_index: int = 0
        # session_id → (proxy, assignment_timestamp)
        self._sessions: dict[str, tuple[str, float]] = {}
        # proxy → consecutive failure count
        self._failure_counts: dict[str, int] = {}
        # Proxies removed due to excessive failures
        self._failed_proxies: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_proxies(self, filepath: str) -> None:
        """Load proxies from *filepath*, one proxy per line.

        Blank lines and lines starting with ``#`` are skipped.
        """
        path = Path(filepath)
        if not path.is_file():
            logger.warning("Proxy file not found: %s", filepath)
            return

        loaded: list[str] = []
        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                loaded.append(line)

        with self._lock:
            self._proxies = loaded
            self._rr_index = 0
            self._failure_counts = {}
            self._failed_proxies.clear()

        logger.info("Loaded %d proxies from %s", len(loaded), filepath)

    def get_proxy(self, session_id: str | None = None) -> str | None:
        """Return a proxy string.

        If *session_id* is provided the same proxy is returned for the
        duration of ``session_duration_sec``.  After expiry a new proxy is
        assigned.  If no proxies are available, returns ``None``.
        """
        with self._lock:
            if not self._proxies:
                return None

            # --- sticky session handling ---
            if session_id is not None:
                now = time.monotonic()
                if session_id in self._sessions:
                    proxy, ts = self._sessions[session_id]
                    if (now - ts) < self._config.session_duration_sec and proxy in self._proxies:
                        return proxy
                    # expired or proxy removed — assign new one
                    del self._sessions[session_id]

                proxy = self._next_proxy()
                if proxy is not None:
                    self._sessions[session_id] = (proxy, now)
                return proxy

            return self._next_proxy()

    def mark_failed(self, proxy: str) -> None:
        """Record a failure for *proxy*.

        After ``_MAX_CONSECUTIVE_FAILURES`` consecutive failures the proxy
        is removed from the active pool.
        """
        with self._lock:
            count = self._failure_counts.get(proxy, 0) + 1
            self._failure_counts[proxy] = count

            if count >= _MAX_CONSECUTIVE_FAILURES:
                if proxy in self._proxies:
                    self._proxies.remove(proxy)
                    self._failed_proxies.add(proxy)
                    # Clean up any sessions using this proxy
                    expired_sessions = [
                        sid for sid, (p, _) in self._sessions.items() if p == proxy
                    ]
                    for sid in expired_sessions:
                        del self._sessions[sid]
                    logger.warning(
                        "Proxy removed after %d failures: %s  (active: %d)",
                        count,
                        proxy,
                        len(self._proxies),
                    )

    def mark_success(self, proxy: str) -> None:
        """Reset the failure counter for *proxy*."""
        with self._lock:
            self._failure_counts.pop(proxy, None)

    def stats(self) -> dict:
        """Return a snapshot of proxy pool statistics."""
        with self._lock:
            now = time.monotonic()
            active_sessions = sum(
                1
                for _, (p, ts) in self._sessions.items()
                if (now - ts) < self._config.session_duration_sec and p in self._proxies
            )
            return {
                "total_loaded": len(self._proxies) + len(self._failed_proxies),
                "active": len(self._proxies),
                "failed": len(self._failed_proxies),
                "active_sessions": active_sessions,
                "rotation_strategy": self._config.rotation_strategy,
            }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _next_proxy(self) -> str | None:
        """Pick the next proxy according to the configured strategy.

        Must be called while ``self._lock`` is held.
        """
        if not self._proxies:
            return None

        if self._config.rotation_strategy == "random":
            return random.choice(self._proxies)

        # Default: round_robin
        idx = self._rr_index % len(self._proxies)
        proxy = self._proxies[idx]
        self._rr_index = idx + 1
        return proxy
