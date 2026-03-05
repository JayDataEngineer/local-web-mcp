"""Summary service using Docker Model Runner"""

import httpx
from loguru import logger
from ..config import settings


class SummaryService:
    """Summarize content using Docker Model Runner"""

    def __init__(self):
        self.client = None

    async def _get_client(self):
        """Lazy HTTP client initialization"""
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=120.0)
        return self.client

    async def close(self):
        """Close the HTTP client"""
        if self.client:
            await self.client.aclose()
            self.client = None

    async def summarize(self, content: str, max_length: int = 300) -> str:
        """
        Summarize content using DMR

        Args:
            content: Text to summarize
            max_length: Target length in words

        Returns:
            Summary text
        """
        client = await self._get_client()

        # Truncate if too long (DMR has context limits)
        max_input = 8000  # chars, conservative for 2B model
        if len(content) > max_input:
            content = content[:max_input] + "..."

        prompt = f"""Summarize the following content concisely (max {max_length} words).
Focus on key information and main points:

{content}

Summary:"""

        try:
            response = await client.post(
                f"{settings.dmr_base_url}/chat/completions",
                json={
                    "model": settings.dmr_model,
                    "messages": [
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": 500,
                    "temperature": 0.3,
                }
            )
            response.raise_for_status()

            data = response.json()
            summary = data["choices"][0]["message"]["content"].strip()

            logger.info(f"Generated summary: {len(summary)} chars")
            return summary

        except Exception as e:
            logger.error(f"Summary failed: {e}")
            # Fallback: return truncated original
            words = content.split()[:max_length]
            return " ".join(words) + ("..." if len(content.split()) > max_length else "")


# Singleton factory
from ..utils import create_async_singleton_factory
get_summary_service = create_async_singleton_factory(SummaryService, "get_summary_service")
