"""MCP Server with SSE transport for Tailscale/HTTP access

This allows Claude Desktop or other MCP clients to connect over HTTP/SSE
instead of stdio. Perfect for remote access via Tailscale.

Tools:
- search_web: Search using multiple search engines
- scrape_url: Scrape URL with automatic method selection
- get_domains: List tracked domains with preferred methods
- clean_database: Clear all domain tracking data
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP, Context
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from loguru import logger
import os


# Configure CORS from environment
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]


# Service singletons (initialized in lifespan)
_search_service = None
_scrape_service = None
_db = None


def _ensure_services():
    """Ensure services are initialized (for direct tool calls outside MCP)"""
    global _search_service, _scrape_service, _db
    if _search_service is None:
        from .services.search_service import get_search_service
        _search_service = get_search_service()
    if _scrape_service is None:
        from .services.scrape_service import get_scrape_service
        _scrape_service = get_scrape_service()
    if _db is None:
        from .db.database import Database
        _db = Database()
        # Don't init here - it will be in lifespan or on first use


async def _get_db():
    """Get database, initializing if needed"""
    global _db
    if _db is None:
        from .db.database import Database
        _db = Database()
    # Ensure initialized
    if _db._engine is None:
        await _db.init()
    return _db


@asynccontextmanager
async def service_lifespan(server: FastMCP):
    """Initialize and cleanup services on server startup/shutdown"""
    global _search_service, _scrape_service, _db

    from .services.search_service import get_search_service
    from .services.scrape_service import get_scrape_service
    from .db.database import Database

    logger.info("Initializing services...")
    _search_service = get_search_service()
    _scrape_service = get_scrape_service()
    _db = Database()
    await _db.init()
    logger.info("Services ready")

    yield

    logger.info("Shutting down services...")
    await _search_service.close()
    await _scrape_service.close()
    await _db.close()
    logger.info("Shutdown complete")


# Create FastMCP instance with metadata
mcp = FastMCP(
    name="mcp-research-server",
    instructions=(
        "Provides web search and URL scraping tools. "
        "Use search_web to find information and scrape_url to extract content from pages. "
        "The server learns which scraping method works best for each domain."
    ),
    lifespan=service_lifespan
)


# Add CORS and health check to the underlying Starlette app
_original_http_app = mcp.http_app


def http_app_with_middleware(**kwargs):
    """Add CORS and health check to the underlying Starlette app"""
    app = _original_http_app(**kwargs)

    # Add CORS if not already present
    if not any(m.cls == CORSMiddleware for m in app.user_middleware):
        app.add_middleware(
            CORSMiddleware,
            allow_origins=ALLOWED_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Add SSE middleware to prevent buffering
    @app.middleware("http")
    async def add_sse_headers(request, call_next):
        response = await call_next(request)
        # Add headers to prevent reverse proxy buffering for SSE endpoints
        if request.url.path in ["/sse", "/messages"]:
            response.headers["X-Accel-Buffering"] = "no"
            response.headers["Cache-Control"] = "no-cache"
            if "Connection" not in response.headers:
                response.headers["Connection"] = "keep-alive"
        return response

    # Add health check endpoint
    async def health_check(request):
        return JSONResponse({"status": "healthy", "server": "mcp-research-server"})

    app.add_route("/health", health_check, methods=["GET"])
    return app


mcp.http_app = http_app_with_middleware


# ========== MCP TOOLS ==========

@mcp.tool()
async def search_web(
    query: str,
    pages: int = 10,
    exclude_blacklist: bool = True,
    ctx: Context | None = None
) -> dict:
    """Search the web using multiple search engines

    Args:
        query: Search query string
        pages: Number of search result pages to fetch (1-10)
        exclude_blacklist: Exclude blacklisted domains from results

    Returns:
        Dictionary with query, total_results, and list of results
    """
    _ensure_services()
    if ctx:
        await ctx.info(f"Searching for: {query}")

    result = await _search_service.search(
        query=query,
        pages=pages,
        exclude_blacklist=exclude_blacklist
    )

    return {
        "query": query,
        "total_results": result.total_results,
        "pages_scraped": result.pages_scraped,
        "engines": result.engines,
        "search_time_ms": result.search_time_ms,
        "cached": result.cached,
        "results": [
            {"title": r.title, "url": r.url, "snippet": r.snippet}
            for r in result.results
        ]
    }


@mcp.tool()
async def scrape_url(
    url: str,
    method: str | None = None,
    css_selector: str | None = None,
    ctx: Context | None = None
) -> dict:
    """Scrape a URL and extract clean markdown content

    The server learns which scraping method works best per domain and
    automatically uses it on future requests.

    Args:
        url: URL to scrape
        method: Force specific scraping method (crawl4ai, selenium, pdf)
        css_selector: Optional CSS selector for targeted content extraction

    Returns:
        Dictionary with success status, title, content, and metadata
    """
    _ensure_services()
    if ctx:
        await ctx.info(f"Scraping: {url}")

    from .models.unified import ScrapeRequest, ScrapingMethod

    request = ScrapeRequest(
        url=url,
        force_method=ScrapingMethod(method) if method else None,
        css_selector=css_selector
    )
    result = await _scrape_service.scrape(request)

    response = {
        "url": result.url,
        "success": result.success,
        "method_used": result.method_used.value if result.method_used else None,
        "title": result.title,
        "content": result.content,
        "word_count": result.metadata.get("word_count", 0) if result.metadata else 0,
    }

    if not result.success and result.error:
        response["error"] = result.error
        if ctx:
            await ctx.warning(f"Scrape failed: {result.error}")

    return response


@mcp.tool()
async def get_domains(ctx: Context | None = None) -> dict:
    """List all tracked domains with their preferred scraping methods

    Returns:
        Dictionary with total count and list of domain records
    """
    db = await _get_db()
    domains = await db.get_all_domains()
    return {
        "total": len(domains),
        "domains": domains
    }


@mcp.tool()
async def clean_database(ctx: Context | None = None) -> dict:
    """Clear all domain tracking data

    This resets all learned scraping methods and blacklist entries.
    Use this to start fresh.

    Returns:
        Dictionary with status and count of removed records
    """
    db = await _get_db()
    count = await db.clean()
    if ctx:
        await ctx.info(f"Cleaned {count} domain records")
    return {
        "status": "success",
        "records_removed": count
    }


# ========== SERVER ENTRY POINT ==========

if __name__ == "__main__":
    port = int(os.getenv("MCP_PORT", 8000))
    host = os.getenv("MCP_HOST", "0.0.0.0")

    logger.info(f"MCP SSE server starting on {host}:{port}")
    logger.info(f"Direct access: http://localhost:{port}/sse")
    logger.info(f"Via Caddy+Tailscale: http://<your-tailscale-ip>/sse")
    logger.info(f"Via MagicDNS+Caddy: https://<hostname>.<tailnet>.ts.net/sse")
    logger.info(f"MCP tools: search_web, scrape_url, get_domains, clean_database")

    mcp.run(transport="sse", host=host, port=port)
