"""SQLite database for domain tracking and blacklist"""

import aiosqlite
import asyncio
from datetime import datetime
from typing import List, Optional
from loguru import logger

from ..constants import BLACKLIST_FAILURE_THRESHOLD


class Database:
    """SQLite database for tracking scraping methods and blacklist"""

    def __init__(self, db_path: str = "/app/data/mcp.db"):
        self.db_path = db_path

    async def init(self):
        """Initialize database schema"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS domains (
                    domain TEXT PRIMARY KEY,
                    preferred_method TEXT NOT NULL DEFAULT 'crawl4ai',
                    last_success TIMESTAMP,
                    last_failure TIMESTAMP,
                    failure_count INTEGER DEFAULT 0,
                    is_blacklisted BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()
            logger.info(f"Database initialized: {self.db_path}")

    async def get_domain_method(self, domain: str) -> Optional[str]:
        """Get preferred scraping method for domain"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT preferred_method FROM domains WHERE domain = ? AND is_blacklisted = 0",
                (domain,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def record_success(self, domain: str, method: str):
        """Record successful scrape"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO domains (domain, preferred_method, last_success, failure_count)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(domain) DO UPDATE SET
                    preferred_method = excluded.preferred_method,
                    last_success = excluded.last_success,
                    failure_count = 0,
                    is_blacklisted = 0,
                    updated_at = CURRENT_TIMESTAMP
            """, (domain, method, datetime.now()))
            await db.commit()
            logger.debug(f"Success recorded: {domain} -> {method}")

    async def set_selenium_only(self, domain: str):
        """Mark domain as selenium-only"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO domains (domain, preferred_method)
                VALUES (?, 'selenium')
                ON CONFLICT(domain) DO UPDATE SET
                    preferred_method = 'selenium',
                    updated_at = CURRENT_TIMESTAMP
            """, (domain,))
            await db.commit()

    async def blacklist(self, domain: str):
        """Blacklist a domain - all scraping failed"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO domains (domain, is_blacklisted)
                VALUES (?, 1)
                ON CONFLICT(domain) DO UPDATE SET
                    is_blacklisted = 1,
                    updated_at = CURRENT_TIMESTAMP
            """, (domain,))
            await db.commit()
            logger.warning(f"Blacklisted: {domain}")

    async def is_blacklisted(self, domain: str) -> bool:
        """Check if domain is blacklisted"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT is_blacklisted FROM domains WHERE domain = ?",
                (domain,)
            ) as cursor:
                row = await cursor.fetchone()
                return bool(row and row[0])

    async def get_blacklisted_domains(self) -> set:
        """Get all blacklisted domains as a set"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT domain FROM domains WHERE is_blacklisted = 1"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0] for row in rows}

    async def get_all_domains(self) -> List[dict]:
        """Get all domain records"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("""
                SELECT domain, preferred_method, last_success, last_failure,
                       failure_count, is_blacklisted
                FROM domains
                ORDER BY created_at DESC
            """) as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        "domain": row[0],
                        "preferred_method": row[1],
                        "last_success": row[2],
                        "last_failure": row[3],
                        "failure_count": row[4],
                        "is_blacklisted": bool(row[5])
                    }
                    for row in rows
                ]

    async def clean(self) -> int:
        """Clean all records from database"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM domains")
            count = cursor.rowcount
            await db.commit()
            logger.info(f"Database cleaned: {count} records removed")
            return count

    async def check_urls(self, max_urls: Optional[int] = None,
                        threshold: int = None) -> dict:
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
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO domains (domain, failure_count, last_failure)
                VALUES (?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    failure_count = excluded.failure_count,
                    last_failure = excluded.last_failure,
                    updated_at = CURRENT_TIMESTAMP
            """, (domain, count, datetime.now()))
            await db.commit()


# Singleton
_db: Database = None


async def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
        await _db.init()
    return _db
