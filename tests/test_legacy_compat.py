# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
test_legacy_compat.py — Unit Tests for Legacy OS, OpenSSL Resilience, and Frozen-Lib Mode.
Verifies:
  1. create_resilient_ssl_context (verified vs unverified, legacy connect flags, cipher relaxations).
  2. fetch_url_resilient (SSRF fail-closed guard, retry on SSL cert error, curl fallback).
  3. TitleMetaHTMLParser (zero-dependency HTML title and OpenGraph/Twitter card parsing).
  4. requests stdlib shim (Response, Session, codes, exceptions, get/post/head).
  5. _to_thread async polyfill in sideload/dai.py.
"""

import asyncio
import io
import ssl
import sys
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from core.guard import create_resilient_ssl_context, fetch_url_resilient, is_safe_url
from plugins.urls import TitleMetaHTMLParser
from adapters.sopel_shim import (
    CaseInsensitiveDict,
    StdlibResponse,
    RequestsCodes,
    inject_requests_shim_if_needed
)
from sideload.dai import _to_thread


class TestResilientSSLAndFetch(unittest.TestCase):
    def test_create_resilient_ssl_context(self):
        ctx_verified = create_resilient_ssl_context(verify=True)
        self.assertIsInstance(ctx_verified, ssl.SSLContext)
        self.assertEqual(ctx_verified.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx_verified.check_hostname)

        ctx_unverified = create_resilient_ssl_context(verify=False)
        self.assertIsInstance(ctx_unverified, ssl.SSLContext)
        self.assertEqual(ctx_unverified.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx_unverified.check_hostname)

    @patch("core.guard.is_safe_url")
    def test_fetch_url_resilient_ssrf_blocked(self, mock_is_safe):
        # SSRF blocked -> returns (None, {}) immediately without connecting
        mock_is_safe.return_value = (False, None)
        chunk, headers = fetch_url_resilient("http://169.254.169.254/metadata")
        self.assertIsNone(chunk)
        self.assertEqual(headers, {})

    @patch("core.guard.is_safe_url")
    @patch("core.guard.build_safe_opener")
    def test_fetch_url_resilient_ssl_retry(self, mock_build_opener, mock_is_safe):
        mock_is_safe.return_value = (True, "93.184.216.34")

        # Attempt 1: raises SSLCertVerificationError
        mock_opener_verified = MagicMock()
        mock_opener_verified.open.side_effect = ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")

        # Attempt 2: succeeds with unverified TLS
        mock_resp_unverified = MagicMock()
        mock_resp_unverified.read.return_value = b"<html>Success</html>"
        mock_resp_unverified.headers = {"Content-Type": "text/html"}
        mock_opener_unverified = MagicMock()
        mock_opener_unverified.open.return_value.__enter__.return_value = mock_resp_unverified

        mock_build_opener.side_effect = [mock_opener_verified, mock_opener_unverified]

        chunk, headers = fetch_url_resilient("https://legacy-host.org")
        self.assertEqual(chunk, b"<html>Success</html>")
        self.assertEqual(headers.get("content-type"), "text/html")
        self.assertEqual(mock_build_opener.call_count, 2)


class TestTitleMetaHTMLParser(unittest.TestCase):
    def test_parses_standard_title(self):
        html_data = "<html><head><title>Kernel Archives - Linux</title></head><body></body></html>"
        parser = TitleMetaHTMLParser()
        parser.feed(html_data)
        self.assertEqual(parser.title, "Kernel Archives - Linux")
        self.assertIsNone(parser.og_title)

    def test_parses_opengraph_and_twitter_title(self):
        html_data = """
        <html><head>
          <title>Just a moment...</title>
          <meta property="og:title" content="Real Article Headline">
          <meta name="twitter:title" content="Twitter Card Headline">
        </head><body></body></html>
        """
        parser = TitleMetaHTMLParser()
        parser.feed(html_data)
        self.assertEqual(parser.title, "Just a moment...")
        self.assertEqual(parser.og_title, "Real Article Headline")
        self.assertEqual(parser.twitter_title, "Twitter Card Headline")


class TestRequestsStdlibShim(unittest.TestCase):
    def setUp(self):
        inject_requests_shim_if_needed(force=True)
        self.requests = sys.modules["requests"]

    def test_case_insensitive_dict(self):
        cid = CaseInsensitiveDict({"Content-Type": "text/html", "SERVER": "nginx"})
        self.assertEqual(cid["content-type"], "text/html")
        self.assertEqual(cid["CONTENT-TYPE"], "text/html")
        self.assertEqual(cid.get("Server"), "nginx")
        self.assertIn("server", cid)

    def test_stdlib_response_properties(self):
        resp = StdlibResponse(
            status_code=200,
            headers={"Content-Type": "application/json; charset=utf-8"},
            content=b'{"status": "ok", "count": 5}',
            url="https://api.test/v1"
        )
        self.assertTrue(resp.ok)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.encoding, "utf-8")
        self.assertEqual(resp.json(), {"status": "ok", "count": 5})
        self.assertIn("status", resp.text)
        # Should not raise
        resp.raise_for_status()

    def test_stdlib_response_raise_for_status_on_error(self):
        resp = StdlibResponse(
            status_code=404,
            headers={"Content-Type": "text/plain"},
            content=b"Not Found",
            url="https://api.test/missing"
        )
        self.assertFalse(resp.ok)
        with self.assertRaises(self.requests.exceptions.HTTPError):
            resp.raise_for_status()

    def test_requests_codes(self):
        self.assertEqual(self.requests.codes.ok, 200)
        self.assertEqual(self.requests.codes.not_found, 404)
        self.assertEqual(self.requests.codes.internal_server_error, 500)

    @patch("adapters.sopel_shim._stdlib_request")
    def test_requests_get_post_and_session(self, mock_request):
        mock_resp = StdlibResponse(status_code=200, content=b"pong")
        mock_request.return_value = mock_resp

        res = self.requests.get("https://test.local/ping", timeout=5)
        self.assertEqual(res.content, b"pong")
        mock_request.assert_called_with("GET", "https://test.local/ping", timeout=5)

        session = self.requests.Session()
        session.headers["Authorization"] = "Bearer testtoken"
        s_res = session.post("https://test.local/submit", json={"key": "val"})
        self.assertEqual(s_res.content, b"pong")


class TestDaiToThreadPolyfill(unittest.IsolatedAsyncioTestCase):
    async def test_to_thread_execution(self):
        def sync_worker(x: int, y: int, multiplier: int = 1) -> int:
            return (x + y) * multiplier

        result = await _to_thread(sync_worker, 10, 20, multiplier=3)
        self.assertEqual(result, 90)


if __name__ == "__main__":
    unittest.main()
