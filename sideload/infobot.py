# SPDX-License-Identifier: AGPL-3.0-or-later
"""
infobot.py — Sideloaded Info, Preview & Search Bot for Dystopi Minibots.
Capabilities:
  • Real-time URL title preview with strict SSRF defense.
  • YouTube preview for desktop, mobile (m.youtube.com), youtu.be, and Shorts
    with title, uploader/channel, duration, view count, and description summary.
  • Wikipedia article search & summary (!wiki / !wikipedia).
  • Grokipedia / fast encyclopedic search (!grok / !grokipedia).
  • Multi-language translation (!tr / !translate).
  • Help menu (!help or <bot>: help).
"""

import html
import json
import logging
import re
import socket
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

try:
    from sopel import module
except ImportError:
    try:
        from adapters.sopel_shim import inject_sopel_shim
    except ImportError:
        from minibots.adapters.sopel_shim import inject_sopel_shim
    inject_sopel_shim()
    from sopel import module

logger = logging.getLogger("InfoBot")

# SSRF Protection
try:
    from core.guard import build_safe_opener, is_safe_url
except ImportError:
    def is_safe_url(url: str) -> Tuple[bool, Optional[str]]:
        try:
            parsed = urllib.parse.urlparse(url)
            host = parsed.hostname
            if not host or host in ('localhost', '127.0.0.1', '::1'):
                return False, None
            ip = socket.gethostbyname(host)
            parts = [int(p) for p in ip.split('.')]
            if parts[0] in (10, 127) or (parts[0] == 172 and 16 <= parts[1] <= 31) or (parts[0] == 192 and parts[1] == 168):
                return False, None
            return True, ip
        except Exception:
            return False, None

    def build_safe_opener():
        return urllib.request.build_opener()

# URL Regexes
YT_REGEX = re.compile(
    r'(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/(?:watch\?.*?v=|embed/|v/|shorts/)|youtu\.be/)([a-zA-Z0-9_-]{11})',
    re.IGNORECASE
)
GENERIC_URL_REGEX = re.compile(r'(https?://[^\s<>"]+|www\.[^\s<>"]+)', re.IGNORECASE)


def format_duration(seconds: int) -> str:
    """Formats integer seconds into MM:SS or HH:MM:SS."""
    if seconds <= 0:
        return "0:00"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_views(views: int) -> str:
    """Formats integer view count into human-readable metric (e.g. 1.2M)."""
    if views >= 1_000_000_000:
        return f"{views / 1_000_000_000:.2f}B"
    elif views >= 1_000_000:
        return f"{views / 1_000_000:.2f}M"
    elif views >= 1_000:
        return f"{views / 1_000:.1f}K"
    return f"{views:,}"


def get_youtube_details(video_id: str, is_shorts: bool = False, timeout: float = 4.0) -> Optional[str]:
    """Fetches comprehensive YouTube video details: title, uploader, length, views, and description."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    title = None
    author = None
    length_sec = 0
    views = 0
    desc_snippet = ""

    # Method 1: Fetch HTML page to extract rich player response & meta tags
    try:
        req = urllib.request.Request(url, headers=headers)
        opener = build_safe_opener()
        with opener.open(req, timeout=timeout) as resp:
            content = resp.read(1048576).decode("utf-8", errors="ignore")

            # 1. Parse player response JSON
            m_json = re.search(r'ytInitialPlayerResponse\s*=\s*({.*?});', content)
            if not m_json:
                m_json = re.search(r'var ytInitialPlayerResponse\s*=\s*({.*?});(?:var|</script>)', content)

            if m_json:
                try:
                    pdata = json.loads(m_json.group(1))
                    details = pdata.get("videoDetails", {})
                    title = details.get("title")
                    author = details.get("author")
                    length_sec = int(details.get("lengthSeconds", 0) or 0)
                    views = int(details.get("viewCount", 0) or 0)
                    desc_snippet = details.get("shortDescription", "")
                except Exception:
                    pass

            # 2. Meta tags fallback
            if not title:
                m_title = re.search(r'<meta property="og:title" content="(.*?)"', content)
                if m_title:
                    title = html.unescape(m_title.group(1).strip())
            if not desc_snippet:
                m_desc = re.search(r'<meta name="description" content="(.*?)"', content)
                if not m_desc:
                    m_desc = re.search(r'<meta property="og:description" content="(.*?)"', content)
                if m_desc:
                    desc_snippet = html.unescape(m_desc.group(1).strip())
    except Exception as e:
        logger.debug(f"[YouTube] Page scrape failed for {video_id}: {e}")

    # Method 2: oEmbed fallback if title not found
    if not title:
        try:
            oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
            req = urllib.request.Request(oembed_url, headers={"User-Agent": "Minibot/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as o_resp:
                odata = json.loads(o_resp.read().decode("utf-8"))
                title = odata.get("title")
                author = odata.get("author_name")
        except Exception:
            pass

    if not title:
        return None

    # Clean title
    title = re.sub(r'\s+', ' ', title).strip()
    title = re.sub(r'\s*-\s*YouTube$', '', title, flags=re.IGNORECASE).strip()

    # Filter out generic YouTube boilerplate descriptions
    if desc_snippet:
        desc_snippet = re.sub(r'\s+', ' ', desc_snippet).strip()
        desc_lower = desc_snippet.lower()
        boilerplate_indicators = [
            "enjoy the videos and music you love",
            "upload original content",
            "share it all with friends, family",
            "about press copyright contact us",
            "sign in to confirm your age",
            "sign in to confirm you’re not a bot",
            "before you continue to youtube",
            "an error occurred. please try again later",
        ]
        if any(ind in desc_lower for ind in boilerplate_indicators):
            desc_snippet = ""
        elif len(desc_snippet) > 120:
            desc_snippet = desc_snippet[:117] + "..."

    badge = "\x02\x0300,04 ▶ Shorts \x0f" if is_shorts else "\x02\x0300,04 ▶ YouTube \x0f"
    res = f"{badge} \x02{title}\x0f"

    if author:
        res += f" \x0314by\x0f \x0310\x02{author}\x0f"

    meta_parts = []
    if length_sec > 0:
        meta_parts.append(f"\x0311⏱ {format_duration(length_sec)}\x0f")
    if views > 0:
        meta_parts.append(f"\x0313👁 {format_views(views)} views\x0f")

    if meta_parts:
        res += " \x0314•\x0f " + " \x0314•\x0f ".join(meta_parts)

    if desc_snippet:
        res += f" \x0314•\x0f \x0315\"{desc_snippet}\"\x0f"

    return res[:380]


def get_web_title(url: str, timeout: float = 4.0) -> Optional[str]:
    """Fetches and parses HTML title tag with SSRF defense."""
    if url.startswith("www."):
        url = "http://" + url

    safe, resolved_ip = is_safe_url(url)
    if not safe or not resolved_ip:
        return None

    parsed = urllib.parse.urlparse(url)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 MinibotsURL/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Host": parsed.hostname or ""
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        opener = build_safe_opener()
        with opener.open(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "").lower()
            if "text/html" not in ctype and "xml" not in ctype:
                return None

            chunk = resp.read(65536).decode("utf-8", errors="ignore")
            match = re.search(r'<title[^>]*>(.*?)</title>', chunk, re.IGNORECASE | re.DOTALL)
            if match:
                raw_title = html.unescape(match.group(1).strip())
                clean_title = re.sub(r'\s+', ' ', raw_title)
                return clean_title[:180]
    except Exception:
        pass
    return None


def search_wikipedia(query: str, timeout: float = 4.0) -> str:
    """Searches Wikipedia and extracts a clean factual summary."""
    clean_q = query.strip()
    if not clean_q:
        return "Usage: !wiki <topic>"

    headers = {"User-Agent": "Minibots/1.0 (https://dystopi.cc; bot@dystopi.cc)"}
    try:
        # Step 1: OpenSearch for best matching article title
        search_url = f"https://en.wikipedia.org/w/api.php?action=opensearch&search={urllib.parse.quote(clean_q)}&limit=1&namespace=0&format=json"
        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            sdata = json.loads(resp.read().decode("utf-8"))
            if not sdata[1]:
                return f"[Wikipedia] No article found for '{clean_q}'."
            title = sdata[1][0]
            link = sdata[3][0]

        # Step 2: REST summary extract
        sum_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}"
        req_sum = urllib.request.Request(sum_url, headers=headers)
        with urllib.request.urlopen(req_sum, timeout=timeout) as resp:
            sum_data = json.loads(resp.read().decode("utf-8"))
            extract = sum_data.get("extract", "")
            desc = sum_data.get("description", "")

            clean_extract = re.sub(r'\s+', ' ', extract).strip()
            if len(clean_extract) > 220:
                clean_extract = clean_extract[:217] + "..."

            header = title
            if desc:
                header += f" ({desc})"

            return f"📖 \x02\x0312[Wikipedia]\x0f \x02{header}\x0f \x0314—\x0f {clean_extract} \x0314|\x0f {link}"
    except Exception as e:
        return f"[Wikipedia Error] Could not retrieve summary: {e}"


def search_grokipedia(query: str, timeout: float = 4.0) -> str:
    """Searches Grokipedia knowledge base (grokipedia.com) and extracts a factual summary."""
    clean_q = query.strip()
    if not clean_q:
        return "Usage: !grok <topic>"

    headers = {"User-Agent": "Mozilla/5.0 Minibots/1.0 (https://dystopi.cc)"}
    try:
        url = f"https://grokipedia.com/search?q={urllib.parse.quote(clean_q)}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html_text = resp.read().decode("utf-8", errors="ignore")

        # Parse search results: extract items pointing to /page/<slug>
        results = []
        for part in html_text.split("<a "):
            if not part.startswith('href="/page/') and 'href="/page/' not in part[:40]:
                continue
            tag_end = part.find(">")
            if tag_end == -1:
                continue
            tag = part[:tag_end]
            a_end = part.find("</a>")
            inner = part[tag_end + 1:a_end] if a_end != -1 else ""

            slug_m = re.search(r'data-slug="([^"]+)"', tag)
            if not slug_m:
                slug_m = re.search(r'href="/page/([^"\s>]+)"', tag)
            slug = slug_m.group(1) if slug_m else ""

            snip_m = re.search(r'data-search-snippet="([^"]*)"', tag)
            snippet = html.unescape(snip_m.group(1)).strip() if snip_m else ""
            if not snippet:
                p_m = re.search(r"<p[^>]*>(.*?)</p>", inner, re.DOTALL)
                if p_m:
                    snippet = re.sub(r"<[^>]+>", "", html.unescape(p_m.group(1))).strip()

            span_m = re.search(r"<span[^>]*truncate[^>]*>(.*?)</span>", inner, re.DOTALL)
            title = re.sub(r"<[^>]+>", "", html.unescape(span_m.group(1))).strip() if span_m else slug.replace("_", " ")

            if slug:
                results.append((title, slug, snippet))

        if not results:
            return f"[Grokipedia] No article found for '{clean_q}'."

        # Prefer exact match if one exists in the search results
        chosen = results[0]
        q_norm = clean_q.lower().strip()
        for r in results:
            if r[0].lower() == q_norm or r[1].lower() == q_norm.replace(" ", "_"):
                chosen = r
                break

        title, slug, snippet = chosen
        clean_snippet = re.sub(r"\[\d*\]", "", snippet)
        clean_snippet = re.sub(r"\[\]\([^\)]*\)", "", clean_snippet)
        clean_snippet = re.sub(r"\s+", " ", clean_snippet).strip()
        if len(clean_snippet) > 230:
            clean_snippet = clean_snippet[:227] + "..."

        page_url = f"https://grokipedia.com/page/{slug}"
        if clean_snippet:
            return f"🧠 \x02\x0306[Grokipedia]\x0f \x02{title}\x0f \x0314—\x0f {clean_snippet} \x0314|\x0f {page_url}"
        else:
            return f"🧠 \x02\x0306[Grokipedia]\x0f \x02{title}\x0f \x0314|\x0f {page_url}"
    except Exception as e:
        return f"[Grokipedia Error] Could not retrieve summary: {e}"


def translate_text(target_lang: str, text: str, timeout: float = 4.0) -> str:
    """Translates text to target language with multi-tier fallback."""
    clean_text = text.strip()
    if not clean_text:
        return "Usage: !tr [lang] <text> (e.g. !tr es Hello world)"

    lang = target_lang.lower().strip() or "en"

    # Tier 1: Google Translate
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=dict-chrome-ex&sl=auto&tl={urllib.parse.quote(lang)}&dt=t&q={urllib.parse.quote(clean_text)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            translated = "".join([part[0] for part in res[0] if part[0]])
            detected_src = res[2] if len(res) > 2 else "auto"
            if translated:
                return f"🌐 [\x02\x0303Translate\x0f \x0314{detected_src.upper()}->{lang.upper()}\x0f] \x02{translated}\x0f"
    except Exception as e:
        logger.debug(f"Google translate failed: {e}")

    # Tier 2: MyMemory Translated API fallback
    try:
        mm_url = f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(clean_text)}&langpair=auto|{urllib.parse.quote(lang)}"
        req = urllib.request.Request(mm_url, headers={"User-Agent": "Mozilla/5.0 Minibots/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            translated = data.get("responseData", {}).get("translatedText", "")
            if translated:
                return f"🌐 [\x02\x0303Translate\x0f \x0314->{lang.upper()}\x0f] \x02{html.unescape(translated)}\x0f"
    except Exception as e:
        logger.debug(f"MyMemory translate failed: {e}")

    return "[Translate Error] Translation service temporarily unavailable."


@module.commands("wiki", "wikipedia")
def cmd_wiki(bot: Any, trigger: Any) -> None:
    """Searches Wikipedia and extracts a clean factual summary."""
    query = (trigger.group(2) or "").strip()
    bot.say(search_wikipedia(query))


@module.commands("grok", "grokipedia", "grokpedia")
def cmd_grok(bot: Any, trigger: Any) -> None:
    """Searches Grokipedia knowledge base (grokipedia.com) and extracts a factual summary."""
    query = (trigger.group(2) or "").strip()
    bot.say(search_grokipedia(query))


@module.commands("tr", "translate")
def cmd_translate(bot: Any, trigger: Any) -> None:
    """Translates text to target language."""
    args_str = (trigger.group(2) or "").strip()
    if not args_str:
        bot.say("Usage: !tr [lang_code] <text> (e.g. !tr es Hello world, !tr de Good morning)")
        return
    parts = args_str.split()
    if len(parts[0]) in (2, 3, 5) and parts[0].isalpha() and len(parts) > 1:
        lang = parts[0]
        body = " ".join(parts[1:])
    else:
        lang = "en"
        body = args_str
    bot.say(translate_text(lang, body))


class Plugin:
    """Universal Minibot plugin for URL/YouTube previews, Wikipedia/Grokipedia, and translation."""

    def __init__(self, bot: Any):
        self.bot = bot

    def on_privmsg(self, target: str, sender_nick: str, message: str, hostmask: str = "") -> None:
        text = message.strip()
        if not text:
            return

        bot_nick = getattr(self.bot, "nick", "").lower()
        is_dm = not target.startswith("#") or (target.lower() == bot_nick)
        reply_target = sender_nick if is_dm else target

        # 1. Parse Command Prefix / Addressing
        cmd_text = ""
        if text.startswith("!") or text.startswith("."):
            cmd_text = text[1:].strip()
        elif text.lower().startswith(f"{bot_nick}:") or text.lower().startswith(f"{bot_nick},"):
            cmd_text = text[len(bot_nick) + 1:].strip()
        elif is_dm:
            cmd_text = text

        if cmd_text:
            parts = cmd_text.split()
            cmd = parts[0].lower()
            args = parts[1:]

            if cmd == "help":
                if hasattr(self.bot, "get_help_lines"):
                    topic = args[0] if args else None
                    for line in self.bot.get_help_lines(topic=topic)[:4]:
                        self.bot.send_msg(reply_target, line)
                else:
                    self.bot.send_msg(reply_target, f"⚙️ [\x02\x0310{getattr(self.bot, 'nick', 'Bot')} Commands\x0f] \x0311!wiki\x0f <query> | \x0311!grok\x0f <query> | \x0311!tr\x0f [lang] <text> | \x0311!help\x0f")
                    self.bot.send_msg(reply_target, "🔗 \x02\x0311Features:\x0f Auto-previews normal URLs and YouTube links (mobile, youtu.be, shorts) with views & description.")
                return
            elif cmd in ("wiki", "wikipedia"):
                query = " ".join(args)
                self.bot.send_msg(reply_target, search_wikipedia(query))
                return
            elif cmd in ("grok", "grokipedia", "grokpedia"):
                query = " ".join(args)
                self.bot.send_msg(reply_target, search_grokipedia(query))
                return
            elif cmd in ("tr", "translate"):
                if not args:
                    self.bot.send_msg(reply_target, "Usage: !tr [lang_code] <text> (e.g. !tr es Hello world, !tr de Good morning)")
                    return
                # Check if first arg is a language code (2-5 letters)
                if len(args[0]) in (2, 3, 5) and args[0].isalpha() and len(args) > 1:
                    lang = args[0]
                    body = " ".join(args[1:])
                else:
                    lang = "en"
                    body = " ".join(args)
                self.bot.send_msg(reply_target, translate_text(lang, body))
                return

        # 2. Passive Channel URL & YouTube Previewer
        if not target.startswith("#"):
            return

        # Suppress previews for messages from known or ignored bots to avoid bot-to-bot echo loops
        try:
            from core.ignore_manager import get_ignore_manager
            if get_ignore_manager().is_ignored(sender_nick):
                return
        except ImportError:
            sender_clean = sender_nick.lower().strip().lstrip("@+~&%")
            KNOWN_BOTS = {"vulnavia", "sopel", "chanserv", "nickserv", "minibots", "toolz", "julesbot", "lertz", "lertz_dystopi", "lertz_admin", "gr_dystopi"}
            if sender_clean in KNOWN_BOTS or sender_clean.endswith("bot") or sender_clean.startswith("lertz"):
                return

        # Suppress previews for messages that are already bot link previews (e.g. [url] ..., 🔗 ..., ▶ ...)
        clean_text = text.strip()
        if clean_text.startswith("[url]") or clean_text.startswith("[URL]") or clean_text.startswith("🔗") or clean_text.startswith("▶"):
            return

        # Check for YouTube links first
        yt_match = YT_REGEX.search(text)
        if yt_match:
            vid = yt_match.group(1)
            is_shorts = "shorts/" in text.lower()
            yt_info = get_youtube_details(vid, is_shorts=is_shorts)
            if yt_info:
                self.bot.send_msg(target, yt_info)
                return

        # Fallback to general URL preview
        url_match = GENERIC_URL_REGEX.search(text)
        if url_match:
            raw_url = url_match.group(1)
            # Skip bare root domains (e.g. www.youtube.com, youtube.com, google.com)
            parsed = urllib.parse.urlparse(raw_url if "://" in raw_url else f"http://{raw_url}")
            if not parsed.path or parsed.path == "/":
                return

            title = get_web_title(raw_url)
            if title and title.lower() not in ("youtube", "google", "before you continue to youtube"):
                self.bot.send_msg(target, f"🔗 \x02\x0311[Link]\x0f \x02{title}\x0f")


# Module-level integration hooks for ScriptAdapter & SideloadEngine
_instances = {}

def setup(bot: Any) -> None:
    """Initialize plugin instance for bot."""
    _instances[id(bot)] = Plugin(bot)

def handle_message(bot: Any, target: str, sender_nick: str, message: str) -> None:
    """Dispatches incoming PRIVMSG to the bot's Plugin instance."""
    inst = _instances.get(id(bot))
    if not inst:
        inst = Plugin(bot)
        _instances[id(bot)] = inst
    inst.on_privmsg(target, sender_nick, message)

