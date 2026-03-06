"""MCP Server with SSE transport for Tailscale/HTTP access

This allows Claude Desktop or other MCP clients to connect over HTTP/SSE
instead of stdio. Perfect for remote access via Tailscale.

Tools:
- search_web: Search using multiple search engines
- scrape_url: Scrape URL with automatic method selection
- get_domains: List tracked domains with preferred methods
- clean_database: Clear all domain tracking data

Documentation (via mcpdoc, namespaced as "docs_"):
- docs_list_doc_sources: List available documentation libraries
- docs_fetch_docs: Fetch documentation from llms.txt sources
"""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlparse

from fastmcp import FastMCP, Context
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware.caching import ResponseCachingMiddleware
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.exceptions import ToolError
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from loguru import logger
from pydantic import Field, HttpUrl, ValidationError
import os


# ========== CONFIGURATION ==========

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

# Cache TTL: 5 minutes for search results, 1 hour for scraped content
SEARCH_CACHE_TTL = int(os.getenv("SEARCH_CACHE_TTL", 300))
SCRAPE_CACHE_TTL = int(os.getenv("SCRAPE_CACHE_TTL", 3600))


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
    from .services.content_cleaner import get_content_cleaner
    from .services.cache_service import get_cache_service
    from .db.database import Database

    logger.info("Initializing services...")
    search_service = get_search_service()
    scrape_service = get_scrape_service()
    cleaner = get_content_cleaner()
    cache = await get_cache_service()
    db = Database()
    await db.init()

    try:
        yield {
            "search_service": search_service,
            "scrape_service": scrape_service,
            "cleaner": cleaner,
            "cache": cache,
            "db": db,
        }
    finally:
        logger.info("Shutting down services...")
        await search_service.close()
        await scrape_service.close()
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
        "Provides web search and URL scraping tools. "
        "Use search_web to find information and scrape_url to extract content from pages. "
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
if redis_store:
    try:
        mcp.add_middleware(ResponseCachingMiddleware(
            cache_storage=redis_store,
            call_tool_settings={"enabled": True, "ttl": SCRAPE_CACHE_TTL},
            list_tools_settings={"enabled": True, "ttl": SEARCH_CACHE_TTL},
        ))
        logger.info(f"Response caching enabled (tools: {SCRAPE_CACHE_TTL}s, list: {SEARCH_CACHE_TTL}s)")
    except Exception as e:
        logger.warning(f"Failed to add caching middleware: {e}")


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


# ========== DOCUMENTATION TOOLS (Native Implementation) ==========

DOCS_CONFIG_PATH = os.getenv("DOCS_CONFIG_PATH", "/app/docs_config.yaml")
DOCS_CACHE_TTL = int(os.getenv("DOCS_CACHE_TTL", SCRAPE_CACHE_TTL))  # 1 hour default
DOCS_LOCAL_DIR = os.getenv("DOCS_LOCAL_DIR", "/app/docs_local")  # Directory for local llms.txt files


def _is_http_or_https(url: str) -> bool:
    """Check if the URL is an HTTP or HTTPS URL."""
    return url.startswith(("http://", "https://"))


def _normalize_path(path: str) -> str:
    """Accept paths in file:/// or relative format and map to absolute paths."""
    return (
        os.path.abspath(path[7:])
        if path.startswith("file://")
        else os.path.abspath(path)
    )


def _extract_domain(url: str) -> str:
    """Extract root domain from URL for allowlist checking.

    Returns the root domain that should be used for fencing:
    - 'python.langchain.com' → 'langchain.com' (allows all subdomains)
    - 'langchain-ai.github.io' → 'langchain-ai.github.io' (github.io is public suffix)
    - 'nextjs.org' → 'nextjs.org'

    This allows fetching from any subdomain of the configured source.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    netloc = parsed.netloc
    # Remove port if present
    if ":" in netloc:
        netloc = netloc.split(":")[0]
    # Remove www. prefix
    if netloc.startswith("www."):
        netloc = netloc[4:]

    # Split into parts
    parts = netloc.split(".")

    # Known public suffixes that should be treated as TLDs
    # These are domains where the "effective" TLD is more than one part
    public_suffixes = {
        "github.io", "gitlab.io", "bitbucket.io",
        "vercel.app", "deno.dev", "workers.dev",
        "pages.dev", "r2.dev", "firebaseapp.com",
        "herokuapp.com", "netlify.app",
    }

    # For 3+ part domains, check if the last 2 parts form a public suffix
    if len(parts) >= 3:
        potential_suffix = ".".join(parts[-2:])
        if potential_suffix in public_suffixes:
            # Keep the 3-part domain (e.g., langchain-ai.github.io)
            return ".".join(parts[-3:])

    # For most domains, return the last 2 parts (e.g., langchain.com)
    if len(parts) >= 2:
        return ".".join(parts[-2:])

    return netloc


def _is_url_allowed(url: str, allowed_domains: set[str]) -> bool:
    """Check if a URL is from an allowed domain.

    Allows subdomains of configured domains. For example, if
    'langchain.com' is allowed, then 'docs.langchain.com' is also allowed.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    netloc = parsed.netloc

    # Remove port if present
    if ":" in netloc:
        netloc = netloc.split(":")[0]

    # Remove www. prefix for checking
    check_netloc = netloc[4:] if netloc.startswith("www.") else netloc

    # Check exact match or subdomain match
    for allowed in allowed_domains:
        if check_netloc == allowed or check_netloc.endswith(f".{allowed}"):
            return True

    return False


def _load_docs_sources() -> tuple[dict, set, set]:
    """Load documentation sources from YAML config file.

    Returns:
        Tuple of (name -> url mapping, set of allowed local file paths, set of allowed domains)
    """
    import yaml
    local_sources = []
    remote_sources = []

    try:
        with open(DOCS_CONFIG_PATH, "r") as f:
            sources = yaml.safe_load(f) or []

        for s in sources:
            url_or_path = s.get("llms_txt", "")
            if _is_http_or_https(url_or_path):
                remote_sources.append(s)
            else:
                local_sources.append(s)

        # Build name -> url mapping
        mapping = {}
        for s in remote_sources:
            name = s.get("name", _extract_domain(s["llms_txt"]))
            mapping[name] = s["llms_txt"]

        # Build allowed local files set (for security)
        allowed_local = set()
        for s in local_sources:
            path = _normalize_path(s["llms_txt"])
            name = s.get("name", os.path.basename(path))
            if not os.path.exists(path):
                logger.warning(f"Local docs file not found: {path}")
                continue
            mapping[name] = f"file://{path}"
            allowed_local.add(path)

        # Build allowed domains set (for security - domain fencing)
        allowed_domains = set()
        for s in remote_sources:
            domain = _extract_domain(s["llms_txt"])
            allowed_domains.add(domain)

        return mapping, allowed_local, allowed_domains

    except FileNotFoundError:
        logger.warning(f"Docs config not found: {DOCS_CONFIG_PATH}")
        return {}, set(), set()
    except Exception as e:
        logger.warning(f"Failed to load docs config: {e}")
        return {}, set(), set()
        return {}, set()


def _extract_domain(url: str) -> str:
    """Extract domain from URL for naming."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return parsed.netloc.replace("www.", "").split(".")[0]


@mcp.tool()
async def docs_list_sources(ctx: Context | None = None) -> str:
    """List all available documentation libraries and their llms.txt URLs.

    START HERE to discover which documentation libraries are available.
    This returns a list of llms.txt endpoints that act as indexes to
    documentation content.

    WORKFLOW:
    1. Call this tool first to get available libraries
    2. Call docs_fetch_docs() with a library's llms.txt URL
    3. Read the returned index to find specific documentation URLs
    4. Call docs_fetch_docs() again with those specific URLs to get actual content

    Returns:
        Formatted list of documentation sources with their URLs
    """
    if ctx:
        await ctx.debug("Loading documentation sources")

    sources, _, _ = _load_docs_sources()
    if not sources:
        return "No documentation sources configured."

    lines = []
    for name, url_or_path in sources.items():
        lines.append(f"{name}")
        if url_or_path.startswith("file://"):
            lines.append(f"  Path: {url_or_path[7:]}")  # Strip file:// prefix
        else:
            lines.append(f"  URL: {url_or_path}")
    return "\n".join(lines)


@mcp.tool()
async def docs_fetch_docs(
    url: Annotated[str, Field(description="The documentation URL to fetch. Use URLs from docs_list_sources or links found in llms.txt files.")],
    use_cache: Annotated[bool, Field(description="Whether to use cached content if available")] = True,
    ctx: Context | None = None
) -> str:
    """Fetch documentation from a URL and convert to clean Markdown.

    CRITICAL WORKFLOW - This is a TWO-STEP process:

    1. FIRST CALL: Fetch the llms.txt URL (from docs_list_sources). This returns
       an INDEX of markdown links, not the actual documentation.

    2. READ THE INDEX: The returned markdown contains links like:
       - [Introduction](https://docs.example.com/intro)
       - [API Reference](https://docs.example.com/api)

    3. SECOND CALL: Call this tool AGAIN with the specific documentation URL
       (e.g., https://docs.example.com/intro) to get the actual content.

    If you only call this tool once with an llms.txt URL, you will NOT have
    the actual documentation - just a list of links. You MUST call it again
    with the specific page URLs.

    Args:
        url: The documentation URL to fetch. Can be:
            - llms.txt URL (returns index of links)
            - Specific documentation page URL (returns actual content)
            - Local file path (must be configured in docs_config.yaml)
        use_cache: Whether to use cached content if available (default: true)

    Returns:
        Clean Markdown content from the documentation source

    Note:
        Results are cached for {DOCS_CACHE_TTL}s. Cached content returns
        instantly without re-fetching from the remote server.

    Security:
        Domain fencing is enabled - only URLs from configured documentation
        sources and their subdomains are allowed. This prevents fetching from
        internal services or arbitrary URLs.
    """
    # Get allowed local files and domains for security
    _, allowed_local_files, allowed_domains = _load_docs_sources()
    url_or_path = url.strip()

    # Handle local file paths
    if not _is_http_or_https(url_or_path):
        # Normalize the path (handles file:// and direct paths)
        abs_path = _normalize_path(url_or_path)

        # Security check: file must be in allowed list
        if abs_path not in allowed_local_files:
            raise ToolError(
                f"Local file not allowed: {abs_path}. "
                f"Allowed files are those listed in docs_config.yaml."
            )

        if ctx:
            await ctx.info(f"Reading local file: {abs_path}")

        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()

            # If it's already markdown, return as-is
            if abs_path.endswith((".md", ".markdown", ".txt")):
                markdown = content
            # If it's HTML, clean it
            elif abs_path.endswith((".html", ".htm")):
                cleaner = ctx.lifespan_context.get("cleaner")
                if not cleaner:
                    raise ToolError("Content cleaner service not available")
                markdown = cleaner.clean(content, url=url_or_path)
            else:
                # Try to detect - if it looks like HTML, clean it
                if content.strip().startswith("<"):
                    cleaner = ctx.lifespan_context.get("cleaner")
                    if not cleaner:
                        raise ToolError("Content cleaner service not available")
                    markdown = cleaner.clean(content, url=url_or_path)
                else:
                    markdown = content

            if ctx:
                word_count = len(markdown.split())
                await ctx.info(f"Read {word_count} words from local file")

            return markdown

        except FileNotFoundError:
            raise ToolError(f"Local file not found: {abs_path}")
        except Exception as e:
            raise ToolError(f"Error reading local file: {str(e)}")

    # Handle HTTP/HTTPS URLs with domain fencing
    if ctx:
        await ctx.info(f"Fetching documentation: {url}")

    # Security: Domain fencing - check if URL is from allowed domain
    if not _is_url_allowed(url, allowed_domains):
        # Extract the domain from the requested URL for the error message
        from urllib.parse import urlparse
        requested_domain = urlparse(url).netloc
        raise ToolError(
            f"URL not allowed: {url} is from domain '{requested_domain}'. "
            f"Documentation fetches are restricted to configured sources only. "
            f"Allowed domains: {', '.join(sorted(allowed_domains))}"
        )

    # Get services from lifespan context with safe access
    cleaner = ctx.lifespan_context.get("cleaner")
    cache = ctx.lifespan_context.get("cache")

    if not cleaner:
        raise ToolError("Content cleaner service not available")

    # Check cache first
    if use_cache and cache:
        cached = await cache.get_scrape(url)
        if cached:
            if ctx:
                await ctx.debug("Returning cached documentation")
            return cached.get("content", "Error: cached content invalid")

    # Fetch the documentation
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            if ctx:
                await ctx.debug(f"Sending HTTP GET to {url}")

            response = await client.get(url)
            response.raise_for_status()
            html = response.text

        # Use ContentCleaner for better HTML->Markdown conversion
        markdown = cleaner.clean(html, url=url)

        if not markdown:
            raise ToolError(f"No content could be extracted from {url}")

        # Cache the result
        if use_cache and cache:
            await cache.set_scrape(url, {
                "url": url,
                "content": markdown,
                "method_used": "docs",
            })

        if ctx:
            word_count = len(markdown.split())
            await ctx.info(f"Fetched {word_count} words of documentation")

        return markdown

    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 404:
            raise ToolError(f"Documentation not found (404) at {url}")
        elif status == 403:
            raise ToolError(f"Access denied (403) when fetching {url}")
        elif status >= 500:
            raise ToolError(f"Server error ({status}) when fetching {url}")
        else:
            raise ToolError(f"HTTP error {status} when fetching {url}")
    except httpx.TimeoutException:
        raise ToolError(f"Request timed out when fetching {url}")
    except httpx.ConnectError:
        raise ToolError(f"Could not connect to {url} - the server may be down")
    except httpx.RequestError as e:
        raise ToolError(f"Network error fetching {url}: {str(e)}")
    except ToolError:
        raise  # Re-raise ToolError as-is (user-facing message)
    except Exception as e:
        logger.error(f"Unexpected error fetching {url}: {e}")
        raise ToolError(f"Unexpected error when processing {url}")


# ========== MCP TOOLS ==========

@mcp.tool()
async def search_web(
    query: Annotated[str, Field(
        description="Search query string",
        min_length=1,
        max_length=500
    )],
    pages: Annotated[int, Field(
        description="Number of search result pages to fetch (1-10)",
        ge=1,
        le=10
    )] = 10,
    exclude_blacklist: Annotated[bool, Field(
        description="Exclude blacklisted domains from results"
    )] = True,
    ctx: Context | None = None
) -> dict:
    """Search the web using multiple search engines

    Args:
        query: Search query string (1-500 characters)
        pages: Number of search result pages to fetch, 1-10 (default: 10)
        exclude_blacklist: Exclude blacklisted domains from results

    Returns:
        Dictionary with query, total_results, and list of results

    Note:
        Results are cached for {SEARCH_CACHE_TTL}s. Cached results will return
        instantly without re-querying search engines.
    """
    if ctx:
        await ctx.info(f"Searching for: {query}")

    # Get services from lifespan context with safe access
    search_svc = ctx.lifespan_context.get("search_service")
    if not search_svc:
        raise ToolError("Search service not available")

    result = await search_svc.search(
        query=query,
        pages=pages,
        exclude_blacklist=exclude_blacklist
    )

    if ctx:
        await ctx.info(f"Found {result.total_results} results")
        if result.cached:
            await ctx.debug("Returned cached results")

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
    url: Annotated[str, Field(description="URL to scrape")],
    method: Annotated[Literal["crawl4ai", "selenium", "pdf"] | None, Field(
        description="Force specific scraping method"
    )] = None,
    css_selector: Annotated[str | None, Field(
        description="Optional CSS selector for targeted content extraction"
    )] = None,
    ctx: Context | None = None
) -> dict:
    """Scrape a URL and extract clean markdown content

    The server learns which scraping method works best per domain and
    automatically uses it on future requests.

    Args:
        url: URL to scrape (must be a valid HTTP/HTTPS URL)
        method: Force specific scraping method (crawl4ai, selenium, pdf)
        css_selector: Optional CSS selector for targeted content extraction

    Returns:
        Dictionary with success status, title, content, and metadata

    Note:
        Results are cached for {SCRAPE_CACHE_TTL}s. Cached scrapes return
        instantly without re-downloading the page.
    """
    # Validate URL
    if not url.startswith(("http://", "https://", "file://")):
        raise ToolError("URL must start with http://, https://, or file://")

    if ctx:
        await ctx.info(f"Scraping: {url}")

    from .models.unified import ScrapeRequest, ScrapingMethod

    # Get services from lifespan context with safe access
    scrape_svc = ctx.lifespan_context.get("scrape_service")
    if not scrape_svc:
        raise ToolError("Scrape service not available")

    try:
        request = ScrapeRequest(
            url=url,
            force_method=ScrapingMethod(method) if method else None,
            css_selector=css_selector
        )
    except ValueError as e:
        raise ToolError(f"Invalid scraping method: {e}")

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
        # Don't raise ToolError here - the result indicates success=False
        # The LLM can see the error field and decide what to do

    return response


@mcp.tool()
async def get_domains(ctx: Context | None = None) -> dict:
    """List all tracked domains with their preferred scraping methods

    Returns:
        Dictionary with total count and list of domain records

    Note:
        This data is managed in PostgreSQL and is not cached.
    """
    if ctx:
        await ctx.debug("Fetching all tracked domains")

    # Get database from lifespan context with safe access
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


@mcp.tool()
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
    # Get database from lifespan context with safe access
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


# ========== SERVER ENTRY POINT ==========

if __name__ == "__main__":
    port = int(os.getenv("MCP_PORT", 8000))
    host = os.getenv("MCP_HOST", "0.0.0.0")

    logger.info(f"MCP SSE server starting on {host}:{port}")
    logger.info(f"Direct access: http://localhost:{port}/sse")
    logger.info(f"Via Caddy+Tailscale: http://<your-tailscale-ip>/sse")
    logger.info(f"Via MagicDNS+Caddy: https://<hostname>.<tailnet>.ts.net/sse")
    logger.info(f"Session state: Redis @ {REDIS_HOST}:{REDIS_PORT}")
    logger.info(f"Caching: enabled (search: {SEARCH_CACHE_TTL}s, scrape: {SCRAPE_CACHE_TTL}s)")
    logger.info(f"Web tools: search_web, scrape_url, get_domains, clean_database")
    logger.info(f"Docs tools: docs_list_sources, docs_fetch_docs")

    mcp.run(transport="sse", host=host, port=port)
