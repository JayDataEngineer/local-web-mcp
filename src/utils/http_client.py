"""HTTP client utilities"""

import httpx
from typing import Optional


def create_async_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """Create a configured async HTTP client"""
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
    )
