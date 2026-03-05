"""Unified scraping service with method routing and consistent output"""

from typing import Optional
from loguru import logger
from urllib.parse import urlparse

from ..models.unified import (
    ScrapeRequest,
    ScrapeResponse,
    ScrapingMethod,
)
from ..db.database import Database
from ..services.content_cleaner import get_content_cleaner
from ..scrapers.base import scrape_with_fallback


class UnifiedScrapeService:
    """
    Unified scraping with consistent output format

    All scrapers run in the same Python process:
    - Crawl4AI (fast, Playwright-based)
    - SeleniumBase (stealth fallback, Pure CDP mode)
    - Reddit JSON API (special handler)

    Routing:
    1. Check blacklist → reject if blacklisted
    2. Reddit → special JSON API handler
    3. Check database → use learned preference if available
    4. Try Crawl4AI first (fast)
    5. Fallback to Selenium (stealth)
    6. Blacklist if both fail
    """

    def __init__(self, db: Database = None, cleaner=None):
        """
        Initialize scrape service

        Args:
            db: Database instance (optional, will use singleton if not provided)
            cleaner: ContentCleaner instance (optional, will use singleton if not provided)
        """
        self._db = db
        self._cleaner = cleaner

    @property
    def db(self) -> Database:
        """Get database instance (lazy initialization)"""
        if self._db is None:
            from ..db.database import get_db
            import asyncio

            # Get or create the singleton DB
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            self._db = loop.run_until_complete(get_db())
        return self._db

    @property
    def cleaner(self):
        """Get content cleaner instance (lazy initialization)"""
        if self._cleaner is None:
            self._cleaner = get_content_cleaner()
        return self._cleaner

    async def scrape(self, request: ScrapeRequest) -> ScrapeResponse:
        """Main scrape entry point with routing"""
        domain = urlparse(request.url).netloc

        # Use shared scraping logic
        result_dict = await scrape_with_fallback(
            url=request.url,
            cleaner=self.cleaner,
            db=self.db,
            force_method=request.force_method.value if request.force_method else None
        )

        # Convert dict to ScrapeResponse
        return self._dict_to_response(result_dict)

    def _dict_to_response(self, data: dict) -> ScrapeResponse:
        """Convert dict result to ScrapeResponse"""
        # Convert method string to ScrapingMethod enum
        method_str = data.get("method_used", "crawl4ai")
        try:
            method = ScrapingMethod(method_str)
        except ValueError:
            method = ScrapingMethod.CRAWL4AI

        return ScrapeResponse(
            success=data.get("success", False),
            url=data.get("url", ""),
            domain=data.get("domain", ""),
            method_used=method,
            title=data.get("title"),
            content=data.get("content"),
            metadata=data.get("metadata", {}),
            error=data.get("error"),
        )

    async def close(self):
        """Cleanup resources"""
        pass


# Singleton for backward compatibility
_scrape_service: UnifiedScrapeService = None


def get_scrape_service() -> UnifiedScrapeService:
    """Get scrape service singleton"""
    global _scrape_service
    if _scrape_service is None:
        _scrape_service = UnifiedScrapeService()
    return _scrape_service
