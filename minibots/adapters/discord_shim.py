# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
discord_shim.py — Zero-Dependency Discord.py Command Compatibility Shim for Minibots.
"""
import inspect
import logging
import sys
import types
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("DiscordShim")

class Context:
    def __init__(self, bot: Any, target: str, author_nick: str, message_text: str):
        self.bot = bot
        self.channel = target
        self.author = author_nick
        self.message = message_text
        self.prefix = "!"
    async def send(self, content: str = "", **kwargs) -> None:
        if hasattr(self.bot, "send_msg"):
            self.bot.send_msg(self.channel, str(content))
    async def reply(self, content: str = "", **kwargs) -> None:
        prefix = f"{self.author}: " if self.channel.startswith("#") else ""
        await self.send(f"{prefix}{content}")

class Commands:
    def __init__(self, bot_instance: Optional[Any] = None):
        self._bot = bot_instance
    def command(self, name: Optional[str] = None, aliases: Optional[List[str]] = None, help: Optional[str] = None, **kwargs) -> Callable:
        def decorator(func: Callable) -> Callable:
            cmd_name = (name or func.__name__).lower().strip()
            all_names = [cmd_name] + [a.lower().strip() for a in (aliases or [])]
            if not hasattr(func, "_discord_commands"):
                func._discord_commands = []
            func._discord_commands.extend(all_names)
            func._discord_help = help or inspect.getdoc(func) or ""
            return func
        return decorator
    def group(self, name: Optional[str] = None, **kwargs) -> Callable:
        return self.command(name=name, **kwargs)

bot = Commands()

def inject_discord_shim() -> None:
    if "discord" not in sys.modules:
        d = types.ModuleType("discord")
        ext = types.ModuleType("discord.ext")
        cmd = types.ModuleType("discord.ext.commands")
        cmd.Bot = Commands
        cmd.command = bot.command
        cmd.group = bot.group
        cmd.Context = Context
        ext.commands = cmd
        d.ext = ext
        sys.modules["discord"] = d
        sys.modules["discord.ext"] = ext
        sys.modules["discord.ext.commands"] = cmd
