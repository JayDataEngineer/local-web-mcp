"""PostgreSQL database for domain tracking and blacklist"""

import asyncio
from datetime import datetime
from typing import List, Optional
from loguru import logger
import os

from ..constants import BLACKLIST_FAILURE_THRESHOLD


class Database:
    """PostgreSQL database for tracking scraping methods and blacklist"""

    def __init__(
        self,
        db_url: str = None,
        host: str = None,
        port: int = None,
        database: str = None,
        user: str = None,
        password: str = None
    ):
        """
        Initialize PostgreSQL database connection

        Args:
            db_url: Full database URL (overrides other params)
            host: Database host (default from env or postgres)
            port: Database port (default from env or 5432)
            database: Database name (default from env or mcp_server)
            user: Database user (default from env or postgres)
            password: Database password (default from env or postgres)
        """
        if db_url:
            self.db_url = db_url
        else:
            host = host or os.getenv("POSTGRES_HOST", "postgres")
            port = port or os.getenv("POSTGRES_PORT", "5432")
            database = database or os.getenv("POSTGRES_DB", "mcp_server")
            user = user or os.getenv("POSTGRES_USER", "postgres")
            password = password or os.getenv("POSTGRES_PASSWORD", "postgres")

            self.db_url = f"postgresql://{user}:{password}@{host}:{port}/{database}"

        self._pool = None

    async def init(self):
        """Initialize database schema and connection pool"""
        import asyncpg

        self._pool = await asyncpg.create_pool(
            self.db_url,
            min_size=2,
            max_size=10,
            command_timeout=30
        )

        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS domains (
                    domain TEXT PRIMARY KEY,
                    preferred_method TEXT NOT NULL DEFAULT 'crawl4ai',
                    last_success TIMESTAMPTZ,
                    last_failure TIMESTAMPTZ,
                    failure_count INTEGER DEFAULT 0,
                    is_blacklisted BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_domains_blacklisted
                ON domains(is_blacklisted)
                WHERE is_blacklisted = TRUE
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_domains_method
                ON domains(preferred_method, is_blacklisted)
                WHERE is_blacklisted = FALSE
            """)
            await conn.execute("""
                CREATE OR REPLACE FUNCTION update_updated_at()
                RETURNS TRIGGER AS $$
                BEGIN
                    NEW.updated_at = CURRENT_TIMESTAMP;
                    RETURN NEW;
                END;
                $$ language 'plpgsql'
            """)
            await conn.execute("""
                DROP TRIGGER IF EXISTS update_domains_updated_at ON domains
            """)
            await conn.execute("""
                CREATE TRIGGER update_domains_updated_at
                    BEFORE UPDATE ON domains
                    FOR EACH ROW
                    EXECUTE FUNCTION update_updated_at()
            """)

        logger.info(f"PostgreSQL initialized: {self.db_url}")

    async def close(self):
        """Close the connection pool"""
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("PostgreSQL connection pool closed")

    async def get_domain_method(self, domain: str) -> Optional[str]:
        """Get preferred scraping method for domain"""
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "SELECT preferred_method FROM domains WHERE domain = $1 AND is_blacklisted = FALSE",
                domain
            )
            return row

    async def record_success(self, domain: str, method: str):
        """Record successful scrape"""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO domains (domain, preferred_method, last_success, failure_count)
                VALUES ($1, $2, CURRENT_TIMESTAMP, 0)
                ON CONFLICT (domain) DO UPDATE SET
                    preferred_method = EXCLUDED.preferred_method,
                    last_success = EXCLUDED.last_success,
                    failure_count = 0,
                    is_blacklisted = FALSE
            """, domain, method)
            logger.debug(f"Success recorded: {domain} -> {method}")

    async def set_selenium_only(self, domain: str):
        """Mark domain as selenium-only"""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO domains (domain, preferred_method)
                VALUES ($1, 'selenium')
                ON CONFLICT (domain) DO UPDATE SET
                    preferred_method = 'selenium'
            """, domain)

    async def blacklist(self, domain: str):
        """Blacklist a domain - all scraping failed"""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO domains (domain, is_blacklisted)
                VALUES ($1, TRUE)
                ON CONFLICT (domain) DO UPDATE SET
                    is_blacklisted = TRUE
            """, domain)
            logger.warning(f"Blacklisted: {domain}")

    async def is_blacklisted(self, domain: str) -> bool:
        """Check if domain is blacklisted"""
        async with self._pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT is_blacklisted FROM domains WHERE domain = $1",
                domain
            )
            return bool(result)

    async def get_blacklisted_domains(self) -> set:
        """Get all blacklisted domains as a set"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT domain FROM domains WHERE is_blacklisted = TRUE"
            )
            return {row["domain"] for row in rows}

    async def get_all_domains(self) -> List[dict]:
        """Get all domain records"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT domain, preferred_method,
                       last_success at time zone 'utc' as last_success,
                       last_failure at time zone 'utc' as last_failure,
                       failure_count, is_blacklisted
                FROM domains
                ORDER BY created_at DESC
            """)
            return [
                {
                    "domain": row["domain"],
                    "preferred_method": row["preferred_method"],
                    "last_success": row["last_success"].isoformat() if row["last_success"] else None,
                    "last_failure": row["last_failure"].isoformat() if row["last_failure"] else None,
                    "failure_count": row["failure_count"],
                    "is_blacklisted": row["is_blacklisted"]
                }
                for row in rows
            ]

    async def clean(self) -> int:
        """Clean all records from database"""
        async with self._pool.acquire() as conn:
            count = await conn.execute("DELETE FROM domains")
            logger.info(f"Database cleaned: {count} records removed")
            return count

    async def check_urls(
        self,
        max_urls: Optional[int] = None,
        threshold: int = None
    ) -> dict:
        """
        Check URLs in database to verify scraping still works

        For each domain:
        - Try to scrape
        - If success: update timestamp
        - If failure: increment count
        - If failures >= threshold: blacklist
        """
        from ..services.scrape_service import get_scrape_service
        from ..models.unified import ScrapeRequest

        if threshold is None:
            threshold = BLACKLIST_FAILURE_THRESHOLD

        scrape_svc = get_scrape_service()
        domains = await self.get_all_domains()

        stats = {
            "total_checked": 0,
            "still_valid": 0,
            "moved_to_selenium": 0,
            "blacklisted": 0,
            "details": []
        }

        for record in domains[:max_urls]:
            domain = record["domain"]

            # Skip already blacklisted
            if record["is_blacklisted"]:
                continue

            stats["total_checked"] += 1

            # Test URL (use domain root or construct one)
            test_url = f"https://{domain}/"

            try:
                result = await scrape_svc.scrape(
                    ScrapeRequest(url=test_url)
                )

                if result.success:
                    await self.record_success(domain, result.method_used.value)
                    stats["still_valid"] += 1
                    stats["details"].append({
                        "domain": domain,
                        "status": "valid",
                        "method": result.method_used.value
                    })
                else:
                    # Increment failure count
                    new_count = record["failure_count"] + 1
                    await self._increment_failure(domain, new_count)

                    if new_count >= threshold:
                        await self.blacklist(domain)
                        stats["blacklisted"] += 1
                        stats["details"].append({
                            "domain": domain,
                            "status": "blacklisted",
                            "failures": new_count
                        })
                    else:
                        stats["details"].append({
                            "domain": domain,
                            "status": "failed",
                            "failures": new_count
                        })

            except Exception as e:
                logger.error(f"Check failed for {domain}: {e}")
                stats["details"].append({
                    "domain": domain,
                    "status": "error",
                    "error": str(e)
                })

        return stats

    async def _increment_failure(self, domain: str, count: int):
        """Increment failure counter"""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO domains (domain, failure_count, last_failure)
                VALUES ($1, $2, CURRENT_TIMESTAMP)
                ON CONFLICT (domain) DO UPDATE SET
                    failure_count = EXCLUDED.failure_count,
                    last_failure = EXCLUDED.last_failure
            """, domain, count)


# Singleton
_db: Database = None


async def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
        await _db.init()
    return _db
