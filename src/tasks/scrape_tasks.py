"""Celery task for scraping with concurrency control"""

from celery import Task
from loguru import logger

from celery_app import app
from ..models.unified import ScrapeResponse, ScrapingMethod
from ..db.database import Database
from ..scrapers.base import scrape_with_fallback
from ..services.content_cleaner import get_content_cleaner


class ScrapeTask(Task):
    """Custom Task class with lazy initialization of heavy resources"""

    _db = None
    _cleaner = None

    @property
    def db(self) -> Database:
        """Lazy init database connection"""
        if self._db is None:
            self._db = Database()
        return self._db

    @property
    def cleaner(self):
        """Lazy init content cleaner"""
        if self._cleaner is None:
            self._cleaner = get_content_cleaner()
        return self._cleaner

    def after_return(self, *args, **kwargs):
        """Cleanup after task completes"""
        # Keep resources alive for reuse in worker process
        pass


@app.task(bind=True, base=ScrapeTask, name="scrape_task")
def scrape_task(self, url: str, force_method: str | None = None) -> dict:
    """
    Scrape a URL with automatic method routing

    This runs in the Celery worker with controlled concurrency.
    Returns dict that can be serialized to JSON for Redis.
    """
    import asyncio

    # Run the async scrape in a new event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            scrape_with_fallback(
                url=url,
                cleaner=self.cleaner,
                db=self.db,
                force_method=force_method
            )
        )
        return result
    finally:
        loop.close()
