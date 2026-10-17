# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
dispatcher.py — Command Router, Zero-Trust Hostmask Auth, Shorthand Matrix & Addressing Engine.
"""

import fnmatch
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Dispatcher")


class CommandDispatcher:
    """Manages command registration, permission checks, aliases, and invocation."""
    def __init__(self, admin_nicks: Optional[List[str]] = None):
        raw_admins = admin_nicks or ["admin"]
        self.admin_patterns = [a.strip() for a in raw_admins if a.strip()]
        self.admin_nicks = [a.rstrip("*").strip() for a in raw_admins if a.strip()]
        self.commands: Dict[str, Dict[str, Any]] = {}
        self.global_prefixes = ["!", ".", "?"]

    def register(
        self,
        name: str,
        handler: Callable,
        min_ring: int = 1,
        help_text: str = "",
        category: str = "core",
        aliases: Optional[List[str]] = None
    ) -> None:
        """Registers a command handler with metadata, category, and shorthands."""
        cmd_meta = {
            "handler": handler,
            "min_ring": min_ring,
            "help": help_text,
            "category": category,
            "name": name.lower()
        }
        self.commands[name.lower()] = cmd_meta
        if aliases:
            for a in aliases:
                self.commands[a.lower()] = cmd_meta

    def is_admin(self, nick: str) -> bool:
        """Verifies if caller matches admin nicks exactly or via configured glob pattern."""
        if not nick:
            return False
        clean_nick = nick.split("!")[0].strip().lower()
        for pat in self.admin_patterns:
            pat_lower = pat.lower()
            if clean_nick == pat_lower or fnmatch.fnmatchcase(clean_nick, pat_lower):
                return True
        for admin in self.admin_nicks:
            if clean_nick == admin.lower():
                return True
        return False

    def parse_line(
        self,
        bot_nick: str,
        target: str,
        sender_nick: str,
        message: str
    ) -> Optional[Tuple[str, List[str], Dict[str, Any]]]:
        """
        Parses incoming IRC message into command name and arguments.
        Supports:
        1. Global prefix: !command arg1 arg2
        2. Flexible Addressing: bot_nick: <cmd>, bot_nick, <cmd>, @bot_nick <cmd>
        3. Direct PM (target == bot_nick or target not starting with #): command arg1 arg2
        """
        text = message.strip()
        if not text:
            return None

        cmd_name = ""
        args: List[str] = []

        is_pm = not target.startswith("#") or (target.lower() == bot_nick.lower())

        # 1. Check Mention / Direct Addressing (@bot, bot:, bot,)
        clean_bot = re.escape(bot_nick.lstrip("@"))
        nick_match = re.match(rf"^(?:@{clean_bot}|{clean_bot})[:,\s]+\s*(.*)$", text, re.IGNORECASE)
        if nick_match:
            addressed_text = nick_match.group(1).strip()
            parts = addressed_text.split()
            if parts:
                cmd_name = parts[0].lower()
                for p in self.global_prefixes:
                    if cmd_name.startswith(p):
                        cmd_name = cmd_name[len(p):]
                        break
                args = parts[1:]
        elif is_pm:
            # 2. Check Direct PM (supports both prefix and prefix-less)
            parts = text.split()
            if parts:
                cmd_name = parts[0].lower()
                for p in self.global_prefixes:
                    if cmd_name.startswith(p):
                        cmd_name = cmd_name[len(p):]
                        break
                args = parts[1:]
        else:
            # 3. Check Global Prefix (!cmd, .cmd, ?cmd)
            for p in self.global_prefixes:
                if text.startswith(p):
                    parts = text[len(p):].split()
                    if parts:
                        cmd_name = parts[0].lower()
                        args = parts[1:]
                    break

        if cmd_name and cmd_name in self.commands:
            meta = self.commands[cmd_name]
            return meta["name"], args, meta

        return None

    def get_help_summary(self, is_admin: bool = False, is_wireguard: bool = True) -> List[str]:
        """Generates network-adaptive, categorized multi-line help listing."""
        if not is_wireguard:
            # Compact summary for public networks (Libera.Chat / OFTC)
            return [
                "[Minibots] Commands: !help [topic] | !task <prompt> | !calc <expr> | !dns <host> | !port <host> <port> | !usage | !jules | !kaggle | !troll | !sideload (Type !help <topic> for manual)"
            ]

        # Multi-line formatted menu for WireGuard mesh (Strictly max 4 lines)
        admin_extra = " | 🛡️ !join | !part | !say | !ignore | !reload" if is_admin else ""
        lines = [
            f"🤖 [Minibots Core] !help (!h) [topic] | !status (!st) | !ping (!p) | !list (!ls) | !usage (!u){admin_extra}",
            "🧠 [AI & CTI Reasoning] !gr (!granite) <subcmd> | !models | !model <name> | !ai <prompt> | !dai <query> | !task <prompt>",
            "🏗️ [Builder & Sideload] !builder | !hub <list|search|info|install|pull> | !craft <name> <prompt> | !cmd <add|py|begin|paste> | !spawn <nick> <#chan> <mods>",
            "🛠️ [Tools & MCP] !calc (!c) <expr> | !dns (!d) <host> | !port <host> <port> | !mcp <tool> <args> | !kaggle | !jules | !troll"
        ]
        return lines[:4]

    def get_topic_help(self, topic: str, is_admin: bool = False) -> List[str]:
        """Returns bounded, categorized contextual manuals for specific topics or commands (strictly max 4 lines)."""
        t = (topic or "").lower().strip().lstrip("!.")

        # Alias mappings
        if t in ("hub", "toolhub", "pkg", "catalog"):
            return [
                "📦 [Tool Hub & Package Catalog] Discover, install, and pull curated and community bot tools.",
                "• Discover: !hub list [category] | Search: !hub search <keyword> | Info: !hub info <tool>",
                "• Install: !hub install <tool_name> [bot_nick] (installs from catalog to sideload/)",
                "• Remote Pull: !hub pull <url_or_github_blob> [name] [bot_nick] (fetches GitHub/Gist with AST & SSRF guard)"
            ]
        elif t in ("craft", "autotool", "synthesize", "generate"):
            return [
                "✨ [AI Tool Crafter] Autonomous on-the-fly Python bot tool generator using LLMRouter.",
                "• Usage: !craft <tool_name> <instructions of what tool should do>",
                "• Example: !craft dice Simple dice roller with !roll command",
                "• Automatically strips code fences, validates AST syntax, enforces security policy, and saves to sideload/."
            ]
        elif t in ("builder", "build", "framework", "frameworks", "bot", "makebot", "botbuilder"):
            return [
                "🏗️ [Dynamic Bot Builder] Assemble custom bots directly via IRC using 5 Python bot frameworks.",
                "1️⃣ Scaffold: !tool scaffold <sopel|errbot|discord|limnoria|ircbot> <name> | 2️⃣ Import: !tool import <url> [name]",
                "3️⃣ Custom Commands: !cmd py <name> <code> | !cmd begin <name> ... !cmd save | !cmd paste <id>",
                "4️⃣ Multi-Framework Spawn: !spawn <nick> <#chan> toolbot,sideload_<name> (Details: !builder frameworks|examples)"
            ]
        elif t in ("cmd", "command", "customcmd", "editor"):
            return [
                "📝 [In-Channel Custom Command Editor] Create, edit, and run custom bot commands channel-wide.",
                "• Quick Macro & Python: !cmd add <name> <template> | !cmd py <name> <code> (sandboxed AST Python)",
                "• Sessions: !cmd begin <name> -> type lines into channel -> !cmd save | !cmd paste <id> <name>",
                "• Management: !cmd list | !cmd info <name> | !cmd rm <name> (immediately invokable via !<name>)"
            ]
        elif t in ("jules", "julesbot", "coding", "agent"):
            return [
                "🚀 [Jules Coding Agent] Autonomous coding workflow in #jules & branch-first GitHub PR engine.",
                "📋 Workflow: !jules init <label> <repo> <prompt> -> !jules approve <label> -> !jules status <label>",
                "🔍 Inspection: !jules diff <label> | !jules logs <label> | !jules cancel <label> | !jules repos | !jules list"
            ]
        elif t in ("task", "do", "taskbot", "code"):
            return [
                "💻 [TaskBot Coding & Technical Execution] Autonomous code generation on remote GPU worker with Pastebin offload.",
                "• Usage: !task <prompt> (or !do <prompt>)",
                "• Example: !task Write a Python script to compute Fibonacci numbers and measure execution time.",
                "• Channels: Restricted to #sandbox, #dev, #troll, #minibot, #minibots, #offtopic, #research.",
                "• Offload Policy: Multi-line code is uploaded directly to configured Pastebin. Untrusted networks clamp to 2 lines."
            ]
        elif t in ("kaggle", "kg", "gpu", "worker"):
            return [
                "⚡ [Kaggle GPU Burst Worker] On-demand GPU container for LLM inferencing & heavy code execution.",
                "⚙️ Controls: !kaggle launch [model] [--account <name>] [--instance <id>] | !kaggle stop [id] | !kaggle status [id]",
                "🧠 Supported Models: qwen-2.5-coder | gemma-4-uncensored | deepseek-r1-distill | hermes-3",
                "🔄 Hot-Swap & Pooling: !kaggle swap <slot> <model> | !kaggle accounts | !kaggle models"
            ]
        elif t in ("troll", "trollbot", "persona", "chat"):
            return [
                "🎭 [Trollbot Persona Engine] Sliding-window chat context with dynamic system prompt personas.",
                "⚙️ Controls: !troll set <preset> | !troll custom <prompt> | !troll list | !troll reset",
                "🎪 Presets: gemma4_uncensored, cynical_hacker, philosopher_troll, hacker, sarcastic, cve_analyst, uncensored, default"
            ]
        elif t in ("tool", "tools", "toolbot", "mcp", "calc", "dns", "port", "hash", "http", "json", "cron", "crypto"):
            return [
                "🛠️ [Toolbot & FastMCP Tools] Native utilities and dynamic framework tools.",
                "🔍 Discovery & Scaffolding: !tool search <query> | !tool info <name> | !tool scaffold <framework> <name> | !tool import <url> [name]",
                "🔢 Utilities: !calc <expr> | !dns <host> | !port <host> <port> | !hash <algo> <text> | !http <url> | !json <expr> | !cron <expr> | !crypto <codec> <enc|dec> <text>",
                "🔌 FastMCP: !mcp <tool_name> <json_or_kwarg_args> (e.g. !mcp threat_search query=cve)"
            ]
        elif t in ("dai", "ai", "ask", "dair", "ddgai", "ai_search", "model", "models", "daimodel"):
            return [
                "🧠 [DAI Search & Model Hub] Fast DuckDuckGo search & Raspberry Pi 5 local LLM engine.",
                "⚙️ Model Switch: !models — List models | !model <name> — Switch Pi 5 worker model",
                "🔄 Auto-Switch: !model auto [on|off] — Dynamic routing between fast and reasoning models",
                "🤖 Pure Chat: !ai <prompt> — Pure local LLM query/chat without web search",
                "🔬 Deep Reasoning: !dair <query> (or !dai -r) | !dai <query> — AI-enhanced web search"
            ]
        elif t in ("sideload", "sideloading", "plugins", "scripts", "shims"):
            return [
                "🔌 [Universal Sideloading & Multi-Framework Shims] Zero-dependency harness for community bot scripts.",
                "📦 Supported: Sopel (@plugin.command), Errbot (@botcmd), Discord.py (@bot.command), Limnoria (callbacks.Plugin), SingleServerIRCBot",
                "⚙️ Commands: !sideload list | !sideload help <script> | !sideload load <script> | !sideload unload <script>",
                "🚀 Builder: !tool scaffold <framework> <name> | !tool import <url> [name] | Drop .py into /opt/minibots/sideload/"
            ]
        elif t in ("spawn", "bots", "lifecycle", "kill", "attach", "detach", "list", "ps"):
            return [
                "🤖 [Minibot Lifecycle] Dynamic multi-bot orchestration & module hot-swapping.",
                "⚙️ Commands: !spawn <nick> <#chan> <modules> | !kill <nick> | !attach <nick> <mod> | !detach <nick> <mod>",
                "📦 Modules: julesbot, toolbot, trollbot, urls, brbot, or any sideloaded script"
            ]
        elif t in ("telemetry", "usage", "quota", "stats", "u"):
            return [
                "📊 [Telemetry & Token Analytics] 24h token consumption, latency telemetry & audit logs.",
                "⚙️ Commands: !usage (!u) (summary) | !logs [N] (recent activity traces) | !cleanup [days] (prune DB)"
            ]
        elif t in ("granite", "gr", "threat", "cti", "actor", "campaign", "mitre", "triage", "cve"):
            return [
                "🛡️ [IBM Granite CTI Analyst] Local AI cybersecurity & IT operations reasoning over Lertz telemetry.",
                "• Triage & Forensics: !gr actor <IP> [--8b] | !gr triage <IP> | !gr mitre <IP> | !gr cve <ID>",
                "• Telemetry: !gr campaign [H] | !gr brief [H] | !gr ask <question> | !gr status",
                "• Engines: Default fast 3B (~3-5s response) | Deep 8B on-demand (--8b or !gr switch 8b|3b)"
            ]
        elif t in ("ignore", "ignorebot", "urlignore"):
            if not is_admin:
                return ["🛡️ [Admin Control] Administrative commands require authorized admin credentials."]
            return [
                "🔇 [Dynamic URL Ignore Filter] Suppress duplicate link previews from sibling bots & bridges.",
                "• Add pattern: !ignore add <nick_or_pattern> (e.g. !ignore add relay* or !ignore add testbot)",
                "• Remove: !ignore del <nick_or_pattern> | List: !ignore list",
                "• Persists across reloads in data/ignored_url_nicks.json"
            ]
        elif t in ("admin", "control", "join", "part", "say", "raw", "reload"):
            if not is_admin:
                return ["🛡️ [Admin Control] Administrative commands require authorized admin credentials."]
            return [
                "🛡️ [Administrative Commands] Sudo/admin channel operations and IPC control.",
                "⚙️ Commands: !join <#chan> | !part <#chan> | !say <target> <msg> | !raw <line> | !ignore <add|del|list> | !reload (!r)"
            ]
        else:
            # Check if command exists in registered command table
            if t in self.commands:
                cmd_meta = self.commands[t]
                return [
                    f"📖 [Command: !{cmd_meta['name']}] Category: {cmd_meta.get('category', 'core')} | Ring: {cmd_meta.get('min_ring', 1)}",
                    f"ℹ️ Description: {cmd_meta.get('help', 'No description available.')}"
                ]
            return [
                f"❓ Unknown help topic '{topic}'. Available topics: task, kaggle, troll, jules, builder, cmd, tools, frameworks, sideload, lifecycle, telemetry, admin",
                "Type !help for the full system command overview or !builder for the bot building guide."
            ]
