import asyncio
import logging
import sys
from src.config import load_config
from main import run_scrape_keywords

# Setup verbose logging to console
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

async def test():
    config = load_config()
    # Force postgresql backend for testing
    config.storage.backend = "postgresql"
    config.storage.pg_dsn = "postgresql://localhost:5432/amazon_scraper"
    config.monitoring.log_level = "DEBUG"
    
    print("Starting keyword scrape test run...")
    await run_scrape_keywords(config, ["mechanical keyboard"], max_pages=1)
    print("Keyword scrape test run finished!")

asyncio.run(test())
