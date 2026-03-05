"""URL utilities"""

from urllib.parse import urlparse
from typing import Optional


def extract_domain(url: str) -> Optional[str]:
    """Extract domain from URL"""
    try:
        parsed = urlparse(url)
        return parsed.netloc or parsed.path
    except Exception:
        return url


def build_scrape_response(
    success: bool,
    url: str,
    method: str,
    title: str = None,
    content: str = None,
    metadata: dict = None,
    error: str = None
) -> dict:
    """Build standard scrape response dict"""
    from ..utils.url_utils import extract_domain
    return {
        "success": success,
        "url": url,
        "domain": extract_domain(url),
        "method_used": method,
        "title": title,
        "content": content,
        "summary": None,
        "metadata": metadata or {},
        "error": error,
    }


def build_content_too_short_response(url: str, method: str, length: int) -> dict:
    """Build response for content that's too short"""
    from ..utils.url_utils import extract_domain
    return {
        "success": False,
        "url": url,
        "domain": extract_domain(url),
        "method_used": method,
        "error": f"Content too short ({length} chars < minimum)",
    }


def build_error_response(url: str, method: str, error) -> dict:
    """Build error response dict"""
    from ..utils.url_utils import extract_domain
    return {
        "success": False,
        "url": url,
        "domain": extract_domain(url),
        "method_used": method,
        "error": str(error),
    }


# Import extract_domain for use in other modules
__all__ = ["extract_domain", "build_scrape_response", "build_content_too_short_response", "build_error_response"]
