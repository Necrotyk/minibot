# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
config.py — Configuration Loader with Keystore Variable Expansion.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import keystore

logger = logging.getLogger("Config")

DEFAULT_CONFIG: Dict[str, Any] = {
    "PRIMARY_NETWORK": os.environ.get("PRIMARY_NETWORK", "local"),
    "ADMIN_NETWORKS": [n.strip() for n in os.environ.get("ADMIN_NETWORKS", "local").split(",") if n.strip()],
    "ADMIN_CHANNELS": [c.strip() for c in os.environ.get("ADMIN_CHANNELS", "#minibot,#minibots,#dev").split(",") if c.strip()],
    "AUTO_SPAWN_BOTS": [],
    "INTERNAL_SERVER": os.environ.get("INTERNAL_SERVER", "127.0.0.1"),
    "ADMIN_NICKS": [n.strip() for n in os.environ.get("ADMIN_NICKS", "admin*").split(",") if n.strip()],
    "SERVERS": {
        "local": {
            "host": os.environ.get("IRC_HOST", "127.0.0.1"),
            "port": int(os.environ.get("IRC_PORT", "6667")),
            "nick": os.environ.get("IRC_NICK", "minibot"),
            "channels": [c.strip() for c in os.environ.get("IRC_CHANNELS", "#minibot,#dev").split(",") if c.strip()],
            "ssl": os.environ.get("IRC_SSL", "false").lower() in ("true", "1", "yes"),
            "trusted": True,
            "tls_verify": False
        }
    },
    "PASTEBIN_URL": os.environ.get("PASTEBIN_URL", "http://127.0.0.1:8000"),
    "JULES": {
        "API_KEY": os.environ.get("JULES_API_KEY", ""),
        "DEFAULT_REPO": os.environ.get("JULES_DEFAULT_REPO", "owner/repo")
    },
    "LLM": {
        "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", ""),
        "LOCAL_LLM_URL": os.environ.get("LOCAL_LLM_URL", "http://127.0.0.1:8080/v1"),
        "KAGGLE_GPU_URL": os.environ.get("KAGGLE_GPU_URL", ""),
        "MCP_SERVER_URL": os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8050/sse")
    },
    "MATRIX": {
        "ENABLED": False,
        "HOMESERVER_URL": os.environ.get("MATRIX_HOMESERVER_URL", "https://matrix.example.org"),
        "USER_ID": os.environ.get("MATRIX_USER_ID", "@minibot:example.org"),
        "ACCESS_TOKEN": os.environ.get("MATRIX_ACCESS_TOKEN", "")
    }
}


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expands ${VAR_NAME} using keystore and environment variables."""
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_expand_env_vars(v) for v in obj]
    elif isinstance(obj, str):
        def _get_val(m):
            key = m.group(1)
            val = keystore.get_secret(key) or os.getenv(key)
            return val if val is not None else m.group(0)
        return re.sub(r'\$\{([^}]+)\}', _get_val, obj)
    return obj


class ConfigManager:
    """Manages configuration lifecycle and parameter retrieval."""
    def __init__(self, config_path: Optional[str] = None):
        self.base_dir = Path(__file__).resolve().parents[1]
        self.config_path = config_path or os.environ.get("MINIBOTS_CONFIG") or str(self.base_dir / "config.json")
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        """Loads config.json or falls back to config.example.json with dynamic expansion."""
        loaded_data = {}
        target_path = Path(self.config_path)

        if not target_path.is_file():
            # Fallback to example configuration
            target_path = self.base_dir / "config.example.json"

        if target_path.is_file():
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    loaded_data = json.load(f)
            except Exception as e:
                logger.error(f"Failed to read config from {target_path}: {e}")

        merged = DEFAULT_CONFIG.copy()
        merged.update(loaded_data)
        self.data = _expand_env_vars(merged)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def admin_nicks(self) -> List[str]:
        return self.data.get("ADMIN_NICKS", ["admin*"])

    @property
    def servers(self) -> Dict[str, Any]:
        return self.data.get("SERVERS", {})

    @property
    def jules(self) -> Dict[str, Any]:
        return self.data.get("JULES", {})

    @property
    def llm(self) -> Dict[str, Any]:
        return self.data.get("LLM", {})

    @property
    def matrix(self) -> Dict[str, Any]:
        return self.data.get("MATRIX", {})

    @property
    def primary_network(self) -> str:
        return self.data.get("PRIMARY_NETWORK", "local")

    @property
    def admin_networks(self) -> List[str]:
        return self.data.get("ADMIN_NETWORKS", ["local"])

    @property
    def admin_channels(self) -> List[str]:
        return self.data.get("ADMIN_CHANNELS", ["#minibot", "#minibots", "#dev"])

    def is_trusted_network(self, network: str) -> bool:
        if not network:
            return False
        net = str(network).lower().strip()
        admin_nets = [str(n).lower().strip() for n in self.admin_networks]
        if net in admin_nets:
            return True
        srv = self.servers.get(network, {})
        if not srv:
            for k, v in self.servers.items():
                if k.lower() == net:
                    srv = v
                    break
        if srv.get("trusted", False):
            return True
        if net.startswith("local") or net.startswith("wireguard") or net == "wg":
            return True
        return False
