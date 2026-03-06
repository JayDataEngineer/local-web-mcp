# MCP Research Server

FastMCP-based research server with MCP (Model Context Protocol) support for unified search, scraping, and clean LLM-ready output. Accessible remotely via Tailscale VPN with automatic HTTPS.

## Features

- **FastMCP Server**: SSE transport for Claude Desktop, Claude Code, and other MCP clients
- **Tailscale Integration**: Automatic HTTPS via MagicDNS (e.g., `https://mcp-server.tailb1e597.ts.net`)
- **Multi-page Search**: SearXNG (Brave, Bing, DuckDuckGo, Ask) with 10-page pagination
- **Smart Scraping**: Crawl4AI (fast) → SeleniumBase (stealth fallback) → Blacklist
- **PDF Support**: Download and extract text from PDF files using PyMuPDF
- **Domain Rate Limiting**: Redis-backed concurrent request limiting
- **Clean Output**: Trafilatura + Readability for LLM-ready markdown
- **Domain Learning**: PostgreSQL tracks which method works per domain
- **Documentation Tools**: Built-in llms.txt support (LangChain, FastAPI, Pydantic, etc.)
- **Redis Caching**: Cached search and scrape results
- **Caddy Reverse Proxy**: Professional deployment with automatic TLS

## Architecture

```
                    ┌─────────────────────────────────────────┐
                    │         Caddy (ports 80/443)          │
                    │  Tailscale TLS via shared socket      │
                    └──────────────┬──────────────────────────┘
                                   │ Docker DNS (mcp_net)
                    ┌──────────────▼──────────────────────────┐
                    │         mcp-server (SSE on :8000)      │
                    │  ┌────────────────────────────────┐   │
                    │  │ Web Tools:                     │   │
                    │  │ • search_web                   │   │
                    │  │ • scrape_url                   │   │
                    │  │ • get_domains                  │   │
                    │  │ • clean_database               │   │
                    │  └────────────────────────────────┘   │
                    │  ┌────────────────────────────────┐   │
                    │  │ Docs (mcpdoc, namespace: docs_) │   │
                    │  │ • list_doc_sources             │   │
                    │  │ • fetch_docs                   │   │
                    │  └────────────────────────────────┘   │
                    └──────────────┬──────────────────────────┘
                                   │
           ┌─────────────────────┼─────────────────────┐
           ▼                     ▼                     ▼
    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
    │  PostgreSQL  │    │  Redis       │    │  SearXNG     │
    │  (domains)   │    │  (cache)     │    │  (search)    │
    └──────────────┘    └──────────────┘    └──────────────┘
                                  │
                    ┌───────────────┼──────────────┐
                    ▼               ▼               ▼
            ┌──────────┐   ┌──────────┐   ┌──────────┐
            │  Worker │   │  Beat    │   │  Flower  │
            │(Celery) │   │(schedule)│   │(monitor) │
            └──────────┘   └──────────┘   └──────────┘
```

## Quick Start

### 1. Prerequisites

- Docker and Docker Compose
- Tailscale account (for remote access)

### 2. Configure Environment

```bash
# Copy example env file
cp .env.example .env

# Edit .env with your values:
# - TS_AUTHKEY: Get from https://login.tailscale.com/admin/settings/keys
# - TAILNET_DOMAIN: Your tailnet domain (e.g., tailb1e597.ts.net)
# - TAILNET_MACHINE_NAME: Your Tailscale machine name (e.g., mcp-server)
```

### 3. Start Services

```bash
docker compose up -d
```

### 4. Connect via Tailscale

Once Tailscale is running, your MCP server is accessible at:

```
https://<TAILNET_MACHINE_NAME>.<TAILNET_DOMAIN>/sse
```

Example: `https://mcp-server.tailb1e597.ts.net/sse`

---

## Connecting Clients

### Claude Code (CLI)

```bash
claude mcp add --transport http research https://mcp-server.tailb1e597.ts.net/sse
```

Replace the URL with your actual Tailscale MagicDNS URL.

### Claude Desktop

Add to your Claude Desktop config:

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "research": {
      "transport": "sse",
      "url": "https://mcp-server.tailb1e597.ts.net/sse",
      "timeout": 120000
    }
  }
}
```

### Other MCP Clients

Any MCP-compatible client can connect via SSE transport to your Tailscale HTTPS URL.

---

## Available Tools

### Web Research (no prefix)

| Tool | Description |
|------|-------------|
| `search_web` | Search the web using multiple search engines |
| `scrape_url` | Scrape a URL and extract clean markdown |
| `get_domains` | List tracked domains with preferred methods |
| `clean_database` | Clear all domain tracking data |

### Documentation (prefix: `docs_`)

| Tool | Description |
|------|-------------|
| `docs_list_doc_sources` | List available documentation libraries |
| `docs_fetch_docs` | Fetch documentation from llms.txt sources |

---

## Docker Services

| Container | Purpose | Resources |
|-----------|---------|-----------|
| mcp-caddy | Reverse proxy with automatic TLS | - |
| mcp-server | FastMCP server with SSE transport | 512MB limit |
| mcp-celery-worker | Scraping worker (10 parallel browsers) | 3GB limit, 2 CPUs |
| mcp-celery-beat | Periodic task scheduler | 256MB limit |
| mcp-flower | Celery monitoring (localhost:5555) | 256MB limit |
| mcp-postgres | Domain tracking database | 512MB limit |
| mcp-redis | Cache + rate limiting | 256MB limit |
| mcp-searxng | Multi-engine search | 512MB limit |
| mcp-ts | Tailscale sidecar (host network) | - |

---

## Environment Variables

See `.env.example` for all configurable values:

```bash
# Tailscale
TS_AUTHKEY=tskey-auth-<your-key>
TAILNET_DOMAIN=your-tailnet.ts.net
TAILNET_MACHINE_NAME=mcp-server

# PostgreSQL
POSTGRES_HOST=postgres
POSTGRES_DB=mcp_server
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres

# SearXNG
SEARXNG_SECRET=<generate with: openssl rand -hex 32>

# Celery
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/0

# API
MCP_PORT=8000
ALLOWED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000
```

---

## Scraping Flow

```
1. Check Cache → Return cached if available
2. Check Rate Limit → Wait if too many concurrent to domain
3. Check Blacklist → Reject if blacklisted
4. PDF? → Extract text with PyMuPDF
5. Reddit? → Use Reddit JSON API
6. Check Database → Use learned preference
7. Try Crawl4AI (fast, JS-enabled)
8. If failed → Try Selenium (stealth mode)
9. If both failed → Blacklist domain
```

---

## Documentation Sources

The server includes official documentation for:

- **LangGraph** - Agent framework
- **LangChain** - LLM framework
- **FastAPI** - Web framework
- **Pydantic** - Data validation
- **FastMCP** - MCP framework

Add more in `docs_config.yaml`.

---

## Tech Stack

- **FastMCP** - MCP server framework
- **Caddy** - Reverse proxy with automatic TLS
- **PostgreSQL** - Domain tracking
- **Celery + Redis** - Task queue
- **SearXNG** - Multi-engine search
- **Crawl4AI** - Fast JS-enabled scraping
- **SeleniumBase** - Stealth scraping fallback
- **Tailscale** - VPN + MagicDNS
