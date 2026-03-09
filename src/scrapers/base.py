"""Shared scraping implementations - used by both FastAPI and Celery"""

from typing import Callable, Any
from loguru import logger
from datetime import datetime
import re

from ..core.constants import (
    CRAWL4AI_WORD_COUNT_THRESHOLD,
    SELENIUM_PAGE_LOAD_WAIT_SECONDS,
    MIN_CONTENT_LENGTH,
    DEFAULT_HEADERS,
    CRAWL4AI_RETRY_COUNT,
    SELENIUM_RETRY_COUNT,
)
from ..utils import extract_domain


# Block detection patterns
BLOCK_PATTERNS = {
    "captcha": re.compile(r"captcha|challenge|prove.?human|robot.?check", re.I),
    "blocked": re.compile(r"access.?denied|forbidden|blocked|unavailable", re.I),
    "rate_limit": re.compile(r"rate.?limit|too.?many.?requests|429", re.I),
    "checkpoint": re.compile(
        r"security.?checkpoint|verifying.?browser|browser.?verification|"
        r"wir.uberprüfen.ihren.browser|vercel.link/security-checkpoint|"
        r"click.here.to.fix.security",
        re.I
    ),
}


def is_security_checkpoint(title: str, content: str, url: str = None) -> bool:
    """Detect if the page is a security checkpoint/challenge page

    Returns True if the page appears to be a bot protection checkpoint
    rather than actual content.
    """
    # Check title first (most reliable)
    title_lower = title.lower() if title else ""
    checkpoint_title_patterns = [
        "security checkpoint",
        "verifying your browser",
        "browser verification",
        "checkpoint",
        "access verification",
        "human verification",
        "wir überprüfen ihren browser",  # German
    ]
    for pattern in checkpoint_title_patterns:
        if pattern in title_lower:
            return True

    # Check content for specific indicators
    content_lower = content.lower() if content else ""
    checkpoint_content_indicators = [
        "vercel.link/security-checkpoint",
        "vercel.link/captcha",
        "cloudflare challenge",
        "checking your browser",
        "please wait while we verify",
        "enable javascript",
    ]
    for indicator in checkpoint_content_indicators:
        if indicator in content_lower:
            return True

    # Check for suspiciously short content on documentation-type URLs
    # (docs pages should have substantial content)
    if url and any(x in url for x in ["/docs/", "/documentation/", "/guide/", "/reference/"]):
        # If content is very short (< 300 chars) and contains verification terms
        if len(content) < 300 and any(
            term in content_lower for term in ["verify", "check", "browser", "human", "robot"]
        ):
            return True

    return False


def detect_blocking(page_content: str, status_code: int = None) -> str | None:
    """Detect if we've been blocked by the website

    Returns:
        Error message describing the block type, or None if not blocked
    """
    if status_code:
        if status_code == 403:
            return "Blocked: HTTP 403 Forbidden"
        if status_code == 429:
            return "Rate limited: Too many requests"
        if status_code >= 500:
            return f"Server error: HTTP {status_code}"

    content_lower = page_content.lower()[:2000]  # Check first 2000 chars
    for block_type, pattern in BLOCK_PATTERNS.items():
        if pattern.search(content_lower):
            if block_type == "captcha":
                return "Blocked: CAPTCHA challenge detected"
            if block_type == "blocked":
                return "Blocked: Access denied"
            if block_type == "rate_limit":
                return "Rate limited: Too many requests"
            if block_type == "checkpoint":
                return "Blocked: Security checkpoint - bot verification required"

    return None


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


async def scrape_crawl4ai(url: str, cleaner, css_selector: str = None, text_only: bool = False) -> dict:
    """
    Scrape using Crawl4AI (fast, JS-enabled) with stealth mode

    Args:
        url: URL to scrape
        cleaner: ContentCleaner instance
        css_selector: Optional CSS selector for targeted extraction
        text_only: If True, disable images for faster loading

    Returns dict with keys: success, url, domain, method_used, title, content, metadata, error
    """
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig

        # Build browser config with stealth mode (always enabled for anti-detection)
        browser_config = BrowserConfig(
            headless=True,
            enable_stealth=True,  # Anti-fingerprinting
            user_agent_mode="random",  # Random user agent
            text_mode=text_only,  # Disable images if requested
            verbose=False
        )

        async with AsyncWebCrawler(config=browser_config, verbose=False) as crawler:
            result = await crawler.arun(
                url=url,
                word_count_threshold=CRAWL4AI_WORD_COUNT_THRESHOLD,
                bypass_cache=True,
                process_iframes=False,
                # Anti-detection: add delay
                mean_delay=0.3,
                delay_before_return_html=0.2,
            )

            if result.success:
                domain = extract_domain(url)
                html_content = result.html
                clean_markdown = cleaner.clean(html_content, url, css_selector)

                # Check minimum content length
                if len(clean_markdown) < MIN_CONTENT_LENGTH:
                    # Try Crawl4AI's built-in markdown as fallback
                    crawl4ai_md = result.markdown
                    # Handle both string (older) and MarkdownGenerationResult (newer)
                    if hasattr(crawl4ai_md, 'raw_markdown'):
                        crawl4ai_md = crawl4ai_md.raw_markdown

                    if len(crawl4ai_md) >= MIN_CONTENT_LENGTH:
                        logger.info(f"Using Crawl4AI's markdown fallback for {url} ({len(crawl4ai_md)} chars)")
                        clean_markdown = crawl4ai_md
                    else:
                        return build_content_too_short_response(url, "crawl4ai", len(clean_markdown))

                title = result.metadata.get("title", "") if hasattr(result, "metadata") else ""

                # Check for security checkpoint pages (bot protection)
                if is_security_checkpoint(title, clean_markdown, url):
                    logger.warning(f"Security checkpoint detected for {url}: {title}")
                    return build_error_response(url, "crawl4ai", "Blocked: Security checkpoint - bot verification required")

                return build_scrape_response(
                    success=True,
                    url=url,
                    method="crawl4ai",
                    title=title,
                    content=clean_markdown,
                    metadata=_build_metadata(len(clean_markdown.split())),
                )

        # If we get here, Crawl4AI didn't succeed - check for blocking
        page_html = getattr(result, 'html', '') or ''
        status_code = getattr(result, 'status_code', None)
        block_error = detect_blocking(page_html, status_code)

        if block_error:
            logger.warning(f"Blocking detected for {url}: {block_error}")
        else:
            # Log generic failure with more details if available
            error_detail = getattr(result, 'error_message', 'No details')
            logger.warning(f"Crawl4AI failed for {url}: {error_detail}")

        return build_error_response(url, "crawl4ai", block_error or "Scraping failed")

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
        from pathlib import Path
        import os

        # Find Playwright's Chromium binary
        chromium_paths = list(Path("/root/.cache/ms-playwright").glob("chromium-*/chrome-linux64/chrome"))
        if chromium_paths:
            browser_path = str(chromium_paths[0])
            logger.info(f"Using Playwright Chromium: {browser_path}")
            # Set environment variable for SeleniumBase
            os.environ["SELENIUM_BROWSER_PATH"] = browser_path
        else:
            logger.warning("Playwright Chromium not found, SeleniumBase will use system browser")

        # Run sync Selenium in thread pool to avoid blocking
        import asyncio
        loop = asyncio.get_event_loop()

        def _scrape_sync():
            # Use undetected Chrome mode with Playwright's Chromium
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

        # Check for security checkpoint pages (bot protection)
        if is_security_checkpoint(title, clean_markdown, url):
            logger.warning(f"Security checkpoint detected for {url}: {title}")
            return build_error_response(url, "selenium", "Blocked: Security checkpoint - bot verification required")

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
    css_selector: str | None = None,
    text_only: bool = False
) -> dict:
    """
    Main scraping routing logic with fallback chain

    Flow:
    1. Check if PDF → Use PDF scraper
    2. Check blacklist
    3. Reddit? → JSON API
    4. Force method? → Use it directly (no retries)
    5. DB prefers selenium? → Start with Selenium retries (3x)
    6. Try Crawl4AI with retries (3x)
    7. If Crawl4AI exhausted → Try Selenium with retries (3x)
    8. If all attempts fail: record failure (blacklist after 3 total failures)

    Retry logic prevents false "selenium-only" marking from temporary issues.

    Args:
        url: URL to scrape
        cleaner: ContentCleaner instance
        db: Database instance
        force_method: Force specific scraping method
        css_selector: Optional CSS selector for targeted extraction
        text_only: If True, disable images for faster loading

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

    # Force method? → Use it directly without retries
    if force_method:
        return await _scrape_with_method(url, force_method, cleaner, css_selector, text_only)

    # Check database for preferred method
    preferred = await db.get_domain_method(domain)

    # ========== RETRY LOGIC ==========
    # 1. If selenium-only preferred, try Selenium first (3x)
    # 2. Always try Crawl4AI (3x) - this prevents false "selenium-only" marks
    # 3. If Crawl4AI fails, mark selenium-only and try Selenium (3x)

    selenium_tried_first = False

    # If domain is already selenium-only, start with Selenium retries
    if preferred == "selenium":
        logger.info(f"Database prefers Selenium for {domain}, starting with Selenium retries")
        selenium_tried_first = True
        for attempt in range(1, SELENIUM_RETRY_COUNT + 1):
            logger.info(f"Selenium attempt {attempt}/{SELENIUM_RETRY_COUNT} for {url}")
            result = await scrape_selenium(url, cleaner, css_selector)
            if result["success"]:
                await db.record_success(domain, "selenium")
                return result
            logger.warning(f"Selenium attempt {attempt} failed for {url}")
        # All Selenium attempts failed - continue to try Crawl4AI
        logger.warning(f"All Selenium attempts failed for {domain}, trying Crawl4AI as fallback")

    # Try Crawl4AI with retries (always try unless already succeeded)
    for attempt in range(1, CRAWL4AI_RETRY_COUNT + 1):
        logger.info(f"Crawl4AI attempt {attempt}/{CRAWL4AI_RETRY_COUNT} for {url}")
        result = await scrape_crawl4ai(url, cleaner, css_selector, text_only)
        if result["success"]:
            await db.record_success(domain, "crawl4ai")
            return result
        logger.warning(f"Crawl4AI attempt {attempt} failed for {url}")

    # Crawl4AI exhausted - mark domain for Selenium and try Selenium
    logger.warning(f"All Crawl4AI attempts failed for {domain}, trying Selenium")
    await db.set_selenium_only(domain)

    # Try Selenium with retries (skip if we already tried it first and it failed)
    if not selenium_tried_first:
        for attempt in range(1, SELENIUM_RETRY_COUNT + 1):
            logger.info(f"Selenium attempt {attempt}/{SELENIUM_RETRY_COUNT} for {url}")
            result = await scrape_selenium(url, cleaner, css_selector)
            if result["success"]:
                await db.record_success(domain, "selenium")
                return result
            logger.warning(f"Selenium attempt {attempt} failed for {url}")

    # All attempts failed - record failure
    logger.error(f"All scraping attempts failed for {domain} (3x Crawl4AI + 3x Selenium)")
    await db.record_failure(domain, "all_methods_failed")

    return build_scrape_response(
        success=False,
        url=url,
        method="both_failed",
        error="All scraping methods failed. Try again later.",
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


async def _scrape_with_method(url: str, method: str, cleaner, css_selector: str = None, text_only: bool = False) -> dict:
    """Scrape using specific method"""
    if method == "crawl4ai":
        return await scrape_crawl4ai(url, cleaner, css_selector, text_only)
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
