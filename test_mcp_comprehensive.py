#!/usr/bin/env python3
"""Comprehensive MCP server test - all endpoints + stress test"""

import asyncio
import sys
import time
import json
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

# Test sites with different characteristics
TEST_SITES = [
    {
        "name": "example.com (simple static)",
        "url": "https://example.com",
        "has_sitemap": False,
        "expectations": {"scrape": True, "crawl": True, "map": False}
    },
    {
        "name": "wikipedia.org (large, complex)",
        "url": "https://en.wikipedia.org/wiki/Web_scraping",
        "has_sitemap": True,
        "expectations": {"scrape": True, "crawl": True, "map": True}
    },
    {
        "name": "python.org docs (documentation)",
        "url": "https://docs.python.org/3/",
        "has_sitemap": True,
        "expectations": {"scrape": True, "crawl": True, "map": True}
    },
]

# URLs for concurrent testing
STRESS_TEST_URLS = [
    "https://example.com",
    "https://httpbin.org/html",
    "https://httpbin.org/json",
    "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
    "https://www.python.org",
    "https://www.gnu.org",
    "https://www.eff.org",
    "https://www.wikipedia.org",
]


async def test_scrape_url(client, url: str) -> dict:
    """Test scrape_url tool"""
    try:
        result = await client.call_tool("scrape_url", {"url": url})
        data = json.loads(result.content[0].text)
        return {
            "success": data.get("success", False),
            "title": data.get("title", ""),
            "content_length": len(data.get("content", "")),
            "method": data.get("method_used", ""),
            "cached": data.get("cached", False),
            "error": data.get("error", ""),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def test_crawl_site(client, url: str) -> dict:
    """Test crawl_site tool"""
    try:
        result = await client.call_tool("crawl_site", {
            "url": url,
            "max_depth": 1,
            "max_pages": 3,
            "word_count_threshold": 10
        })
        data = json.loads(result.content[0].text)
        return {
            "success": data.get("successful", 0) > 0,
            "total_crawled": data.get("total_crawled", 0),
            "successful": data.get("successful", 0),
            "failed": data.get("failed", 0),
            "block_detected": data.get("block_detected", False),
            "error_message": data.get("error_message", ""),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def test_map_domain(client, domain: str) -> dict:
    """Test map_domain tool"""
    try:
        result = await client.call_tool("map_domain", {
            "domain": domain,
            "source": "sitemap+cc",
            "max_urls": 10
        })
        data = json.loads(result.content[0].text)
        return {
            "success": data.get("valid_urls", 0) > 0 or data.get("total_urls", 0) > 0,
            "total_urls": data.get("total_urls", 0),
            "valid_urls": data.get("valid_urls", 0),
            "source_used": data.get("source_used", ""),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def test_search_web(client, query: str, time_filter: str = None) -> dict:
    """Test search_web tool with optional time filter"""
    try:
        params = {"query": query, "pages": 1, "top_k": 5}
        if time_filter:
            params["time_filter"] = time_filter

        result = await client.call_tool("search_web", params)
        data = json.loads(result.content[0].text)
        return {
            "success": data.get("total_results", 0) > 0,
            "total_results": data.get("total_results", 0),
            "results_count": len(data.get("results", [])),
            "search_time_ms": data.get("search_time_ms", 0),
            "error": str(data.get("error", "")),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def test_scrape_structured(client, url: str) -> dict:
    """Test scrape_structured tool (uses Crawl4AI arun_many)"""
    try:
        result = await client.call_tool("scrape_structured", {
            "url": url,
            "extract_depth_strategy": "auto"
        })
        data = json.loads(result.content[0].text)
        return {
            "success": data.get("success", False),
            "is_list": data.get("is_list", False),
            "has_markdown": bool(data.get("markdown")),
            "item_count": data.get("item_count", 0),
            "error": data.get("error", ""),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def test_time_filters(client):
    """Test all time filter options"""
    print("\n" + "=" * 70)
    print("TIME FILTER TEST")
    print("=" * 70)

    filters = ["day", "week", "month", "year"]
    query = "AI news"

    for tf in filters:
        print(f"\nTesting time_filter='{tf}'...")
        result = await test_search_web(client, query, time_filter=tf)
        if result["success"]:
            print(f"  PASS - Found {result['results_count']} results in {result['search_time_ms']:.1f}ms")
        else:
            print(f"  FAIL - {result.get('error', 'Unknown error')}")


async def stress_test_concurrent(client, num_concurrent: int = 10):
    """Stress test with concurrent requests"""
    print("\n" + "=" * 70)
    print(f"STRESS TEST - {num_concurrent} CONCURRENT REQUESTS")
    print("=" * 70)

    # Pick URLs for concurrent testing
    test_urls = STRESS_TEST_URLS[:num_concurrent]

    start = time.time()

    # Fire all requests concurrently
    tasks = [test_scrape_url(client, url) for url in test_urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    elapsed = time.time() - start

    # Analyze results
    successes = 0
    errors = 0
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            errors += 1
            print(f"  [{i+1}] Exception: {r}")
        elif isinstance(r, dict):
            if r.get("success"):
                successes += 1
                print(f"  [{i+1}] PASS - {test_urls[i][:50]}... ({r.get('method', 'unknown')})")
            else:
                errors += 1
                print(f"  [{i+1}] FAIL - {test_urls[i][:50]}...: {r.get('error', 'unknown')[:60]}")

    print(f"\n  Total time: {elapsed:.1f}s ({elapsed/num_concurrent:.2f}s per request)")
    print(f"  Success rate: {successes}/{num_concurrent} ({100*successes/num_concurrent:.0f}%)")
    print(f"  Error rate: {errors}/{num_concurrent} ({100*errors/num_concurrent:.0f}%)")

    return successes == num_concurrent


async def test_checkpoint_detection(client):
    """Test checkpoint detection with Vercel-protected site"""
    print("\n" + "=" * 70)
    print("CHECKPOINT DETECTION TEST")
    print("=" * 70)

    # This site has Vercel protection
    test_url = "https://nextra.site/docs"

    print(f"\nTesting checkpoint detection on: {test_url}")
    result = await test_scrape_url(client, test_url)

    if result["success"]:
        print(f"  PASS - Successfully scraped with {result.get('method', 'unknown')}")
        print(f"  Title: {result.get('title', '')[:60]}")
    else:
        error = result.get("error", "")
        if "checkpoint" in error.lower():
            print(f"  EXPECTED - Checkpoint detected and handled: {error[:60]}")
        else:
            print(f"  FAIL - Other error: {error[:80]}")


async def run_tests():
    """Run comprehensive tests"""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(url="http://localhost:8000/mcp")
    client = Client(transport)

    print("=" * 70)
    print(f"MCP Server Comprehensive Test - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    all_results = []

    async with client:
        # ========== BASIC FUNCTIONAL TESTS ==========
        print("\n" + "=" * 70)
        print("BASIC FUNCTIONAL TESTS")
        print("=" * 70)

        for i, site in enumerate(TEST_SITES, 1):
            print(f"\n[{i}/{len(TEST_SITES)}] {site['name']}")
            print(f"    URL: {site['url']}")
            print("-" * 60)

            site_results = {
                "site": site["name"],
                "url": site["url"],
                "scrape_url": None,
                "crawl_site": None,
                "map_domain": None,
            }

            # Test scrape_url
            print("  scrape_url...")
            scrape_result = await test_scrape_url(client, site["url"])
            site_results["scrape_url"] = scrape_result
            status = "PASS" if scrape_result["success"] else "FAIL"
            print(f"    {status} - {scrape_result.get('content_length', 0)} chars ({scrape_result.get('method', 'unknown')})")

            # Test crawl_site
            print("  crawl_site...")
            crawl_result = await test_crawl_site(client, site["url"])
            site_results["crawl_site"] = crawl_result
            status = "PASS" if crawl_result["success"] else "FAIL"
            print(f"    {status} - {crawl_result.get('successful', 0)}/{crawl_result.get('total_crawled', 0)} pages")

            # Test map_domain
            print("  map_domain...")
            map_result = await test_map_domain(client, site["url"])
            site_results["map_domain"] = map_result
            status = "PASS" if map_result["success"] else "FAIL"
            print(f"    {status} - {map_result.get('valid_urls', 0)} URLs ({map_result.get('source_used', 'unknown')})")

            all_results.append(site_results)

        # ========== SEARCH TEST ==========
        print("\n" + "=" * 70)
        print("SEARCH TEST")
        print("=" * 70)

        print("\nTesting basic search...")
        search_result = await test_search_web(client, "python async await")
        if search_result["success"]:
            print(f"  PASS - Found {search_result['results_count']} results in {search_result['search_time_ms']:.1f}ms")
        else:
            print(f"  FAIL - {search_result.get('error', 'Unknown error')}")

        # Test time filters
        await test_time_filters(client)

        # ========== STRUCTURED SCRAPING TEST ==========
        print("\n" + "=" * 70)
        print("STRUCTURED SCRAPING TEST")
        print("=" * 70)

        structured_url = "https://example.com"
        print(f"\nTesting scrape_structured on: {structured_url}")
        structured_result = await test_scrape_structured(client, structured_url)
        if structured_result["success"]:
            print(f"  PASS - is_list={structured_result['is_list']}, markdown={structured_result['has_markdown']}")
        else:
            print(f"  Note: {structured_result.get('error', 'Unknown')[:80]}")

        # ========== CHECKPOINT DETECTION ==========
        await test_checkpoint_detection(client)

        # ========== STRESS TEST ==========
        await stress_test_concurrent(client, num_concurrent=10)

        # ========== SECOND STRESS TEST (higher load) ==========
        await stress_test_concurrent(client, num_concurrent=20)

    # ========== SUMMARY ==========
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    scrape_success = sum(1 for r in all_results if r["scrape_url"] and r["scrape_url"]["success"])
    crawl_success = sum(1 for r in all_results if r["crawl_site"] and r["crawl_site"]["success"])
    map_success = sum(1 for r in all_results if r["map_domain"] and r["map_domain"]["success"])

    print(f"\nscrape_url:    {scrape_success}/{len(all_results)} successful")
    print(f"crawl_site:    {crawl_success}/{len(all_results)} successful")
    print(f"map_domain:    {map_success}/{len(all_results)} successful")

    blocked = [r for r in all_results if r["crawl_site"] and r["crawl_site"].get("block_detected")]
    if blocked:
        print(f"\nWARNING: Blocked on {len(blocked)} sites")
        for r in blocked:
            print(f"  - {r['site']}")

    print("\n" + "=" * 70)
    print("All tests complete!")
    print("=" * 70)

    return True


if __name__ == "__main__":
    success = asyncio.run(run_tests())
    sys.exit(0 if success else 1)
