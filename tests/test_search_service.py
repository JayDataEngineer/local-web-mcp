"""Tests for search service - re-ranking, result processing"""

import pytest
from src.services.search_service import UnifiedSearchService
from src.models.unified import SearchResult


class TestSearchResultReranking:
    """Test flash re-ranking functionality"""

    @pytest.fixture
    def search_service(self):
        return UnifiedSearchService()

    @pytest.fixture
    def sample_results(self):
        """Sample search results for testing"""
        return [
            SearchResult(
                title="Python Documentation",
                url="https://docs.python.org",
                snippet="Official Python documentation and tutorials",
                domain="docs.python.org"
            ),
            SearchResult(
                title="Random Blog Post",
                url="https://randomblog.com/post",
                snippet="Something about python maybe",
                domain="randomblog.com"
            ),
            SearchResult(
                title="Python Tutorial on GitHub",
                url="https://github.com/python/tutorial",
                snippet="Learn Python programming",
                domain="github.com"
            ),
            SearchResult(
                title="Completely Unrelated",
                url="https://example.com",
                snippet="Nothing about programming here",
                domain="example.com"
            ),
        ]

    def test_flash_rerank_prioritizes_title_matches(self, search_service, sample_results):
        """Re-ranking should prioritize query term matches in title"""
        query = "python documentation"

        reranked = search_service._flash_rerank(query, sample_results)

        # First result should have "python" and "documentation" in title
        assert "Python" in reranked[0].title
        assert "documentation" in reranked[0].title.lower()

    def test_flash_rerank_authoritative_domains_boost(self, search_service):
        """Authoritative domains should get a boost"""
        results = [
            SearchResult(
                title="Python Info",
                url="https://wikipedia.org/Python",
                snippet="Python programming language",
                domain="wikipedia.org"
            ),
            SearchResult(
                title="Python Info",
                url="https://randomsite.com/python",
                snippet="About Python",
                domain="randomsite.com"
            ),
        ]

        reranked = search_service._flash_rerank("python", results)

        # Wikipedia should be ranked higher due to authoritative domain
        assert reranked[0].domain == "wikipedia.org"

    def test_flash_rerank_empty_query(self, search_service, sample_results):
        """Empty query should return original order"""
        original_order = [r.url for r in sample_results]
        reranked = search_service._flash_rerank("", sample_results)
        new_order = [r.url for r in reranked]
        assert original_order == new_order

    def test_flash_rerank_single_result(self, search_service):
        """Single result should pass through unchanged"""
        result = SearchResult(
            title="Test",
            url="https://test.com",
            snippet="Test content",
            domain="test.com"
        )
        reranked = search_service._flash_rerank("test", [result])
        assert len(reranked) == 1
        assert reranked[0].url == "https://test.com"

    def test_flash_rerank_preserves_all_results(self, search_service, sample_results):
        """Re-ranking should not lose any results"""
        reranked = search_service._flash_rerank("python", sample_results)
        assert len(reranked) == len(sample_results)


class TestSearchResultDeduplication:
    """Test result deduplication"""

    @pytest.fixture
    def search_service(self):
        return UnifiedSearchService()

    def test_deduplicate_removes_duplicates(self, search_service):
        """Remove duplicate URLs"""
        results = [
            SearchResult(
                title="Page 1",
                url="https://example.com/page",
                snippet="Content 1",
                domain="example.com"
            ),
            SearchResult(
                title="Page 2",
                url="https://example.com/page",  # Duplicate URL
                snippet="Content 2",
                domain="example.com"
            ),
        ]

        unique = search_service._deduplicate(results)
        assert len(unique) == 1
        assert unique[0].url == "https://example.com/page"

    def test_deduplicate_keeps_first_occurrence(self, search_service):
        """Keep first occurrence of duplicate URLs"""
        results = [
            SearchResult(
                title="First",
                url="https://example.com/page",
                snippet="First content",
                domain="example.com"
            ),
            SearchResult(
                title="Second",
                url="https://example.com/page",
                snippet="Second content",
                domain="example.com"
            ),
        ]

        unique = search_service._deduplicate(results)
        assert unique[0].title == "First"

    def test_deduplicate_handles_empty_list(self, search_service):
        """Handle empty list"""
        assert search_service._deduplicate([]) == []

    def test_deduplicate_handles_no_duplicates(self, search_service):
        """Handle list with no duplicates"""
        results = [
            SearchResult(
                title="Page 1",
                url="https://example.com/page1",
                snippet="Content 1",
                domain="example.com"
            ),
            SearchResult(
                title="Page 2",
                url="https://example.com/page2",
                snippet="Content 2",
                domain="example.com"
            ),
        ]

        unique = search_service._deduplicate(results)
        assert len(unique) == 2


class TestTextCleaning:
    """Test text cleaning utilities"""

    @pytest.fixture
    def search_service(self):
        return UnifiedSearchService()

    def test_clean_removes_extra_whitespace(self, search_service):
        """Remove extra whitespace"""
        assert search_service._clean_text("hello     world") == "hello world"
        assert search_service._clean_text("test   \n\n  text") == "test text"

    def test_clean_removes_unicode_artifacts(self, search_service):
        """Remove common unicode artifacts"""
        text_with_artifacts = "hello\u2026world\u00a0test"
        cleaned = search_service._clean_text(text_with_artifacts)
        assert "\u2026" not in cleaned
        assert "\u00a0" not in cleaned

    def test_clean_strips_leading_trailing(self, search_service):
        """Strip leading and trailing whitespace"""
        assert search_service._clean_text("  hello world  ") == "hello world"

    def test_clean_empty_string(self, search_service):
        """Handle empty string"""
        assert search_service._clean_text("") == ""
        assert search_service._clean_text("   ") == ""
