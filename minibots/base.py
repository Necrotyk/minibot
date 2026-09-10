# SPDX-License-Identifier: AGPL-3.0-or-later
"""
base.py — MiniBot Base Class with Dynamic Module Management & Event Routing.
"""

import logging
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Set

from core.dispatcher import CommandDispatcher

logger = logging.getLogger("MiniBotBase")


KNOWN_MODULE_HELP: Dict[str, Dict[str, Any]] = {
    "dai": {
        "title": "🧠 \x02DAI Search & Reasoning\x0f",
        "summary": "\x0311.dai\x0f <query> | \x0311.ai\x0f <prompt> | \x0311.dair\x0f <query> (deep reasoning)",
        "triggers": ["dai", "ai", "ask", "dair", "ddgai", "ai_search"],
        "details": [
            "• \x02\x0311.dai <query>\x0f — Fast AI-enhanced DuckDuckGo search & synthesis (also !dai)",
            "• \x02\x0311.ai <prompt>\x0f — Pure local LLM chat without web search (also !ai)",
            "• \x02\x0311.dair <query>\x0f — Deep 3-phase deductive reasoning pass",
            "• \x02Topical Continuity\x0f — Automatically links multi-user context within channel"
        ]
    },
    "lertz_threat_intel": {
        "title": "🛡️ \x02IBM Granite CTI Analyst\x0f",
        "summary": "\x0311!gr\x0f <actor|campaign|mitre|triage|cve|brief|ask> [--8b]",
        "triggers": ["lertz_threat_intel", "threat_intel", "granite", "gr", "threat", "cti"],
        "details": [
            "• \x02\x0311!gr actor <IP> [--8b]\x0f — Fingerprints threat actor, scanner tool & intent",
            "• \x02\x0311!gr campaign [H]\x0f — Multi-IP clusters & sweep detection",
            "• \x02\x0311!gr mitre <IP>\x0f — Maps probes to MITRE ATT&CK tactics",
            "• \x02\x0311!gr triage <IP>\x0f — Cybernetic triage verdict | \x02\x0311!gr cve <ID>\x0f — CVE impact"
        ]
    },
    "web_search": {
        "title": "🔍 \x02Web Search\x0f",
        "summary": "\x0311!ddg\x0f <query> | \x0311!hn\x0f [N] | \x0311!gh\x0f <repo> | \x0311!r\x0f <subreddit>",
        "triggers": ["web_search", "search", "ddg", "duck", "hn", "hackernews", "gh", "github", "r", "reddit"],
        "details": [
            "• \x02\x0311!ddg <query>\x0f — Instant answers and web links via DuckDuckGo",
            "• \x02\x0311!gh <owner/repo>\x0f — GitHub repo info | \x02\x0311!hn [N]\x0f — Top Hacker News stories",
            "• \x02\x0311!r <subreddit>\x0f — Top hot post from a subreddit (e.g. !r netsec)"
        ]
    },
    "infobot": {
        "title": "📖 \x02Info & Media\x0f",
        "summary": "\x0311!wiki\x0f <query> | \x0311!grok\x0f <query> | \x0311!tr\x0f [lang] <text> | URLs & YouTube",
        "triggers": ["infobot", "info", "wiki", "wikipedia", "grok", "grokipedia", "grokpedia", "tr", "translate", "youtube", "yt", "url", "urls"],
        "details": [
            "• \x02\x0311!wiki <query>\x0f — Wikipedia summary | \x02\x0311!grok <query>\x0f — Grokipedia knowledge",
            "• \x02\x0311!tr [lang] <text>\x0f — Multi-language translation (e.g. !tr es Hello world)",
            "• \x02Auto-Preview\x0f — YouTube (views, duration, description) and URL title cards"
        ]
    },
    "weather_time": {
        "title": "⛅ \x02Weather & Time\x0f",
        "summary": "\x0311!w\x0f <location> | \x0311!time\x0f [timezone]",
        "triggers": ["weather_time", "weather", "w", "time", "tz"],
        "details": [
            "• \x02\x0311!w <location>\x0f — Live weather conditions from wttr.in (e.g. !w Seattle)",
            "• \x02\x0311!time [tz/loc]\x0f — Current UTC time or timezone offset (e.g. !time est)"
        ]
    },
    "crypto_calc": {
        "title": "🔢 \x02Crypto & Conversion\x0f",
        "summary": "\x0311!hex\x0f | \x0311!unhex\x0f | \x0311!b64\x0f | \x0311!unb64\x0f | \x0311!hash\x0f | \x0311!jwt\x0f | \x0311!uuid\x0f | \x0311!convert\x0f",
        "triggers": ["crypto_calc", "crypto", "hex", "unhex", "b64", "base64", "unb64", "hash", "jwt", "uuid", "convert"],
        "details": [
            "• \x02\x0311!hex / !unhex\x0f | \x02\x0311!b64 / !unb64 <text>\x0f — Hex and Base64 codecs",
            "• \x02\x0311!hash [algo] <text>\x0f — md5/sha1/sha256 | \x02\x0311!jwt <token>\x0f — Validate JWT",
            "• \x02\x0311!uuid\x0f — UUIDv4 generator | \x02\x0311!convert <val> <from> to <to>\x0f — Units/FX"
        ]
    },
    "sysadmin_tools": {
        "title": "💻 \x02Sysadmin Tools\x0f",
        "summary": "\x0311!dig\x0f <host> | \x0311!ssl\x0f <host> | \x0311!port\x0f <host> <port> | \x0311!whois\x0f <domain>",
        "triggers": ["sysadmin_tools", "sysadmin", "dig", "dns", "ssl", "cert", "port", "whois", "rdap"],
        "details": [
            "• \x02\x0311!dig <host> [type]\x0f — DNS query | \x02\x0311!whois <domain>\x0f — WHOIS/RDAP query",
            "• \x02\x0311!ssl <host>\x0f — Certificate expiry | \x02\x0311!port <host> <port>\x0f — TCP test"
        ]
    },
    "ai_assistant": {
        "title": "🤖 \x02AI Assistant\x0f",
        "summary": "\x0311!ai\x0f <prompt> | \x0311!summarize\x0f <text> | \x0311!explain\x0f <code>",
        "triggers": ["ai_assistant", "ai", "ask", "summarize", "explain"],
        "details": [
            "• \x02\x0311!ai <prompt>\x0f — Queries LLM for reasoning and answers",
            "• \x02\x0311!summarize <text>\x0f — Summarizes text | \x02\x0311!explain <code>\x0f — Explains code"
        ]
    },
    "toolbot": {
        "title": "🛠️ Tools",
        "summary": "!calc <expr> | !dns <host> | !port <host> <port> | !mcp <tool> <args>",
        "triggers": ["toolbot", "calc", "mcp"],
        "details": [
            "• !calc <expr> — Math evaluator | !mcp <tool> <args> — FastMCP execution",
            "• !dns <host> — Resolve DNS hostname | !port <host> <port> — Test TCP port"
        ]
    },
    "urls": {
        "title": "🔗 URLs",
        "summary": "Automatic SSRF-guarded title extraction for posted links",
        "triggers": ["urls", "url"],
        "details": [
            "• Auto-previews web links with SSRF protection"
        ]
    },
    "julesbot": {
        "title": "🚀 Jules",
        "summary": "!jules init | approve | status | diff | logs",
        "triggers": ["julesbot", "jules"],
        "details": [
            "• !jules init <label> <repo> <prompt> — Start coding agent session",
            "• !jules approve <label> | status <label> — Approve PR / check job state",
            "• !jules diff <label> | logs <label> — Inspect code patch diff & logs"
        ]
    },
    "trollbot": {
        "title": "🎭 Troll",
        "summary": "!troll set <preset> | custom <prompt> | list | reset",
        "triggers": ["trollbot", "troll"],
        "details": [
            "• !troll set <preset> | custom <prompt> — Change persona preset or prompt",
            "• !troll list | reset — List available presets or reset to default"
        ]
    }
}


class MiniBot:
    """Represents a lightweight, isolated IRC bot instance."""
    def __init__(
        self,
        nick: str,
        network: str,
        channels: List[str],
        creator_nick: str = "admin",
        raw_send_callback: Optional[Callable[[str, str], None]] = None,
        config: Optional[Dict[str, Any]] = None,
        on_channels_updated: Optional[Callable[[Any], None]] = None
    ):
        self.nick = nick
        self.network = network
        self.channels = channels.copy()
        self.creator_nick = creator_nick
        self.raw_send_callback = raw_send_callback
        self.config = config or {}
        self.on_channels_updated = on_channels_updated
        self.manager: Any = None

        self.plugins: Dict[str, Any] = {}
        self.active_modules: Set[str] = set()
        self.is_running = True
        self.lock = threading.RLock()

    def send_msg(self, target: str, message: str) -> None:
        """Sends PRIVMSG to target channel or user."""
        # IRC line boundary guard (380 bytes max per line)
        for line in message.splitlines():
            clean_line = line.strip()
            if not clean_line:
                continue
            truncated = clean_line[:380]
            self.raw_send(f"PRIVMSG {target} :{truncated}")

    def raw_send(self, raw_line: str) -> None:
        """Emits raw line through the connection broker."""
        if self.raw_send_callback:
            self.raw_send_callback(self.nick, raw_line)
        else:
            logger.debug(f"[{self.nick} RAW] {raw_line}")

    def join(self, channel: str) -> None:
        """Joins target channel."""
        with self.lock:
            if channel not in self.channels:
                self.channels.append(channel)
                if self.on_channels_updated:
                    self.on_channels_updated(self)
        self.raw_send(f"JOIN {channel}")

    def part(self, channel: str, reason: str = "Leaving") -> None:
        """Parts target channel."""
        with self.lock:
            if channel in self.channels:
                self.channels.remove(channel)
                if self.on_channels_updated:
                    self.on_channels_updated(self)
        self.raw_send(f"PART {channel} :{reason}")

    def enable_module(self, name: str, plugin_instance: Any) -> None:
        """Attaches a dynamic plugin or sideloaded adapter."""
        with self.lock:
            self.plugins[name] = plugin_instance
            self.active_modules.add(name)
        logger.info(f"[{self.nick}] Enabled module: {name}")

    def disable_module(self, name: str) -> None:
        """Detaches and stops an active module."""
        with self.lock:
            plugin = self.plugins.pop(name, None)
            self.active_modules.discard(name)
        if plugin and hasattr(plugin, "teardown"):
            try:
                plugin.teardown()
            except Exception as e:
                logger.error(f"[{self.nick}] Error during teardown of {name}: {e}")
        logger.info(f"[{self.nick}] Disabled module: {name}")

    def get_help_lines(self, topic: Optional[str] = None) -> List[str]:
        """Generates concise, non-flooding help for all active modules or a specific topic (strictly max 4 lines)."""
        active_clean = {}
        for m in self.active_modules:
            clean = m.replace("sideload_", "").replace("sideload:", "").lower()
            active_clean[clean] = m

        if topic:
            t = topic.lower().strip().lstrip("!.")
            # If topic refers to this bot itself (e.g. !help toolz), show full bot menu
            if t != self.nick.lower():
                for clean_name, orig_mod in active_clean.items():
                    meta = KNOWN_MODULE_HELP.get(clean_name)
                    if meta and (t == clean_name or t in meta.get("triggers", [])):
                        lines = [f"⚙️ [\x02\x0310{self.nick}\x0f • {meta['title']}]"]
                        lines.extend(meta.get("details", []))
                        return lines[:4]

                    # Dynamic plugin command check
                    plugin = self.plugins.get(orig_mod)
                    if plugin and hasattr(plugin, "get_commands"):
                        cmds = plugin.get_commands()
                        if t == clean_name or t in cmds:
                            return [
                                f"🔌 [\x02\x0310{self.nick}\x0f • \x02{clean_name}\x0f]",
                                f"• Available commands: {', '.join(f'!{c}' for c in cmds)}"
                            ][:4]

                return [f"❌ [{self.nick}] No help topic found for '{topic}'. Active: {', '.join(sorted(active_clean.keys()))}"][:4]

        # Full Help Menu for all active modules (Strictly max 4 lines total)
        clean_list = ", ".join(sorted(active_clean.keys())) or "none"
        header = f"🤖 [\x02\x0310{self.nick}\x0f] Active: \x0311{clean_list}\x0f | {self.nick}: status | modules"

        module_items = []
        for clean_name, orig_mod in sorted(active_clean.items()):
            meta = KNOWN_MODULE_HELP.get(clean_name)
            if meta:
                module_items.append(f"{meta['title']}: {meta['summary']}")
            else:
                plugin = self.plugins.get(orig_mod)
                if plugin and hasattr(plugin, "get_commands"):
                    cmds = plugin.get_commands()
                    if cmds:
                        cmds_str = " | ".join(f"\x0311!{c}\x0f" for c in cmds)
                        module_items.append(f"🔌 \x02[{clean_name}]\x0f {cmds_str}")
                    else:
                        module_items.append(f"🔌 \x02[{clean_name}]\x0f Active")
                else:
                    module_items.append(f"🔌 \x02[{clean_name}]\x0f Active")

        if not module_items:
            return [
                header,
                "No command modules loaded. Use !status or !modules for bot state."
            ]

        lines = [header]
        if len(module_items) <= 3:
            for item in module_items:
                lines.append(item)
        else:
            import math
            n = len(module_items)
            chunk_size = math.ceil(n / 3)
            for i in range(0, n, chunk_size):
                chunk = module_items[i:i + chunk_size]
                lines.append("  •  ".join(chunk))

        return lines[:4]

    def on_privmsg(self, target: str, sender_nick: str, message: str, hostmask: str = "") -> None:
        """Dispatches incoming PRIVMSG to all active plugins and handles direct addressing."""
        text = message.strip()
        bot_nick = self.nick.lower()
        is_dm = not target.startswith("#") or (target.lower() == bot_nick)

        # Check for direct addressing in channels: bot_nick: <cmd>, @bot_nick <cmd>, bot_nick, <cmd>
        clean_bot = re.escape(bot_nick)
        addressed_match = re.match(rf"^(?:@{clean_bot}|{clean_bot})[:,\s]+\s*(.*)$", text, re.IGNORECASE)

        if is_dm:
            if self._handle_bot_cmd(target, sender_nick, text, is_dm=True):
                return
        elif addressed_match:
            sub_cmd = addressed_match.group(1).strip()
            if self._handle_bot_cmd(target, sender_nick, sub_cmd, is_dm=False):
                return
        elif any(text.lower().startswith(p) for p in ("!help", ".help", "?help")):
            parts = text.split()
            topic = parts[1] if len(parts) > 1 else None
            # In shared public channels with an active manager, suppress generic !help
            # so the central daemon handles it without multi-bot flooding.
            if not is_dm and self.manager:
                other_bots = {b.lower() for b in getattr(self.manager, "active_bots", {}).keys()} - {bot_nick}
                if topic and topic.lower() in other_bots:
                    return
                # Suppress bare and unaddressed help so central daemon responds without duplicate output
                if not topic or topic.lower() != bot_nick:
                    return

            reply_dest = sender_nick if is_dm else target
            for line in self.get_help_lines(topic=topic)[:4]:
                self.send_msg(reply_dest, line)
            return

        with self.lock:
            active_plugins = list(self.plugins.values())

        for plugin in active_plugins:
            if hasattr(plugin, "on_privmsg") and callable(plugin.on_privmsg):
                try:
                    import inspect
                    sig = inspect.signature(plugin.on_privmsg)
                    if "hostmask" in sig.parameters:
                        plugin.on_privmsg(target, sender_nick, message, hostmask=hostmask)
                    else:
                        plugin.on_privmsg(target, sender_nick, message)
                except Exception as e:
                    logger.error(f"[{self.nick}] Error in plugin {type(plugin).__name__}: {e}")

    def _handle_bot_cmd(self, target: str, sender: str, text: str, is_dm: bool = False) -> bool:
        """Handles commands addressed to this specific bot instance."""
        parts = text.strip().split()
        if not parts:
            return False

        cmd = parts[0].lower().lstrip("!.")
        args = parts[1:]
        is_creator = sender.lower() == self.creator_nick.lower() or sender.lower() in ("admin", "admin*")
        reply_dest = sender if is_dm else target

        if cmd == "help":
            topic = args[0] if args else None
            for line in self.get_help_lines(topic=topic)[:4]:
                self.send_msg(reply_dest, line)
            return True
        elif cmd == "status":
            mods = ", ".join(self.active_modules) or "none"
            chans = ", ".join(self.channels) or "none"
            self.send_msg(reply_dest, f"[{self.nick}] Status: Online | Chans: {chans} | Modules: {mods}")
            return True
        elif cmd == "modules":
            mods = ", ".join(self.active_modules) or "none"
            self.send_msg(reply_dest, f"[{self.nick}] Active Modules: {mods}")
            return True
        elif is_creator:
            if cmd in ("join", "part", "say"):
                from core.guard import is_public_channel
                if is_public_channel(self.network, target):
                    self.send_msg(target, f"[{self.nick}] Admin commands cannot be used in public channels.")
                    return True

            if cmd == "join" and args:
                self.join(args[0])
                self.send_msg(reply_dest, f"Joined {args[0]}")
                return True
            elif cmd == "part" and args:
                self.part(args[0])
                self.send_msg(reply_dest, f"Parted {args[0]}")
                return True
            elif cmd == "say" and len(args) >= 2:
                target_chan = args[0]
                msg_body = " ".join(args[1:])
                self.send_msg(target_chan, msg_body)
                return True
        return False

    def stop(self) -> None:
        """Gracefully halts minibot and unloads all plugins."""
        self.is_running = False
        with self.lock:
            for name in list(self.plugins.keys()):
                self.disable_module(name)
        self.raw_send(f"QUIT :Minibot terminated by manager")
