# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_urls_ssrf.py — Unit Tests for SSRF IP-Pinning Defense & URL Parsing.
"""

import unittest
from unittest.mock import MagicMock, patch

import urllib.request
from core.guard import SafeProxyHandler, RotatingProxyErrorHandler, build_safe_opener, is_safe_url
from plugins.urls import URLPlugin, get_url_title


class TestSSRFGuard(unittest.TestCase):
    def test_blocks_private_and_loopback_ips(self):
        # Localhost / Loopback
        safe, _ = is_safe_url("http://127.0.0.1/admin")
        self.assertFalse(safe)

        # RFC1918 Private space
        safe, _ = is_safe_url("http://10.0.0.1:6667")
        self.assertFalse(safe)
        safe, _ = is_safe_url("http://192.168.1.1/router")
        self.assertFalse(safe)
        safe, _ = is_safe_url("http://172.16.0.5/secret")
        self.assertFalse(safe)

        # Link-local / Cloud Metadata
        safe, _ = is_safe_url("http://169.254.169.254/latest/meta-data")
        self.assertFalse(safe)

        # IPv6 Loopback
        safe, _ = is_safe_url("http://[::1]:8000")
        self.assertFalse(safe)

    @patch("core.guard.socket.getaddrinfo")
    def test_allows_public_ips(self, mock_getaddrinfo):
        mock_getaddrinfo.return_value = [
            (None, None, None, None, ("93.184.216.34", 80))
        ]
        safe, ip = is_safe_url("https://example.com")
        self.assertTrue(safe)
        self.assertEqual(ip, "93.184.216.34")

    @patch("plugins.urls.is_safe_url")
    @patch("plugins.urls.build_safe_opener")
    def test_get_url_title(self, mock_build_opener, mock_is_safe):
        mock_is_safe.return_value = (True, "93.184.216.34")

        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_resp.read.return_value = b"<html><head><title>Example Domain</title></head><body></body></html>"

        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = mock_resp
        mock_build_opener.return_value = mock_opener

        title = get_url_title("https://example.com")
        self.assertEqual(title, "Example Domain")


class TestSafeProxyHandlerAndRetry(unittest.TestCase):
    def test_safe_proxy_bypass_internal_addresses(self):
        handler = SafeProxyHandler(proxy_url="http://127.0.0.1:1088")
        # Direct loopback & local hostnames
        self.assertTrue(handler.should_bypass("127.0.0.1"))
        self.assertTrue(handler.should_bypass("localhost"))
        self.assertTrue(handler.should_bypass("::1"))
        self.assertTrue(handler.should_bypass("cluster.local"))
        self.assertTrue(handler.should_bypass("node.internal"))
        self.assertTrue(handler.should_bypass("hidden.onion"))
        self.assertTrue(handler.should_bypass("service.i2p"))

        # Private subnets
        self.assertTrue(handler.should_bypass("10.0.0.1"))
        self.assertTrue(handler.should_bypass("10.10.10.1"))
        self.assertTrue(handler.should_bypass("10.50.50.2"))

        # RFC1918 private subnets
        self.assertTrue(handler.should_bypass("192.168.1.1"))
        self.assertTrue(handler.should_bypass("10.0.0.5"))
        self.assertTrue(handler.should_bypass("172.16.0.1"))

        # Public internet destinations
        self.assertFalse(handler.should_bypass("example.com"))
        self.assertFalse(handler.should_bypass("api.github.com"))
        self.assertFalse(handler.should_bypass("93.184.216.34"))

    def test_safe_proxy_adds_fail_closed_header(self):
        handler = SafeProxyHandler(proxy_url="http://127.0.0.1:1088")
        req = urllib.request.Request("https://example.com/test")
        self.assertFalse(req.has_header("X-fail-closed"))

        with patch.object(urllib.request.ProxyHandler, "proxy_open", return_value="dispatched"):
            ret = handler.proxy_open(req, "http://127.0.0.1:1088", "https")
            self.assertEqual(ret, "dispatched")
            self.assertTrue(req.has_header("X-fail-closed"))
            self.assertEqual(req.get_header("X-fail-closed"), "1")

    def test_safe_proxy_bypasses_direct_without_proxy(self):
        handler = SafeProxyHandler(proxy_url="http://127.0.0.1:1088")
        req = urllib.request.Request("http://10.0.0.1:6667")
        ret = handler.proxy_open(req, "http://127.0.0.1:1088", "http")
        self.assertIsNone(ret)
        self.assertFalse(req.has_header("X-fail-closed"))

    def test_rotating_proxy_error_handler_429_retry(self):
        handler = RotatingProxyErrorHandler()
        parent_mock = MagicMock()
        parent_mock.open.return_value = "rotated_response"
        handler.parent = parent_mock

        req = urllib.request.Request("https://example.com/api")
        mock_fp = MagicMock()

        res = handler.http_error_429(req, mock_fp, 429, "Too Many Requests", {})
        self.assertEqual(res, "rotated_response")
        self.assertTrue(req.has_header("X-proxy-rotate"))
        self.assertEqual(req.get_header("X-proxy-rotate"), "1")
        mock_fp.close.assert_called_once()

    def test_rotating_proxy_error_handler_prevents_infinite_loop(self):
        handler = RotatingProxyErrorHandler()
        parent_mock = MagicMock()
        handler.parent = parent_mock

        req = urllib.request.Request("https://example.com/api")
        req.add_header("X-Proxy-Rotate", "1")
        mock_fp = MagicMock()

        # When X-Proxy-Rotate is already present, handler returns None to fail naturally
        res = handler.http_error_429(req, mock_fp, 429, "Too Many Requests", {})
        self.assertIsNone(res)
        parent_mock.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
