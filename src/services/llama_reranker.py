"""Llama.cpp Reranker Service

Uses Jina Reranker v3 running via llama.cpp for intelligent result reranking.
"""

import httpx
from loguru import logger
from typing import List

from ..models.unified import SearchResult


class LamaReranker:
    """Jina Reranker v3 via llama.cpp for intelligent result reranking"""

    def __init__(self, reranker_url: str = "http://llama-reranker:8082"):
        self.reranker_url = reranker_url
        self.client = httpx.AsyncClient(timeout=30.0)

    async def rerank(
        self,
        query: str,
        results: List[SearchResult],
        top_k: int | None = None
    ) -> List[SearchResult]:
        """Rerank search results using Jina Reranker v3

        Args:
            query: The original search query
            results: List of search results to rerank
            top_k: Maximum number of results to return (None = all)

        Returns:
            Re-ranked list of search results
        """
        if not results:
            return results

        # Prepare documents for reranking
        documents = [
            f"{r.title}. {r.snippet}" for r in results
        ]

        try:
            response = await self.client.post(
                f"{self.reranker_url}/v1/rerank",
                json={
                    "model": "jina",
                    "query": query,
                    "documents": documents,
                    "top_n": top_k if top_k else len(results)
                }
            )
            response.raise_for_status()
            data = response.json()

            # Extract reranked results
            reranked = []
            for item in data.get("results", []):
                idx = item.get("index")
                score = item.get("relevance_score", 0)
                if 0 <= idx < len(results):
                    result = results[idx]
                    # Add relevance score as metadata
                    if hasattr(result, '__dict__'):
                        result.relevance_score = score
                    reranked.append(result)

            logger.info(f"Reranked {len(reranked)} results, top score: {max([r.get('relevance_score', 0) for r in data.get('results', [])], default=0):.4f}")

            return reranked

        except httpx.HTTPError as e:
            logger.warning(f"Reranker HTTP error: {e}")
            # Fall back to original order on error
            return results
        except Exception as e:
            logger.warning(f"Reranker error: {e}")
            return results

    async def close(self):
        await self.client.aclose()


# Singleton factory
_reranker_service: LamaReranker | None = None


def get_reranker_service() -> LamaReranker:
    """Get or create the reranker service singleton"""
    global _reranker_service
    if _reranker_service is None:
        _reranker_service = LamaReranker()
    return _reranker_service
