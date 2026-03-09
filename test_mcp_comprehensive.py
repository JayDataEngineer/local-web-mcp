#!/usr/bin/env python3
"""Comprehensive MCP server test on multiple sites"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

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

async def test_scrape_url(client, site):
    """Test scrape_url tool"""
    try:
        result = await client.call_tool("scrape_url", {"url": site["url"]})
        import json
        data = json.loads(result.content[0].text)
        return {
            "success": data.get("success", False),
            "title": data.get("title", ""),
            "content_length": len(data.get("content", "")),
            "error": data.get("error", ""),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

async def test_crawl_site(client, site):
    """Test crawl_site tool"""
    try:
        result = await client.call_tool("crawl_site", {
            "url": site["url"],
            "max_depth": 1,
            "max_pages": 3,
            "word_count_threshold": 10  # Lower threshold for testing
        })
        import json
        data = json.loads(result.content[0].text)

        # Check for blocking
        block_detected = data.get("block_detected", False)
        error_msg = data.get("error_message", "")

        return {
            "success": data.get("successful", 0) > 0,
            "total_crawled": data.get("total_crawled", 0),
            "successful": data.get("successful", 0),
            "failed": data.get("failed", 0),
            "block_detected": block_detected,
            "error_message": error_msg,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

async def test_map_domain(client, site):
    """Test map_domain tool"""
    try:
        # Use sitemap+cc for better coverage
        result = await client.call_tool("map_domain", {
            "domain": site["url"],
            "source": "sitemap+cc",  # Use both sources for better results
            "max_urls": 10
        })
        import json
        data = json.loads(result.content[0].text)

        return {
            "success": data.get("valid_urls", 0) > 0 or data.get("total_urls", 0) > 0,
            "total_urls": data.get("total_urls", 0),
            "valid_urls": data.get("valid_urls", 0),
            "source_used": data.get("source_used", ""),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

async def run_tests():
    """Run comprehensive tests"""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(url="http://localhost:8000/mcp")
    client = Client(transport)

    print("=" * 70)
    print(f"MCP Server Comprehensive Test - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    print()

    all_results = []

    async with client:
        for i, site in enumerate(TEST_SITES, 1):
            print(f"[{i}/{len(TEST_SITES)}] Testing: {site['name']}")
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
            print("  Testing scrape_url...")
            scrape_result = await test_scrape_url(client, site)
            site_results["scrape_url"] = scrape_result
            status = "PASS" if scrape_result["success"] else "FAIL"
            print(f"    {status} - Success={scrape_result['success']}, Content={scrape_result.get('content_length', 0)} chars")
            if scrape_result.get("error"):
                print(f"    Error: {scrape_result['error'][:80]}")

            # Test crawl_site
            print("  Testing crawl_site...")
            crawl_result = await test_crawl_site(client, site)
            site_results["crawl_site"] = crawl_result
            status = "PASS" if crawl_result["success"] else "FAIL"
            print(f"    {status} - Crawled={crawl_result.get('successful', 0)}/{crawl_result.get('total_crawled', 0)} pages")
            if crawl_result.get("block_detected"):
                print(f"    WARNING - BLOCKED: {crawl_result.get('error_message', 'Unknown block')}")
            elif crawl_result.get("error"):
                print(f"    Error: {crawl_result['error'][:80]}")

            # Test map_domain
            print("  Testing map_domain...")
            map_result = await test_map_domain(client, site)
            site_results["map_domain"] = map_result
            status = "PASS" if map_result["success"] else "FAIL"
            print(f"    {status} - URLs found: {map_result.get('valid_urls', 0)}")
            print(f"    Source: {map_result.get('source_used', 'unknown')}")
            if map_result.get("error"):
                print(f"    Error: {map_result['error'][:80]}")

            all_results.append(site_results)
            print()

    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)

    scrape_success = sum(1 for r in all_results if r["scrape_url"] and r["scrape_url"]["success"])
    crawl_success = sum(1 for r in all_results if r["crawl_site"] and r["crawl_site"]["success"])
    map_success = sum(1 for r in all_results if r["map_domain"] and r["map_domain"]["success"])

    print(f"\nscrape_url:  {scrape_success}/{len(all_results)} sites successful")
    print(f"crawl_site:  {crawl_success}/{len(all_results)} sites successful")
    print(f"map_domain:  {map_success}/{len(all_results)} sites successful")

    # Check for blocking
    blocked = [r for r in all_results if r["crawl_site"] and r["crawl_site"].get("block_detected")]
    if blocked:
        print(f"\nWARNING: Blocked on {len(blocked)} sites:")
        for r in blocked:
            print(f"  - {r['site']}: {r['crawl_site'].get('error_message', 'Unknown')}")

    print("\n" + "=" * 70)
    print("Test complete!")
    print("=" * 70)

    return True

if __name__ == "__main__":
    success = asyncio.run(run_tests())
    sys.exit(0 if success else 1)
