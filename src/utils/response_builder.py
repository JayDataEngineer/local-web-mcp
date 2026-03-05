"""Response builders for API responses"""

from ..utils.url_utils import extract_domain
from ..models.unified import ScrapingMethod


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
    return {
        "success": False,
        "url": url,
        "domain": extract_domain(url),
        "method_used": method,
        "error": f"Content too short ({length} chars < minimum)",
    }


def build_error_response(url: str, method: str, error) -> dict:
    """Build error response dict"""
    return {
        "success": False,
        "url": url,
        "domain": extract_domain(url),
        "method_used": method,
        "error": str(error),
    }
