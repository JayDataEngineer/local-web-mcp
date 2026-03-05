"""MCP Server - FastAPI with Unified Search & Scrape"""

import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from .models.unified import (
    SearchResult,
    CombinedSearchResponse,
    ScrapeRequest,
    ScrapeResponse,
    CheckRequest,
    CheckResponse
)
from .services.search_service import get_search_service
from .services.scrape_service import get_scrape_service
from .db.database import get_db

# Celery for scrape task queuing
try:
    from celery.result import AsyncResult
    from .celery_app import app as celery_app
    CELERY_AVAILABLE = True
    logger.info("Celery enabled - scrape tasks will be queued")
except ImportError:
    CELERY_AVAILABLE = False
    logger.warning("Celery not available - using direct scraping")
    celery_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown"""
    logger.info("Starting MCP Server...")
    # Optional database initialization
    try:
        db = await get_db()
        logger.info("Database ready")
    except Exception as e:
        logger.warning(f"Database not available (this is OK for local development): {e}")

    yield
    logger.info("Shutting down...")
    await get_search_service().close()
    await get_scrape_service().close()


app = FastAPI(
    title="MCP Research Server",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============ ENDPOINTS ============

@app.get("/")
async def root():
    """Health check"""
    return {"status": "ok", "service": "mcp-server"}


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "healthy", "service": "mcp-server"}


@app.post("/search", response_model=CombinedSearchResponse)
async def search(
    query: str,
    pages: int = 10,
    exclude_blacklist: bool = True  # Default: exclude blacklisted domains
):
    """
    Multi-page search using SearXNG

    - Fetches multiple pages of results
    - Combines and deduplicates results
    - Returns unified JSON format
    - By default excludes blacklisted domains
    """
    search_svc = get_search_service()

    try:
        result = await search_svc.search(
            query=query,
            pages=pages,
            exclude_blacklist=exclude_blacklist
        )
        return result

    except Exception as e:
        logger.error(f"Search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/scrape", response_model=ScrapeResponse)
async def scrape(request: ScrapeRequest):
    """
    Scrape a URL with automatic routing

    Flow:
    1. Check blacklist → reject if blacklisted
    2. Check database → use known preferred method
    3. Try Crawl4AI (fast)
    4. Fallback to Selenium (stealth)
    5. Blacklist if both fail

    Special handlers:
    - Reddit → Reddit API → JSON → Markdown

    Returns:
    Unified ScrapeResponse with content and optional AI summary

    To enable AI summary, set include_summary=true in the request.

    Note: When Celery is available, the task is queued to a worker
    with controlled concurrency (max 10 browsers). The endpoint waits
    for the result to maintain MCP compatibility.
    """
    # Use Celery if available for concurrency control
    if CELERY_AVAILABLE:
        try:
            from .celery_app import app as celery_app
            # Send task to Celery by name
            task = celery_app.send_task(
                'scrape_task',
                args=[request.url],
                kwargs={
                    'force_method': request.force_method.value if request.force_method else None
                }
            )

            # Block until result is ready (keeps MCP contract)
            # Timeout after 5 minutes
            result_dict = task.get(timeout=300)

            # Convert dict back to ScrapeResponse
            return ScrapeResponse(**result_dict)

        except Exception as e:
            logger.error(f"Celery scrape failed for {request.url}: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    else:
        # Fallback to direct scraping (no concurrency control)
        scrape_svc = get_scrape_service()
        try:
            result = await scrape_svc.scrape(request)
            return result
        except Exception as e:
            logger.error(f"Scrape failed for {request.url}: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/clean")
async def clean_database():
    """
    Clean the domain tracking database

    Clears all records (methods, blacklist, etc.)
    Useful for starting fresh or resetting learned behavior
    """
    db = await get_db()

    try:
        count = await db.clean()
        return {
            "status": "success",
            "records_removed": count
        }

    except Exception as e:
        logger.error(f"Clean failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/check", response_model=CheckResponse)
async def check_database(request: CheckRequest):
    """
    Verify URLs in database are still valid

    For each tracked domain:
    - Attempt to scrape using preferred method
    - Update success/failure timestamps
    - Move to blacklist if threshold exceeded

    This validates that the database accurately reflects reality.
    """
    db = await get_db()

    try:
        stats = await db.check_urls(
            max_urls=request.max_urls,
            threshold=request.blacklist_threshold
        )
        return CheckResponse(**stats)

    except Exception as e:
        logger.error(f"Check failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/domains")
async def list_domains():
    """List all tracked domains with their methods"""
    db = await get_db()
    domains = await db.get_all_domains()
    return {
        "total": len(domains),
        "domains": domains
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
