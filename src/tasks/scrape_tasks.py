"""Celery task for scraping with concurrency control and caching"""

import asyncio
from celery import Task

from ..celery_app import app
from ..models.unified import ScrapeResponse, ScrapingMethod
from ..db.database import Database
from ..scrapers.base import scrape_with_fallback
from ..services.content_cleaner import get_content_cleaner


class ScrapeTask(Task):
    """Custom Task class with lazy initialization of heavy resources"""

    _db = None
    _db_initialized = False
    _cleaner = None
    _cache = None
    _cache_loop = None

    @property
    def db(self) -> Database:
        """Lazy init database connection"""
        if self._db is None:
            self._db = Database()
            # Initialize the async database connection
            if not self._db_initialized:
                self._run_async(self._db.init())
                self._db_initialized = True
        return self._db

    @property
    def cleaner(self):
        """Lazy init content cleaner"""
        if self._cleaner is None:
            self._cleaner = get_content_cleaner()
        return self._cleaner

    def _run_async(self, coro):
        """Run an async coroutine in the Celery worker context"""
        # Reuse or create event loop for this thread
        if self._cache_loop is None or self._cache_loop.is_closed():
            self._cache_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._cache_loop)
        return self._cache_loop.run_until_complete(coro)

    @property
    def cache(self):
        """Lazy init cache service (sync wrapper)"""
        if self._cache is None:
            try:
                from ..services.cache_service import get_cache_service
                # Properly await the async singleton factory
                self._cache = self._run_async(get_cache_service())
            except Exception as e:
                # Cache service not available
                self._cache = None
        return self._cache

    def after_return(self, *args, **kwargs):
        """Cleanup after task completes"""
        # Keep resources alive for reuse in worker process
        pass


@app.task(bind=True, base=ScrapeTask, name="scrape_task")
def scrape_task(
    self,
    url: str,
    force_method: str | None = None,
    css_selector: str | None = None
) -> dict:
    """
    Scrape a URL with automatic method routing and caching

    This runs in the Celery worker with controlled concurrency.
    Returns dict that can be serialized to JSON for Redis.

    Args:
        url: URL to scrape
        force_method: Force specific scraping method
        css_selector: Optional CSS selector for targeted content extraction
    """
    # Check cache first
    if self.cache is not None:
        try:
            cached_result = self._run_async(self.cache.get_scrape(url))
            if cached_result:
                # Mark as cached and return immediately
                cached_result["cached"] = True
                return cached_result
        except Exception as e:
            # Cache miss or error, continue with scraping
            pass

    # Run the async scrape in a new event loop
    result = self._run_async(
        scrape_with_fallback(
            url=url,
            cleaner=self.cleaner,
            db=self.db,
            force_method=force_method,
            css_selector=css_selector
        )
    )

    # Cache successful results
    if result.get("success") and self.cache is not None:
        try:
            self._run_async(self.cache.set_scrape(url, result))
        except Exception:
            pass  # Cache set failed, but scrape succeeded

    # Mark as not cached (fresh scrape)
    result["cached"] = False
    return result
