"""PostgreSQL database for domain tracking and blacklist

Uses SQLAlchemy 2.0 async ORM instead of raw SQL.
"""

from datetime import datetime
from typing import List, Optional
from loguru import logger
import os

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select, update, delete, func
from sqlalchemy.orm import selectinload

from .models import Domain
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
            # Convert postgresql:// to postgresql+asyncpg:// for SQLAlchemy
            if not db_url.startswith("postgresql+asyncpg://"):
                db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")
            self.db_url = db_url
        else:
            host = host or os.getenv("POSTGRES_HOST", "postgres")
            port = port or os.getenv("POSTGRES_PORT", "5432")
            database = database or os.getenv("POSTGRES_DB", "mcp_server")
            user = user or os.getenv("POSTGRES_USER", "postgres")
            # Require password from env or parameter - no default
            password = password or os.getenv("POSTGRES_PASSWORD")
            if not password:
                raise ValueError("POSTGRES_PASSWORD environment variable must be set")

            self.db_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"

        self._engine = None
        self._sessionmaker = None

    async def init(self):
        """Initialize database schema and connection pool"""
        from .models import Base

        self._engine = create_async_engine(
            self.db_url,
            echo=False,
            pool_size=10,
            max_overflow=20,
        )

        self._sessionmaker = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        # Create tables
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        logger.info(f"PostgreSQL initialized (SQLAlchemy 2.0): {self.db_url}")

    async def close(self):
        """Close the connection pool"""
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None
            logger.info("PostgreSQL connection pool closed")

    def _get_session(self) -> AsyncSession:
        """Get a new database session"""
        if self._sessionmaker is None:
            raise RuntimeError("Database not initialized. Call init() first.")
        return self._sessionmaker()

    async def get_domain_method(self, domain: str) -> Optional[str]:
        """Get preferred scraping method for domain"""
        async with self._get_session() as session:
            result = await session.execute(
                select(Domain.preferred_method)
                .where(Domain.domain == domain)
                .where(Domain.is_blacklisted == False)
            )
            return result.scalar_one_or_none()

    async def record_success(self, domain: str, method: str):
        """Record successful scrape"""
        async with self._get_session() as session:
            # Check if domain exists
            result = await session.execute(
                select(Domain).where(Domain.domain == domain)
            )
            db_domain = result.scalar_one_or_none()

            if db_domain:
                # Update existing
                db_domain.preferred_method = method
                db_domain.last_success = func.now()
                db_domain.failure_count = 0
                db_domain.is_blacklisted = False
            else:
                # Insert new
                new_domain = Domain(
                    domain=domain,
                    preferred_method=method,
                    last_success=datetime.now(),
                    failure_count=0,
                    is_blacklisted=False
                )
                session.add(new_domain)

            await session.commit()
            logger.debug(f"Success recorded: {domain} -> {method}")

    async def set_selenium_only(self, domain: str):
        """Mark domain as selenium-only"""
        async with self._get_session() as session:
            result = await session.execute(
                select(Domain).where(Domain.domain == domain)
            )
            db_domain = result.scalar_one_or_none()

            if db_domain:
                db_domain.preferred_method = "selenium"
            else:
                new_domain = Domain(domain=domain, preferred_method="selenium")
                session.add(new_domain)

            await session.commit()

    async def blacklist(self, domain: str):
        """Blacklist a domain - all scraping failed"""
        async with self._get_session() as session:
            result = await session.execute(
                select(Domain).where(Domain.domain == domain)
            )
            db_domain = result.scalar_one_or_none()

            if db_domain:
                db_domain.is_blacklisted = True
            else:
                new_domain = Domain(domain=domain, is_blacklisted=True)
                session.add(new_domain)

            await session.commit()
            logger.warning(f"Blacklisted: {domain}")

    async def is_blacklisted(self, domain: str) -> bool:
        """Check if domain is blacklisted"""
        async with self._get_session() as session:
            result = await session.execute(
                select(Domain.is_blacklisted)
                .where(Domain.domain == domain)
            )
            return result.scalar_one_or_none() or False

    async def get_blacklisted_domains(self) -> set:
        """Get all blacklisted domains as a set"""
        async with self._get_session() as session:
            result = await session.execute(
                select(Domain.domain)
                .where(Domain.is_blacklisted == True)
            )
            return set(result.scalars().all())

    async def get_all_domains(self) -> List[dict]:
        """Get all domain records"""
        async with self._get_session() as session:
            result = await session.execute(
                select(Domain)
                .order_by(Domain.created_at.desc())
            )
            domains = result.scalars().all()
            return [d.to_dict() for d in domains]

    async def cleanup_old_blacklisted(self, days_old: int = 7) -> int:
        """Remove blacklisted domains older than specified days"""
        from datetime import timedelta

        cutoff_date = datetime.now() - timedelta(days=days_old)

        async with self._get_session() as session:
            # Find blacklisted domains with updated_at older than cutoff
            result = await session.execute(
                select(Domain.domain)
                .where(Domain.is_blacklisted == True)
                .where(Domain.updated_at < cutoff_date)
            )
            old_domains = result.scalars().all()

            if not old_domains:
                return 0

            # Delete old blacklisted domains
            delete_result = await session.execute(
                delete(Domain)
                .where(Domain.domain.in_(old_domains))
            )
            await session.commit()

            count = delete_result.rowcount
            logger.info(f"Cleaned up {count} blacklisted domains older than {days_old} days")
            return count

    async def clean(self) -> int:
        """Clean all records from database"""
        async with self._get_session() as session:
            result = await session.execute(
                delete(Domain)
            )
            await session.commit()
            count = result.rowcount
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
        async with self._get_session() as session:
            result = await session.execute(
                select(Domain).where(Domain.domain == domain)
            )
            db_domain = result.scalar_one_or_none()

            if db_domain:
                db_domain.failure_count = count
                db_domain.last_failure = datetime.now()
            else:
                new_domain = Domain(
                    domain=domain,
                    failure_count=count,
                    last_failure=datetime.now()
                )
                session.add(new_domain)

            await session.commit()


# Singleton
_db: Database = None


async def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
        await _db.init()
    return _db
