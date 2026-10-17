#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
main.py — Minibots Logic Daemon, Command Dispatcher & Hot-Reloader.
Connects to conn_daemon over UNIX domain IPC socket (data/minibots_ipc.sock).
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Path resolution
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import keystore
from core.config import ConfigManager
from core.dispatcher import CommandDispatcher
from core.command_editor import CommandEditor
from core.guard import is_public_channel
from core.kaggle_manager import KaggleKernelManager
from core.logger import activity_buffer, setup_logger
from core.telemetry import TelemetryEngine
from core.tool_catalog import ToolCatalog
from core.tool_craft import ToolCrafter
from minibots.manager import MinibotManager
from core.kaggle_bot_binder import KaggleBotBinder
from plugins.kagglebot import KaggleBotPlugin
from plugins.taskbot import TaskBotPlugin
from plugins.toolbot import call_mcp_tool, check_port, eval_calc_expression, resolve_dns
from plugins.trollbot import PERSONA_PRESETS
from plugins.dpop_auth import DPoPAuthPlugin

logger = setup_logger("MinibotsMain", logging.INFO)


SOCKET_IPC_PATH = os.environ.get("MINIBOTS_IPC_SOCK", str(BASE_DIR / "data" / "minibots_ipc.sock"))


def _safe_create_task(coro):
    """Schedules coroutine in active event loop, falling back to asyncio.run if outside a loop."""
    try:
        loop = asyncio.get_running_loop()
        return loop.create_task(coro)
    except RuntimeError:
        return asyncio.run(coro)


class _DaemonBotProxy:
    """Lightweight adapter bridging MinibotsLogicDaemon to Sopel/sideload plugins."""
    def __init__(self, daemon: Any, network: str):
        self.daemon = daemon
        self.network = network
        server_cfg = daemon.config.data.get("SERVERS", {}).get(network, {}) if hasattr(daemon.config, "data") else {}
        self.nick = server_cfg.get("nick", "minibots")
        self.channels = server_cfg.get("channels", ["#minibot", "#minibots"])
        self._minibot = self
        self.dispatcher = getattr(daemon, "dispatcher", None)

    def say(self, message: str, recipient: Optional[str] = None) -> None:
        target = recipient or (self.channels[0] if self.channels else "#minibot")
        self.daemon.send_privmsg(self.network, target, message)

    def reply(self, message: str, recipient: Optional[str] = None) -> None:
        target = recipient or (self.channels[0] if self.channels else "#minibot")
        if target.startswith("#"):
            self.daemon.send_privmsg(self.network, target, f"{target}: {message}")
        else:
            self.daemon.send_privmsg(self.network, target, message)


class MinibotsLogicDaemon:
    """Core logic daemon managing bots, plugins, commands, and telemetry."""
    def __init__(self, config: ConfigManager, db_path: Optional[str] = None):
        self.config = config
        self.telemetry = TelemetryEngine()
        self.dispatcher = CommandDispatcher(admin_nicks=config.admin_nicks)
        self.kaggle_mgr = KaggleKernelManager(
            config=config.data,
            broadcast_callback=lambda msg: self.send_privmsg(self.primary_network, self.admin_channels[0] if self.admin_channels else "#minibots", msg)
        )
        self.manager = MinibotManager(
            config=config.data,
            raw_send_callback=self._send_raw_to_ipc,
            conn_event_callback=self._send_event_to_ipc,
            db_path=db_path
        )
        self.sideload_engine = self.manager.sideload_engine
        self.bot_binder = self.manager.bot_binder
        self.kaggle_plugin = KaggleBotPlugin(self, self.kaggle_mgr, self.bot_binder)
        self.task_plugin = TaskBotPlugin(self, self.manager.llm_router, bot_binder=self.bot_binder)
        self.command_editor = CommandEditor()
        self.tool_catalog = ToolCatalog(sideload_dir=self.sideload_engine.sideload_dir)
        self.tool_crafter = ToolCrafter(self.manager.llm_router, sideload_dir=self.sideload_engine.sideload_dir)

        class _DPoPBotProxy:
            def __init__(self, daemon):
                self.daemon = daemon
                self.nick = "minibots"
                self.network = daemon.primary_network
                self.raw_send_callback = lambda nick, line: self.daemon._send_raw_to_ipc(nick, line, network=self.network)
            def send_msg(self, target: str, message: str) -> None:
                self.daemon.send_privmsg(self.network, target, message)

        self.dpop_plugin = DPoPAuthPlugin(_DPoPBotProxy(self), config=config.data)

        self.ipc_reader: Optional[asyncio.StreamReader] = None
        self.ipc_writer: Optional[asyncio.StreamWriter] = None
        self._running = True
        self.start_time = time.time()

        self._register_commands()

    @property
    def primary_network(self) -> str:
        if hasattr(self.config, "primary_network") and isinstance(self.config.primary_network, str):
            return self.config.primary_network
        return "local"

    @property
    def admin_channels(self) -> List[str]:
        if hasattr(self.config, "admin_channels") and isinstance(self.config.admin_channels, list):
            return self.config.admin_channels
        return ["#minibot", "#minibots", "#dev"]

    def is_trusted_network(self, network: str) -> bool:
        if hasattr(self.config, "is_trusted_network") and callable(self.config.is_trusted_network):
            res = self.config.is_trusted_network(network)
            if isinstance(res, bool):
                return res
        net = (network or "").lower().strip()
        return net.startswith("local") or net.startswith("wireguard") or net == "wg"

    def _send_raw_to_ipc(self, bot_nick: str, raw_line: str, network: Optional[str] = None) -> None:
        """Transmits raw IRC line to conn_daemon over IPC."""
        if network is None:
            network = self.primary_network
        if self.ipc_writer and not self.ipc_writer.is_closing():
            payload = json.dumps({"type": "raw", "network": network, "nick": bot_nick, "line": raw_line}) + "\n"
            try:
                self.ipc_writer.write(payload.encode("utf-8"))
            except Exception as e:
                logger.error(f"Failed to write to IPC: {e}")

    def _send_event_to_ipc(self, event_payload: Dict[str, Any]) -> None:
        """Transmits control event (e.g. spawn_conn, kill_conn) to conn_daemon over IPC."""
        if self.ipc_writer and not self.ipc_writer.is_closing():
            payload = json.dumps(event_payload) + "\n"
            try:
                self.ipc_writer.write(payload.encode("utf-8"))
            except Exception as e:
                logger.error(f"Failed to send event to IPC: {e}")

    def _register_commands(self) -> None:
        """Registers the full interactive command suite with aliases and shorthands."""
        # Help & Info
        self.dispatcher.register("builder", self._cmd_builder, min_ring=1, help_text="Bot builder walkthrough: builder [frameworks|examples|editor]", category="bots", aliases=["build", "botbuilder"])
        self.dispatcher.register("help", self._cmd_help, min_ring=1, help_text="Show command menu", category="info", aliases=["h", "commands", "man"])
        self.dispatcher.register("ping", self._cmd_ping, min_ring=1, help_text="Check latency and connectivity", category="info", aliases=["p", "pong"])
        self.dispatcher.register("status", self._cmd_status, min_ring=1, help_text="Daemon status and statistics", category="info", aliases=["st", "info"])

        # Bot Lifecycle
        self.dispatcher.register("list", self._cmd_list, min_ring=1, help_text="List active minibots", category="bots", aliases=["ls", "bots", "ps"])
        self.dispatcher.register("spawn", self._cmd_spawn, min_ring=0, help_text="Spawn new bot: spawn <nick> <chan> <mods>", category="bots", aliases=["create", "addbot"])
        self.dispatcher.register("kill", self._cmd_kill, min_ring=0, help_text="Kill bot: kill <nick>", category="bots", aliases=["stop", "rm", "delbot"])
        self.dispatcher.register("attach", self._cmd_attach, min_ring=0, help_text="Attach module: attach <nick> <mod>", category="bots")
        self.dispatcher.register("detach", self._cmd_detach, min_ring=0, help_text="Detach module: detach <nick> <mod>", category="bots")
        self.dispatcher.register("reload", self._cmd_reload, min_ring=0, help_text="Hot-reload plugins and sideloads: reload [mod|all]", category="bots", aliases=["r", "hotreload"])
        self.dispatcher.register("sideload", self._cmd_sideload, min_ring=1, help_text="Sideload script operations: sideload list", category="bots")

        # Diagnostics & Math Tools
        self.dispatcher.register("calc", self._cmd_calc, min_ring=1, help_text="Evaluate math expression: calc <expr>", category="tools", aliases=["c"])
        self.dispatcher.register("dns", self._cmd_dns, min_ring=1, help_text="Resolve DNS hostname: dns <host>", category="tools", aliases=["d"])
        self.dispatcher.register("port", self._cmd_port, min_ring=1, help_text="Test TCP port: port <host> <port>", category="tools", aliases=["pinghost"])
        self.dispatcher.register("mcp", self._cmd_mcp, min_ring=1, help_text="Call FastMCP tool: mcp <tool> <args>", category="tools")
        self.dispatcher.register("hub", self._cmd_hub, min_ring=1, help_text="Tool catalog & dynamic puller: hub <list|search|info|install|pull>", category="tools", aliases=["toolhub", "pkg"])
        self.dispatcher.register("tool", self._cmd_tool, min_ring=1, help_text="Native tool execution: tool <dns|ping|calc|port|hash|http|json|cron|crypto|search|info|scaffold|import>", category="tools")
        self.dispatcher.register("cmd", self._cmd_custom_command, min_ring=1, help_text="Custom command editor: add | py | begin | save | cancel | paste | list | info | rm", category="tools", aliases=["command", "customcmd"])
        self.dispatcher.register("hash", self._cmd_hash, min_ring=1, help_text="Compute cryptographic hash: hash <algo> <text>", category="tools")
        self.dispatcher.register("http", self._cmd_http, min_ring=1, help_text="Inspect HTTP status & headers: http <url>", category="tools")
        self.dispatcher.register("json", self._cmd_json, min_ring=1, help_text="Validate and inspect JSON: json <expr>", category="tools")
        self.dispatcher.register("cron", self._cmd_cron, min_ring=1, help_text="Explain cron schedule: cron <5 fields>", category="tools")
        self.dispatcher.register("crypto", self._cmd_crypto, min_ring=1, help_text="Cipher codec: crypto <rot13|base64|hex> <enc|dec> <text>", category="tools")
        self.dispatcher.register("wiki", self._cmd_wiki, min_ring=1, help_text="Wikipedia article lookup: wiki <topic>", category="tools", aliases=["wikipedia"])
        self.dispatcher.register("grok", self._cmd_grok, min_ring=1, help_text="Grokipedia fast lookup: grok <topic>", category="tools", aliases=["grokipedia", "grokpedia"])

        # AI & Reasoning
        self.dispatcher.register("model", self._cmd_model, min_ring=1, help_text="Inspect or switch LLM model: model <name|list|auto>", category="ai", aliases=["daimodel"])
        self.dispatcher.register("models", self._cmd_models, min_ring=1, help_text="List available LLM models on Pi 5 worker", category="ai")
        self.dispatcher.register("ai", self._cmd_ai, min_ring=1, help_text="Pure local LLM chat query: ai [-r] <prompt>", category="ai", aliases=["ask"])
        self.dispatcher.register("air", self._cmd_air, min_ring=1, help_text="Pure local LLM query with deep reasoning: air <prompt>", category="ai")
        self.dispatcher.register("dai", self._cmd_dai, min_ring=1, help_text="AI search & reasoning pass: dai [-r] <query>", category="ai", aliases=["ddgai", "ai_search"])
        self.dispatcher.register("dair", self._cmd_dair, min_ring=1, help_text="Deep 3-phase reasoning search pass: dair <query>", category="ai", aliases=["diar"])
        self.dispatcher.register("craft", self._cmd_craft, min_ring=0, help_text="AI tool crafter: craft <name> <prompt>", category="ai", aliases=["autotool"])
        self.dispatcher.register("jules", self._cmd_jules, min_ring=0, help_text="Google Jules coding agent: init | approve | status | cancel", category="ai")
        self.dispatcher.register("troll", self._cmd_troll, min_ring=1, help_text="Trollbot persona configuration: set | custom | list | reset", category="ai")
        self.dispatcher.register("kaggle", self._cmd_kaggle, min_ring=0, help_text="Kaggle GPU worker control: start | stop | status | model <name>", category="ai", aliases=["kg"])
        self.dispatcher.register("task", self._cmd_task, min_ring=1, help_text="Autonomous task & code execution: task <prompt>", category="ai", aliases=["do"])
        self.dispatcher.register("granite", self._cmd_granite, min_ring=1, help_text="IBM Granite CTI analyst: gr <actor|campaign|mitre|triage|cve|brief|ask|status|switch> [args]", category="ai", aliases=["gr"])

        # Telemetry & Auditing
        self.dispatcher.register("usage", self._cmd_usage, min_ring=1, help_text="24h token telemetry summary", category="telemetry", aliases=["u", "stats", "quota"])
        self.dispatcher.register("cleanup", self._cmd_cleanup, min_ring=0, help_text="Prune old telemetry records: cleanup [days]", category="telemetry")
        self.dispatcher.register("logs", self._cmd_logs, min_ring=0, help_text="Inspect recent activity logs: logs [N]", category="admin", aliases=["l", "tail"])

        # Admin Channel Controls
        self.dispatcher.register("join", self._cmd_join, min_ring=0, help_text="Join channel: join [bot_nick] <#chan>", category="admin")
        self.dispatcher.register("part", self._cmd_part, min_ring=0, help_text="Part channel: part [bot_nick] <#chan>", category="admin")
        self.dispatcher.register("say", self._cmd_say, min_ring=0, help_text="Say message: say <target> <msg>", category="admin")
        self.dispatcher.register("raw", self._cmd_raw, min_ring=0, help_text="Send raw IRC line: raw <line>", category="admin")
        if self.config.data.get("DPOP_AUTH", {}).get("ENABLED", False):
            self.dispatcher.register("dpop", self._cmd_dpop, min_ring=0, help_text="DPoP sanctuary pass control: list | approve | deny | mode | revoke", category="admin", aliases=["authadmin"])
        self.dispatcher.register("server", self._cmd_server, min_ring=0, help_text="Dynamic IRC server registration: server add <net> <host[:port]> [ssl] | server list", category="admin", aliases=["srv"])

        # Umbrella aliases
        self.dispatcher.register("minibot", self._cmd_minibot, min_ring=0, help_text="Umbrella minibot command", category="bots", aliases=["mbot"])

    async def connect_ipc_and_run(self) -> None:
        """Connects to conn_daemon IPC socket, restores state, and enters event loop."""
        sock_path = Path(SOCKET_IPC_PATH)

        while self._running:
            try:
                if not sock_path.exists():
                    logger.debug(f"IPC socket {sock_path} not ready yet. Retrying in 2s...")
                    await asyncio.sleep(2.0)
                    continue

                logger.info(f"Connecting to connection daemon at {sock_path}...")
                self.ipc_reader, self.ipc_writer = await asyncio.open_unix_connection(str(sock_path))
                logger.info("Successfully attached to connection daemon IPC.")

                # Rehydrate persisted bots from SQLite
                rehydrated = self.manager.rehydrate_bots()
                if rehydrated:
                    logger.info(f"Rehydrated {len(rehydrated)} minibots from database: {', '.join(rehydrated)}")

                # Declaratively auto-spawn configured bots
                for auto_bot in self.config.data.get("AUTO_SPAWN_BOTS", []):
                    if isinstance(auto_bot, dict):
                        b_nick = auto_bot.get("nick")
                        b_net = auto_bot.get("network", self.primary_network)
                        b_chans = auto_bot.get("channels", self.admin_channels)
                        b_mods = auto_bot.get("modules", [])
                        b_creator = auto_bot.get("creator_nick", "admin")
                        if b_nick and b_nick not in self.manager.active_bots:
                            self.manager.spawn_bot(b_nick, b_net, b_chans, b_mods, creator_nick=b_creator)

                self.manager.sideload_engine.start_watcher(interval=2.0)
                asyncio.create_task(self._broadcast_startup_changelog())

                # Load troll targets from config into DAI troll system
                troll_nicks = [n.lower() for n in self.config.data.get("TROLL_NICKS", [])]
                if troll_nicks:
                    try:
                        from sideload.dai import _TROLL_NICKS
                    except ImportError:
                        from minibots.sideload.dai import _TROLL_NICKS
                    _TROLL_NICKS.update(troll_nicks)
                    logger.info(f"Troll system loaded {len(troll_nicks)} target(s): {', '.join(troll_nicks)}")

                while self._running and self.ipc_reader:
                    data = await self.ipc_reader.readline()
                    if not data:
                        break
                    line_str = data.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue
                    try:
                        payload = json.loads(line_str)
                        msg_type = payload.get("type")
                        if msg_type == "irc_line":
                            self._handle_irc_event(
                                payload.get("network", self.primary_network),
                                payload.get("bot_nick", "minibots"),
                                payload.get("line", "")
                            )
                        elif msg_type == "nick_update":
                            self.manager.update_bot_nick(
                                payload.get("network", self.primary_network),
                                payload.get("old_nick", ""),
                                payload.get("new_nick", "")
                            )
                    except Exception as e:
                        logger.error(f"Error handling IPC event: {e}")

            except (asyncio.CancelledError, KeyboardInterrupt):
                break
            except Exception as e:
                logger.error(f"IPC error: {e}. Reconnecting in 3s...")
                await asyncio.sleep(3.0)

    def _handle_irc_event(self, network: str, bot_nick: str, raw_line: str) -> None:
        """Parses raw IRC line, routes to bots and dispatcher."""
        # Handle NICK changes for troll tracking (follows targets across renames)
        if " NICK " in raw_line and " PRIVMSG " not in raw_line:
            try:
                parts = raw_line.split()
                if len(parts) >= 3 and parts[1] == "NICK":
                    old_nick = raw_line.lstrip(":").split("!")[0]
                    new_nick = parts[2].lstrip(":")
                    try:
                        from sideload.dai import on_nick_change
                    except ImportError:
                        from minibots.sideload.dai import on_nick_change
                    on_nick_change(old_nick, new_nick)
            except Exception:
                pass
            return

        if " PRIVMSG " not in raw_line:
            return

        try:
            prefix, trailing = raw_line.split(" PRIVMSG ", 1)
            target, message = trailing.split(" :", 1)
            prefix_clean = prefix.lstrip(":")
            sender_nick = prefix_clean.split("!")[0]
            hostmask = prefix_clean.split("!", 1)[1] if "!" in prefix_clean else ""
            is_trusted_net = self.is_trusted_network(network)

            # 1. Check multi-line command editing session
            sess = self.command_editor.get_session(sender_nick)
            if sess and not message.strip().startswith("!cmd"):
                if is_public_channel(network, target):
                    return
                self.command_editor.append_session_line(sender_nick, message)
                return

            # 2. Dispatch to spawned minibots
            self.manager.dispatch_privmsg(network, target, sender_nick, message, hostmask=hostmask)

            # 3. Check primary command dispatcher (only for core daemon bots)
            active_nicks = {n.lower() for n in self.manager.active_bots.keys()}
            if bot_nick.lower() in active_nicks:
                return

            parsed = self.dispatcher.parse_line(bot_nick, target, sender_nick, message)
            if parsed:
                cmd_name, args, meta = parsed
                start_ts = time.time()
                is_admin = self.dispatcher.is_admin(sender_nick)

                # Ring 0 / Admin Security Guard:
                is_admin_cmd = meta.get("min_ring", 1) == 0 or meta.get("category") == "admin"
                is_pub = is_public_channel(network, target)

                if is_admin_cmd:
                    if is_pub:
                        self.send_privmsg(network, target, f"[{cmd_name.upper()}] Admin commands cannot be used in public channels.")
                        activity_buffer.record(network, target, sender_nick, f"!{cmd_name} {' '.join(args)}", "DENIED_PUBLIC_CHANNEL")
                        return
                    if not is_trusted_net:
                        self.send_privmsg(network, target, f"[{cmd_name.upper()}] Ring 0 commands restricted to trusted admin networks.")
                        activity_buffer.record(network, target, sender_nick, f"!{cmd_name} {' '.join(args)}", "DENIED_NETWORK")
                        return
                    if not is_admin:
                        self.send_privmsg(network, target, f"[{cmd_name.upper()}] Permission denied: Ring 0 Admin required.")
                        activity_buffer.record(network, target, sender_nick, f"!{cmd_name} {' '.join(args)}", "DENIED_AUTH")
                        return

                handler = meta["handler"]
                handler(network, target, sender_nick, args)
                dur_ms = (time.time() - start_ts) * 1000.0
                activity_buffer.record(network, target, sender_nick, f"!{cmd_name} {' '.join(args)}", "OK", duration_ms=dur_ms)

        except Exception as e:
            logger.error(f"Failed to parse IRC line: {raw_line} ({e})")

    async def _broadcast_startup_changelog(self, force_show: bool = False) -> None:
        """Broadcasts git changelog delta on startup/reload to admin channels."""
        if not force_show and not self.config.data.get("BROADCAST_GIT_CHANGELOG", False):
            return
        await asyncio.sleep(2.0)
        repo_dir = Path(__file__).resolve().parent
        state_file = repo_dir / "data" / "last_broadcasted_commit.txt"
        state_file.parent.mkdir(parents=True, exist_ok=True)

        last_commit = ""
        if state_file.exists():
            try:
                last_commit = state_file.read_text().strip()
            except Exception:
                pass

        current_hash = ""
        commits = []
        try:
            import subprocess
            res_h = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3)
            if res_h.returncode == 0:
                current_hash = res_h.stdout.strip()
        except Exception:
            pass

        if not current_hash:
            c_info = repo_dir / "commit_info.txt"
            if c_info.exists():
                try:
                    c_lines = [l.strip() for l in c_info.read_text().splitlines() if l.strip()]
                    if c_lines:
                        current_hash = c_lines[0].split(" - ", 1)[0]
                        if not commits:
                            commits = [l.split(" - ", 1)[-1] for l in c_lines[:4] if not l.split(" - ", 1)[-1].startswith("Merge ")]
                except Exception:
                    pass

        if not current_hash or (not force_show and last_commit == current_hash):
            return

        git_args = ["git", "-C", str(repo_dir), "log"]
        if last_commit and last_commit != current_hash and not force_show:
            git_args.append(f"{last_commit}..HEAD")
        else:
            git_args.extend(["-n", "4"])
        git_args.extend(["--format=%h - %s"])

        try:
            res_l = subprocess.run(git_args, capture_output=True, text=True, timeout=3)
            if res_l.returncode == 0 and res_l.stdout.strip():
                raw = [l.strip().split(" - ", 1)[-1] for l in res_l.stdout.strip().splitlines() if l.strip()]
                commits = [c for c in raw if not c.startswith("Merge ")][:4]
        except Exception:
            pass

        if not commits:
            commits = ["Minibots maintenance and performance improvements"]

        total_cnt = len(commits)
        label = f"{total_cnt} improvement{'s' if total_cnt != 1 else ''}"
        lines = [
            f"[\x02\x0310MINIBOTS::RELEASE\x0f] \x02New Build Deployed\x0f (\x02\x0303{current_hash}\x0f | \x0311{label}\x0f):"
        ]
        for c in commits:
            lines.append(f"  \x0303•\x0f {c[:160]}")

        target_chans = self.admin_channels or ["#minibots", "#minibot"]
        for ch in target_chans:
            for l in lines:
                self.send_privmsg(self.primary_network, ch, l)
                await asyncio.sleep(0.15)

        try:
            state_file.write_text(current_hash)
        except Exception:
            pass

    def send_privmsg(self, network: str, target: str, message: str, bot_nick: Optional[str] = None) -> None:
        """Sends PRIVMSG via IPC broker (capped at 380 bytes)."""
        if not bot_nick:
            bot_nick = self.config.data.get("SERVERS", {}).get(network, {}).get("nick", "minibots")
        self._send_raw_to_ipc(bot_nick, f"PRIVMSG {target} :{message[:380]}", network=network)

    def send_notice(self, network: str, target: str, message: str, bot_nick: Optional[str] = None) -> None:
        """Sends NOTICE via IPC broker."""
        if not bot_nick:
            bot_nick = self.config.data.get("SERVERS", {}).get(network, {}).get("nick", "minibots")
        self._send_raw_to_ipc(bot_nick, f"NOTICE {target} :{message[:380]}", network=network)

    # ── Command Handlers ────────────────────────────────────────────────────────
    def _cmd_builder(self, network: str, target: str, sender: str, args: List[str]) -> None:
        """Interactive bot builder walkthrough and framework scaffolding assistant."""
        sub = args[0].lower().strip() if args else ""
        if sub in ("framework", "frameworks", "shims"):
            lines = [
                "📦 [Supported Framework Shims] Write or drop in community plugins from 5 bot frameworks:",
                "• Sopel: @plugin.command('hello') | @plugin.rule(r'regex') | def my_cmd(bot, trigger): bot.say('hi')",
                "• Errbot: @botcmd | @arg_botcmd('--name', dest='name') | class MyPlugin(BotPlugin): def cmd(self, msg, args): ...",
                "• Discord.py: @bot.command(name='ping') | async def ping(ctx): await ctx.send('pong')",
                "• Limnoria/Supybot: class MyPlugin(callbacks.Plugin): def mycmd(self, irc, msg, args): ...",
                "• Jaraco IRC: class MyBot(SingleServerIRCBot): def on_pubmsg(self, conn, event): ...",
                "💡 Scaffold any of these instantly: !tool scaffold <framework> <name>"
            ]
        elif sub in ("example", "examples", "recipes"):
            lines = [
                "🛠️ [Bot Builder Recipes & Examples]",
                "1. Fast Macro: !cmd add shout [ANNOUNCEMENT] {args} (by {nick}) -> Run with !shout Deploy complete!",
                "2. Python Tool: !cmd py cube: return int(args[0])**3 -> Run with !cube 5",
                "3. Multi-Line Code: !cmd begin greeter -> type code -> !cmd save -> Run with !greeter",
                "4. Pastebin Import: !cmd paste a1b2c3d myplugin -> Run with !myplugin",
                "5. Spawn Combo Bot: !spawn secbot #security toolbot,urls,sideload_myplugin"
            ]
        elif sub in ("editor", "edit"):
            lines = [
                "📝 [In-Channel Multi-Line Code Editor]",
                "1. Start buffer: !cmd begin <name>",
                "2. Type Python code directly into the channel line-by-line",
                "   Example: def execute(args, sender, channel):",
                "   Example:     return f'Hello {sender}, arguments: {args}'",
                "3. Save and compile: !cmd save (AST pre-flight checks for safety)",
                "4. Run immediately in any channel: !<name> <args> (or !cmd cancel to abort)"
            ]
        else:
            lines = [
                "🏗️ [Minibot Framework Builder Walkthrough] Assemble custom bots directly via IRC commands:",
                "1️⃣ Scaffold from Framework: !tool scaffold <sopel|errbot|discord|limnoria|ircbot> <name>",
                "2️⃣ Import Community Script: !tool import <pastebin_or_raw_url> [name]",
                "3️⃣ Create Custom Command: !cmd py <name> <code> OR !cmd begin <name> ... !cmd save",
                "4️⃣ Spawn Multi-Framework Bot: !spawn <nick> <#channel> toolbot,sideload_<name>",
                "💡 Details: !builder frameworks | !builder examples | !builder editor | !help builder"
            ]

        for l in lines:
            self.send_privmsg(network, target, l)

    def _cmd_help(self, network: str, target: str, sender: str, args: List[str]) -> None:
        is_admin = self.dispatcher.is_admin(sender)
        is_trusted_net = self.is_trusted_network(network)

        if args:
            topic = args[0].strip()
            topic_lines = self.dispatcher.get_topic_help(topic, is_admin=is_admin)
            for l in topic_lines[:4]:
                self.send_privmsg(network, target, l)
            return

        lines = self.dispatcher.get_help_summary(is_admin=is_admin, is_wireguard=is_trusted_net)

        if is_trusted_net:
            for l in lines[:4]:
                self.send_privmsg(network, target, l)
        else:
            # Send compact 1-line in channel, full menu via NOTICE to avoid flood kicks
            self.send_privmsg(network, target, lines[0])
            for l in self.dispatcher.get_help_summary(is_admin=is_admin, is_wireguard=True)[:4]:
                self.send_notice(network, sender, l)

    def _cmd_ping(self, network: str, target: str, sender: str, args: List[str]) -> None:
        self.send_privmsg(network, target, f"PONG 🏓 [{network}]")

    def _cmd_status(self, network: str, target: str, sender: str, args: List[str]) -> None:
        uptime_sec = int(time.time() - self.start_time)
        hrs, rem = divmod(uptime_sec, 3600)
        mins, secs = divmod(rem, 60)
        bot_count = len(self.manager.active_bots)
        stats = self.telemetry.get_summary(hours=24)
        self.send_privmsg(
            network, target,
            f"⚡ [Minibots Status] Uptime: {hrs}h {mins}m {secs}s | Active Bots: {bot_count} | 24h Telemetry: {stats['requests']} calls ({stats['tokens_in']}in/{stats['tokens_out']}out) | Latency: {stats['avg_latency_ms']}ms"
        )

    def _cmd_list(self, network: str, target: str, sender: str, args: List[str]) -> None:
        active = list(self.manager.active_bots.values())
        if args:
            net_filter = args[0].lower()
            filtered = [b for b in active if b.network.lower() == net_filter]
            if not filtered:
                servers_str = ", ".join(self.config.servers.keys())
                self.send_privmsg(network, target, f"[Minibots] No active minibots on network '{net_filter}'. (Active networks: {servers_str})")
                return
            active = filtered

        if not active:
            servers_str = ", ".join(self.config.servers.keys())
            self.send_privmsg(network, target, f"[Minibots] No active minibots. (Configured networks: {servers_str})")
            return
        summary = [
            f"{b.nick} on {b.network} ({', '.join(b.channels)}) [mods: {', '.join(b.active_modules) if b.active_modules else 'none'}]"
            for b in active
        ]
        self.send_privmsg(network, target, f"🤖 [Active Minibots ({len(active)})] {' | '.join(summary)}")

    def _cmd_spawn(self, network: str, target: str, sender: str, args: List[str]) -> None:
        if len(args) < 2:
            self.send_privmsg(network, target, "Usage: !spawn <nick> <#chan> [mods] OR !spawn <nick> <network> <#chan> [mods]")
            return
        if len(args) >= 3 and not args[1].startswith("#"):
            bot_nick = args[0]
            target_net = args[1].lower()
            bot_chan = args[2]
            mods = [m.strip() for m in args[3].split(",") if m.strip()] if len(args) >= 4 else []
        else:
            bot_nick = args[0]
            target_net = network
            bot_chan = args[1]
            mods = [m.strip() for m in args[2].split(",") if m.strip()] if len(args) >= 3 else []

        bot_chans = [c.strip() for c in bot_chan.split(",") if c.strip()]
        self.manager.spawn_bot(bot_nick, target_net, bot_chans, mods, creator_nick=sender)
        mods_str = f" with modules: {', '.join(mods)}" if mods else ""
        self.send_privmsg(network, target, f"✨ [Minibots] Spawned bot '{bot_nick}' on {target_net} in {', '.join(bot_chans)}{mods_str}")

    def _cmd_dpop(self, network: str, target: str, sender: str, args: List[str]) -> None:
        """Ring 0 administrative handler for DPoP sanctuary pass requests."""
        res = self.dpop_plugin.handle_admin_cmd(network, target, sender, args)
        for line in res.splitlines():
            if line.strip():
                self.send_privmsg(network, target, line.strip())

    def _cmd_server(self, network: str, target: str, sender: str, args: List[str]) -> None:
        """Dynamic IRC network management and registration."""
        if not args or args[0].lower() in ("list", "ls"):
            srvs = self.config.servers
            lines = [f"[Server] Registered IRC Networks ({len(srvs)}):"]
            for name, data in srvs.items():
                lines.append(f"  • {name}: {data.get('host')}:{data.get('port', 6667)} (ssl={data.get('ssl', False)})")
            for l in lines:
                self.send_privmsg(network, target, l)
            return

        if args[0].lower() == "add":
            if len(args) < 3:
                self.send_privmsg(network, target, "Usage: !server add <network_name> <host[:port]> [ssl]")
                return
            net_name = args[1].lower()
            host_port = args[2]
            use_ssl = len(args) >= 4 and args[3].lower() in ("ssl", "true", "1", "yes")

            if ":" in host_port:
                host, port_s = host_port.split(":", 1)
                try:
                    port = int(port_s)
                except ValueError:
                    port = 6697 if use_ssl else 6667
            else:
                host = host_port
                port = 6697 if use_ssl else 6667

            self.config.servers[net_name] = {
                "host": host,
                "port": port,
                "ssl": use_ssl
            }
            self._send_event_to_ipc({
                "type": "add_server",
                "network": net_name,
                "host": host,
                "port": port,
                "ssl": use_ssl
            })
            self.send_privmsg(network, target, f"✅ [Server] Registered network '{net_name}' -> {host}:{port} (SSL: {use_ssl})")
            return

        self.send_privmsg(network, target, "Usage: !server <list|add> [args...]")

    def _cmd_kill(self, network: str, target: str, sender: str, args: List[str]) -> None:
        if not args:
            self.send_privmsg(network, target, "Usage: !kill <nick> [network]")
            return
        bot_nick = args[0]
        explicit_net = args[1] if len(args) > 1 else None

        killed = self.manager.kill_bot(bot_nick, creator=sender)
        if killed:
            self.send_privmsg(network, target, f"🛑 [Minibots] Terminated bot '{bot_nick}'")
        else:
            # Force-kill orphaned socket on connection broker across configured networks
            nets = [explicit_net] if explicit_net else list(self.config.servers.keys())
            for net in nets:
                self._send_event_to_ipc({
                    "type": "kill_conn",
                    "network": net,
                    "nick": bot_nick,
                    "creator": sender
                })
            self.send_privmsg(network, target, f"🛑 [Minibots] Force-killed socket for '{bot_nick}' across connection broker (networks: {', '.join(nets)}).")

    def _cmd_attach(self, network: str, target: str, sender: str, args: List[str]) -> None:
        if len(args) < 2:
            self.send_privmsg(network, target, "Usage: !attach <nick> <module>")
            return
        nick, mod = args[0], args[1]
        if nick not in self.manager.active_bots:
            self.send_privmsg(network, target, f"❌ [Minibots] Bot '{nick}' not found in active bots. Use !list to view running bots.")
            return

        if self.manager.attach_module(nick, mod):
            self.send_privmsg(network, target, f"🧩 [Minibots] Attached '{mod}' to bot '{nick}'.")
        else:
            avail = self.manager.sideload_engine.list_available_scripts()
            avail_str = ", ".join(s.replace(".py", "") for s in avail) if avail else "none"
            self.send_privmsg(network, target, f"❌ [Minibots] Failed to attach '{mod}' to '{nick}'. Unknown module or script not found. Available sideload scripts: {avail_str} | Native: urls, toolbot, trollbot, julesbot")

    def _cmd_detach(self, network: str, target: str, sender: str, args: List[str]) -> None:
        if len(args) < 2:
            self.send_privmsg(network, target, "Usage: !detach <nick> <module>")
            return
        nick, mod = args[0], args[1]
        if self.manager.detach_module(nick, mod):
            self.send_privmsg(network, target, f"🔌 [Minibots] Detached '{mod}' from bot '{nick}'.")
        else:
            self.send_privmsg(network, target, f"❌ [Minibots] Bot '{nick}' not found.")

    def _cmd_reload(self, network: str, target: str, sender: str, args: List[str]) -> None:
        target_mod = args[0] if args else None
        reloaded = self.manager.reload_all_modules(target_mod=target_mod)
        self.send_privmsg(network, target, f"🔄 [Minibots] Hot-reload complete. Modules updated: {', '.join(reloaded) if reloaded else 'none'}")

    def send_msg(self, target_or_net: str, message_or_target: str, message: Optional[str] = None) -> None:
        """Sends PRIVMSG supporting both (target, msg) and (network, target, msg) call signatures."""
        if message is not None:
            self.send_privmsg(target_or_net, message_or_target, message)
        else:
            self.send_privmsg(self.primary_network, target_or_net, message_or_target)

    def _register_sideload_commands(self, mod: Any, mod_stem: str) -> List[str]:
        """Registers discovered framework commands on logic daemon dispatcher."""
        try:
            from adapters.sopel_shim import SopelBotWrapper, SopelTrigger
            from adapters.discord_shim import Context
        except ImportError:
            from minibots.adapters.sopel_shim import SopelBotWrapper, SopelTrigger
            from minibots.adapters.discord_shim import Context
        cmds_registered = []

        for attr_name in dir(mod):
            func = getattr(mod, attr_name)
            if callable(func):
                # 1. Sopel commands
                sopel_cmds = getattr(func, "_sopel_commands", [])
                for sc in sopel_cmds:
                    def _make_sopel_handler(f, cmd_label):
                        def _handler(net, tgt, sender, args):
                            wrapper = SopelBotWrapper(self, tgt, sender)
                            raw_line = f"!{cmd_label} {' '.join(args)}".strip()
                            trigger = SopelTrigger("minibots", tgt, sender, f"{sender}!user@host", raw_line)
                            f(wrapper, trigger)
                        return _handler
                    self.dispatcher.register(sc, _make_sopel_handler(func, sc), min_ring=1, help_text=f"Sideloaded command from {mod_stem}", category="tools")
                    cmds_registered.append(sc)

                # 2. Discord.py commands
                discord_cmds = getattr(func, "_discord_commands", [])
                for dc in discord_cmds:
                    def _make_discord_handler(f, cmd_label):
                        def _handler(net, tgt, sender, args):
                            ctx = Context(self, tgt, sender, f"!{cmd_label} {' '.join(args)}".strip())
                            if asyncio.iscoroutinefunction(f):
                                asyncio.create_task(f(ctx, *args))
                            else:
                                f(ctx, *args)
                        return _handler
                    self.dispatcher.register(dc, _make_discord_handler(func, dc), min_ring=1, help_text=f"Sideloaded Discord command from {mod_stem}", category="tools")
                    cmds_registered.append(dc)

        return cmds_registered

    def _unregister_sideload_commands(self, mod_stem: str) -> None:
        """Removes sideloaded commands belonging to a module."""
        to_del = [k for k, v in self.dispatcher.commands.items() if f"from {mod_stem}" in v.get("help", "")]
        for k in to_del:
            self.dispatcher.commands.pop(k, None)

    def _cmd_sideload(self, network: str, target: str, sender: str, args: List[str]) -> None:
        sub = args[0].lower() if args else "list"
        if sub in ("load", "unload", "rm", "spawn", "reload"):
            if is_public_channel(network, target):
                self.send_privmsg(network, target, f"[SIDELOAD] Admin command '{sub}' cannot be used in public channels.")
                activity_buffer.record(network, target, sender, f"!sideload {' '.join(args)}", "DENIED_PUBLIC_CHANNEL")
                return
            if not self.dispatcher.is_admin(sender):
                self.send_privmsg(network, target, f"[SIDELOAD] Permission denied: Admin required for '{sub}'.")
                activity_buffer.record(network, target, sender, f"!sideload {' '.join(args)}", "DENIED_AUTH")
                return

        if sub == "list":
            scripts = self.manager.sideload_engine.list_available_scripts()
            self.send_privmsg(network, target, f"📦 [Sideload Scripts] {', '.join(scripts) if scripts else 'none found in sideload/'}")
        elif sub == "reload":
            target_mod = args[1] if len(args) > 1 else None
            reloaded = self.manager.reload_all_modules(target_mod=target_mod)
            self.send_privmsg(network, target, f"🔄 [Sideload Reload] Reloaded: {', '.join(reloaded) if reloaded else 'none'}")
        elif sub in ("info", "help") and len(args) > 1:
            script_name = args[1]
            help_lines = self.manager.sideload_engine.get_module_help(f"sideload_{Path(script_name).stem}")
            for hl in help_lines:
                self.send_privmsg(network, target, hl)
        elif sub == "load" and len(args) > 1:
            raw_name = args[1].strip()
            script_file = raw_name if raw_name.endswith(".py") else f"{raw_name}.py"
            target_bot = args[2] if len(args) > 2 else None
            mod = self.manager.sideload_engine.load_script(script_file)
            if not mod:
                self.send_privmsg(network, target, f"❌ Failed to load '{script_file}'. File not found or AST syntax error.")
                return
            mod_stem = Path(script_file).stem
            mod_key = f"sideload_{mod_stem}"

            attached_nicks = []
            if target_bot:
                if self.manager.attach_module(target_bot, mod_key):
                    attached_nicks.append(target_bot)
                else:
                    self.send_privmsg(network, target, f"⚠️ Bot '{target_bot}' not found. Loading into Minibots dispatcher.")
            else:
                for b_nick in list(self.manager.active_bots.keys()):
                    if self.manager.attach_module(b_nick, mod_key):
                        attached_nicks.append(b_nick)

            registered_cmds = self._register_sideload_commands(mod, mod_stem)
            attach_msg = f" (attached to {', '.join(attached_nicks)})" if attached_nicks else ""
            cmds_msg = f" | Commands: {', '.join(f'!{c}' for c in registered_cmds)}" if registered_cmds else ""
            self.send_privmsg(network, target, f"✅ [Sideload] Loaded '{script_file}' as '{mod_key}'{attach_msg}{cmds_msg}")

        elif sub in ("unload", "rm") and len(args) > 1:
            raw_name = args[1].strip()
            mod_stem = Path(raw_name).stem.replace("sideload_", "")
            mod_key = f"sideload_{mod_stem}"
            detached_nicks = []
            for b_nick in list(self.manager.active_bots.keys()):
                if self.manager.detach_module(b_nick, mod_key):
                    detached_nicks.append(b_nick)
            self._unregister_sideload_commands(mod_stem)
            self.manager.sideload_engine.loaded_modules.pop(mod_key, None)
            self.send_privmsg(network, target, f"🗑️ [Sideload] Unloaded '{mod_key}' from {len(detached_nicks)} bot(s) and dispatcher.")

        elif sub == "spawn" and len(args) >= 3:
            script_name, bot_nick, chan = args[1], args[2], args[3] if len(args) > 3 else "#minibots"
            mod = self.manager.sideload_engine.load_script(script_name)
            if mod:
                self.manager.spawn_bot(bot_nick, [chan], [f"sideload_{Path(script_name).stem}"])
                self.send_privmsg(network, target, f"🚀 [Sideload Spawn] Spawned bot '{bot_nick}' in {chan} with '{script_name}'.")
            else:
                self.send_privmsg(network, target, f"❌ Failed to load script '{script_name}'. Check AST errors.")
        else:
            self.send_privmsg(network, target, "Usage: !sideload list | !sideload load <script> [bot] | !sideload unload <script> | !sideload info <script> | !sideload spawn <script> <nick> <chan>")

    def _cmd_kaggle(self, network: str, target: str, sender: str, args: List[str]) -> None:
        asyncio.create_task(self.kaggle_plugin.handle_command(target, sender, args))

    def _cmd_task(self, network: str, target: str, sender: str, args: List[str]) -> None:
        prompt = " ".join(args)
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self.task_plugin.execute_task, network, target, sender, prompt)
        except RuntimeError:
            self.task_plugin.execute_task(network, target, sender, prompt)

    def _cmd_models(self, network: str, target: str, sender: str, args: List[str]) -> None:
        self._cmd_model(network, target, sender, ["list"])

    def _cmd_model(self, network: str, target: str, sender: str, args: List[str]) -> None:
        """Inspects or switches active LLM models on Raspberry Pi 5 worker and toggles auto-switching."""
        from sideload.dai import (
            format_model_list,
            get_active_model_info,
            switch_pi_model,
            set_auto_switch,
            get_auto_switch,
            MODEL_PRESETS,
            PRESET_LOOKUP,
            C_TEAL,
            C_BOLD,
            C_RESET,
            C_RED,
        )

        is_pm = not target.startswith("#")

        # 1. Listing models (.models or .model list or .model with no args)
        if not args or args[0].lower() in ("list", "show", "status", "info"):
            async def _show():
                active_alias, _ = await get_active_model_info()
                lines = format_model_list(active_alias, is_pm=is_pm)
                for l in lines:
                    self.send_privmsg(network, target, l)
                    await asyncio.sleep(0.15)
            _safe_create_task(_show())
            return

        subcmd = args[0].lower()

        # 2. Auto-switch toggle (.model auto [on|off])
        if subcmd == "auto":
            if len(args) < 2:
                status = "ON" if get_auto_switch() else "OFF"
                self.send_privmsg(
                    network, target,
                    f"🧠 {C_TEAL}{C_BOLD}[DAI·Model]{C_RESET} Auto-switch is currently {C_BOLD}{status}{C_RESET}. Use .model auto on|off to toggle."
                )
                return

            is_trusted_net = self.is_trusted_network(network)
            admin_channels = {c.lower() for c in self.admin_channels}
            is_admin_chan = target.lower() in admin_channels
            is_admin = self.dispatcher.is_admin(sender)

            if not is_admin:
                self.send_privmsg(
                    network, target,
                    f"🧠 {C_RED}{C_BOLD}[DAI·Auth]{C_RESET} {sender}: Permission denied. Model switching is restricted to bot admins."
                )
                return

            if not is_trusted_net and not is_admin_chan:
                self.send_privmsg(
                    network, target,
                    f"🧠 {C_RED}{C_BOLD}[DAI·Auth]{C_RESET} {sender}: Permission denied. Model switching is restricted to the internal WireGuard network or admin channels (#minibots)."
                )
                return

            toggle = args[1].lower()
            if toggle in ("1", "on", "enable", "true"):
                set_auto_switch(True)
                self.send_privmsg(
                    network, target,
                    f"🧠 {C_TEAL}{C_BOLD}[DAI·Model]{C_RESET} Auto-switch enabled. Queries will dynamically route between {C_BOLD}llama{C_RESET} (fast) and {C_BOLD}lfm{C_RESET} (reasoning)."
                )
            elif toggle in ("0", "off", "disable", "false"):
                set_auto_switch(False)
                self.send_privmsg(network, target, f"🧠 {C_TEAL}{C_BOLD}[DAI·Model]{C_RESET} Auto-switch disabled.")
            else:
                self.send_privmsg(network, target, f"🧠 {C_TEAL}{C_BOLD}[DAI·Model]{C_RESET} Usage: .model auto on|off")
            return

        # 3. Model switch command (.model <name>)
        target_key = subcmd
        is_trusted_net = self.is_trusted_network(network)
        admin_channels = {c.lower() for c in self.admin_channels}
        is_admin_chan = target.lower() in admin_channels
        is_admin = self.dispatcher.is_admin(sender)

        if not is_admin:
            self.send_privmsg(
                network, target,
                f"🧠 {C_RED}{C_BOLD}[DAI·Auth]{C_RESET} {sender}: Permission denied. Model switching is restricted to bot admins."
            )
            return

        if not is_trusted_net and not is_admin_chan:
            self.send_privmsg(
                network, target,
                f"🧠 {C_RED}{C_BOLD}[DAI·Auth]{C_RESET} {sender}: Permission denied. Model switching is restricted to the internal WireGuard network or admin channels (#minibots)."
            )
            return

        async def _do_switch():
            canonical = PRESET_LOOKUP.get(target_key)
            if not canonical:
                avail = ", ".join(MODEL_PRESETS.keys())
                self.send_privmsg(
                    network, target,
                    f"🧠 {C_RED}{C_BOLD}[DAI·Error]{C_RESET} Unknown model preset '{target_key}'. Available: {avail}"
                )
                return

            preset = MODEL_PRESETS[canonical]
            from sideload import dai
            if getattr(dai, "MODEL_SWITCH_LOCK", None) and dai.MODEL_SWITCH_LOCK.locked():
                loading_desc = getattr(dai, "_CURRENTLY_SWITCHING", None) or "another model"
                self.send_privmsg(
                    network, target,
                    f"🧠 {C_TEAL}{C_BOLD}[DAI·Model]{C_RESET} A model switch is currently in progress (loading {loading_desc}). Please wait for it to finish."
                )
                return

            cur_alias, _ = await get_active_model_info()
            if cur_alias == preset["alias"]:
                self.send_privmsg(
                    network, target,
                    f"🧠 {C_TEAL}{C_BOLD}[DAI·Model]{C_RESET} Model {preset['name']} ({preset['alias']}) is already active."
                )
                return

            self.send_privmsg(
                network, target,
                f"🧠 {C_TEAL}{C_BOLD}[DAI·Model]{C_RESET} Switching Pi 5 worker to {preset['name']}... please wait."
            )
            def _prog(msg: str):
                self.send_privmsg(network, target, f"🧠 {C_TEAL}{C_BOLD}[DAI·Model]{C_RESET} {msg}")

            success, msg = await switch_pi_model(canonical, progress_callback=_prog)
            if success:
                self.send_privmsg(network, target, f"🧠 {C_TEAL}{C_BOLD}[DAI·Model]{C_RESET} {msg}")
            else:
                self.send_privmsg(network, target, f"🧠 {C_RED}{C_BOLD}[DAI·Error]{C_RESET} {msg}")

        _safe_create_task(_do_switch())

    def _cmd_air(self, network: str, target: str, sender: str, args: List[str]) -> None:
        self._cmd_ai(network, target, sender, args, force_reasoning=True)

    def _cmd_ai(self, network: str, target: str, sender: str, args: List[str], force_reasoning: bool = False) -> None:
        """Pure local LLM chat query without web search or citations."""
        from sideload.dai import run_pipeline, C_TEAL, C_BOLD, C_RESET

        query = " ".join(args).strip()
        if not query:
            badge = f"🧠 {C_TEAL}{C_BOLD}[{'AI·Reason' if force_reasoning else 'AI'}]{C_RESET}"
            self.send_privmsg(network, target, f"{badge} {sender}: Usage: .ai [-r] <prompt>")
            return

        proxy = _DaemonBotProxy(self, network)
        _safe_create_task(run_pipeline(proxy, target, sender, query, force_reasoning=force_reasoning, enable_web_search=False))

    def _cmd_dair(self, network: str, target: str, sender: str, args: List[str]) -> None:
        self._cmd_dai(network, target, sender, args, force_reasoning=True)

    def _cmd_dai(self, network: str, target: str, sender: str, args: List[str], force_reasoning: bool = False) -> None:
        """AI search and conditioned generation with DuckDuckGo retrieval."""
        from sideload.dai import run_pipeline, C_TEAL, C_BOLD, C_RESET

        query = " ".join(args).strip()
        if not query:
            badge = f"🧠 {C_TEAL}{C_BOLD}[{'DAI·Reason' if force_reasoning else 'DAI'}]{C_RESET}"
            self.send_privmsg(network, target, f"{badge} {sender}: Usage: .dai [-r] <query> or .dair <query>")
            return

        proxy = _DaemonBotProxy(self, network)
        _safe_create_task(run_pipeline(proxy, target, sender, query, force_reasoning=force_reasoning, enable_web_search=True))

    def _cmd_granite(self, network: str, target: str, sender: str, args: List[str]) -> None:
        """IBM Granite CTI reasoning and threat analysis."""
        yield_channels = [c.lower() for c in self.config.data.get("CTI", {}).get("YIELD_CHANNELS", [])]
        if target.lower() in yield_channels:
            return

        for bot in self.manager.active_bots.values():
            if target.lower() in [c.lower() for c in bot.channels]:
                if any("lertz_threat_intel" in mod or "granite" in mod for mod in bot.enabled_modules):
                    return

        from sideload.lertz_threat_intel import handle_granite_command

        def _send(tgt: str, msg: str) -> None:
            self.send_privmsg(network, tgt, msg)

        is_admin = self.dispatcher.is_admin(sender)
        _safe_create_task(handle_granite_command(_send, target, sender, args, is_admin=is_admin))

    def _cmd_ignore(self, network: str, target: str, sender: str, args: List[str]) -> None:
        """Dynamic URL preview ignore engine control."""
        from core.ignore_manager import get_ignore_manager
        mgr = get_ignore_manager()

        if not args:
            ignored = mgr.list_ignored()
            ignored_str = ", ".join(ignored) if ignored else "none (default bot set active)"
            self.send_privmsg(network, target, f"🔇 [IGNORE] Active URL-ignored patterns: {ignored_str} (Usage: !ignore <add|del|list> <pattern>)")
            return

        action = args[0].lower().strip()
        sub_args = args[1:]

        if action in ("add", "+"):
            if not sub_args:
                self.send_privmsg(network, target, "Usage: !ignore add <nick_or_pattern>")
                return
            pat = sub_args[0].strip()
            ok = mgr.add(pat)
            if ok:
                self.send_privmsg(network, target, f"🔇 [IGNORE] Added '{pat}' to URL ignore filter.")
            else:
                self.send_privmsg(network, target, f"🔇 [IGNORE] Pattern '{pat}' is already ignored.")
        elif action in ("del", "rm", "remove", "-"):
            if not sub_args:
                self.send_privmsg(network, target, "Usage: !ignore del <nick_or_pattern>")
                return
            pat = sub_args[0].strip()
            ok = mgr.remove(pat)
            if ok:
                self.send_privmsg(network, target, f"🔇 [IGNORE] Removed '{pat}' from URL ignore filter.")
            else:
                self.send_privmsg(network, target, f"🔇 [IGNORE] Pattern '{pat}' was not found in ignore filter.")
        elif action in ("list", "ls", "show"):
            ignored = mgr.list_ignored()
            ignored_str = ", ".join(ignored) if ignored else "none"
            self.send_privmsg(network, target, f"🔇 [IGNORE] Active URL-ignored patterns: {ignored_str}")
        else:
            # Shorthand: !ignore <nick> -> toggle or add
            pat = action
            if pat in mgr.list_ignored():
                mgr.remove(pat)
                self.send_privmsg(network, target, f"🔇 [IGNORE] Removed '{pat}' from URL ignore filter.")
            else:
                mgr.add(pat)
                self.send_privmsg(network, target, f"🔇 [IGNORE] Added '{pat}' to URL ignore filter.")

    def _cmd_wiki(self, network: str, target: str, sender: str, args: List[str]) -> None:
        query = " ".join(args).strip()
        if not query:
            self.send_privmsg(network, target, "Usage: !wiki <topic>")
            return
        try:
            from sideload.infobot import search_wikipedia
            self.send_privmsg(network, target, search_wikipedia(query))
        except Exception as e:
            self.send_privmsg(network, target, f"[Wikipedia Error] {e}")

    def _cmd_grok(self, network: str, target: str, sender: str, args: List[str]) -> None:
        query = " ".join(args).strip()
        if not query:
            self.send_privmsg(network, target, "Usage: !grok <topic>")
            return
        try:
            from sideload.infobot import search_grokipedia
            self.send_privmsg(network, target, search_grokipedia(query))
        except Exception as e:
            self.send_privmsg(network, target, f"[Grokipedia Error] {e}")

    def _cmd_calc(self, network: str, target: str, sender: str, args: List[str]) -> None:
        res = eval_calc_expression(" ".join(args))
        self.send_privmsg(network, target, f"🔢 [CALC] {res}")

    def _cmd_dns(self, network: str, target: str, sender: str, args: List[str]) -> None:
        if not args:
            self.send_privmsg(network, target, "Usage: !dns <hostname>")
            return
        res = resolve_dns(args[0])
        self.send_privmsg(network, target, f"🌐 [DNS] {res}")

    def _cmd_port(self, network: str, target: str, sender: str, args: List[str]) -> None:
        if not args:
            self.send_privmsg(network, target, "Usage: !port <host> [port]")
            return
        host = args[0]
        port = int(args[1]) if len(args) > 1 and args[1].isdigit() else 80
        res = check_port(host, port)
        self.send_privmsg(network, target, f"🔌 [PORT] {res}")

    def _cmd_mcp(self, network: str, target: str, sender: str, args: List[str]) -> None:
        if not args:
            self.send_privmsg(network, target, "Usage: !mcp <tool_name> [arguments...]")
            return
        tool_name = args[0]
        mcp_url = self.config.data.get("LLM", {}).get("MCP_SERVER_URL", os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8050/sse"))
        res = call_mcp_tool(mcp_url, tool_name, args[1:])
        self.send_privmsg(network, target, f"⚙️ [MCP:{tool_name}] {res}")

    def _cmd_tool(self, network: str, target: str, sender: str, args: list) -> None:
        if not args:
            self.send_privmsg(network, target, "🛠️ [Toolbot] Usage: !tool <dns|ping|calc|port|hash|http|json|cron|crypto|search|info|scaffold|import> [args...]")
            return
        sub = args[0].lower()
        if sub in ("search", "find") and len(args) > 1:
            self._cmd_tool_search(network, target, sender, args[1:])
        elif sub == "info" and len(args) > 1:
            self._cmd_tool_info(network, target, sender, args[1:])
        elif sub == "scaffold" and len(args) >= 3:
            if is_public_channel(network, target):
                self.send_privmsg(network, target, "[TOOLBOT] Admin command 'scaffold' cannot be used in public channels.")
                activity_buffer.record(network, target, sender, f"!tool {' '.join(args)}", "DENIED_PUBLIC_CHANNEL")
                return
            if not self.dispatcher.is_admin(sender):
                self.send_privmsg(network, target, "[TOOLBOT] Permission denied: Admin required.")
                activity_buffer.record(network, target, sender, f"!tool {' '.join(args)}", "DENIED_AUTH")
                return
            self._cmd_tool_scaffold(network, target, sender, args[1], args[2])
        elif sub in ("import", "fetch") and len(args) >= 2:
            if is_public_channel(network, target):
                self.send_privmsg(network, target, "[TOOLBOT] Admin command 'import' cannot be used in public channels.")
                activity_buffer.record(network, target, sender, f"!tool {' '.join(args)}", "DENIED_PUBLIC_CHANNEL")
                return
            if not self.dispatcher.is_admin(sender):
                self.send_privmsg(network, target, "[TOOLBOT] Permission denied: Admin required.")
                activity_buffer.record(network, target, sender, f"!tool {' '.join(args)}", "DENIED_AUTH")
                return
            c_name = args[2] if len(args) >= 3 else None
            self._cmd_tool_import(network, target, sender, args[1], c_name)
        elif sub == "dns" and len(args) > 1:
            self._cmd_dns(network, target, sender, args[1:])
        elif sub in ["ping", "port"] and len(args) > 1:
            self._cmd_port(network, target, sender, args[1:])
        elif sub == "calc" and len(args) > 1:
            self._cmd_calc(network, target, sender, args[1:])
        elif sub == "hash" and len(args) > 1:
            self._cmd_hash(network, target, sender, args[1:])
        elif sub == "http" and len(args) > 1:
            self._cmd_http(network, target, sender, args[1:])
        elif sub == "json" and len(args) > 1:
            self._cmd_json(network, target, sender, args[1:])
        elif sub == "cron" and len(args) > 1:
            self._cmd_cron(network, target, sender, args[1:])
        elif sub == "crypto" and len(args) > 1:
            self._cmd_crypto(network, target, sender, args[1:])
        else:
            self.send_privmsg(network, target, "🛠️ [Toolbot] Usage: !tool <dns|ping|calc|port|hash|http|json|cron|crypto|search|info|scaffold|import> [args...]")

    def _cmd_troll(self, network: str, target: str, sender: str, args: List[str]) -> None:
        if not args:
            presets = list(PERSONA_PRESETS.keys())
            self.send_privmsg(network, target, f"🎭 [Trollbot] Presets: {', '.join(presets)} | Usage: !troll set <preset> | !troll custom <prompt> | !troll reset")
            return
        sub = args[0].lower()
        if sub in ("set", "custom", "reset"):
            if is_public_channel(network, target):
                self.send_privmsg(network, target, f"[TROLLBOT] Admin command '{sub}' cannot be used in public channels.")
                activity_buffer.record(network, target, sender, f"!troll {' '.join(args)}", "DENIED_PUBLIC_CHANNEL")
                return
            if not self.dispatcher.is_admin(sender):
                self.send_privmsg(network, target, f"[TROLLBOT] Permission denied: Admin required for '{sub}'.")
                activity_buffer.record(network, target, sender, f"!troll {' '.join(args)}", "DENIED_AUTH")
                return

        if sub == "list":
            presets = list(PERSONA_PRESETS.keys())
            self.send_privmsg(network, target, f"🎭 [Trollbot Presets] {', '.join(presets)}")
        elif sub == "set" and len(args) > 1:
            preset = args[1].lower()
            if preset in PERSONA_PRESETS:
                # Update all active trollbot plugins
                for bot in self.manager.active_bots.values():
                    plugin = bot.active_modules.get("trollbot")
                    if plugin and hasattr(plugin, "set_preset"):
                        plugin.set_preset(preset)
                self.send_privmsg(network, target, f"🎭 [Trollbot] Applied persona preset: '{preset}'.")
            else:
                self.send_privmsg(network, target, f"❌ Unknown preset '{preset}'. Available: {', '.join(PERSONA_PRESETS.keys())}")
        elif sub == "custom" and len(args) > 1:
            custom_prompt = " ".join(args[1:])
            for bot in self.manager.active_bots.values():
                plugin = bot.active_modules.get("trollbot")
                if plugin and hasattr(plugin, "set_persona"):
                    plugin.set_persona(custom_prompt)
            self.send_privmsg(network, target, "🎭 [Trollbot] Applied custom system prompt.")
        elif sub == "reset":
            for bot in self.manager.active_bots.values():
                plugin = bot.active_modules.get("trollbot")
                if plugin and hasattr(plugin, "set_preset"):
                    plugin.set_preset("default")
            self.send_privmsg(network, target, "🎭 [Trollbot] Reset persona to default.")
        else:
            self.send_privmsg(network, target, "Usage: !troll [list|set <preset>|custom <prompt>|reset]")

    def _cmd_jules(self, network: str, target: str, sender: str, args: List[str]) -> None:
        self.send_privmsg(network, target, "🚀 [Jules] Direct coding tasks to #jules (e.g. 'julesbot: init <label> <repo> <prompt>' or '!jules help').")

    def _cmd_usage(self, network: str, target: str, sender: str, args: List[str]) -> None:
        stats = self.telemetry.get_summary(hours=24)
        self.send_privmsg(
            network, target,
            f"📊 [Telemetry 24h] Requests: {stats['requests']} | Tokens: {stats['tokens_in']} in / {stats['tokens_out']} out | Avg Latency: {stats['avg_latency_ms']}ms"
        )

    def _cmd_cleanup(self, network: str, target: str, sender: str, args: List[str]) -> None:
        days = int(args[0]) if args and args[0].isdigit() else 30
        deleted = self.telemetry.prune_older_than(days=days)
        self.send_privmsg(network, target, f"🧹 [Cleanup] Pruned {deleted} records older than {days} days from telemetry.")

    def _cmd_logs(self, network: str, target: str, sender: str, args: List[str]) -> None:
        count = int(args[0]) if args and args[0].isdigit() else 5
        logs = activity_buffer.tail(lines=count)
        if not logs:
            self.send_privmsg(network, target, "[Activity Logs] Buffer empty.")
            return
        self.send_privmsg(network, target, f"📜 [Activity Logs (last {len(logs)})]:")
        for l in logs:
            self.send_privmsg(network, target, f"  {l}")

    def _cmd_join(self, network: str, target: str, sender: str, args: List[str]) -> None:
        if not args:
            self.send_privmsg(network, target, "Usage: !join <#chan> or !join <bot_nick> <#chan>")
            return
        if len(args) >= 2 and not args[0].startswith("#"):
            bot_nick, chan = args[0], args[1]
            if bot_nick in self.manager.active_bots:
                ok = self.manager.bot_join(bot_nick, chan)
                if ok:
                    self.send_privmsg(network, target, f"🚪 [{bot_nick}] Joining channel {chan}")
                else:
                    self.send_privmsg(network, target, f"❌ Failed to join {chan} for bot '{bot_nick}'.")
            else:
                self.send_privmsg(network, target, f"❌ Bot '{bot_nick}' not found in active bots.")
            return

        for chan in args:
            if chan.startswith("#"):
                self._send_raw_to_ipc("minibots", f"JOIN {chan}", network=network)
                self.send_privmsg(network, target, f"🚪 [Minibots] Joining channel {chan}")
            else:
                self.send_privmsg(network, target, f"❌ Invalid channel '{chan}'. Channels must start with '#'.")

    def _cmd_part(self, network: str, target: str, sender: str, args: List[str]) -> None:
        if not args:
            chan = target
            self._send_raw_to_ipc("minibots", f"PART {chan} :Parted by {sender}", network=network)
            self.send_privmsg(network, target, f"🚪 [Minibots] Parted channel {chan}")
            return

        if len(args) >= 2 and not args[0].startswith("#"):
            bot_nick, chan = args[0], args[1]
            if bot_nick in self.manager.active_bots:
                ok = self.manager.bot_part(bot_nick, chan, reason=f"Parted by {sender}")
                if ok:
                    self.send_privmsg(network, target, f"🚪 [{bot_nick}] Parted channel {chan}")
                else:
                    self.send_privmsg(network, target, f"❌ Failed to part {chan} for bot '{bot_nick}'.")
            else:
                self.send_privmsg(network, target, f"❌ Bot '{bot_nick}' not found in active bots.")
            return

        chan = args[0]
        self._send_raw_to_ipc("minibots", f"PART {chan} :Parted by {sender}", network=network)
        self.send_privmsg(network, target, f"🚪 [Minibots] Parted channel {chan}")

    def _cmd_say(self, network: str, target: str, sender: str, args: List[str]) -> None:
        if len(args) < 2:
            self.send_privmsg(network, target, "Usage: !say <target> <message>")
            return
        dest, msg = args[0], " ".join(args[1:])
        self.send_privmsg(network, dest, msg)

    def _cmd_raw(self, network: str, target: str, sender: str, args: List[str]) -> None:
        if not args:
            self.send_privmsg(network, target, "Usage: !raw <raw_irc_line>")
            return
        raw_line = " ".join(args)
        self._send_raw_to_ipc("minibots", raw_line, network=network)
        self.send_privmsg(network, target, f"📡 [Minibots Raw] Sent: {raw_line}")

    def _cmd_minibot(self, network: str, target: str, sender: str, args: List[str]) -> None:
        if not args:
            self.send_privmsg(network, target, "Minibot Commands: spawn <nick> <chan> <mods> | list | kill <nick> | join <nick> <chan> | part <nick> <chan> | attach <nick> <mod> | detach <nick> <mod> | reload | sideload list | usage | logs")
            return
        sub = args[0].lower()
        if sub == "spawn":
            self._cmd_spawn(network, target, sender, args[1:])
        elif sub in ["list", "ls", "ps"]:
            self._cmd_list(network, target, sender, args[1:])
        elif sub in ["kill", "stop", "rm"]:
            self._cmd_kill(network, target, sender, args[1:])
        elif sub == "join":
            self._cmd_join(network, target, sender, args[1:])
        elif sub in ["part", "leave"]:
            self._cmd_part(network, target, sender, args[1:])
        elif sub == "attach":
            self._cmd_attach(network, target, sender, args[1:])
        elif sub == "detach":
            self._cmd_detach(network, target, sender, args[1:])
        elif sub == "reload":
            self._cmd_reload(network, target, sender, args[1:])
        elif sub == "sideload":
            self._cmd_sideload(network, target, sender, args[1:])
        elif sub in ["kaggle", "kg", "gpu"]:
            self._cmd_kaggle(network, target, sender, args[1:])
        elif sub in ["model", "models", "daimodel"]:
            self._cmd_model(network, target, sender, args[1:])
        elif sub in ["ai", "ask"]:
            self._cmd_ai(network, target, sender, args[1:])
        elif sub in ["air"]:
            self._cmd_air(network, target, sender, args[1:])
        elif sub in ["dai", "ddgai"]:
            self._cmd_dai(network, target, sender, args[1:])
        elif sub in ["dair", "diar"]:
            self._cmd_dair(network, target, sender, args[1:])
        elif sub in ["granite", "gr", "threat"]:
            self._cmd_granite(network, target, sender, args[1:])
        elif sub in ["ignore", "urlignore"]:
            self._cmd_ignore(network, target, sender, args[1:])
        elif sub in ["usage", "stats"]:
            self._cmd_usage(network, target, sender, args[1:])
        elif sub in ["logs", "tail"]:
            self._cmd_logs(network, target, sender, args[1:])
        else:
            self.send_privmsg(network, target, f"[Minibots] Unknown subcommand '{sub}'. Type !help for command list.")

    def reload_modules(self) -> None:
        """Triggered by SIGHUP for zero-disconnect hot-reloading."""
        logger.info("Executing SIGHUP hot-reload...")
        self.config.load()
        reloaded = self.manager.reload_all_modules()
        logger.info(f"SIGHUP reload complete in <20ms. Updated: {reloaded}")

    def _cmd_custom_command(self, network: str, target: str, sender: str, args: list) -> None:
        """Interactive custom command and macro editor."""
        if not args:
            self.send_privmsg(network, target, "📝 [Command Editor] Usage: !cmd <add|py|begin|save|cancel|paste|list|info|rm|test> [args...]")
            return

        sub = args[0].lower()
        if sub in ("add", "template", "py", "begin", "save", "cancel", "paste", "import", "rm", "delete"):
            if is_public_channel(network, target):
                self.send_privmsg(network, target, f"[CMD-EDITOR] Admin command '{sub}' cannot be used in public channels.")
                activity_buffer.record(network, target, sender, f"!cmd {' '.join(args)}", "DENIED_PUBLIC_CHANNEL")
                return
            if not self.dispatcher.is_admin(sender):
                self.send_privmsg(network, target, "[CMD-EDITOR] Permission denied: Admin required.")
                activity_buffer.record(network, target, sender, f"!cmd {' '.join(args)}", "DENIED_AUTH")
                return

        if sub in ("add", "template") and len(args) >= 3:
            name = args[1]
            tmpl = " ".join(args[2:])
            self.command_editor.add_template(name, tmpl, author=sender)
            self.send_privmsg(network, target, f"[\x02\x0303CMD-EDITOR\x0f] Template command '!{name.lower()}' registered successfully.")

        elif sub == "py" and len(args) >= 3:
            name = args[1]
            code = " ".join(args[2:])
            ok, msg = self.command_editor.add_python(name, code, author=sender)
            if ok:
                self.send_privmsg(network, target, f"[\x02\x0303CMD-EDITOR\x0f] Python command '!{name.lower()}' compiled & registered successfully.")
            else:
                self.send_privmsg(network, target, f"[\x02\x0304CMD-EDITOR\x0f] AST compilation error: {msg}")

        elif sub == "begin" and len(args) >= 2:
            name = args[1]
            self.command_editor.begin_session(sender, name)
            self.send_privmsg(network, target, f"[\x02\x0310CMD-EDITOR\x0f] Started multi-line buffer for '!{name.lower()}'. Send code lines or !cmd save to finish.")

        elif sub == "save":
            ok, msg = self.command_editor.save_session(sender)
            color = "\x0303" if ok else "\x0304"
            self.send_privmsg(network, target, f"[\x02{color}CMD-EDITOR\x0f] {msg}")

        elif sub == "cancel":
            self.command_editor.cancel_session(sender)
            self.send_privmsg(network, target, "[\x02\x0314CMD-EDITOR\x0f] Buffer session cancelled.")

        elif sub in ("paste", "import") and len(args) >= 3:
            paste_id = args[1]
            name = args[2]
            ok, msg = self.command_editor.import_from_paste(paste_id, name, author=sender)
            color = "\x0303" if ok else "\x0304"
            self.send_privmsg(network, target, f"[\x02{color}CMD-EDITOR\x0f] {msg}")

        elif sub in ("list", "ls"):
            cmds = self.command_editor.list_commands()
            if not cmds:
                self.send_privmsg(network, target, "[\x02\x0310CMD-EDITOR\x0f] No custom commands registered. Create one with: !cmd add <name> <template>")
                return
            c_strs = [f"!{c['name']} ({c['command_type']}, calls={c['call_count']})" for c in cmds]
            self.send_privmsg(network, target, f"[\x02\x0310CUSTOM COMMANDS\x0f] {' | '.join(c_strs)}")

        elif sub == "info" and len(args) >= 2:
            info = self.command_editor.get_command_info(args[1])
            if not info:
                self.send_privmsg(network, target, f"[\x02\x0304CMD-EDITOR\x0f] Command '!{args[1]}' not found.")
                return
            self.send_privmsg(network, target, f"[\x02\x0310CMD-INFO\x0f] !{info['name']} | Type: {info['command_type']} | Author: {info['author']} | Calls: {info['call_count']}")
            self.send_privmsg(network, target, f"  Content: {info['content'][:150]}")

        elif sub in ("rm", "delete") and len(args) >= 2:
            if not self.dispatcher.is_admin(sender):
                self.send_privmsg(network, target, "[\x02\x0304CMD-EDITOR\x0f] Permission denied.")
                return
            ok = self.command_editor.remove_command(args[1])
            if ok:
                self.send_privmsg(network, target, f"[\x02\x0303CMD-EDITOR\x0f] Command '!{args[1]}' removed.")
            else:
                self.send_privmsg(network, target, f"[\x02\x0304CMD-EDITOR\x0f] Command '!{args[1]}' not found.")

        elif sub == "test" and len(args) >= 2:
            res = self.command_editor.execute_command(args[1], args[2:], sender=sender, target=target)
            if res is not None:
                self.send_privmsg(network, target, f"[\x02\x0310CMD-TEST\x0f] {res}")
            else:
                self.send_privmsg(network, target, f"[\x02\x0304CMD-TEST\x0f] Command '!{args[1]}' not found.")

    def _cmd_hash(self, network: str, target: str, sender: str, args: list) -> None:
        from plugins.toolbot import compute_hash
        if len(args) < 2:
            self.send_privmsg(network, target, "Usage: !hash <md5|sha1|sha256|sha512|b64> <text>")
            return
        res = compute_hash(args[0], " ".join(args[1:]))
        self.send_privmsg(network, target, f"[HASH] {res}")

    def _cmd_http(self, network: str, target: str, sender: str, args: list) -> None:
        from plugins.toolbot import inspect_http
        if not args:
            self.send_privmsg(network, target, "Usage: !http <url>")
            return
        res = inspect_http(args[0])
        self.send_privmsg(network, target, f"[HTTP] {res}")

    def _cmd_json(self, network: str, target: str, sender: str, args: list) -> None:
        from plugins.toolbot import validate_json_text
        if not args:
            self.send_privmsg(network, target, "Usage: !json <json_string>")
            return
        res = validate_json_text(" ".join(args))
        self.send_privmsg(network, target, f"[JSON] {res}")

    def _cmd_cron(self, network: str, target: str, sender: str, args: list) -> None:
        from plugins.toolbot import explain_cron
        if not args:
            self.send_privmsg(network, target, "Usage: !cron <5_fields>")
            return
        res = explain_cron(" ".join(args))
        self.send_privmsg(network, target, f"[CRON] {res}")

    def _cmd_crypto(self, network: str, target: str, sender: str, args: list) -> None:
        from plugins.toolbot import cipher_codec
        if len(args) < 3:
            self.send_privmsg(network, target, "Usage: !crypto <rot13|base64|hex> <enc|dec> <text>")
            return
        res = cipher_codec(args[0], args[1], " ".join(args[2:]))
        self.send_privmsg(network, target, f"[CRYPTO] {res}")

    def _cmd_tool_search(self, network: str, target: str, sender: str, args: list) -> None:
        query = " ".join(args).lower().strip()
        all_tools = ["dns", "ping", "calc", "port", "hash", "http", "json", "cron", "crypto", "wg", "asn", "cve"]
        matches = [t for t in all_tools if query in t]
        custom_cmds = [c["name"] for c in self.command_editor.list_commands() if query in c["name"]]
        sideloads = [f.stem for f in self.sideload_engine.sideload_dir.glob("*.py") if query in f.stem]

        res = []
        if matches:
            res.append(f"Native: {', '.join(matches)}")
        if custom_cmds:
            res.append(f"Custom: {', '.join(custom_cmds)}")
        if sideloads:
            res.append(f"Sideload: {', '.join(sideloads)}")

        if res:
            self.send_privmsg(network, target, f"[TOOL SEARCH] {' | '.join(res)}")
        else:
            self.send_privmsg(network, target, f"[TOOL SEARCH] No tools matching '{query}' found.")

    def _cmd_tool_info(self, network: str, target: str, sender: str, args: list) -> None:
        t = args[0].lower().strip().lstrip("!")
        docs = {
            "dns": "Resolve hostname to A/AAAA IPs: !dns <host>",
            "ping": "Test TCP port reachability: !ping <host> [port]",
            "calc": "Evaluate safe mathematical expression: !calc <expr>",
            "port": "TCP port connectivity probe: !port <host> <port>",
            "hash": "Cryptographic digest (md5, sha1, sha256, sha512, b64): !hash <algo> <text>",
            "http": "HTTP status code & server header inspector: !http <url>",
            "json": "JSON validator & structural analyzer: !json <expr>",
            "cron": "Parse 5-field crontab expression: !cron <min> <hr> <dom> <mon> <dow>",
            "crypto": "Codec cipher encoder/decoder (rot13, base64, hex): !crypto <codec> <enc|dec> <text>",
            "wg": "WireGuard mesh peer handshake status",
            "asn": "IP reputation & autonomous system lookup: !asn <ip>",
            "cve": "CVE vulnerability enrichment & severity metrics: !cve <CVE-YYYY-NNNN>"
        }
        if t in docs:
            self.send_privmsg(network, target, f"[TOOL INFO: {t.upper()}] {docs[t]}")
            return
        cmd_info = self.command_editor.get_command_info(t)
        if cmd_info:
            self.send_privmsg(network, target, f"[CUSTOM CMD: !{t}] Type: {cmd_info['command_type']} | Author: {cmd_info['author']} | Content: {cmd_info['content'][:120]}")
            return
        self.send_privmsg(network, target, f"[TOOL INFO] Tool '{t}' not found. Type !tool search <query> to discover tools.")

    def _cmd_tool_scaffold(self, network: str, target: str, sender: str, framework: str, name: str) -> None:
        ok, msg = self.sideload_engine.scaffold_tool(framework, name)
        color = "\x0303" if ok else "\x0304"
        self.send_privmsg(network, target, f"[\x02{color}TOOL SCAFFOLD\x0f] {msg}")

    def _cmd_tool_import(self, network: str, target: str, sender: str, url: str, name: str = None) -> None:
        ok, msg = self.sideload_engine.import_plugin(url, name)
        color = "\x0303" if ok else "\x0304"
        self.send_privmsg(network, target, f"[\x02{color}TOOL IMPORT\x0f] {msg}")

    def _cmd_hub(self, network: str, target: str, sender: str, args: List[str]) -> None:
        """Tool catalog and dynamic remote code puller."""
        if not args:
            self.send_privmsg(network, target, "📦 [Tool Hub] Usage: !hub <list [cat]|search <q>|info <tool>|install <tool> [bot]|pull <url> [name] [bot]>")
            return

        sub = args[0].lower()

        if sub in ("list", "ls"):
            cat = args[1].lower() if len(args) > 1 else None
            tools = self.tool_catalog.list_tools(category=cat)
            if not tools:
                self.send_privmsg(network, target, f"📦 [Tool Hub] No tools found in category '{cat}'. Available: info, search, sysadmin, crypto, weather, ai")
                return
            summary = [f"{t['name']} ({t['category']})" for t in tools]
            self.send_privmsg(network, target, f"📦 [Tool Hub Catalog ({len(tools)})] {', '.join(summary)} — Use '!hub info <tool>' for details.")

        elif sub in ("search", "find"):
            if len(args) < 2:
                self.send_privmsg(network, target, "Usage: !hub search <keyword>")
                return
            query = " ".join(args[1:])
            results = self.tool_catalog.search_tools(query)
            if not results:
                self.send_privmsg(network, target, f"📦 [Tool Hub] No tools matching '{query}'.")
                return
            items = [f"{t['name']} ({t['category']}): {t['description'][:50]}..." for t in results]
            self.send_privmsg(network, target, f"🔍 [Hub Search] {(' | '.join(items))[:380]}")

        elif sub in ("info", "show"):
            if len(args) < 2:
                self.send_privmsg(network, target, "Usage: !hub info <tool_name>")
                return
            tname = args[1]
            info = self.tool_catalog.get_tool_info(tname)
            if not info:
                self.send_privmsg(network, target, f"❌ [Tool Hub] Tool '{tname}' not found in catalog.")
                return
            cmds = ", ".join(info.get("commands", [])) or "None"
            passive = ", ".join(info.get("passive", [])) or "None"
            inst = "✓ Installed" if self.tool_catalog.is_installed(tname) else "Not installed"
            self.send_privmsg(network, target, f"📦 [{info['name']} v{info['version']} ({info['category']})] {info['description']}")
            self.send_privmsg(network, target, f"   Commands: !{cmds} | Passive: {passive} | Status: {inst}")

        elif sub in ("install", "i", "get"):
            if is_public_channel(network, target):
                self.send_privmsg(network, target, f"[TOOL HUB] Admin command '{sub}' cannot be used in public channels.")
                activity_buffer.record(network, target, sender, f"!hub {' '.join(args)}", "DENIED_PUBLIC_CHANNEL")
                return
            if not self.dispatcher.is_admin(sender):
                self.send_privmsg(network, target, f"[TOOL HUB] Permission denied: Admin required for '{sub}'.")
                activity_buffer.record(network, target, sender, f"!hub {' '.join(args)}", "DENIED_AUTH")
                return
            if len(args) < 2:
                self.send_privmsg(network, target, "Usage: !hub install <tool_name> [bot_nick]")
                return
            tname = args[1]
            ok, msg = self.tool_catalog.install_tool(tname)
            if not ok:
                self.send_privmsg(network, target, f"❌ [Tool Hub] {msg}")
                return
            self.send_privmsg(network, target, f"✅ [Tool Hub] {msg}")

            if len(args) >= 3:
                bot_nick = args[2]
                mod_name = f"sideload_{tname}"
                if self.manager.attach_module(bot_nick, mod_name):
                    self.send_privmsg(network, target, f"🧩 [Tool Hub] Attached '{mod_name}' to bot '{bot_nick}'.")
                else:
                    self.send_privmsg(network, target, f"⚠️ [Tool Hub] Installed, but bot '{bot_nick}' was not found.")

        elif sub in ("pull", "fetch"):
            if is_public_channel(network, target):
                self.send_privmsg(network, target, f"[TOOL HUB] Admin command '{sub}' cannot be used in public channels.")
                activity_buffer.record(network, target, sender, f"!hub {' '.join(args)}", "DENIED_PUBLIC_CHANNEL")
                return
            if not self.dispatcher.is_admin(sender):
                self.send_privmsg(network, target, f"[TOOL HUB] Permission denied: Admin required for '{sub}'.")
                activity_buffer.record(network, target, sender, f"!hub {' '.join(args)}", "DENIED_AUTH")
                return
            if len(args) < 2:
                self.send_privmsg(network, target, "Usage: !hub pull <url_or_github_blob> [custom_name] [bot_nick]")
                return
            url = args[1]
            c_name = args[2] if len(args) >= 3 and not args[2].startswith("#") else None
            ok, msg = self.sideload_engine.import_plugin(url, c_name)
            if not ok:
                self.send_privmsg(network, target, f"❌ [Tool Hub Pull] {msg}")
                return
            self.send_privmsg(network, target, f"✅ [Tool Hub Pull] {msg}")

            target_bot = args[3] if len(args) >= 4 else (args[2] if len(args) >= 3 and c_name is None else None)
            if target_bot and c_name:
                mod_name = f"sideload_{c_name}"
                self.manager.attach_module(target_bot, mod_name)
                self.send_privmsg(network, target, f"🧩 [Tool Hub] Attached '{mod_name}' to bot '{target_bot}'.")

        else:
            self.send_privmsg(network, target, f"Unknown hub subcommand '{sub}'. Use: !hub list | search | info | install | pull")

    def _cmd_craft(self, network: str, target: str, sender: str, args: List[str]) -> None:
        """Autonomous AI tool crafter."""
        if len(args) < 2:
            self.send_privmsg(network, target, "Usage: !craft <tool_name> <instructions...>")
            return

        tname = args[0]
        instructions = " ".join(args[1:])
        self.send_privmsg(network, target, f"⚙️ [ToolCrafter] Synthesizing tool '{tname}' via LLM...")

        ok, msg = self.tool_crafter.craft_tool(tname, instructions)
        if not ok:
            self.send_privmsg(network, target, f"❌ [ToolCrafter] {msg}")
            return

        self.send_privmsg(network, target, f"✨ [ToolCrafter] {msg}")



    def shutdown(self) -> None:
        """Graceful shutdown."""
        self._running = False
        logger.info("Shutting down logic daemon...")
        if self.ipc_writer:
            self.ipc_writer.close()


async def main():
    config = ConfigManager()
    daemon = MinibotsLogicDaemon(config)

    loop = asyncio.get_running_loop()

    # SIGHUP hot-reload signal handler
    def _sighup_handler():
        daemon.reload_modules()

    # SIGTERM / SIGINT shutdown
    stop_event = asyncio.Event()
    def _sigterm_handler():
        logger.info("Received termination signal. Stopping logic daemon...")
        daemon.shutdown()
        stop_event.set()

    loop.add_signal_handler(signal.SIGHUP, _sighup_handler)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _sigterm_handler)

    run_task = asyncio.create_task(daemon.connect_ipc_and_run())
    await stop_event.wait()
    run_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

