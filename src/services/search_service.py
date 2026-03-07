"""Unified search service - SearXNG multi-engine"""

import httpx
import re
from loguru import logger
from urllib.parse import urlparse

from ..models.unified import SearchResult, CombinedSearchResponse
from ..core.constants import DEFAULT_SEARCH_ENGINES, HTTP_REQUEST_TIMEOUT


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
        exclude_blacklist: bool = True,
        top_k: int | None = None,
        rerank: bool = False
    ) -> CombinedSearchResponse:
        """Search SearXNG with pagination, blacklist filtering, and re-ranking

        Args:
            query: Search query
            pages: Number of pages to fetch
            exclude_blacklist: Filter out blacklisted domains
            top_k: Maximum number of results to return (None = all results)
            rerank: Apply flash re-ranking based on query relevance
        """
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

        # Apply flash re-ranking if requested
        if rerank and unique_results:
            unique_results = self._flash_rerank(query, unique_results)
            logger.info(f"Re-ranked {len(unique_results)} results")

        # Apply top_k limit
        if top_k is not None and top_k > 0:
            unique_results = unique_results[:top_k]

        search_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000

        return CombinedSearchResponse(
            query=query,
            total_results=len(unique_results),
            pages_scraped=pages,
            results=unique_results,
            engines={"searxng": len(results)},
            search_time_ms=round(search_time_ms, 2)
        )

    def _flash_rerank(self, query: str, results: list[SearchResult]) -> list[SearchResult]:
        """Flash re-ranking based on query term overlap and position

        Prioritizes results where:
        - Query terms appear in title (higher weight)
        - Query terms appear early in title/snippet
        - More query terms matched

        Args:
            query: Original search query
            results: List of search results to re-rank

        Returns:
            Re-ranked list of search results
        """
        # Extract query terms (lowercase, remove common words)
        query_lower = query.lower()
        stop_words = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
                     "of", "with", "by", "from", "as", "is", "was", "are", "be", "been",
                     "this", "that", "these", "those", "it", "its", "what", "which", "who"}

        query_terms = [w for w in re.findall(r"\b\w+\b", query_lower) if w not in stop_words and len(w) > 1]
        if not query_terms:
            return results

        scored_results = []
        for result in results:
            score = 0.0
            title_lower = result.title.lower()
            snippet_lower = result.snippet.lower()

            # Score based on term matches in title (highest weight)
            for term in query_terms:
                # Exact phrase in title - big bonus
                if term in title_lower:
                    # Position bonus: earlier in title = higher score
                    first_pos = title_lower.find(term)
                    position_bonus = 1.0 - (first_pos / len(title_lower)) * 0.5
                    score += 2.0 * position_bonus

                # Exact phrase in snippet
                if term in snippet_lower:
                    first_pos = snippet_lower.find(term)
                    position_bonus = 1.0 - (first_pos / len(snippet_lower)) * 0.3
                    score += 1.0 * position_bonus

            # Domain authority bonus (common reputable sources)
            domain = result.domain.lower()
            authoritative_domains = {
                "wikipedia.org", "github.com", "stackoverflow.com", "docs.",
                "developer.mozilla.org", "python.org", "nodejs.org",
                "mozilla.org", "w3.org", "mdn."
            }
            if any(d in domain for d in authoritative_domains):
                score += 0.5

            scored_results.append((result, score))

        # Sort by score descending
        scored_results.sort(key=lambda x: x[1], reverse=True)

        # Log re-ranking changes
        if len(scored_results) > 1:
            top_score = scored_results[0][1]
            logger.info(f"Flash re-rank: top score={top_score:.2f}, results={len(scored_results)}")

        return [r for r, s in scored_results]

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
