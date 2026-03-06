"""Pytest configuration and shared fixtures

This file contains fixtures and configuration that are shared across all tests.
Fixtures here are automatically available to any test file without explicit import.
"""

import pytest
import os
import sys

# Add src to path so tests can import modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def sample_html():
    """Sample HTML content for testing cleaners"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Test Page</title>
        <script>console.log('test');</script>
        <style>body { margin: 0; }</style>
    </head>
    <body>
        <nav class="navigation">Navigation Link</nav>
        <main>
            <article>
                <h1>Main Article Title</h1>
                <p>This is the first paragraph with <strong>bold text</strong>.</p>
                <p>This is the second paragraph with <a href="http://example.com">a link</a>.</p>
                <h2>Subsection</h2>
                <ul>
                    <li>List item one</li>
                    <li>List item two</li>
                </ul>
            </article>
        </main>
        <footer>Copyright 2025</footer>
    </body>
    </html>
    """


@pytest.fixture
def sample_search_results():
    """Sample SearXNG search response for testing"""
    return {
        "query": "test search",
        "results": [
            {
                "title": "Example Result 1",
                "url": "https://example.com/1",
                "content": "This is a snippet for result 1",
                "engine": "brave"
            },
            {
                "title": "Example Result 2",
                "url": "https://example.com/2",
                "content": "This is a snippet for result 2",
                "engine": "bing"
            }
        ]
    }
