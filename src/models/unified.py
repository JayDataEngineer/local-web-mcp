"""Unified data models for search and scrape results"""

from pydantic import BaseModel, Field
from typing import List, Optional, Literal
from datetime import datetime
from enum import Enum


class ScrapingMethod(str, Enum):
    CRAWL4AI = "crawl4ai"
    SELENIUM = "selenium"
    REDDIT_API = "reddit_api"
    PDF = "pdf"
    BLACKLISTED = "blacklisted"


class SearchResult(BaseModel):
    """Unified search result format"""
    title: str
    url: str
    snippet: str
    domain: str


class CombinedSearchResponse(BaseModel):
    """Combined search from multiple engines"""
    query: str
    total_results: int
    pages_scraped: int
    results: List[SearchResult]
    engines: dict[str, int]  # {"searxng": N} where N is results per engine
    search_time_ms: float
    cached: bool = False  # True if results came from cache


class ScrapeRequest(BaseModel):
    url: str
    force_method: Optional[ScrapingMethod] = None


class ScrapeResponse(BaseModel):
    """Unified scrape response"""
    success: bool
    url: str
    domain: str
    method_used: ScrapingMethod
    title: Optional[str] = None
    content: Optional[str] = None  # Unified markdown format
    summary: Optional[str] = None  # AI-generated summary (currently unused)
    metadata: dict = {}
    error: Optional[str] = None
    cached: bool = False  # True if result came from cache


class DomainRecord(BaseModel):
    """Database record for domain tracking"""
    domain: str
    preferred_method: ScrapingMethod
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    failure_count: int = 0
    is_blacklisted: bool = False


class CheckRequest(BaseModel):
    """Check database URLs"""
    max_urls: Optional[int] = None
    blacklist_threshold: int = 3


class CheckResponse(BaseModel):
    """Check results"""
    total_checked: int
    still_valid: int
    moved_to_selenium: int
    blacklisted: int
    details: List[dict]


class TaskStatus(str, Enum):
    """Celery task status"""
    PENDING = "PENDING"
    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    REVOKED = "REVOKED"
    RETRY = "RETRY"


class TaskResponse(BaseModel):
    """Response when task is submitted"""
    task_id: str
    status: TaskStatus
    message: str


class TaskStatusResponse(BaseModel):
    """Response when polling task status"""
    task_id: str
    status: TaskStatus
    result: Optional[ScrapeResponse] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
