# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
web_search.py — Web & Developer Search Module for Dystopi Minibots.
Supports DuckDuckGo, Hacker News, GitHub, and Reddit queries.
"""

import html
import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Optional

from sopel import module

logger = logging.getLogger("WebSearch")

# SSRF Guard
try:
    from core.guard import build_safe_opener, is_safe_url
except ImportError:
    def is_safe_url(url: str):
        return True, "127.0.0.1"

    def build_safe_opener():
        return urllib.request.build_opener()


@module.commands("ddg", "duck", "search")
def duckduckgo_search(bot, trigger):
    """Searches DuckDuckGo for query results."""
    query = trigger.group(2)
    if not query:
        bot.reply("Usage: !ddg <query>")
        return

    url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MinibotsWebSearch/1.0"})
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            abstract = data.get("AbstractText")
            source_url = data.get("AbstractURL")

            if abstract:
                res = f"🦆 [DDG] {abstract}"
                if source_url:
                    res += f" — {source_url}"
                bot.say(res[:380])
                return

            # Check related topics
            related = data.get("RelatedTopics", [])
            for item in related:
                txt = item.get("Text")
                u = item.get("FirstURL")
                if txt and u:
                    bot.say(f"🦆 [DDG] {txt} — {u}"[:380])
                    return

            # Direct fallback link
            bot.reply(f"🦆 [DDG] No direct instant answer. Web link: https://duckduckgo.com/?q={urllib.parse.quote(query)}")
    except Exception as e:
        logger.debug(f"DDG query failed: {e}")
        bot.reply("❌ [DDG] Search query failed or timed out.")


@module.commands("hn", "hackernews")
def hacker_news_top(bot, trigger):
    """Fetches top Hacker News stories."""
    args = (trigger.group(2) or "").split()
    count = 3
    if args and args[0].isdigit():
        count = min(max(int(args[0]), 1), 5)

    try:
        top_url = "https://hacker-news.firebaseio.com/v0/topstories.json"
        req = urllib.request.Request(top_url, headers={"User-Agent": "MinibotsHN/1.0"})
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            ids = json.loads(resp.read().decode("utf-8"))[:count]

        for sid in ids:
            item_url = f"https://hacker-news.firebaseio.com/v0/item/{sid}.json"
            req_item = urllib.request.Request(item_url, headers={"User-Agent": "MinibotsHN/1.0"})
            with urllib.request.urlopen(req_item, timeout=3.0) as item_resp:
                item = json.loads(item_resp.read().decode("utf-8"))
                title = item.get("title", "Untitled")
                score = item.get("score", 0)
                comments = item.get("descendants", 0)
                link = item.get("url") or f"https://news.ycombinator.com/item?id={sid}"
                bot.say(f"🟠 [HN] {title} ({score} pts, {comments} comments) — {link}"[:380])
    except Exception as e:
        logger.debug(f"HN query failed: {e}")
        bot.reply("❌ [HN] Failed to fetch top stories.")


@module.commands("gh", "github")
def github_repo_lookup(bot, trigger):
    """Looks up GitHub repository metadata."""
    repo = trigger.group(2)
    if not repo or "/" not in repo:
        bot.reply("Usage: !gh <owner/repo> (e.g. !gh torvalds/linux)")
        return

    clean_repo = repo.strip().strip("/")
    url = f"https://api.github.com/repos/{clean_repo}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "MinibotsGitHub/1.0",
            "Accept": "application/vnd.github.v3+json"
        })
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            name = data.get("full_name")
            desc = data.get("description") or "No description provided."
            stars = data.get("stargazers_count", 0)
            forks = data.get("forks_count", 0)
            lang = data.get("language") or "Unknown"
            html_url = data.get("html_url")
            bot.say(f"🐙 [GitHub] {name} ({lang}) ★{stars:,} | ⑂{forks:,} — {desc} [{html_url}]"[:380])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            bot.reply(f"❌ [GitHub] Repository '{clean_repo}' not found.")
        elif e.code == 403:
            bot.reply("⚠️ [GitHub] API rate limit reached. Please try again later.")
        else:
            bot.reply(f"❌ [GitHub] HTTP error {e.code}.")
    except Exception as e:
        logger.debug(f"GitHub query error: {e}")
        bot.reply("❌ [GitHub] Lookup failed or timed out.")


@module.commands("r", "reddit")
def reddit_top_post(bot, trigger):
    """Fetches top post from a subreddit."""
    sub = trigger.group(2)
    if not sub:
        bot.reply("Usage: !r <subreddit> (e.g. !r netsec)")
        return

    clean_sub = sub.strip().lstrip("r/").strip()
    url = f"https://www.reddit.com/r/{urllib.parse.quote(clean_sub)}/hot.json?limit=1"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Minibots/1.0 (+https://github.com/Necrotyk/minibot)"
        })
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            posts = data.get("data", {}).get("children", [])
            if not posts:
                bot.reply(f"❌ [Reddit] No posts found in r/{clean_sub}.")
                return

            post = posts[0].get("data", {})
            title = html.unescape(post.get("title", ""))
            author = post.get("author", "anonymous")
            score = post.get("score", 0)
            permalink = f"https://reddit.com{post.get('permalink', '')}"
            bot.say(f"🤖 [r/{clean_sub}] \"{title}\" (▲{score} by u/{author}) — {permalink}"[:380])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            bot.reply(f"❌ [Reddit] Subreddit r/{clean_sub} not found or banned.")
        elif e.code == 403:
            bot.reply(f"🔒 [Reddit] Subreddit r/{clean_sub} is private.")
        else:
            bot.reply(f"❌ [Reddit] HTTP error {e.code}.")
    except Exception as e:
        logger.debug(f"Reddit query error: {e}")
        bot.reply("❌ [Reddit] Search failed or timed out.")
