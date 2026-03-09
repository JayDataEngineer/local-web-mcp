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
    from .services.crawl_service import get_map_crawl_service
    from .services.content_cleaner import get_content_cleaner
    from .services.cache_service import get_cache_service
    from .db.database import Database

    logger.info("Initializing services...")
    search_service = get_search_service()
    scrape_service = get_scrape_service()
    crawl_service = get_map_crawl_service()
    cleaner = get_content_cleaner()
    cache = await get_cache_service()
    db = Database()
    await db.init()

    try:
        yield {
            "search_service": search_service,
            "scrape_service": scrape_service,
            "crawl_service": crawl_service,
            "cleaner": cleaner,
            "cache": cache,
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


# Session-level storage for dynamically discovered domains
_session_allowed_domains: set[str] = set()

def _add_domains_from_content(content: str, base_url: str) -> None:
    """Extract domains from markdown links and add to session allowlist."""
    import re
    from urllib.parse import urlparse

    # Extract markdown links: [text](url) or [text](url "title")
    link_pattern = r'\[([^\]]+)\]\(([^)]+)\)'
    base_parsed = urlparse(base_url)
    base_domain = base_parsed.netloc.replace("www.", "")

    for match in re.finditer(link_pattern, content):
        url = match.group(2).split()[0]  # Remove trailing "title" if present
        try:
            parsed = urlparse(url)
            if parsed.netloc:
                domain = parsed.netloc.replace("www.", "")
                # Only add external domains (not the same as base)
                if domain != base_domain and domain not in _session_allowed_domains:
                    _session_allowed_domains.add(domain)
                    logger.debug(f"Added discovered domain to allowlist: {domain}")
        except:
            pass


def _is_url_allowed(url: str, configured_domains: set[str]) -> bool:
    """Check if a URL is from an allowed domain.

    Allows:
    - Exact matches with configured domains
    - Subdomains of configured domains
    - Session-discovered domains (from llms.txt content)
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    netloc = parsed.netloc

    # Remove port if present
    if ":" in netloc:
        netloc = netloc.split(":")[0]

    # Remove www. prefix for checking
    check_netloc = netloc[4:] if netloc.startswith("www.") else netloc

    # Combine configured + session-discovered domains
    all_allowed = configured_domains | _session_allowed_domains

    # Check exact match or if requested domain is a subdomain of allowed
    for allowed in all_allowed:
        # Direct match
        if check_netloc == allowed:
            return True
        # Requested URL is a subdomain of allowed domain
        if check_netloc.endswith(f".{allowed}"):
            return True
        # Allowed domain is a subdomain of requested URL (for base domain matching)
        if allowed.endswith(f".{check_netloc}"):
            return True

    return False


def _is_url_blacklisted(url: str) -> bool:
    """Check if a URL is blacklisted for security reasons.

    Blocks access to:
    - localhost and loopback addresses
    - Private network IPs (RFC 1918)
    - Link-local addresses
    - AWS metadata service
    - Other internal services

    Returns True if the URL should be blocked.
    """
    from urllib.parse import urlparse
    import ipaddress

    try:
        parsed = urlparse(url)
        netloc = parsed.netloc

        # Remove port if present
        if ":" in netloc:
            netloc = netloc.split(":")[0]

        # Remove www. prefix for hostname checking
        hostname = netloc[4:] if netloc.startswith("www.") else netloc

        # Block localhost variants
        blocked_hostnames = {
            "localhost",
            "127.0.0.1",
            "0.0.0.0",
            "::1",
            "ip6-localhost",
            "ip6-loopback",
        }
        if hostname.lower() in blocked_hostnames:
            return True

        # Block AWS metadata service
        if hostname == "169.254.169.254":
            return True

        # Try to parse as IP address
        try:
            ip = ipaddress.ip_address(hostname)

            # Block private IP ranges (RFC 1918)
            if ip.is_private:
                return True

            # Block link-local addresses
            if ip.is_link_local:
                return True

            # Block reserved addresses
            if ip.is_reserved:
                return True

            # Block loopback addresses (in case hostname was an IP)
            if ip.is_loopback:
                return True

        except ValueError:
            # Not an IP address, continue checking
            pass

        # Block internal TLDs
        if hostname.endswith(".local") or hostname.endswith(".internal"):
            return True

        return False

    except Exception:
        # If we can't parse the URL, err on the side of caution and block
        return True


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
    """Extract full domain from URL for naming and domain fencing."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    # Return full domain without www prefix
    # e.g., "langchain-ai.github.io" not "langchain-ai"
    # e.g., "python.langchain.com" not "python"
    return parsed.netloc.replace("www.", "")


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

        # Extract domains from links in the content and add to session allowlist
        _add_domains_from_content(markdown, url)

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
    top_k: Annotated[int | None, Field(
        description="Maximum number of results to return (None = all results)"
    )] = None,
    rerank: Annotated[bool, Field(
        description="Apply flash re-ranking based on query relevance"
    )] = False,
    time_filter: Annotated[Literal["day", "week", "month", "year"] | None, Field(
        description="Filter results by time: day (24h), week (7d), month (30d), year (365d)"
    )] = None,
    ctx: Context | None = None
) -> dict:
    """Search the web using multiple search engines

    Args:
        query: Search query string (1-500 characters)
        pages: Number of search result pages to fetch, 1-10 (default: 10)
        exclude_blacklist: Exclude blacklisted domains from results
        top_k: Maximum number of results to return (default: all results)
        rerank: Apply flash re-ranking to prioritize relevant results
        time_filter: Filter by time - day (24h), week (7d), month (30d), year (365d)

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
        exclude_blacklist=exclude_blacklist,
        top_k=top_k,
        rerank=rerank,
        time_filter=time_filter
    )

    if ctx:
        await ctx.info(f"Found {result.total_results} results")
        if rerank:
            await ctx.info("Results re-ranked by query relevance")
        if time_filter:
            await ctx.info(f"Filtered by time: {time_filter}")
        if result.cached:
            await ctx.debug("Returned cached results")

    return {
        "query": query,
        "total_results": result.total_results,
        "pages_scraped": result.pages_scraped,
        "engines": result.engines,
        "search_time_ms": result.search_time_ms,
        "cached": result.cached,
        "reranked": rerank,
        "time_filter": time_filter,
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
    text_only: Annotated[bool, Field(
        description="Disable images for faster loading (Crawl4AI only)"
    )] = False,
    ctx: Context | None = None
) -> dict:
    """Scrape a URL and extract clean markdown content

    The server learns which scraping method works best per domain and
    automatically uses it on future requests.

    Args:
        url: URL to scrape (must be a valid HTTP/HTTPS URL)
        method: Force specific scraping method (crawl4ai, selenium, pdf)
        css_selector: Optional CSS selector for targeted content extraction
        text_only: Disable images for faster loading (Crawl4AI only)

    Returns:
        Dictionary with success status, title, content, and metadata

    Note:
        Results are cached for {SCRAPE_CACHE_TTL}s. Cached scrapes return
        instantly without re-downloading the page.

    Security:
        Internal and private IPs are blocked (localhost, 127.0.0.1, 10.*,
        172.16-31.*, 192.168.*, 169.254.*). Only public URLs can be scraped.
    """
    # Validate URL
    if not url.startswith(("http://", "https://", "file://")):
        raise ToolError("URL must start with http://, https://, or file://")

    # Security: Check for blacklisted URLs (internal/private IPs)
    if url.startswith(("http://", "https://")) and _is_url_blacklisted(url):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        raise ToolError(
            f"URL is not allowed for security reasons: {parsed.netloc} "
            f"appears to be a private or internal address. "
            f"Only public URLs can be scraped."
        )

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
            css_selector=css_selector,
            text_only=text_only
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


@mcp.tool()
async def map_domain(
    domain: Annotated[str, Field(
        description="Domain to map (e.g., 'example.com' or 'https://example.com')",
        min_length=3
    )],
    source: Annotated[Literal["sitemap", "cc", "sitemap+cc"], Field(
        description="URL source: sitemap (fast), cc (Common Crawl), or sitemap+cc (both)"
    )] = "sitemap+cc",
    pattern: Annotated[str, Field(
        description="URL pattern filter (e.g., '*/blog/*' for blog posts, '*' for all)"
    )] = "*",
    max_urls: Annotated[int, Field(
        description="Maximum URLs to return (1-10000)"
    )] = 1000,
    extract_head: Annotated[bool, Field(
        description="Extract metadata from <head> section (slower but richer)"
    )] = False,
    query: Annotated[str | None, Field(
        description="Optional search query for BM25 relevance scoring"
    )] = None,
    score_threshold: Annotated[float | None, Field(
        description="Minimum BM25 relevance score (0.0-1.0) when using query"
    )] = None,
    ctx: Context | None = None
) -> dict:
    """Discover URLs from a domain using sitemaps or Common Crawl

    This is a URL DISCOVERY tool - it finds URLs without crawling them.
    Use this BEFORE scraping to understand a site's structure.

    WORKFLOW:
    1. Call map_domain to discover URLs (e.g., all blog posts)
    2. Filter URLs by pattern, metadata, or relevance score
    3. Call scrape_url or crawl_site on selected URLs

    USE CASES:
    - Find all documentation pages: pattern="*/docs/*"
    - Discover blog posts: pattern="*/blog/*"
    - Find product pages: pattern="*/product/*"
    - Relevance search: query="python tutorial" with score_threshold=0.3

    SOURCES:
    - sitemap: Fast XML sitemap parsing (100-1000 URLs/second)
    - cc: Common Crawl dataset (50-500 URLs/second)
    - sitemap+cc: Both sources for maximum coverage

    Returns:
        Dictionary with domain, total URLs found, and list of URLs with metadata

    Note:
        Results are NOT cached - each call performs fresh discovery.
    """
    from .services.crawl_service import MapConfig

    if ctx:
        await ctx.info(f"Mapping domain: {domain} (source={source})")

    crawl_svc = ctx.lifespan_context.get("crawl_service")
    if not crawl_svc:
        raise ToolError("Crawl service not available")

    config = MapConfig(
        source=source,
        pattern=pattern,
        extract_head=extract_head,
        max_urls=max_urls,
        query=query,
        scoring_method="bm25" if query else None,
        score_threshold=score_threshold,
        filter_nonsense=True,
    )

    result = await crawl_svc.map_domain(domain, config)

    # Format output
    urls_summary = []
    for url_entry in result.urls[:50]:  # Limit output to first 50
        url_info = {"url": url_entry.get("url", "")}
        if url_entry.get("relevance_score") is not None:
            url_info["score"] = round(url_entry["relevance_score"], 3)
        if url_entry.get("head_data"):
            head = url_entry["head_data"]
            if head.get("title"):
                url_info["title"] = head["title"]
            if head.get("meta", {}).get("description"):
                url_info["description"] = head["meta"]["description"][:100]
        urls_summary.append(url_info)

    if ctx:
        await ctx.info(f"Discovered {result.valid_urls} valid URLs (total: {result.total_urls})")

    return {
        "domain": result.domain,
        "source_used": result.source_used,
        "total_urls": result.total_urls,
        "valid_urls": result.valid_urls,
        "urls": urls_summary,
        "_note": f"Showing first {len(urls_summary)} URLs. Use smaller max_urls or pattern filters for targeted discovery." if result.valid_urls > 50 else "",
    }


@mcp.tool()
async def crawl_site(
    url: Annotated[str, Field(
        description="Starting URL to crawl"
    )],
    max_depth: Annotated[int, Field(
        description="Maximum depth to crawl (1-5)"
    )] = 2,
    max_pages: Annotated[int, Field(
        description="Maximum pages to crawl (1-200)"
    )] = 50,
    include_external: Annotated[bool, Field(
        description="Follow links to external domains"
    )] = False,
    pattern: Annotated[str | None, Field(
        description="Optional URL pattern filter (e.g., '*/docs/*')"
    )] = None,
    word_count_threshold: Annotated[int, Field(
        description="Minimum word count for pages (50-1000)"
    )] = 100,
    # Filter chain options
    include_patterns: Annotated[Optional[str], Field(
        description="URL patterns to include (comma-separated: '*api*,*reference*')"
    )] = None,
    exclude_patterns: Annotated[Optional[str], Field(
        description="URL patterns to exclude (comma-separated: '*v1*,*old*')"
    )] = None,
    # Best-First strategy options
    strategy: Annotated[Literal["bfs", "best_first"], Field(
        description="Crawling strategy: bfs (systematic) or best_first (prioritize relevant pages)"
    )] = "bfs",
    keywords: Annotated[Optional[str], Field(
        description="Keywords for relevance scoring (comma-separated: 'api,tutorial')"
    )] = None,
    ctx: Context | None = None
) -> dict:
    """Deep crawl a site following links (BFS or Best-First strategy)

    This is a DEEP CRAWL tool - it discovers and crawls pages by following links.
    Use this AFTER map_domain when you need actual page content.

    WORKFLOW:
    1. Call map_domain to discover URLs (optional but recommended)
    2. Call crawl_site with starting URL to crawl linked pages
    3. Review crawled pages and extract specific URLs of interest

    STRATEGIES:
    - bfs (default): Systematic breadth-first exploration
    - best_first: Prioritize pages matching keywords (requires keywords parameter)

    FILTERING:
    - pattern: Simple URL pattern (e.g., '*/docs/*')
    - include_patterns: Multiple patterns to include (e.g., ['*api*', '*reference*'])
    - exclude_patterns: Multiple patterns to exclude (e.g., ['*deprecated*', '*v1*'])

    USE CASES:
    - Crawl documentation with filters: url="https://docs.example.com", include_patterns=["*api*"]
    - Best-First for specific topics: url="https://docs.example.com", strategy="best_first", keywords=["api", "tutorial"]
    - Exclude old versions: url="https://docs.example.com", exclude_patterns=["*v1*", "*deprecated*"]

    Returns:
        Dictionary with crawl stats and list of crawled pages with content

    Warning:
        Deep crawling is resource-intensive. Start with low max_depth (2)
        and max_pages (20) for testing, then increase as needed.

    Note:
        Results are NOT cached due to dynamic nature of link discovery.
    """
    from .services.crawl_service import CrawlConfig

    # Validate URL
    if not url.startswith(("http://", "https://")):
        raise ToolError("URL must start with http:// or https://")

    # Security: Check for blacklisted URLs
    if _is_url_blacklisted(url):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        raise ToolError(
            f"URL is not allowed for security reasons: {parsed.netloc} "
            f"appears to be a private or internal address."
        )

    # Robust parameter parsing - handle LLMs that serialize lists as JSON strings
    def _parse_flex(value: str | None) -> list[str] | None:
        """Parse flexible list parameter from string.

        Handles:
        - JSON arrays: '["a","b"]' -> ['a', 'b']
        - Comma-separated: 'a,b' -> ['a', 'b']
        - Single value: 'a' -> ['a']
        - None: None
        """
        if value is None:
            return None

        value = value.strip()

        # Try parsing as JSON array first (LLM bug scenario)
        if value.startswith("[") and value.endswith("]"):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return [str(v).strip() for v in parsed if str(v).strip()]
            except json.JSONDecodeError:
                pass

        # Fall back to comma-separated
        if "," in value:
            return [v.strip() for v in value.split(",") if v.strip()]

        # Single value
        return [value]

    include_patterns = _parse_flex(include_patterns)
    exclude_patterns = _parse_flex(exclude_patterns)
    keywords = _parse_flex(keywords)

    if ctx:
        await ctx.info(f"Deep crawling: {url} (strategy={strategy}, max_depth={max_depth}, max_pages={max_pages})")

    crawl_svc = ctx.lifespan_context.get("crawl_service")
    if not crawl_svc:
        raise ToolError("Crawl service not available")

    # Validate best_first requires keywords
    if strategy == "best_first" and not keywords:
        raise ToolError("best_first strategy requires keywords parameter")

    # Ensure word_count_threshold has a valid value
    if word_count_threshold is None:
        word_count_threshold = 100

    config = CrawlConfig(
        max_depth=max_depth,
        max_pages=max_pages,
        include_external=include_external,
        pattern=pattern,
        only_text=True,
        word_count_threshold=word_count_threshold,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        strategy=strategy,
        keywords=keywords,
    )

    result = await crawl_svc.crawl_site(url, config)

    # Format output - limit content size
    pages_summary = []
    for page in result.pages:
        page_info = {
            "url": page["url"],
            "success": page["success"],
            "depth": page.get("depth", 0),
        }
        if page.get("title"):
            page_info["title"] = page["title"]
        if page.get("content"):
            content = page["content"]
            # Truncate long content
            if len(content) > 2000:
                page_info["content"] = content[:2000] + f"... (truncated, was {len(content)} chars)"
                page_info["truncated"] = True
            else:
                page_info["content"] = content
        pages_summary.append(page_info)

    if ctx:
        await ctx.info(f"Crawled {result.total_crawled} pages ({result.successful} successful, {result.failed} failed)")

    # Check for blocking - if all pages failed with block errors, provide clear message
    if result.failed > 0 and result.pages:
        block_errors = [p.get("error") for p in result.pages if p.get("error")]
        if block_errors and any("blocked" in e.lower() or "captcha" in e.lower() or "rate" in e.lower() for e in block_errors if e):
            # Count block types
            blocked_count = sum(1 for e in block_errors if e and ("blocked" in e.lower() or "captcha" in e.lower()))
            rate_limited = sum(1 for e in block_errors if e and "rate" in e.lower())

            if blocked_count > 0 or rate_limited > 0:
                error_msg = "Unable to crawl - "
                if blocked_count > 0:
                    error_msg += f"site is blocking automated crawlers (CAPTCHA/access denied). "
                if rate_limited > 0:
                    error_msg += f"rate limiting detected. "

                error_msg += "Try scrape_url instead which has SeleniumBase fallback."
                if ctx:
                    await ctx.info(error_msg)

                return {
                    "start_url": url,
                    "total_crawled": result.total_crawled,
                    "successful": result.successful,
                    "failed": result.failed,
                    "pages": pages_summary,
                    "block_detected": True,
                    "error_message": error_msg.strip(),
                }

    return {
        "start_url": url,
        "total_crawled": result.total_crawled,
        "successful": result.successful,
        "failed": result.failed,
        "pages": pages_summary,
    }


@mcp.tool()
async def scrape_structured(
    url: Annotated[str, Field(description="URL to scrape with structured extraction")],
    schema_type: Annotated[Literal["ecommerce", "news", "jobs", "blog", "social", "products"], Field(
        description="Pre-built schema type for extraction"
    )] = "ecommerce",
    custom_selector: Annotated[str | None, Field(
        description="Custom CSS selector to override the base selector"
    )] = None,
    bypass_cache: Annotated[bool, Field(
        description="Bypass cache and fetch fresh data"
    )] = True,
    ctx: Context | None = None
) -> dict:
    """Scrape a URL and extract structured data using schema-based extraction

    This tool uses pre-built CSS extraction schemas to extract structured
    JSON data from web pages WITHOUT using LLMs. Much faster and cheaper
    than LLM-based extraction.

    SCHEMA TYPES:
    - ecommerce: Products (name, price, rating, availability, image, url)
    - news: Articles (headline, author, date, content, category, summary)
    - jobs: Listings (title, company, location, salary, description)
    - blog: Posts (title, author, date, content, tags, excerpt)
    - social: Social posts (username, content, timestamp, likes, shares)
    - products: Product catalog multi-item extraction

    WORKFLOW:
    1. Choose the appropriate schema_type for your target page
    2. Optional: Provide custom_selector to target specific container
    3. Tool returns structured JSON array with extracted items

    VS scrape_url:
    - scrape_url: Returns full page content as markdown
    - scrape_structured: Returns structured JSON data for specific elements

    Returns:
        Dictionary with extracted items as JSON array

    Note:
        Results are NOT cached by default (bypass_cache=True) since
        structured data changes frequently.
    """
    from .services.crawl_service import StructuredScrapeConfig

    # Validate URL
    if not url.startswith(("http://", "https://")):
        raise ToolError("URL must start with http:// or https://")

    # Security: Check for blacklisted URLs
    if _is_url_blacklisted(url):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        raise ToolError(
            f"URL is not allowed for security reasons: {parsed.netloc} "
            f"appears to be a private or internal address."
        )

    if ctx:
        await ctx.info(f"Structured scraping: {url} (schema={schema_type})")

    crawl_svc = ctx.lifespan_context.get("crawl_service")
    if not crawl_svc:
        raise ToolError("Crawl service not available")

    config = StructuredScrapeConfig(
        schema_type=schema_type,
        custom_selector=custom_selector,
        bypass_cache=bypass_cache,
    )

    result = await crawl_svc.scrape_structured(url, config)

    if ctx:
        if result.success:
            await ctx.info(f"Extracted {result.item_count} items")
        else:
            await ctx.error(f"Extraction failed: {result.error}")

    return {
        "url": result.url,
        "success": result.success,
        "schema_type": result.schema_type,
        "item_count": result.item_count,
        "items": result.items,
        "title": result.title,
        "error": result.error,
    }


@mcp.tool()
async def list_schemas(ctx: Context | None = None) -> dict:
    """List all available structured extraction schemas

    Returns information about pre-built schemas available for
    scrape_structured, including field counts and descriptions.

    Returns:
        Dictionary with list of available schemas
    """
    from .services.extraction_schemas import list_schemas

    schemas = list_schemas()

    return {
        "total": len(schemas),
        "schemas": schemas,
        "usage": "Use scrape_structured with schema_type parameter",
    }


# ========== SERVER ENTRY POINT ==========

if __name__ == "__main__":
    port = int(os.getenv("MCP_PORT", 8000))
    host = os.getenv("MCP_HOST", "0.0.0.0")

    logger.info(f"MCP HTTP server starting on {host}:{port}")
    logger.info(f"Direct access: http://localhost:{port}/mcp")
    logger.info(f"Via Caddy+Tailscale: http://<your-tailscale-ip>/mcp")
    logger.info(f"Via MagicDNS+Caddy: https://<hostname>.<tailnet>.ts.net/mcp")
    logger.info(f"Session state: Redis @ {REDIS_HOST}:{REDIS_PORT}")
    logger.info(f"Caching: enabled (search: {SEARCH_CACHE_TTL}s, scrape: {SCRAPE_CACHE_TTL}s)")
    logger.info(f"Web tools: search_web, scrape_url, map_domain, crawl_site, scrape_structured, list_schemas, get_domains, clean_database")
    logger.info(f"Docs tools: docs_list_sources, docs_fetch_docs")

    mcp.run(transport="http", host=host, port=port)
