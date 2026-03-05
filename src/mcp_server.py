"""MCP Server for Claude Desktop integration

Provides tools for web search and URL scraping via Model Context Protocol.
"""

import asyncio
from typing import Optional
from mcp.server.models import InitializationOptions
from mcp.server import NotificationOptions, Server
from mcp.types import (
    Resource,
    Tool,
    TextContent,
    EmptyParams,
)
from loguru import logger

from .services.search_service import get_search_service
from .services.scrape_service import get_scrape_service
from .db.database import Database


class MCPServer:
    """MCP Server for search and scraping operations"""

    def __init__(self):
        self.server = Server("mcp-research")
        self.search_service = None
        self.scrape_service = None
        self.db = None

    async def init_resources(self):
        """Initialize services"""
        self.db = Database()
        await self.db.init()
        self.search_service = get_search_service(db=self.db)
        self.scrape_service = get_scrape_service(db=self.db)

    async def cleanup(self):
        """Cleanup resources"""
        if self.scrape_service:
            await self.scrape_service.close()
        if self.search_service:
            await self.search_service.close()
        if self.db:
            await self.db.close()


# Global server instance
mcp_server = MCPServer()
server = mcp_server.server


@server.list_resources()
async def handle_list_resources() -> list[Resource]:
    """List available resources"""
    return []


@server.read_resource()
async def handle_read_resource(uri: str) -> str:
    """Read a resource"""
    return ""


@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    """List available tools"""
    from .models.unified import ScrapingMethod

    return [
        Tool(
            name="search_web",
            description="Search the web using multiple search engines",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query"
                    },
                    "pages": {
                        "type": "number",
                        "description": "Number of pages to fetch (1-10)",
                        "default": 5
                    },
                    "exclude_blacklist": {
                        "type": "boolean",
                        "description": "Filter out blacklisted domains",
                        "default": True
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="scrape_url",
            description="Scrape a URL and extract clean markdown content. Supports PDF files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to scrape"
                    },
                    "method": {
                        "type": "string",
                        "enum": [m.value for m in ScrapingMethod],
                        "description": "Force specific scraping method"
                    }
                },
                "required": ["url"]
            }
        ),
        Tool(
            name="get_domains",
            description="List all tracked domains with their preferred scraping methods",
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "enum": [m.value for m in ScrapingMethod],
                        "description": "Filter by method"
                    }
                }
            }
        ),
        Tool(
            name="clean_database",
            description="Clear all domain tracking data from the database",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls"""

    # Ensure services are initialized
    if mcp_server.db is None:
        await mcp_server.init_resources()

    try:
        if name == "search_web":
            result = await mcp_server.search_service.search(
                query=arguments.get("query", ""),
                pages=arguments.get("pages", 5),
                exclude_blacklist=arguments.get("exclude_blacklist", True)
            )
            return [TextContent(
                type="text",
                text=f"Found {result.total_results} results:\n\n" +
                     "\n".join([
                         f"1. [{r.title}]({r.url})\n   {r.snippet}\n   Source: {r.domain}\n"
                         for r in result.results[:10]
                     ])
            )]

        elif name == "scrape_url":
            from .models.unified import ScrapeRequest, ScrapingMethod

            request = ScrapeRequest(
                url=arguments["url"],
                force_method=ScrapingMethod(arguments["method"]) if arguments.get("method") else None
            )
            result = await mcp_server.scrape_service.scrape(request)

            if result.success:
                content = f"# {result.title}\n\n{result.content}"
                if result.metadata.get("pages"):
                    content = f"# {result.title}\n\n(PDF: {result.metadata['pages']} pages)\n\n{result.content}"
                return [TextContent(type="text", text=content)]
            else:
                return [TextContent(type="text", text=f"Error: {result.error}")]

        elif name == "get_domains":
            from .models.unified import ScrapingMethod

            filter_method = arguments.get("filter")
            domains = await mcp_server.db.get_all_domains()

            if filter_method:
                domains = [d for d in domains if d["preferred_method"] == filter_method]

            return [TextContent(
                type="text",
                text=f"Tracking {len(domains)} domains:\n\n" +
                     "\n".join([
                         f"- {d['domain']}: {d['preferred_method']} " +
                         f"(blacklisted: {d['is_blacklisted']}, failures: {d['failure_count']})"
                         for d in domains
                     ])
            )]

        elif name == "clean_database":
            count = await mcp_server.db.clean()
            return [TextContent(type="text", text=f"Cleared {count} domain records from database")]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        logger.error(f"Error in tool call {name}: {e}")
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def main():
    """Main entry point"""
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="mcp-research",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
