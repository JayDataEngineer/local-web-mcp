"""Application constants - single source of truth for magic numbers"""

# Scraping timeouts and thresholds
CRAWL4AI_WORD_COUNT_THRESHOLD = 10
SELENIUM_PAGE_LOAD_WAIT_SECONDS = 3
HTTP_REQUEST_TIMEOUT = 30.0

# Content limits
REDDIT_MAX_COMMENTS = 20
REDDIT_MAX_POSTS = 20
MIN_CONTENT_LENGTH = 100

# Celery configuration
CELERY_TASK_TIMEOUT_SECONDS = 300
CELERY_TASK_SOFT_TIMEOUT_SECONDS = 270
CELERY_WORKER_CONCURRENCY = 10
CELERY_RESULT_EXPIRE_SECONDS = 3600

# Blacklist threshold
BLACKLIST_FAILURE_THRESHOLD = 3

# User agents
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

# Headers for HTTP requests
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
