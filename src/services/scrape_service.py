"""Unified scraping service with method routing and consistent output

Flow:
1. Rate limiting (max 3 concurrent per domain)
2. Check blacklist -> reject if blacklisted
3. Reddit -> special JSON API handler
4. Check database -> use learned preference
5. Try Crawl4AI (fast)
6. Fallback to Selenium (stealth)
7. Blacklist if both fail

Note: Caching is handled by FastMCP's ResponseCachingMiddleware at the framework level.
Note: Rate limiting uses in-memory semaphores (no Redis required).
Note: Domain tracking uses PostgreSQL (shared with Celery workers).
"""

from loguru import logger

from ..models.unified import ScrapeRequest, ScrapeResponse, ScrapingMethod
from ..services.content_cleaner import get_content_cleaner
from ..utils.rate_limiter import get_rate_limiter
from ..scrapers.base import scrape_with_fallback
from ..utils import extract_domain, create_singleton_factory


class UnifiedScrapeService:
    """Unified scraping with consistent output format

    Caching is handled by FastMCP's ResponseCachingMiddleware at the framework level.
    Rate limiting uses in-memory semaphores (no Redis required).
    Domain tracking uses PostgreSQL (shared with Celery workers).
    """

    def __init__(self, db=None, cleaner=None):
        self._db = db
        self._cleaner = cleaner
        self._db_instance = None

    async def _get_db(self):
        if self._db is not None:
            return self._db
        if self._db_instance is None:
            from ..db.database import get_db
            self._db_instance = await get_db()
        return self._db_instance

    @property
    def cleaner(self):
        if self._cleaner is None:
            self._cleaner = get_content_cleaner()
        return self._cleaner

    async def scrape(self, request: ScrapeRequest) -> ScrapeResponse:
        """Main scrape entry point with routing and rate limiting"""
        db = await self._get_db()
        domain = extract_domain(request.url)
        rate_limiter = get_rate_limiter()

        # Try with rate limiting
        acquired = await rate_limiter.acquire(domain)
        if not acquired:
            return ScrapeResponse(
                success=False,
                url=request.url,
                domain=domain,
                method_used=ScrapingMethod.CRAWL4AI,
                error="Rate limit: Too many concurrent requests to this domain.",
            )

        try:
            result_dict = await scrape_with_fallback(
                url=request.url,
                cleaner=self.cleaner,
                db=db,
                force_method=request.force_method.value if request.force_method else None,
                css_selector=request.css_selector,
                text_only=request.text_only
            )

            return self._dict_to_response(result_dict)

        finally:
            await rate_limiter.release(domain)

    def _dict_to_response(self, data: dict) -> ScrapeResponse:
        """Convert dict result to ScrapeResponse"""
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
            summary=data.get("summary"),
            metadata=data.get("metadata", {}),
            error=data.get("error"),
        )


# Singleton factory
get_scrape_service = create_singleton_factory(UnifiedScrapeService, "get_scrape_service")
