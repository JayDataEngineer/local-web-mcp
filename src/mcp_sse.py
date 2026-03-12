"""MCP Server with Streamable HTTP transport for Tailscale/HTTP access

This allows Claude Desktop or other MCP clients to connect over HTTP
instead of stdio. Perfect for remote access via Tailscale.

Web Research Tools:
- search_web: Search using multiple search engines
- scrape_url: Scrape URL with automatic method selection
- map_domain: Discover URLs from sitemaps/Common Crawl
- crawl_site: Deep crawl with BFS strategy
- scrape_structured: Extract structured JSON data using pre-built schemas (NEW)
- list_schemas: List available extraction schemas (NEW)
- get_domains: List tracked domains with preferred methods
- clear_blacklist: Clear all blacklisted domains (unblock them)
- clean_database: Clear all domain tracking data

Documentation (via mcpdoc, namespaced as "docs_"):
- docs_list_doc_sources: List available documentation libraries
- docs_fetch_docs: Fetch documentation from llms.txt sources
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional
from urllib.parse import urlparse

from fastmcp import FastMCP, Context
from fastmcp.server.lifespan import lifespan
from fastmcp.server.providers import LocalProvider
from fastmcp.server.middleware.caching import ResponseCachingMiddleware
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.exceptions import ToolError
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from loguru import logger
from pydantic import Field, HttpUrl, ValidationError, field_validator, BeforeValidator
import json
import os


# ========== CONFIGURATION ==========

from .settings import get_settings

settings = get_settings()

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in settings.allowed_origins.split(",")
    if origin.strip()
]

REDIS_HOST = settings.redis_host
REDIS_PORT = settings.redis_port
REDIS_PASSWORD = settings.redis_password

# Cache TTL from settings
SEARCH_CACHE_TTL = settings.search_cache_ttl
SCRAPE_CACHE_TTL = settings.scrape_cache_ttl


# ========== REDIS STORE SETUP ==========

def _create_redis_store():
    """Create namespaced Redis store for FastMCP state and caching"""
    from key_value.aio.stores.redis import RedisStore
    from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper

    base_store = RedisStore(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        db=0,  # Use database 0 for FastMCP state
    )

    # Namespace to avoid conflicts with other apps using same Redis
    return PrefixCollectionsWrapper(
        key_value=base_store,
        prefix="mcp-server"
    )


# ========== LIFESPAN ==========

@lifespan
async def service_lifespan(server: FastMCP):
    """Initialize and cleanup services on server startup/shutdown

    Yields a dict with services that becomes accessible via ctx.lifespan_context
    """
    from .services.search_service import get_search_service
    from .services.scrape_service import get_scrape_service
    from .services.crawl_service import get_map_crawl_service
    from .services.content_cleaner import get_content_cleaner
    from .db.database import get_db

    logger.info("Initializing services...")
    search_service = get_search_service()
    scrape_service = get_scrape_service()
    crawl_service = get_map_crawl_service()
    cleaner = get_content_cleaner()
    db = await get_db()

    try:
        yield {
            "search_service": search_service,
            "scrape_service": scrape_service,
            "crawl_service": crawl_service,
            "cleaner": cleaner,
            "db": db,
        }
    finally:
        logger.info("Shutting down services...")
        await search_service.close()
        await scrape_service.close()
        await crawl_service.close()
        await db.close()
        logger.info("Shutdown complete")


# ========== FASTMCP SERVER ==========

# Create Redis store for session state and caching
redis_store = None
try:
    redis_store = _create_redis_store()
except ImportError:
    logger.warning("py-key-value-aio[redis] not available, using in-memory session state")
except Exception as e:
    logger.warning(f"Failed to initialize Redis store: {e}")

mcp = FastMCP(
    name="mcp-research-server",
    instructions=(
        "Provides web research tools: search_web, scrape_url, map_domain, crawl_site, scrape_structured. "
        "Use search_web to find information, scrape_url for single pages, "
        "map_domain to discover URLs from sitemaps, crawl_site for deep crawling, "
        "and scrape_structured for schema-based JSON extraction. "
        "The server learns which scraping method works best for each domain."
    ),
    lifespan=service_lifespan,
    session_state_store=redis_store,
    mask_error_details=True,  # Hide internal errors from clients for security
)

# Add middleware in order: error handling first, then caching
mcp.add_middleware(ErrorHandlingMiddleware(
    include_traceback=True,  # Log full tracebacks server-side
    transform_errors=True,    # Convert exceptions to MCP errors
))

# Add response caching middleware with Redis backend
# Note: Streaming endpoints are automatically excluded by FastMCP
# Note: Using explicit allowlist for tools - dynamic operations (map_domain, crawl_site)
#       are excluded to ensure fresh data on each call
if redis_store:
    try:
        mcp.add_middleware(ResponseCachingMiddleware(
            cache_storage=redis_store,
            call_tool_settings={
                "enabled": True,
                "ttl": SCRAPE_CACHE_TTL,
                "included_tools": [  # Explicit allowlist - cache only stable operations
                    "search_web",
                    "scrape_url",
                    "scrape_structured",
                    "docs_fetch_docs",
                    "docs_list_sources",
                    "list_schemas",
                    "get_domains",
                    "clear_blacklist",
                ],
                # Excluded from caching (fresh results each call):
                # - map_domain: Sitemaps change frequently, need fresh discovery
                # - crawl_site: Dynamic link discovery, content changes
                # - clean_database: Must always execute
            },
            list_tools_settings={"enabled": True, "ttl": SEARCH_CACHE_TTL},
        ))
        cached_tools = ", ".join([
            "search_web", "scrape_url", "scrape_structured",
            "docs_fetch_docs", "docs_list_sources", "list_schemas", "get_domains", "clear_blacklist"
        ])
        logger.info(f"Response caching enabled ({SCRAPE_CACHE_TTL}s)")
        logger.info(f"Cached tools: {cached_tools}")
        logger.info(f"Uncached: map_domain, crawl_site, clean_database (always fresh)")
    except Exception as e:
        logger.warning(f"Failed to add caching middleware: {e}")


# ========== CORS & HEALTH CHECK ==========

_original_http_app = mcp.http_app


def http_app_with_middleware(**kwargs):
    """Add CORS and health check to the underlying Starlette app

    Note: We don't add custom streaming headers middleware because:
    1. FastMCP already handles HTTP streaming headers correctly
    2. Custom middleware on top of streaming causes buffering issues
    """
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

    # Add health check endpoint
    async def health_check(request):
        return JSONResponse({"status": "healthy", "server": "mcp-research-server"})

    app.add_route("/health", health_check, methods=["GET"])
    return app


mcp.http_app = http_app_with_middleware


# ========== TOOL REGISTRATION ==========

# Import tool functions from modular structure
from .tools.web_tools import search_web, scrape_url, scrape_structured, list_schemas
from .tools.crawl_tools import map_domain, crawl_site
from .tools.docs_tools import docs_list_sources, docs_fetch_docs
from .tools.admin_tools import get_domains, clean_database, clear_blacklist

# Register all tools with FastMCP
mcp.add_tool(search_web)
mcp.add_tool(scrape_url)
mcp.add_tool(scrape_structured)
mcp.add_tool(list_schemas)
mcp.add_tool(map_domain)
mcp.add_tool(crawl_site)
mcp.add_tool(docs_list_sources)
mcp.add_tool(docs_fetch_docs)
mcp.add_tool(get_domains)
mcp.add_tool(clear_blacklist)
mcp.add_tool(clean_database)


# ========== SERVER ENTRY POINT ==========

if __name__ == "__main__":
    port = settings.port
    host = settings.host

    logger.info(f"MCP HTTP server starting on {host}:{port}")
    logger.info(f"Direct access: http://localhost:{port}/mcp")
    logger.info(f"Via Caddy+Tailscale: http://<your-tailscale-ip>/mcp")
    logger.info(f"Via MagicDNS+Caddy: https://<hostname>.<tailnet>.ts.net/mcp")
    logger.info(f"Session state: Redis @ {REDIS_HOST}:{REDIS_PORT}")
    logger.info(f"Caching: enabled (search: {SEARCH_CACHE_TTL}s, scrape: {SCRAPE_CACHE_TTL}s)")
    logger.info(f"Web tools: search_web, scrape_url, map_domain, crawl_site, scrape_structured, list_schemas, get_domains, clear_blacklist, clean_database")
    logger.info(f"Docs tools: docs_list_sources, docs_fetch_docs")

    mcp.run(transport="http", host=host, port=port)
