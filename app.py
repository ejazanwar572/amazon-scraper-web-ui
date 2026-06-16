import asyncio
import collections
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import psycopg
from fastapi import FastAPI, BackgroundTasks, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from src.config import load_config
from main import run_scrape_keywords, scrape_progress

# Configure logging capture handler
class DequeHandler(logging.Handler):
    """Custom logging handler to keep a rolling buffer of logs for the UI."""
    def __init__(self, deque_obj: collections.deque) -> None:
        super().__init__()
        self.deque = deque_obj

    def emit(self, record: logging.LogRecord) -> None:
        # Ignore debugging noise in the UI to keep it readable
        if record.levelno < logging.INFO:
            return
        log_entry = self.format(record)
        self.deque.append(log_entry)

# Global rolling log buffer (last 150 log entries)
log_buffer = collections.deque(maxlen=150)
log_handler = DequeHandler(log_buffer)
log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))

# Add handler to root logger
root_logger = logging.getLogger()
root_logger.addHandler(log_handler)
logging.getLogger("amzscraper").addHandler(log_handler)

# Global dict to store the summary metrics of the last completed scrape run
last_scrape = {
    "keyword": "",
    "marketplace": "",
    "max_pages": 0,
    "total_extracted_asins": 0,
    "products_saved": 0,
    "failed_runs": 0,
    "avg_products_per_page": 0.0,
    "duration_seconds": 0.0,
    "timestamp": "",
    "success_rate": 0.0,
}

app = FastAPI(title="Amazon Scraper Dashboard API")

# Configuration
PG_DSN = "postgresql://localhost:5432/amazon_scraper"
scraping_lock = asyncio.Lock()

# Models
class ScrapeRequest(BaseModel):
    keyword: str = Field(..., min_length=1, max_length=1000)
    max_pages: int = Field(1, ge=1, le=10)
    marketplace: str = Field("in", min_length=2, max_length=10)

# Database helper
async def get_db_conn():
    return await psycopg.AsyncConnection.connect(PG_DSN)

# Background worker
async def run_scraper_task(keyword: str, max_pages: int, marketplace: str):
    async with scraping_lock:
        log_buffer.clear()
        logging.info("Starting background scraping task for keyword: '%s' (max_pages=%d, marketplace=%s)", keyword, max_pages, marketplace)
        
        start_time = time.monotonic()
        try:
            config = load_config()
            config.scraping.marketplace = marketplace
            config.storage.backend = "postgresql"
            config.storage.pg_dsn = PG_DSN
            config.monitoring.log_level = "DEBUG"
            
            # Setup metadata in progress tracker
            scrape_progress["keyword"] = keyword
            
            # Split keyword if it is comma-separated
            keywords_list = [k.strip() for k in keyword.split(",") if k.strip()]
            
            # Execute scraper orchestrator
            await run_scrape_keywords(config, keywords_list, max_pages=max_pages)
            logging.info("Background scraping task finished successfully!")
            
            # Capture metrics on success
            duration = time.monotonic() - start_time
            total = scrape_progress.get("total", 0)
            saved = scrape_progress.get("saved", 0)
            failed = scrape_progress.get("failed", 0)
            success_rate = (saved / total * 100) if total > 0 else 100.0
            avg_per_page = round(total / max_pages, 1) if max_pages > 0 else 0.0
            
            last_scrape.update({
                "keyword": keyword,
                "marketplace": marketplace,
                "max_pages": max_pages,
                "total_extracted_asins": total,
                "products_saved": saved,
                "failed_runs": failed,
                "avg_products_per_page": avg_per_page,
                "duration_seconds": round(duration, 1),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "success_rate": round(success_rate, 1)
            })
        except Exception as e:
            logging.error("Exception occurred during background scraping: %s", e, exc_info=True)
            # Capture metrics on failure
            duration = time.monotonic() - start_time
            total = scrape_progress.get("total", 0)
            saved = scrape_progress.get("saved", 0)
            failed = scrape_progress.get("failed", 0)
            success_rate = (saved / total * 100) if total > 0 else 0.0
            avg_per_page = round(total / max_pages, 1) if max_pages > 0 else 0.0
            
            last_scrape.update({
                "keyword": keyword,
                "marketplace": marketplace,
                "max_pages": max_pages,
                "total_extracted_asins": total,
                "products_saved": saved,
                "failed_runs": failed,
                "avg_products_per_page": avg_per_page,
                "duration_seconds": round(duration, 1),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "success_rate": round(success_rate, 1)
            })
        finally:
            scrape_progress["active"] = False

async def run_price_check_task():
    async with scraping_lock:
        log_buffer.clear()
        logging.info("Starting background price refresh task for all stored products...")
        start_time = time.monotonic()
        try:
            config = load_config()
            config.storage.backend = "postgresql"
            config.storage.pg_dsn = PG_DSN
            config.monitoring.log_level = "DEBUG"
            
            # Fetch all product URLs from the database
            async with await get_db_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT url FROM products;")
                    rows = await cur.fetchall()
                    urls = [row[0] for row in rows]
            
            if not urls:
                logging.info("No products found in database to check.")
                return
            
            logging.info("Found %d products to check. Starting scrape...", len(urls))
            
            # Setup metadata in progress tracker
            scrape_progress["keyword"] = "Price Check (All DB Products)"
            
            # Execute core scrape loop
            from main import run_scrape
            await run_scrape(config, urls)
            
            # Calculate metrics
            duration = time.monotonic() - start_time
            total = scrape_progress.get("total", 0)
            saved = scrape_progress.get("saved", 0)
            failed = scrape_progress.get("failed", 0)
            success_rate = (saved / total * 100) if total > 0 else 100.0
            
            last_scrape.update({
                "keyword": "Price Check (All DB Products)",
                "marketplace": "All",
                "max_pages": 0,
                "total_extracted_asins": total,
                "products_saved": saved,
                "failed_runs": failed,
                "avg_products_per_page": 0.0,
                "duration_seconds": round(duration, 1),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "success_rate": round(success_rate, 1)
            })
            logging.info("Background price refresh task finished successfully!")
        except Exception as e:
            logging.error("Exception occurred during background price check: %s", e, exc_info=True)
            duration = time.monotonic() - start_time
            total = scrape_progress.get("total", 0)
            saved = scrape_progress.get("saved", 0)
            failed = scrape_progress.get("failed", 0)
            success_rate = (saved / total * 100) if total > 0 else 0.0
            
            last_scrape.update({
                "keyword": "Price Check (All DB Products)",
                "marketplace": "All",
                "max_pages": 0,
                "total_extracted_asins": total,
                "products_saved": saved,
                "failed_runs": failed,
                "avg_products_per_page": 0.0,
                "duration_seconds": round(duration, 1),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "success_rate": round(success_rate, 1)
            })
        finally:
            scrape_progress["active"] = False

# Static File serving (HTML, CSS, JS)
static_path = Path(__file__).parent / "static"
static_path.mkdir(exist_ok=True)

@app.post("/api/scrape")
async def start_scrape(request: ScrapeRequest, background_tasks: BackgroundTasks):
    if scraping_lock.locked() or scrape_progress.get("active"):
        raise HTTPException(status_code=400, detail="A scraping task is already running. Please wait for it to finish.")
    
    background_tasks.add_task(
        run_scraper_task,
        keyword=request.keyword,
        max_pages=request.max_pages,
        marketplace=request.marketplace
    )
    return {"status": "started", "message": f"Scraping started for '{request.keyword}'"}

@app.post("/api/check-prices")
async def check_prices(background_tasks: BackgroundTasks):
    if scraping_lock.locked() or scrape_progress.get("active"):
        raise HTTPException(status_code=400, detail="A scraping task is already running. Please wait for it to finish.")
    
    background_tasks.add_task(run_price_check_task)
    return {"status": "started", "message": "Background price refresh started for all products in DB"}

@app.get("/api/price-alerts")
async def get_price_alerts(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    search: Optional[str] = None,
    min_change: float = Query(30.0, ge=0.0, le=100.0)
):
    offset = (page - 1) * limit
    
    where_clauses = ["ip.initial_price > 0", "ABS((p.price - ip.initial_price) / ip.initial_price * 100) >= %s"]
    params = [min_change]
    
    if search:
        where_clauses.append("(p.title ILIKE %s OR p.asin ILIKE %s OR p.brand ILIKE %s)")
        search_param = f"%{search}%"
        params.extend([search_param, search_param, search_param])
        
    where_sql = f"WHERE { ' AND '.join(where_clauses) }" if where_clauses else ""
    
    alerts_query = f"""
        WITH first_history_ids AS (
            SELECT MIN(id) AS first_id
            FROM price_history
            GROUP BY asin, marketplace
        ),
        initial_prices AS (
            SELECT asin, marketplace, price AS initial_price
            FROM price_history
            WHERE id IN (SELECT first_id FROM first_history_ids)
        )
        SELECT 
            p.asin, p.marketplace, p.title, p.price, p.currency, p.rating, p.review_count,
            p.bsr, p.availability, p.seller, p.brand, p.category, p.image_url, p.url,
            p.scraped_at, p.specification, ip.initial_price,
            ((p.price - ip.initial_price) / ip.initial_price * 100) AS change_percent
        FROM products p
        JOIN initial_prices ip ON p.asin = ip.asin AND p.marketplace = ip.marketplace
        {where_sql}
        ORDER BY ABS((p.price - ip.initial_price) / ip.initial_price * 100) DESC
        LIMIT %s OFFSET %s
    """
    
    count_query = f"""
        WITH first_history_ids AS (
            SELECT MIN(id) AS first_id
            FROM price_history
            GROUP BY asin, marketplace
        ),
        initial_prices AS (
            SELECT asin, marketplace, price AS initial_price
            FROM price_history
            WHERE id IN (SELECT first_id FROM first_history_ids)
        )
        SELECT COUNT(*)
        FROM products p
        JOIN initial_prices ip ON p.asin = ip.asin AND p.marketplace = ip.marketplace
        {where_sql}
    """
    
    try:
        async with await get_db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(count_query, params)
                row = await cur.fetchone()
                total_records = row[0] if row else 0
                
            async with conn.cursor() as cur:
                await cur.execute(alerts_query, params + [limit, offset])
                rows = await cur.fetchall()
                
            alerts = []
            for row in rows:
                alerts.append({
                    "asin": row[0],
                    "marketplace": row[1],
                    "title": row[2],
                    "price": float(row[3]) if row[3] is not None else None,
                    "currency": row[4],
                    "rating": float(row[5]) if row[5] is not None else None,
                    "review_count": row[6],
                    "bsr": row[7],
                    "availability": row[8],
                    "seller": row[9],
                    "brand": row[10],
                    "category": row[11],
                    "image_url": row[12],
                    "url": row[13],
                    "scraped_at": row[14].isoformat() if row[14] else None,
                    "specification": row[15],
                    "initial_price": float(row[16]) if row[16] is not None else None,
                    "change_percent": float(row[17]) if row[17] is not None else None
                })
                
            total_pages = math.ceil(total_records / limit)
            
            return {
                "alerts": alerts,
                "pagination": {
                    "total_records": total_records,
                    "page": page,
                    "limit": limit,
                    "total_pages": total_pages
                }
            }
    except Exception as e:
        logging.error("Database query failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

@app.post("/api/logs/clear")
async def clear_logs():
    log_buffer.clear()
    return {"status": "success", "message": "Log buffer cleared"}

@app.get("/api/status")
async def get_status():
    return {
        "active": scrape_progress.get("active", False),
        "keyword": scrape_progress.get("keyword", ""),
        "total": scrape_progress.get("total", 0),
        "done": scrape_progress.get("done", 0),
        "failed": scrape_progress.get("failed", 0),
        "saved": scrape_progress.get("saved", 0),
        "current_asin": scrape_progress.get("current_asin", ""),
        "logs": list(log_buffer),
        "last_scrape": last_scrape
    }

@app.get("/api/products")
async def get_products(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    search: Optional[str] = None,
    brand: Optional[str] = None,
    seller: Optional[str] = None,
    sort_by: str = Query("scraped_at", pattern="^(asin|price|rating|review_count|bsr|brand|seller|scraped_at)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$")
):
    offset = (page - 1) * limit
    
    where_clauses = []
    params = []
    
    if search:
        where_clauses.append("(title ILIKE %s OR asin ILIKE %s OR brand ILIKE %s)")
        search_param = f"%{search}%"
        params.extend([search_param, search_param, search_param])
        
    if brand:
        where_clauses.append("brand = %s")
        params.append(brand)
        
    if seller:
        where_clauses.append("seller = %s")
        params.append(seller)
        
    where_sql = f"WHERE { ' AND '.join(where_clauses) }" if where_clauses else ""
    
    products_query = f"""
        SELECT asin, marketplace, title, price, currency, rating, review_count,
               bsr, availability, seller, brand, category, image_url, url, scraped_at, specification
        FROM products
        {where_sql}
        ORDER BY {sort_by} {sort_order}
        LIMIT %s OFFSET %s
    """
    
    count_query = f"""
        SELECT COUNT(*) FROM products {where_sql}
    """
    
    try:
        async with await get_db_conn() as conn:
            # Fetch total count
            async with conn.cursor() as cur:
                await cur.execute(count_query, params)
                row = await cur.fetchone()
                total_records = row[0] if row else 0
                
            # Fetch products page
            async with conn.cursor() as cur:
                await cur.execute(products_query, params + [limit, offset])
                rows = await cur.fetchall()
                
            products = []
            for row in rows:
                products.append({
                    "asin": row[0],
                    "marketplace": row[1],
                    "title": row[2],
                    "price": float(row[3]) if row[3] is not None else None,
                    "currency": row[4],
                    "rating": float(row[5]) if row[5] is not None else None,
                    "review_count": row[6],
                    "bsr": row[7],
                    "availability": row[8],
                    "seller": row[9],
                    "brand": row[10],
                    "category": row[11],
                    "image_url": row[12],
                    "url": row[13],
                    "scraped_at": row[14].isoformat() if row[14] else None,
                    "specification": row[15]
                })
                
            total_pages = math.ceil(total_records / limit)
            
            return {
                "products": products,
                "pagination": {
                    "total_records": total_records,
                    "page": page,
                    "limit": limit,
                    "total_pages": total_pages
                }
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

@app.get("/api/stats")
async def get_stats():
    query_total = "SELECT COUNT(*) FROM products"
    query_avg_price = "SELECT AVG(price) FROM products WHERE price IS NOT NULL"
    query_unique_brands = "SELECT COUNT(DISTINCT brand) FROM products WHERE brand IS NOT NULL"
    query_unique_sellers = "SELECT COUNT(DISTINCT seller) FROM products WHERE seller IS NOT NULL"
    
    # Top 5 Brands query
    query_top_brands = """
        SELECT brand, COUNT(*) as count 
        FROM products 
        WHERE brand IS NOT NULL 
        GROUP BY brand 
        ORDER BY count DESC 
        LIMIT 5
    """
    
    # Marketplace distribution
    query_marketplaces = """
        SELECT marketplace, COUNT(*) 
        FROM products 
        GROUP BY marketplace
    """

    try:
        async with await get_db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query_total)
                total = (await cur.fetchone())[0]
                
                await cur.execute(query_avg_price)
                avg_price = float((await cur.fetchone())[0] or 0.0)
                
                await cur.execute(query_unique_brands)
                brands = (await cur.fetchone())[0]
                
                await cur.execute(query_unique_sellers)
                sellers = (await cur.fetchone())[0]
                
                await cur.execute(query_top_brands)
                top_brands = [{"brand": r[0], "count": r[1]} for r in await cur.fetchall()]
                
                await cur.execute(query_marketplaces)
                marketplaces = {r[0]: r[1] for r in await cur.fetchall()}
                
            return {
                "total_products": total,
                "avg_price": round(avg_price, 2),
                "unique_brands": brands,
                "unique_sellers": sellers,
                "top_brands": top_brands,
                "marketplaces": marketplaces
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

# Serve index.html directly on root
@app.get("/")
async def read_index():
    index_file = static_path / "index.html"
    if not index_file.is_file():
        # Return a simple placeholder if static files aren't created yet
        return JSONResponse({"status": "error", "message": "HTML interface file not created yet."})
    return FileResponse(index_file)

# Mount static files handler
app.mount("/static", StaticFiles(directory=str(static_path)), name="static")
