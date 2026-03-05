"""Unified HTML content cleaner for LLM-ready output

CONSISTENT MARKDOWN OUTPUT: All paths use markdownify for conversion.
Different parsers are used ONLY for content extraction (getting clean HTML).
"""

from typing import Optional
from loguru import logger
from bs4 import BeautifulSoup
from markdownify import markdownify as md

# Try to import selectolax for ultra-fast parsing
try:
    from selectolax.parser import HTMLParser
    SELECTOLAX_AVAILABLE = True
except ImportError:
    SELECTOLAX_AVAILABLE = False
    logger.warning("selectolax not available, using fallback parsers")

# Import readability for content extraction
try:
    from readability import Document
    READABILITY_AVAILABLE = True
except ImportError:
    READABILITY_AVAILABLE = False
    logger.warning("readability-lxml not available")


class ContentCleaner:
    """
    Clean HTML to produce LLM-ready markdown

    STRATEGY: Multiple extractors, ONE markdown converter (markdownify)

    Extraction priority (for getting clean HTML):
    1. CSS selector (if provided)
    2. selectolax (fastest, finds <main>/<article>)
    3. readability (Mozilla's algorithm, great for articles)
    4. trafilatura (fallback for news/blogs)
    5. BeautifulSoup basic (nuclear option)

    ALL extracted HTML is converted to markdown via markdownify,
    ensuring CONSISTENT output format regardless of which extractor succeeds.
    """

    def clean(self, html_content: str, url: str = "", css_selector: str = None) -> str:
        """
        Convert HTML to clean LLM-ready markdown

        Args:
            html_content: Raw HTML
            url: Source URL (for metadata/logging)
            css_selector: Optional CSS selector for targeted extraction

        Returns:
            Clean markdown content (consistent format via markdownify)
        """
        if not html_content:
            return ""

        # Step 1: Extract core content HTML (using best available parser)
        core_html = self._extract_core_html(html_content, css_selector)

        if not core_html:
            logger.debug(f"No content extracted for {url}")
            return ""

        # Step 2: Convert to CONSISTENT markdown (single source of truth)
        return self._html_to_consistent_markdown(core_html)

    def _extract_core_html(self, html_content: str, css_selector: str = None) -> Optional[str]:
        """Extract core content HTML using best available parser"""

        # Priority 1: CSS selector (if provided)
        if css_selector:
            html = self._extract_by_css_selector(html_content, css_selector)
            if html:
                return html

        # Priority 2: selectolax (fastest, finds main content areas)
        if SELECTOLAX_AVAILABLE:
            html = self._extract_with_selectolax(html_content)
            if html:
                return html

        # Priority 3: readability (Mozilla's Readability algorithm)
        if READABILITY_AVAILABLE:
            html = self._extract_with_readability(html_content)
            if html:
                return html

        # Priority 4: trafilatura (great for articles/blogs)
        html = self._extract_with_trafilatura(html_content)
        if html:
            return html

        # Priority 5: BeautifulSoup basic extraction
        return self._extract_basic(html_content)

    def _html_to_consistent_markdown(self, html: str) -> str:
        """
        Convert HTML to markdown using markdownify (CONSISTENT OUTPUT)

        This is the ONLY place where markdown conversion happens.
        All extraction paths feed into this, ensuring identical output format.
        """
        try:
            soup = BeautifulSoup(html, "html.parser")

            # Brutally remove all noise that shouldn't be in markdown
            for tag in soup(["script", "style", "noscript", "iframe", "svg", "nav", "footer"]):
                tag.decompose()

            # Convert to markdown with consistent settings
            markdown = md(
                soup,
                heading_style="ATX",  # # ## ### style (not underline)
                strip=["script", "style", "noscript"],
                strip_comments=True,
                convert=["p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
                        "strong", "em", "a", "blockquote", "pre", "code", "hr", "br",
                        "table", "thead", "tbody", "tr", "th", "td"]
            )

            # Normalize whitespace
            return self._normalize_whitespace(markdown)

        except Exception as e:
            logger.warning(f"markdownify conversion failed: {e}")
            return self._normalize_whitespace(soup.get_text(separator="\n"))

    def _normalize_whitespace(self, text: str) -> str:
        """Normalize whitespace in markdown output"""
        if not text:
            return ""

        import re

        # Remove duplicate spaces within lines
        text = re.sub(r' +', ' ', text)

        # Compress multiple blank lines to max one
        text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)

        # Strip leading/trailing whitespace from each line
        lines = [line.strip() for line in text.split('\n') if line.strip()]

        return '\n\n'.join(lines)

    # ========== EXTRACTION METHODS (HTML output only) ==========

    def _extract_by_css_selector(self, html_content: str, css_selector: str) -> Optional[str]:
        """Extract HTML using CSS selector"""
        try:
            if SELECTOLAX_AVAILABLE:
                parser = HTMLParser(html_content)
                nodes = parser.css(css_selector)

                if not nodes:
                    return None

                # Combine matched nodes into single HTML string
                html_parts = []
                for node in nodes:
                    html_parts.append(node.html)
                return "".join(html_parts) if html_parts else None

            else:
                # Fallback to BeautifulSoup
                soup = BeautifulSoup(html_content, "lxml")
                results = soup.select(css_selector)

                if not results:
                    return None

                return "".join(str(tag) for tag in results)

        except Exception as e:
            logger.warning(f"CSS extraction failed: {e}")
            return None

    def _extract_with_selectolax(self, html_content: str) -> Optional[str]:
        """Extract core content HTML using selectolax (fastest)"""
        try:
            parser = HTMLParser(html_content)

            # Remove noise tags
            for tag in parser.tags('script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe', 'noscript'):
                tag.decompose()

            # Find main content area
            main = parser.css.first('main') or parser.css.first('article') or parser.css.first('#content') or parser.body

            if not main:
                return None

            # Return HTML string (no markdown conversion here)
            return main.html

        except Exception as e:
            logger.debug(f"selectolax extraction failed: {e}")
            return None

    def _extract_with_readability(self, html_content: str) -> Optional[str]:
        """Extract core content HTML using Mozilla's Readability"""
        try:
            doc = Document(html_content)
            # Returns HTML string (no markdown conversion here)
            return doc.summary()
        except Exception as e:
            logger.debug(f"Readability extraction failed: {e}")
            return None

    def _extract_with_trafilatura(self, html_content: str) -> Optional[str]:
        """
        Extract core content HTML using trafilatura

        Note: We extract as HTML, not markdown, to feed into markdownify
        """
        try:
            import trafilatura
            # Extract as HTML first
            html = trafilatura.extract(
                html_content,
                include_comments=False,
                include_tables=True,
                no_fallback=False,
                output_format="html"  # Get HTML, not markdown
            )
            return html
        except Exception as e:
            logger.debug(f"Trafilatura extraction failed: {e}")
            return None

    def _extract_basic(self, html_content: str) -> Optional[str]:
        """Basic HTML extraction when all else fails"""
        try:
            soup = BeautifulSoup(html_content, "lxml")

            # Remove noise tags
            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe", "noscript"]):
                tag.decompose()

            # Try to find main content
            main = (soup.find("main") or soup.find("article") or
                    soup.find("div", class_=lambda x: x and ("content" in x.lower() or "main" in x.lower() or "article" in x.lower())) or
                    soup.body)

            if main:
                return str(main)

            return str(soup)

        except Exception as e:
            logger.warning(f"Basic extraction failed: {e}")
            return None


# Singleton
_cleaner: ContentCleaner = None


def get_content_cleaner() -> ContentCleaner:
    global _cleaner
    if _cleaner is None:
        _cleaner = ContentCleaner()
    return _cleaner
