# SPDX-License-Identifier: AGPL-3.0-or-later
"""
ircbot_shim.py — Zero-Dependency SingleServerIRCBot (jaraco/irc) Compatibility Shim.
"""
import logging
import sys
import types
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("IRCBotShim")

class Event:
    def __init__(self, event_type: str, source: str, target: str, arguments: Optional[List[str]] = None):
        self.type = event_type
        self.source = source
        self.target = target
        self.arguments = arguments or []

class ServerConnection:
    def __init__(self, bot_instance: Any):
        self._bot = bot_instance
    def privmsg(self, target: str, text: str) -> None:
        if hasattr(self._bot, "send_msg"):
            self._bot.send_msg(target, str(text))
    def notice(self, target: str, text: str) -> None:
        self.privmsg(target, f"NOTICE: {text}")

class SingleServerIRCBot:
    def __init__(self, server_list: Any = None, nickname: str = "minibot", realname: str = "Minibot", **kwargs):
        self._nickname = nickname
        self._realname = realname
        self.connection = ServerConnection(self)
    def on_pubmsg(self, connection: ServerConnection, event: Event) -> None: pass
    def on_privmsg(self, connection: ServerConnection, event: Event) -> None: pass

def inject_ircbot_shim() -> None:
    if "irc" not in sys.modules:
        i = types.ModuleType("irc")
        b = types.ModuleType("irc.bot")
        c = types.ModuleType("irc.client")
        b.SingleServerIRCBot = SingleServerIRCBot
        c.Event = Event
        c.ServerConnection = ServerConnection
        i.bot = b
        i.client = c
        sys.modules["irc"] = i
        sys.modules["irc.bot"] = b
        sys.modules["irc.client"] = c
