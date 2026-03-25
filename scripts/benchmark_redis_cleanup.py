
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

# Simulated aioredis module
class MockAioRedis:
    async def scan_iter(self, redis, match=None, count=None):
        # Yield some dummy keys for each scan
        keys = [f"key:{match}:{i}" for i in range(5)]
        for key in keys:
            yield key

async def original_implementation(redis, domain_names):
    aioredis = MockAioRedis()
    round_trips = 0

    pipe = redis.pipeline()
    for domain in domain_names:
        # FastMCP cache keys match like: mcp-server__tools/call::scrape_url:{"url":"https://domain.com/..."}
        # We'll use SCAN to find matching keys
        async for key in aioredis.scan_iter(redis, match=f"*scrape_url*{domain}*", count=100):
            await redis.delete(key)
            round_trips += 1
        async for key in aioredis.scan_iter(redis, match=f"*scrape_url*{domain.replace('.', '.')}*", count=100):
            await redis.delete(key)
            round_trips += 1
    await pipe.execute()
    round_trips += 1
    return round_trips

async def optimized_implementation(redis, domain_names):
    aioredis = MockAioRedis()
    round_trips = 0

    pipe = redis.pipeline()
    for domain in domain_names:
        async for key in aioredis.scan_iter(redis, match=f"*scrape_url*{domain}*", count=100):
            pipe.delete(key)
        # Note: I'm keeping the domain.replace('.', '.') for now to match original logic,
        # but I suspect it's a typo for something else or redundant.
        async for key in aioredis.scan_iter(redis, match=f"*scrape_url*{domain.replace('.', '.')}*", count=100):
            pipe.delete(key)
    await pipe.execute()
    round_trips += 1
    return round_trips

async def main():
    redis = MagicMock()
    redis.delete = AsyncMock()
    redis.pipeline = MagicMock()
    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis.pipeline.return_value = pipe

    domain_names = [f"domain{i}.com" for i in range(10)]

    print("Benchmarking original implementation...")
    start = time.perf_counter()
    orig_trips = await original_implementation(redis, domain_names)
    end = time.perf_counter()
    print(f"Original Round Trips: {orig_trips}")
    print(f"Original Time (simulated): {end - start:.6f}s")

    print("\nBenchmarking optimized implementation...")
    start = time.perf_counter()
    opt_trips = await optimized_implementation(redis, domain_names)
    end = time.perf_counter()
    print(f"Optimized Round Trips: {opt_trips}")
    print(f"Optimized Time (simulated): {end - start:.6f}s")

    improvement = (orig_trips - opt_trips) / orig_trips * 100
    print(f"\nRound trip reduction: {improvement:.2f}%")

if __name__ == "__main__":
    asyncio.run(main())
