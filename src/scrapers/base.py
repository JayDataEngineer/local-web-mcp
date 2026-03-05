"""Shared scraping implementations - used by both FastAPI and Celery"""

from typing import Callable, Any
from urllib.parse import urlparse
from loguru import logger
from datetime import datetime

from ..constants import (
    CRAWL4AI_WORD_COUNT_THRESHOLD,
    SELENIUM_PAGE_LOAD_WAIT_SECONDS,
    MIN_CONTENT_LENGTH,
    DEFAULT_HEADERS,
)


async def scrape_crawl4ai(url: str, cleaner) -> dict:
    """
    Scrape using Crawl4AI (fast, JS-enabled)

    Returns dict with keys: success, url, domain, method_used, title, content, metadata, error
    """
    try:
        from crawl4ai import AsyncWebCrawler

        async with AsyncWebCrawler(verbose=False) as crawler:
            result = await crawler.arun(
                url=url,
                word_count_threshold=CRAWL4AI_WORD_COUNT_THRESHOLD,
                bypass_cache=True,
                process_iframes=False,
            )

            if result.success:
                domain = urlparse(url).netloc
                html_content = result.html
                clean_markdown = cleaner.clean(html_content, url)

                # Check minimum content length
                if len(clean_markdown) < MIN_CONTENT_LENGTH:
                    return {
                        "success": False,
                        "url": url,
                        "domain": domain,
                        "method_used": "crawl4ai",
                        "error": f"Content too short: {len(clean_markdown)} chars"
                    }

                title = result.metadata.get("title", "") if hasattr(result, "metadata") else ""

                return {
                    "success": True,
                    "url": url,
                    "domain": domain,
                    "method_used": "crawl4ai",
                    "title": title,
                    "content": clean_markdown,
                    "metadata": _build_metadata(len(clean_markdown.split())),
                }

        # If we get here, Crawl4AI didn't succeed
        return {
            "success": False,
            "url": url,
            "domain": urlparse(url).netloc,
            "method_used": "crawl4ai",
            "error": "Scraping failed"
        }

    except ImportError:
        logger.warning("Crawl4AI not installed")
        return {
            "success": False,
            "url": url,
            "domain": urlparse(url).netloc,
            "method_used": "crawl4ai",
            "error": "Crawl4AI not installed"
        }
    except Exception as e:
        logger.warning(f"Crawl4AI error for {url}: {e}")
        return {
            "success": False,
            "url": url,
            "domain": urlparse(url).netloc,
            "method_used": "crawl4ai",
            "error": str(e)
        }


async def scrape_selenium(url: str, cleaner) -> dict:
    """
    Scrape using SeleniumBase Pure CDP mode (stealth)

    Returns dict with keys: success, url, domain, method_used, title, content, metadata, error
    """
    try:
        from seleniumbase import cdp_driver
        import asyncio

        driver = await cdp_driver.start_async()
        await driver.get(url)
        await asyncio.sleep(SELENIUM_PAGE_LOAD_WAIT_SECONDS)

        html_content = await driver.get_page_source()
        title = await driver.get_title()
        await driver.stop()

        clean_markdown = cleaner.clean(html_content, url)

        # Check minimum content length
        if len(clean_markdown) < MIN_CONTENT_LENGTH:
            return {
                "success": False,
                "url": url,
                "domain": urlparse(url).netloc,
                "method_used": "selenium",
                "error": f"Content too short: {len(clean_markdown)} chars"
            }

        return {
            "success": True,
            "url": url,
            "domain": urlparse(url).netloc,
            "method_used": "selenium",
            "title": title,
            "content": clean_markdown,
            "metadata": _build_metadata(len(clean_markdown.split())),
        }

    except Exception as e:
        logger.warning(f"Selenium error for {url}: {e}")
        return {
            "success": False,
            "url": url,
            "domain": urlparse(url).netloc,
            "method_used": "selenium",
            "error": str(e)
        }


async def scrape_reddit(url: str, cleaner=None) -> dict:
    """
    Scrape Reddit using JSON API (bypasses HTML entirely)

    Returns dict with keys: success, url, domain, method_used, title, content, error
    """
    try:
        import httpx

        json_url = normalize_reddit_url(url)
        logger.info(f"Fetching Reddit JSON: {json_url}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(json_url, headers=DEFAULT_HEADERS)
            response.raise_for_status()
            data = response.json()

        content, title = format_reddit_content(url, data)

        return {
            "success": True,
            "url": url,
            "domain": "reddit.com",
            "method_used": "reddit_api",
            "title": title,
            "content": content.strip(),
        }

    except Exception as e:
        logger.error(f"Reddit API error for {url}: {e}")
        return {
            "success": False,
            "url": url,
            "domain": "reddit.com",
            "method_used": "reddit_api",
            "error": str(e)
        }


async def scrape_with_fallback(
    url: str,
    cleaner,
    db: Any,
    force_method: str | None = None
) -> dict:
    """
    Main scraping routing logic with fallback chain

    Flow:
    1. Check blacklist
    2. Reddit? → JSON API
    3. Force method? → Use it
    4. DB prefers selenium? → Try selenium first
    5. Try Crawl4AI
    6. Try Selenium (if Crawl4AI failed)
    7. Blacklist if all failed

    Returns dict response
    """
    domain = urlparse(url).netloc

    # Check blacklist
    if await db.is_blacklisted(domain):
        return {
            "success": False,
            "url": url,
            "domain": domain,
            "method_used": "blacklisted",
            "error": "Domain is blacklisted"
        }

    # Special handler: Reddit API
    if "reddit.com" in domain or "redd.it" in domain:
        logger.info(f"Using Reddit API for {url}")
        result = await scrape_reddit(url)
        if result["success"]:
            await db.record_success(domain, "reddit_api")
        return result

    # Force method?
    if force_method:
        return await _scrape_with_method(url, force_method, cleaner)

    # Check database for preferred method
    preferred = await db.get_domain_method(domain)

    if preferred == "selenium":
        logger.info(f"Database says use Selenium for {domain}")
        result = await scrape_selenium(url, cleaner)
        if result["success"]:
            await db.record_success(domain, "selenium")
            return result
        # If failed, continue to try other methods

    # Try Crawl4AI first (fast)
    result = await scrape_crawl4ai(url, cleaner)
    if result["success"]:
        await db.record_success(domain, "crawl4ai")
        return result

    # Crawl4AI failed - mark for selenium and try
    logger.warning(f"Crawl4AI failed for {domain}, trying Selenium")
    await db.set_selenium_only(domain)

    result = await scrape_selenium(url, cleaner)
    if result["success"]:
        await db.record_success(domain, "selenium")
        return result

    # Everything failed - blacklist
    logger.error(f"All methods failed for {domain}, blacklisting")
    await db.blacklist(domain)

    return {
        "success": False,
        "url": url,
        "domain": domain,
        "method_used": "blacklisted",
        "error": "All scraping methods failed"
    }


def normalize_reddit_url(url: str) -> str:
    """Normalize Reddit URL to JSON API format"""
    if ".json" not in url:
        clean_url = url.replace("old.reddit.com", "www.reddit.com")
        clean_url = clean_url.replace("new.reddit.com", "www.reddit.com")
        clean_url = clean_url.rstrip("/")
        return f"{clean_url}.json"
    return url


def format_reddit_content(url: str, data: dict) -> tuple[str, str]:
    """
    Format Reddit JSON data into markdown

    Returns (content, title) tuple
    """
    content = f"# Reddit Thread\n\n**Original URL:** {url}\n\n"

    if "comments" in url:
        # Thread view - [post_data, comments_data]
        if isinstance(data, list) and len(data) >= 2:
            post_data = data[0].get("data", {}).get("children", [{}])[0].get("data", {})
            comments_data = data[1].get("data", {})
        else:
            post_data = data.get("data", {})
            comments_data = {}

        title = post_data.get("title", "")
        selftext = post_data.get("selftext", "")
        author = post_data.get("author", "")
        score = post_data.get("score", 0)
        num_comments = post_data.get("num_comments", 0)
        permalink = post_data.get("permalink", "")

        content += f"## {title}\n\n"
        if selftext:
            content += f"{selftext}\n\n"
        content += f"**Score:** {score} | **Comments:** {num_comments}\n"
        content += f"**Posted by:** u/{author}\n"
        content += f"**Link:** https://www.reddit.com{permalink}\n\n"

        # Top comments
        from ..constants import REDDIT_MAX_COMMENTS
        comments = comments_data.get("children", [])
        if comments:
            content += "### Top Comments\n\n"
            for comment in comments[:REDDIT_MAX_COMMENTS]:
                comment_data = comment.get("data", {})
                if not comment_data:
                    continue

                comment_text = comment_data.get("body", "")
                comment_author = comment_data.get("author", "")
                comment_score = comment_data.get("score", 0)

                if comment_text:
                    comment_text = comment_text.replace("&gt;", ">")
                    content += f"**u/{comment_author}** ({comment_score} points):\n"
                    content += f"{comment_text}\n\n"
    else:
        # Subreddit or search results
        if isinstance(data, list):
            posts = data[0].get("data", {}).get("children", [])
        else:
            posts = data.get("data", {}).get("children", [])

        title = "Reddit Posts"
        content += "### Posts\n\n"

        from ..constants import REDDIT_MAX_POSTS
        for post in posts[:REDDIT_MAX_POSTS]:
            post_data = post.get("data", {})
            if not post_data:
                continue

            title = post_data.get("title", "")
            selftext = post_data.get("selftext", "")
            author = post_data.get("author", "")
            score = post_data.get("score", 0)
            permalink = post_data.get("permalink", "")
            is_self = post_data.get("is_self", False)

            content += f"#### {title}\n\n"
            if is_self and selftext:
                if len(selftext) > 500:
                    content += f"{selftext[:500]}...\n\n"
                else:
                    content += f"{selftext}\n\n"

            content += f"**{score} points** | [link](https://www.reddit.com{permalink}) by u/{author}\n\n"

    return content, title or data.get("data", {}).get("title", "Reddit Thread") if isinstance(data, dict) else "Reddit Thread"


async def _scrape_with_method(url: str, method: str, cleaner) -> dict:
    """Scrape using specific method"""
    if method == "crawl4ai":
        return await scrape_crawl4ai(url, cleaner)
    elif method == "selenium":
        return await scrape_selenium(url, cleaner)
    elif method == "reddit_api":
        return await scrape_reddit(url, cleaner)
    else:
        return {
            "success": False,
            "url": url,
            "domain": urlparse(url).netloc,
            "method_used": method,
            "error": "Unknown method"
        }


def _build_metadata(word_count: int) -> dict:
    """Build metadata dict for successful scrapes"""
    return {
        "word_count": word_count,
        "fetched_at": datetime.now().isoformat()
    }


def dict_to_scrape_response(data: dict, response_class):
    """Convert dict to ScrapeResponse (for FastAPI)"""
    return response_class(**data)
