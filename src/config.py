"""Configuration for MCP Server"""

import os
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

from .constants import (
    BLACKLIST_FAILURE_THRESHOLD,
    DEFAULT_SEARCH_ENGINES,
    CELERY_WORKER_CONCURRENCY,
)

load_dotenv()


@dataclass
class Settings:
    # API
    host: str = os.getenv("MCP_HOST", "0.0.0.0")
    port: int = int(os.getenv("MCP_PORT", "8000"))

    # External Services
    searxng_url: str = os.getenv("SEARXNG_URL", "http://lang-tools-searxng:8080")

    # Scraper
    stealth_scraper_url: str = os.getenv("STEALTH_SCRAPER_URL", "http://stealth-scraper:8000")
    default_timeout: int = int(os.getenv("DEFAULT_TIMEOUT", "30"))

    # PostgreSQL Database
    postgres_host: str = os.getenv("POSTGRES_HOST", "localhost")
    postgres_port: int = int(os.getenv("POSTGRES_PORT", "5432"))
    postgres_db: str = os.getenv("POSTGRES_DB", "mcp_server")
    postgres_user: str = os.getenv("POSTGRES_USER", "postgres")
    postgres_password: str = os.getenv("POSTGRES_PASSWORD", "postgres")

    @property
    def postgres_url(self) -> str:
        """Build PostgreSQL connection URL"""
        return f"postgresql://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    # Search (using constants)
    default_engines: List[str] = field(default_factory=lambda: DEFAULT_SEARCH_ENGINES)

    # Domain tracking (using constants)
    known_waf_domains: List[str] = field(default_factory=lambda: [
        "stackoverflow.com",
        "reddit.com",
        "linkedin.com",
        "twitter.com",
        "facebook.com",
        "instagram.com",
    ])


settings = Settings()
