# SPDX-License-Identifier: AGPL-3.0-or-later
"""
ddg_fast_api.py — Async DuckDuckGo Search Integration with URL Sanitization & Dual Fallback.
Provides lightweight, token-bounded web search snippets for Minibots and DAI.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger("DDGFastSearch")

# In-memory LRU search cache (5-minute TTL, max 100 entries)
_SEARCH_CACHE: OrderedDict[str, Tuple[float, List[Dict[str, str]]]] = OrderedDict()
_CACHE_TTL_SECONDS = 300.0
_CACHE_MAX_ENTRIES = 100

# Query parameters to strip from URLs to prevent line bloat on IRC
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "fbclid", "gclid", "msclkid", "mc_eid", "yclid"
}


def sanitize_url(url: str) -> str:
    """Removes marketing and tracking query parameters from URLs."""
    if not url or not url.startswith(("http://", "https://")):
        return url
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        query_dict = parse_qs(parsed.query, keep_blank_values=False)
        cleaned_query = {k: v for k, v in query_dict.items() if k.lower() not in TRACKING_PARAMS}
        new_query = urlencode(cleaned_query, doseq=True)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))
    except Exception:
        return url


class DDGFastSearch:
    """Async DuckDuckGo search integration with HTML scraping and Instant Answer API fallback."""

    def __init__(self, max_results: int = 2) -> None:
        self.max_results = max(1, min(max_results, 5))
        self.html_endpoint = "https://html.duckduckgo.com/html/"
        self.api_endpoint = "https://api.duckduckgo.com/"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Sec-Ch-Ua": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Upgrade-Insecure-Requests": "1",
        }

    async def search(self, query: str) -> List[Dict[str, str]]:
        """
        Executes web search for query.
        Tries DuckDuckGo HTML scraping first; falls back to Instant Answer JSON API on failure.
        """
        q = (query or "").strip()
        if not q:
            return []

        # Cache lookup
        cache_key = f"{q.lower()}:{self.max_results}"
        now = time.monotonic()
        if cache_key in _SEARCH_CACHE:
            cached_ts, cached_res = _SEARCH_CACHE[cache_key]
            if now - cached_ts < _CACHE_TTL_SECONDS:
                _SEARCH_CACHE.move_to_end(cache_key)
                return cached_res
            else:
                del _SEARCH_CACHE[cache_key]

        # Attempt 1: HTML scraping
        results = await self._search_html(q)
        if not results:
            # Attempt 2: Instant Answer API fallback
            results = await self._search_api(q)

        if results:
            if len(_SEARCH_CACHE) >= _CACHE_MAX_ENTRIES:
                _SEARCH_CACHE.popitem(last=False)
            _SEARCH_CACHE[cache_key] = (now, results)

        return results

    async def _search_html(self, query: str) -> List[Dict[str, str]]:
        """Scrapes DuckDuckGo HTML interface using lightweight regex."""
        if not httpx:
            return []

        results: List[Dict[str, str]] = []
        payload = {"q": query}

        try:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=httpx.Timeout(4.5, connect=2.0), follow_redirects=True
            ) as client:
                res = await client.post(self.html_endpoint, data=payload)
                if res.status_code != 200:
                    logger.debug(f"DDG HTML returned HTTP {res.status_code}")
                    return []

                html_text = res.text

                # Extract snippets
                snippet_matches = re.findall(
                    r'<a[^>]*class="result__snippet[^"]*"[^>]*>(.*?)</a>',
                    html_text,
                    re.DOTALL
                )

                # Extract titles and destination URLs
                link_matches = re.findall(
                    r'<a[^>]*class="result__url"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                    html_text,
                    re.DOTALL
                )

                for idx, raw_snippet in enumerate(snippet_matches[:self.max_results]):
                    clean_snippet = re.sub(r"<[^>]+>", "", raw_snippet).strip()
                    clean_snippet = " ".join(clean_snippet.split())

                    url = ""
                    title = "Web Result"

                    if idx < len(link_matches):
                        raw_href, raw_title = link_matches[idx]
                        clean_title = re.sub(r"<[^>]+>", "", raw_title).strip()
                        if clean_title:
                            title = clean_title
                        # DDG links often have uddg= wrapper
                        if "uddg=" in raw_href:
                            m = re.search(r"uddg=([^&]+)", raw_href)
                            if m:
                                import urllib.parse
                                url = urllib.parse.unquote(m.group(1))
                        else:
                            url = raw_href

                    # Skip ad links, sponsored trackers, and redirect wrappers
                    if any(bad in url.lower() for bad in ("duckduckgo.com/y.js", "ad_domain", "ad_provider", "bingv7aa", "doubleclick", "googleadservices")):
                        continue

                    if not url.startswith(("http://", "https://")) or "duckduckgo.com/?q=" in url:
                        continue

                    clean_url = sanitize_url(url)
                    results.append({
                        "title": title[:50].strip(),
                        "snippet": clean_snippet[:140].strip(),
                        "url": clean_url
                    })

        except Exception as e:
            logger.debug(f"DDG HTML search error: {e}")

        return results

    async def _search_api(self, query: str) -> List[Dict[str, str]]:
        """Queries DuckDuckGo Instant Answer JSON API."""
        if not httpx:
            return []

        results: List[Dict[str, str]] = []
        params = {
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1"
        }

        try:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=httpx.Timeout(3.5, connect=2.0)
            ) as client:
                res = await client.get(self.api_endpoint, params=params)
                if res.status_code != 200:
                    return []

                data = res.json()
                abstract = data.get("AbstractText", "").strip()
                abstract_url = data.get("AbstractURL", "").strip()
                heading = data.get("Heading", "Instant Answer").strip()

                if abstract and abstract_url and not "duckduckgo.com/?q=" in abstract_url:
                    results.append({
                        "title": heading[:50],
                        "snippet": abstract[:140],
                        "url": sanitize_url(abstract_url)
                    })

                # Check RelatedTopics if we need more results
                related = data.get("RelatedTopics", [])
                for item in related:
                    if len(results) >= self.max_results:
                        break
                    txt = item.get("Text", "").strip()
                    u = item.get("FirstURL", "").strip()
                    if txt and u:
                        results.append({
                            "title": "Topic",
                            "snippet": txt[:140],
                            "url": sanitize_url(u)
                        })

        except Exception as e:
            logger.debug(f"DDG API search error: {e}")

        return results
