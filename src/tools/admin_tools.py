"""Admin Tools

Administrative tools for managing the MCP server.
- get_domains: List tracked domains with preferred methods
- clean_database: Clear all domain tracking data

Note: Domain tracking uses PostgreSQL database shared with Celery workers.
"""

from typing import Annotated

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field


async def get_domains(ctx: Context | None = None) -> dict:
    """List all tracked domains with their preferred scraping methods

    Returns:
        Dictionary with total count and list of domain records

    Note:
        This data is managed in PostgreSQL database and is not cached.
    """
    if ctx:
        await ctx.debug("Fetching all tracked domains")

    # Get db from lifespan context with safe access
    db = ctx.lifespan_context.get("db")
    if not db:
        raise ToolError("Database service not available")

    domains = await db.get_all_domains()

    if ctx:
        await ctx.debug(f"Retrieved {len(domains)} domains")

    return {
        "total": len(domains),
        "domains": domains
    }


async def clean_database(ctx: Context | None = None) -> dict:
    """Clear all domain tracking data

    This resets all learned scraping methods and blacklist entries.
    Use this to start fresh.

    Returns:
        Dictionary with status and count of removed records

    Warning:
        This operation cannot be undone. All learned domain preferences
        will be lost and must be re-learned through scraping.
    """
    # Get db from lifespan context with safe access
    db = ctx.lifespan_context.get("db")
    if not db:
        raise ToolError("Database service not available")

    count = await db.clean()

    if ctx:
        await ctx.info(f"Cleaned {count} domain records")

    return {
        "status": "success",
        "records_removed": count
    }
