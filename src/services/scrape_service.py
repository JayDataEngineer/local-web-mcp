"""Unified scraping service with method routing, caching, and consistent output

Flow:
1. Check cache -> return cached result if available
2. Rate limiting (max 3 concurrent per domain)
3. Check blacklist -> reject if blacklisted
4. Reddit -> special JSON API handler
5. Check database -> use learned preference
6. Try Crawl4AI (fast)
7. Fallback to Selenium (stealth)
8. Blacklist if both fail
"""

from loguru import logger

from ..models.unified import ScrapeRequest, ScrapeResponse, ScrapingMethod
from ..services.content_cleaner import get_content_cleaner
from ..services.rate_limit_service import get_rate_limit_service
from ..scrapers.base import scrape_with_fallback
from ..utils import extract_domain, create_singleton_factory


class UnifiedScrapeService:
    """Unified scraping with consistent output format and caching"""

    def __init__(self, db=None, cleaner=None):
        self._db = db
        self._cleaner = cleaner
        self._db_instance = None
        self._cache = None

    async def _get_db(self):
        if self._db is not None:
            return self._db
        if self._db_instance is None:
            from ..db.database import get_db
            self._db_instance = await get_db()
        return self._db_instance

    async def _get_cache(self):
        if self._cache is None:
            from ..services.cache_service import get_cache_service
            self._cache = await get_cache_service()
        return self._cache

    @property
    def cleaner(self):
        if self._cleaner is None:
            self._cleaner = get_content_cleaner()
        return self._cleaner

    async def scrape(self, request: ScrapeRequest) -> ScrapeResponse:
        """Main scrape entry point with routing, caching, and rate limiting"""
        cache = await self._get_cache()

        # Check cache
        cached_result = await cache.get_scrape(request.url)
        if cached_result:
            logger.info(f"Cache HIT for scrape: {request.url}")
            response = self._dict_to_response(cached_result)
            response.cached = True
            return response

        logger.info(f"Cache MISS for scrape: {request.url}")

        db = await self._get_db()
        domain = extract_domain(request.url)
        rate_limiter = get_rate_limit_service()

        # Try with rate limiting
        try:
            if rate_limiter._redis is None:
                await rate_limiter._get_redis()

            acquired = await rate_limiter.acquire(domain)
            if not acquired:
                return ScrapeResponse(
                    success=False,
                    url=request.url,
                    domain=domain,
                    method_used=ScrapingMethod.CRAWL4AI,
                    error="Rate limit: Too many concurrent requests to this domain.",
                )

            result_dict = await scrape_with_fallback(
                url=request.url,
                cleaner=self.cleaner,
                db=db,
                force_method=request.force_method.value if request.force_method else None,
                css_selector=request.css_selector
            )

            response = self._dict_to_response(result_dict)
            response.cached = False

            if response.success:
                await cache.set_scrape(request.url, result_dict)

            return response

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

    async def close(self):
        if self._cache:
            await self._cache.close()


# Singleton factory
get_scrape_service = create_singleton_factory(UnifiedScrapeService, "get_scrape_service")
