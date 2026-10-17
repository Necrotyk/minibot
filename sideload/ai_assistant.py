# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
ai_assistant.py — AI Assistance, Summarization & Explanation Module for Dystopi Minibots.
Routes generation queries to the local or remote LLMRouter.
"""

import logging
from typing import Any, Optional

from sopel import module

logger = logging.getLogger("AIAssistant")

_router = None

def _get_router(bot: Any):
    global _router
    if _router is None:
        try:
            from core.config import ConfigManager
            from core.llm_router import LLMRouter
            cfg = ConfigManager().llm
            _router = LLMRouter(cfg)
        except Exception as e:
            logger.error(f"Failed to initialize LLMRouter in ai_assistant: {e}")
            _router = False
    return _router if _router is not False else None


@module.commands("ai", "ask")
def ai_query(bot, trigger):
    """Sends a query to the AI assistant for a concise IRC response."""
    prompt = trigger.group(2)
    if not prompt:
        bot.reply("Usage: !ai <question or prompt>")
        return

    router = _get_router(bot)
    if not router:
        bot.reply("❌ [AI] LLM router not available.")
        return

    sys_prompt = "You are an intelligent, concise IRC assistant. Provide clear, direct answers in 1 to 2 sentences without markdown fluff or preamble."
    try:
        ans = router.generate(
            prompt=prompt,
            system_prompt=sys_prompt,
            source_bot=getattr(bot, "nick", "ai_bot"),
            channel=getattr(trigger, "channel", ""),
            user_nick=getattr(trigger, "nick", ""),
            max_tokens=150
        )
        if ans:
            # Flatten newlines for IRC
            single_line = " ".join(ans.split())
            bot.say(f"🤖 [AI] {single_line[:380]}")
        else:
            bot.reply("❌ [AI] No response generated.")
    except Exception as e:
        bot.reply(f"❌ [AI] Request error: {e}")


@module.commands("summarize")
def ai_summarize(bot, trigger):
    """Summarizes text or input concisely."""
    text = trigger.group(2)
    if not text:
        bot.reply("Usage: !summarize <text to summarize>")
        return

    router = _get_router(bot)
    if not router:
        bot.reply("❌ [AI] LLM router not available.")
        return

    sys_prompt = "Summarize the given text in 1-2 punchy sentences for an IRC chat channel."
    try:
        ans = router.generate(
            prompt=f"Summarize this:\n\n{text}",
            system_prompt=sys_prompt,
            max_tokens=150
        )
        if ans:
            single_line = " ".join(ans.split())
            bot.say(f"📝 [Summary] {single_line[:380]}")
        else:
            bot.reply("❌ [AI] Failed to summarize.")
    except Exception as e:
        bot.reply(f"❌ [AI] Request error: {e}")


@module.commands("explain")
def ai_explain(bot, trigger):
    """Explains a code snippet or error message."""
    snippet = trigger.group(2)
    if not snippet:
        bot.reply("Usage: !explain <code or error message>")
        return

    router = _get_router(bot)
    if not router:
        bot.reply("❌ [AI] LLM router not available.")
        return

    sys_prompt = "Explain what this code or error does in 1-2 concise sentences for an IRC chat."
    try:
        ans = router.generate(
            prompt=f"Explain this briefly:\n\n{snippet}",
            system_prompt=sys_prompt,
            max_tokens=150
        )
        if ans:
            single_line = " ".join(ans.split())
            bot.say(f"💡 [Explain] {single_line[:380]}")
        else:
            bot.reply("❌ [AI] Failed to explain.")
    except Exception as e:
        bot.reply(f"❌ [AI] Request error: {e}")
