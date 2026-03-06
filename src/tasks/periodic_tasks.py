"""Periodic Celery tasks for maintenance operations"""

from loguru import logger

from ..celery_app import app
from ..tasks.base import BaseTask


@app.task(bind=True, base=BaseTask, name="tasks.periodic.cleanup_blacklist")
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
    count = self.run_async(self.db.cleanup_old_blacklisted(days_old))
    result = {
        "status": "success",
        "days_old": days_old,
        "domains_removed": count
    }
    logger.info(f"Periodic cleanup completed: {result}")
    return result


@app.task(bind=True, base=BaseTask, name="tasks.periodic.health_check")
def health_check(self) -> dict:
    """
    Periodic health check task

    Verifies that core services are responsive.
    """
    # Use TRY pattern - return default on error
    try:
        domains_count = self.run_async(self.db.get_all_domains())
        db_status = "ok"
        count = len(domains_count)
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        db_status = f"error: {e}"
        count = 0

    result = {
        "status": "success",
        "database": db_status,
        "domains_count": count
    }
    logger.info(f"Health check completed: {db_status}")
    return result
