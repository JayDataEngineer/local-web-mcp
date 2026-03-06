"""Unified search service - SearXNG multi-engine"""

import httpx
from loguru import logger
from urllib.parse import urlparse

from ..models.unified import SearchResult, CombinedSearchResponse
from ..constants import DEFAULT_SEARCH_ENGINES, HTTP_REQUEST_TIMEOUT


class UnifiedSearchService:
    """SearXNG multi-engine search with unified output format"""

    def __init__(self, searxng_url: str = "http://searxng:8080"):
        self.searxng_url = searxng_url
        self.client = httpx.AsyncClient(timeout=HTTP_REQUEST_TIMEOUT, follow_redirects=True)
        self._db = None

    async def _get_db(self):
        """Lazy database initialization"""
        if self._db is None:
            from ..db.database import get_db
            self._db = await get_db()
        return self._db

    async def search(
        self,
        query: str,
        pages: int = 10,
        exclude_blacklist: bool = True
    ) -> CombinedSearchResponse:
        """Search SearXNG with pagination and blacklist filtering"""
        import asyncio

        start_time = asyncio.get_event_loop().time()

        # Get blacklist if needed
        blacklisted = set()
        if exclude_blacklist:
            db = await self._get_db()
            blacklisted = await db.get_blacklisted_domains()
            logger.info(f"Blacklisted domains: {blacklisted}")

        results = await self._fetch_results(query, pages, blacklisted)
        unique_results = self._deduplicate(results)

        search_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000

        return CombinedSearchResponse(
            query=query,
            total_results=len(unique_results),
            pages_scraped=pages,
            results=unique_results,
            engines={"searxng": len(results)},
            search_time_ms=round(search_time_ms, 2)
        )

    async def _fetch_results(self, query: str, pages: int, blacklisted: set[str]) -> list[SearchResult]:
        """Fetch results from SearXNG with pagination"""
        results = []

        for page in range(1, pages + 1):
            params = {
                "q": query,
                "format": "json",
                "pageno": page,
                "engines": ",".join(DEFAULT_SEARCH_ENGINES)
            }

            try:
                response = await self.client.get(f"{self.searxng_url}/search", params=params)
                response.raise_for_status()
                data = response.json()

                for item in data.get("results", []):
                    url = item.get("url", "")
                    domain = urlparse(url).netloc or urlparse(url).path

                    if domain in blacklisted:
                        continue

                    results.append(SearchResult(
                        title=self._clean_text(item.get("title", "")),
                        url=url,
                        snippet=self._clean_text(item.get("content", "")),
                        domain=domain
                    ))

            except httpx.HTTPStatusError as e:
                logger.warning(f"SearXNG HTTP error on page {page}: {e.response.status_code}")
            except Exception as e:
                logger.warning(f"SearXNG error on page {page}: {e}")

        return results

    def _deduplicate(self, results: list[SearchResult]) -> list[SearchResult]:
        """Remove duplicate results by URL"""
        seen = set()
        unique = []
        for r in results:
            if r.url not in seen:
                seen.add(r.url)
                unique.append(r)
        return unique

    def _clean_text(self, text: str) -> str:
        """Clean and normalize text"""
        if not text:
            return ""

        # Remove extra whitespace
        text = " ".join(text.split())

        # Remove common artifacts
        for artifact in ["\u2026", "\u00a0", "\u200b"]:
            text = text.replace(artifact, " ")

        return text.strip()

    async def close(self):
        await self.client.aclose()


# Singleton factory
_search_service: UnifiedSearchService | None = None


def get_search_service() -> UnifiedSearchService:
    global _search_service
    if _search_service is None:
        _search_service = UnifiedSearchService()
    return _search_service
