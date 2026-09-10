#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
keystore.py — Canonical Secret Loader & Protection for the Minibots Ecosystem.
Reads credentials from kernel keyrings (keyctl), systemd transient memory (LoadCredential),
and local ~/.secrets.json with strict permission guards (0600).
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("Keystore")

SECRETS_CACHE: Dict[str, str] = {}
SECRETS_LOADED: bool = False


def _strip_ansi(text: str) -> str:
    """Strips ANSI escape sequences before regex parsing."""
    return re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-9;]*[a-zA-Z])', '', text)


def _load_from_keyring(key_name: str) -> Optional[str]:
    """Attempts to fetch secret from Linux kernel user keyring (@u)."""
    try:
        res = subprocess.run(["keyctl", "search", "@u", "user", key_name], capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            key_id = res.stdout.strip()
            read_res = subprocess.run(["keyctl", "read", key_id], capture_output=True, text=True, timeout=2)
            if read_res.returncode == 0:
                val = read_res.stdout.strip()
                if val:
                    return val
    except Exception:
        pass
    return None


def _load_from_systemd_creds(key_name: str) -> Optional[str]:
    """Attempts to fetch secret from systemd LoadCredential directory."""
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir and os.path.isdir(cred_dir):
        cred_path = os.path.join(cred_dir, key_name)
        if os.path.isfile(cred_path):
            try:
                with open(cred_path, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                pass
    return None


def init_keystore() -> None:
    """Initializes and caches secrets from all available zero-trust vaults."""
    global SECRETS_CACHE, SECRETS_LOADED
    if SECRETS_LOADED:
        return

    # 1. Check local .secrets.json in parent or current dir
    potential_files = [
        Path.home() / ".secrets.json",
        Path.home() / ".config" / "minibots" / "secrets.json",
        Path(__file__).resolve().parent / ".secrets.json",
        Path(__file__).resolve().parents[1] / ".secrets.json",
    ]

    for p in potential_files:
        if p.is_file():
            try:
                # Ensure 0600 permissions
                mode = p.stat().st_mode & 0o777
                if mode != 0o600 and os.name != 'nt':
                    try:
                        p.chmod(0o600)
                    except Exception:
                        pass
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        SECRETS_CACHE.update(data)
            except Exception as e:
                logger.debug(f"Could not load secrets from {p}: {e}")

    SECRETS_LOADED = True


def get_secret(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    Retrieves secret value checking:
    1. Linux kernel keyring (@u)
    2. systemd LoadCredential
    3. OS Environment variable
    4. Cached .secrets.json
    """
    init_keystore()

    # Keyring check
    keyring_val = _load_from_keyring(key)
    if keyring_val:
        return keyring_val

    # systemd check
    sd_val = _load_from_systemd_creds(key)
    if sd_val:
        return sd_val

    # Environment variable check
    env_val = os.getenv(key)
    if env_val is not None:
        return env_val

    # Cache check
    return SECRETS_CACHE.get(key, default)


def mask_secret(val: Optional[str], show_chars: int = 4) -> str:
    """Safely masks secret for log visibility."""
    if not val:
        return "[EMPTY]"
    if len(val) <= show_chars * 2:
        return "********"
    return f"{val[:show_chars]}...{val[-show_chars:]}"
