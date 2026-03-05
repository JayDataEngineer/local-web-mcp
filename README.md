# MCP Research Server

FastAPI-based research server with MCP (Model Context Protocol) support for unified search, scraping, and clean LLM-ready output.

## Features

- **MCP Protocol**: Native MCP server with stdio transport for Claude Desktop
- **Multi-page Search**: SearXNG (Brave, Bing, DuckDuckGo, Ask) with 10-page pagination
- **Smart Scraping**: Crawl4AI (fast) → SeleniumBase (stealth fallback) → Blacklist
- **PDF Support**: Download and extract text from PDF files using PyMuPDF
- **Domain Rate Limiting**: Redis-backed concurrent request limiting (max 3 per domain)
- **Clean Output**: Trafilatura + Readability for LLM-ready markdown
- **Domain Learning**: PostgreSQL tracks which method works per domain
- **Reddit Handler**: Special JSON API for clean Reddit threads (FULL results, no artificial limits)
- **Blacklist Filtering**: Automatically filters bad domains from search results
- **Celery Beat**: Scheduled tasks for automated maintenance (daily blacklist cleanup)
- **Redis Caching**: Cached search and scrape results for performance
- **Tailscale VPN**: Remote access from anywhere on your tailnet

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     FastAPI (port 8000)                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   /search    │  │   /scrape    │  │   /domains   │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
└─────────┼──────────────────┼──────────────────┼──────────────┘
          │                  │                  │
          ▼                  ▼                  ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  SearXNG        │  │  Celery Queue   │  │  PostgreSQL     │
│  (multi-engine) │  │  (Redis)        │  │  (domain DB)    │
└─────────────────┘  └────────┬────────┘  └─────────────────┘
                               │
                ┌──────────────┼──────────────┐
                │              │              │
                ▼              ▼              ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │  Scrape      │  │  Celery      │  │  Rate        │
    │  Worker      │  │  Beat        │  │  Limiter     │
    │  (10 conc.)  │  │  (scheduled) │  │  (per dom.)  │
    └──────┬───────┘  └──────────────┘  └──────────────┘
           │
           ▼
  ┌──────────────────────────────┐
  │  Scraping Layer              │
  │  1. PDF files (PyMuPDF)      │
  │  2. Reddit JSON API          │
  │  3. Crawl4AI (fast)          │
  │  4. SeleniumBase (stealth)   │
  └────────┬─────────────────────┘
           │
           ▼
  ┌──────────────────────┐
  │  Content Cleaner     │
  │  (Trafilatura +      │
  │   Readability)       │
  └──────────────────────┘
```

## MCP Usage (Claude Desktop)

### Option 1: Docker (Recommended)

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "mcp-research": {
      "command": "docker",
      "args": [
        "exec", "-i", "mcp-server",
        "python", "-m", "src.mcp_server"
      ],
      "env": {
        "POSTGRES_HOST": "postgres",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "mcp_server",
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": "postgres",
        "SEARXNG_URL": "http://searxng:8080",
        "CELERY_BROKER_URL": "redis://redis:6379/0",
        "CELERY_RESULT_BACKEND": "redis://redis:6379/0"
      }
    }
  }
}
```

Then start services:
```bash
docker compose up -d
```

### Option 2: Local Python

1. Install dependencies:
```bash
uv sync
```

2. Add to Claude Desktop config:
```json
{
  "mcpServers": {
    "mcp-research": {
      "command": "uv",
      "args": ["run", "python", "-m", "src.mcp_server"],
      "cwd": "/path/to/lang-tools",
      "env": {
        "POSTGRES_HOST": "localhost",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "mcp_server",
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": "postgres",
        "SEARXNG_URL": "http://localhost:8080",
        "CELERY_BROKER_URL": "redis://localhost:6379/0",
        "CELERY_RESULT_BACKEND": "redis://localhost:6379/0"
      }
    }
  }
}
```

## MCP Tools

### search_web
Search the web using multiple search engines.

```python
search_web(query="python async await", pages=5, exclude_blacklist=true)
```

### scrape_url
Scrape a URL and extract clean markdown content. Supports PDF files.

```python
scrape_url(url="https://example.com", method="crawl4ai")
scrape_url(url="https://example.com/document.pdf")  # Auto-detected
```

### get_domains
List all tracked domains with their preferred scraping methods.

```python
get_domains(filter="crawl4ai")
```

### clean_database
Clear all domain tracking data.

```python
clean_database()
```

## REST API (for testing/curl)

The FastAPI REST API runs on port 8000.

### POST /search
Multi-page search with blacklist filtering.

```bash
curl -X POST "http://localhost:8000/search?query=python&pages=10"
```

### POST /scrape
Scrape a URL with automatic method routing. Poll for async results.

```bash
# Queue scrape job
curl -X POST "http://localhost:8000/scrape" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.python.org"}'
# Response: {"task_id": "...", "status": "PENDING"}

# Poll for result
curl "http://localhost:8000/status/{task_id}"
```

### GET /domains
List all tracked domains with their methods.

```bash
curl "http://localhost:8000/domains"
```

### POST /clean
Clear the domain tracking database.

```bash
curl -X POST "http://localhost:8000/clean"
```

## Docker Services

| Container | Purpose | Resources |
|-----------|---------|-----------|
| mcp-server | FastAPI + MCP Server | 512MB limit |
| mcp-celery-worker | Scraping worker (concurrency: 10) | 3GB limit, 2 CPUs |
| mcp-celery-beat | Periodic task scheduler | 256MB limit |
| mcp-flower | Celery monitoring (port 5555) | 256MB limit |
| mcp-postgres | PostgreSQL database | 512MB limit |
| mcp-redis | Celery queue + cache + rate limiting | 256MB limit |
| mcp-searxng | Multi-engine search | 512MB limit |
| mcp-ts | Tailscale VPN | - |

## Quick Start

```bash
# Start all services
docker compose up -d

# Check status
docker ps

# View logs
docker compose logs -f mcp-server

# Stop all services
docker compose down
```

## Access

- **API**: http://localhost:8000
- **Flower (Celery)**: http://localhost:5555
- **Tailscale**: http://<your-tailnet-ip>:8000

## Environment Variables

```bash
# PostgreSQL
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=mcp_server
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres

# External Services
SEARXNG_URL=http://searxng:8080

# Celery
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/0

# SearXNG
SEARXNG_SECRET=<generate a random string>

# Tailscale (optional)
TS_AUTHKEY=tskey-auth-<your-key>
```

## Scraping Flow

```
1. Check Cache → Return cached if available
2. Check Rate Limit → Wait if too many concurrent to domain
3. Check Blacklist → Reject if blacklisted
4. PDF? → Extract text with PyMuPDF
5. Reddit? → Use Reddit JSON API (FULL results, no limits)
6. Check Database → Use learned preference
7. Try Crawl4AI (fast, JS-enabled)
8. If failed → Try Selenium (stealth mode)
9. If both failed → Blacklist domain
```

## Periodic Tasks (Celery Beat)

- **cleanup_blacklist**: Runs daily at 2 AM - removes blacklisted domains older than 7 days
- **health_check**: Verifies database and service health

## Tech Stack

- **FastAPI** - Web framework
- **MCP SDK** - Model Context Protocol
- **PostgreSQL** - Domain tracking & blacklist
- **Celery + Redis** - Task queue with controlled concurrency
- **SearXNG** - Multi-engine search
- **Crawl4AI** - Fast JS-enabled scraping
- **SeleniumBase** - Stealth scraping fallback (Pure CDP mode)
- **PyMuPDF** - PDF text extraction
- **Trafilatura** - HTML → Markdown conversion
- **Tailscale** - Remote VPN access

## Configuration

See `src/constants.py` for all configurable values:

```python
# Scraping
CRAWL4AI_WORD_COUNT_THRESHOLD = 10
SELENIUM_PAGE_LOAD_WAIT_SECONDS = 3
HTTP_REQUEST_TIMEOUT = 30.0
MIN_CONTENT_LENGTH = 100
BLACKLIST_FAILURE_THRESHOLD = 3

# Celery
CELERY_WORKER_CONCURRENCY = 10
CELERY_TASK_TIMEOUT_SECONDS = 300
CELERY_RESULT_EXPIRE_SECONDS = 86400

# Rate Limiting
RATE_LIMIT_MAX_CONCURRENT = 3  # Per domain
RATE_LIMIT_ACQUIRE_TIMEOUT = 30

# Search
DEFAULT_SEARCH_ENGINES = ["brave", "bing", "duckduckgo", "ask"]
```
