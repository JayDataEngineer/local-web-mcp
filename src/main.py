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
    CheckResponse,
    TaskResponse,
    TaskStatusResponse,
    TaskStatus,
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
    return {"status": "ok", "service": "mcp-server", "celery_enabled": CELERY_AVAILABLE}


@app.get("/health")
async def health():
    """Detailed health check for Docker"""
    from datetime import datetime
    return {
        "status": "healthy",
        "service": "mcp-server",
        "celery_enabled": CELERY_AVAILABLE,
        "timestamp": datetime.now().isoformat()
    }


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


@app.post("/scrape", response_model=TaskResponse)
async def scrape(request: ScrapeRequest):
    """
    Submit a scrape job to the queue (non-blocking)

    Returns immediately with a task_id. Poll /status/{task_id} for results.

    Flow:
    1. Check blacklist → reject if blacklisted
    2. Check database → use known preferred method
    3. Queue to Celery worker
    4. Return task_id immediately

    Special handlers:
    - Reddit → Reddit API → JSON → Markdown
    """
    if not CELERY_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Celery not available. Queue processing is disabled."
        )

    try:
        # Send task to Celery by name
        task = celery_app.send_task(
            'scrape_task',
            args=[request.url],
            kwargs={
                'force_method': request.force_method.value if request.force_method else None,
            }
        )

        return TaskResponse(
            task_id=task.id,
            status=TaskStatus.PENDING,
            message=f"Scrape job queued for {request.url}. Poll /status/{task.id} for results."
        )

    except Exception as e:
        logger.error(f"Failed to queue scrape for {request.url}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    """
    Poll the status of a scrape task

    Returns:
    - PENDING: Task is waiting in queue
    - STARTED: Task is being processed
    - SUCCESS: Task completed (result included)
    - FAILURE: Task failed (error included)
    """
    if not CELERY_AVAILABLE:
        raise HTTPException(status_code=503, detail="Celery not available")

    try:
        result = AsyncResult(task_id, app=celery_app)

        status_map = {
            "PENDING": TaskStatus.PENDING,
            "STARTED": TaskStatus.STARTED,
            "SUCCESS": TaskStatus.SUCCESS,
            "FAILURE": TaskStatus.FAILURE,
            "REVOKED": TaskStatus.REVOKED,
            "RETRY": TaskStatus.RETRY,
        }

        task_status = status_map.get(result.state, TaskStatus.PENDING)

        response = TaskStatusResponse(
            task_id=task_id,
            status=task_status,
        )

        # Add timing info if available
        if result.info:
            info = result.info if not isinstance(result.info, Exception) else {}
            if isinstance(info, dict):
                response.created_at = info.get("created_at")
                response.started_at = info.get("started_at")
                response.completed_at = info.get("completed_at")

        # Add result if successful
        if result.ready() and result.successful():
            response.result = ScrapeResponse(**result.result)

        # Add error if failed
        if result.ready() and result.failed():
            response.error = str(result.info)

        return response

    except Exception as e:
        logger.error(f"Failed to get status for {task_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/scrape/sync", response_model=ScrapeResponse)
async def scrape_sync(request: ScrapeRequest):
    """
    Synchronous scrape (blocks until complete)

    WARNING: This endpoint blocks for ~5 seconds. Use /scrape + /status/{task_id}
    for production use. This endpoint is mainly for testing/debugging.

    Direct scraping fallback if Celery is not available.
    """
    # If Celery is available, use it but block (old behavior)
    if CELERY_AVAILABLE:
        try:
            task = celery_app.send_task(
                'scrape_task',
                args=[request.url],
                kwargs={
                    'force_method': request.force_method.value if request.force_method else None,
                }
            )

            # Block until result is ready (OLD BEHAVIOR - AVOID IN PRODUCTION)
            result_dict = task.get(timeout=300)
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


@app.post("/cache/clear")
async def clear_cache():
    """
    Clear all cached scrape and search results

    Forces fresh fetches on next requests.
    """
    from .services.cache_service import get_cache_service
    cache = get_cache_service()

    try:
        count = await cache.clear_all()
        return {
            "status": "success",
            "cleared_items": count
        }
    except Exception as e:
        logger.error(f"Cache clear failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/cache/invalidate")
async def invalidate_cache(url: str):
    """
    Invalidate cache for a specific URL

    Forces a fresh scrape on the next request for this URL.
    """
    from .services.cache_service import get_cache_service
    cache = get_cache_service()

    try:
        success = await cache.invalidate_scrape(url)
        return {
            "status": "success" if success else "failed",
            "url": url,
            "invalidated": success
        }
    except Exception as e:
        logger.error(f"Cache invalidation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
