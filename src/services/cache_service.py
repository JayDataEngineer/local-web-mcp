"""Redis caching service for scrape and search results"""

import json
import hashlib
from typing import Optional, Any
from loguru import logger

from ..constants import (
    CACHE_ENABLED,
    CACHE_SCRAPE_TTL_SECONDS,
    CACHE_SEARCH_TTL_SECONDS,
    CACHE_KEY_PREFIX,
)


class CacheService:
    """Redis-based caching for scrape and search results"""

    def __init__(self, redis_url: str = None):
        self._redis = None
        self._redis_url = redis_url

    async def _get_redis(self):
        """Lazy init Redis connection"""
        if self._redis is None:
            try:
                import redis.asyncio as aioredis
                if self._redis_url:
                    self._redis = await aioredis.from_url(
                        self._redis_url,
                        encoding="utf-8",
                        decode_responses=True
                    )
                else:
                    # Try default from env
                    import os
                    redis_url = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
                    # Extract just the host:port for Redis client
                    redis_url = redis_url.split("/")[0] if "//" in redis_url else redis_url
                    self._redis = await aioredis.from_url(
                        redis_url,
                        encoding="utf-8",
                        decode_responses=True
                    )
                logger.info(f"Cache connected to Redis: {self._redis_url or 'default'}")
            except ImportError:
                logger.warning("Redis not available - caching disabled")
                self._redis = False
            except Exception as e:
                logger.warning(f"Failed to connect to Redis: {e} - caching disabled")
                self._redis = False
        return self._redis

    def _is_available(self) -> bool:
        """Check if caching is available"""
        return CACHE_ENABLED and self._redis is not False

    def _make_key(self, prefix: str, identifier: str) -> str:
        """Create a cache key"""
        return f"{CACHE_KEY_PREFIX}{prefix}:{identifier}"

    def _hash_url(self, url: str) -> str:
        """Create a consistent hash for URL cache keys"""
        return hashlib.sha256(url.encode()).hexdigest()[:16]

    async def get_scrape(self, url: str) -> Optional[dict]:
        """Get cached scrape result for a URL"""
        if not self._is_available():
            return None

        redis = await self._get_redis()
        if not redis:
            return None

        key = self._make_key("scrape", self._hash_url(url))
        try:
            data = await redis.get(key)
            if data:
                logger.debug(f"Cache HIT for scrape: {url}")
                return json.loads(data)
            logger.debug(f"Cache MISS for scrape: {url}")
            return None
        except Exception as e:
            logger.warning(f"Cache get failed: {e}")
            return None

    async def set_scrape(self, url: str, result: dict) -> bool:
        """Cache a scrape result"""
        if not self._is_available():
            return False

        redis = await self._get_redis()
        if not redis:
            return False

        key = self._make_key("scrape", self._hash_url(url))
        try:
            await redis.setex(
                key,
                CACHE_SCRAPE_TTL_SECONDS,
                json.dumps(result)
            )
            logger.debug(f"Cached scrape result: {url}")
            return True
        except Exception as e:
            logger.warning(f"Cache set failed: {e}")
            return False

    async def get_search(self, query: str, pages: int, exclude_blacklist: bool) -> Optional[dict]:
        """Get cached search results"""
        if not self._is_available():
            return None

        redis = await self._get_redis()
        if not redis:
            return None

        # Create cache key from search parameters
        params = f"{query}:{pages}:{exclude_blacklist}"
        params_hash = hashlib.sha256(params.encode()).hexdigest()[:16]
        key = self._make_key("search", params_hash)

        try:
            data = await redis.get(key)
            if data:
                logger.debug(f"Cache HIT for search: {query}")
                return json.loads(data)
            logger.debug(f"Cache MISS for search: {query}")
            return None
        except Exception as e:
            logger.warning(f"Cache get failed: {e}")
            return None

    async def set_search(self, query: str, pages: int, exclude_blacklist: bool, result: dict) -> bool:
        """Cache search results"""
        if not self._is_available():
            return False

        redis = await self._get_redis()
        if not redis:
            return False

        params = f"{query}:{pages}:{exclude_blacklist}"
        params_hash = hashlib.sha256(params.encode()).hexdigest()[:16]
        key = self._make_key("search", params_hash)

        try:
            await redis.setex(
                key,
                CACHE_SEARCH_TTL_SECONDS,
                json.dumps(result)
            )
            logger.debug(f"Cached search results: {query}")
            return True
        except Exception as e:
            logger.warning(f"Cache set failed: {e}")
            return False

    async def invalidate_scrape(self, url: str) -> bool:
        """Invalidate cached scrape for a URL (force refresh)"""
        if not self._is_available():
            return False

        redis = await self._get_redis()
        if not redis:
            return False

        key = self._make_key("scrape", self._hash_url(url))
        try:
            await redis.delete(key)
            logger.info(f"Invalidated cache for: {url}")
            return True
        except Exception as e:
            logger.warning(f"Cache invalidation failed: {e}")
            return False

    async def clear_all(self) -> int:
        """Clear all cached items"""
        if not self._is_available():
            return 0

        redis = await self._get_redis()
        if not redis:
            return 0

        try:
            # Find all keys with our prefix
            keys = []
            async for key in redis.scan_iter(f"{CACHE_KEY_PREFIX}*"):
                keys.append(key)

            if keys:
                await redis.delete(*keys)
                logger.info(f"Cleared {len(keys)} cached items")
            return len(keys)
        except Exception as e:
            logger.warning(f"Cache clear failed: {e}")
            return 0

    async def close(self):
        """Close Redis connection"""
        if self._redis and self._redis is not False:
            await self._redis.close()
            self._redis = None


# Singleton factory
from ..utils import create_singleton_factory
get_cache_service = create_singleton_factory(
    lambda: CacheService(),
    "get_cache_service"
)
