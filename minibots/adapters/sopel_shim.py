# SPDX-License-Identifier: AGPL-3.0-or-later
"""
sopel_shim.py — Zero-Dependency In-Memory Sopel Plugin Compatibility Shim.
Injected into sys.modules['sopel'] and sys.modules['sopel.module'] to execute
community Sopel plugins without heavy pip dependencies.
"""

import re
import sys
import types
from typing import Any, Callable, Dict, List, Optional


class SopelBotWrapper:
    """Mock Sopel bot instance mapping .say(), .reply(), .action() to minibot."""
    def __init__(self, minibot_instance: Any, current_target: str, sender_nick: str):
        self._minibot = minibot_instance
        self._target = current_target
        self._sender = sender_nick
        self.nick = getattr(minibot_instance, "nick", "minibot")
        self.channels = getattr(minibot_instance, "channels", [])
        self.network = getattr(minibot_instance, "network", "")

    def say(self, message: str, recipient: Optional[str] = None, max_messages: int = 1) -> None:
        target = recipient or self._target
        self._minibot.send_msg(target, message)

    def reply(self, message: str, recipient: Optional[str] = None) -> None:
        target = recipient or self._target
        if target.startswith("#"):
            self._minibot.send_msg(target, f"{self._sender}: {message}")
        else:
            self._minibot.send_msg(target, message)

    def action(self, message: str, recipient: Optional[str] = None) -> None:
        target = recipient or self._target
        if hasattr(self._minibot, "raw_send"):
            self._minibot.raw_send(f"PRIVMSG {target} :\x01ACTION {message}\x01")
        elif hasattr(self._minibot, "send_msg"):
            self._minibot.send_msg(target, f"* {message}")


class SopelTrigger:
    """Mock Sopel trigger instance encapsulating IRC event payload."""
    def __init__(self, bot_nick: str, target: str, sender_nick: str, hostmask: str, text: str, match_obj: Optional[re.Match] = None):
        self.nick = sender_nick
        self.user = sender_nick
        self.host = hostmask
        self.sender = target
        self.channel = target if target.startswith("#") else ""
        self.is_privmsg = not target.startswith("#")
        self.raw = text

        if match_obj:
            self.match = match_obj
            self.group = match_obj.group
            self.groups = match_obj.groups
        else:
            # Emulate Sopel command regex: group(0)=full, group(1)=cmd, group(2)=args
            cmd_match = re.match(r"^!?[^\s]+\s*(.*)$", text)
            parts = text.split()
            cmd_name = parts[0].lstrip("!.") if parts else ""
            args_str = cmd_match.group(1).strip() if cmd_match else ""

            def _group(n=0):
                if n == 0:
                    return text
                elif n == 1:
                    return cmd_name
                elif n == 2:
                    return args_str
                return ""

            self.match = None
            self.group = _group
            self.groups = lambda: (cmd_name, args_str)


# Decorators for sopel.module
def commands(*names: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        if not hasattr(func, "_sopel_commands"):
            func._sopel_commands = []
        func._sopel_commands.extend(names)
        return func
    return decorator


def rule(*patterns: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        if not hasattr(func, "_sopel_rules"):
            func._sopel_rules = []
        func._sopel_rules.extend(patterns)
        return func
    return decorator


def interval(seconds: int) -> Callable:
    def decorator(func: Callable) -> Callable:
        func._sopel_interval = seconds
        return func
    return decorator


def rate(user: int = 0, channel: int = 0, server: int = 0) -> Callable:
    def decorator(func: Callable) -> Callable:
        func._sopel_rate = {"user": user, "channel": channel, "server": server}
        return func
    return decorator


def example(msg: str, result: Optional[str] = None) -> Callable:
    def decorator(func: Callable) -> Callable:
        return func
    return decorator


def require_chan(*channels: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        func._sopel_require_chan = [c.lower() for c in channels]
        return func
    return decorator


def require_admin(func: Callable) -> Callable:
    func._sopel_require_admin = True
    return func


def inject_sopel_shim() -> None:
    """Injects lightweight sopel and sopel.module into sys.modules."""
    if "sopel" not in sys.modules:
        sopel_mod = types.ModuleType("sopel")
        sopel_mod.__path__ = []
        sopel_module_mod = types.ModuleType("sopel.module")
        sopel_module_mod.__path__ = []

        sopel_module_mod.commands = commands
        sopel_module_mod.rule = rule
        sopel_module_mod.interval = interval
        sopel_module_mod.rate = rate
        sopel_module_mod.example = example
        sopel_module_mod.require_chan = require_chan
        sopel_module_mod.require_admin = require_admin

        sopel_mod.module = sopel_module_mod
        sys.modules["sopel"] = sopel_mod
        sys.modules["sopel.module"] = sopel_module_mod


class module:
    commands = commands
    rule = rule
    interval = interval
    rate = rate
    example = example
    require_chan = require_chan
    require_admin = require_admin
