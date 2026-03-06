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

from fastmcp import FastMCP, Context
from fastmcp.server.lifespan import lifespan
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


# ========== LIFESPAN ==========

@lifespan
async def service_lifespan(server: FastMCP):
    """Initialize and cleanup services on server startup/shutdown

    Yields a dict with services that becomes accessible via ctx.lifespan_context
    """
    from .services.search_service import get_search_service
    from .services.scrape_service import get_scrape_service
    from .db.database import Database

    logger.info("Initializing services...")
    search_service = get_search_service()
    scrape_service = get_scrape_service()
    db = Database()
    await db.init()

    # Yield lifespan context - accessible from tools via ctx.lifespan_context
    try:
        yield {
            "search_service": search_service,
            "scrape_service": scrape_service,
            "db": db,
        }
    finally:
        logger.info("Shutting down services...")
        await search_service.close()
        await scrape_service.close()
        await db.close()
        logger.info("Shutdown complete")


# ========== FASTMCP SERVER ==========

mcp = FastMCP(
    name="mcp-research-server",
    instructions=(
        "Provides web search and URL scraping tools. "
        "Use search_web to find information and scrape_url to extract content from pages. "
        "The server learns which scraping method works best for each domain."
    ),
    lifespan=service_lifespan
)


# ========== CORS & HEALTH CHECK ==========

_original_http_app = mcp.http_app


def http_app_with_middleware(**kwargs):
    """Add CORS, SSE headers, and health check to the underlying Starlette app"""
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
    if ctx:
        await ctx.info(f"Searching for: {query}")

    # Get services from lifespan context
    search_svc = ctx.lifespan_context["search_service"]

    result = await search_svc.search(
        query=query,
        pages=pages,
        exclude_blacklist=exclude_blacklist
    )

    if ctx:
        await ctx.info(f"Found {result.total_results} results")

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
    if ctx:
        await ctx.info(f"Scraping: {url}")

    from .models.unified import ScrapeRequest, ScrapingMethod

    # Get services from lifespan context
    scrape_svc = ctx.lifespan_context["scrape_service"]

    request = ScrapeRequest(
        url=url,
        force_method=ScrapingMethod(method) if method else None,
        css_selector=css_selector
    )
    result = await scrape_svc.scrape(request)

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
            await ctx.error(f"Scrape failed: {result.error}")

    return response


@mcp.tool()
async def get_domains(ctx: Context | None = None) -> dict:
    """List all tracked domains with their preferred scraping methods

    Returns:
        Dictionary with total count and list of domain records
    """
    if ctx:
        await ctx.debug("Fetching all tracked domains")

    # Get database from lifespan context
    db = ctx.lifespan_context["db"]
    domains = await db.get_all_domains()

    if ctx:
        await ctx.debug(f"Retrieved {len(domains)} domains")

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
    # Get database from lifespan context
    db = ctx.lifespan_context["db"]
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
