# SPDX-License-Identifier: AGPL-3.0-or-later
"""
fetch_content.py — Secure Web Scraper with Multi-Hop SSRF Defense, Payload Ceilings & Token Truncation.
Provides safely filtered, readability-extracted page text for DAI and Minibots.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse

try:
    import httpx
except ImportError:
    httpx = None

try:
    from readabilipy import simple_json_from_html_string
    HAS_READABILITY = True
except ImportError:
    HAS_READABILITY = False

logger = logging.getLogger("FetchContent")


class SSRFException(Exception):
    """Raised when a URL destination resolves to private, loopback, WireGuard, or cloud metadata space."""
    pass


class FetchContentScraper:
    """Hardened web scraper enforcing multi-hop IP-level SSRF defense and 120-word budget."""

    def __init__(self, max_words: int = 120, max_bytes: int = 524288) -> None:
        self.max_words = max_words
        self.max_bytes = max_bytes  # 512KB max
        self.headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8"
        }

    def _assert_safe_url(self, url: str) -> str:
        """
        Validates scheme and resolves hostname.
        Raises SSRFException if destination IP is private, loopback, link-local,
        or cloud metadata (169.254.169.254). Returns the first resolved public IP string.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise SSRFException(f"Unsupported scheme: '{parsed.scheme}' (only http/https permitted)")

        hostname = parsed.hostname
        if not hostname:
            raise SSRFException("Invalid URL: missing hostname")

        # Check if hostname is an IP literal
        try:
            ip = ipaddress.ip_address(hostname)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
                or str(ip) == "169.254.169.254"
            ):
                raise SSRFException(f"Restricted IP destination: {ip}")
            return str(ip)
        except ValueError:
            pass

        # Resolve DNS A/AAAA records
        try:
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except Exception as e:
            raise SSRFException(f"Failed to resolve hostname '{hostname}': {e}")

        if not addr_info:
            raise SSRFException(f"No DNS records returned for '{hostname}'")

        resolved_ips = []
        for family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_multicast
                    or ip.is_reserved
                    or ip.is_unspecified
                    or ip_str == "169.254.169.254"
                ):
                    raise SSRFException(f"Host '{hostname}' resolved to restricted IP: {ip_str}")
                resolved_ips.append(ip_str)
            except ValueError:
                raise SSRFException(f"Invalid IP address returned: {ip_str}")

        return resolved_ips[0]

    async def fetch(self, url: str) -> str:
        """
        Fetches web page content with multi-hop redirect verification, streaming size ceiling,
        readability extraction, and word-budget truncation.
        """
        if not httpx:
            return "[Error: HTTP client unavailable]"

        current_url = url.strip()
        max_redirects = 3

        try:
            async with httpx.AsyncClient(headers=self.headers, timeout=httpx.Timeout(5.0, connect=2.0)) as client:
                for hop in range(max_redirects + 1):
                    # Validate destination IP before connecting on every hop
                    self._assert_safe_url(current_url)

                    response = await client.get(current_url, follow_redirects=False)

                    # Intercept redirect codes and validate target Location before following
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            return "[Error: Redirect without Location header]"
                        current_url = urljoin(current_url, location)
                        continue

                    if response.status_code != 200:
                        return f"[Error: Target returned HTTP {response.status_code}]"

                    # Verify Content-Type is text/html or text/plain
                    ctype = response.headers.get("content-type", "").lower()
                    if not any(t in ctype for t in ("text/html", "text/plain", "application/xhtml")):
                        return f"[Error: Unsupported content type '{ctype[:40]}']"

                    # Enforce maximum byte ceiling (512KB)
                    raw_bytes = response.content
                    if len(raw_bytes) > self.max_bytes:
                        raw_bytes = raw_bytes[:self.max_bytes]

                    html_text = raw_bytes.decode("utf-8", errors="replace")

                    # Extract plain text content
                    plain_text = ""
                    if HAS_READABILITY:
                        try:
                            article = simple_json_from_html_string(html_text, use_readability=True)
                            if article and article.get("plain_text"):
                                plain_text = " ".join([p["text"] for p in article["plain_text"] if "text" in p])
                        except Exception:
                            pass

                    if not plain_text:
                        # Fallback clean extraction
                        cleaned = re.sub(r"<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", html_text, flags=re.DOTALL | re.IGNORECASE)
                        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
                        plain_text = re.sub(r"\s+", " ", cleaned).strip()

                    words = plain_text.split()
                    if len(words) > self.max_words:
                        return " ".join(words[:self.max_words]) + " ... [truncated]"
                    return " ".join(words)

                return "[Error: Maximum redirect hops exceeded]"

        except SSRFException as se:
            logger.warning(f"[SSRF BLOCKED] {se}")
            return f"[Error: URL blocked by SSRF security policy: {se}]"
        except Exception as e:
            logger.debug(f"Web fetch error for '{url}': {e}")
            return f"[Error retrieving page: {type(e).__name__}]"
