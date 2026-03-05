"""Unified search service - SearXNG multi-engine"""

import asyncio
import httpx
from typing import List, Dict, Set
from loguru import logger
from urllib.parse import urlparse

from ..models.unified import SearchResult, CombinedSearchResponse
from ..db.database import Database
from ..constants import DEFAULT_SEARCH_ENGINES, HTTP_REQUEST_TIMEOUT


class UnifiedSearchService:
    """SearXNG multi-engine search with unified output format"""

    def __init__(
        self,
        searxng_url: str = "http://searxng:8080",
    ):
        self.searxng_url = searxng_url
        self.client = httpx.AsyncClient(timeout=HTTP_REQUEST_TIMEOUT, follow_redirects=True)
        self._db = None

    @property
    def db(self) -> Database:
        """Get database instance (lazy initialization)"""
        if self._db is None:
            from ..db.database import get_db

            # Get or create the singleton DB
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            self._db = loop.run_until_complete(get_db())
        return self._db

    async def search(
        self,
        query: str,
        pages: int = 10,
        exclude_blacklist: bool = True
    ) -> CombinedSearchResponse:
        """
        Search SearXNG with pagination

        Args:
            query: Search query
            pages: Number of pages to fetch (1-10)
            exclude_blacklist: Filter out blacklisted domains

        Returns:
            CombinedSearchResponse with unified format
        """
        start_time = asyncio.get_event_loop().time()

        # Get blacklist if needed
        blacklisted_domains: Set[str] = set()
        if exclude_blacklist:
            blacklisted_domains = await self.db.get_blacklisted_domains()
            logger.info(f"Blacklisted domains: {blacklisted_domains}")

        results = await self._search_searxng(query, pages, blacklisted_domains)

        # Deduplicate by URL
        seen_urls = set()
        unique_results = []
        for r in results:
            if r.url not in seen_urls:
                seen_urls.add(r.url)
                unique_results.append(r)

        search_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000

        return CombinedSearchResponse(
            query=query,
            total_results=len(unique_results),
            pages_scraped=pages,
            results=unique_results,
            engines={"searxng": len(results)},
            search_time_ms=round(search_time_ms, 2)
        )

    async def _search_searxng(self, query: str, pages: int, blacklisted_domains: Set[str] = None) -> List[SearchResult]:
        """Search SearXNG with proper pagination"""
        if blacklisted_domains is None:
            blacklisted_domains = set()

        results = []

        for page in range(1, pages + 1):
            params = {
                "q": query,
                "format": "json",
                "pageno": page,  # Proper pagination
                "engines": ",".join(DEFAULT_SEARCH_ENGINES)
            }

            try:
                response = await self.client.get(
                    f"{self.searxng_url}/search",
                    params=params
                )
                response.raise_for_status()
                data = response.json()

                for item in data.get("results", []):
                    url = item.get("url", "")
                    domain = urlparse(url).netloc or urlparse(url).path

                    # Skip blacklisted domains
                    if domain in blacklisted_domains:
                        logger.debug(f"Skipping blacklisted domain: {domain}")
                        continue

                    results.append(SearchResult(
                        title=self._clean_text(item.get("title", "")),
                        url=url,
                        snippet=self._clean_text(item.get("content", "")),
                        domain=domain
                    ))

            except httpx.HTTPStatusError as e:
                logger.warning(f"SearXNG HTTP error on page {page}: {e.response.status_code}")
            except httpx.RequestError as e:
                logger.warning(f"SearXNG request error on page {page}: {e}")
            except Exception as e:
                logger.warning(f"SearXNG error on page {page}: {e}")

        return results

    def _clean_text(self, text: str) -> str:
        """Clean and normalize text"""
        if not text:
            return ""

        # Remove extra whitespace
        text = " ".join(text.split())

        # Remove common artifacts
        artifacts = [
            "\u2026",  # ellipsis
            "\u00a0",  # non-breaking space
            "\u200b",  # zero-width space
        ]
        for artifact in artifacts:
            text = text.replace(artifact, " ")

        return text.strip()

    async def close(self):
        await self.client.aclose()


# Singleton
_search_service: UnifiedSearchService = None


def get_search_service() -> UnifiedSearchService:
    global _search_service
    if _search_service is None:
        _search_service = UnifiedSearchService()
    return _search_service
