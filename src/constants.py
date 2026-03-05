"""Configuration constants for MCP Research Server"""

# Scraping timeouts
CRAWL4AI_WORD_COUNT_THRESHOLD = 10
SELENIUM_PAGE_LOAD_WAIT_SECONDS = 3
HTTP_REQUEST_TIMEOUT = 30.0

# Content filtering
MIN_CONTENT_LENGTH = 100
BLACKLIST_FAILURE_THRESHOLD = 3

# Celery configuration
CELERY_WORKER_CONCURRENCY = 10
CELERY_TASK_TIMEOUT_SECONDS = 300
CELERY_TASK_SOFT_TIMEOUT_SECONDS = 270
CELERY_RESULT_EXPIRE_SECONDS = 86400  # 24 hours

# Rate limiting
RATE_LIMIT_MAX_CONCURRENT = 3  # Per domain
RATE_LIMIT_ACQUIRE_TIMEOUT = 30
RATE_LIMIT_TTL = 300  # 5 minutes

# Search configuration
DEFAULT_SEARCH_ENGINES = ["brave", "bing", "duckduckgo", "ask"]
MAX_SEARCH_PAGES = 10

# Reddit API limits
REDDIT_MAX_COMMENTS = 20
REDDIT_MAX_POSTS = 10

# Cache TTL (seconds)
CACHE_TTL_SCRAPE = 86400  # 24 hours
CACHE_TTL_SEARCH = 3600  # 1 hour

# HTTP headers for requests
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
