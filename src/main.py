"""MCP Server - FastAPI with Unified Search & Scrape"""

import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
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


@app.post("/scrape")
async def scrape(
    scrape_request: ScrapeRequest,
    http_request: Request  # FastAPI Request for query params
):
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
    - With Celery: {"task_id": "...", "status": "pending"} - poll /status/{task_id} for result
    - Without Celery: Direct ScrapeResponse

    For async/polling mode, use wait=false query param to always get task_id.
    """
    from .models.unified import ScrapeResponse

    # Check if client wants async mode (explicit or default with Celery)
    wait = http_request.query_params.get("wait", "false").lower() == "true"

    if CELERY_AVAILABLE and not wait:
        # Async mode - return task_id immediately
        try:
            from .celery_app import app as celery_app
            task = celery_app.send_task(
                'scrape_task',
                args=[scrape_request.url],
                kwargs={
                    'force_method': scrape_request.force_method.value if scrape_request.force_method else None,
                    'css_selector': scrape_request.css_selector
                }
            )
            return {
                "task_id": task.id,
                "status": "pending",
                "message": "Task queued. Poll /status/" + task.id + " for result."
            }
        except Exception as e:
            logger.error(f"Celery task creation failed for {scrape_request.url}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # Blocking mode (wait=true or no Celery)
    if CELERY_AVAILABLE:
        try:
            from .celery_app import app as celery_app
            task = celery_app.send_task(
                'scrape_task',
                args=[scrape_request.url],
                kwargs={
                    'force_method': scrape_request.force_method.value if scrape_request.force_method else None,
                    'css_selector': scrape_request.css_selector
                }
            )
            # Block until result is ready
            result_dict = task.get(timeout=300)
            return ScrapeResponse(**result_dict)
        except Exception as e:
            logger.error(f"Celery scrape failed for {scrape_request.url}: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    else:
        # Direct scraping
        scrape_svc = get_scrape_service()
        try:
            result = await scrape_svc.scrape(scrape_request)
            return result
        except Exception as e:
            logger.error(f"Scrape failed for {request.url}: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@app.get("/status/{task_id}")
async def get_task_status(task_id: str):
    """
    Check status of an async scrape task

    Returns task status and result if ready.
    Use with /scrape endpoint in async mode.
    """
    if not CELERY_AVAILABLE:
        raise HTTPException(status_code=501, detail="Celery not available")

    try:
        from celery.result import AsyncResult
        from .celery_app import app as celery_app

        task = AsyncResult(task_id, app=celery_app)

        if task.state == 'PENDING':
            return {"task_id": task_id, "status": "pending", "ready": False}
        elif task.state == 'PROGRESS':
            return {"task_id": task_id, "status": "progress", "ready": False, "info": task.info}
        elif task.state == 'SUCCESS':
            return {"task_id": task_id, "status": "complete", "ready": True, "result": task.result}
        else:  # FAILURE
            return {"task_id": task_id, "status": "failed", "ready": True, "error": str(task.info)}

    except Exception as e:
        logger.error(f"Status check failed for {task_id}: {e}")
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
