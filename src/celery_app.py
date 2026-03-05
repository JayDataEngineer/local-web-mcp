"""Celery configuration for MCP Server"""

from celery import Celery

from .constants import (
    CELERY_TASK_TIMEOUT_SECONDS,
    CELERY_TASK_SOFT_TIMEOUT_SECONDS,
    CELERY_WORKER_CONCURRENCY,
    CELERY_RESULT_EXPIRE_SECONDS,
)

# Create Celery app
app = Celery("mcp_server")

# Configuration - values from constants
app.conf.update(
    # Task settings
    task_track_started=True,
    task_time_limit=CELERY_TASK_TIMEOUT_SECONDS,
    task_soft_time_limit=CELERY_TASK_SOFT_TIMEOUT_SECONDS,
    task_acks_late=True,  # Only ack after task completes
    worker_prefetch_multiplier=1,  # Don't prefetch extra tasks

    # Result expiration - critical for memory management
    result_expires=CELERY_RESULT_EXPIRE_SECONDS,

    # Worker settings - will be overridden by CLI arg
    worker_concurrency=CELERY_WORKER_CONCURRENCY,

    # Serialization
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
)

# Import tasks explicitly (autodiscovery doesn't work with our structure)
# This MUST happen after app is created
from .tasks.scrape_tasks import scrape_task  # noqa: E402


@app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    """Setup any periodic tasks here if needed"""
    pass


if __name__ == "__main__":
    app.start()
