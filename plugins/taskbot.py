# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
taskbot.py — Channel-Gated Coding & Technical Task Execution Plugin.
Routes task instructions to Kaggle GPU coding models (Qwen Coder / Gemma) with WireGuard Pastebin offload.
"""

import logging
import re
from typing import Any, List, Optional

from core.kaggle_bot_binder import KaggleBotBinder
from core.llm_router import LLMRouter

logger = logging.getLogger("TaskBot")

TASK_ALLOWED_CHANNELS = [
    "#sandbox",
    "#dev",
    "#troll",
    "#minibots",
    "#offtopic",
    "#research",
]


class TaskBotPlugin:
    """Plugin providing channel-gated autonomous technical and coding task execution."""

    def __init__(
        self,
        minibot: Any,
        llm_router: Optional[LLMRouter] = None,
        bot_binder: Optional[KaggleBotBinder] = None
    ):
        self.minibot = minibot
        self.config = getattr(minibot, "config", {})
        if hasattr(self.config, "data"):
            self.config = self.config.data
        self.llm_router = llm_router or LLMRouter(self.config)
        self.bot_binder = bot_binder or KaggleBotBinder(self.config)
        self.allowed_channels = self.bot_binder.uncensored_channels or TASK_ALLOWED_CHANNELS

    def is_channel_allowed(self, channel: str) -> bool:
        """Verifies if the channel is whitelisted for task execution."""
        if not channel:
            return False
        if not channel.startswith("#"):
            return True
        chan_lower = channel.lower().strip()
        if "+task" in chan_lower or "+dev" in chan_lower:
            return True
        return chan_lower in [c.lower() for c in self.allowed_channels]

    def _reply(self, network: str, target: str, message: str) -> None:
        """Sends a response via minibot or daemon messaging abstraction."""
        if hasattr(self.minibot, "send_privmsg"):
            self.minibot.send_privmsg(network, target, message)
        elif hasattr(self.minibot, "send_msg"):
            net = getattr(self.minibot, "network", network)
            try:
                self.minibot.send_msg(target, message)
            except TypeError:
                self.minibot.send_msg(net, target, message)
        elif hasattr(self.minibot, "reply"):
            self.minibot.reply(message)

    def execute_task(self, network: str, target: str, sender: str, prompt: str) -> None:
        """Executes a coding/technical prompt, offloading large code blocks to Pastebin."""
        clean_prompt = prompt.strip()
        if not clean_prompt:
            self._reply(network, target, f"Usage: !task <prompt> (or !do <prompt>)")
            return

        if not self.is_channel_allowed(target):
            chans = ", ".join(TASK_ALLOWED_CHANNELS[:6])
            self._reply(
                network,
                target,
                f"⚠️ [TaskBot] Task execution is restricted to dev & research channels ({chans})."
            )
            return

        self._reply(network, target, f"⚙️ [TaskBot] Processing task for {sender} (role: coding, timeout: 120s)...")

        system_prompt = (
            "You are an expert autonomous software engineer and system technician for Minibots. "
            "Produce clean, efficient, self-contained code or technical instructions. "
            "Do not add conversational fluff or unprompted preambles. Output pure code or clear answers."
        )

        try:
            reply = self.llm_router.generate(
                prompt=clean_prompt,
                system_prompt=system_prompt,
                source_bot="taskbot",
                channel=target,
                user_nick=sender,
                role="coding",
                max_tokens=1024,
                temperature=0.2,
                timeout=120
            )

            if not reply or reply.startswith("[Error"):
                err_msg = reply if reply else "No response generated."
                self._reply(network, target, f"❌ [TaskBot] {err_msg}")
                return

            lines = reply.splitlines()
            has_fences = "```" in reply
            has_code_markers = any(l.strip().startswith(("def ", "class ", "import ", "from ", "#!/")) for l in lines)

            if (has_fences or len(lines) > 4) and (has_fences or has_code_markers):
                paste_url = self.bot_binder.upload_to_pastebin(
                    code_text=reply,
                    title=f"Task for {sender}: {clean_prompt[:30]}",
                    filename="task_output.py",
                    network=network
                )
                if paste_url:
                    self._reply(network, target, f"✅ [TaskBot] {sender}; Task completed. Full code output: {paste_url}")
                    return

            safe_lines = self.bot_binder.defang_irc_output(reply, bot_nick="taskbot")
            for l in safe_lines:
                self._reply(network, target, f"[TaskBot] {l}")

        except Exception as e:
            logger.exception(f"[TaskBot] Execution failed: {e}")
            self._reply(network, target, f"❌ [TaskBot] Error: {e}")

    def on_privmsg(self, target: str, sender_nick: str, message: str) -> None:
        """Handles messages when attached as a MiniBot module."""
        text = message.strip()
        if not text:
            return

        bot_nick = getattr(self.minibot, "nick", "taskbot").lower()
        clean_bot = bot_nick.lstrip("@")
        net = getattr(self.minibot, "network", "wireguard")

        task_prompt = ""
        if text.startswith(("!task ", ".task ", "?task ")):
            task_prompt = text[6:].strip()
        elif text.startswith(("!do ", ".do ", "?do ")):
            task_prompt = text[4:].strip()
        else:
            nick_prefix = rf"^(?:@{clean_bot}|{clean_bot})[:,\s]+\s*(?:task|do)?\s*(.*)$"
            match = re.match(nick_prefix, text, re.IGNORECASE)
            if match:
                task_prompt = match.group(1).strip()

        if task_prompt:
            self.execute_task(net, target, sender_nick, task_prompt)
