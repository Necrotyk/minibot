# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
script_adapter.py — Universal Multi-Framework Script Adapter for Minibots.
Seamlessly routes IRC events across Sopel, Errbot, Discord.py, Limnoria, SingleServerIRCBot, and Native plugins.
"""

import asyncio
import inspect
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from .discord_shim import Context, inject_discord_shim
from .errbot_shim import BotPlugin, inject_errbot_shim
from .ircbot_shim import Event, SingleServerIRCBot, inject_ircbot_shim
from .limnoria_shim import LimnoriaIrcWrapper, LimnoriaMsgWrapper, inject_limnoria_shim
from .sopel_shim import SopelBotWrapper, SopelTrigger, inject_sopel_shim

logger = logging.getLogger("ScriptAdapter")


class ScriptAdapter:
    """Universal adapter exposing a unified IRC event lifecycle across multiple bot frameworks."""

    def __init__(self, module: Any, minibot_instance: Any):
        self.module = module
        self.minibot = minibot_instance
        self.name = getattr(module, "__name__", "script")

        # Inject shims into sys.modules
        inject_sopel_shim()
        inject_limnoria_shim()
        inject_errbot_shim()
        inject_discord_shim()
        inject_ircbot_shim()

        self.instances: List[Any] = []
        self._init_framework_instances()

        # Check for setup/init hook
        if hasattr(module, "setup") and callable(module.setup):
            try:
                module.setup(self.minibot)
            except Exception as e:
                logger.error(f"Error in setup() for {self.name}: {e}")

    def _init_framework_instances(self):
        """Discovers and instantiates plugin classes (Errbot BotPlugin, IRCBot, Limnoria)."""
        for attr_name in dir(self.module):
            attr = getattr(self.module, attr_name)
            if inspect.isclass(attr) and attr.__module__ == self.module.__name__:
                try:
                    if issubclass(attr, BotPlugin):
                        inst = attr(bot=self.minibot)
                        inst.activate()
                        self.instances.append(inst)
                    elif issubclass(attr, SingleServerIRCBot):
                        inst = attr(nickname=getattr(self.minibot, "nick", "minibot"))
                        inst.connection._bot = self.minibot
                        inst._parent_bot = self.minibot
                        self.instances.append(inst)
                except Exception as e:
                    logger.debug(f"Could not auto-instantiate class {attr_name} in {self.name}: {e}")

    def on_privmsg(self, target: str, sender_nick: str, message: str) -> None:
        """Dispatches PRIVMSG events across all framework command bindings."""
        text = message.strip()
        if not text:
            return

        bot_nick = getattr(self.minibot, "nick", "minibot").lower()
        cmd_prefix = "!"
        is_cmd = text.startswith(cmd_prefix) or text.startswith(".") or text.startswith("?")
        cmd_name = ""
        cmd_args: List[str] = []

        if is_cmd:
            parts = text[1:].split()
            if parts:
                cmd_name = parts[0].lower()
                cmd_args = parts[1:]
        elif text.lower().startswith(f"{bot_nick}:") or text.lower().startswith(f"{bot_nick},"):
            parts = text.split()[1:]
            if parts:
                cmd_name = parts[0].lower().lstrip("!.")
                cmd_args = parts[1:]

        # 1. Dispatch Sopel Commands & Rules
        for attr_name in dir(self.module):
            func = getattr(self.module, attr_name)
            if callable(func):
                # Sopel commands
                sopel_cmds = getattr(func, "_sopel_commands", [])
                if cmd_name and cmd_name in sopel_cmds:
                    try:
                        wrapper = SopelBotWrapper(self.minibot, target, sender_nick)
                        trigger = SopelTrigger(bot_nick, target, sender_nick, f"{sender_nick}!user@host", text)
                        func(wrapper, trigger)
                    except Exception as e:
                        logger.error(f"[Sopel Error] {self.name}.{attr_name}: {e}")

                # Sopel rules
                sopel_rules = getattr(func, "_sopel_rules", [])
                for pattern in sopel_rules:
                    m = re.search(pattern, text)
                    if m:
                        try:
                            wrapper = SopelBotWrapper(self.minibot, target, sender_nick)
                            trigger = SopelTrigger(bot_nick, target, sender_nick, f"{sender_nick}!user@host", text, match_obj=m)
                            func(wrapper, trigger)
                        except Exception as e:
                            logger.error(f"[Sopel Rule Error] {self.name}.{attr_name}: {e}")

                # Discord.py style commands
                discord_cmds = getattr(func, "_discord_commands", [])
                if cmd_name and cmd_name in discord_cmds:
                    try:
                        ctx = Context(self.minibot, target, sender_nick, text)
                        if asyncio.iscoroutinefunction(func):
                            asyncio.create_task(func(ctx, *cmd_args))
                        else:
                            func(ctx, *cmd_args)
                    except Exception as e:
                        logger.error(f"[Discord Error] {self.name}.{attr_name}: {e}")

        # 2. Dispatch Errbot BotPlugin methods
        for inst in self.instances:
            if isinstance(inst, BotPlugin):
                for method_name in dir(inst):
                    method = getattr(inst, method_name)
                    if callable(method) and hasattr(method, "_errbot_commands"):
                        for meta in method._errbot_commands:
                            if cmd_name and meta["name"] == cmd_name:
                                try:
                                    res = method(text, " ".join(cmd_args))
                                    if res:
                                        self.minibot.send_msg(target, str(res))
                                except Exception as e:
                                    logger.error(f"[Errbot Error] {inst.__class__.__name__}.{method_name}: {e}")

            # 3. Dispatch SingleServerIRCBot events
            elif isinstance(inst, SingleServerIRCBot):
                try:
                    ev_type = "pubmsg" if target.startswith("#") else "privmsg"
                    ev = Event(ev_type, sender_nick, target, [text])
                    if ev_type == "pubmsg":
                        inst.on_pubmsg(inst.connection, ev)
                    else:
                        inst.on_privmsg(inst.connection, ev)
                except Exception as e:
                    logger.error(f"[IRCBot Error] {inst.__class__.__name__}: {e}")

        # 4. Dispatch Native Hooks
        if hasattr(self.module, "handle_message") and callable(self.module.handle_message):
            try:
                self.module.handle_message(self.minibot, target, sender_nick, message)
            except Exception as e:
                logger.error(f"Error in handle_message() for {self.name}: {e}")

        if hasattr(self.module, "on_privmsg") and callable(self.module.on_privmsg):
            try:
                self.module.on_privmsg(self.minibot, target, sender_nick, message)
            except Exception as e:
                logger.error(f"Error in on_privmsg() for {self.name}: {e}")

    def get_commands(self) -> List[str]:
        """Returns list of primary commands registered in this script."""
        cmds = []
        for attr_name in dir(self.module):
            func = getattr(self.module, attr_name)
            if callable(func):
                sopel_cmds = getattr(func, "_sopel_commands", [])
                if sopel_cmds:
                    cmds.append(sopel_cmds[0])
                discord_cmds = getattr(func, "_discord_commands", [])
                if discord_cmds:
                    cmds.append(discord_cmds[0])
        for inst in self.instances:
            if isinstance(inst, BotPlugin):
                for method_name in dir(inst):
                    m = getattr(inst, method_name)
                    if hasattr(m, "_errbot_commands"):
                        for meta in m._errbot_commands:
                            cmds.append(meta["name"])
        return sorted(list(set(cmds)))

    def teardown(self) -> None:
        for inst in self.instances:
            if hasattr(inst, "deactivate") and callable(inst.deactivate):
                try:
                    inst.deactivate()
                except Exception as e:
                    logger.debug(f"Error deactivating {inst}: {e}")

        if hasattr(self.module, "teardown") and callable(self.module.teardown):
            try:
                self.module.teardown(self.minibot)
            except Exception as e:
                logger.error(f"Error in teardown() for {self.name}: {e}")
