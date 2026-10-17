# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
tool_catalog.py — Curated Tool Registry & Package Catalog for Dystopi Minibots.
Enables discovery, metadata inspection, and zero-touch installation of tool scripts.
"""

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ToolCatalog")

CATALOG_TOOLS: Dict[str, Dict[str, Any]] = {
    "infobot": {
        "name": "infobot",
        "description": "Universal URL & YouTube previewer (views, duration, description), Wikipedia/Grokipedia search, and multi-language translation.",
        "category": "info",
        "framework": "generic",
        "commands": ["wiki", "wikipedia", "grok", "grokipedia", "grokpedia", "tr", "translate", "help"],
        "passive": ["URL preview (SSRF safe)", "YouTube preview (mobile, desktop, shorts, youtu.be)"],
        "author": "Minibots Core Team",
        "version": "1.0.0",
        "source_file": "infobot.py"
    },
    "web_search": {
        "name": "web_search",
        "description": "Fast multi-source web search: DuckDuckGo instant answers, Hacker News stories, GitHub repo lookup, and Reddit posts.",
        "category": "search",
        "framework": "sopel",
        "commands": ["ddg", "hn", "gh", "r"],
        "passive": [],
        "author": "Minibots Core Team",
        "version": "1.0.0",
        "source_file": "web_search.py"
    },
    "sysadmin_tools": {
        "name": "sysadmin_tools",
        "description": "Network & sysadmin toolkit: DNS dig (A, AAAA, MX, TXT, NS), SSL certificate inspector, TCP port check, and WHOIS/RDAP.",
        "category": "sysadmin",
        "framework": "sopel",
        "commands": ["dig", "ssl", "port", "whois", "ip"],
        "passive": [],
        "author": "Minibots Core Team",
        "version": "1.0.0",
        "source_file": "sysadmin_tools.py"
    },
    "crypto_calc": {
        "name": "crypto_calc",
        "description": "Cryptographic and encoding utilities: Base/hex/b64, MD5/SHA hashes, JWT claim inspector, UUID generator, and unit conversion.",
        "category": "crypto",
        "framework": "sopel",
        "commands": ["hex", "unhex", "b64", "unb64", "bin", "hash", "jwt", "uuid", "convert"],
        "passive": [],
        "author": "Minibots Core Team",
        "version": "1.0.0",
        "source_file": "crypto_calc.py"
    },
    "weather_time": {
        "name": "weather_time",
        "description": "Live weather reports via wttr.in and global timezone / world clock lookups.",
        "category": "weather",
        "framework": "sopel",
        "commands": ["w", "weather", "time", "tz"],
        "passive": [],
        "author": "Minibots Core Team",
        "version": "1.0.0",
        "source_file": "weather_time.py"
    },
    "ai_assistant": {
        "name": "ai_assistant",
        "description": "Direct AI queries, text summarization, and code explanations routed through Minibots LLMRouter.",
        "category": "ai",
        "framework": "sopel",
        "commands": ["ai", "ask", "summarize", "explain"],
        "passive": [],
        "author": "Minibots Core Team",
        "version": "1.0.0",
        "source_file": "ai_assistant.py"
    }
}


class ToolCatalog:
    """Registry manager for discovering, inspecting, and installing curated minibot tools."""

    def __init__(self, catalog_dir: Optional[Path] = None, sideload_dir: Optional[Path] = None):
        base_dir = Path(__file__).resolve().parents[1]
        self.catalog_dir = catalog_dir or (base_dir / "sideload")
        self.sideload_dir = sideload_dir or (base_dir / "sideload")
        self.tools = CATALOG_TOOLS

    def list_tools(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns catalog tools, optionally filtered by category."""
        res = []
        cat_lower = category.lower().strip() if category else None
        for meta in self.tools.values():
            if cat_lower and meta.get("category", "").lower() != cat_lower:
                continue
            res.append(meta)
        return sorted(res, key=lambda x: x["name"])

    def search_tools(self, query: str) -> List[Dict[str, Any]]:
        """Fuzzy keyword search matching tool name, description, commands, or category."""
        q = query.lower().strip()
        if not q:
            return self.list_tools()

        results = []
        for meta in self.tools.values():
            name = meta.get("name", "").lower()
            desc = meta.get("description", "").lower()
            cat = meta.get("category", "").lower()
            cmds = [c.lower() for c in meta.get("commands", [])]

            if q in name or q in desc or q in cat or any(q in c for c in cmds):
                results.append(meta)

        return sorted(results, key=lambda x: x["name"])

    def get_tool_info(self, name: str) -> Optional[Dict[str, Any]]:
        """Retrieves structured metadata for a specific tool."""
        clean_name = name.lower().strip().replace("sideload_", "").replace(".py", "")
        return self.tools.get(clean_name)

    def is_installed(self, name: str) -> bool:
        """Checks whether the tool source file exists in the active sideload directory."""
        clean_name = name.lower().strip().replace("sideload_", "").replace(".py", "")
        target = self.sideload_dir / f"{clean_name}.py"
        return target.is_file()

    def install_tool(self, name: str) -> Tuple[bool, str]:
        """Copies or verifies the curated tool file into the sideload directory."""
        clean_name = name.lower().strip().replace("sideload_", "").replace(".py", "")
        meta = self.tools.get(clean_name)
        if not meta:
            return False, f"Tool '{name}' not found in catalog. Use '!hub list' to see available tools."

        source_file = self.catalog_dir / meta["source_file"]
        target_file = self.sideload_dir / meta["source_file"]

        if target_file.is_file():
            return True, f"Tool '{clean_name}' is already installed in sideload/ ({target_file.name})."

        if not source_file.is_file():
            return False, f"Source file '{source_file.name}' not found in catalog repository."

        try:
            self.sideload_dir.mkdir(parents=True, exist_ok=True)
            if source_file.resolve() != target_file.resolve():
                shutil.copy2(source_file, target_file)
            return True, f"Successfully installed '{clean_name}' to sideload/. Ready to load or spawn."
        except Exception as e:
            logger.error(f"Failed to install tool {name}: {e}")
            return False, f"Failed to install tool '{name}': {e}"
