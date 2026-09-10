# SPDX-License-Identifier: AGPL-3.0-or-later
"""
manager.py — Minibot Lifecycle Manager & Dynamic State Registry.
"""

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from core.llm_router import LLMRouter
from core.sideload import SideloadEngine
from core.telemetry import sqlite_retry
from minibots.adapters.limnoria_shim import inject_limnoria_shim
from minibots.adapters.script_adapter import ScriptAdapter
from minibots.adapters.sopel_shim import inject_sopel_shim
from minibots.base import MiniBot
from plugins.julesbot import JulesBotPlugin
from plugins.taskbot import TaskBotPlugin
from plugins.toolbot import ToolbotPlugin
from plugins.trollbot import TrollbotPlugin
from plugins.urls import URLPlugin

logger = logging.getLogger("MinibotManager")


class MinibotManager:
    """Orchestrates spawning, state persistence, routing, and termination of minibots."""
    def __init__(
        self,
        config: Dict[str, Any],
        raw_send_callback: Optional[Callable[[str, str, str], None]] = None,
        conn_event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        db_path: Optional[str] = None
    ):
        self.config = config
        self.raw_send_callback = raw_send_callback
        self.conn_event_callback = conn_event_callback
        if not db_path:
            env_db = os.environ.get("MINIBOTS_DB_PATH")
            if env_db:
                db_path = env_db
            else:
                data_dir = Path(__file__).resolve().parents[1] / "data"
                data_dir.mkdir(parents=True, exist_ok=True)
                db_path = str(data_dir / "minibots_state.db")

        self.db_path = db_path
        self.active_bots: Dict[str, MiniBot] = {}
        self.lock = threading.RLock()
        self.sideload_engine = SideloadEngine(on_reload_callback=self._on_sideload_reloaded)
        self.llm_router = LLMRouter(self.config)
        from core.kaggle_bot_binder import KaggleBotBinder
        self.bot_binder = KaggleBotBinder(self.config)

        # Inject shim modules into sys.modules
        inject_sopel_shim()
        inject_limnoria_shim()

        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.row_factory = sqlite3.Row
        return conn

    @sqlite_retry()
    def _init_db(self) -> None:
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS minibots (
                        nick TEXT PRIMARY KEY,
                        network TEXT NOT NULL,
                        channels TEXT NOT NULL,
                        modules TEXT NOT NULL,
                        creator_nick TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );
                """)
        finally:
            conn.close()

    def get_registered_nicks(self) -> Set[str]:
        """Returns set of all active minibot nicknames for loop suppression."""
        with self.lock:
            return {b.nick.lower() for b in self.active_bots.values()}

    def spawn_bot(
        self,
        nick: str,
        network: str,
        channels: List[str],
        module_names: List[str],
        creator_nick: str = "admin"
    ) -> MiniBot:
        """Spawns, registers, and requests discrete socket connection for a new minibot instance."""
        with self.lock:
            if nick in self.active_bots:
                logger.warning(f"Bot {nick} already active, stopping prior instance.")
                self.active_bots[nick].stop()

            # Helper for bot instance raw send with network context
            def _bot_raw_send(bot_nick: str, line: str) -> None:
                if self.raw_send_callback:
                    self.raw_send_callback(bot_nick, line, network)

            bot = MiniBot(
                nick=nick,
                network=network,
                channels=channels,
                creator_nick=creator_nick,
                raw_send_callback=_bot_raw_send,
                config=self.config,
                on_channels_updated=lambda b: self._persist_bot(b, list(b.active_modules))
            )
            bot.manager = self

            # Attach requested modules
            for mod in module_names:
                self._attach_module(bot, mod.strip())

            self.active_bots[nick] = bot
            self._persist_bot(bot, list(bot.active_modules))

        # Request discrete socket spawn in conn_daemon
        if self.conn_event_callback:
            self.conn_event_callback({
                "type": "spawn_conn",
                "network": network,
                "nick": nick,
                "channels": channels
            })

        logger.info(f"Spawned minibot {nick} on {network} in {channels} with modules {module_names}")
        return bot

    def _attach_module(self, bot: MiniBot, mod_name: str) -> bool:
        """Attaches native plugin or sideloaded script adapter to bot. Returns True on success, False otherwise."""
        mod_lower = mod_name.lower()

        if mod_lower == "urls":
            bot.enable_module("urls", URLPlugin(bot))
            return True
        elif mod_lower == "trollbot":
            bot.enable_module("trollbot", TrollbotPlugin(bot, self.llm_router, bot_binder=self.bot_binder))
            return True
        elif mod_lower == "toolbot":
            bot.enable_module("toolbot", ToolbotPlugin(bot, self.config.get("LLM", {}).get("MCP_SERVER_URL")))
            return True
        elif mod_lower == "julesbot":
            bot.enable_module("julesbot", JulesBotPlugin(bot, self.config.get("JULES", {}).get("API_KEY")))
            return True
        elif mod_lower in ("taskbot", "task"):
            bot.enable_module("taskbot", TaskBotPlugin(bot, self.llm_router, bot_binder=self.bot_binder))
            return True
        elif mod_lower in ("dpop_auth", "auth", "dpop"):
            from plugins.dpop_auth import DPoPAuthPlugin
            bot.enable_module("dpop_auth", DPoPAuthPlugin(bot, self.config))
            return True
        elif mod_lower.startswith("sideload:") or mod_lower.startswith("sideload_") or mod_lower.endswith(".py"):
            script_path = mod_name.replace("sideload:", "").replace("sideload_", "")
            if not script_path.endswith(".py"):
                script_path = f"{script_path}.py"
            mod_obj = self.sideload_engine.load_script(script_path)
            if mod_obj:
                adapter = ScriptAdapter(mod_obj, bot)
                bot.enable_module(f"sideload_{Path(script_path).stem}", adapter)
                return True
            logger.error(f"Failed to load script '{script_path}' for {bot.nick}")
            return False
        else:
            # Check if mod_name corresponds to an existing script in sideload_dir (e.g. "web_search")
            candidate = mod_name.replace("sideload:", "").replace("sideload_", "")
            candidate_file = candidate if candidate.endswith(".py") else f"{candidate}.py"
            candidate_path = self.sideload_engine.sideload_dir / candidate_file
            if candidate_path.exists():
                mod_obj = self.sideload_engine.load_script(candidate_file)
                if mod_obj:
                    adapter = ScriptAdapter(mod_obj, bot)
                    bot.enable_module(f"sideload_{Path(candidate_file).stem}", adapter)
                    return True
                logger.error(f"Failed to load script '{candidate_file}' for {bot.nick}")
                return False

            logger.warning(f"Unknown module requested for {bot.nick}: {mod_name}")
            return False

    def attach_module(self, nick: str, mod_name: str) -> bool:
        """Dynamically attaches a module to an active minibot and persists update."""
        with self.lock:
            bot = self.active_bots.get(nick)
            if not bot:
                return False
            ok = self._attach_module(bot, mod_name)
            if not ok:
                return False
            current_mods = list(bot.active_modules)
            self._persist_bot(bot, current_mods)
            return True

    def detach_module(self, nick: str, mod_name: str) -> bool:
        """Dynamically detaches a module from an active minibot and persists update."""
        with self.lock:
            bot = self.active_bots.get(nick)
            if not bot:
                return False
            bot.disable_module(mod_name)
            current_mods = list(bot.active_modules)
            self._persist_bot(bot, current_mods)
            return True

    def bot_join(self, nick: str, channel: str) -> bool:
        """Instructs a minibot to join a channel and persists update."""
        with self.lock:
            bot = self.active_bots.get(nick)
            if not bot:
                return False
            bot.join(channel)
            current_mods = list(bot.active_modules)
            self._persist_bot(bot, current_mods)
            return True

    def bot_part(self, nick: str, channel: str, reason: str = "Leaving") -> bool:
        """Instructs a minibot to part a channel and persists update."""
        with self.lock:
            bot = self.active_bots.get(nick)
            if not bot:
                return False
            bot.part(channel, reason=reason)
            current_mods = list(bot.active_modules)
            self._persist_bot(bot, current_mods)
            return True

    def update_bot_nick(self, network: str, old_nick: str, new_nick: str) -> None:
        """Updates in-memory and database records when a nick collision (433) occurs."""
        with self.lock:
            bot = self.active_bots.pop(old_nick, None)
            if bot:
                bot.nick = new_nick
                self.active_bots[new_nick] = bot

        conn = self._get_conn()
        try:
            with conn:
                conn.execute("UPDATE minibots SET nick = ? WHERE nick = ?;", (new_nick, old_nick))
        except Exception as e:
            logger.error(f"Failed to update bot nick in db: {e}")
        finally:
            conn.close()

    @sqlite_retry()
    def _persist_bot(self, bot: MiniBot, module_names: List[str]) -> None:
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("""
                    INSERT OR REPLACE INTO minibots (nick, network, channels, modules, creator_nick, created_at)
                    VALUES (?, ?, ?, ?, ?, ?);
                """, (bot.nick, bot.network, json.dumps(bot.channels), json.dumps(module_names), bot.creator_nick, time.time()))
        except Exception as e:
            logger.error(f"Failed to persist bot {bot.nick}: {e}")
        finally:
            conn.close()

    @sqlite_retry()
    def kill_bot(self, nick: str, creator: str = "admin") -> bool:
        """Stops and removes a minibot instance, closing its discrete socket."""
        bot_network = None
        with self.lock:
            bot = self.active_bots.pop(nick, None)
            if bot:
                bot_network = bot.network
                bot.stop()
                if self.conn_event_callback:
                    self.conn_event_callback({
                        "type": "kill_conn",
                        "network": bot.network,
                        "nick": nick,
                        "creator": creator
                    })

        conn = self._get_conn()
        found_in_db = False
        try:
            with conn:
                cur = conn.execute("SELECT network FROM minibots WHERE nick = ?;", (nick,))
                row = cur.fetchone()
                if row:
                    found_in_db = True
                    if not bot_network:
                        bot_network = row["network"]
                conn.execute("DELETE FROM minibots WHERE nick = ?;", (nick,))
        except Exception as e:
            logger.error(f"Failed to delete bot record {nick}: {e}")
        finally:
            conn.close()

        if not bot and found_in_db and bot_network and self.conn_event_callback:
            self.conn_event_callback({
                "type": "kill_conn",
                "network": bot_network,
                "nick": nick,
                "creator": creator
            })

        return (bot is not None) or found_in_db

    @sqlite_retry()
    def rehydrate_bots(self) -> List[str]:
        """Restores all persisted minibots from SQLite on daemon startup."""
        conn = self._get_conn()
        restored = []
        try:
            cur = conn.execute("SELECT nick, network, channels, modules, creator_nick FROM minibots;")
            rows = cur.fetchall()
            for r in rows:
                nick = r["nick"]
                network = r["network"]
                channels = json.loads(r["channels"])
                modules = json.loads(r["modules"])
                creator = r["creator_nick"]
                self.spawn_bot(nick, network, channels, modules, creator_nick=creator)
                restored.append(nick)
        except Exception as e:
            logger.error(f"Failed to rehydrate bots from database: {e}")
        finally:
            conn.close()
        return restored

    def dispatch_privmsg(self, network: str, target: str, sender_nick: str, message: str, hostmask: str = "") -> None:
        """Routes incoming PRIVMSG to target minibots."""
        # Cross-bot loop check
        registered_nicks = self.get_registered_nicks()
        if sender_nick.lower() in registered_nicks:
            return  # Suppress loop

        with self.lock:
            bots = list(self.active_bots.values())

        for bot in bots:
            if bot.network == network:
                if target.startswith("#") and any(c.lower() == target.lower() for c in bot.channels):
                    bot.on_privmsg(target, sender_nick, message, hostmask=hostmask)
                elif target.lower() == bot.nick.lower():
                    bot.on_privmsg(target, sender_nick, message, hostmask=hostmask)

    def _on_sideload_reloaded(self, reloaded: List[str]) -> None:
        """Re-attaches updated sideload modules to all active minibots that use them."""
        with self.lock:
            for bot in self.active_bots.values():
                for mod_name in reloaded:
                    stem = mod_name.replace("sideload_", "")
                    if mod_name in bot.active_modules or stem in bot.active_modules or f"sideload_{stem}" in bot.active_modules:
                        self._attach_module(bot, f"sideload_{stem}")
                        logger.info(f"Hot-reloaded module {mod_name} on bot {bot.nick}")

    def reload_all_modules(self, target_mod: Optional[str] = None) -> List[str]:
        """Hot-reloads all active plugins and sideloaded scripts, updating active minibots."""
        reloaded: List[str] = []
        if target_mod and target_mod.lower() != "all":
            raw = target_mod.lower().replace("sideload:", "").replace("sideload_", "")
            script_file = raw if raw.endswith(".py") else f"{raw}.py"
            mod_key = f"sideload_{Path(script_file).stem}"

            # Check if it's a native plugin
            if raw in ("urls", "toolbot", "trollbot", "julesbot", "dpop_auth", "auth", "dpop"):
                import importlib
                mod_name_map = {
                    "urls": "plugins.urls",
                    "toolbot": "plugins.toolbot",
                    "trollbot": "plugins.trollbot",
                    "julesbot": "plugins.julesbot",
                    "dpop_auth": "plugins.dpop_auth",
                    "auth": "plugins.dpop_auth",
                    "dpop": "plugins.dpop_auth",
                }
                py_mod = mod_name_map.get(raw)
                if py_mod and py_mod in sys.modules:
                    try:
                        importlib.reload(sys.modules[py_mod])
                    except Exception as e:
                        logger.error(f"Failed reloading native plugin {py_mod}: {e}")
                with self.lock:
                    for bot in self.active_bots.values():
                        if raw in bot.active_modules:
                            self._attach_module(bot, raw)
                reloaded.append(raw)
            else:
                mod = self.sideload_engine.load_script(script_file)
                if mod:
                    reloaded.append(mod_key)
                    self._on_sideload_reloaded([mod_key])
        else:
            # Check mtimes for modified sideloads
            modified_sideloads = self.sideload_engine.check_and_reload_modified()
            reloaded.extend(modified_sideloads)

            # If target_mod == "all", also reload all already-loaded sideload scripts & native plugins
            if target_mod and target_mod.lower() == "all":
                for name, mod in list(self.sideload_engine.loaded_modules.items()):
                    file_path = getattr(mod, "__file__", None)
                    if file_path and os.path.isfile(file_path):
                        if self.sideload_engine.load_script(file_path, name):
                            if name not in reloaded:
                                reloaded.append(name)
                import importlib
                for py_mod in ["plugins.urls", "plugins.toolbot", "plugins.trollbot", "plugins.julesbot"]:
                    if py_mod in sys.modules:
                        try:
                            importlib.reload(sys.modules[py_mod])
                            short = py_mod.split(".")[-1]
                            if short not in reloaded:
                                reloaded.append(short)
                        except Exception as e:
                            logger.error(f"Failed reloading {py_mod}: {e}")

                # Re-attach to all active bots
                with self.lock:
                    for bot in self.active_bots.values():
                        for mod in list(bot.active_modules):
                            self._attach_module(bot, mod)

        return reloaded
