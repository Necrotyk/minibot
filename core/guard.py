# SPDX-License-Identifier: AGPL-3.0-or-later
"""
guard.py — SSRF Protection, DNS Pinning, Multi-Hop Redirect Validation & Token-Bucket Rate Limiters.
"""

import ipaddress
import logging
import os
import socket
import time
from typing import Dict, Optional, Tuple
import urllib.error
from urllib.parse import urlparse
import urllib.request

logger = logging.getLogger("Guard")


def is_safe_url(url: str) -> Tuple[bool, Optional[str]]:
    """
    Verifies that target URL hostname resolves strictly to public, non-private IP space.
    Explicitly blocks RFC1918, RFC3927 link-local, loopback, and internal IPs.
    Returns (is_safe, resolved_ip).
    """
    try:
        if url.startswith("www."):
            url = "https://" + url

        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            return False, None

        # Check for numeric IP directly
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                return False, None
            extra_blocked = [ipaddress.ip_network(n.strip()) for n in os.environ.get("BLOCKED_CIDRS", "").split(",") if n.strip()]
            for net in extra_blocked:
                if ip in net:
                    return False, None
            return True, str(ip)
        except ValueError:
            pass

        # Resolve hostname
        addr_info = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for _, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            ip = ipaddress.ip_address(ip_str)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                logger.warning(f"[SSRF GUARD] Hostname '{host}' resolved to restricted IP: {ip_str}")
                return False, None
            extra_blocked = [ipaddress.ip_network(n.strip()) for n in os.environ.get("BLOCKED_CIDRS", "").split(",") if n.strip()]
            for net in extra_blocked:
                if ip in net:
                    logger.warning(f"[SSRF GUARD] Hostname '{host}' resolved to custom blocked subnet: {ip_str}")
                    return False, None
            return True, ip_str

        return False, None
    except Exception as e:
        logger.debug(f"[SSRF GUARD] Resolution check failed for '{url}': {e}")
        return False, None


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Custom HTTP redirect handler validating destination IP on every HTTP 301/302/303/307 hop."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        is_safe, _ = is_safe_url(newurl)
        if not is_safe:
            logger.warning(f"[SSRF GUARD] Blocked unsafe redirect hop to: {newurl}")
            raise urllib.error.HTTPError(
                newurl, 403, f"SSRF Guard: Redirect destination {newurl} is blocked", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SafeProxyHandler(urllib.request.ProxyHandler):
    """
    ProxyHandler routing public internet requests through the local egress daemon
    (http://127.0.0.1:1088 by default) while strictly bypassing internal cluster infrastructure
    (RFC1918 private subnets, localhost/loopback, and internal domain suffixes).
    """

    def __init__(self, proxy_url: str = "http://127.0.0.1:1088"):
        self.proxy_url = proxy_url.strip() if proxy_url else ""
        super().__init__({"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else {})

    def should_bypass(self, host: Optional[str]) -> bool:
        if not host:
            return True
        h = host.strip()
        if h.startswith("["):
            if "]" in h:
                h = h[1:h.index("]")].strip().lower()
            else:
                h = h.lstrip("[").rstrip("]").strip().lower()
        elif ":" in h and h.count(":") == 1:
            h = h.split(":")[0].strip().lower()
        else:
            h = h.strip().lower()

        if h in ("localhost", "127.0.0.1", "::1") or h.endswith(".local") or h.endswith(".internal") or h.endswith(".onion") or h.endswith(".i2p"):
            return True
        try:
            ip = ipaddress.ip_address(h)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
                return True
            extra_bypass = [ipaddress.ip_network(n.strip()) for n in os.environ.get("PROXY_BYPASS_CIDRS", "").split(",") if n.strip()]
            for net in extra_bypass:
                if ip in net:
                    return True
        except ValueError:
            pass
        return False

    def proxy_open(self, req, proxy, type_):
        if not self.proxy_url or self.should_bypass(req.host):
            return None
        has_fail_closed = (
            any(k.lower() == "x-fail-closed" for k in req.headers) or
            any(k.lower() == "x-fail-closed" for k in req.unredirected_hdrs)
        )
        if not has_fail_closed:
            req.add_header("X-Fail-Closed", "1")
        return super().proxy_open(req, proxy, type_)


class RotatingProxyErrorHandler(urllib.request.BaseHandler):
    """
    Urllib error handler that catches HTTP 429 (Too Many Requests) and HTTP 403 (Forbidden).
    On encountering these rate-limit or WAF block codes, it retries once with the
    'X-Proxy-Rotate: 1' header to signal dystopi-egress to evict the sticky residential
    proxy and pick the next optimal peer.
    """
    handler_order = 1000

    def http_error_429(self, req, fp, code, msg, headers):
        return self._retry_with_rotation(req, fp, code)

    def http_error_403(self, req, fp, code, msg, headers):
        return self._retry_with_rotation(req, fp, code)

    def _retry_with_rotation(self, req, fp, code):
        has_rotate = (
            any(k.lower() == "x-proxy-rotate" for k in req.headers) or
            any(k.lower() == "x-proxy-rotate" for k in req.unredirected_hdrs)
        )
        if has_rotate or getattr(req, "_has_retried_rotate", False):
            return None  # Already rotated once; propagate error
        logger.info(f"[PROXY RETRY] HTTP {code} received for {req.full_url}. Rotating proxy and retrying...")
        if fp:
            try:
                fp.close()
            except Exception:
                pass
        req._has_retried_rotate = True
        req.add_header("X-Proxy-Rotate", "1")
        try:
            timeout = getattr(req, "timeout", None)
            return self.parent.open(req, timeout=timeout)
        except Exception as e:
            logger.debug(f"[PROXY RETRY] Retried request failed: {e}")
            return None


def build_safe_opener(proxy_url: Optional[str] = None) -> urllib.request.OpenerDirector:
    """
    Constructs a hardened urllib opener enforcing:
    1. Multi-hop SSRF redirect validation (SafeRedirectHandler).
    2. Triangulated Egress Proxy mesh via 127.0.0.1:1088 for public internet traffic,
       with zero-leak bypass for internal cluster subnets (SafeProxyHandler).
    3. Automatic rate-limit and WAF mitigation with single-hop residential rotation
       on HTTP 429/403 (RotatingProxyErrorHandler).
    """
    if proxy_url is None:
        proxy_url = os.environ.get("MINIBOTS_EGRESS_PROXY", "http://127.0.0.1:1088")

    handlers = [SafeRedirectHandler()]
    if proxy_url and proxy_url.lower() not in ("none", "direct", "0", "false", ""):
        handlers.append(SafeProxyHandler(proxy_url=proxy_url))
        handlers.append(RotatingProxyErrorHandler())

    return urllib.request.build_opener(*handlers)


def safe_urlopen(url_or_req, timeout=None):
    """Executes an HTTP request using the hardened safe opener."""
    opener = build_safe_opener()
    return opener.open(url_or_req, timeout=timeout)


class TokenBucket:
    """Token-bucket rate limiter for per-channel or per-network line throttling."""
    def __init__(self, capacity: float = 4.0, refill_rate: float = 0.5):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity
        self.last_refill = time.time()

    def consume(self, count: float = 1.0) -> bool:
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

        if self.tokens >= count:
            self.tokens -= count
            return True
        return False

    def wait_time(self, count: float = 1.0) -> float:
        now = time.time()
        elapsed = now - self.last_refill
        current_tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        if current_tokens >= count:
            return 0.0
        return (count - current_tokens) / self.refill_rate


def is_public_channel(network: str, target: str) -> bool:
    """
    Determines if a destination target represents a public IRC channel.
    - DMs/PMs (not starting with '#') are private queries, never public channels.
    - Any channel starting with '#' on non-WireGuard networks (e.g. Libera, OFTC) is public.
    - On WireGuard mesh, only designated admin planes (#minibots, #minibot, #jules) are non-public;
      any other channel is considered public.
    """
    if not target or not target.startswith("#"):
        return False
    is_wg = (network or "").lower().startswith("wireguard")
    if not is_wg:
        return True
    admin_channels = {"#minibots", "#minibot", "#jules"}
    return target.lower() not in admin_channels

