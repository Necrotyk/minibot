# SPDX-License-Identifier: AGPL-3.0-or-later
"""
urls.py — Real-Time URL Previewer with SSRF IP-Pinning Defense.
"""

import logging
import re
import socket
import urllib.request
from typing import Any, Optional
from urllib.parse import urlparse

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

from core.guard import build_safe_opener, is_safe_url
from core.ignore_manager import get_ignore_manager

logger = logging.getLogger("URLPreviewer")

URL_REGEX = re.compile(r'(https?://[^\s<>"]+|www\.[^\s<>"]+)', re.IGNORECASE)


def get_url_title(url: str, timeout: float = 4.0) -> Optional[str]:
    """Fetches and parses HTML title tag with strict SSRF & redirect defense."""
    if url.startswith("www."):
        url = "http://" + url

    safe, resolved_ip = is_safe_url(url)
    if not safe or not resolved_ip:
        logger.warning(f"[URL PREVIEW] Dropped unsafe/restricted URL: {url}")
        return None

    parsed = urlparse(url)
    hostname = parsed.hostname

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 MinibotURL/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Host": hostname or ""
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        opener = build_safe_opener()
        with opener.open(req, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" not in content_type and "xml" not in content_type:
                return None

            # Read first 64KB only
            chunk = response.read(65536)
            title = None
            if HAS_BS4:
                try:
                    soup = BeautifulSoup(chunk, "html.parser")
                    if soup.title and soup.title.string:
                        title = soup.title.string.strip()
                except Exception:
                    pass

            if not title:
                match = re.search(r'<title[^>]*>(.*?)</title>', chunk.decode('utf-8', errors='ignore'), re.IGNORECASE | re.DOTALL)
                if match:
                    title = match.group(1).strip()

            if title:
                # Clean newlines and excess whitespace
                title = re.sub(r'\s+', ' ', title)
                return title[:180]
    except Exception as e:
        logger.debug(f"[URL PREVIEW] Failed to extract title from {url}: {e}")

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

        # Process at most 1 URL per message to avoid spam
        target_url = urls[0]
        parsed = urlparse(target_url if "://" in target_url else f"http://{target_url}")
        netloc = (parsed.netloc or "").lower()
        if any(yt in netloc for yt in ("youtube.com", "youtu.be")):
            return
        if not parsed.path or parsed.path == "/":
            return

        title = get_url_title(target_url)
        if title and title.lower() not in ("youtube", "google", "before you continue to youtube"):
            self.minibot.send_msg(target, f"[URL] {title}")
