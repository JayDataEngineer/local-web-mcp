"""MCP Server entry point - exposes research tools via Model Context Protocol

This server wraps the existing search and scrape services as MCP tools.
It communicates via stdio for Claude Desktop and other MCP clients.

Usage:
    # Direct (stdio transport for Claude Desktop)
    python -m src.mcp_server

    # With SSE transport (for HTTP clients)
    python -m src.mcp_server --transport sse --port 8080
"""

import asyncio
import json
from typing import Optional
from loguru import logger
from mcp.server.models import InitializationOptions
from mcp.server import Server, NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .config import settings
from .services.search_service import get_search_service
from .services.scrape_service import get_scrape_service
from .db.database import get_db

# Create MCP server instance
server = Server("mcp-research-server")

# Service singletons (lazy initialized)
_search_service = None
_scrape_service = None
_db = None


async def get_services():
    """Initialize all services"""
    global _search_service, _scrape_service, _db

    if _search_service is None:
        _search_service = await get_search_service()
    if _scrape_service is None:
        _scrape_service = await get_scrape_service()
    if _db is None:
        try:
            _db = await get_db()
        except Exception as e:
            logger.warning(f"Database not available: {e}")
            _db = None

    return _search_service, _scrape_service, _db


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available MCP tools"""
    return [
        Tool(
            name="search_web",
            description="Search the web using multiple search engines (Brave, Bing, DuckDuckGo, Ask). "
            "Returns multiple pages of deduplicated results. Optionally excludes blacklisted domains.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query"
                    },
                    "pages": {
                        "type": "number",
                        "description": "Number of search result pages to fetch (default: 10)",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 20
                    },
                    "exclude_blacklist": {
                        "type": "boolean",
                        "description": "Exclude blacklisted domains from results (default: true)",
                        "default": True
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="scrape_url",
            description="Scrape a URL and extract clean markdown content. "
            "Automatically tries Crawl4AI first (fast), falls back to Selenium (stealth), "
            "and uses Reddit JSON API for Reddit URLs. "
            "Learns which method works best per domain. Blacklists after 3 failures.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to scrape"
                    },
                    "method": {
                        "type": "string",
                        "description": "Force specific scraping method (crawl4ai, selenium, reddit_api)",
                        "enum": ["crawl4ai", "selenium", "reddit_api"],
                        "default": None
                    }
                },
                "required": ["url"]
            }
        ),
        Tool(
            name="get_domains",
            description="List all tracked domains with their preferred scraping methods and status",
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Filter by method (crawl4ai, selenium, reddit_api, blacklisted)",
                        "enum": ["crawl4ai", "selenium", "reddit_api", "blacklisted"],
                        "default": None
                    }
                }
            }
        ),
        Tool(
            name="clean_database",
            description="Clear all domain tracking data (methods, blacklist, etc). "
            "Useful for starting fresh or resetting learned behavior.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls"""
    search_svc, scrape_svc, db = await get_services()

    if name == "search_web":
        query = arguments.get("query")
        pages = arguments.get("pages", 10)
        exclude_blacklist = arguments.get("exclude_blacklist", True)

        result = await search_svc.search(
            query=query,
            pages=pages,
            exclude_blacklist=exclude_blacklist
        )

        return [TextContent(
            type="text",
            text=json.dumps({
                "query": query,
                "total_results": result.total,
                "results": [
                    {
                        "title": r.title,
                        "url": r.url,
                        "domain": r.domain,
                        "snippet": r.snippet
                    }
                    for r in result.results
                ]
            }, indent=2)
        )]

    elif name == "scrape_url":
        url = arguments.get("url")
        method = arguments.get("method")

        # Create scrape request
        from .models.unified import ScrapeRequest, ScrapingMethod
        request = ScrapeRequest(
            url=url,
            force_method=ScrapingMethod(method) if method else None
        )

        result = await scrape_svc.scrape(request)

        response = {
            "url": result.url,
            "success": result.success,
            "method_used": result.method_used.value if result.method_used else None,
        }

        if result.success:
            response.update({
                "title": result.title,
                "content": result.content,
                "word_count": result.metadata.get("word_count", 0) if result.metadata else 0
            })
        else:
            response["error"] = result.error

        return [TextContent(
            type="text",
            text=json.dumps(response, indent=2)
        )]

    elif name == "get_domains":
        if db is None:
            return [TextContent(
                type="text",
                text=json.dumps({"error": "Database not available"}, indent=2)
            )]

        filter_method = arguments.get("filter")
        domains = await db.get_all_domains()

        if filter_method:
            domains = [d for d in domains if d.get("method") == filter_method]

        return [TextContent(
            type="text",
            text=json.dumps({
                "total": len(domains),
                "domains": domains
            }, indent=2)
        )]

    elif name == "clean_database":
        if db is None:
            return [TextContent(
                type="text",
                text=json.dumps({"error": "Database not available"}, indent=2)
            )]

        count = await db.clean()
        return [TextContent(
            type="text",
            text=json.dumps({
                "status": "success",
                "records_removed": count
            }, indent=2)
        )]

    else:
        return [TextContent(
            type="text",
            text=json.dumps({"error": f"Unknown tool: {name}"}, indent=2)
        )]


async def main():
    """Run the MCP server with stdio transport"""
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        logger.info("MCP Server starting on stdio...")
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="mcp-research-server",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                )
            )
        )


def main_sse(host: str = "0.0.0.0", port: int = 8080):
    """Run the MCP server with SSE transport (for HTTP clients)"""
    import uvicorn
    from mcp.server.sse import SseServerTransport

    async def handle_sse():
        from mcp.server.server import Server as MCPServer
        from mcp.server.stdio import stdio_server

        # For SSE, we need to expose a different endpoint
        # This is a placeholder for future SSE implementation
        logger.info(f"MCP Server starting SSE on {host}:{port}...")

    # For now, we'll run the FastAPI app alongside
    from .main import app
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--sse":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
        logger.info(f"Starting MCP Server with SSE on port {port}")
        # SSE mode - run both FastAPI and MCP capabilities
        import uvicorn
        from .main import app
        uvicorn.run(app, host="0.0.0.0", port=port)
    else:
        # Stdio mode (default for Claude Desktop)
        logger.info("Starting MCP Server with stdio transport")
        asyncio.run(main())
