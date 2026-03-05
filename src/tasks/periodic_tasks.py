"""Periodic Celery tasks for maintenance operations"""

from celery import Task
from loguru import logger

from ..celery_app import app
from ..db.database import Database


class PeriodicTask(Task):
    """Custom Task class with lazy database initialization"""

    _db = None

    @property
    def db(self) -> Database:
        """Lazy init database connection"""
        if self._db is None:
            self._db = Database()
        return self._db


@app.task(bind=True, base=PeriodicTask, name="tasks.periodic.cleanup_blacklist")
def cleanup_blacklist(self, days_old: int = 7) -> dict:
    """
    Periodic task to clean up old blacklisted domains

    Removes blacklisted domains that haven't been updated in X days.
    This prevents the blacklist from growing indefinitely.

    Args:
        days_old: Remove domains older than this many days

    Returns:
        Dict with cleanup results
    """
    import asyncio

    async def _cleanup():
        count = await self.db.cleanup_old_blacklisted(days_old)
        return {
            "status": "success",
            "days_old": days_old,
            "domains_removed": count
        }

    # Run async cleanup in new event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(_cleanup())
        logger.info(f"Periodic cleanup completed: {result}")
        return result
    finally:
        loop.close()


@app.task(bind=True, base=PeriodicTask, name="tasks.periodic.health_check")
def health_check(self) -> dict:
    """
    Periodic health check task

    Verifies that core services are responsive.
    """
    import asyncio

    async def _check():
        # Check database connection
        try:
            domains_count = await self.db.get_all_domains()
            db_status = "ok"
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            db_status = f"error: {e}"

        # TODO: Add checks for other services (Redis, SearXNG)

        return {
            "status": "success",
            "database": db_status,
            "domains_count": len(domains_count) if db_status == "ok" else 0
        }

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(_check())
        logger.info(f"Health check completed: {result.get('database')}")
        return result
    finally:
        loop.close()
