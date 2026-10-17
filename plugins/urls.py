# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
urls.py — Real-Time URL Previewer with SSRF IP-Pinning Defense.
"""

import gzip
import html
import html.parser
import logging
import re
import shutil
import socket
import ssl
import subprocess
import urllib.error
import urllib.request
import zlib
from typing import Any, Optional
from urllib.parse import urlparse

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except Exception:
    HAS_BS4 = False

from core.guard import build_safe_opener, fetch_url_resilient, is_safe_url
from core.ignore_manager import get_ignore_manager

logger = logging.getLogger("URLPreviewer")

URL_REGEX = re.compile(r'(https?://[^\s<>"]+|www\.[^\s<>"]+)', re.IGNORECASE)

BARE_SEARCH_DOMAINS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "google.com", "www.google.com"
}

GENERIC_TITLES = {
    "just a moment...", "redirecting...", "attention required!",
    "cloudflare", "ddos-guard", "security check", "access denied",
    "403 forbidden", "404 not found", "robot or human?",
    "verify you are human", "please wait...", "before you continue to youtube"
}


def strip_url_punctuation(url: str) -> str:
    """Strips trailing punctuation while preserving balanced parentheses and brackets."""
    url = url.strip()
    while url and url[-1] in '.,;:!?"\'>)]}':
        if url[-1] == ')' and url.count('(') >= url.count(')'):
            break
        if url[-1] == ']' and url.count('[') >= url.count(']'):
            break
        if url[-1] == '}' and url.count('{') >= url.count('}'):
            break
        url = url[:-1]
    return url


def format_size(size_bytes: int) -> str:
    """Formats bytes into human-readable size string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def decompress_payload(chunk: bytes, content_encoding: str) -> bytes:
    """Safely decompresses gzip/deflate response chunks with streaming fallback."""
    enc = (content_encoding or "").lower()
    if "gzip" in enc or chunk.startswith(b"\x1f\x8b"):
        try:
            return gzip.decompress(chunk)
        except Exception:
            try:
                d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                return d.decompress(chunk)
            except Exception:
                return chunk
    elif "deflate" in enc:
        try:
            return zlib.decompress(chunk)
        except Exception:
            try:
                return zlib.decompress(chunk, -zlib.MAX_WBITS)
            except Exception:
                try:
                    d = zlib.decompressobj(-zlib.MAX_WBITS)
                    return d.decompress(chunk)
                except Exception:
                    return chunk
    return chunk


def decode_html_chunk(chunk: bytes, content_type: str) -> str:
    """Decodes HTML chunk detecting charset from header or meta tags."""
    charset = None
    m_charset = re.search(r'charset=([\w-]+)', content_type, re.IGNORECASE)
    if m_charset:
        charset = m_charset.group(1).strip()

    if not charset:
        head_preview = chunk[:4096].decode("ascii", errors="ignore")
        m_meta = re.search(r'<meta[^>]+charset=["\']?([\w-]+)', head_preview, re.IGNORECASE)
        if m_meta:
            charset = m_meta.group(1).strip()
        else:
            m_http = re.search(r'<meta[^>]+content=["\'][^"\']*charset=([\w-]+)', head_preview, re.IGNORECASE)
            if m_http:
                charset = m_http.group(1).strip()

    if charset:
        try:
            return chunk.decode(charset, errors="replace")
        except Exception:
            pass

    return chunk.decode("utf-8", errors="replace")


def extract_opengraph_title(html_text: str) -> Optional[str]:
    """Extracts OpenGraph og:title or Twitter title if available."""
    patterns = [
        r'<meta\s+[^>]*?(?:property|name)=["\'](?:og:title|twitter:title)["\'][^>]*?content=["\']([^"\']+)["\']',
        r'<meta\s+[^>]*?content=["\']([^"\']+)["\'][^>]*?(?:property|name)=["\'](?:og:title|twitter:title)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html_text, re.IGNORECASE)
        if m:
            val = html.unescape(m.group(1)).strip()
            if val:
                return re.sub(r'\s+', ' ', val)
    return None


class TitleMetaHTMLParser(html.parser.HTMLParser):
    """Zero-dependency HTML parser for extracting title and og/twitter meta tags."""
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.title_parts: list[str] = []
        self.title: Optional[str] = None
        self.og_title: Optional[str] = None
        self.twitter_title: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t == "title":
            self.in_title = True
        elif t == "meta":
            attr_dict = {k.lower(): (v or "") for k, v in attrs}
            prop = attr_dict.get("property", "").lower()
            name = attr_dict.get("name", "").lower()
            content = attr_dict.get("content", "").strip()
            if content:
                if prop == "og:title" or name == "og:title":
                    self.og_title = content
                elif name == "twitter:title" or prop == "twitter:title":
                    self.twitter_title = content

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False
            if self.title_parts and not self.title:
                self.title = "".join(self.title_parts).strip()

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)


def get_url_title(url: str, timeout: float = 4.0) -> Optional[str]:
    """Fetches and parses HTML title tag or media preview with resilient multi-tier SSL/curl fallback."""
    if url.startswith("www."):
        url = "http://" + url

    safe, resolved_ip = is_safe_url(url)
    if not safe or not resolved_ip:
        logger.warning(f"[URL PREVIEW] Dropped unsafe/restricted URL: {url}")
        return None

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 MinibotURL/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/*,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
    }

    req = urllib.request.Request(url, headers=headers)
    chunk: Optional[bytes] = None
    content_type = ""
    content_encoding = ""
    content_length_raw = None

    # Attempt 1: Standard verified fetch with build_safe_opener()
    try:
        opener = build_safe_opener()
        with opener.open(req, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            content_encoding = response.headers.get("Content-Encoding", "")
            content_length_raw = response.headers.get("Content-Length")

            is_html = "text/html" in content_type or "xml" in content_type or not content_type
            if not is_html and content_type:
                clean_type = content_type.split(";")[0].strip()
                size_str = ""
                if content_length_raw:
                    try:
                        size_str = f" ({format_size(int(content_length_raw))})"
                    except Exception:
                        pass
                return f"[File] {clean_type}{size_str}"

            chunk = response.read(65536)
    except (ssl.SSLError, ssl.CertificateError, urllib.error.URLError) as e:
        err_str = str(e).lower()
        is_ssl_err = isinstance(e, ssl.SSLError) or "cert" in err_str or "ssl" in err_str or "handshake" in err_str
        if not is_ssl_err:
            logger.debug(f"[URL PREVIEW] Attempt 1 non-SSL failure: {e}")
            return None
        logger.info(f"[URL PREVIEW] SSL error ({e}), retrying with unverified TLS context...")
    except Exception as e:
        logger.debug(f"[URL PREVIEW] Attempt 1 failed: {e}")
        return None

    # Attempt 2: Unverified TLS fallback with relaxed ciphers
    if chunk is None:
        try:
            opener2 = build_safe_opener(verify_ssl=False)
            with opener2.open(req, timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                content_encoding = response.headers.get("Content-Encoding", "")
                content_length_raw = response.headers.get("Content-Length")

                is_html = "text/html" in content_type or "xml" in content_type or not content_type
                if not is_html and content_type:
                    clean_type = content_type.split(";")[0].strip()
                    size_str = ""
                    if content_length_raw:
                        try:
                            size_str = f" ({format_size(int(content_length_raw))})"
                        except Exception:
                            pass
                    return f"[File] {clean_type}{size_str}"

                chunk = response.read(65536)
        except Exception as e:
            logger.debug(f"[URL PREVIEW] Attempt 2 failed: {e}")

    # Attempt 3: curl subprocess fallback
    if chunk is None and shutil.which("curl"):
        try:
            cmd = [
                "curl", "-sSLk", "-i",
                "--max-time", str(int(max(1.0, timeout))),
                "--max-filesize", "262144",
                "-A", headers["User-Agent"],
                url
            ]
            res = subprocess.run(cmd, capture_output=True, timeout=timeout + 2.0)
            if res.returncode == 0 and res.stdout:
                raw = res.stdout
                parts = re.split(b'\r?\n\r?\n', raw)
                chunk = parts[-1] if len(parts) > 1 else raw
                last_headers = parts[-2].decode("latin-1", errors="ignore") if len(parts) > 1 else ""
                for line in last_headers.splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        if k.strip().lower() == "content-type":
                            content_type = v.strip().lower()
                        elif k.strip().lower() == "content-encoding":
                            content_encoding = v.strip().lower()
        except Exception as e:
            logger.debug(f"[URL PREVIEW] Attempt 3 (curl) failed: {e}")

    if chunk is None:
        return None

    decompressed = decompress_payload(chunk, content_encoding)
    decoded_text = decode_html_chunk(decompressed, content_type)

    title = None
    if HAS_BS4:
        try:
            soup = BeautifulSoup(decoded_text, "html.parser")
            if soup.title and soup.title.string:
                title = soup.title.string.strip()
            if not title or title.lower() in GENERIC_TITLES:
                meta_og = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
                if meta_og and meta_og.get("content"):
                    title = meta_og["content"].strip()
        except Exception:
            pass

    if not title or title.lower() in GENERIC_TITLES:
        try:
            parser = TitleMetaHTMLParser()
            parser.feed(decoded_text)
            if parser.title and parser.title.lower() not in GENERIC_TITLES:
                title = parser.title
            elif parser.og_title and parser.og_title.lower() not in GENERIC_TITLES:
                title = parser.og_title
            elif parser.twitter_title and parser.twitter_title.lower() not in GENERIC_TITLES:
                title = parser.twitter_title
        except Exception:
            pass

    if not title:
        match = re.search(r'<title[^>]*>(.*?)</title>', decoded_text, re.IGNORECASE | re.DOTALL)
        if match:
            raw = html.unescape(match.group(1).strip())
            if raw and raw.lower() not in GENERIC_TITLES:
                title = raw

    if not title or title.lower() in GENERIC_TITLES:
        og_title = extract_opengraph_title(decoded_text)
        if og_title:
            title = og_title

    if title:
        title = html.unescape(title)
        title = re.sub(r'\s+', ' ', title).strip()
        return title[:180]

    return None


class URLPlugin:
    """Minibot plugin that automatically previews URLs posted in channels."""
    def __init__(self, minibot: Any):
        self.minibot = minibot

    def on_privmsg(self, target: str, sender_nick: str, message: str) -> None:
        if not target.startswith("#"):
            return

        if get_ignore_manager().is_ignored(sender_nick):
            return

        clean_text = message.strip()
        if clean_text.startswith("[url]") or clean_text.startswith("[URL]") or clean_text.startswith("🔗") or clean_text.startswith("▶"):
            return

        urls = URL_REGEX.findall(message)
        if not urls:
            return

        seen_urls = set()
        previews_sent = 0

        for raw_url in urls:
            if previews_sent >= 2:
                break
            target_url = strip_url_punctuation(raw_url)
            if target_url in seen_urls:
                continue
            seen_urls.add(target_url)

            parsed = urlparse(target_url if "://" in target_url else f"http://{target_url}")
            netloc = (parsed.netloc or "").lower().split(":")[0]
            if any(yt in netloc for yt in ("youtube.com", "youtu.be")):
                continue

            # Only skip bare search engine / video homepages
            if netloc in BARE_SEARCH_DOMAINS and (not parsed.path or parsed.path == "/"):
                continue

            title = get_url_title(target_url)
            if title and title.lower() not in ("youtube", "google", "before you continue to youtube"):
                self.minibot.send_msg(target, f"[URL] {title}")
                previews_sent += 1
