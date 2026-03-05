"""Unified HTML content cleaner for LLM-ready output"""

from typing import Optional
from loguru import logger
from readability import Document
import trafilatura


class ContentCleaner:
    """
    Clean HTML to produce LLM-ready markdown

    Uses multiple strategies:
    1. Trafilatura (fast, great for articles)
    2. Readability (fallback, good for general pages)
    3. BeautifulSoup (last resort, basic cleaning)
    """

    def clean(self, html_content: str, url: str = "") -> str:
        """
        Convert HTML to clean LLM-ready markdown

        Args:
            html_content: Raw HTML
            url: Source URL (for metadata)

        Returns:
            Clean markdown content
        """
        if not html_content:
            return ""

        # Try trafilatura first (best for articles/blogs)
        content = self._try_trafilatura(html_content)
        if content and len(content) > 100:
            return self._normalize_markdown(content)

        # Fallback to readability
        content = self._try_readability(html_content)
        if content and len(content) > 100:
            return self._normalize_markdown(content)

        # Last resort - basic cleanup
        return self._basic_cleanup(html_content)

    def _try_trafilatura(self, html_content: str) -> Optional[str]:
        """Extract content using trafilatura"""
        try:
            return trafilatura.extract(
                html_content,
                include_comments=False,
                include_tables=True,
                no_fallback=False,
                output_format="markdown",
            )
        except Exception as e:
            logger.debug(f"Trafilatura failed: {e}")
            return None

    def _try_readability(self, html_content: str) -> Optional[str]:
        """Extract content using readability-lxml"""
        try:
            doc = Document(html_content)
            content_html = doc.summary()

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content_html, "lxml")

            # Remove scripts, styles, nav, footer, header, aside
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()

            return self._html_to_markdown(soup)
        except Exception as e:
            logger.debug(f"Readability failed: {e}")
            return None

    def _html_to_markdown(self, soup) -> str:
        """Convert BeautifulSoup to basic markdown"""
        lines = []

        for elem in soup.find_all([
            'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
            'p', 'li', 'ul', 'ol', 'blockquote',
            'pre', 'code', 'a', 'strong', 'em', 'br'
        ]):
            text = elem.get_text(strip=True)
            if not text:
                continue

            tag = elem.name

            if tag == 'h1':
                lines.append(f"# {text}")
            elif tag == 'h2':
                lines.append(f"## {text}")
            elif tag == 'h3':
                lines.append(f"### {text}")
            elif tag == 'h4':
                lines.append(f"#### {text}")
            elif tag == 'p':
                lines.append(text)
            elif tag == 'li':
                lines.append(f"- {text}")
            elif tag == 'blockquote':
                lines.append(f"> {text}")
            elif tag == 'pre' or tag == 'code':
                lines.append(f"```\n{text}\n```")
            elif tag == 'strong' or tag == 'b':
                lines.append(f"**{text}**")
            elif tag == 'em' or tag == 'i':
                lines.append(f"*{text}*")

        return "\n\n".join(lines)

    def _basic_cleanup(self, html_content: str) -> str:
        """Basic HTML cleanup when extractors fail"""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, "lxml")

        # Remove noise
        for tag in soup([
            "script", "style", "nav", "footer", "header",
            "aside", "iframe", "noscript"
        ]):
            tag.decompose()

        # Get text
        text = soup.get_text(separator="\n", strip=True)

        # Remove excessive whitespace
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        return "\n\n".join(lines)

    def _normalize_markdown(self, content: str) -> str:
        """Normalize markdown output"""
        if not content:
            return ""

        # Clean up common issues
        lines = content.split("\n")
        cleaned = []

        for line in lines:
            line = line.strip()
            if line:
                # Remove duplicate spaces
                line = " ".join(line.split())
                cleaned.append(line)

        return "\n\n".join(cleaned)


# Singleton
_cleaner: ContentCleaner = None


def get_content_cleaner() -> ContentCleaner:
    global _cleaner
    if _cleaner is None:
        _cleaner = ContentCleaner()
    return _cleaner
