"""Configuration for MCP Server"""

import os
from dataclasses import dataclass
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
    ollama_url: str = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")

    # Scraper
    stealth_scraper_url: str = os.getenv("STEALTH_SCRAPER_URL", "http://stealth-scraper:8000")
    default_timeout: int = int(os.getenv("DEFAULT_TIMEOUT", "30"))

    # Models
    summary_model: str = os.getenv("SUMMARY_MODEL", "llama3.2:3b")
    main_model: str = os.getenv("MAIN_MODEL", "llama3.2")

    # Database
    db_path: str = os.getenv("DB_PATH", "/app/data/mcp_server.db")

    # Search (using constants)
    default_engines: List[str] = DEFAULT_SEARCH_ENGINES

    # Domain tracking (using constants)
    known_waf_domains: List[str] = [
        "stackoverflow.com",
        "reddit.com",
        "linkedin.com",
        "twitter.com",
        "facebook.com",
        "instagram.com",
    ]


settings = Settings()
