"""URL queue with bloom-filter deduplication."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mmh3
from bitarray import bitarray

from src.config import QueueConfig

__all__ = ["BloomFilter", "URLQueue"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bloom filter
# ---------------------------------------------------------------------------


class BloomFilter:
    """Probabilistic set membership using *mmh3* hashes and a *bitarray*.

    Parameters
    ----------
    capacity:
        Expected number of items.
    error_rate:
        Target false-positive probability.
    """

    def __init__(self, capacity: int = 1_000_000, error_rate: float = 0.001) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if not (0 < error_rate < 1):
            raise ValueError("error_rate must be in (0, 1)")

        self._capacity = capacity
        self._error_rate = error_rate

        # Optimal bit-array size: m = -n·ln(p) / (ln2)²
        ln2 = math.log(2)
        self._size = int(math.ceil(-capacity * math.log(error_rate) / (ln2 ** 2)))
        # Optimal number of hashes: k = (m/n) · ln2
        self._num_hashes = max(1, int(round((self._size / capacity) * ln2)))

        self._bits = bitarray(self._size)
        self._bits.setall(False)
        self._count = 0

    # ------------------------------------------------------------------

    def add(self, item: str) -> bool:
        """Insert *item*.  Returns ``True`` if it was **possibly already present**."""
        indices = self._hash_indices(item)
        already = all(self._bits[i] for i in indices)
        for i in indices:
            self._bits[i] = True
        if not already:
            self._count += 1
        return already

    def __contains__(self, item: str) -> bool:  # noqa: D105
        return all(self._bits[i] for i in self._hash_indices(item))

    def __len__(self) -> int:  # noqa: D105
        return self._count

    @property
    def size_bytes(self) -> int:
        """Approximate memory footprint of the bit array in bytes."""
        return math.ceil(self._size / 8)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _hash_indices(self, item: str) -> list[int]:
        """Compute *k* bit positions via double-hashing with mmh3."""
        h1 = mmh3.hash(item, seed=0, signed=False)
        h2 = mmh3.hash(item, seed=h1 & 0xFFFF, signed=False)
        return [(h1 + i * h2) % self._size for i in range(self._num_hashes)]


# ---------------------------------------------------------------------------
# URL queue
# ---------------------------------------------------------------------------


class URLQueue:
    """Async URL queue with bloom-filter deduplication.

    Default backend uses :class:`asyncio.Queue` with JSON file persistence.
    """

    def __init__(self, config: QueueConfig) -> None:
        self._config = config
        self._bloom = BloomFilter(
            capacity=config.bloom_filter_capacity,
            error_rate=config.bloom_error_rate,
        )
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        # url → {"error": str, "retry_count": int, "timestamp": str}
        self._failed: dict[str, dict[str, Any]] = {}
        self._done: set[str] = set()
        self._pending_set: set[str] = set()  # track what's currently in the queue
        self._done_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def enqueue(self, urls: list[str]) -> int:
        """Add *urls* to the queue, skipping duplicates.

        Returns the number of genuinely new URLs added.
        """
        added = 0
        for url in urls:
            if url in self._bloom:
                continue
            self._bloom.add(url)
            await self._queue.put(url)
            self._pending_set.add(url)
            added += 1
        if added:
            logger.debug("Enqueued %d new URLs (skipped %d duplicates)", added, len(urls) - added)
        return added

    async def dequeue(self, batch_size: int = 10) -> list[str]:
        """Remove and return up to *batch_size* URLs from the queue."""
        batch: list[str] = []
        for _ in range(batch_size):
            if self._queue.empty():
                break
            url = self._queue.get_nowait()
            self._pending_set.discard(url)
            batch.append(url)
        return batch

    async def mark_done(self, url: str) -> None:
        """Mark a URL as successfully processed."""
        self._done.add(url)
        self._done_count += 1
        self._failed.pop(url, None)

    async def mark_failed(self, url: str, error: str) -> None:
        """Record a failure for *url*."""
        entry = self._failed.get(url, {"error": "", "retry_count": 0, "timestamp": ""})
        entry["error"] = error
        entry["retry_count"] = entry.get("retry_count", 0) + 1
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        self._failed[url] = entry

    async def requeue_failed(self, max_retries: int = 3) -> int:
        """Re-enqueue failed URLs that haven't exceeded *max_retries*.

        Returns the number of URLs requeued.
        """
        requeued = 0
        to_remove: list[str] = []
        for url, info in self._failed.items():
            if info.get("retry_count", 0) < max_retries:
                await self._queue.put(url)
                self._pending_set.add(url)
                to_remove.append(url)
                requeued += 1
        for url in to_remove:
            del self._failed[url]
        if requeued:
            logger.info("Requeued %d failed URLs for retry", requeued)
        return requeued

    async def save_state(self, filepath: str = "data/queue_state.json") -> None:
        """Persist queue state to a JSON file."""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Drain queue to capture pending URLs, then re-fill
        pending: list[str] = []
        while not self._queue.empty():
            pending.append(self._queue.get_nowait())
        for url in pending:
            await self._queue.put(url)

        state = {
            "pending": pending,
            "failed": self._failed,
            "done_count": self._done_count,
        }
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        logger.info("Queue state saved to %s (pending=%d, failed=%d)", filepath, len(pending), len(self._failed))

    async def load_state(self, filepath: str = "data/queue_state.json") -> None:
        """Restore queue state from a JSON file."""
        path = Path(filepath)
        if not path.is_file():
            logger.debug("No queue state file found at %s", filepath)
            return

        raw = json.loads(path.read_text(encoding="utf-8"))

        pending: list[str] = raw.get("pending", [])
        for url in pending:
            self._bloom.add(url)
            await self._queue.put(url)
            self._pending_set.add(url)

        self._failed = raw.get("failed", {})
        for url in self._failed:
            self._bloom.add(url)

        self._done_count = raw.get("done_count", 0)

        logger.info(
            "Queue state loaded from %s (pending=%d, failed=%d, done=%d)",
            filepath,
            len(pending),
            len(self._failed),
            self._done_count,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def pending_count(self) -> int:
        """Number of URLs waiting to be processed."""
        return self._queue.qsize()

    @property
    def done_count(self) -> int:
        """Number of URLs successfully processed."""
        return self._done_count

    @property
    def failed_count(self) -> int:
        """Number of URLs in the failed set."""
        return len(self._failed)
