# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
errbot_shim.py — Zero-Dependency Errbot Compatibility Shim for Minibots.
"""
import inspect
import logging
import sys
import types
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ErrbotShim")

def botcmd(*args, **kwargs) -> Callable:
    def decorator(func: Callable) -> Callable:
        name = kwargs.get("name", func.__name__).lower().strip()
        hidden = kwargs.get("hidden", False)
        admin_only = kwargs.get("admin_only", False)
        if not hasattr(func, "_errbot_commands"):
            func._errbot_commands = []
        func._errbot_commands.append({
            "name": name,
            "hidden": hidden,
            "admin_only": admin_only,
            "doc": inspect.getdoc(func) or ""
        })
        return func
    if len(args) == 1 and callable(args[0]):
        return decorator(args[0])
    return decorator

def arg_botcmd(*args, **kwargs) -> Callable:
    def decorator(func: Callable) -> Callable:
        if not hasattr(func, "_errbot_args"):
            func._errbot_args = []
        func._errbot_args.append((args, kwargs))
        return func
    return decorator

class BotPlugin:
    def __init__(self, bot=None):
        self._bot = bot
        self.log = logging.getLogger(self.__class__.__name__)
    def activate(self): pass
    def deactivate(self): pass
    def send(self, target: Any, text: str) -> None:
        if hasattr(self._bot, "send_msg"):
            self._bot.send_msg(str(target), str(text))

def inject_errbot_shim() -> None:
    if "errbot" not in sys.modules:
        m = types.ModuleType("errbot")
        m.BotPlugin = BotPlugin
        m.botcmd = botcmd
        m.arg_botcmd = arg_botcmd
        sys.modules["errbot"] = m
