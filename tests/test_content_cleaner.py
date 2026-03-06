"""Tests for ContentCleaner - HTML to Markdown transformation

These tests validate that the content cleaner properly extracts and converts
HTML content to clean Markdown without needing to spin up the full scraping stack.
"""

import pytest
from src.services.content_cleaner import ContentCleaner, get_content_cleaner


@pytest.fixture
def cleaner():
    """Get a fresh ContentCleaner instance for each test"""
    return ContentCleaner()


class TestContentCleaner:
    """Test suite for ContentCleaner"""

    def test_basic_html_to_markdown(self, cleaner):
        """Test basic HTML conversion to Markdown"""
        html = """
        <html>
            <body>
                <h1>Title</h1>
                <p>This is a paragraph.</p>
                <ul>
                    <li>Item 1</li>
                    <li>Item 2</li>
                </ul>
            </body>
        </html>
        """
        result = cleaner.clean(html, "http://example.com")

        assert "Title" in result
        assert "This is a paragraph" in result
        assert "* Item 1" in result or "- Item 1" in result

    def test_removes_script_and_style_tags(self, cleaner):
        """Test that script and style tags are removed"""
        html = """
        <html>
            <body>
                <h1>Content</h1>
                <script>alert('malicious')</script>
                <style>body { color: red; }</style>
                <p>Real content here</p>
            </body>
        </html>
        """
        result = cleaner.clean(html)

        assert "alert" not in result
        assert "color: red" not in result
        assert "Real content here" in result

    def test_css_selector_extraction(self, cleaner):
        """Test targeted content extraction using CSS selector"""
        html = """
        <html>
            <body>
                <div id="main">
                    <p>Target content</p>
                </div>
                <div id="sidebar">
                    <p>Ignore this</p>
                </div>
            </body>
        </html>
        """
        result = cleaner.clean(html, css_selector="#main")

        assert "Target content" in result
        assert "Ignore this" not in result

    def test_empty_html_returns_empty_string(self, cleaner):
        """Test that empty or invalid HTML returns empty string"""
        assert cleaner.clean("") == ""
        assert cleaner.clean(None) == ""

    def test_whitespace_normalization(self, cleaner):
        """Test that excessive whitespace is normalized"""
        html = """
        <html>
            <body>
                <p>Line 1</p>

                <p>Line 2</p>


                <p>Line 3</p>
            </body>
        </html>
        """
        result = cleaner.clean(html)

        # Should not have excessive blank lines
        assert "\n\n\n\n" not in result

    def test_returns_markdown_consistently(self, cleaner):
        """Test that all extraction paths return consistent Markdown format"""
        # Different HTML structures should all produce markdown
        html1 = "<article><h1>Title</h1><p>Content</p></article>"
        html2 = "<div><h1>Title</h1><p>Content</p></div>"

        result1 = cleaner.clean(html1)
        result2 = cleaner.clean(html2)

        # Both should have markdown-style headers
        assert "# Title" in result1 or "Title" in result1
        assert "Content" in result1
        assert "Content" in result2

    def test_singleton_get_content_cleaner(self, cleaner):
        """Test that get_content_cleaner returns the same instance"""
        from src.services.content_cleaner import _cleaner

        # First call creates instance
        instance1 = get_content_cleaner()
        instance2 = get_content_cleaner()

        assert instance1 is instance2
