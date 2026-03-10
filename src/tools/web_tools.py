"""Web Research Tools

Tools for searching the web, scraping URLs, and extracting structured data.
- search_web: Search using multiple search engines
- scrape_url: Scrape URL with automatic method selection
- scrape_structured: Extract structured JSON data using pre-built schemas
- list_schemas: List available extraction schemas
"""

from typing import Annotated, Literal, Optional

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field
from loguru import logger


# Cache TTL from settings
from ..settings import get_settings
_settings = get_settings()
SEARCH_CACHE_TTL = _settings.search_cache_ttl
SCRAPE_CACHE_TTL = _settings.scrape_cache_ttl


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

    return {
        "query": query,
        "total_results": result.total_results,
        "pages_scraped": result.pages_scraped,
        "engines": result.engines,
        "search_time_ms": result.search_time_ms,
        "reranked": rerank,
        "time_filter": time_filter,
        "results": [
            {"title": r.title, "url": r.url, "snippet": r.snippet}
            for r in result.results
        ]
    }


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

    from ..models.unified import ScrapeRequest, ScrapingMethod

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

    return response


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
    from ..services.crawl_service import StructuredScrapeConfig

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


async def list_schemas(ctx: Context | None = None) -> dict:
    """List all available structured extraction schemas

    Returns information about pre-built schemas available for
    scrape_structured, including field counts and descriptions.

    Returns:
        Dictionary with list of available schemas
    """
    from ..services.extraction_schemas import list_schemas as list_available_schemas

    schemas = list_available_schemas()

    return {
        "total": len(schemas),
        "schemas": schemas,
        "usage": "Use scrape_structured with schema_type parameter",
    }
