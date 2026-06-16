"""Product storage backed by SQLite or PostgreSQL."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import psycopg

from src.config import StorageConfig
from src.models import Product

__all__ = ["ProductStorage"]

logger = logging.getLogger(__name__)

# --- SQLite Schema ---
_CREATE_PRODUCTS_SQLITE = """
CREATE TABLE IF NOT EXISTS products (
    asin          TEXT    NOT NULL,
    marketplace   TEXT    NOT NULL,
    title         TEXT,
    price         REAL,
    currency      TEXT,
    rating        REAL,
    review_count  INTEGER,
    bsr           INTEGER,
    availability  TEXT,
    seller        TEXT,
    brand         TEXT,
    category      TEXT,
    image_url     TEXT,
    url           TEXT    NOT NULL,
    scraped_at    TEXT    NOT NULL,
    raw_html_hash TEXT,
    specification TEXT,
    PRIMARY KEY (asin, marketplace)
);
"""

_CREATE_PRICE_HISTORY_SQLITE = """
CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asin        TEXT    NOT NULL,
    marketplace TEXT    NOT NULL,
    price       REAL,
    currency    TEXT,
    scraped_at  TEXT    NOT NULL
);
"""

# --- PostgreSQL Schema ---
_CREATE_PRODUCTS_PG = """
CREATE TABLE IF NOT EXISTS products (
    asin          VARCHAR(50)  NOT NULL,
    marketplace   VARCHAR(50)  NOT NULL,
    title         TEXT,
    price         NUMERIC,
    currency      VARCHAR(10),
    rating        NUMERIC,
    review_count  INTEGER,
    bsr           INTEGER,
    availability  TEXT,
    seller        TEXT,
    brand         TEXT,
    category      TEXT,
    image_url     TEXT,
    url           TEXT         NOT NULL,
    scraped_at    TIMESTAMPTZ  NOT NULL,
    raw_html_hash VARCHAR(64),
    specification VARCHAR(100),
    PRIMARY KEY (asin, marketplace)
);
"""

_CREATE_PRICE_HISTORY_PG = """
CREATE TABLE IF NOT EXISTS price_history (
    id          SERIAL PRIMARY KEY,
    asin        VARCHAR(50)  NOT NULL,
    marketplace VARCHAR(50)  NOT NULL,
    price       NUMERIC,
    currency    VARCHAR(10),
    scraped_at  TIMESTAMPTZ  NOT NULL
);
"""

_CREATE_PRICE_HISTORY_INDEX = """
CREATE INDEX IF NOT EXISTS idx_price_history_asin
    ON price_history (asin, marketplace);
"""

# --- Queries (uses standard '?' placeholder, translated to '%s' for Postgres) ---
_UPSERT_PRODUCT_SQLITE = """
INSERT OR REPLACE INTO products
    (asin, marketplace, title, price, currency, rating, review_count,
     bsr, availability, seller, brand, category, image_url, url,
     scraped_at, raw_html_hash, specification)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""

_UPSERT_PRODUCT_PG = """
INSERT INTO products
    (asin, marketplace, title, price, currency, rating, review_count,
     bsr, availability, seller, brand, category, image_url, url,
     scraped_at, raw_html_hash, specification)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (asin, marketplace) DO UPDATE SET
    title = EXCLUDED.title,
    price = EXCLUDED.price,
    currency = EXCLUDED.currency,
    rating = EXCLUDED.rating,
    review_count = EXCLUDED.review_count,
    bsr = EXCLUDED.bsr,
    availability = EXCLUDED.availability,
    seller = EXCLUDED.seller,
    brand = EXCLUDED.brand,
    category = EXCLUDED.category,
    image_url = EXCLUDED.image_url,
    url = EXCLUDED.url,
    scraped_at = EXCLUDED.scraped_at,
    raw_html_hash = EXCLUDED.raw_html_hash,
    specification = EXCLUDED.specification;
"""

_INSERT_PRICE_HISTORY = """
INSERT INTO price_history (asin, marketplace, price, currency, scraped_at)
VALUES (?, ?, ?, ?, ?);
"""

_SELECT_PRODUCT = """
SELECT asin, marketplace, title, price, currency, rating, review_count,
       bsr, availability, seller, brand, category, image_url, url,
       scraped_at, raw_html_hash, specification
FROM products
WHERE asin = ? AND marketplace = ?;
"""

_SELECT_UPDATED_SINCE = """
SELECT asin, marketplace, title, price, currency, rating, review_count,
       bsr, availability, seller, brand, category, image_url, url,
       scraped_at, raw_html_hash, specification
FROM products
WHERE scraped_at >= ?;
"""

_SELECT_CURRENT_PRICE = """
SELECT price FROM products WHERE asin = ? AND marketplace = ?;
"""

_COLUMN_NAMES = [
    "asin",
    "marketplace",
    "title",
    "price",
    "currency",
    "rating",
    "review_count",
    "bsr",
    "availability",
    "seller",
    "brand",
    "category",
    "image_url",
    "url",
    "scraped_at",
    "raw_html_hash",
    "specification",
]


def _row_to_product(row: tuple) -> Product:
    """Map a DB row tuple to a :class:`Product`."""
    data = dict(zip(_COLUMN_NAMES, row))
    return Product.from_dict(data)


class ProductStorage:
    """Stores scraped products with daily price history snapshots.

    Supports SQLite (WAL mode) and PostgreSQL backends.
    """

    def __init__(self, config: StorageConfig) -> None:
        self._config = config
        self._backend = config.backend.lower()
        self._sqlite_conn: aiosqlite.Connection | None = None
        self._pg_conn: psycopg.AsyncConnection | None = None

    async def initialize(self) -> None:
        """Open the database connection and create tables if needed."""
        if self._backend == "postgresql":
            if not self._config.pg_dsn:
                raise ValueError("pg_dsn must be configured for postgresql backend")
            self._pg_conn = await psycopg.AsyncConnection.connect(self._config.pg_dsn)
            async with self._pg_conn.cursor() as cur:
                await cur.execute(_CREATE_PRODUCTS_PG)
                await cur.execute(_CREATE_PRICE_HISTORY_PG)
                await cur.execute(_CREATE_PRICE_HISTORY_INDEX)
                # Alter table migration to add column if it doesn't exist
                await cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS specification VARCHAR(100);")
                # Clean up existing products with no price
                await cur.execute("DELETE FROM products WHERE price IS NULL;")
                await cur.execute("DELETE FROM price_history WHERE price IS NULL;")
                
                # Backfill specification for existing products where it is NULL
                await cur.execute("SELECT asin, marketplace, title FROM products WHERE specification IS NULL;")
                rows = await cur.fetchall()
                if rows:
                    logger.info("Backfilling specifications for %d existing PostgreSQL products...", len(rows))
                    from src.parser import AmazonParser
                    parser = AmazonParser()
                    for asin, marketplace, title in rows:
                        spec = parser._extract_specification(title)
                        if spec:
                            await cur.execute(
                                "UPDATE products SET specification = %s WHERE asin = %s AND marketplace = %s;",
                                (spec, asin, marketplace)
                            )
            await self._pg_conn.commit()
            logger.info("PostgreSQL storage initialised")
        else:
            path = Path(self._config.db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._sqlite_conn = await aiosqlite.connect(str(path))
            await self._sqlite_conn.execute("PRAGMA journal_mode=WAL;")
            await self._sqlite_conn.execute("PRAGMA foreign_keys=ON;")
            # SQLite Alter table migration to add column if it doesn't exist
            try:
                await self._sqlite_conn.execute("ALTER TABLE products ADD COLUMN specification TEXT;")
            except Exception:
                pass
            await self._sqlite_conn.execute(_CREATE_PRODUCTS_SQLITE)
            await self._sqlite_conn.execute(_CREATE_PRICE_HISTORY_SQLITE)
            await self._sqlite_conn.execute(_CREATE_PRICE_HISTORY_INDEX)
            # Clean up existing products with no price
            await self._sqlite_conn.execute("DELETE FROM products WHERE price IS NULL;")
            await self._sqlite_conn.execute("DELETE FROM price_history WHERE price IS NULL;")
            await self._sqlite_conn.commit()
            
            # Backfill specification for existing products where it is NULL
            cursor = await self._sqlite_conn.execute("SELECT asin, marketplace, title FROM products WHERE specification IS NULL;")
            rows = await cursor.fetchall()
            if rows:
                logger.info("Backfilling specifications for %d existing SQLite products...", len(rows))
                from src.parser import AmazonParser
                parser = AmazonParser()
                for asin, marketplace, title in rows:
                    spec = parser._extract_specification(title)
                    if spec:
                        await self._sqlite_conn.execute(
                            "UPDATE products SET specification = ? WHERE asin = ? AND marketplace = ?;",
                            (spec, asin, marketplace)
                        )
                await self._sqlite_conn.commit()
            logger.info("SQLite storage initialised at %s", self._config.db_path)

    async def _execute(self, sql: str, params: tuple = ()) -> Any:
        """Helper to run a query with the correct placeholder mapping."""
        # Convert datetime objects to ISO strings for SQLite
        params = tuple(
            p.isoformat() if isinstance(p, datetime) and self._backend == "sqlite" else p
            for p in params
        )

        if self._backend == "postgresql":
            if self._pg_conn is None:
                raise RuntimeError("PostgreSQL storage not initialised")
            sql = sql.replace("?", "%s")
            return await self._pg_conn.execute(sql, params)
        else:
            if self._sqlite_conn is None:
                raise RuntimeError("SQLite storage not initialised")
            return await self._sqlite_conn.execute(sql, params)

    async def commit(self) -> None:
        """Commit the current transaction."""
        if self._backend == "postgresql" and self._pg_conn:
            await self._pg_conn.commit()
        elif self._backend == "sqlite" and self._sqlite_conn:
            await self._sqlite_conn.commit()

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def save_product(self, product: Product) -> None:
        """Upsert a single product and record price history if changed."""
        if product.price is None:
            logger.info("Skipping product %s from save: price is None", product.asin)
            return
        # Check whether price has changed to avoid redundant history rows
        await self._record_price_if_changed(product)

        upsert_query = _UPSERT_PRODUCT_PG if self._backend == "postgresql" else _UPSERT_PRODUCT_SQLITE
        await self._execute(
            upsert_query,
            (
                product.asin,
                product.marketplace,
                product.title,
                product.price,
                product.currency,
                product.rating,
                product.review_count,
                product.bsr,
                product.availability,
                product.seller,
                product.brand,
                product.category,
                product.image_url,
                product.url,
                product.scraped_at,
                product.raw_html_hash,
                product.specification,
            ),
        )
        await self.commit()

    async def save_products_batch(self, products: list[Product]) -> int:
        """Upsert a batch of products. Returns the count saved."""
        count = 0
        for product in products:
            if product.price is None:
                logger.info("Skipping product %s from batch save: price is None", product.asin)
                continue
            await self._record_price_if_changed(product)
            upsert_query = _UPSERT_PRODUCT_PG if self._backend == "postgresql" else _UPSERT_PRODUCT_SQLITE
            await self._execute(
                upsert_query,
                (
                    product.asin,
                    product.marketplace,
                    product.title,
                    product.price,
                    product.currency,
                    product.rating,
                    product.review_count,
                    product.bsr,
                    product.availability,
                    product.seller,
                    product.brand,
                    product.category,
                    product.image_url,
                    product.url,
                    product.scraped_at,
                    product.raw_html_hash,
                    product.specification,
                ),
            )
            count += 1
        await self.commit()
        logger.debug("Batch-saved %d products", count)
        return count

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_product(self, asin: str, marketplace: str) -> Product | None:
        """Fetch a single product by ASIN + marketplace."""
        cursor = await self._execute(_SELECT_PRODUCT, (asin, marketplace))
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_product(row)

    async def get_products_updated_since(self, since: datetime) -> list[Product]:
        """Return all products whose ``scraped_at`` >= *since*."""
        cursor = await self._execute(_SELECT_UPDATED_SINCE, (since,))
        rows = await cursor.fetchall()
        return [_row_to_product(r) for r in rows]

    async def get_daily_stats(self) -> dict:
        """Aggregate statistics for the current UTC day."""
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        row = await (
            await self._execute(
                "SELECT COUNT(*) FROM products WHERE scraped_at >= ?",
                (today,),
            )
        ).fetchone()
        products_today = row[0] if row else 0

        row = await (
            await self._execute("SELECT COUNT(*) FROM products")
        ).fetchone()
        total_products = row[0] if row else 0

        row = await (
            await self._execute(
                "SELECT AVG(price) FROM products WHERE price IS NOT NULL AND scraped_at >= ?",
                (today,),
            )
        ).fetchone()
        avg_price = round(float(row[0]), 2) if row and row[0] is not None else 0.0

        row = await (
            await self._execute(
                "SELECT COUNT(DISTINCT seller) FROM products WHERE seller IS NOT NULL AND scraped_at >= ?",
                (today,),
            )
        ).fetchone()
        unique_sellers = row[0] if row else 0

        row = await (
            await self._execute(
                "SELECT COUNT(DISTINCT brand) FROM products WHERE brand IS NOT NULL AND scraped_at >= ?",
                (today,),
            )
        ).fetchone()
        unique_brands = row[0] if row else 0

        return {
            "products_scraped_today": products_today,
            "total_products": total_products,
            "avg_price": avg_price,
            "unique_sellers": unique_sellers,
            "unique_brands": unique_brands,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the database connection."""
        if self._sqlite_conn is not None:
            await self._sqlite_conn.close()
            self._sqlite_conn = None
            logger.info("SQLite storage connection closed")
        if self._pg_conn is not None:
            await self._pg_conn.close()
            self._pg_conn = None
            logger.info("PostgreSQL storage connection closed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _record_price_if_changed(self, product: Product) -> None:
        """Insert a price-history row when the price differs from the stored value."""
        if product.price is None:
            return
        cursor = await self._execute(
            _SELECT_CURRENT_PRICE, (product.asin, product.marketplace)
        )
        row = await cursor.fetchone()
        if row is None or float(row[0]) != product.price:
            await self._execute(
                _INSERT_PRICE_HISTORY,
                (
                    product.asin,
                    product.marketplace,
                    product.price,
                    product.currency,
                    product.scraped_at,
                ),
            )
