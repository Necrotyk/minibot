# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
limnoria_shim.py — Zero-Dependency Limnoria / Supybot Plugin Shim.
"""

import sys
import types
from typing import Any, Callable, Dict, Optional


class LimnoriaIrcWrapper:
    """Mock Limnoria irc object mapping .reply(), .error() to minibot."""
    def __init__(self, minibot_instance: Any, target: str, sender: str = ''):
        self._minibot = minibot_instance
        self._target = target
        self._sender = sender

    def reply(self, msg: str, prefixNick: bool = True, to: Optional[str] = None) -> None:
        dest = to or self._target
        if prefixNick and bool(self._sender) and dest.startswith("#"):
            self._minibot.send_msg(dest, f"{self._sender}: {msg}")
        else:
            self._minibot.send_msg(dest, msg)

    def error(self, msg: str) -> None:
        self._minibot.send_msg(self._target, f"[Error] {msg}")


class LimnoriaMsgWrapper:
    """Mock Limnoria msg object encapsulating command arguments."""
    def __init__(self, target: str, sender: str, text: str, args: list):
        self.nick = sender
        self.channel = target if target.startswith("#") else None
        self.args = [target, text] + args


def inject_limnoria_shim() -> None:
    """Injects lightweight supybot modules into sys.modules."""
    if "supybot" not in sys.modules:
        supybot = types.ModuleType("supybot")
        callbacks = types.ModuleType("supybot.callbacks")

        class Plugin(object):
            pass

        class PluginReg(object):
            pass

        callbacks.Plugin = Plugin
        callbacks.PluginReg = PluginReg

        supybot.callbacks = callbacks
        sys.modules["supybot"] = supybot
        sys.modules["supybot.callbacks"] = callbacks


class callbacks:
    class Plugin(object):
        pass
    class PluginReg(object):
        pass
