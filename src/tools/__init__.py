"""MCP Server Tools - Modular organization

This package exports all MCP tools organized by functionality:
- Web tools: search_web, scrape_url, scrape_structured, list_schemas
- Crawl tools: map_domain, crawl_site
- Docs tools: docs_list_sources, docs_fetch_docs
- Admin tools: get_domains, clean_database
"""

from .web_tools import search_web, scrape_url, scrape_structured, list_schemas
from .crawl_tools import map_domain, crawl_site
from .docs_tools import docs_list_sources, docs_fetch_docs
from .admin_tools import get_domains, clean_database

__all__ = [
    # Web tools
    "search_web",
    "scrape_url",
    "scrape_structured",
    "list_schemas",
    # Crawl tools
    "map_domain",
    "crawl_site",
    # Docs tools
    "docs_list_sources",
    "docs_fetch_docs",
    # Admin tools
    "get_domains",
    "clean_database",
]
