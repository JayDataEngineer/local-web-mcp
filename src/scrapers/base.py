"""Shared scraping implementations - used by both FastAPI and Celery"""

from typing import Callable, Any
from loguru import logger
from datetime import datetime

from ..core.constants import (
    CRAWL4AI_WORD_COUNT_THRESHOLD,
    SELENIUM_PAGE_LOAD_WAIT_SECONDS,
    MIN_CONTENT_LENGTH,
    DEFAULT_HEADERS,
)
from ..utils import extract_domain


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


async def scrape_crawl4ai(url: str, cleaner, css_selector: str = None) -> dict:
    """
    Scrape using Crawl4AI (fast, JS-enabled)

    Args:
        url: URL to scrape
        cleaner: ContentCleaner instance
        css_selector: Optional CSS selector for targeted extraction

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
                domain = extract_domain(url)
                html_content = result.html
                clean_markdown = cleaner.clean(html_content, url, css_selector)

                # Check minimum content length
                if len(clean_markdown) < MIN_CONTENT_LENGTH:
                    return build_content_too_short_response(url, "crawl4ai", len(clean_markdown))

                title = result.metadata.get("title", "") if hasattr(result, "metadata") else ""

                return build_scrape_response(
                    success=True,
                    url=url,
                    method="crawl4ai",
                    title=title,
                    content=clean_markdown,
                    metadata=_build_metadata(len(clean_markdown.split())),
                )

        # If we get here, Crawl4AI didn't succeed
        return build_error_response(url, "crawl4ai", "Scraping failed")

    except ImportError:
        logger.warning("Crawl4AI not installed")
        return build_error_response(url, "crawl4ai", "Crawl4AI not installed")
    except Exception as e:
        logger.warning(f"Crawl4AI error for {url}: {e}")
        return build_error_response(url, "crawl4ai", e)


async def scrape_selenium(url: str, cleaner, css_selector: str = None) -> dict:
    """
    Scrape using SeleniumBase with undetected Chrome mode

    Args:
        url: URL to scrape
        cleaner: ContentCleaner instance
        css_selector: Optional CSS selector for targeted extraction

    Returns dict with keys: success, url, domain, method_used, title, content, metadata, error
    """
    try:
        from seleniumbase import DriverContext

        # Run sync Selenium in thread pool to avoid blocking
        import asyncio
        loop = asyncio.get_event_loop()

        def _scrape_sync():
            with DriverContext(uc=True, headless=True) as driver:
                driver.open(url)
                driver.sleep(SELENIUM_PAGE_LOAD_WAIT_SECONDS)
                html_content = driver.get_page_source()
                title = driver.get_title()
                return html_content, title

        html_content, title = await loop.run_in_executor(None, _scrape_sync)
        clean_markdown = cleaner.clean(html_content, url, css_selector)

        # Check minimum content length
        if len(clean_markdown) < MIN_CONTENT_LENGTH:
            return build_content_too_short_response(url, "selenium", len(clean_markdown))

        return build_scrape_response(
            success=True,
            url=url,
            method="selenium",
            title=title,
            content=clean_markdown,
            metadata=_build_metadata(len(clean_markdown.split())),
        )

    except Exception as e:
        logger.warning(f"Selenium error for {url}: {e}")
        return build_error_response(url, "selenium", e)


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

        return build_scrape_response(
            success=True,
            url=url,
            method="reddit_api",
            title=title,
            content=content.strip(),
        )

    except Exception as e:
        logger.error(f"Reddit API error for {url}: {e}")
        return build_error_response(url, "reddit_api", e)


async def scrape_pdf(url: str, cleaner=None) -> dict:
    """
    Scrape PDF files by downloading and extracting text content

    Returns dict with keys: success, url, domain, method_used, title, content, metadata, error
    """
    import httpx
    import fitz  # PyMuPDF
    import io

    try:
        logger.info(f"Downloading PDF: {url}")

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(url, headers=DEFAULT_HEADERS)
            response.raise_for_status()

            # Verify it's actually a PDF
            content_type = response.headers.get("content-type", "")
            if "application/pdf" not in content_type and not url.lower().endswith(".pdf"):
                logger.warning(f"URL doesn't appear to be a PDF: {content_type}")

            pdf_data = response.content
            pdf_file = io.BytesIO(pdf_data)

            # Open PDF with PyMuPDF
            doc = fitz.open(stream=pdf_file.read(), filetype="pdf")

            if doc.is_encrypted:
                return build_error_response(url, "pdf", "PDF is password protected")

            # Extract content from all pages
            markdown_content = []
            metadata = {
                "pages": len(doc),
                "fetched_at": datetime.now().isoformat()
            }

            # Get PDF metadata for title
            pdf_metadata = doc.metadata
            pdf_title = pdf_metadata.get("title") or pdf_metadata.get("subject", "")

            # Extract text from each page
            for page_num in range(len(doc)):
                page = doc[page_num]
                text = page.get_text("text")

                if text.strip():
                    # Add page header
                    markdown_content.append(f"\n## Page {page_num + 1}\n\n")
                    markdown_content.append(text)

            doc.close()

            if not markdown_content:
                return build_error_response(url, "pdf", "No text content found in PDF")

            full_content = "".join(markdown_content)
            word_count = len(full_content.split())
            metadata["word_count"] = word_count

            # Use filename from URL as fallback title
            if not pdf_title:
                pdf_title = url.split("/")[-1].replace(".pdf", "").replace("_", " ").replace("-", " ").title()

            return build_scrape_response(
                success=True,
                url=url,
                method="pdf",
                title=pdf_title,
                content=full_content.strip(),
                metadata=metadata,
            )

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error downloading PDF {url}: {e}")
        return build_error_response(url, "pdf", f"HTTP error: {e.response.status_code}")
    except httpx.TimeoutException:
        logger.error(f"Timeout downloading PDF {url}")
        return build_error_response(url, "pdf", "Download timeout")
    except Exception as e:
        logger.error(f"PDF processing error for {url}: {e}")
        return build_error_response(url, "pdf", str(e))


async def scrape_with_fallback(
    url: str,
    cleaner,
    db: Any,
    force_method: str | None = None,
    css_selector: str | None = None
) -> dict:
    """
    Main scraping routing logic with fallback chain

    Flow:
    1. Check if PDF → Use PDF scraper
    2. Check blacklist
    3. Reddit? → JSON API
    4. Force method? → Use it
    5. DB prefers selenium? → Try selenium first
    6. Try Crawl4AI
    7. Try Selenium (if Crawl4AI failed)
    8. If both fail: record failure (blacklist after 3 failures)

    Args:
        url: URL to scrape
        cleaner: ContentCleaner instance
        db: Database instance
        force_method: Force specific scraping method
        css_selector: Optional CSS selector for targeted extraction

    Returns dict response
    """
    domain = extract_domain(url)

    # Special handler: PDF files
    if url.lower().endswith(".pdf") or "pdf" in url.lower():
        logger.info(f"Using PDF scraper for {url}")
        result = await scrape_pdf(url)
        if result["success"]:
            await db.record_success(domain, "pdf")
        return result

    # Check blacklist
    if await db.is_blacklisted(domain):
        return build_scrape_response(
            success=False,
            url=url,
            method="blacklisted",
            error="Domain is blacklisted",
        )

    # Special handler: Reddit API
    if "reddit.com" in domain or "redd.it" in domain:
        logger.info(f"Using Reddit API for {url}")
        result = await scrape_reddit(url)
        if result["success"]:
            await db.record_success(domain, "reddit_api")
        return result

    # Force method?
    if force_method:
        return await _scrape_with_method(url, force_method, cleaner, css_selector)

    # Check database for preferred method
    preferred = await db.get_domain_method(domain)

    if preferred == "selenium":
        logger.info(f"Database says use Selenium for {domain}")
        result = await scrape_selenium(url, cleaner, css_selector)
        if result["success"]:
            await db.record_success(domain, "selenium")
            return result
        # If failed, continue to try other methods

    # Try Crawl4AI first (fast)
    result = await scrape_crawl4ai(url, cleaner, css_selector)
    if result["success"]:
        await db.record_success(domain, "crawl4ai")
        return result

    # Crawl4AI failed - mark for selenium and try
    logger.warning(f"Crawl4AI failed for {domain}, trying Selenium")
    await db.set_selenium_only(domain)

    result = await scrape_selenium(url, cleaner, css_selector)
    if result["success"]:
        await db.record_success(domain, "selenium")
        return result

    # Everything failed - record failure (may blacklist after threshold)
    logger.error(f"All scraping methods failed for {domain}")
    failure_result = await db.record_failure(domain, "both_failed")

    if failure_result["blacklisted"]:
        # Domain has now exceeded failure threshold and is blacklisted
        return build_scrape_response(
            success=False,
            url=url,
            method="blacklisted",
            error=f"All scraping methods failed. Domain blacklisted after {failure_result['failure_count']} failures.",
        )
    else:
        # Not yet blacklisted - return temporary failure
        return build_scrape_response(
            success=False,
            url=url,
            method="both_failed",
            error=f"All scraping methods failed. Failure {failure_result['failure_count']}/3 before blacklist.",
        )


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


async def _scrape_with_method(url: str, method: str, cleaner, css_selector: str = None) -> dict:
    """Scrape using specific method"""
    if method == "crawl4ai":
        return await scrape_crawl4ai(url, cleaner, css_selector)
    elif method == "selenium":
        return await scrape_selenium(url, cleaner, css_selector)
    elif method == "reddit_api":
        return await scrape_reddit(url, cleaner)
    elif method == "pdf":
        return await scrape_pdf(url, cleaner)
    else:
        return build_error_response(url, method, "Unknown method")


def _build_metadata(word_count: int) -> dict:
    """Build metadata dict for successful scrapes"""
    return {
        "word_count": word_count,
        "fetched_at": datetime.now().isoformat()
    }


def dict_to_scrape_response(data: dict, response_class):
    """Convert dict to ScrapeResponse (for FastAPI)"""
    return response_class(**data)
