"""MCP Server with SSE transport for Tailscale/HTTP access

This allows Claude Desktop or other MCP clients to connect over HTTP/SSE
instead of stdio. Perfect for remote access via Tailscale.

Tools:
- search_web: Search using multiple search engines
- scrape_url: Scrape URL with automatic method selection
- get_domains: List tracked domains with preferred methods
- clean_database: Clear all domain tracking data
"""

import os
from json import dumps
from loguru import logger

from fastmcp import FastMCP
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from .services.search_service import get_search_service
from .services.scrape_service import get_scrape_service
from .db.database import get_db
from .models.unified import ScrapeRequest, ScrapingMethod


# Create FastMCP instance
mcp = FastMCP("mcp-research-server")

# Configure CORS
allowed_origins = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]

# Wrap http_app to add CORS and health check
_original_http_app = mcp.http_app


def http_app_with_middleware(**kwargs):
    """Add CORS and health check to the underlying Starlette app"""
    app = _original_http_app(**kwargs)

    # Add CORS if not already present
    if not any(m.cls == CORSMiddleware for m in app.user_middleware):
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Add health check endpoint
    async def health_check(request):
        return JSONResponse({"status": "healthy", "server": "mcp-research-server"})

    app.add_route("/health", health_check, methods=["GET"])
    return app


mcp.http_app = http_app_with_middleware


# ========== MCP TOOLS ==========

@mcp.tool()
async def search_web(query: str, pages: int = 10, exclude_blacklist: bool = True) -> str:
    """Search the web using multiple search engines (Brave, Bing, DuckDuckGo, Ask)"""
    search_service = get_search_service()
    result = await search_service.search(
        query=query,
        pages=pages,
        exclude_blacklist=exclude_blacklist
    )
    return dumps({
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
    }, indent=2)


@mcp.tool()
async def scrape_url(url: str, method: str = None, css_selector: str = None) -> str:
    """Scrape a URL and extract clean markdown content. Learns which method works best per domain."""
    scrape_service = get_scrape_service()

    request = ScrapeRequest(
        url=url,
        force_method=ScrapingMethod(method) if method else None,
        css_selector=css_selector
    )
    result = await scrape_service.scrape(request)

    return dumps({
        "url": result.url,
        "success": result.success,
        "method_used": result.method_used.value if result.method_used else None,
        "title": result.title,
        "content": result.content,
        "word_count": result.metadata.get("word_count", 0) if result.metadata else 0,
        "error": result.error
    }, indent=2)


@mcp.tool()
async def get_domains() -> str:
    """List all tracked domains with their preferred scraping methods"""
    db = await get_db()
    domains = await db.get_all_domains()
    return dumps({"total": len(domains), "domains": domains}, indent=2)


@mcp.tool()
async def clean_database() -> str:
    """Clear all domain tracking data"""
    db = await get_db()
    count = await db.clean()
    return dumps({"status": "success", "records_removed": count}, indent=2)


# ========== SERVER ENTRY POINT ==========

if __name__ == "__main__":
    port = int(os.getenv("MCP_PORT", 8000))
    host = os.getenv("MCP_HOST", "0.0.0.0")
    logger.info(f"MCP SSE server starting on {host}:{port}")
    logger.info(f"Connect via Tailscale: http://<your-tailscale-ip>:{port}/sse")
    logger.info(f"MCP tools: search_web, scrape_url, get_domains, clean_database")

    mcp.run(transport="sse", host=host, port=port)
