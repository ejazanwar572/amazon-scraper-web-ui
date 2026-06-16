import asyncio
import os
import shutil
from datetime import datetime, timezone
import psycopg
from src.config import load_config
from src.models import Product, ScrapeResult
from src.parser import AmazonParser, AmazonSearchParser
from src.storage import ProductStorage
from src.queue import URLQueue
from src.proxy import ProxyManager, ProxyConfig

MOCK_HTML = """
<!DOCTYPE html>
<html>
<head>
    <link rel="canonical" href="https://www.amazon.in/dp/B07XJ8C8F2" />
</head>
<body>
    <span id="productTitle" class="a-size-large">Test Amazon Echo Dot (3rd Gen) - 500ml</span>
    <span class="a-price">
        <span class="a-offscreen">₹3,499.00</span>
    </span>
    <span class="a-icon-alt">4.4 out of 5 stars</span>
    <span id="acrCustomerReviewText" class="a-size-base">105,420 ratings</span>
    <div id="availability">
        <span class="a-size-medium a-color-success">In Stock.</span>
    </div>
    <div id="merchant-info">
        Sold by <span>Cloudtail India</span> and Fulfilled by Amazon.
    </div>
    <a id="bylineInfo" class="a-link-normal">Visit the Amazon Store</a>
    <img id="landingImage" src="https://images-na.ssl-images-amazon.com/images/I/6182S7v%2B1EL._SL1000_.jpg" />
</body>
</html>
"""

MOCK_SEARCH_HTML = """
<!DOCTYPE html>
<html>
<body>
    <div data-component-type="s-search-result" data-asin="B07XJ8C8F2"></div>
    <div data-component-type="s-search-result" data-asin="B085VNVNXK"></div>
    <div data-component-type="s-search-result" data-asin="B09XXINVALID"></div> <!-- Invalid, length 10 required -->
</body>
</html>
"""

async def test_all():
    print("Starting integration verification tests...")
    
    # 1. Test configuration
    config = load_config()
    print("Config loaded successfully.")
    
    # 2. Test Parsers
    parser = AmazonParser(marketplace="in")
    product = parser.parse(MOCK_HTML, "https://www.amazon.in/dp/B07XJ8C8F2")
    
    assert product is not None, "Failed to parse product"
    assert product.asin == "B07XJ8C8F2", f"Expected ASIN B07XJ8C8F2, got {product.asin}"
    assert product.title == "Test Amazon Echo Dot (3rd Gen) - 500ml", f"Title mismatch: {product.title}"
    assert product.specification == "500ml", f"Specification mismatch: {product.specification}"
    assert product.price == 3499.0, f"Price mismatch: {product.price}"
    assert product.currency == "INR", f"Currency mismatch: {product.currency}"
    assert product.rating == 4.4, f"Rating mismatch: {product.rating}"
    assert product.review_count == 105420, f"Review count mismatch: {product.review_count}"
    assert "In Stock" in product.availability, f"Availability mismatch: {product.availability}"
    assert "Cloudtail India" in product.seller, f"Seller mismatch: {product.seller}"
    assert "Amazon" in product.brand, f"Brand mismatch: {product.brand}"
    assert "6182S7v" in product.image_url, f"Image URL mismatch: {product.image_url}"
    print("Parser (product page) tests passed successfully.")

    search_parser = AmazonSearchParser(marketplace="in")
    asins = search_parser.extract_asins(MOCK_SEARCH_HTML)
    assert len(asins) == 2, f"Expected 2 ASINs, got {len(asins)}"
    assert "B07XJ8C8F2" in asins
    assert "B085VNVNXK" in asins
    print("Parser (search page) tests passed successfully.")

    # 3. Test Storage - SQLite
    print("Running SQLite storage tests...")
    if os.path.exists("data/test_products.db"):
        os.remove("data/test_products.db")
    config.storage.backend = "sqlite"
    config.storage.db_path = "data/test_products.db"
    
    storage_sqlite = ProductStorage(config.storage)
    await storage_sqlite.initialize()
    await storage_sqlite.save_product(product)
    retrieved = await storage_sqlite.get_product("B07XJ8C8F2", "in")
    assert retrieved is not None, "Failed to retrieve saved product from SQLite"
    assert retrieved.title == product.title
    assert retrieved.specification == "500ml", f"SQLite spec mismatch: {retrieved.specification}"
    
    # Test price change history
    product.price = 3299.0
    product.scraped_at = datetime.now(timezone.utc)
    await storage_sqlite.save_product(product)
    
    stats_sqlite = await storage_sqlite.get_daily_stats()
    assert stats_sqlite["total_products"] == 1
    assert stats_sqlite["products_scraped_today"] == 1

    # Test price alerts logic
    product.price = 2000.0
    product.scraped_at = datetime.now(timezone.utc)
    await storage_sqlite.save_product(product)
    
    alerts = await storage_sqlite.get_price_alerts(min_change_pct=30.0)
    assert len(alerts) == 1, f"Expected 1 price alert, got {len(alerts)}"
    alert_prod, init_price = alerts[0]
    assert alert_prod.asin == "B07XJ8C8F2"
    assert init_price == 3499.0, f"Expected initial price 3499.0, got {init_price}"
    assert alert_prod.price == 2000.0
    
    # Verify that a 50% threshold does not return the alert
    high_alerts = await storage_sqlite.get_price_alerts(min_change_pct=50.0)
    assert len(high_alerts) == 0, f"Expected 0 alerts for 50% change, got {len(high_alerts)}"

    await storage_sqlite.close()
    
    if os.path.exists("data/test_products.db"):
        os.remove("data/test_products.db")
    print("SQLite storage tests passed successfully.")

    # 4. Test Storage - PostgreSQL
    print("Running PostgreSQL storage tests...")
    # Ensure test database exists first
    try:
        conn = await psycopg.AsyncConnection.connect("postgresql://localhost:5432/postgres", autocommit=True)
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM pg_database WHERE datname = 'amazon_scraper_test';")
            exists = await cur.fetchone()
            if not exists:
                await cur.execute("CREATE DATABASE amazon_scraper_test;")
        await conn.close()
    except Exception as e:
        print(f"Warning: Failed to check/create test database: {e}")

    config.storage.backend = "postgresql"
    config.storage.pg_dsn = "postgresql://localhost:5432/amazon_scraper_test"
    
    storage_pg = ProductStorage(config.storage)
    await storage_pg.initialize()
    
    # Truncate tables for a clean test run in the test DB
    async with await psycopg.AsyncConnection.connect(config.storage.pg_dsn) as conn:
        await conn.execute("TRUNCATE TABLE products CASCADE;")
        await conn.execute("TRUNCATE TABLE price_history CASCADE;")
        await conn.commit()
    
    # Reset product price for Postgres test
    product.price = 3499.0
    product.scraped_at = datetime.now(timezone.utc)
    await storage_pg.save_product(product)
    
    retrieved_pg = await storage_pg.get_product("B07XJ8C8F2", "in")
    assert retrieved_pg is not None, "Failed to retrieve saved product from PostgreSQL"
    assert retrieved_pg.title == product.title
    assert retrieved_pg.specification == "500ml", f"Postgres spec mismatch: {retrieved_pg.specification}"
    
    # Test price change history
    product.price = 3199.0
    product.scraped_at = datetime.now(timezone.utc)
    await storage_pg.save_product(product)
    
    stats_pg = await storage_pg.get_daily_stats()
    assert stats_pg["total_products"] == 1
    assert stats_pg["products_scraped_today"] == 1

    # Test price alerts logic on Postgres
    product.price = 2000.0
    product.scraped_at = datetime.now(timezone.utc)
    await storage_pg.save_product(product)
    
    alerts_pg = await storage_pg.get_price_alerts(min_change_pct=30.0)
    assert len(alerts_pg) == 1, f"Expected 1 Postgres price alert, got {len(alerts_pg)}"
    alert_prod_pg, init_price_pg = alerts_pg[0]
    assert alert_prod_pg.asin == "B07XJ8C8F2"
    assert init_price_pg == 3499.0

    await storage_pg.close()
    print("PostgreSQL storage tests passed successfully.")

    # 5. Test Queue with Bloom Filter
    queue = URLQueue(config.queue)
    if os.path.exists("data/queue_state.json"):
        os.remove("data/queue_state.json")
        
    urls = [
        "https://www.amazon.in/dp/B07XJ8C8F2",
        "https://www.amazon.in/dp/B07XJ8C8F2", # Duplicate
        "https://www.amazon.in/dp/B085VNVNXK"
    ]
    added = await queue.enqueue(urls)
    assert added == 2, f"Bloom filter failed to dedup. Added {added} instead of 2"
    assert queue.pending_count == 2, f"Expected 2 pending, got {queue.pending_count}"
    
    batch = await queue.dequeue(1)
    assert len(batch) == 1
    assert batch[0] == "https://www.amazon.in/dp/B07XJ8C8F2"
    assert queue.pending_count == 1
    
    await queue.mark_done(batch[0])
    assert queue.done_count == 1
    
    await queue.save_state("data/test_queue_state.json")
    
    # Reload queue
    new_queue = URLQueue(config.queue)
    await new_queue.load_state("data/test_queue_state.json")
    assert new_queue.done_count == 1
    assert new_queue.pending_count == 1
    
    # Clean up test files
    if os.path.exists("data/test_queue_state.json"):
        os.remove("data/test_queue_state.json")
    print("Queue and Bloom Filter tests passed successfully.")

    # 6. Test Proxy Manager
    p_config = ProxyConfig(session_duration_sec=2, rotation_strategy="round_robin")
    pm = ProxyManager(p_config)
    
    with open("test_proxies.txt", "w") as f:
        f.write("http://proxy1.com:8080\n")
        f.write("http://proxy2.com:8080\n")
        
    pm.load_proxies("test_proxies.txt")
    assert pm.stats()["active"] == 2
    
    p1 = pm.get_proxy("session_1")
    p1_again = pm.get_proxy("session_1")
    assert p1 == p1_again, "Session stickiness failed"
    
    # Wait for session expiry
    await asyncio.sleep(2.5)
    p2 = pm.get_proxy("session_1")
    assert p1 != p2, "Session expiry did not rotate proxy"
    
    # Test proxy failure removal
    for _ in range(5):
        pm.mark_failed(p2)
    assert pm.stats()["active"] == 1
    assert p2 not in pm._proxies, "Failed proxy was not removed"
    
    if os.path.exists("test_proxies.txt"):
        os.remove("test_proxies.txt")
    print("Proxy Manager tests passed successfully.")
    
    print("All integration tests passed successfully! 🎉")

if __name__ == "__main__":
    asyncio.run(test_all())
