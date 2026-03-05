"""Utility modules for reducing code duplication"""

from .url_utils import extract_domain
from .response_builder import (
    build_scrape_response,
    build_error_response,
    build_content_too_short_response,
)
from .singleton import (
    singleton,
    create_singleton_factory,
    create_async_singleton_factory,
)
from .http_client import (
    create_http_client,
    create_async_client,
    create_sync_client,
)

__all__ = [
    "extract_domain",
    "build_scrape_response",
    "build_error_response",
    "build_content_too_short_response",
    "singleton",
    "create_singleton_factory",
    "create_async_singleton_factory",
    "create_http_client",
    "create_async_client",
    "create_sync_client",
]
